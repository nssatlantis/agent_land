"""viewer._pr_helpers - shared PR helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import config
from db import _content, _pr_vote

if TYPE_CHECKING:
    pass


def _pr_state_label(state: str) -> str:
    return {
        "open": "open",
        "merged": "merged",
        "declined": "declined",
        "closed": "withdrawn",
    }.get(state, state)


def _pr_state_class(state: str) -> str:
    return {
        "open": "label-success",
        "merged": "label-info",
        "declined": "label-danger",
        "closed": "label-default",
    }.get(state, "")


def _format_sha(sha: str, length: int = 7) -> str:
    return sha[:length] if sha else "—"


_pr_prs_cache: TTLCache[list[dict] | None] = TTLCache(ttl_seconds=300.0)
_pr_diff_cache: TTLCache[tuple[dict | None, bool]] = TTLCache(ttl_seconds=300.0)
_prs_state_cache: TTLCache[list[dict] | None] = TTLCache(ttl_seconds=60.0)

_pr_prs_last = {"time": datetime(2000, 1, 1, tzinfo=timezone.utc)}


def _fetch_pr_list() -> list[dict]:
    """Return enriched PR list: number, title, author, state, created, updated."""
    now = datetime.now(timezone.utc)
    _pr_prs_last["time"] = now
    rows = _content.list_prs(state="open")
    result = []
    for r in rows:
        num = r["number"]
        diff, has_conflict = pr_diff(num)
        votes = _pr_vote.pr_vote_tally(num)
        result.append(
            {
                "number": num,
                "title": r["title"],
                "author": r.get("author", "unknown"),
                "state": r["state"],
                "created_at": r["created_at"],
                "updated_at": r.get("updated_at"),
                "diff": diff,
                "has_conflict": has_conflict,
                "votes": votes,
            }
        )
    return result


def _fetch_pr_diff(pr_number: int) -> tuple[dict | None, bool]:
    """Return (diff_dict, has_conflict) for one PR."""
    try:
        import urllib.request

        url = f"https://github.com/{config.REPO_FULL}/pull/{pr_number}.diff"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        has_conflict = "console.log('CONFLICT')" in raw or "<<<<<<<" in raw
        lines = raw.splitlines()
        files = []
        current_file = None
        for line in lines:
            if line.startswith("diff --git"):
                parts = line.split()
                if len(parts) >= 4:
                    current_file = parts[3].strip()
                    files.append({"path": current_file, "additions": 0, "deletions": 0, "patch": []})
            elif current_file is not None and line.startswith(("+", "-")):
                if not line.startswith(("+++", "---")):
                    if files:
                        files[-1]["additions" if line.startswith("+") else "deletions"] += 1
                        files[-1]["patch"].append(line)
        return {
            "files": files,
            "total_additions": sum(f["additions"] for f in files),
            "total_deletions": sum(f["deletions"] for f in files),
        }, has_conflict
    except Exception:
        return None, False


def pr_list() -> list[dict]:
    """Return cached PR list, refreshed every 5 minutes."""
    return _pr_prs_cache.get_or_compute("list", _fetch_pr_list) or []


def pr_diff(pr_number: int) -> tuple[dict | None, bool]:
    """Return (diff_dict, has_conflict) for one PR, cached for 5 minutes."""
    return _pr_diff_cache.get_or_compute(pr_number, lambda: _fetch_pr_diff(pr_number))


def _fetch_prs_state() -> list[dict]:
    rows = _content.list_prs(state="open")
    return [
        {"number": r["number"], "title": r["title"], "state": r["state"]}
        for r in rows
    ]


def prs_state() -> list[dict]:
    """Return minimal PR state list, cached for 60 seconds."""
    return _prs_state_cache.get_or_compute("state", _fetch_prs_state) or []