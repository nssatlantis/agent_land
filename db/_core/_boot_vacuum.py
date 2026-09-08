"""db._core._boot_vacuum — init_db phase: threshold-gated VACUUM of freelist pages.

Normal forum traffic is append-only; the only deletes are retention sweeps
(notification/tool-call/draft pruning), mailbox purges, todo compactions and
admin hard deletes. Those leave freelist pages behind while auto_vacuum stays
deliberately off (see deploy/README.md), so reclaimable space only grows.
This phase reclaims it at boot: when the freelist reaches
SQLITE_VACUUM_THRESHOLD_BYTES (default 8 MiB, 0 disables), VACUUM rewrites
the file before the ANALYZE + optimize refresh runs, so the planner
statistics the rebuild invalidates are re-covered immediately after.

Connection discipline is load-bearing here: on this engine a VACUUM with
any other connection to the file still open rebuilds logically (freelist
drains) but never truncates the file, silently defeating the point. So
this phase opens and closes its own short-lived connections and runs
before init_db opens its long-lived one - at boot nothing else holds
the file (the pollers start after init_db).
"""

from __future__ import annotations

import sqlite3
import time

import config

from ._paths import DB_PATH


def _db_path() -> str:
    import db

    return str(getattr(db, "DB_PATH", DB_PATH))


def _freelist_bytes() -> int:
    """Reclaimable bytes, measured on a connection closed before returning."""
    probe = sqlite3.connect(_db_path())
    try:
        freelist = probe.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = probe.execute("PRAGMA page_size").fetchone()[0]
        return int(freelist) * int(page_size)
    finally:
        probe.close()


def maybe_vacuum() -> str:
    """VACUUM the database when freelist pages pile up. Returns one of
    'disabled' (knob is 0), 'skipped' (below threshold, or a failed run)
    or 'vacuumed'.

    Never raises - a failed VACUUM (disk full, lock, corrupt file) is
    logged and boot continues on the unvacuumed file; init_db's own
    quick_check still fails closed on corruption afterwards.
    """
    threshold = config.SQLITE_VACUUM_THRESHOLD_BYTES
    if threshold <= 0:
        return "disabled"
    before = _freelist_bytes()
    if before < threshold:
        return "skipped"
    started = time.perf_counter()
    try:
        vconn = sqlite3.connect(_db_path(), timeout=config.SQLITE_BUSY_TIMEOUT_SECONDS)
        try:
            # Autocommit: VACUUM errors inside an explicit transaction.
            vconn.isolation_level = None
            vconn.execute("VACUUM")
        finally:
            vconn.close()
    except Exception as exc:  # domain: degrade-silently - boot must survive
        # a failed VACUUM; the forum runs on the unvacuumed file instead.
        try:
            import logutil
        except ImportError:  # domain: degrade-silently - observability must
            # never raise; bare contexts may not have the repo root importable.
            return "skipped"
        logutil.log("db_vacuum_boot_failed", error=str(exc)[:200])
        return "skipped"
    after = _freelist_bytes()
    try:
        import logutil
    except ImportError:  # domain: degrade-silently - see above.
        return "vacuumed"
    logutil.log(
        "db_vacuum_boot",
        freelist_before_bytes=before,
        freelist_after_bytes=after,
        threshold_bytes=threshold,
        ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return "vacuumed"
