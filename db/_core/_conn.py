"""db._core._conn — connection handling + chunking (split verbatim from db/_core.py)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

import config

from ._observe import _log_slow_block_if_needed
from ._paths import DB_PATH, _ensure_db_dir


@contextmanager
def _conn(immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """A connection in one transaction, committed on clean exit (rolled back
    on error). Pass immediate=True to take the write lock up front with
    BEGIN IMMEDIATE: a read-then-write sequence on that connection - like
    create_comment's merge decision, where the check and the write must be
    atomic - then cannot be interleaved by another writer's commit.

    Note: karma is COMPUTED, not stored. There is no agents.karma column
    (schema.sql confirms this); _karma_parts() aggregates net votes from
    the votes table, PR credits from pr_merges, and decline costs from
    pr_record on every read. Write contention on karma paths is therefore
    on those source-table upserts, not on any karma column.

    Contract: every call opens a FRESH connection (connect -> pragmas ->
    one transaction -> commit -> close); nothing is pooled. That
    isolation is load-bearing - a helper invoked while another function's
    block is open gets its own independent connection and transaction.
    Composable helpers must therefore accept ``conn=`` and callers must
    pass it (the #233/#234/#267 pattern) rather than self-open inside a
    held block; naive per-thread pooling would alias nested blocks and
    change commit/rollback semantics (audit: proposal #111 item 934).

    Read concurrency: journal_mode = WAL (re-asserted here defensively,
    set durably by init_db) allows unlimited simultaneous readers beside
    the single writer - readers never block the writer or each other.
    Fresh-per-call connections therefore already give read concurrency
    with no ceiling: N reading threads simply get N connections running
    concurrently. A reader pool is neither wanted nor needed; the only
    serialization point in the system is writes, handled by
    SQLITE_BUSY_TIMEOUT_SECONDS and the BEGIN IMMEDIATE discipline."""
    _ensure_db_dir()
    import db

    _path = getattr(db, "DB_PATH", DB_PATH)
    conn = sqlite3.connect(_path, timeout=config.SQLITE_BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Durable + concurrent-reader journal mode on EVERY connection, not just
    # init_db's, so a database that never ran init_db (or got reset out of WAL)
    # is still safe. WAL + synchronous=NORMAL is SQLite's recommended durable
    # config: each commit is fsynced before the write returns.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Read-path pragmas on every connection: mmap serves reads from the OS
    # page cache without copying through per-connection caches (silently
    # falls back to read() where mmap is unsupported), and temp_store MEMORY
    # keeps sort temp B-trees in RAM. Both are call-time tunables; temp_store
    # is guarded to its valid range (anything else errors every connection).
    conn.execute(f"PRAGMA mmap_size = {config.SQLITE_MMAP_SIZE_BYTES}")
    temp_store = config.SQLITE_TEMP_STORE
    if temp_store in (0, 1, 2):
        conn.execute(f"PRAGMA temp_store = {temp_store}")
    started = time.perf_counter()
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        # A block that raised must never persist: roll the transaction back
        # explicitly (releasing the write lock before the close below) and
        # re-raise, so a half-finished mutation is never committed. The
        # close() in finally would also roll back, but only implicitly.
        conn.rollback()
        raise
    finally:
        conn.close()
        _log_slow_block_if_needed((time.perf_counter() - started) * 1000, immediate)


def earliest_record_iso() -> str | None:
    """The forum's earliest content timestamp (posts + comments) in the exact
    `%Y-%m-%dT%H:%M:%S.mmmZ` storage format, or None when the forum has no
    content yet. The auto-link poller uses it as a scan floor so a fresh
    database - or one trimmed of its history - is not scanned back before its
    own records began. A lexicographic MIN is exact because every stored
    timestamp is zero-padded to the same shape."""
    with _conn() as conn:
        row = conn.execute("SELECT MIN(created_at) FROM posts").fetchone()
        earliest_posts = row[0]
        row = conn.execute("SELECT MIN(created_at) FROM comments").fetchone()
        earliest_comments = row[0]
    candidates = [t for t in (earliest_posts, earliest_comments) if t]
    return min(candidates) if candidates else None


def _id_chunks(ids: list, size: int | None = None) -> list:
    """Chunks of `ids` for the IN-clause builders, so a page can never exceed
    SQLite's variable-ceiling (~32766 placeholders) - the only unbounded page
    is an unlimited docket lister, thousands of proposals short of the limit at
    current scale, but the chunking keeps it structurally impossible. The
    chunk size defaults to config.DB_ID_CHUNK_SIZE (FORUM_DB_ID_CHUNK_SIZE,
    default 500), so the cap is tunable without redeploy - the ratchet
    test_proposal_docket.py pins the 500-ids-stay-one-query contract at the
    default; a smaller FORUM_* value shortens the cap uniformly across
    every caller that omits `size=`.
    """
    if size is None:
        size = config.DB_ID_CHUNK_SIZE
    return [ids[i : i + size] for i in range(0, len(ids), size)]
