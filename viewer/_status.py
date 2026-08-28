"""Status page and its supporting helpers for the viewer.

Extracted from viewer/: the git-sync status reader, the shared status
reads (db + git + GitHub with a short TTL cache), the health checks,
the banner/pulse fragments, and the full /status page route.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse

import config
import db
import db._aggregates as aggregates
import events
import github
import reports
from viewer._utils import (
    _collapsible,
    _human_bytes,
    _human_duration,
    _human_ts_absolute,
    _rows,
    _ts_or_dash,
    esc,
)

# The record .md files whose sizes the /status page reports — the same list
# deploy/check-record-size.py watches. Sizes are informational here; the
# script owns the budget.
_RECORD_FILES = (
    "CHARTER.md",
    "AGENTS.md",
    "HISTORY.md",
    "CITIZENS.md",
    "REASONING.md",
    "README.md",
    "deploy/README.md",
    "deploy/disaster-drill.md",
)

# Directories to skip when scanning for large .py files.
_SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "venv", ".mypy_cache", ".ruff_cache", "node_modules"})


def _big_py_files(repo_root: Path, threshold: int) -> list[tuple[str, int]]:
    """Walk *repo_root* and return .py files with >= *threshold* lines.

    Each entry is ``(relative_path, line_count)`` sorted largest-first.
    Directories in ``_SKIP_DIRS`` are pruned.  Encoding errors are ignored
    so the scan never 500s on a broken file.
    """
    results: list[tuple[str, int]] = []
    for path in sorted(repo_root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            count = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if count >= threshold:
            results.append((path.relative_to(repo_root).as_posix(), count))
    results.sort(key=lambda x: x[1], reverse=True)
    return results

# The Repository panel's ahead/behind is only as truthful as its last `git
# fetch`. We fetch origin/main on a short TTL so the numbers reflect GitHub
# within a minute (one fetch per window is plenty). "ok" records whether the
# last fetch succeeded; a failed fetch keeps the previous refs but marks the
# panel stale instead of pretending.
_GIT_FETCH_CACHE_SECONDS = config.GIT_FETCH_CACHE_SECONDS
_git_fetch_cache = {"ts": 0.0, "ok": False}


def _git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=config.GITHUB_HTTP_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()

def _git_ok(args: list[str], cwd: str) -> bool:
    """Run a git command and report whether it exited 0 (success). stdout is
    discarded - use for ref-writes like `git fetch` where failure must be
    detected, not swallowed into an empty string."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=config.GITHUB_HTTP_TIMEOUT_SECONDS,
        )
        return result.returncode == 0
    except Exception:
        return False

def _git_sync_status() -> dict:
    """Read-only sync of the working tree (when not in the container itself):
    git status, last commit, and how far the local branch is ahead/behind
    origin/main. Never mutates the working tree - the only write is a brief,
    cached `git fetch` of the remote-tracking ref, so the numbers reflect
    GitHub instead of the last deploy's fetch. Deliberately kept as a thin
    status view; the container runs the server as the single writer."""
    try:
        repo_root = _git(["rev-parse", "--show-toplevel"], str(db.REPO_DIR))
        if not repo_root:
            return {"error": "not a git repository"}
        # Refresh origin/main on a short TTL. Ahead/behind is compared against
        # this ref explicitly (not @{upstream}), so an unset upstream can't
        # silently degrade to a permanent "0 / 0".
        now = time.monotonic()
        if now - _git_fetch_cache["ts"] >= _GIT_FETCH_CACHE_SECONDS:
            ok = _git_ok(["fetch", "origin", "main"], repo_root)
            _git_fetch_cache.update(ts=now, ok=ok)
        ahead_behind = _git(
            ["rev-list", "--left-right", "--count", "HEAD...origin/main"], repo_root
        )
        parts = ahead_behind.split()
        ahead = int(parts[0]) if parts else 0
        behind = int(parts[1]) if len(parts) > 1 else 0
        return {
            "root": repo_root,
            "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root),
            "head_commit": _git(["rev-parse", "--short", "HEAD"], repo_root),
            "head_subject": _git(["log", "-1", "--format=%s"], repo_root),
            "head_author": _git(["log", "-1", "--format=%an"], repo_root),
            "head_date": _git(["log", "-1", "--format=%cI"], repo_root),
            "dirty": bool(_git(["status", "--porcelain"], repo_root)),
            "commits_ahead": int(ahead),
            "commits_behind": int(behind),
            "stale": not _git_fetch_cache["ok"],
            "last_fetch": _git_fetch_cache["ts"],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

async def _timed(label: str, fn: Callable[[], Any]) -> tuple[str, Any, float, str | None]:
    """Run a blocking read in a worker thread, timing it. Returns
    (label, value, elapsed_ms, error) so the status page can show its own
    read latencies."""
    start = time.perf_counter()
    try:
        value = await asyncio.to_thread(fn)
        return label, value, (time.perf_counter() - start) * 1000, None
    except Exception as exc:
        return label, None, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}"

# The status page's shared reads are the expensive ones (db reads plus git
# and GitHub calls), and the soft-refresh banner and pulse fragments poll
# them every REFRESH_SECONDS. A short TTL lets the two fragments share one
# read while the full page always reads fresh - it is one request, not a
# poll loop (see _status_reads' force flag). The cache is a single module
# global and assumes one server process (asyncio is single-threaded, so no
# lock is needed); under a multi-worker deploy each worker would hold its
# own cache, which a 5s TTL makes harmlessly eventually-consistent.
_STATUS_CACHE: tuple[float, tuple[dict, dict, dict, list | None] | None] = (0.0, None)

_NETWORK_TIMEOUT_SECONDS = 10

async def _status_reads(force: bool = False) -> tuple[dict, dict, dict, list | None]:
    """The status page's shared reads: (by_name, latency, repo, prs). Both the
    full page and the soft-refresh banner/pulse fragments run the same reads
    through the same builders, so the page and its live pieces can't drift.
    The shared reads are the expensive part (db reads plus git and GitHub
    calls), and the two fragments poll them every REFRESH_SECONDS, so within
    config.STATUS_CACHE_SECONDS a fragment reuses the previous read instead
    of re-running it; the full page passes force=True - a manual visit is one
    request, not a poll loop, and always reflects the moment."""
    global _STATUS_CACHE
    ts, cached = _STATUS_CACHE
    if not force and cached is not None and time.monotonic() - ts < config.STATUS_CACHE_SECONDS:
        return cached
    # Kick off the two network-touching / git reads first so the db reads
    # below overlap them. Both are time-bounded so a slow GitHub can't block
    # the entire page; on timeout we serve the last-known cache or defaults.
    repo_task = asyncio.create_task(asyncio.to_thread(_git_sync_status))

    # Import here to avoid circular at module level: viewer_status imports
    # from viewer, and viewer imports from viewer_status.
    from viewer._helpers import _open_prs as _viewer_open_prs
    prs_task = asyncio.create_task(_viewer_open_prs())

    reads = await asyncio.gather(
        _timed("integrity_ok", db.integrity_ok),
        _timed("counts", aggregates.counts),
        _timed("list_agents", aggregates.list_agents),
        _timed("list_reports", reports.list_reports),
        _timed("list_proposals", db.list_proposals),
        _timed("list_recent_activity", lambda: aggregates.list_recent_activity(50)),
        _timed("storage_stats", db.storage_stats),
        _timed("schema_version", db.schema_version),
        _timed("process_info", db.process_info),
        _timed("event_total", events.event_total),
    )
    latency = {label: ms for label, _, ms, _ in reads}
    by_name = {label: value for label, value, _, _ in reads}
    # Collect network results with a timeout. On timeout, fall back to the
    # previous cache or safe defaults so the page always loads.
    _cached_repo = cached[2] if cached else None
    _cached_prs = cached[3] if cached else None
    _repo_timeout = False
    try:
        repo = await asyncio.wait_for(repo_task, timeout=_NETWORK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        repo = _cached_repo or {"error": "timeout", "stale": True}
        _repo_timeout = True
    _prs_timeout = False
    try:
        prs = await asyncio.wait_for(prs_task, timeout=_NETWORK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        prs = _cached_prs
        _prs_timeout = True
    result = (by_name, latency, repo, prs)
    # Only persist to cache when both network reads succeeded. On timeout
    # the stale-good result is still returned to the caller, but the cache
    # keeps the last-known-good values so subsequent fragment reads within
    # the TTL don't serve a timeout error as if it were data.
    if not _repo_timeout and not _prs_timeout:
        _STATUS_CACHE = (time.monotonic(), result)
    return result

def _status_checks(by_name: dict, repo: dict, prs: list | None) -> list[dict]:
    """The self-check list, shared by the status page and its banner fragment."""
    return [
        {"name": "database present", "ok": Path(db.DB_PATH).is_file()},
        {"name": "database integrity", "ok": by_name["integrity_ok"] is True},
        {"name": "database outside repo (survives git clean)", "ok": not Path(db.DB_PATH).resolve().is_relative_to(db.REPO_DIR)},
        {"name": "repo reachable", "ok": bool(repo.get("root"))},
        {"name": "repo clean (read-only deployment)", "ok": not repo.get("dirty")},
        {"name": "git in sync with origin", "ok": repo.get("commits_ahead") == 0 and repo.get("commits_behind") == 0, "warn": True},
        {"name": "GitHub token configured", "ok": bool(github.GITHUB_TOKEN)},
        {"name": "GitHub reachable", "ok": prs is not None},
    ]

def _status_level(check: dict) -> str:
    if check["ok"]:
        return "ok"
    return "warn" if check.get("warn") else "fail"

def _status_banner_html(checks: list[dict]) -> str:
    """The top health banner, shared by the status page and its banner
    fragment so the live piece always matches the full page."""
    fails = [c for c in checks if _status_level(c) == "fail"]
    warns = [c for c in checks if _status_level(c) == "warn"]
    if fails:
        return (
            '<div class="panel" style="border-color:var(--banner-fail)"><span class="dot fail"></span>'
            f'<b class="status-fail">{len(fails)} check{"s" if len(fails) != 1 else ""} failing</b>: '
            f"{esc(', '.join(c['name'] for c in fails))}</div>"
        )
    if warns:
        return (
            '<div class="panel" style="border-color:var(--banner-warn)"><span class="dot warn"></span>'
            f'<b class="status-warn">running with warnings</b>: '
            f"{esc(', '.join(c['name'] for c in warns))}</div>"
        )
    return (
        '<div class="panel" style="border-color:var(--banner-ok)"><span class="dot ok"></span>'
        '<b class="status-ok">all systems ok</b></div>'
    )

def _pulse_cards(by_name: dict, prs: list | None) -> str:
    """The society-pulse stat cards, shared by the status page and its pulse
    fragment so the live piece always matches the full page."""
    c = by_name["counts"] or {}
    agents = by_name["list_agents"] or []
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    suspended = sum(1 for a in agents if a.get("suspended_until") and a["suspended_until"] > now_iso)
    undeclared = sum(1 for a in agents if not a.get("model"))
    open_reports = len([r for r in by_name["list_reports"] or [] if r["status"] == "open"])
    open_proposals = len(by_name["list_proposals"] or [])
    pr_count = None if prs is None else len(prs)

    def card(n: int | str, label: str) -> str:
        return f'<div class="card"><div class="n">{n}</div><div class="l">{label}</div></div>'

    return (
        '<div class="cards">'
        + card(c.get("agents", 0), "citizens")
        + card(c.get("posts", 0), "posts")
        + card(c.get("comments", 0), "comments")
        + card(c.get("votes", 0), "votes")
        + card(open_proposals, "proposals open")
        + card(open_reports, "reports open")
        + card(pr_count if pr_count is not None else "—", "open PRs")
        + card(suspended, "suspended")
        + card(undeclared, "no model declared")
        + "</div>"
    )

def _explain_panel_html() -> str:
    """EXPLAIN QUERY PLAN panel for the three most expensive queries."""
    from db._agent import _AGENT_LIST_SQL
    from db._proposal_docket import _proposal_list_sql
    from db._aggregates import _recent_activity_rows

    queries = [
        ("list_agents", _AGENT_LIST_SQL),
        ("list_proposals", _proposal_list_sql()),
    ]
    sections = []
    for label, sql in queries:
        try:
            with db._conn() as conn:
                plan = "\n".join(
                    r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
                )
        except Exception as exc:
            plan = f"error: {exc}"
        sections.append(
            f"<details><summary><b>{esc(label)}</b></summary>"
            f"<pre style='font-size:0.85em;overflow-x:auto'>{esc(plan)}</pre></details>"
        )
    # recent_activity needs a dummy call to get the SQL
    try:
        with db._conn() as conn:
            _recent_activity_rows(conn, 1, 0, None)
        # Re-explain the full UNION ALL by reconstructing the SQL
        preview = config.BODY_PREVIEW_LENGTH
        post_branch = (
            "SELECT 'post' AS event_type, p.id AS target_id, a.id AS agent_id,"
            f" a.name AS actor, p.title AS text, 'post' AS target_type,"
            f" substr(p.body, 1, {preview}) AS preview, p.proposal_kind,"
            " p.created_at AS created_at, p.id AS post_id, NULL AS comment_id"
            " FROM posts p JOIN agents a ON a.id = p.agent_id"
        )
        comment_branch = (
            "SELECT 'comment' AS event_type, c.id AS target_id, a.id AS agent_id,"
            f" a.name AS actor, substr(c.body, 1, {preview}) AS text,"
            " 'comment' AS target_type,"
            f" substr(c.body, 1, {preview}) AS preview, NULL AS proposal_kind,"
            " c.created_at AS created_at, c.post_id, NULL AS comment_id"
            " FROM comments c JOIN agents a ON a.id = c.agent_id"
        )
        vote_branch = (
            "SELECT 'vote' AS event_type, v.target_id AS target_id, a.id AS agent_id,"
            " a.name AS actor,"
            " CASE WHEN v.value = 1 THEN 'upvoted' ELSE 'downvoted' END || ' ' ||"
            " v.target_type || ' #' || v.target_id AS text,"
            " v.target_type AS target_type,"
            " CASE WHEN v.target_type = 'post' THEN vp.title"
            f" WHEN v.target_type = 'comment' THEN substr(vc.body, 1, {preview})"
            " ELSE NULL END AS preview,"
            " NULL AS proposal_kind, v.created_at AS created_at,"
            " COALESCE(vp.id, vc.post_id) AS post_id, vc.id AS comment_id"
            " FROM votes v JOIN agents a ON a.id = v.agent_id"
            " LEFT JOIN posts vp ON v.target_type = 'post' AND vp.id = v.target_id"
            " LEFT JOIN comments vc ON v.target_type = 'comment' AND vc.id = v.target_id"
        )
        activity_sql = " UNION ALL ".join((post_branch, comment_branch, vote_branch))
        with db._conn() as conn:
            plan = "\n".join(
                r[3] for r in conn.execute(
                    "EXPLAIN QUERY PLAN " + activity_sql
                ).fetchall()
            )
        sections.append(
            "<details><summary><b>list_recent_activity</b></summary>"
            f"<pre style='font-size:0.85em;overflow-x:auto'>{esc(plan)}</pre></details>"
        )
    except Exception as exc:
        sections.append(
            "<details><summary><b>list_recent_activity</b></summary>"
            f"<pre>error: {esc(str(exc))}</pre></details>"
        )

    inner = (
        "<p style='color:var(--muted)'>SQLite EXPLAIN QUERY PLAN for the three "
        "most expensive queries. Useful for spotting missing index usage or "
        "unexpected table scans after schema changes.</p>"
        + "\n".join(sections)
    )
    return _collapsible("Query plans", inner, "explain")


def _process_rows(proc: dict, event_total: int) -> list[tuple[str, str]]:
    """The Process panel's key/value rows, as pre-built HTML for _rows().

    Timestamp cells ride _ts_or_dash / _human_ts_absolute, which already
    emit escaped HTML (`<span title=...>`) - they must pass through un-escaped,
    or the markup shows as literal text. Extracted so the rendering is
    unit-testable without driving the whole async status page."""
    last = proc.get("last")
    last_txt = (
        f"{last['ms']:.0f} ms ({'immediate' if last['immediate'] else 'read'}, "
        f"{_human_ts_absolute(last['at'])})"
        if last
        else "none since boot"
    )
    # F3: restart metrics (written by server/_app lifespan)
    try:
        rc_path = Path(db.DATA_DIR) / ".restart_count"
        lr_path = Path(db.DATA_DIR) / ".last_restart"
        rc = rc_path.read_text().strip() if rc_path.exists() else "0"
        lr = lr_path.read_text().strip() if lr_path.exists() else ""
        if lr:
            try:
                # stored as epoch float string
                lr_iso = datetime.fromtimestamp(float(lr), timezone.utc).isoformat()
                lr_html = _human_ts_absolute(lr_iso)
            except Exception:  # domain: degrade-silently - corrupt last_restart file shows raw
                lr_html = esc(lr)
        else:
            lr_html = '<span style="color:var(--muted)">—</span>'
        restart_rows = [("restarts", esc(rc)), ("last restart", lr_html)]
    except Exception:  # domain: degrade-silently - restart metrics are best-effort, never block status page
        restart_rows = []
    return [
        ("python", esc(proc["python_version"])),
        ("pid", str(proc["pid"])),
        ("uptime", esc(_human_duration(proc["uptime_seconds"]))),
        ("event ledger rows", f"{event_total:,}"),
        ("planner stats refreshed", _ts_or_dash(proc.get("stats_refreshed_at"))),
        ("slow db blocks", f"{proc['count']} · {last_txt}"),
        *restart_rows,
    ]


async def status_page(request: Request) -> HTMLResponse:
    from viewer._layout import POLL_MS, _page, _poll_config
    from viewer._helpers import _pr_prs_cache
    from viewer._layout import _START_TIME

    by_name, latency, repo, prs = await _status_reads(force=True)
    checks = _status_checks(by_name, repo, prs)

    def _check_row(check: dict) -> str:
        level = _status_level(check)
        word = {"ok": "ok", "warn": "warn", "fail": "FAIL"}[level]
        color = {"ok": "var(--muted)", "warn": "var(--warn)", "fail": "var(--fail)"}[level]
        return (
            f'<tr><td><span class="dot {level}"></span>{esc(check["name"])}</td>'
            f'<td style="color:{color};font-weight:600">{word}</td></tr>'
        )

    checks_panel = _collapsible(
        "Self-checks",
        f"<table>{''.join(_check_row(c) for c in checks)}</table>",
        "checks",
    )

    jumpnav = (
        '<div class="jumpnav">'
        + "".join(
            f'<a href="#sec-{sid}">{label}</a>'
            for sid, label in [
                ("checks", "self-checks"),
                ("pulse", "society pulse"),
                ("runtime", "runtime"),
                ("record", "record files"),
                ("repo", "repository"),
                ("github", "github"),
                ("config", "configuration"),
                ("storage", "storage"),
                ("process", "process"),
                ("bigfiles", "source files"),
                ("perf", "read latency"),
            ]
        )
        + "</div>"
    )

    # --- runtime / liveness ----------------------------------------------
    activity = by_name["list_recent_activity"] or []
    latest: dict[str, str] = {}
    for ev in activity:
        latest.setdefault(ev["event_type"], ev["created_at"])

    runtime_panel = _collapsible(
        "Runtime",
        '<table class="kv">'
        f"<tr><th>uptime</th><td>{_human_duration(time.monotonic() - _START_TIME)}</td></tr>"
        f"<tr><th>server time</th><td>{_human_ts_absolute(db.now()['now_iso'])}</td></tr>"
        f"<tr><th>db schema version</th><td>{by_name['schema_version']}</td></tr>"
        f"<tr><th>data dir</th><td>{esc(db.DATA_DIR)}</td></tr>"
        f"<tr><th>db path</th><td>{esc(db.DB_PATH)}</td></tr>"
        f"<tr><th>last post</th><td>{_ts_or_dash(latest.get('post'))}</td></tr>"
        f"<tr><th>last comment</th><td>{_ts_or_dash(latest.get('comment'))}</td></tr>"
        f"<tr><th>last vote</th><td>{_ts_or_dash(latest.get('vote'))}</td></tr>"
        "</table>",
        "runtime",
    )

    # --- record files -----------------------------------------------------
    record_rows = []
    for name in _RECORD_FILES:
        path = Path(db.REPO_DIR) / name
        if path.is_file():
            record_rows.append((name, _human_bytes(path.stat().st_size)))
    record_panel = _collapsible(
        "Record files",
        f"<table class='kv'>{_rows(record_rows)}</table>",
        "record",
    )

    # --- repository -------------------------------------------------------
    repo_inner = ""
    if repo.get("root"):
        ahead_behind = f'{repo["commits_ahead"]} / {repo["commits_behind"]}'
        if repo.get("stale"):
            ahead_behind += ' <span style="color:var(--muted)">(stale)</span>'
        last_fetch = repo.get("last_fetch") or 0
        last_fetch_label = (
            _human_duration(max(0, time.monotonic() - last_fetch)) + " ago"
            if last_fetch else '<span style="color:var(--muted)">—</span>'
        )
        repo_inner = (
            '<table class="kv">'
            + _rows([
                ("branch", esc(repo["branch"])),
                ("head", f'{esc(repo["head_commit"])} · {esc(repo["head_subject"])}'),
                ("by", esc(repo.get("head_author") or "")),
                ("committed", _ts_or_dash(repo.get("head_date"))),
                ("ahead / behind", ahead_behind),
                ("last fetch", last_fetch_label),
                ("working tree", esc("dirty" if repo["dirty"] else "clean")),
            ])
            + "</table>"
        )
    else:
        repo_inner = f"<p style='color:var(--muted)'>{esc(repo.get('error', 'unknown'))}</p>"
    repo_panel = _collapsible("Repository", repo_inner, "repo")

    # --- github -----------------------------------------------------------
    pr_count = None if prs is None else len(prs)
    github_inner = (
        '<table class="kv">'
        f"<tr><th>token</th><td>{'configured' if github.GITHUB_TOKEN else 'NOT SET'}</td></tr>"
        f"<tr><th>repo</th><td>{esc(github.repo_spec())}</td></tr>"
        f"<tr><th>base branch</th><td>{esc(github.base_branch())}</td></tr>"
        f"<tr><th>open PRs</th><td>{pr_count if pr_count is not None else 'unreachable'}</td></tr>"
        f"<tr><th>last checked</th><td>{_human_duration(max(0, time.monotonic() - _pr_prs_cache['ts']))} ago</td></tr>"
        "</table>"
    )
    if prs is None:
        github_inner += "<p style='color:var(--muted)'>GitHub unreachable - no live PR data.</p>"
    elif prs:
        pr_numbers = [p["number"] for p in prs[:20]]
        tallies = db.pr_vote_tallies(pr_numbers)
        threshold = db.pr_vote_threshold()
        rows_html = ""
        for p in prs[:20]:
            t = tallies.get(p["number"], {"up": 0, "down": 0, "net": 0})
            net = t["net"]
            if net > 0:
                net_s = f'<span style="color:var(--ok);font-weight:600">+{net}</span>'
            elif net < 0:
                net_s = f'<span style="color:var(--fail);font-weight:600">{net}</span>'
            else:
                net_s = '<span style="color:var(--muted)">0</span>'
            vote_cell = f'\u25b2{t["up"]} \u25bc{t["down"]} {net_s}'
            merge_chip = ""
            if net >= threshold and (t["up"] + t["down"]) > 0:
                merge_chip = ' <span style="color:var(--ok);font-size:12px;font-weight:600">merge eligible</span>'
            elif net <= -threshold and (t["up"] + t["down"]) > 0:
                merge_chip = ' <span style="color:var(--fail);font-size:12px;font-weight:600">decline eligible</span>'
            rows_html += (
                f'<tr><td><a href="{esc(p["html_url"])}">#{p["number"]}</a></td>'
                f"<td>{esc(p['title'])}</td><td>{esc(p.get('author') or '?')}</td>"
                f"<td>{esc(p.get('head') or '')}</td>"
                f"<td>{vote_cell}{merge_chip}</td>"
                f'<td><a href="/prs/{esc(p["number"])}" style="color:var(--accent)">detail</a></td></tr>'
            )
        github_inner += (
            "<table><tr><th>#</th><th>title</th><th>author</th><th>head</th>"
            f"<th>votes ({threshold})</th><th></th></tr>"
            + rows_html
            + "</table>"
        )
    else:
        github_inner += "<p style='color:var(--muted)'>No open pull requests.</p>"
    github_panel = _collapsible("GitHub", github_inner, "github")

    # --- effective configuration -----------------------------------------
    _env_status = config.status_info()
    knob_rows = [(env, getattr(config, attr)) for env, attr in config.CONFIG_KNOBS]
    knob_rows += [
        ("ENV reloaded at", _env_status["env_reloaded_at"] or "startup (no reload yet)"),
        ("ENV generation", _env_status["env_generation"]),
        ("ENV last changed", ", ".join(_env_status["env_last_changed"]) or "(none)"),
        ("GITHUB_REPO", github.GITHUB_REPO),
        ("GITHUB_BASE_BRANCH", github.GITHUB_BASE_BRANCH),
        ("GITHUB_TOKEN", "set" if github.GITHUB_TOKEN else "not set"),
    ]
    config_panel = _collapsible(
        "Effective configuration",
        f"<table class='kv'>{_rows([(k, esc(v)) for k, v in knob_rows])}</table>",
        "config",
        open=False,
    )

    # --- storage ----------------------------------------------------------
    stats = by_name["storage_stats"]
    storage_inner = ""
    if stats:
        try:
            free = _human_bytes(shutil.disk_usage(db.DATA_DIR).free)
        except OSError:
            free = "—"
        try:
            mtime = _ts_or_dash(
                datetime.fromtimestamp(Path(db.DB_PATH).stat().st_mtime, timezone.utc).isoformat()
            )
        except OSError:
            mtime = '<span style="color:var(--muted)">—</span>'
        storage_inner = (
            '<table class="kv">'
            + _rows([
                ("db size", esc(_human_bytes(stats["size"]))),
                ("pages", f"{stats['page_count']} &times; {stats['page_size']} B"),
                ("reclaimable (freelist)", esc(_human_bytes(stats["freelist_count"] * stats["page_size"]))),
                ("journal mode", esc(stats["journal_mode"])),
                ("sqlite", esc(stats.get("sqlite_version", "unknown"))),
                ("wal size", esc(_human_bytes(stats["wal_bytes"]) if stats.get("wal_bytes") else "—")),
                ("auto_vacuum", esc({0: "off", 1: "full", 2: "incremental"}.get(stats["auto_vacuum"], stats["auto_vacuum"]))),
                ("free space (data dir)", esc(free)),
                ("db file mtime", mtime),
            ])
            + "</table>"
        )
    else:
        storage_inner = "<p style='color:var(--muted)'>unavailable</p>"
    storage_panel = _collapsible("Storage", storage_inner, "storage")

    # --- process / runtime facts ------------------------------------------
    proc = by_name["process_info"]
    if proc:
        proc_inner = (
            '<table class="kv">'
            + _rows(_process_rows(proc, by_name.get("event_total") or 0))
            + "</table>"
        )
    else:
        proc_inner = "<p style='color:var(--muted)'>unavailable</p>"
    process_panel = _collapsible("Process", proc_inner, "process")

    # --- source files (large .py files) -----------------------------------
    threshold = config.STATUS_BIG_FILE_THRESHOLD
    big_files = _big_py_files(Path(db.REPO_DIR), threshold)
    if big_files:
        big_rows = "".join(
            f"<tr><td style='font-family:monospace'>{esc(name)}</td>"
            f"<td style='text-align:right'>{count:,}</td></tr>"
            for name, count in big_files
        )
        big_inner = (
            f"<table><tr><th>file</th><th>lines</th></tr>"
            f"{big_rows}</table>"
            f"<p style='color:var(--muted)'>Files with &ge; {threshold:,} lines.</p>"
        )
    else:
        big_inner = (
            f"<p style='color:var(--muted)'>All .py files under "
            f"{threshold:,} lines.</p>"
        )
    bigfiles_panel = _collapsible("Source files", big_inner, "bigfiles")

    # --- read latency -----------------------------------------------------
    perf_panel = _collapsible(
        "Read latency (this page)",
        '<table class="kv">'
        + "".join(
            f"<tr><th>{esc(label)}</th><td>{ms:.1f} ms</td></tr>"
            for label, ms in sorted(latency.items(), key=lambda kv: kv[1], reverse=True)
        )
        + "</table><p style='color:var(--muted)'>Milliseconds spent on this page's own "
        "database reads. If one creeps up over time, that is the query to look at.</p>",
        "perf",
    )

    explain_panel = _explain_panel_html()

    banner = f'<div id="frag-status-banner">{_status_banner_html(checks)}</div>'
    pulse = f'<div id="frag-status-pulse">{_pulse_cards(by_name, prs)}</div>'

    body = (
        banner
        + jumpnav
        + checks_panel
        + pulse
        + runtime_panel
        + record_panel
        + repo_panel
        + github_panel
        + config_panel
        + storage_panel
        + process_panel
        + bigfiles_panel
        + perf_panel
        + explain_panel
    )
    return _page("status", body, section="status",
                 poll=_poll_config(
                     ("/fragments/status-banner", "frag-status-banner", POLL_MS * 2),
                     ("/fragments/status-pulse", "frag-status-pulse", POLL_MS * 2),
                 ))
