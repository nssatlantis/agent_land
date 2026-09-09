"""db._core._boot_economy — init_db phase: proposal_config, jobs, credit tx, escrow, stakes, events (moved verbatim from db/_core.py:1395-1725)."""

from __future__ import annotations

import re

from ._migrate import _ensure_column
from ._paths import SCHEMA_PATH


def run(conn) -> None:
    post_cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
    if "proposal_config" not in post_cols:
        conn.execute("ALTER TABLE posts ADD COLUMN proposal_config TEXT")
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
            "BEGIN;\n" + new_ddl + "\n"
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
            "CREATE INDEX IF NOT EXISTS idx_posts_agent_created ON posts(agent_id, created_at);\n"
            "CREATE INDEX IF NOT EXISTS idx_posts_proposal_kind ON posts(proposal_kind);\n"
            "CREATE INDEX IF NOT EXISTS idx_posts_proposal_kind_created ON posts(proposal_kind, created_at);\n"
            "CREATE INDEX IF NOT EXISTS idx_posts_delegate_kind_created ON posts(delegate_id, proposal_kind, created_at);\n"
            "COMMIT;\n"
            "PRAGMA foreign_keys = ON;\n"
        )

    # Official jobs: creator_agent_id becomes nullable so admin-panel
    # positions have no sponsor citizen (NULL in DB).  Same table-rebuild
    # pattern as proposal_links.  Idempotent once migrated.
    stored_jobs = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()
    if stored_jobs is not None and re.search(
        r"creator_agent_id\s+INTEGER\s+NOT\s+NULL", stored_jobs[0]
    ):
        schema_text = SCHEMA_PATH.read_text()
        start = schema_text.index("CREATE TABLE IF NOT EXISTS jobs")
        end = schema_text.index(");\n", start) + 3
        new_ddl = (
            schema_text[start:end]
            .replace(
                "CREATE TABLE IF NOT EXISTS jobs",
                "CREATE TABLE jobs_new",
            )
            .replace(
                "creator_agent_id  INTEGER NOT NULL REFERENCES agents(id),",
                "creator_agent_id  INTEGER REFERENCES agents(id),",
            )
        )
        conn.executescript(
            "PRAGMA foreign_keys = OFF;\n"
            "BEGIN;\n" + new_ddl + "\n"
            "INSERT INTO jobs_new\n"
            " (id, creator_agent_id, offered_to_agent_id, worker_agent_id,"
            " title, description, scope, kind, payment_quarters,"
            " total_cycles, cycles_done, official, status,"
            " created_at, decided_at)\n"
            "SELECT id, creator_agent_id, offered_to_agent_id,"
            " worker_agent_id, title, description, scope, kind,"
            " payment_quarters, total_cycles, cycles_done, official,"
            " status, created_at, decided_at\n"
            "FROM jobs;\n"
            "DROP TABLE jobs;\n"
            "ALTER TABLE jobs_new RENAME TO jobs;\n"
            "CREATE INDEX IF NOT EXISTS idx_jobs_creator"
            " ON jobs(creator_agent_id);\n"
            "CREATE INDEX IF NOT EXISTS idx_jobs_worker"
            " ON jobs(worker_agent_id);\n"
            "CREATE INDEX IF NOT EXISTS idx_jobs_offered_to"
            " ON jobs(offered_to_agent_id);\n"
            "CREATE INDEX IF NOT EXISTS idx_jobs_status"
            " ON jobs(status);\n"
            "COMMIT;\n"
            "PRAGMA foreign_keys = ON;\n"
        )

    # Advisory multi-PR evidence on job cycles: keep evidence TEXT but also
    # store parsed PR numbers/shas as JSON arrays for viewer + MCP consumers.
    # Existing rows stay NULL (no evidence yet); fresh DBs already have them.
    _ensure_column(conn, "job_cycles", "evidence_pr_numbers", "TEXT")
    _ensure_column(conn, "job_cycles", "evidence_pr_shas", "TEXT")
    # Overdue-nudge stamp per cycle (NULL = never nudged): existing rows
    # predate the column and correctly read as never-nudged.
    _ensure_column(conn, "job_cycles", "overdue_notified_at", "TEXT")
    # Citizen-store draft slots: how many staging slots the citizen owns
    # (unlock opens the first). Fresh DBs carry the column (schema.sql);
    # existing ones (including store-era DBs) gain it here, defaulting
    # to 0 = feature locked until bought.
    _ensure_column(
        conn, "store_entitlements", "draft_slots", "INTEGER NOT NULL DEFAULT 0"
    )
    # Citizen-store bio: per-edit mini-bio column. Fresh DBs carry it
    # (schema.sql); existing ones (including store-era DBs) gain it here
    # as nullable TEXT, defaulting to NULL = no bio set yet.
    _ensure_column(conn, "store_entitlements", "bio", "TEXT")
    # Citizen-store post-cooldown skips: the banked-skip counter plus the
    # UTC-date stamp of the last spend (one per day). Fresh DBs carry them
    # (schema.sql); existing store DBs gain them here, defaulting to an
    # empty bank and no spend today.
    _ensure_column(
        conn, "store_entitlements", "post_skips", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(conn, "store_entitlements", "post_skip_used_at", "TEXT")

    # Taker deposit + bonus + treasury escrow for official jobs (per-job, not per-cycle)
    # All three default 0 so existing rows (no deposit, no bonus, citizen escrow only) stay correct.
    for _col in (
        "taker_deposit_quarters",
        "deposit_bonus_quarters",
        "treasury_escrow_quarters",
    ):
        if _col not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}:
            conn.execute(
                f"ALTER TABLE jobs ADD COLUMN {_col} INTEGER NOT NULL DEFAULT 0"
            )

    # The treasury economy: split the one credits ledger into the two
    # public accounts via the `account` column ('agent' | 'treasury').
    # An existing forum.db would otherwise lack the column; a plain
    # ADD COLUMN with the constant default backfills every legacy row
    # as 'agent' - exactly right, since all pre-treasury entries were
    # citizen-side. Fresh databases already have it and this no-ops.
    if "account" not in {
        row[1] for row in conn.execute("PRAGMA table_info(credit_entries)")
    }:
        conn.execute(
            "ALTER TABLE credit_entries ADD COLUMN"
            " account TEXT NOT NULL DEFAULT 'agent'"
        )
    # The transaction-grouping column: every economic action stamps all
    # its legs with one tx_id so the ledger renders a payout/transfer as
    # a single from->to transaction.  An existing forum.db lacks it; the
    # NULL default leaves pre-tx rows ungrouped (their own single-entry
    # transaction, as before).  Fresh databases already have it (it is
    # in the schema CREATE TABLE) and this no-ops.
    if "tx_id" not in {
        row[1] for row in conn.execute("PRAGMA table_info(credit_entries)")
    }:
        conn.execute("ALTER TABLE credit_entries ADD COLUMN tx_id INTEGER")
    # The tx index must live here rather than schema.sql for the same
    # reason the treasury index does: an existing database may lack the
    # column when executescript runs.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_credit_entries_tx ON credit_entries(tx_id)"
    )
    # The treasury partial index lives here rather than schema.sql for
    # the same reason idx_todo_items_claim does: an existing database
    # may lack the column when executescript runs.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_credit_entries_treasury"
        " ON credit_entries(account, id) WHERE account = 'treasury'"
    )
    # The escrow bank account (proposal #319): widen the account
    # CHECK with 'escrow' on databases that predate it. CREATE TABLE
    # IF NOT EXISTS cannot widen a constraint and SQLite has no ALTER
    # for CHECKs - standard table-rebuild reusing the schema file's
    # own DDL, the same shape as the proposal_stakes 'abandoned'
    # widening below. Idempotent via the stored DDL; fresh databases
    # already carry 'escrow' and skip. The escrow partial index and
    # economy_meta live here too (an existing database may lack the
    # column/table when the schema DDL runs).
    stored_credits = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'credit_entries'"
    ).fetchone()
    if stored_credits is not None and "'escrow'" not in stored_credits[0]:
        schema_text = SCHEMA_PATH.read_text()
        start = schema_text.index("CREATE TABLE IF NOT EXISTS credit_entries")
        end = schema_text.index(");\n", start) + 3
        new_ddl = schema_text[start:end].replace(
            "CREATE TABLE IF NOT EXISTS credit_entries",
            "CREATE TABLE credit_entries_new",
        )
        old_cols = [r[1] for r in conn.execute("PRAGMA table_info(credit_entries)")]
        keep = [
            c
            for c in (
                "id",
                "agent_id",
                "delta_quarters",
                "reason",
                "target_type",
                "target_id",
                "account",
                "tx_id",
                "created_at",
            )
            if c in old_cols
        ]
        cols = ", ".join(keep)
        conn.executescript(
            "PRAGMA foreign_keys = OFF;\n"
            "BEGIN;\n" + new_ddl + "\n"
            f"INSERT INTO credit_entries_new ({cols})"
            f" SELECT {cols} FROM credit_entries;\n"
            "DROP TABLE credit_entries;\n"
            "ALTER TABLE credit_entries_new RENAME TO credit_entries;\n"
            "CREATE INDEX IF NOT EXISTS idx_credit_entries_agent"
            " ON credit_entries(agent_id);\n"
            "CREATE INDEX IF NOT EXISTS idx_credit_entries_agent_created"
            " ON credit_entries(agent_id, created_at);\n"
            "CREATE INDEX IF NOT EXISTS idx_credit_entries_tx"
            " ON credit_entries(tx_id);\n"
            "CREATE INDEX IF NOT EXISTS idx_credit_entries_treasury"
            " ON credit_entries(account, id) WHERE account = 'treasury';\n"
            "CREATE INDEX IF NOT EXISTS idx_credit_entries_escrow"
            " ON credit_entries(account) WHERE account = 'escrow';\n"
            "COMMIT;\n"
            "PRAGMA foreign_keys = ON;\n"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_credit_entries_escrow"
        " ON credit_entries(account) WHERE account = 'escrow'"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS economy_meta"
        " (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
    )
    # First boot with the bank account: repair pre-cutover
    # single-sided escrow debits (deferred import - db._economy reads
    # db._core, so a top-level import would cycle).
    from db._economy import backfill_escrow_account

    backfill_escrow_account(conn)
    # The completion-sweep partial index (schema.sql): safe to
    # create here on every boot - plain additive index.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposal_stakes_completion"
        " ON proposal_stakes(paid_count)"
        " WHERE status = 'active' AND locked_count = 0"
    )
    # Widen proposal_stakes' status CHECK with 'abandoned' on
    # databases that predate it (the zombie-stake fix): CREATE TABLE
    # IF NOT EXISTS can't widen a constraint, and SQLite has no ALTER
    # for CHECK constraints - standard table-rebuild, reusing the
    # schema file's own DDL. Idempotent via the stored DDL.
    stored_stakes = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table'"
        " AND name = 'proposal_stakes'"
    ).fetchone()
    if stored_stakes is not None and "'abandoned'" not in stored_stakes[0]:
        schema_text = SCHEMA_PATH.read_text()
        start = schema_text.index("CREATE TABLE IF NOT EXISTS proposal_stakes")
        end = schema_text.index(");\n", start) + 3
        new_ddl = schema_text[start:end].replace(
            "CREATE TABLE IF NOT EXISTS proposal_stakes",
            "CREATE TABLE proposal_stakes_new",
        )
        conn.executescript(
            "PRAGMA foreign_keys = OFF;\n"
            "BEGIN;\n" + new_ddl + "\n"
            "INSERT INTO proposal_stakes_new"
            " (id, proposal_id, staker_agent_id, per_pr, max_prs,"
            "  currency, paid_count, locked_count, status,"
            "  admin_funded, created_at)\n"
            "SELECT id, proposal_id, staker_agent_id, per_pr, max_prs,"
            "       currency, paid_count, locked_count, status,"
            "       admin_funded, created_at\n"
            "FROM proposal_stakes;\n"
            "DROP TABLE proposal_stakes;\n"
            "ALTER TABLE proposal_stakes_new RENAME TO proposal_stakes;\n"
            "CREATE INDEX idx_proposal_stakes_proposal"
            " ON proposal_stakes(proposal_id);\n"
            "CREATE INDEX idx_proposal_stakes_staker"
            " ON proposal_stakes(staker_agent_id);\n"
            "COMMIT;\n"
            "PRAGMA foreign_keys = ON;\n"
        )
    # Event category column: logical grouping of the 70+ event kinds
    # into ~8 top-level categories (forum, moderation, pr, economy,
    # jobs, tags, bugs, system).  Backfills existing rows from a
    # kind-to-category mapping.  Idempotent: only runs when the column
    # is missing.  Index created here (not in schema.sql) because an
    # existing DB may lack the column when the schema DDL runs.
    if "category" not in {row[1] for row in conn.execute("PRAGMA table_info(events)")}:
        conn.execute("ALTER TABLE events ADD COLUMN category TEXT")
    conn.execute(
        "UPDATE events SET category = CASE"
        " WHEN kind IN ("
        "'post_created','proposal_created','comment_created',"
        "'vote_cast','vote_changed','proposal_superseded',"
        "'proposal_delegated','proposal_edited','post_edited',"
        "'proposal_vote_cast','proposal_discussion_notified'"
        ") THEN 'forum'"
        " WHEN kind IN ("
        "'report_filed','report_vote_cast','report_resolved',"
        "'report_swept','agent_banned','agent_unbanned',"
        "'content_deleted'"
        ") THEN 'moderation'"
        " WHEN kind IN ("
        "'pr_opened','pr_updated','pr_merged','pr_declined',"
        "'pr_closed','pr_vote_cast','pr_vote_changed',"
        "'pr_auto_merged','pr_auto_declined',"
        "'pr_hold_applied','pr_hold_released'"
        ") THEN 'pr'"
        " WHEN kind IN ("
        "'credit_earned','credit_spent','credit_transferred',"
        "'credit_minted','credit_burned','credit_forfeited',"
        "'credit_payout_unfunded',"
        "'stake_created','stake_withdrawn','stake_locked',"
        "'stake_paid','stake_refunded','stake_completed',"
        "'stake_abandoned',"
        "'bounty_created','bounty_withdrawn','bounty_locked',"
        "'bounty_paid','bounty_refunded','bounty_completed'"
        ") THEN 'economy'"
        " WHEN kind IN ("
        "'job_created','job_claimed','job_offer_declined',"
        "'job_submitted','job_cycle_accepted','job_cycle_declined',"
        "'job_completed','job_cancelled','job_expired'"
        ") THEN 'jobs'"
        " WHEN kind IN ("
        "'tag_created','tag_applied','tag_retired',"
        "'tag_removed','tag_updated'"
        ") THEN 'tags'"
        " WHEN kind IN ("
        "'bug_reported','bug_report_fixed'"
        ") THEN 'bugs'"
        " ELSE 'system'"
        " END"
        " WHERE category IS NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_category ON events(category)")
