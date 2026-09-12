"""db._core._boot_collab — init_db phase: collaborative/claims/todo/bug_reports/subscription migrations (moved verbatim from db/_core.py:855-1141)."""

from __future__ import annotations

from ._migrate import _ensure_column, _rebuild_table, _widen_notifications_check


def run(conn) -> set:
    _ensure_column(conn, "posts", "collaborative", "INTEGER NOT NULL DEFAULT 0")
    existing_tables = {
        row[0]
        for row in conn.execute(
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
    # (schema.sql). An existing forum.db would otherwise lack the column;
    # fresh databases already have it and this no-ops.
    _ensure_column(conn, "tags", "description", "TEXT DEFAULT NULL")
    # Claimable proposals: the 'claimable' flag on posts and the
    # proposal_claims table. An existing forum.db would otherwise lack
    # the column and the table. Fresh databases already have them and
    # this no-ops.
    _ensure_column(conn, "posts", "claimable", "INTEGER NOT NULL DEFAULT 0")
    # Reuse existing_tables from above (no tables created between checks)
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
    _ensure_column(conn, "posts", "collaborative_closed", "TEXT")
    _ensure_column(conn, "posts", "pr_goal", "INTEGER")
    _ensure_column(conn, "posts", "todo_claim_mode", "INTEGER NOT NULL DEFAULT 0")
    # To-do item claiming (proposal #140): per-item ownership on
    # collaborative proposals' to-do lists. Existing databases lack the
    # columns; fresh ones already carry them (schema.sql) and no-op here.
    _ensure_column(
        conn, "todo_items", "claimed_by_agent_id", "INTEGER REFERENCES agents(id)"
    )
    _ensure_column(conn, "todo_items", "claimed_at", "TEXT")
    # Auto-check PR binding: a nullable pr_number on the item whose merge
    # ticks it done (db.bind_todo_item_to_pr). Existing databases lack it;
    # fresh ones carry it (schema.sql) and no-op here.
    _ensure_column(conn, "todo_items", "pr_number", "INTEGER")
    # Thread reopen notes: the note_comment_id pointer beside
    # verdict_comment_id (schema.sql). An existing forum.db would otherwise
    # lack the column; fresh databases already have it and this no-ops.
    _ensure_column(
        conn, "threads", "note_comment_id", "INTEGER REFERENCES comments(id)"
    )
    # Create the claim partial index (moved here from schema.sql because
    # an existing database may lack the column when executescript runs).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_todo_items_claim"
        " ON todo_items(claimed_by_agent_id)"
        " WHERE claimed_by_agent_id IS NOT NULL"
    )
    # One item per PR: global uniqueness for the nullable pr_number
    # binding (Option A). Partial unique index is the race-proof backstop
    # for the application guard in bind_todo_item_to_pr; WHERE pr_number
    # IS NOT NULL lets many NULLs coexist.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_todo_items_pr_number"
        " ON todo_items(pr_number) WHERE pr_number IS NOT NULL"
    )
    # Whole-list claiming on collaborative proposals (todo_claim_mode=1,
    # see claim_todo_list): the same per-item claim pattern, but the claim
    # rides the todo_lists row and covers the whole category. Existing
    # databases lack the columns; fresh ones carry them (schema.sql).
    list_cols = {row[1] for row in conn.execute("PRAGMA table_info(todo_lists)")}
    if "claimed_by_agent_id" not in list_cols:
        conn.execute(
            "ALTER TABLE todo_lists ADD COLUMN claimed_by_agent_id"
            " INTEGER REFERENCES agents(id)"
        )
    if "claimed_at" not in list_cols:
        conn.execute("ALTER TABLE todo_lists ADD COLUMN claimed_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_todo_lists_claim"
        " ON todo_lists(claimed_by_agent_id)"
        " WHERE claimed_by_agent_id IS NOT NULL"
    )
    # To-do edit trail: every to-do mutation is snapshotted as compact
    # JSON (after-side only; the before side derives from the previous
    # row) so a destructive wipe is recoverable.
    # Fresh databases already have the table (schema.sql); existing
    # ones get it via CREATE TABLE IF NOT EXISTS.
    if "todo_edits" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS todo_edits (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id          INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                editor_agent_id  INTEGER NOT NULL REFERENCES agents(id),
                old_lists        TEXT,
                new_lists        TEXT NOT NULL,
                edited_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_todo_edits_post ON todo_edits(post_id);
        """)
    else:
        # Polish: old_lists "" sentinel -> NULL saves per-row overhead
        # (TEXT 0 bytes vs 1). Existing DBs have TEXT NOT NULL (sentinel
        # ""), fresh ones already nullable. Rebuild once if still NOT NULL.
        try:
            _ti = conn.execute("PRAGMA table_info(todo_edits)").fetchall()
            _notnull = next((r[3] for r in _ti if r[1] == "old_lists"), 0)
        except Exception:  # domain: degrade-silently - pragma probe never blocks boot
            _notnull = 0
        if _notnull == 1:
            _fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            conn.executescript("""
                PRAGMA foreign_keys = OFF;
                BEGIN;
                CREATE TABLE todo_edits_new (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id          INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    editor_agent_id  INTEGER NOT NULL REFERENCES agents(id),
                    old_lists        TEXT,
                    new_lists        TEXT NOT NULL,
                    edited_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                INSERT INTO todo_edits_new (id, post_id, editor_agent_id, old_lists, new_lists, edited_at)
                SELECT id, post_id, editor_agent_id,
                       CASE WHEN old_lists = '' THEN NULL ELSE old_lists END,
                       new_lists, edited_at FROM todo_edits;
                DROP TABLE todo_edits;
                ALTER TABLE todo_edits_new RENAME TO todo_edits;
                CREATE INDEX idx_todo_edits_post ON todo_edits(post_id);
                COMMIT;
            """)
            try:
                conn.execute(f"PRAGMA foreign_keys = {'ON' if _fk else 'OFF'}")
            except Exception:  # domain: degrade-silently
                pass
    # PR votes table for community governance on pull requests. The
    # bar_at_cast column mirrors schema.sql (proposal #400) - keep this
    # fallback DDL in sync if the canonical one changes.
    if "pr_votes" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pr_votes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pr_number  INTEGER NOT NULL,
                voter_id   INTEGER NOT NULL REFERENCES agents(id),
                value      INTEGER NOT NULL CHECK (value IN (-1, 1)),
                bar_at_cast INTEGER,
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
    _ensure_column(conn, "report_votes_archive", "voter_model", "TEXT")
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
    # Bug resolution columns + widened status CHECK (quorum close):
    # existing databases gain resolution/resolution_note via ALTER and
    # the CHECK is rebuilt to admit 'closed' via the standard
    # table-rebuild pattern (mirrors the posts proposal_kind widening).
    _ensure_column(conn, "bug_reports", "resolution", "TEXT")
    _ensure_column(conn, "bug_reports", "resolution_note", "TEXT")
    stored_bugs = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'bug_reports'"
    ).fetchone()
    if stored_bugs is not None and "'closed'" not in stored_bugs[0]:
        _rebuild_table(
            conn,
            "bug_reports",
            "id, agent_id, title, body, url, status, confidence,"
            " created_at, decided_at, resolution, resolution_note",
            "'closed'",
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_agent"
            " ON bug_reports(agent_id);\n"
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_status"
            " ON bug_reports(status);\n"
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_url"
            " ON bug_reports(url);\n"
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_created"
            " ON bug_reports(created_at);\n",
        )
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
    _widen_notifications_check(conn, "subscription")
    # The mailbox gained an 'economy' notification kind.
    _widen_notifications_check(conn, "economy")
    # The mailbox gained a 'jobs' notification kind (CHARTER IX.6).
    _widen_notifications_check(conn, "jobs")
    # The mailbox gained a 'workflow' notification kind.
    _widen_notifications_check(conn, "workflow")
    # The mailbox gained a 'poll' notification kind (polls attached to
    # posts): the same CHECK-widen rebuild as the kinds above.
    _widen_notifications_check(conn, "poll")
    # The mailbox gained a 'skill' notification kind (ratees are pinged
    # when rated, proposal #422): same rebuild.
    _widen_notifications_check(conn, "skill")
    return existing_tables
