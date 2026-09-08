"""db._core._observe — sqlite observability state (split verbatim from db/_core.py)."""

from __future__ import annotations

import config

from ._time import _now_iso

_slow_block_count = 0
_last_slow_block: dict | None = None
_stats_refreshed_at: str | None = None


def slow_block_stats() -> dict:
    """How many db blocks have logged as slow since process start, plus the
    most recent one. The UI face of FORUM_SQLITE_SLOW_BLOCK_MS - a rising
    count after an engine or schema change is the signal to look closer."""
    return {"count": _slow_block_count, "last": _last_slow_block}


def stats_refreshed_at() -> str | None:
    """When init_db last ran the ANALYZE + optimize refresh (None until the
    first boot with that code path). Confirms on /status that the planner
    statistics are fresh after an upgrade."""
    return _stats_refreshed_at


def _log_slow_block_if_needed(elapsed_ms: float, immediate: bool) -> None:
    """Emit one structured 'sqlite_slow_block' event for a database block
    that ran at least FORUM_SQLITE_SLOW_BLOCK_MS (0 disables). Observability,
    not enforcement: the point is a before/after evidence trail for schema,
    index and engine changes - e.g. when comparing plans across a SQLite or
    OS-level library upgrade."""
    threshold = config.SQLITE_SLOW_BLOCK_MS
    if threshold > 0 and elapsed_ms >= threshold:
        global _slow_block_count, _last_slow_block
        _slow_block_count += 1
        _last_slow_block = {
            "ms": round(elapsed_ms, 1),
            "immediate": immediate,
            "at": _now_iso(),
        }
        try:
            import logutil
        except ImportError:
            # domain: degrade-silently - observability must never raise,
            # least of all out of a contextmanager's __exit__: bare
            # contexts (deploy scripts run with their own sys.path) may
            # not have the repo root importable, and this counter still
            # surfaced on /status regardless of whether the log line fired.
            return

        logutil.log(
            "sqlite_slow_block",
            ms=round(elapsed_ms, 1),
            threshold=threshold,
            immediate=immediate,
        )


def _set_stats_refreshed_at(value: str | None) -> None:
    """Stamp the planner-stats refresh time (written once per init_db boot)."""
    global _stats_refreshed_at
    _stats_refreshed_at = value
