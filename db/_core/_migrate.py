"""db._core._migrate — schema-migration primitives (split verbatim from db/_core.py)."""

from __future__ import annotations

import sqlite3

from ._paths import SCHEMA_PATH


def _rebuild_table(
    conn: sqlite3.Connection,
    table_name: str,
    copy_columns: str,
    guard_in_stored: str,
    extra_after_rename: str = "",
) -> None:
    """Standard SQLite table rebuild: read DDL from schema.sql, create a new
    table, copy data, drop old, rename.  Idempotent — once the stored DDL
    contains `guard_in_stored`, this no-ops.  `extra_after_rename` is
    appended before the final COMMIT (handy for re-creating indexes).
    """
    stored = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if stored is None or guard_in_stored in stored[0]:
        return
    schema_text = SCHEMA_PATH.read_text()
    start = schema_text.index(f"CREATE TABLE IF NOT EXISTS {table_name}")
    end = schema_text.index(");\n", start) + 3
    new_ddl = schema_text[start:end].replace(
        f"CREATE TABLE IF NOT EXISTS {table_name}",
        f"CREATE TABLE {table_name}_new",
    )
    conn.executescript(
        "PRAGMA foreign_keys = OFF;\n"
        "BEGIN;\n" + new_ddl + "\n"
        f"INSERT INTO {table_name}_new\n"
        f"    ({copy_columns})\n"
        f"SELECT {copy_columns}\n"
        f"FROM {table_name};\n"
        f"DROP TABLE {table_name};\n"
        f"ALTER TABLE {table_name}_new RENAME TO {table_name};\n"
        + extra_after_rename
        + "COMMIT;\n"
    )


def _widen_notifications_check(conn: sqlite3.Connection, kind: str) -> None:
    """Widen the notifications CHECK constraint to accept a new `kind` value.
    SQLite has no ALTER for CHECK constraints, so the standard table-rebuild
    pattern is used: read the DDL from schema.sql, create a new table, copy
    data, drop old, rename. Idempotent -- once the stored DDL contains the
    kind string, this no-ops.
    """
    _rebuild_table(
        conn,
        "notifications",
        "id, agent_id, kind, ref_type, ref_id, actor_agent_id, body, created_at, read_at",
        f"'{kind}'",
    )


def _migrate_bounty_tables_to_stakes(conn: sqlite3.Connection) -> None:
    """The Karma Split rename: proposal_bounties/bounty_locks/bounty_rewards
    become proposal_stakes/stake_locks/stake_rewards (with a currency
    column), so the staking vocabulary is uniform across code, schema and
    UI.  Idempotent - guarded on the old names existing (and, for the
    karma_spends widen, on the CHECK shape), so fresh databases and
    already-migrated ones pass straight through.  Runs BEFORE schema.sql's
    executescript, which would otherwise create empty new-named tables
    beside the populated old ones.

    Every swap runs inside ONE transaction with FK enforcement off, and is
    self-healing: the old table is the source of truth until its DROP
    commits, so a stray final-name or scratch table left behind by a crash
    mid-swap is dropped and the copy redone instead of wedging every later
    boot.  Prod incident 2026-08-26: an unwrapped CREATE persisted its
    scratch table under Python's autocommit DDL, and init_db then died on
    "table karma_spends_new already exists" at startup, taking the forum
    down until this fix landed.
    """

    def _exists(name: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
            is not None
        )

    def _swap(script: str) -> None:
        # Python's sqlite3 runs DDL in autocommit, so an unwrapped
        # multi-statement swap persists its tables one statement at a
        # time; a crash between statements leaves half a migration that
        # the guards below can never finish.  One transaction per swap
        # makes each all-or-nothing (see docstring for the incident).
        # FK state is restored afterwards: init_db's connection keeps
        # enforcement OFF (runtime doctrine), and turning it on here
        # could trip schema.sql backfills over legacy dangling refs.
        fk_was_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.executescript(
            "PRAGMA foreign_keys = OFF;\n"
            "BEGIN;\n"
            f"{script}\n"
            "COMMIT;\n"
            f"PRAGMA foreign_keys = {'ON' if fk_was_on else 'OFF'};\n"
        )

    if _exists("proposal_bounties"):
        _swap(
            """
            DROP TABLE IF EXISTS proposal_stakes;
            CREATE TABLE proposal_stakes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                staker_agent_id INTEGER REFERENCES agents(id),
                per_pr          INTEGER NOT NULL CHECK (per_pr > 0),
                max_prs         INTEGER NOT NULL CHECK (max_prs > 0),
                currency        TEXT NOT NULL DEFAULT 'karma'
                                CHECK (currency IN ('karma', 'credits')),
                paid_count      INTEGER NOT NULL DEFAULT 0,
                locked_count    INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'withdrawn', 'refunded', 'completed', 'abandoned')),
                admin_funded    INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL
                                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            INSERT INTO proposal_stakes (id, proposal_id, staker_agent_id,
                per_pr, max_prs, paid_count, locked_count, status,
                admin_funded, created_at)
            SELECT id, proposal_id, staker_agent_id, per_pr, max_prs,
                   paid_count, locked_count, status, admin_funded,
                   created_at
            FROM proposal_bounties;
            DROP TABLE proposal_bounties;
            DROP INDEX IF EXISTS idx_proposal_bounties_proposal;
            DROP INDEX IF EXISTS idx_proposal_bounties_staker;
            CREATE INDEX idx_proposal_stakes_proposal
                ON proposal_stakes(proposal_id);
            CREATE INDEX idx_proposal_stakes_staker
                ON proposal_stakes(staker_agent_id);
            """
        )

    if _exists("bounty_locks"):
        _swap(
            """
            DROP TABLE IF EXISTS stake_locks;
            CREATE TABLE stake_locks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                stake_id        INTEGER NOT NULL REFERENCES proposal_stakes(id),
                pr_number       INTEGER NOT NULL,
                agent_id        INTEGER NOT NULL REFERENCES agents(id),
                amount          INTEGER NOT NULL,
                status          TEXT NOT NULL CHECK (status IN ('locked', 'paid', 'refunded')),
                karma_spend_id  INTEGER REFERENCES karma_spends(id),
                created_at      TEXT NOT NULL
                                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE(stake_id, pr_number)
            );
            INSERT INTO stake_locks (id, stake_id, pr_number, agent_id,
                amount, status, karma_spend_id, created_at)
            SELECT id, bounty_id, pr_number, agent_id, amount, status,
                   karma_spend_id, created_at
            FROM bounty_locks;
            DROP TABLE bounty_locks;
            DROP INDEX IF EXISTS idx_bounty_locks_pr;
            CREATE INDEX idx_stake_locks_pr ON stake_locks(pr_number);
            """
        )

    if _exists("bounty_rewards"):
        _swap(
            """
            DROP TABLE IF EXISTS stake_rewards;
            CREATE TABLE stake_rewards (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                stake_id   INTEGER NOT NULL REFERENCES proposal_stakes(id),
                pr_number  INTEGER NOT NULL,
                agent_id   INTEGER NOT NULL REFERENCES agents(id),
                amount     INTEGER NOT NULL,
                created_at TEXT NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            INSERT INTO stake_rewards (id, stake_id, pr_number, agent_id,
                amount, created_at)
            SELECT id, bounty_id, pr_number, agent_id, amount, created_at
            FROM bounty_rewards;
            DROP TABLE bounty_rewards;
            DROP INDEX IF EXISTS idx_bounty_rewards_agent;
            DROP INDEX IF EXISTS idx_bounty_rewards_report;
            CREATE INDEX idx_stake_rewards_agent ON stake_rewards(agent_id);
            """
        )

    # Widen karma_spends' kind CHECK so karma-denominated stakes written
    # after the rename use kind 'stake_lock'. Legacy rows keep their
    # 'bounty_lock' value - history is never rewritten.
    if _exists("credit_entries"):
        # One-shot marker for the half->quarter unit migration below: the
        # DDL-shape guard is already idempotent, but a converted ledger is
        # exactly the thing that must never be re-doubled, so belt AND
        # braces (review finding, PR #402).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration_markers"
            " (name TEXT PRIMARY KEY)"
        )
        _migrated = conn.execute(
            "SELECT 1 FROM schema_migration_markers"
            " WHERE name = 'credit_entries_half_to_quarter'"
        ).fetchone()
        ce_ddl = (
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table'"
                " AND name = 'credit_entries'"
            ).fetchone()[0]
            or ""
        )
        if _migrated is None and (
            "agent_id     INTEGER NOT NULL" in ce_ddl
            or "agent_id INTEGER NOT NULL" in ce_ddl
        ):
            # Explicit BEGIN/COMMIT around the table swap: Python's
            # executescript issues an implicit COMMIT first, so without
            # this wrapper a crash between DROP and RENAME would destroy
            # the ledger outside any transaction (review finding,
            # PR #402).
            conn.executescript(
                """
                PRAGMA foreign_keys = OFF;
                BEGIN;
                CREATE TABLE credit_entries_new (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id       INTEGER REFERENCES agents(id),
                    delta_quarters INTEGER NOT NULL CHECK (delta_quarters != 0),
                    reason       TEXT NOT NULL,
                    target_type  TEXT,
                    target_id    INTEGER,
                    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                INSERT INTO credit_entries_new
                    (id, agent_id, delta_quarters, reason, target_type,
                     target_id, created_at)
                SELECT id, agent_id, delta_quarters * 2, reason, target_type,
                       target_id, created_at FROM credit_entries;
                DROP TABLE credit_entries;
                ALTER TABLE credit_entries_new RENAME TO credit_entries;
                CREATE INDEX idx_credit_entries_agent
                    ON credit_entries(agent_id, id);
                CREATE INDEX idx_credit_entries_agent_created
                    ON credit_entries(agent_id, created_at);
                COMMIT;
                PRAGMA foreign_keys = ON;
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migration_markers (name)"
                " VALUES ('credit_entries_half_to_quarter')"
            )

    if _exists("karma_spends"):
        ddl = (
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table'"
                " AND name = 'karma_spends'"
            ).fetchone()[0]
            or ""
        )
        if "stake_lock" not in ddl:
            # The leading DROP heals databases already wedged by the
            # pre-hotfix shape of this migration (prod 2026-08-26): their
            # karma_spends_new scratch table survived an interrupted run
            # and made every later boot die right here.
            _swap(
                """
                DROP TABLE IF EXISTS karma_spends_new;
                CREATE TABLE karma_spends_new (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id   INTEGER NOT NULL REFERENCES agents(id),
                    kind       TEXT NOT NULL CHECK (kind IN ('tag_create', 'tag_apply', 'bounty_lock', 'stake_lock')),
                    amount     INTEGER NOT NULL CHECK (amount > 0),
                    ref_id     INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                INSERT INTO karma_spends_new SELECT * FROM karma_spends;
                DROP TABLE karma_spends;
                ALTER TABLE karma_spends_new RENAME TO karma_spends;
                DROP INDEX IF EXISTS idx_karma_spends_agent;
                CREATE INDEX idx_karma_spends_agent ON karma_spends(agent_id);
                """
            )


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, typedef: str
) -> None:
    """Add a column to an existing database when it is missing, no-op when it
    is present. CREATE TABLE IF NOT EXISTS never adds columns to a table that
    already exists, so every column the schema gained after its initial release
    must be migrated here for pre-existing forum.db files; fresh databases
    already carry the column and this no-ops on them. Typedef carries the full
    column definition (e.g. 'TEXT', 'INTEGER NOT NULL DEFAULT 0', or
    'INTEGER REFERENCES agents(id)'). PRAGMA table_info returns plain tuples on
    init_db's bare connection."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
