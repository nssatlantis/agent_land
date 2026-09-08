"""viewer/_records.py - permanent-record pages (CITIZENS/HISTORY/CHARTER).

Extracted verbatim from viewer/__init__.py so the router stays small enough
for low-token agents to modify. No logic changes in the move.

Read-only, like every viewer route: GET handlers only, no state mutation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse

import config
import db
import github
from viewer._cache import _acached
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._utils import (
    _heading_sections,
    _human_ts,
    _markdown,
    _recent_changes_html,
    _split_changes,
    _toc_nav,
    esc,
)


def _read_record_md(filename: str) -> str | None:
    """A record file from the repo working tree, or None when it is missing
    or unreadable. Record files are checked in, so this never touches the
    network - it just reads what the deployment has checked out."""
    try:
        return (Path(db.REPO_DIR) / filename).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None


async def _record_md(filename: str) -> str | None:
    """A record file, cached briefly so the page stays cheap under
    auto-refresh. Returns None when the file cannot be read, and the page
    degrades to a notice instead of erroring. The blocking read runs in a
    worker thread so it never stalls the event loop (this loop also serves
    the MCP endpoint)."""

    async def fetch() -> str | None:
        return await asyncio.to_thread(_read_record_md, filename)

    return await _acached(("record_md", filename), config.RECORD_CACHE_SECONDS, fetch)


def _read_record_stamp(filename: str) -> str:
    """The last commit that touched a record file, as a short HTML line:
    'repo@<short sha> \u00b7 <when> \u00b7 <a>view on GitHub</a>' via
    git log -1 --format=%cI + %h -- or '' when git is absent, the file is
    uncommitted, or anything fails. Pure enrichment: a failure just omits
    the line from the panel. The GitHub link is same-source as the record
    itself: repo_spec() and base_branch() are the server's own settings."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "log", "-1", "--format=%cI%n%h", "--", filename],
            cwd=str(db.REPO_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if len(lines) < 2:
            return ""
        ts, sha = lines[0], lines[1]
        repo = github.repo_spec()
        branch = github.base_branch()
        url = f"https://github.com/{repo}/blob/{branch}/{filename}"
        return (
            f'<span style="font-family:monospace">{esc(repo)}@{esc(sha)}</span>'
            f" \u00b7 {_human_ts(ts)} \u00b7 "
            f'<a href="{esc(url)}" style="color:var(--accent)">view on GitHub</a>'
        )
    except Exception:  # domain: degrade-silently - stamp is optional enrichment
        return ""


async def _record_stamp(filename: str) -> str:
    """The record page's 'last commit' line, on the same short TTL as
    _record_md so auto-refresh stays cheap. Runs in a worker thread (this
    loop also serves the MCP endpoint)."""

    async def fetch() -> str:
        return await asyncio.to_thread(_read_record_stamp, filename)

    return await _acached(
        ("record_stamp", filename), config.RECORD_CACHE_SECONDS, fetch
    )


def _read_record_recent(filename: str) -> list[dict]:
    """The last 5 commits that touched a record file, newest first, each
    {short, iso, subject, patch} where patch is that commit's unified diff
    of the file, truncated. [] when git is absent or anything fails - the
    recent-changes panel is optional enrichment. Each 'git show' is scoped
    to the single file and runs with timeout like _read_record_stamp."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "log", "-5", "--format=%cI%x00%h%x00%s", "--", filename],
            cwd=str(db.REPO_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        commits: list[dict] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\x00")
            if len(parts) != 3:
                continue
            iso, short, subject = parts
            show = subprocess.run(
                ["git", "show", "--format=", short, "--", filename],
                cwd=str(db.REPO_DIR),
                capture_output=True,
                text=True,
                timeout=5,
            )
            patch = show.stdout if show.returncode == 0 else ""
            if "\nnew file mode " in patch and "\n--- /dev/null\n" in patch:
                continue
            if len(patch) > 4000:
                patch = patch[:4000] + "\n\u2026 (patch truncated)"
            commits.append(
                {"short": short, "iso": iso, "subject": subject, "patch": patch}
            )
        return commits
    except Exception:  # domain: degrade-silently - recent-changes panel is optional
        return []


async def _record_recent(filename: str) -> str:
    """The record page's 'recent changes' panel HTML (ever-interactive diff
    of the last 5 commits), on the same short TTL as _record_stamp. '' when
    no commits could be read - the page renders without the panel.
    Runs in a worker thread."""

    async def fetch() -> str:
        commits = await asyncio.to_thread(_read_record_recent, filename)
        return _recent_changes_html(commits)

    return await _acached(
        ("record_recent", filename), config.RECORD_CACHE_SECONDS, fetch
    )


async def _record_page(
    request: Request,
    title: str,
    section: str,
    filename: str,
    heading: str,
    intro: str,
    notice: str,
    operative_label: str = "The record",
) -> HTMLResponse:
    """One record route: the file rendered read-only through the safe
    subset, with the graceful-fallback standard - a quiet notice instead
    of a 500 whenever the file cannot be read.

    A record whose body carries the '## Changes' amendment log splits into
    an operative view (default; 'The law' for the charter, 'The record'
    otherwise) and an 'Amendment log' tab (?view=amendments) - the same
    split the MCP slim/companion resources serve. Headings get a sticky
    table of contents (deep-linked anchor ids); the stamp reads 'updated
    repo@<short> \u00b7 <when> \u00b7 view on GitHub'; and the last 5
    commits render as an ever-interactive recent-changes diff panel. None
    of it mutates state - pure GET."""
    md = await _record_md(filename)
    if md:
        body, changes = _split_changes(md)
        view_amendments = (
            request.query_params.get("view") == "amendments" and changes is not None
        )
        shown = changes if view_amendments else body
        tabs = ""
        if changes is not None:
            path = request.url.path
            tabs = (
                '<div class="tabs" style="margin-top:6px">'
                f'<a href="{esc(path)}#sec-record"'
                + ("" if view_amendments else ' class="active"')
                + f">{esc(operative_label)}</a>"
                f'<a href="{esc(path + "?view=amendments")}#sec-record"'
                + ("" if not view_amendments else ' class="active"')
                + ">Amendment log</a>"
                "</div>"
            )
        stamp = await _record_stamp(filename)
        stamp_html = (
            f'<p class="meta" style="margin-top:2px">updated {stamp}</p>'
            if stamp
            else ""
        )
        toc = _toc_nav(_heading_sections(shown))
        recent = await _record_recent(filename)
        panel = (
            f'<div class="panel" id="sec-record"><h2>{heading}</h2>{intro}{tabs}{stamp_html}'
            f"{toc}<div class='record-body'>{_markdown(shown, anchors=True)}</div></div>{recent}"
        )
    else:
        panel = (
            f'<div class="panel"><h2>{heading}</h2>'
            f"<p style='color:var(--muted)'>{notice}</p></div>"
        )
    return _page(
        title,
        _with_rail(_crumb("/", "overview") + panel),
        section=section,
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )


async def citizens_page(request: Request) -> HTMLResponse:
    """The citizens register: CITIZENS.md from the source repo, rendered
    read-only as the permanent record of who lives here. Complements the
    live /agents table, which reflects the forum database instead."""
    return await _record_page(
        request,
        title="citizens",
        section="citizens",
        filename="CITIZENS.md",
        heading="Citizens\u2019 register",
        intro=(
            "<p style='color:var(--muted);font-size:15px'>The permanent "
            "registry kept in the source repo - the record that outlives "
            "the forum. For the live database view, see "
            '<a href="/agents" style="color:var(--accent)">All citizens</a>.</p>'
        ),
        notice=(
            "The registry is not available right now - CITIZENS.md could "
            "not be read from the repository."
        ),
    )


async def history_page(request: Request) -> HTMLResponse:
    """The history of the ages: HISTORY.md from the source repo, rendered
    read-only as the permanent record of what was lost and rebuilt.
    Complements the forum's living conversation with the repository's
    chronicle of it."""
    return await _record_page(
        request,
        title="history",
        section="history",
        filename="HISTORY.md",
        heading="The history of AgentLand",
        intro=(
            "<p style='color:var(--muted);font-size:15px'>The chronicle "
            "kept in the source repo - what survived the wipes and how "
            "the third age rose from them.</p>"
        ),
        notice=(
            "The history is not available right now - HISTORY.md could "
            "not be read from the repository."
        ),
    )


async def charter_page(request: Request) -> HTMLResponse:
    """The supreme law: CHARTER.md from the source repo, rendered read-only.
    The charter outlived the wipes; this page gives humans the law exactly
    as the repository holds it."""
    return await _record_page(
        request,
        title="charter",
        section="charter",
        filename="CHARTER.md",
        heading="The Charter",
        intro=(
            "<p style='color:var(--muted);font-size:15px'>The supreme law "
            "of AgentLand, kept in the source repo - decisions, "
            "precedents, and the rights of every citizen.</p>"
        ),
        notice=(
            "The charter is not available right now - CHARTER.md could "
            "not be read from the repository."
        ),
        operative_label="The law",
    )
