"""viewer/_status.py - /status page, /status/pulse, /status/pulse/banner and /status/pulse/pager.

Async page and live-fragment handlers for the server health and repository
status panel. The three endpoints share the same reads via _status_reads so
the page and its live fragments can never drift apart.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import config
import db
import events
import github
import reports
from db import aggregates
from viewer._utils import (
    TTLCache,
    _human_duration,
    _human_ts_absolute,
    _human_ts,
    _human_date,
    esc,
    _record_files_list,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import HTMLResponse

_HOST = config.VIEWER_HOST
_PORT = config.VIEWER_PORT
_START_TIME = time.monotonic()

_REFRESH_MS = config.STATUS_REFRESH_MS
_REFRESH_SECONDS = _REFRESH_MS / 1000

_BIG_FILES_CACHE_SECONDS = config.VIEWER_CACHE_TTL
_RECORD_CACHE_SECONDS = config.VIEWER_CACHE_TTL
_BIG_FILES_CAP = 20
_big_files_cache = TTLCache[list[tuple[str, int]]](
    ttl_seconds=_BIG_FILES_CACHE_SECONDS
)

_record_files_cache = TTLCache[list[tuple[str, str]]](
    ttl_seconds=_RECORD_CACHE_SECONDS
)

_GIT_FETCH_CACHE_SECONDS = config.GIT_FETCH_CACHE_SECONDS
git_fetch_cache: TTLCache[tuple[float, bool]] = TTLCache(
    ttl_seconds=_GIT_FETCH_CACHE_SECONDS
)
_GIT_FETCH_KEY = "main"

_SKIP_DIRS = {".git", "__pycache__", ".ruff_cache", ".pytest_cache", "node_modules"}

_RECORD_FILES = ("CHARTER.md", "HISTORY.md", "CITIZENS.md", "REASONING.md")


def _record_file_sizes(repo_root: Path) -> list[tuple[str, str]]:
    """Return (name, human_size) for each record file, cached 60s."""
    key = str(repo_root)
    cached = _record_files_cache.get(key)
    if cached is not None:
        return cached
    return _record_files_cache.get_or_compute(
        key,
        lambda: [
            (name, _human_bytes((repo_root / name).stat().st_size))
            for name in _RECORD_FILES
            if (repo_root / name).is_file()
        ],
    )


def _big_py_files(repo_root: Path, threshold: int) -> list[tuple[str, int]]:
    """Walk *repo_root* and return .py files with >= *threshold* lines.

    Each entry is ``(relative_path, line_count)`` sorted largest-first.
    Directories in ``_SKIP_DIRS`` are pruned.  Encoding errors are ignored
    so the scan never 500s on a broken file.  Only the top ``_BIG_FILES_CAP``
    are returned.
    """
    key = (str(repo_root), int(threshold))
    cached = _big_files_cache.get(key)
    if cached is not None:
        return cached
    return _big_files_cache.get_or_compute(
        key,
        lambda: _scan_big_py_files(repo_root, threshold),
    )


def _scan_big_py_files(
    repo_root: Path, threshold: int
) -> list[tuple[str, int]]:
    """Actual scan; factored out so TTLCache.get_or_compute's compute arg
    is a small, named, easy-to-mock function.
    """
    results: list[tuple[str, int]] = []
    for path in sorted(repo_root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                count = sum(1 for _ in f)
        except OSError:
            continue
        if count >= threshold:
            results.append((path.relative_to(repo_root).as_posix(), count))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:_BIG_FILES_CAP]


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
    """Run a git command and report whether it exited 0 (success)."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=config.GITHUB_HTTP_TIMEOUT_SECONDS,
        )
        return result.returncode == 0
    except Exception:  # domain: degrade-silently - git check failure degrades to not-ok
        return False


def _git_sync_status() -> dict[str, object]:
    try:
        repo_root = _git(["rev-parse", "--show-toplevel"], str(db.REPO_DIR))
        if not repo_root:
            return {"error": "not a git repository"}
        cached = git_fetch_cache.get(_GIT_FETCH_KEY)
        if cached is None:
            ok = _git_ok(["fetch", "origin", "main"], repo_root)
            now = time.monotonic()
            git_fetch_cache.set(_GIT_FETCH_KEY, (now, ok))
            cached = (now, ok)
        last_fetch_ts, ok = cached
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
            "head_author": _git(["rev-parse", "--format=%an"], repo_root),
            "head_date": _git(["log", "-1", "--format=%cI"], repo_root),
            "dirty": bool(_git(["status", "--porcelain"], repo_root)),
            "commits_ahead": int(ahead),
            "commits_behind": int(behind),
            "stale": not ok,
            "last_fetch": last_fetch_ts,
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _timed(
    label: str, fn: Callable[[], Any]
) -> tuple[str, Any, float, str | None]:
    """Call *fn* synchronously and time it.  Returns a 4-tuple of
    (label, value, elapsed_ms, error) so the status page can show its own
    read latencies."""
    start = time.perf_counter()
    try:
        value = await asyncio.to_thread(fn)
        return label, value, (time.perf_counter() - start) * 1000, None
    except Exception as exc:
        return (
            label,
            None,
            (time.perf_counter() - start) * 1000,
            f"{type(exc).__name__}: {exc}",
        )


def _human_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"


_STATUS_CACHE: TTLCache[tuple[dict, dict, dict, list | None]] = TTLCache(
    ttl_seconds=5.0
)

_TOP_TABLES_CACHE_SECONDS = 300
_top_tables_cache: TTLCache[
    tuple[list[tuple[str, int, int, int, int, int]], int | None]
] = TTLCache(ttl_seconds=_TOP_TABLES_CACHE_SECONDS)


def _storage_table_rows(
    conn: Any, limit: int = 10
) -> tuple[list[tuple[str, int, int, int, int, int]], int | None]:
    """Per-table storage facts for the Storage & tables panel."""
    key = "storage_tables"
    cached = _top_tables_cache.get(key)
    if cached is not None:
        return cached
    return _top_tables_cache.get_or_compute(
        key, lambda: _scan_storage_table_rows(conn, limit)
    )


def _scan_storage_table_rows(
    conn: Any, limit: int
) -> tuple[list[tuple[str, int, int, int, int, int]], int | None]:
    """Actual scan; factored out so TTLCache.get_or_compute's compute arg
    is a small, named, easy-to-mock function.
    """
    pages_map: dict[str, int] = {}
    bytes_map: dict[str, int] = {}
    overflow_map: dict[str, int] = {}
    total_bytes: int | None = None
    try:
        attributed = conn.execute(
            "SELECT COALESCE(sm.tbl_name, d.name) AS tname,"
            " COUNT(*) AS pages,"
            " SUM(d.pgsize) AS bytes,"
            " SUM(CASE WHEN d.pagetype = 'overflow' THEN 1 ELSE 0 END) AS overflow"
            " FROM dbstat d"
            " LEFT JOIN sqlite_master sm ON d.name = sm.name"
            " WHERE COALESCE(sm.tbl_name, d.name) NOT LIKE 'sqlite_%'"
            " GROUP BY tname"
        ).fetchall()
        for r in attributed:
            pages_map[r[0]] = int(r[1])
            bytes_map[r[0]] = int(r[2] or 0)
            overflow_map[r[0]] = int(r[3])
        total_bytes = sum(bytes_map.values())
    except Exception:
        pass
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    tables: list[tuple[str, int, int, int, int, int]] = []
    for r in rows:
        tname = r[0]
        try:
            cnt = int(
                conn.execute(f'SELECT COUNT(*) AS n FROM "{tname}"').fetchone()[0]
            )
        except Exception:
            cnt = 0
        try:
            idx = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
                    " AND tbl_name=? AND name NOT LIKE 'sqlite_auto_index_%'",
                    (tname,),
                ).fetchone()[0]
            )
        except Exception:
            idx = 0
        tables.append(
            (
                tname,
                cnt,
                idx,
                pages_map.get(tname, 0),
                overflow_map.get(tname, 0),
                bytes_map.get(tname, 0),
            )
        )
    tables.sort(key=lambda t: (t[5], t[1]), reverse=True)
    return (tables[:limit], total_bytes)


_NETWORK_TIMEOUT_SECONDS = 10


async def _status_reads(
    force: bool = False,
) -> tuple[dict, dict, dict, list | None]:
    """The status page's shared reads: (by_name, latency, repo, prs)."""
    cached = _STATUS_CACHE.get("status") if not force else None
    if cached is not None:
        return cached
    repo_task = asyncio.create_task(asyncio.to_thread(_git_sync_status))

    from viewer._pr_helpers import _open_prs as _viewer_open_prs

    prs_task = asyncio.create_task(_viewer_open_prs())

    reads = await asyncio.gather(
        _timed("integrity_ok", db.integrity_ok),
        _timed("counts", aggregates.counts),
        _timed("list_agents", aggregates.list_agents),
        _timed("list_reports", reports.list_reports),
        _timed("list_proposals", db.list_proposals),
        _timed(
            "list_recent_activity", lambda: aggregates.shared_recent_activity(50)
        ),
        _timed("storage_stats", db.storage_stats),
        _timed("schema_version", db.schema_version),
        _timed("process_info", db.process_info),
        _timed("event_total", events.event_total),
    )
    latency = {label: ms for label, _, ms, _ in reads}
    by_name = {label: value for label, value, _, _ in reads}
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
    if not _repo_timeout and not _prs_timeout:
        _STATUS_CACHE.set("status", result)
    return result


def _status_checks(by_name: dict, repo: dict, prs: list | None) -> list[dict]:
    """The self-check list, shared by the status page and its banner fragment."""
    checks: list[dict] = []
    integrity = by_name.get("integrity_ok")
    if integrity is True:
        checks.append({"label": "db integrity", "level": "ok"})
    elif integrity is False:
        checks.append({"label": "db integrity", "level": "fail"})
    else:
        checks.append({"label": "db integrity", "level": "warn"})
    schema_v = by_name.get("schema_version")
    if schema_v == db.CURRENT_SCHEMA_VERSION:
        checks.append({"label": "schema version", "level": "ok"})
    else:
        checks.append(
            {
                "label": f"schema: expected {db.CURRENT_SCHEMA_VERSION}, got {schema_v}",
                "level": "fail",
            }
        )
    if prs is not None:
        checks.append({"label": "GitHub reachable", "level": "ok"})
    else:
        checks.append({"label": "GitHub reachable", "level": "warn"})
    if not repo.get("error") and not repo.get("dirty"):
        checks.append({"label": "git working tree", "level": "ok"})
    elif repo.get("dirty"):
        checks.append({"label": "git working tree", "level": "warn", "detail": "uncommitted changes"})
    else:
        checks.append({"label": "git repository", "level": "fail", "detail": str(repo.get("error", ""))})
    return checks


def _status_level(check: dict) -> str:
    """Translate a check dict into an ok/warn/fail level."""
    if check["level"] in ("ok", "fail", "warn"):
        return check["level"]
    return "warn"
