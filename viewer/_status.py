"""viewer._status - repository status panel."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import config

from ._utils import TTLCache

_REPO_ROOT = Path(config.REPO_PATH).resolve()

_BIG_FILE_THRESHOLD = 500_000

_MAX_CONFIG_LINES = 10

_record_files_cache: TTLCache[list[str]] = TTLCache(ttl_seconds=300.0)
_big_files_cache: TTLCache[list[tuple[str, int]]] = TTLCache(ttl_seconds=300.0)
_git_fetch_cache: TTLCache[bool] = TTLCache(ttl_seconds=300.0)
_git_fetch_last = {"time": datetime(2000, 1, 1, tzinfo=timezone.utc)}


def _git_fetch_if_needed() -> bool:
    """Re-run `git fetch` if the last run is older than 5 minutes."""
    key = "fetch"
    if _git_fetch_cache.get(key):
        return True
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=_REPO_ROOT,
            capture_output=True,
            timeout=30,
        )
        _git_fetch_last["time"] = datetime.now(timezone.utc)
        _git_fetch_cache.set(key, True)
        return True
    except Exception:
        _git_fetch_cache.set(key, False)
        return False


def _ahead_behind() -> tuple[int, int]:
    """Return (ahead, behind) counts vs origin/main."""
    try:
        result = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 0, 0


def _commit_info() -> dict:
    """Return (sha, date, message) for HEAD."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", '--format=%H|%aI|%s'],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split("|", 2)
            if len(parts) == 3:
                return {"sha": parts[0], "date": parts[1], "message": parts[2]}
    except Exception:
        pass
    return {"sha": "unknown", "date": "unknown", "message": "unknown"}


def _big_py_files(repo_root: Path, threshold: int) -> list[tuple[str, int]]:
    key = (str(repo_root), int(threshold))
    return _big_files_cache.get_or_compute(
        key, lambda: _scan_big_py_files(repo_root, threshold)
    )


def _scan_big_py_files(repo_root: Path, threshold: int) -> list[tuple[str, int]]:
    """Return [(path, size), ...] for .py files above threshold bytes."""
    result = []
    for py_file in repo_root.glob("**/*.py"):
        try:
            size = py_file.stat().st_size
            if size >= threshold:
                result.append((str(py_file.relative_to(repo_root)), size))
        except Exception:
            pass
    result.sort(key=lambda x: x[1], reverse=True)
    return result


_status_cache: TTLCache[dict] = TTLCache(ttl_seconds=60.0)


def _status_reads() -> dict:
    """Return the full repository status dict, cached for 60 seconds."""
    key = "status"
    cached = _status_cache.get(key)
    if cached is not None:
        return cached
    data = _build_status()
    _status_cache.set(key, data)
    return data


def _build_status() -> dict:
    """Return a dict describing the current repo state."""
    _git_fetch_if_needed()
    sha_info = _commit_info()
    ahead, behind = _ahead_behind()
    py_files = _big_py_files(_REPO_ROOT, _BIG_FILE_THRESHOLD)
    record_files = _record_files()
    last_fetch = _git_fetch_last["time"]
    return {
        "sha": sha_info["sha"],
        "date": sha_info["date"],
        "message": sha_info["message"],
        "ahead": ahead,
        "behind": behind,
        "big_py_files": py_files,
        "record_files": record_files,
        "last_fetch": last_fetch,
    }


def status_html() -> str:
    """Return the repository status panel HTML."""
    data = _status_reads()
    sha = data["sha"]
    date = data["date"]
    msg = data["message"]
    ahead = data["ahead"]
    behind = data["behind"]
    big = data["big_py_files"]
    records = data["record_files"]
    fetch_time = data["last_fetch"]
    sync = (
        "synced"
        if (ahead == 0 and behind == 0)
        else f"ahead={ahead} behind={behind}"
    )
    big_rows = "\n".join(
        f"<tr><td>{path}</td><td>{size // 1024}KB</td></tr>"
        for path, size in big[:10]
    )
    record_rows = "\n".join(f"<li>{r}</li>" for r in records)
    return (
        f'<div class="panel panel-info">'
        f'<div class="panel-heading">Repository Status</div>'
        f'<div class="panel-body">'
        f"<p><strong>SHA:</strong> <code>{sha[:7]}</code></p>"
        f"<p><strong>Date:</strong> {date}</p>"
        f"<p><strong>Message:</strong> {msg}</p>"
        f"<p><strong>Sync:</strong> {sync}</p>"
        f"<p><strong>Last fetch:</strong> {fetch_time}</p>"
        f"<p><strong>Big files:</strong></p>"
        f'<table class="table table-condensed">{big_rows}</table>'
        f"<p><strong>Record files:</strong></p>"
        f"<ul>{record_rows}</ul>"
        f"</div></div>"
    )


def _record_files() -> list[str]:
    """Return list of record .md files in the repo root."""
    key = "record"
    cached = _record_files_cache.get(key)
    if cached is not None:
        return cached
    result = []
    for name in ["CHARTER.md", "HISTORY.md", "CITIZENS.md", "AGENTS.md"]:
        p = _REPO_ROOT / name
        if p.exists():
            result.append(name)
    _record_files_cache.set(key, result)
    return result


_top_tables_cache: TTLCache[list[tuple[str, int]]] = TTLCache(ttl_seconds=300.0)


def top_tables() -> list[tuple[str, int]]:
    """Return top-10 tables by row count."""
    key = "storage_tables"
    return _top_tables_cache.pop(key, [])