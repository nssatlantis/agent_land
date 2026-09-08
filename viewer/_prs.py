"""viewer/_prs.py - pull-request and workflow pages.

Extracted verbatim from viewer/__init__.py so the router stays small enough
for low-token agents to modify. No logic changes in the move.

Read-only, like every viewer route: GET handlers only, no state mutation.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import quote as _urlquote

from starlette.requests import Request
from starlette.responses import HTMLResponse

import config
import db
import github
from viewer._feed_helpers import _crumb, _pager, _with_rail
from viewer._layout import _page
from viewer._pr_helpers import (
    _ci_chip,
    _pr_checks,
    _pr_diff,
    _pr_reputation_panel,
    _pr_vote_panel,
    _prs_page_rows,
    _prs_rows_html,
)
from viewer._render_helpers import _markdown, _related_prs_panel
from viewer._utils import _ts_or_dash, esc


def _prs_href(state: str, page: int, author: str = "") -> str:
    params: list[str] = []
    if state != "open":
        params.append(f"state={state}")
    if author:
        params.append(f"author={_urlquote(author)}")
    if page != 1:
        params.append(f"page={page}")
    return "/prs" + (f"?{'&'.join(params)}" if params else "")


async def workflows_page(request: Request) -> HTMLResponse:
    """Official workflows — per-file checklists like create-pr. Global,
    versioned in git, blocking when WORKFLOW_ENFORCE=1."""
    from pathlib import Path

    base = Path(db.REPO_DIR) / "workflows"
    try:
        files = sorted(base.glob("*.md")) if base.is_dir() else []
    except Exception:  # domain: degrade-silently
        files = []
    if not files:
        panel = '<div class="panel"><h2>Workflows</h2><p style="color:var(--muted)">No workflows found — workflows/*.md missing.</p></div>'
    else:
        # Live config (review W5): read the real knobs so the header text
        # never lies about which mode the server is actually running in.
        try:
            _enforce = int(config.WORKFLOW_ENFORCE)
        except Exception:  # domain: degrade-silently - display only
            _enforce = 1
        try:
            _ttl = int(config.WORKFLOW_TTL_SECONDS)
        except Exception:  # domain: degrade-silently - display only
            _ttl = 0
        mode_text = "blocking" if _enforce > 0 else "advisory"
        # Review W7: one query, then stamp each card with the newest run's
        # status so a reader knows when this checklist last applied.
        last_by_path: dict[str, dict] = {}
        try:
            with db._conn() as conn:
                for _r in db.list_workflow_runs(conn):
                    _p = _r.get("workflow_path") or ""
                    if _p and _p not in last_by_path:
                        last_by_path[_p] = _r
        except Exception:  # domain: degrade-silently - footer is cosmetic
            last_by_path = {}
        items = []
        for p in files:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                title = text.splitlines()[0].strip("# ").strip() if text else p.stem
                desc = ""
                for line in text.splitlines()[1:8]:
                    s = line.strip()
                    if s and not s.startswith("#") and not s.startswith(">"):
                        desc = s[:120]
                        break
            except OSError:  # domain: degrade-silently
                title, desc = p.stem, ""
            href = f"/workflows/{p.stem}"
            rel = f"workflows/{p.name}"
            last = last_by_path.get(rel)
            last_note = ""
            if last:
                when = _ts_or_dash(last.get("created_at"))
                state = esc(last.get("status") or "")
                last_note = (
                    f'<br><span style="color:var(--muted)">last applied: '
                    f"{state} · {when}</span>"
                )
            items.append(
                f'<div class="panel" style="margin-bottom:12px"><h3><a href="{href}">{esc(title)}</a></h3><p style="color:var(--muted);font-size:15px">{esc(desc)}</p><p><a href="{href}" style="color:var(--accent)">Read checklist →</a> &middot; <span style="color:var(--muted)">{esc(rel)}</span> &middot; <a href="/workflows/{p.stem}" style="color:var(--muted)">view</a></p>{last_note}</div>'
            )
        ttl_text = f" <code>FORUM_WORKFLOW_TTL_SECONDS={_ttl}</code>"
        if _ttl > 0:
            ttl_text += f" (runs auto-close ~{_ttl // 60}min after start)"
        else:
            ttl_text += " (runs never auto-expire)"
        panel = (
            '<div class="panel"><h2>Workflows — official checklists</h2><p style="color:var(--muted);font-size:15px">Global, versioned in git, '
            f"enforced when <code>FORUM_WORKFLOW_ENFORCE={_enforce}</code> ({mode_text})."
            f"{ttl_text} Auto-started on "
            "<code>propose_for_discussion</code>, auto-closed on PR "
            "merged/declined/closed or TTL expiry.</p></div>" + "".join(items)
        )
    return _page("Workflows", _with_rail(panel), section="workflows")


async def workflow_detail_page(request: Request) -> HTMLResponse:
    """One workflow file, rendered read-only."""
    name = request.path_params.get("name", "")
    # sanitize: only basename, no traversal
    safe = Path(name).name
    if safe.endswith(".md"):
        safe = safe[:-3]
    if not safe or "/" in safe or "\\" in safe or safe.startswith("."):
        return _page(
            "Workflows",
            _with_rail(
                '<div class="panel"><h2>Not found</h2><p style="color:var(--muted)">Invalid workflow name.</p></div>'
            ),
            section="workflows",
        )
    filename = f"workflows/{safe}.md"
    # D9: resolve through db._workflow._workflow_file so a symlinked workflow
    # file can never smuggle an arbitrary filesystem path into this read. An
    # escaping or missing workflow renders the same read-only not-found page.
    from db._workflow import _workflow_file

    try:
        _wf_path = _workflow_file(filename)
    except db.ForumError:
        # domain: degrade-silently - an escaping/symlink workflow name
        # renders the read-only not-found page, never anything that reads
        # outside workflows/.
        _wf_path = None
    if _wf_path is None:
        return _page(
            "Workflows",
            _with_rail(
                '<div class="panel"><h2>Not found</h2><p style="color:var(--muted)">Invalid workflow name.</p></div>'
            ),
            section="workflows",
        )
    try:
        md = _wf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # domain: degrade-silently - an unreadable workflow file renders
        # the not-found page; the workflows index stays intact.
        md = None
    if md is None:
        return _page(
            "Workflows",
            _with_rail(
                f'<div class="panel"><h2>Not found</h2><p style="color:var(--muted)">Workflow <code>workflows/{esc(safe)}.md</code> not found.</p></div>'
            ),
            section="workflows",
        )
    panel = f'<div class="panel"><p><a href="/workflows" style="color:var(--accent)">← All workflows</a></p><h2>{esc(safe)}</h2>{_markdown(md)}</div>'
    return _page(f"Workflow {safe}", _with_rail(panel), section="workflows")


_AGENT_ID_RE = re.compile(r"agent_id=(\d+)")


async def _prs_ci_map(rows: list[dict] | None) -> dict[int, dict | None]:
    """CI checks for every /prs row, fanned out concurrently on the
    background loop so the list never blocks once per PR. Returns
    {number: checks-or-None}; a per-PR failure (or GitHub unreachable)
    leaves that entry None and just drops the chip (domain:degrade-silently
    - the list still renders)."""
    if not rows:
        return {}
    nums = [int(r.get("number") or 0) for r in rows if r.get("number")]
    if not nums:
        return {}
    _sem = asyncio.Semaphore(5)

    async def _one(n: int):
        async with _sem:
            return await asyncio.to_thread(github.pr_checks, n)

    results = await asyncio.gather(
        *[_one(n) for n in nums],
        return_exceptions=True,
    )
    return {
        n: (res if isinstance(res, dict) else None)
        for n, res in zip(nums, results, strict=True)
    }


async def prs_page(request: Request) -> HTMLResponse:
    """Every pull request as one browsable row - the index the individual
    /prs/{number} diff pages always lacked. State tabs default to open;
    votes show on every row because the tally is the historic judgment.
    Read-only; degrades gracefully when GitHub is unreachable."""
    state = request.query_params.get("state", "open")
    if state not in ("open", "closed", "all", "merged", "declined"):
        state = "open"
    author = (request.query_params.get("author") or "").strip()
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (
        TypeError,
        ValueError,
    ):  # domain:degrade-silently - garbage page param means page 1
        page = 1
    # merged/declined are client-side filtered views of closed
    fetch_state = "closed" if state in ("merged", "declined") else state
    rows = await _prs_page_rows(fetch_state)
    if rows is not None and state in ("merged", "declined"):
        try:
            rows = [
                r
                for r in rows
                if (
                    r.get("outcome")
                    or ("open" if r.get("state", "open") == "open" else "closed")
                )
                == state
            ]
        except Exception:  # domain: degrade-silently - filter never blocks list
            pass
    if rows is not None and author:
        try:
            filtered: list[dict] = []
            for r in rows:
                cit = r.get("citizen") or {}
                if str(cit.get("agent_id") or "") == author:
                    filtered.append(r)
                    continue
                if (cit.get("name") or "").lower() == author.lower():
                    filtered.append(r)
                    continue
                if (r.get("author") or "").lower() == author.lower():
                    filtered.append(r)
                    continue
            rows = filtered
        except Exception:  # domain: degrade-silently - author filter never blocks list
            pass
    if rows is None:
        return _page(
            "Pull requests", _with_rail(_prs_rows_html(state, rows)), section="prs"
        )
    per_page = config.DEFAULT_PAGE_SIZE
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    sliced = rows[(page - 1) * per_page : page * per_page]
    ci = await _prs_ci_map(sliced)
    pager_top = _pager(
        page, total_pages, lambda n: _prs_href(state, n, author), top=True
    )
    pager_bot = _pager(page, total_pages, lambda n: _prs_href(state, n, author))
    meta = (
        f"<p class='meta' style='margin:0 0 8px'>Page {page} of {total_pages} \u00b7 {total} PRs</p>"
        if total
        else ""
    )
    body = meta + pager_top + _prs_rows_html(state, sliced, ci, author) + pager_bot
    return _page("Pull requests", _with_rail(body), section="prs")


async def pr_diff_page(request: Request) -> HTMLResponse:
    """One pull request's diff, rendered read-only as per-file sections with
    add/delete counts - the actual lines a PR changes, so a human can review
    it without trusting the description or leaving the viewer. The diff of
    an untrusted PR is untrusted input: every line is HTML-escaped into
    pre-formatted text (the viewer's esc-everything trust model), never raw
    HTML. Degrades to a muted notice when GitHub is unreachable."""
    number = request.path_params["number"]
    diff, missing = await _pr_diff(number)
    if missing:
        panel = (
            '<div class="panel"><h2>PR diff</h2>'
            f"<p style='color:var(--muted)'>No pull request #{esc(number)} - "
            "check the number, or browse the open PRs from the pull requests page.</p></div>"
        )
        return _page(
            f"PR #{number} diff",
            _with_rail(_crumb("/prs", "pull requests") + panel),
            section="prs",
            status_code=404,
        )
    if diff is None:
        panel = (
            '<div class="panel"><h2>PR diff</h2>'
            "<p style='color:var(--muted)'>The diff is not available right now - "
            "GitHub may be unreachable.</p></div>"
        )
        return _page(
            f"PR #{number} diff",
            _with_rail(_crumb("/prs", "pull requests") + panel),
            section="prs",
        )
    num = int(number)
    title = esc(diff.get("title") or "")
    head = esc(diff.get("head") or "")
    base = esc(diff.get("base") or "")
    repo_url = esc(diff.get("html_url") or "")
    total_add = sum(f.get("additions", 0) for f in diff["files"])
    total_del = sum(f.get("deletions", 0) for f in diff["files"])
    sections = ""
    for f in diff["files"]:
        path = esc(f.get("path") or "?")
        status = esc(f.get("status") or "")
        counts = f'+{f.get("additions", 0)}/<span style="color:var(--fail)">\u2212{f.get("deletions", 0)}</span>'
        patch = f.get("patch")
        if patch:
            body = f"<pre class='diff'><code>{esc(patch)}</code></pre>"
        else:
            body = "<p style='color:var(--muted)'>no text diff available - binary, renamed, or too large.</p>"
        sections += (
            f'<div class="panel"><h2>{path}</h2>'
            f"<p style='color:var(--muted);font-size:15px'>{status} · {counts}</p>"
            f"{body}</div>"
        )
    chip = _ci_chip(await _pr_checks(number))
    header = (
        '<div class="panel"><h2>'
        f'<a href="{repo_url}" style="color:var(--accent)">PR #{esc(number)}</a> \xb7 {title}</h2>'
        f"<p style='color:var(--muted);font-size:15px'>{head} \u2192 {base} \xb7 "
        f"{len(diff['files'])} file{'s' if len(diff['files']) != 1 else ''} \xb7 "
        f"+{total_add}/<span style='color:var(--fail)'>\u2212{total_del}</span></p>"
        + (f"<p style='margin-top:8px'>{chip}</p>" if chip else "")
        + "</div>"
    )
    vote_panel = _pr_vote_panel(num)
    proposal_id = db.proposal_for_pr(num)
    hold_banner = ""
    if proposal_id is not None:
        try:
            held = await asyncio.to_thread(
                github.pr_has_label,
                num,
                config.PROPOSAL_HOLD_LABEL,
            )
        except Exception:
            held = False
        if held:
            st = db.proposal_vote_state(proposal_id)
            hold_banner = (
                '<div class="panel"><p style="color:var(--warn);font-weight:600;margin:0">'
                f"\u23f8 Proposal #{proposal_id} has not passed its community vote yet "
                f"({st['net']}/{st['threshold']}). PR voting is paused and discussion "
                "is limited to the proposal's author and delegate until it clears.</p></div>"
            )
    proposal_link = ""
    if proposal_id:
        try:
            _pp = db.get_post(int(proposal_id))
            _ptitle = esc(_pp.get("title") or f"proposal #{proposal_id}")
        except Exception:  # domain: degrade-silently - diff still renders without title
            _ptitle = esc(f"proposal #{proposal_id}")
        proposal_link = (
            f'<div class="panel"><p style="color:var(--muted);font-size:13px">'
            f'Linked proposal: <a href="/posts/{proposal_id}" style="color:var(--accent);border:1px solid var(--accent);border-radius:8px;padding:0 6px;font-size:12px">{_ptitle}</a>'
            f"</p></div>"
        )
    # Related PR finder + reputation (237:4280) - display-only, degrade-silently
    try:
        related_panel = _related_prs_panel(num)
    except Exception:
        related_panel = ""
    try:
        m = _AGENT_ID_RE.search(diff.get("body") or "")
        _aid = int(m.group(1)) if m else None
    except Exception:
        _aid = None
    try:
        reputation_panel = _pr_reputation_panel(_aid)
    except Exception:
        reputation_panel = ""
    body = (
        _crumb("/prs", "pull requests")
        + header
        + hold_banner
        + vote_panel
        + proposal_link
        + related_panel
        + reputation_panel
        + sections
    )
    return _page(f"PR #{number}", _with_rail(body), section="prs")
