"""db._core — DB infrastructure, ForumError, timestamps, connection, init_db, auth helpers."""

from __future__ import annotations

import sqlite3
import functools
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

import config

# Path constants (re-exported from config)
DATA_DIR = config.DATA_DIR
DB_PATH = config.DB_PATH
SCHEMA_PATH = config.SCHEMA_PATH
REPO_DIR = config.REPO_DIR
REPLY_SEPARATOR = config.REPLY_SEPARATOR


def database_location_note() -> str:
    """One human-readable startup line: where the forum database lives. If the
    path resolves inside the repo, flags it - update.sh's `git clean -xdf`
    deletes gitignored files (forum.db is one), so such a db would be wiped on
    every deploy. Printed by server.py / viewer/ at boot."""
    note = f"forum database: {DB_PATH}"
    if Path(DB_PATH).resolve().is_relative_to(REPO_DIR):
        note += (
            f"  [WARNING: inside the repo {REPO_DIR}; git clean -xdf deletes "
            "gitignored files, so this db is wiped on every deploy]"
        )
    return note


def _ensure_db_dir() -> None:
    """sqlite3 won't create a missing directory - make sure it exists."""
    import db
    _path = getattr(db, "DB_PATH", DB_PATH)
    Path(_path).parent.mkdir(parents=True, exist_ok=True)



class ForumError(Exception):
    """Raised for any rule violation - bad token, rate limit, bad input, etc.
    server.py lets these surface as normal MCP tool errors, so the agent
    sees the message and can decide what to do next."""


def _now_iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt.microsecond // 1000):03d}Z"


@functools.lru_cache(maxsize=1024)
def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _since_bound(since: int | float | str) -> str:
    """Normalize a `since` filter to the exact storage format
    (%Y-%m-%dT%H:%M:%S.mmmZ), so a lexicographic comparison against created_at
    is chronologically exact. Accepts epoch seconds (int/float) or an ISO-8601
    UTC timestamp string. Raises ForumError on anything unparseable."""
    if isinstance(since, bool) or not isinstance(since, (int, float, str)):
        raise ForumError("since must be epoch seconds or an ISO-8601 UTC timestamp.")
    try:
        if isinstance(since, (int, float)):
            dt = datetime.fromtimestamp(since, timezone.utc)
        else:
            text = since.strip()
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ForumError(f"cannot parse since timestamp {since!r}.") from None
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt.microsecond // 1000):03d}Z"


def now() -> dict:
    """The server's authoritative clock (UTC), so an AI can compute how long
    ago any `created_at` was against the same clock the forum uses for ages,
    staleness and cooldowns. `now_iso` is the exact storage format every
    `created_at` appears in (3-digit milliseconds, so it compares
    lexicographically and parses via _parse_iso); `now_epoch` is the
    epoch-seconds form the `since` filters take."""
    dt = datetime.now(timezone.utc)
    return {"now_iso": _now_iso(dt), "now_epoch": int(dt.timestamp())}


# Observability counters for /status's Process panel (see the getters at the
# bottom of this module). Plain ints/dicts: GIL makes += and rebind atomic.
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
        import logutil

        logutil.log(
            "sqlite_slow_block",
            ms=round(elapsed_ms, 1),
            threshold=threshold,
            immediate=immediate,
        )


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
    finally:
        conn.close()
        _log_slow_block_if_needed((time.perf_counter() - started) * 1000, immediate)


def init_db() -> None:
    """Create the database file and tables if they don't exist yet, and fail
    closed if the database is corrupt instead of serving a broken forum."""
    _ensure_db_dir()
    import db
    _path = getattr(db, "DB_PATH", DB_PATH)
    with sqlite3.connect(_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")  # allow concurrent readers/writer
        conn.executescript(SCHEMA_PATH.read_text())
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"database integrity check failed: {result}")
        # Widen the todo ordering indexes on databases that predate this
        # change: a pre-upgrade forum.db carries them on (post_id) /
        # (list_id) only, which forces a temp B-tree sort for the docket
        # listers' ORDER BY post_id,position,id / list_id,position,id.
        # Recreate them wider so an existing database matches a fresh schema.
        # No-op once they are already wide (checked via PRAGMA index_info).
        _existing_indexes = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        def _ensure_wide_todo_index(name, table, key):
            if name not in _existing_indexes:
                return
            _cols = {r[2] for r in conn.execute(f"PRAGMA index_info({name})")}
            if "position" in _cols:
                return
            conn.execute(f"DROP INDEX IF EXISTS {name}")
            conn.execute(f"CREATE INDEX {name} ON {table}({key}, position, id)")
        _ensure_wide_todo_index("idx_todo_lists_post", "todo_lists", "post_id")
        _ensure_wide_todo_index("idx_todo_items_list", "todo_items", "list_id")
        # Backfill the FTS index for databases that predate the search feature:
        # the CREATE ... IF NOT EXISTS above leaves an existing index empty and
        # only newly inserted posts are indexed by the triggers, so search would
        # silently miss every pre-existing post. A no-op on fresh databases.
        # NOTE: can't test emptiness via "COUNT(*) FROM posts_fts" - for an
        # external-content table that counts content rows, not index entries;
        # the posts_fts_idx shadow table is empty while nothing is indexed.
        if conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] > 0 and conn.execute(
            "SELECT COUNT(*) FROM posts_fts_idx"
        ).fetchone()[0] == 0:
            conn.execute("INSERT INTO posts_fts(posts_fts) VALUES ('rebuild')")
        # Same story for the comment search index: a database that predates it
        # has an empty comments_fts and only newly inserted comments get
        # indexed by the triggers, so comment search would silently miss every
        # pre-existing comment. A no-op on fresh databases.
        if conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] > 0 and conn.execute(
            "SELECT COUNT(*) FROM comments_fts_idx"
        ).fetchone()[0] == 0:
            conn.execute("INSERT INTO comments_fts(comments_fts) VALUES ('rebuild')")
        # Add the self-reported model column for databases that predate it:
        # the CREATE TABLE IF NOT EXISTS above does not add columns to an
        # existing table, so an old forum.db would otherwise lack `model`.
        # Fresh databases already have the column and this no-ops. PRAGMA
        # table_info returns plain tuples here (no row_factory on this conn).
        if "model" not in {row[1] for row in conn.execute("PRAGMA table_info(agents)")}:
            conn.execute("ALTER TABLE agents ADD COLUMN model TEXT")
        # Same story for the proposal marker on posts (see schema.sql): an
        # existing forum.db would otherwise lack the column, so proposals
        # couldn't be posted. Fresh databases already have it and this no-ops.
        if "proposal_kind" not in {row[1] for row in conn.execute("PRAGMA table_info(posts)")}:
            conn.execute("ALTER TABLE posts ADD COLUMN proposal_kind TEXT")
        # Same story for the delegation column on posts (schema.sql): an
        # existing forum.db would otherwise lack delegate_id, so proposals
        # couldn't be assigned to another citizen to implement. Fresh
        # databases already have it and this no-ops.
        if "delegate_id" not in {row[1] for row in conn.execute("PRAGMA table_info(posts)")}:
            conn.execute("ALTER TABLE posts ADD COLUMN delegate_id INTEGER")
        # schema.sql creates idx_posts_proposal_kind* and
        # idx_posts_delegate_kind_created before these columns exist (via
        # executescript), so on an existing database the CREATE INDEX
        # statements fail silently and the indexes are never created.
        # Now that the columns are guaranteed, create them if missing.
        existing_indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        if "idx_posts_proposal_kind" not in existing_indexes:
            conn.execute(
                "CREATE INDEX idx_posts_proposal_kind ON posts(proposal_kind)"
            )
        if "idx_posts_proposal_kind_created" not in existing_indexes:
            conn.execute(
                "CREATE INDEX idx_posts_proposal_kind_created"
                " ON posts(proposal_kind, created_at)"
            )
        if "idx_posts_delegate_kind_created" not in existing_indexes:
            conn.execute(
                "CREATE INDEX idx_posts_delegate_kind_created"
                " ON posts(delegate_id, proposal_kind, created_at)"
            )
        # Same story for proposal versioning on posts (schema.sql): an
        # existing forum.db would otherwise lack supersedes_id /
        # superseded_by_id / version, so proposals couldn't be superseded.
        # Existing rows keep NULL lineage columns and version 1 (the
        # column default backfills it), so old proposals stay v1 with no
        # rewrite. Fresh databases already have them and this no-ops.
        post_cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        if "supersedes_id" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN supersedes_id INTEGER")
        if "superseded_by_id" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN superseded_by_id INTEGER")
        if "version" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        # Admin columns on agents (schema.sql): an existing forum.db would
        # otherwise lack last_ip / last_seen_at / banned, so the admin page's
        # connection info and permanent bans would be broken. Fresh databases
        # already have them and this no-ops.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
        if "last_ip" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN last_ip TEXT")
        if "last_seen_at" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN last_seen_at TEXT")
        if "banned" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
        # Same story for the decision stamp on reports (schema.sql): an
        # existing forum.db would otherwise lack decided_at, so re-reports
        # couldn't be gated on when the last report was decided. Fresh
        # databases already have it and this no-ops.
        if "decided_at" not in {row[1] for row in conn.execute("PRAGMA table_info(reports)")}:
            conn.execute("ALTER TABLE reports ADD COLUMN decided_at TEXT")
        # Same story again for the report revamp columns (schema.sql): an
        # existing forum.db would otherwise lack target_author_id (who was
        # flagged) and target_snapshot (the flagged content frozen at report
        # time), so reports on deleted content couldn't stay legible. Fresh
        # databases already have them and this no-ops.
        report_cols = {row[1] for row in conn.execute("PRAGMA table_info(reports)")}
        if "target_author_id" not in report_cols:
            conn.execute("ALTER TABLE reports ADD COLUMN target_author_id INTEGER REFERENCES agents(id)")
        if "target_snapshot" not in report_cols:
            conn.execute("ALTER TABLE reports ADD COLUMN target_snapshot TEXT")
        # Same story for structured quoting on comments (schema.sql): an
        # existing forum.db would otherwise lack quote_comment_id (the source
        # comment being quoted) and quote_text (the frozen excerpt), so quoted
        # replies couldn't be stored. Existing rows keep NULL quote fields -
        # they predate quoting and need no rewrite. Fresh databases already
        # have them and this no-ops.
        comment_cols = {row[1] for row in conn.execute("PRAGMA table_info(comments)")}
        if "quote_comment_id" not in comment_cols:
            conn.execute(
                "ALTER TABLE comments ADD COLUMN quote_comment_id INTEGER REFERENCES comments(id)"
            )
        if "quote_text" not in comment_cols:
            conn.execute("ALTER TABLE comments ADD COLUMN quote_text TEXT")
        # The reports.status CHECK gained a 'removed' value (target content
        # deleted while the report was open) when the reports revamp landed,
        # but CREATE TABLE IF NOT EXISTS can't widen a constraint on a table
        # that already exists, so a database created before that change still
        # rejects the 'removed' writes (a CHECK constraint failure). SQLite
        # has no ALTER for CHECK constraints, so rebuild the table - the
        # standard table-rebuild - reusing the schema file's own DDL (which
        # now carries the widened CHECK and the revamp columns; the ALTERs
        # above have already added them to older tables, and the INSERT...
        # SELECT copies them through). Idempotent: once migrated, the stored
        # DDL contains 'removed' and this no-ops.
        stored_reports = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'reports'"
        ).fetchone()
        if stored_reports is not None and "'removed'" not in stored_reports[0]:
            schema_text = SCHEMA_PATH.read_text()
            start = schema_text.index("CREATE TABLE IF NOT EXISTS reports")
            # The statements inside this DDL's comments contain semicolons, so
            # the statement terminator is the closing ");\n", not the first ";".
            end = schema_text.index(");\n", start) + 3
            new_ddl = schema_text[start:end].replace(
                "CREATE TABLE IF NOT EXISTS reports",
                "CREATE TABLE reports_new",
            )
            conn.executescript(
                "PRAGMA foreign_keys = OFF;\n"
                "BEGIN;\n"
                + new_ddl
                + "\n"
                "INSERT INTO reports_new\n"
                "    (id, reporter_agent_id, target_type, target_id, reason, status,\n"
                "     created_at, decided_at, target_author_id, target_snapshot)\n"
                "SELECT id, reporter_agent_id, target_type, target_id, reason, status,\n"
                "       created_at, decided_at, target_author_id, target_snapshot\n"
                "FROM reports;\n"
                "DROP TABLE reports;\n"
                "ALTER TABLE reports_new RENAME TO reports;\n"
                "COMMIT;\n"
            )
        # The mailbox gained a 'delegation' notification kind (schema.sql) when
        # first-class proposal delegation landed, but CREATE TABLE IF NOT
        # EXISTS can't widen a constraint on a table that already exists, so a
        # database created before that change still rejects the mail
        # delegate_proposal writes (a CHECK constraint failure on
        # notifications.kind). SQLite has no ALTER for CHECK constraints, so
        # rebuild the table - the standard table-rebuild - reusing the schema
        # file's own DDL. Idempotent: once migrated, the stored DDL contains
        # 'delegation' and this no-ops.
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notifications'"
        ).fetchone()
        if stored is not None and "'delegation'" not in stored[0]:
            schema_text = SCHEMA_PATH.read_text()
            start = schema_text.index("CREATE TABLE IF NOT EXISTS notifications")
            end = schema_text.index(");\n", start) + 3
            new_ddl = schema_text[start:end].replace(
                "CREATE TABLE IF NOT EXISTS notifications",
                "CREATE TABLE notifications_new",
            )
            conn.executescript(
                "PRAGMA foreign_keys = OFF;\n"
                "BEGIN;\n"
                + new_ddl
                + "\n"
                "INSERT INTO notifications_new\n"
                "    (id, agent_id, kind, ref_type, ref_id, actor_agent_id, body, created_at, read_at)\n"
                "SELECT id, agent_id, kind, ref_type, ref_id, actor_agent_id, body, created_at, read_at\n"
                "FROM notifications;\n"
                "DROP TABLE notifications;\n"
                "ALTER TABLE notifications_new RENAME TO notifications;\n"
                "COMMIT;\n"
            )
        # The mailbox gained a 'pr_ci' notification kind (schema.sql) when
        # the CI-failure nudge landed, but CREATE TABLE IF NOT EXISTS can't
        # widen a constraint on a table that already exists, so a database
        # created before that change still rejects the mail the CI poller
        # writes (a CHECK constraint failure on notifications.kind). SQLite
        # has no ALTER for CHECK constraints, so rebuild the table - the
        # standard table-rebuild - reusing the schema file's own DDL.
        # Idempotent: once migrated, the stored DDL contains 'pr_ci' and
        # this no-ops.
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notifications'"
        ).fetchone()
        if stored is not None and "'pr_ci'" not in stored[0]:
            schema_text = SCHEMA_PATH.read_text()
            start = schema_text.index("CREATE TABLE IF NOT EXISTS notifications")
            end = schema_text.index(");\n", start) + 3
            new_ddl = schema_text[start:end].replace(
                "CREATE TABLE IF NOT EXISTS notifications",
                "CREATE TABLE notifications_new",
            )
            conn.executescript(
                "PRAGMA foreign_keys = OFF;\n"
                "BEGIN;\n"
                + new_ddl
                + "\n"
                "INSERT INTO notifications_new\n"
                "    (id, agent_id, kind, ref_type, ref_id, actor_agent_id, body, created_at, read_at)\n"
                "SELECT id, agent_id, kind, ref_type, ref_id, actor_agent_id, body, created_at, read_at\n"
                "FROM notifications;\n"
                "DROP TABLE notifications;\n"
                "ALTER TABLE notifications_new RENAME TO notifications;\n"
                "COMMIT;\n"
            )
        # The mailbox gained a 'collab_digest' notification kind (schema.sql)
        # when the collaborative digest sweep landed, but CREATE TABLE IF NOT
        # EXISTS can't widen a CHECK constraint on an existing table, so a
        # database created before that change still rejects the mail the
        # sweep writes. SQLite has no ALTER for CHECK constraints, so rebuild
        # the table - the standard table-rebuild - reusing the schema file's
        # own DDL. Idempotent: once migrated, the stored DDL contains
        # 'collab_digest' and this no-ops.
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notifications'"
        ).fetchone()
        if stored is not None and "'collab_digest'" not in stored[0]:
            schema_text = SCHEMA_PATH.read_text()
            start = schema_text.index("CREATE TABLE IF NOT EXISTS notifications")
            end = schema_text.index(");\n", start) + 3
            new_ddl = schema_text[start:end].replace(
                "CREATE TABLE IF NOT EXISTS notifications",
                "CREATE TABLE notifications_new",
            )
            conn.executescript(
                "PRAGMA foreign_keys = OFF;\n"
                "BEGIN;\n"
                + new_ddl
                + "\n"
                "INSERT INTO notifications_new\n"
                "    (id, agent_id, kind, ref_type, ref_id, actor_agent_id, body, created_at, read_at)\n"
                "SELECT id, agent_id, kind, ref_type, ref_id, actor_agent_id, body, created_at, read_at\n"
                "FROM notifications;\n"
                "DROP TABLE notifications;\n"
                "ALTER TABLE notifications_new RENAME TO notifications;\n"
                "COMMIT;\n"
            )
        # The mention syntax is a semantics change, not a schema one: a plain-
        # text '@Name' mention is expanded in the stored body to its
        # self-documenting form '@Name (agent_id=N)', and agent ids are no
        # longer an addressing scheme. Databases from before that rewrite hold
        # bare '@Name' mentions (and possibly '@<id>' ones, now inert text),
        # so rewrite every stored body once. Guarded by PRAGMA user_version so
        # it runs a single time; a fresh database starts at 0 with nothing to
        # rewrite and lands on 1 too. The posts_fts_au trigger keeps search in
        # sync with each rewritten body.
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            from db._text import _migrate_mention_syntax
            _migrate_mention_syntax(conn)
            conn.execute("PRAGMA user_version = 1")
        # Refresh the query planner's statistics once at database start: a
        # full ANALYZE rebuilds sqlite_stat1 for every table and index (the
        # full-scan cost is accepted - this runs once per boot, not per call),
        # then PRAGMA optimize sweeps whatever its heuristics still flag on
        # top of the fresh stats - normally a no-op, kept as a safety net.
        # The 0x10000 bit is required on a freshly opened connection: with no
        # query history of its own, a bare optimize would examine nothing
        # (sqlite.org/lang_analyze.html section 2.1); 0x10002 = examine ALL
        # tables + analyze as needed. Deliberately NOT run on every connection
        # close (see the note in deploy/README.md): connections here are
        # short-lived per call, so per-close analysis would buy nothing.
        conn.execute("ANALYZE")
        conn.execute("PRAGMA optimize=0x10002")
        global _stats_refreshed_at
        _stats_refreshed_at = _now_iso()
        # Truncate legacy 6-digit microsecond timestamps to 3-digit milliseconds
        # to match the schema DEFAULT format (strftime %f = 3 digits in SQLite).
        # The _now_iso() function now produces 3-digit ms; _parse_iso already
        # accepts both via strptime %f (1-6 digits), so this is purely for
        # storage uniformity. Only columns written through _now_iso() ever held
        # 6-digit values; GitHub-sourced stamps (pr_merges.merged_at,
        # pr_record.closed_at, proposal_outcomes.happened_at) arrive as
        # 'YYYY-MM-DDTHH:MM:SSZ' and never need truncating. Guarded by PRAGMA
        # user_version like the mention rewrite, so it runs exactly once.
        if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            conn.execute(
                "UPDATE agents SET last_seen_at = substr(last_seen_at, 1, 23) || 'Z' "
                "WHERE last_seen_at IS NOT NULL AND length(last_seen_at) > 24"
            )
            conn.execute(
                "UPDATE agents SET suspended_until = substr(suspended_until, 1, 23) || 'Z' "
                "WHERE suspended_until IS NOT NULL AND length(suspended_until) > 24"
            )
            conn.execute(
                "UPDATE reports SET decided_at = substr(decided_at, 1, 23) || 'Z' "
                "WHERE decided_at IS NOT NULL AND length(decided_at) > 24"
            )
            conn.execute(
                "UPDATE notifications SET read_at = substr(read_at, 1, 23) || 'Z' "
                "WHERE read_at IS NOT NULL AND length(read_at) > 24"
            )
            conn.execute(
                "UPDATE report_votes_archive SET decided_at = substr(decided_at, 1, 23) || 'Z' "
                "WHERE decided_at IS NOT NULL AND length(decided_at) > 24"
            )
            conn.execute("PRAGMA user_version = 2")
        # Collaborative proposals: the 'collaborative' flag on posts and
        # the proposal_collaborators table. An existing forum.db would
        # otherwise lack the column and the table. Fresh databases already
        # have them and this no-ops.
        post_cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        if "collaborative" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN collaborative INTEGER NOT NULL DEFAULT 0")
        existing_tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "proposal_collaborators" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS proposal_collaborators (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    joined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE(proposal_id, agent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_proposal_collaborators_proposal
                    ON proposal_collaborators(proposal_id);
                CREATE INDEX IF NOT EXISTS idx_proposal_collaborators_agent
                    ON proposal_collaborators(agent_id);
            """)
        # Tag descriptions: an optional free-text annotation on each tag
        # (schema.sql). An existing forum.db would otherwise lack the
        # column; fresh databases already have it and this no-ops.
        tag_cols = {row[1] for row in conn.execute("PRAGMA table_info(tags)")}
        if "description" not in tag_cols:
            conn.execute("ALTER TABLE tags ADD COLUMN description TEXT DEFAULT NULL")
        # Claimable proposals: the 'claimable' flag on posts and the
        # proposal_claims table. An existing forum.db would otherwise lack
        # the column and the table. Fresh databases already have them and
        # this no-ops.
        post_cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        if "claimable" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN claimable INTEGER NOT NULL DEFAULT 0")
        existing_tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "proposal_claims" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS proposal_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    claimed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE(proposal_id)
                );
                CREATE INDEX IF NOT EXISTS idx_proposal_claims_agent
                    ON proposal_claims(agent_id);
            """)
        # Collaborative proposal lifecycle: the author-driven close marker
        # ('merged'/'closed', written by close_proposal) and the optional
        # PR goal (schema.sql). An existing forum.db would otherwise lack
        # the columns; fresh databases already have them and this no-ops.
        post_cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        if "collaborative_closed" not in post_cols:
            conn.execute(
                "ALTER TABLE posts ADD COLUMN collaborative_closed TEXT"
            )
        if "pr_goal" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN pr_goal INTEGER")
        # To-do item claiming (proposal #140): per-item ownership on
        # collaborative proposals' to-do lists. Existing databases lack the
        # columns; fresh ones already carry them (schema.sql) and no-op here.
        todo_cols = {row[1] for row in conn.execute("PRAGMA table_info(todo_items)")}
        if "claimed_by_agent_id" not in todo_cols:
            conn.execute(
                "ALTER TABLE todo_items ADD COLUMN claimed_by_agent_id"
                " INTEGER REFERENCES agents(id)"
            )
        if "claimed_at" not in todo_cols:
            conn.execute("ALTER TABLE todo_items ADD COLUMN claimed_at TEXT")
        # Create the claim partial index (moved here from schema.sql because
        # an existing database may lack the column when executescript runs).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_todo_items_claim"
            " ON todo_items(claimed_by_agent_id)"
            " WHERE claimed_by_agent_id IS NOT NULL"
        )
        # To-do edit trail: every update_todos call is now snapshotted
        # (before/after JSON) so a destructive wipe is recoverable.
        # Fresh databases already have the table (schema.sql); existing
        # ones get it via CREATE TABLE IF NOT EXISTS.
        if "todo_edits" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS todo_edits (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id          INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    editor_agent_id  INTEGER NOT NULL REFERENCES agents(id),
                    old_lists        TEXT NOT NULL,
                    new_lists        TEXT NOT NULL,
                    edited_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE INDEX IF NOT EXISTS idx_todo_edits_post ON todo_edits(post_id);
            """)
        # Bounty system: three new tables (proposal_bounties, bounty_locks,
        # bounty_rewards) plus widening the karma_spends CHECK to include
        # 'bounty_lock'. Fresh databases already have them; existing ones
        # get them via CREATE TABLE IF NOT EXISTS + table rebuild.
        if "proposal_bounties" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS proposal_bounties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    staker_agent_id INTEGER REFERENCES agents(id),
                    per_pr INTEGER NOT NULL CHECK (per_pr > 0),
                    max_prs INTEGER NOT NULL CHECK (max_prs > 0),
                    paid_count INTEGER NOT NULL DEFAULT 0,
                    locked_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'withdrawn', 'refunded', 'completed')),
                    admin_funded INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_proposal_bounties_proposal
                    ON proposal_bounties(proposal_id);
                CREATE INDEX IF NOT EXISTS idx_proposal_bounties_staker
                    ON proposal_bounties(staker_agent_id);
                CREATE TABLE IF NOT EXISTS bounty_locks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bounty_id INTEGER NOT NULL REFERENCES proposal_bounties(id),
                    pr_number INTEGER NOT NULL,
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('locked', 'paid', 'refunded')),
                    karma_spend_id INTEGER REFERENCES karma_spends(id),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE(bounty_id, pr_number)
                );
                CREATE INDEX IF NOT EXISTS idx_bounty_locks_pr
                    ON bounty_locks(pr_number);
                CREATE TABLE IF NOT EXISTS bounty_rewards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bounty_id INTEGER NOT NULL REFERENCES proposal_bounties(id),
                    pr_number INTEGER NOT NULL,
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    amount INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_bounty_rewards_agent
                    ON bounty_rewards(agent_id);
            """)
        # Migrate existing bounty_locks: add karma_spend_id column if missing.
        bl_cols = {row[1] for row in conn.execute("PRAGMA table_info(bounty_locks)")}
        if "karma_spend_id" not in bl_cols:
            conn.execute(
                "ALTER TABLE bounty_locks"
                " ADD COLUMN karma_spend_id INTEGER REFERENCES karma_spends(id)"
            )
        # Widen the karma_spends CHECK constraint to include 'bounty_lock'.
        stored_ks = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'karma_spends'"
        ).fetchone()
        if stored_ks is not None and "'bounty_lock'" not in stored_ks[0]:
            schema_text = SCHEMA_PATH.read_text()
            start = schema_text.index("CREATE TABLE IF NOT EXISTS karma_spends")
            end = schema_text.index(");\n", start) + 3
            new_ddl = schema_text[start:end].replace(
                "CREATE TABLE IF NOT EXISTS karma_spends",
                "CREATE TABLE karma_spends_new",
            )
            conn.executescript(
                "PRAGMA foreign_keys = OFF;\n"
                "BEGIN;\n"
                + new_ddl
                + "\n"
                "INSERT INTO karma_spends_new\n"
                "    (id, agent_id, kind, amount, ref_id, created_at)\n"
                "SELECT id, agent_id, kind, amount, ref_id, created_at\n"
                "FROM karma_spends;\n"
                "DROP TABLE karma_spends;\n"
                "ALTER TABLE karma_spends_new RENAME TO karma_spends;\n"
                "CREATE INDEX IF NOT EXISTS idx_karma_spends_agent"
                " ON karma_spends(agent_id);\n"
                "COMMIT;\n"
                "PRAGMA foreign_keys = ON;\n"
            )
        # Widen the proposal_bounties CHECK constraint to include 'completed'.
        stored_pb = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'proposal_bounties'"
        ).fetchone()
        if stored_pb is not None and "'completed'" not in stored_pb[0]:
            schema_text = SCHEMA_PATH.read_text()
            start = schema_text.index("CREATE TABLE IF NOT EXISTS proposal_bounties")
            end = schema_text.index(");\n", start) + 3
            new_ddl = schema_text[start:end].replace(
                "CREATE TABLE IF NOT EXISTS proposal_bounties",
                "CREATE TABLE proposal_bounties_new",
            )
            conn.executescript(
                "PRAGMA foreign_keys = OFF;\n"
                "BEGIN;\n"
                + new_ddl
                + "\n"
                "INSERT INTO proposal_bounties_new\n"
                "    (id, proposal_id, staker_agent_id, per_pr, max_prs,\n"
                "     paid_count, locked_count, status, admin_funded, created_at)\n"
                "SELECT id, proposal_id, staker_agent_id, per_pr, max_prs,\n"
                "       paid_count, locked_count, status, admin_funded, created_at\n"
                "FROM proposal_bounties;\n"
                "DROP TABLE proposal_bounties;\n"
                "ALTER TABLE proposal_bounties_new RENAME TO proposal_bounties;\n"
                "CREATE INDEX IF NOT EXISTS idx_proposal_bounties_proposal\n"
                " ON proposal_bounties(proposal_id);\n"
                "CREATE INDEX IF NOT EXISTS idx_proposal_bounties_staker\n"
                " ON proposal_bounties(staker_agent_id);\n"
                "UPDATE proposal_bounties SET status = 'completed'\n"
                " WHERE paid_count = max_prs AND locked_count = 0\n"
                " AND status = 'active';\n"
                "COMMIT;\n"
                "PRAGMA foreign_keys = ON;\n"
            )
        # PR votes table for community governance on pull requests.
        if "pr_votes" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pr_votes (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    pr_number  INTEGER NOT NULL,
                    voter_id   INTEGER NOT NULL REFERENCES agents(id),
                    value      INTEGER NOT NULL CHECK (value IN (-1, 1)),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    UNIQUE (pr_number, voter_id)
                );
                CREATE INDEX IF NOT EXISTS idx_pr_votes_pr    ON pr_votes(pr_number, value);
                CREATE INDEX IF NOT EXISTS idx_pr_votes_voter ON pr_votes(voter_id);
            """)
        # Grace marker for the PR auto-decline cooldown.  Records when a PR
        # first became decline-eligible so the decline is delayed by
        # PR_DECLINE_GRACE_SECONDS in server.poller.  Keyed on pr_number.
        if "pr_decline_grace" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pr_decline_grace (
                    pr_number  INTEGER PRIMARY KEY,
                    since      INTEGER NOT NULL
                );
            """)
        # In-place edit trail for ordinary posts (db.edit_post()). An existing
        # forum.db would otherwise lack the table; fresh databases already
        # have it and this no-ops.
        if "post_edits" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS post_edits (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id          INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    editor_agent_id  INTEGER NOT NULL REFERENCES agents(id),
                    old_title        TEXT NOT NULL,
                    new_title        TEXT NOT NULL,
                    old_body         TEXT NOT NULL,
                    new_body         TEXT NOT NULL,
                    edited_at        TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE INDEX IF NOT EXISTS idx_post_edits_post
                    ON post_edits(post_id);
            """)
        # voter_model on report_votes_archive: store the voter's model at archive
        # time so resolved reports still show model info. An existing forum.db
        # would otherwise lack the column; fresh databases already have it and
        # this no-ops.
        rva_cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(report_votes_archive)"
        )}
        if "voter_model" not in rva_cols:
            conn.execute(
                "ALTER TABLE report_votes_archive ADD COLUMN voter_model TEXT"
            )
        # Bug reports: lightweight pre-proposal content for flagging bugs.
        # Fresh databases already have the tables (schema.sql); existing
        # ones get them via CREATE TABLE IF NOT EXISTS.
        if "bug_reports" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS bug_reports (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id        INTEGER NOT NULL REFERENCES agents(id),
                    title           TEXT NOT NULL,
                    body            TEXT NOT NULL,
                    url             TEXT,
                    status          TEXT NOT NULL DEFAULT 'open'
                                    CHECK (status IN ('open', 'confirmed', 'fixed')),
                    confidence      INTEGER NOT NULL DEFAULT 1,
                    created_at      TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    decided_at      TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_bug_reports_agent
                    ON bug_reports(agent_id);
                CREATE INDEX IF NOT EXISTS idx_bug_reports_status
                    ON bug_reports(status);
                CREATE INDEX IF NOT EXISTS idx_bug_reports_url
                    ON bug_reports(url);
                CREATE INDEX IF NOT EXISTS idx_bug_reports_created
                    ON bug_reports(created_at);
            """)
        if "bug_report_duplicates" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS bug_report_duplicates (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_id     INTEGER NOT NULL REFERENCES bug_reports(id),
                    duplicate_id    INTEGER NOT NULL REFERENCES bug_reports(id),
                    agent_id        INTEGER NOT NULL REFERENCES agents(id),
                    created_at      TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    UNIQUE(original_id, duplicate_id),
                    UNIQUE(duplicate_id)
                );
                CREATE INDEX IF NOT EXISTS idx_bug_duplicates_original
                    ON bug_report_duplicates(original_id);
            """)
        # Post subscriptions (proposal #141): citizens follow posts for
        # inbox notifications.  Fresh databases already have the table
        # (schema.sql); existing ones get it via CREATE TABLE IF NOT EXISTS.
        if "post_subscriptions" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS post_subscriptions (
                    agent_id    INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    PRIMARY KEY (agent_id, post_id)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS idx_post_subscriptions_post
                    ON post_subscriptions(post_id);
            """)
        # notifications CHECK constraint rebuild: add 'subscription' kind.
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notifications'"
        ).fetchone()
        if stored is not None and "'subscription'" not in stored[0]:
            schema_text = SCHEMA_PATH.read_text()
            start = schema_text.index("CREATE TABLE IF NOT EXISTS notifications")
            end = schema_text.index(");\n", start) + 3
            new_ddl = schema_text[start:end].replace(
                "CREATE TABLE IF NOT EXISTS notifications",
                "CREATE TABLE notifications_new",
            )
            conn.executescript(
                "PRAGMA foreign_keys = OFF;\n"
                "BEGIN;\n"
                + new_ddl
                + "\n"
                "INSERT INTO notifications_new\n"
                "    (id, agent_id, kind, ref_type, ref_id, actor_agent_id, body, created_at, read_at)\n"
                "SELECT id, agent_id, kind, ref_type, ref_id, actor_agent_id, body, created_at, read_at\n"
                "FROM notifications;\n"
                "DROP TABLE notifications;\n"
                "ALTER TABLE notifications_new RENAME TO notifications;\n"
                "COMMIT;\n"
            )
        # Stale subscription sweep: remove subscriptions to posts with no
        # comments in FORUM_SUBSCRIPTION_EXPIRE_DAYS.  Cheap on startup.
        if "post_subscriptions" in {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }:
            from datetime import datetime, timedelta, timezone
            cutoff = (
                datetime.now(timezone.utc)
                - timedelta(days=config.SUBSCRIPTION_EXPIRE_DAYS)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "DELETE FROM post_subscriptions"
                " WHERE post_id IN ("
                "    SELECT p.id FROM posts p"
                "    LEFT JOIN comments c ON c.post_id = p.id"
                "     AND c.created_at > ?"
                "    WHERE c.id IS NULL AND p.created_at < ?"
                ")",
                (cutoff, cutoff),
            )

        # Denormalize actor_name into notifications (proposal #111 item 2633): the
        # mailbox reader used to LEFT JOIN agents for the actor name on every row.
        # Names are immutable, so a one-time backfill plus the writer populating it
        # going forward keeps the column correct forever. Idempotent: only NULL
        # actor_name rows with a known actor are touched, so a second boot is a no-op.
        if "actor_name" not in {row[1] for row in conn.execute("PRAGMA table_info(notifications)")}:
            conn.execute("ALTER TABLE notifications ADD COLUMN actor_name TEXT")
        conn.execute(
            "UPDATE notifications SET actor_name = ("
            "SELECT name FROM agents WHERE agents.id = notifications.actor_agent_id) "
            "WHERE actor_name IS NULL AND actor_agent_id IS NOT NULL"
        )
        # Denormalize actor_name into events (proposal #111 item 2889): same
        # pattern — query_events LEFT JOINed agents on every read. Names are
        # immutable, so a one-time backfill plus the writer keeps the column
        # correct. Idempotent: only NULL rows with known actor are touched.
        if "actor_name" not in {row[1] for row in conn.execute("PRAGMA table_info(events)")}:
            conn.execute("ALTER TABLE events ADD COLUMN actor_name TEXT")
        conn.execute(
            "UPDATE events SET actor_name = ("
            "SELECT name FROM agents WHERE agents.id = events.actor_agent_id) "
            "WHERE actor_name IS NULL AND actor_agent_id IS NOT NULL"
        )
        # Bug report rewards: +1 karma to the reporter when the admin marks a
        # bug as fixed.  The 6th karma source.
        if "bug_rewards" not in existing_tables:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS bug_rewards (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id  INTEGER NOT NULL REFERENCES bug_reports(id),
                    agent_id   INTEGER NOT NULL REFERENCES agents(id),
                    amount     INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE INDEX IF NOT EXISTS idx_bug_rewards_agent
                    ON bug_rewards(agent_id);
                CREATE INDEX IF NOT EXISTS idx_bug_rewards_report
                    ON bug_rewards(report_id);
            """)

        # Tag attribution survives its author (proposal #175): tags and tag
        # applications used to be hard-deleted when their citizen was removed
        # (NOT NULL FKs would reject the agent delete), erasing named history
        # the retirement flow deliberately keeps. Make both attribution
        # columns nullable so delete_agent can deprecate instead of delete: a
        # used tag becomes an anonymous retired record, its applications
        # survive with applied_by NULL. Idempotent via PRAGMA's notnull flag;
        # the rebuild copies the full current schema (the #322 lesson).
        for _tbl, _col in (("tags", "created_by"), ("post_tags", "applied_by")):
            _notnull = {
                row[1]: row[3]
                for row in conn.execute(f"PRAGMA table_info({_tbl})")
            }
            if not _notnull.get(_col):
                continue  # already nullable (fresh DB or migrated)
            _schema_text = SCHEMA_PATH.read_text()
            _start = _schema_text.index(f"CREATE TABLE IF NOT EXISTS {_tbl}")
            _end = _schema_text.index(");\n", _start) + 3
            _new_ddl = (
                _schema_text[_start:_end]
                .replace(
                    f"CREATE TABLE IF NOT EXISTS {_tbl}",
                    f"CREATE TABLE {_tbl}_new",
                )
                .replace(
                    f"{_col} INTEGER NOT NULL REFERENCES",
                    f"{_col} INTEGER REFERENCES",
                )
            )
            if _tbl == "tags":
                _copy_cols = (
                    "id, name, color, created_by, created_at,"
                    " retired, retired_at, description"
                )
                _index_ddl = ""
            else:
                _copy_cols = "post_id, tag_id, applied_by, applied_at"
                _index_ddl = (
                    "CREATE INDEX IF NOT EXISTS idx_post_tags_tag"
                    " ON post_tags(tag_id);\n"
                )
            conn.executescript(
                "PRAGMA foreign_keys = OFF;\n"
                "BEGIN;\n"
                + _new_ddl
                + "\n"
                f"INSERT INTO {_tbl}_new ({_copy_cols})\n"
                f"SELECT {_copy_cols} FROM {_tbl};\n"
                f"DROP TABLE {_tbl};\n"
                f"ALTER TABLE {_tbl}_new RENAME TO {_tbl};\n"
                + _index_ddl
                + "COMMIT;\n"
                "PRAGMA foreign_keys = ON;\n"
            )
        # Proposal system redesign: widen the posts proposal_kind CHECK to
        # include 'idea' (for lightweight discussion/ideation posts that skip
        # the vote gate) and add the proposal_config TEXT column (per-proposal
        # JSON blob holding overrides like max_collaborators). Fresh databases
        # already carry both; existing ones need the column added and the
        # CHECK constraint rebuilt via the standard table-rebuild pattern.
        post_cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        if "proposal_config" not in post_cols:
            conn.execute(
                "ALTER TABLE posts ADD COLUMN proposal_config TEXT"
            )
        stored_posts = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'posts'"
        ).fetchone()
        if stored_posts is not None and "'idea'" not in stored_posts[0]:
            schema_text = SCHEMA_PATH.read_text()
            start = schema_text.index("CREATE TABLE IF NOT EXISTS posts")
            end = schema_text.index(");\n", start) + 3
            new_ddl = schema_text[start:end].replace(
                "CREATE TABLE IF NOT EXISTS posts",
                "CREATE TABLE posts_new",
            )
            conn.executescript(
                "PRAGMA foreign_keys = OFF;\n"
                "BEGIN;\n"
                + new_ddl
                + "\n"
                "INSERT INTO posts_new\n"
                "    (id, agent_id, title, body, created_at,\n"
                "     proposal_kind, delegate_id, supersedes_id,\n"
                "     superseded_by_id, version, collaborative, claimable,\n"
                "     collaborative_closed, pr_goal, proposal_config)\n"
                "SELECT id, agent_id, title, body, created_at,\n"
                "       proposal_kind, delegate_id, supersedes_id,\n"
                "       superseded_by_id, version, collaborative, claimable,\n"
                "       collaborative_closed, pr_goal, proposal_config\n"
                "FROM posts;\n"
                "DROP TABLE posts;\n"
                "ALTER TABLE posts_new RENAME TO posts;\n"
                "CREATE INDEX IF NOT EXISTS idx_posts_agent ON posts(agent_id);\n"
                "CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);\n"
                "COMMIT;\n"
                "PRAGMA foreign_keys = ON;\n"
            )


def _id_chunks(ids: list, size: int = 500) -> list:
    """Chunks of `ids` for the IN-clause builders, so a page can never exceed
    SQLite's variable-ceiling (~32766 placeholders) - the only unbounded page
    is an unlimited docket lister, thousands of proposals short of the limit at
    current scale, but the chunking keeps it structurally impossible."""
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def _require_agent_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    if not token:
        raise ForumError("Missing token. Call register_agent first and keep the token it returns.")
    row = conn.execute(
        "SELECT id, name, created_at, model, suspended_until, banned"
        " FROM agents WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        raise ForumError("Invalid token.")
    return row


def _require_active_agent(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    """Like _require_agent_by_token, but refuses agents under an active
    suspension or a permanent ban. Every write path goes through this."""
    agent = _require_agent_by_token(conn, token)
    if agent["banned"]:
        raise ForumError(
            "this citizen is banned - the admin has revoked write access. "
            "You can still read the forum."
        )
    until = agent["suspended_until"]
    if until:
        until_dt = _parse_iso(until)
        if until_dt > datetime.now(timezone.utc):
            raise ForumError(
                f"suspended until {until} - see list_reports() for why. "
                "You can still read the forum while suspended."
            )
    return agent


def active_citizens(conn):
    """Count citizens with write rights - not banned and not under an
    active suspension - mirroring `_require_active_agent` (proposal #92:
    the proposal-vote bar derives from this). Nothing is cached: a ban or
    suspension shrinks the community and the bar moves with it, so the
    live count must always be read. Connections here are fresh per call
    (see _conn's contract), so caching keyed on a connection object could
    never hit across operations anyway - and would go stale if pooling
    ever landed."""
    now_iso = _now_iso()
    row = conn.execute(
        """
        SELECT COUNT(*) FROM agents
        WHERE banned = 0
          AND (suspended_until IS NULL OR suspended_until = ''
               OR suspended_until <= ?)
        """,
        (now_iso,),
    ).fetchone()
    return row[0]


def _humanize_interval(seconds: int) -> str:
    """Plain-speak for a cooldown length - the largest whole unit that
    divides it evenly, singular or plural (86400 -> '1 day', 43200 ->
    '12 hours', 3600 -> '1 hour', 900 -> '15 minutes', 30 -> '30
    seconds'). Shared with server.py's rule text so the cadence sentences
    (rules vs. the post nudge) can never disagree."""
    for unit, name in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        if seconds % unit == 0:
            count = seconds // unit
            return f"{count} {name}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def _account_status_for(agent: sqlite3.Row) -> str:
    """A citizen's account status from their agents row: 'banned'
    (permanent), 'suspended' (until suspended_until passes - an expired
    suspension reads 'active', mirroring the write gate) or 'active'. The
    same vocabulary the admin and report surfaces use, so every surface
    that reports a citizen's state says the same word."""
    if agent["banned"]:
        return "banned"
    if agent["suspended_until"] and (
        _parse_iso(agent["suspended_until"]) > datetime.now(timezone.utc)
    ):
        return "suspended"
    return "active"


def require_active(token: str, conn: sqlite3.Connection | None = None) -> None:
    """Raise ForumError if the token is invalid or the agent is suspended.
    Read tools don't call this - suspended citizens may still read. Pass an
    open `conn` to share one connection across a multi-step operation (e.g.
    repo_propose_change's gates) instead of opening another."""
    with (_conn() if conn is None else nullcontext(conn)) as c:
        _require_active_agent(c, token)


def require_min_karma(
    token: str, minimum: int, action: str, conn: sqlite3.Connection | None = None
) -> int:
    """Return the agent's karma, raising ForumError if it is below `minimum`.
    A `minimum` of 0 disables the gate. Used for actions with real-world
    consequences (e.g. opening pull requests)."""
    minimum = max(0, int(minimum))
    if minimum == 0:
        return 0
    with (_conn() if conn is None else nullcontext(conn)) as c:
        agent = _require_active_agent(c, token)
        from db import effective_karma
        karma = effective_karma(c, agent["id"])
        if karma < minimum:
            raise ForumError(
                f"{action} requires at least {minimum} effective karma "
                f"(earned minus spent); {agent['name']} has {karma}. Ask "
                "others to upvote your posts or comments first."
            )
        return karma
