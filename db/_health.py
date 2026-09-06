"""db._health — schema diagnostics."""

from __future__ import annotations

import os
import platform
import sqlite3
import time

from db._core import (
    DB_PATH,
    _conn,
    slow_block_stats,
    stats_refreshed_at,
)

# Captured when this module first loads (seconds after true process start):
# the denominator for process_info()'s uptime figure.
_LOADED_MONO = time.monotonic()


def schema_version() -> int:
    """The database's PRAGMA user_version (0 for the initial schema)."""
    with _conn() as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def integrity_ok() -> bool:
    """Run PRAGMA quick_check and report whether the database is intact."""
    with _conn() as conn:
        return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def storage_stats() -> dict:
    """SQLite size and journaling metrics for ops dashboards (read-only):
    page_count * page_size is the file's size in bytes, freelist_count is
    reclaimable pages, journal_mode / auto_vacuum describe how writes are
    journaled, and sqlite_version names the engine actually linked into this
    process (the ground truth after a library or OS upgrade).
    Protocol-agnostic - it is just numbers and one string."""
    with _conn() as conn:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        try:
            wal_bytes: int | None = os.path.getsize(str(DB_PATH) + "-wal")
        except OSError:
            # domain: degrade-silently - no -wal file right now is the normal
            # steady state; /status shows a dash and nothing is lost.
            wal_bytes = None
        return {
            "sqlite_version": sqlite3.sqlite_version,
            "wal_bytes": wal_bytes,
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": conn.execute("PRAGMA freelist_count").fetchone()[0],
            "auto_vacuum": conn.execute("PRAGMA auto_vacuum").fetchone()[0],
            "size": page_count * page_size,
        }


def process_info() -> dict:
    """Runtime facts for /status's Process panel: which interpreter this is
    (the ground truth during a Python upgrade), the process id, how long the
    process has been up, when the planner statistics were last refreshed by
    init_db(), and the slow-block counters. Protocol-agnostic."""
    return {
        "python_version": platform.python_version(),
        "pid": os.getpid(),
        "uptime_seconds": int(time.monotonic() - _LOADED_MONO),
        "stats_refreshed_at": stats_refreshed_at(),
        **slow_block_stats(),
    }
