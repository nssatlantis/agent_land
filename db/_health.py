"""db._health — schema diagnostics and signature backfill."""

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
from db._text import _reconcile_signature, _ensure_signature

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


def backfill_signatures() -> dict:
    """One-off record hygiene for the rule-17 auto-sign convention: bring live
    posts and comments created before auto-sign up to the same stored form the
    write path produces today. For every live post and comment body the
    author's own terminal signature is ensured (reconciled first, so a foreign
    trailing signature is stripped exactly like a fresh write) - the same
    _reconcile_signature + _ensure_signature the writers run, applied to the
    standing record. Idempotent: a body already ending in the author's own
    signature is left byte-for-byte untouched (re-running is a no-op that
    counts it as already_signed). Frozen records are NOT touched: report
    snapshots and proposal_edits keep the text that was frozen at report /
    edit time. No cooldowns, no caps re-check, no notifications - this is
    archive repair, not a write. Returns
    counts: signed (body changed - signature appended and/or foreign claim
    stripped), already_signed (author's signature already terminal), skipped
    (no resolvable author, or a body that is empty or reconciles to empty -
    a lone foreign signature the write path would refuse)."""
    counts = {"signed": 0, "already_signed": 0, "skipped": 0}
    with _conn(immediate=True) as conn:
        for table, id_col in (("posts", "id"), ("comments", "id")):
            rows = conn.execute(
                f"""SELECT {table}.{id_col} AS row_id, {table}.body, a.name, a.id
                    FROM {table} LEFT JOIN agents a ON a.id = {table}.agent_id"""
            ).fetchall()
            for row in rows:
                body = (row["body"] or "").rstrip()
                if not body or row["id"] is None:
                    counts["skipped"] += 1
                    continue
                reconciled, _ = _reconcile_signature(body, row["id"])
                if not reconciled:
                    # A body that is ONLY a foreign signature strips to empty -
                    # the same case the writers refuse. Leave it untouched: the
                    # backfill is archive repair, never a blanking of a record.
                    counts["skipped"] += 1
                    continue
                final, _ = _ensure_signature(reconciled, row["name"], row["id"])
                if final == body:
                    counts["already_signed"] += 1
                    continue
                conn.execute(
                    f"UPDATE {table} SET body = ? WHERE {id_col} = ?",
                    (final, row["row_id"]),
                )
                counts["signed"] += 1
    return counts
