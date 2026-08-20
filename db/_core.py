"""db._core — DB infrastructure, ForumError, timestamps, connection, init_db, auth helpers."""

from __future__ import annotations

import sqlite3
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
        raise ForumError(f"cannot parse since timestamp {since!r}.")
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
    on those source-table upserts, not on any karma column."""
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
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    finally:
        conn.close()


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
            end = schema_text.index(";", start) + 1
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
            end = schema_text.index(";", start) + 1
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
        # Refresh the query planner's statistics (auto-ANALYZE) once at
        # database start so lookups like the karma aggregates in list_agents
        # keep using good plans as the DB grows. Deliberately NOT run on every
        # connection close (see the PRAGMA optimize note in deploy/README.md):
        # the connections here are short-lived per call, and optimize only
        # needs to run when the planner sees something worth analyzing.
        conn.execute("PRAGMA optimize")
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
                        CHECK (status IN ('active', 'withdrawn', 'refunded')),
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
                CREATE INDEX IF NOT EXISTS idx_pr_votes_pr ON pr_votes(pr_number);
                CREATE INDEX IF NOT EXISTS idx_pr_votes_voter ON pr_votes(voter_id);
            """)


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
