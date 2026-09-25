"""db._core._boot_collab — init_db phase: collaborative/claims/todo/bug_reports/subscription migrations (moved verbatim from db/_core.py:855-1141)."""

from __future__ import annotations

from ._migrate import (
    _ensure_column,
    _ensure_column_with_backfill,
    _rebuild_table,
    _widen_notifications_check,
)


def run(conn) -> set:
    _ensure_column(conn, "posts", "collaborative", "INTEGER NOT NULL DEFAULT 0")
    existing_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "transfer_tickets" in existing_tables:
        _ensure_column(
            conn,
            "transfer_tickets",
            "claim_id",
            "INTEGER REFERENCES workspace_claims(id)",
        )
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
    # Item progress notes (tick_todo_item(progress=...)): short sticky
    # resume text per item. Existing databases lack it; fresh ones carry
    # it (schema.sql) and no-op here.
    _ensure_column(conn, "todo_items", "progress", "TEXT NOT NULL DEFAULT ''")
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
    # Bug triage fields (overhaul #492): severity + repro + evidence +
    # solution/fix tracking. Fresh databases carry them via schema.sql;
    # existing ones gain them here (plain types, code validates the enum).
    _ensure_column(conn, "bug_reports", "severity", "TEXT")
    _ensure_column(conn, "bug_reports", "repro_steps", "TEXT")
    _ensure_column(conn, "bug_reports", "evidence", "TEXT")
    _ensure_column(conn, "bug_reports", "solution", "TEXT")
    _ensure_column(conn, "bug_reports", "solved_by", "INTEGER REFERENCES agents(id)")
    _ensure_column(conn, "bug_reports", "solved_at", "TEXT")
    _ensure_column(conn, "bug_reports", "fix_pr", "INTEGER")
    _ensure_column(conn, "bug_reports", "updated_at", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bug_reports_severity ON bug_reports(severity)"
    )
    # Bug claiming (proposal #498): who reserved the bug, when, and the bound
    # proposal. Fresh databases carry them via schema.sql; existing ones gain
    # them here. Expiry is computed (claimed_at + timeout), never stored.
    _ensure_column(conn, "bug_reports", "claimed_by", "INTEGER REFERENCES agents(id)")
    _ensure_column(conn, "bug_reports", "claimed_at", "TEXT")
    _ensure_column(conn, "bug_reports", "claimed_proposal_id", "INTEGER")
    # Bug bounties (proposal #509): the auto-posted job funding the fix.
    # Fresh databases carry it via schema.sql; existing ones gain it here.
    _ensure_column(
        conn,
        "bug_reports",
        "bounty_job_id",
        "INTEGER REFERENCES jobs(id) ON DELETE SET NULL",
    )
    # Server-error auto-reports (proposal #521): the signature a machine
    # filing stamps on its report (NULL = human-filed), plus the durable
    # per-signature hit counter. Fresh databases carry both via schema.sql;
    # existing ones gain them here.
    _ensure_column(conn, "bug_reports", "auto_signature", "TEXT")
    if "server_error_hits" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS server_error_hits (
                signature   TEXT PRIMARY KEY,
                path        TEXT NOT NULL,
                exc_type    TEXT NOT NULL,
                first_seen  TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                last_seen   TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                occurrences INTEGER NOT NULL DEFAULT 0,
                report_id   INTEGER REFERENCES bug_reports(id)
                    ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_server_error_hits_report
                ON server_error_hits(report_id);
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bug_reports_bounty_job"
        " ON bug_reports(bounty_job_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bug_reports_claimed_by"
        " ON bug_reports(claimed_by)"
    )
    # Bug-comment links: fresh databases carry the table via schema.sql;
    # existing ones get it via CREATE TABLE IF NOT EXISTS (no backfill -
    # comment #B cites accrue live from here on).
    if "bug_comment_links" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bug_comment_links (
                report_id  INTEGER NOT NULL REFERENCES bug_reports(id)
                    ON DELETE CASCADE,
                comment_id INTEGER NOT NULL REFERENCES comments(id)
                    ON DELETE CASCADE,
                post_id    INTEGER NOT NULL REFERENCES posts(id)
                    ON DELETE CASCADE,
                agent_id   INTEGER NOT NULL REFERENCES agents(id),
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (report_id, comment_id)
            );
            CREATE INDEX IF NOT EXISTS idx_bug_comment_links_comment
                ON bug_comment_links(comment_id);
            CREATE INDEX IF NOT EXISTS idx_bug_comment_links_report
                ON bug_comment_links(report_id);
        """)
    # Bug remarks (proposal #502): fresh databases carry the table via
    # schema.sql; existing ones get it via CREATE TABLE IF NOT EXISTS
    # (append-only, no backfill - remarks accrue live from here on). The
    # index rides outside the gate so an index-only loss heals on boot.
    if "bug_remarks" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bug_remarks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id  INTEGER NOT NULL REFERENCES bug_reports(id)
                    ON DELETE CASCADE,
                agent_id   INTEGER NOT NULL REFERENCES agents(id)
                    ON DELETE CASCADE,
                kind       TEXT,
                body       TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bug_remarks_report ON bug_remarks(report_id)"
    )
    # Review findings board (proposal #710): fresh databases carry the
    # tables via schema.sql; existing ones get them here.  Per-table
    # gates (not one shared check): an interrupted boot commits the
    # tables it reached, so a shared gate would leave a partial loss
    # unhealed forever.  The ledger is append-only with no backfill -
    # findings accrue live from here on.  Indexes ride outside the gates
    # so an index-only loss heals on boot.
    if "review_findings" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS review_findings (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id            INTEGER NOT NULL REFERENCES posts(id)
                    ON DELETE CASCADE,
                pr_number          INTEGER NOT NULL,
                finder_agent_id    INTEGER NOT NULL REFERENCES agents(id),
                category           TEXT NOT NULL CHECK (category IN
                    ('bug', 'improvement')),
                class              TEXT NOT NULL,
                check_text         TEXT NOT NULL,
                flip_path          TEXT NOT NULL,
                paths              TEXT NOT NULL DEFAULT '[]',
                auto_flip          INTEGER NOT NULL DEFAULT 0 CHECK
                    (auto_flip IN (0, 1)),
                fixed_by_agent_id  INTEGER REFERENCES agents(id),
                state              TEXT NOT NULL DEFAULT 'open' CHECK
                    (state IN ('open', 'resolved', 'disputed', 'stale')),
                verified_by_agent_id INTEGER REFERENCES agents(id),
                verified_head_sha TEXT,
                bounty_units       INTEGER NOT NULL DEFAULT 0,
                dispute_seq        INTEGER NOT NULL DEFAULT 0,
                created_at         TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS finding_corroborations (
                finding_id INTEGER NOT NULL REFERENCES review_findings(id)
                    ON DELETE CASCADE,
                agent_id   INTEGER NOT NULL REFERENCES agents(id)
                    ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (finding_id, agent_id)
            ) WITHOUT ROWID;
        """)
    if "finding_corroborations" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS finding_corroborations (
                finding_id INTEGER NOT NULL REFERENCES review_findings(id)
                    ON DELETE CASCADE,
                agent_id   INTEGER NOT NULL REFERENCES agents(id)
                    ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (finding_id, agent_id)
            ) WITHOUT ROWID;
        """)
    if "finding_notes" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS finding_notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                finding_id INTEGER NOT NULL REFERENCES review_findings(id)
                    ON DELETE CASCADE,
                agent_id   INTEGER NOT NULL REFERENCES agents(id)
                    ON DELETE CASCADE,
                body       TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_findings_post"
        " ON review_findings(post_id, state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_findings_pr"
        " ON review_findings(pr_number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_findings_finder"
        " ON review_findings(finder_agent_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_finding_notes_finding"
        " ON finding_notes(finding_id)"
    )
    # Finding fix-fund tables (proposal #710, phase 4): same per-table
    # gates - fresh databases carry them via schema.sql, existing ones
    # get them here, no backfill (bounties accrue live from here on).
    # dispute_seq rides _ensure_column on pre-phase-4 boards (existing
    # rows start at seq 0, and no verifications predate the column, so
    # the quorum reads stay exact).
    _ensure_column(conn, "review_findings", "dispute_seq", "INTEGER NOT NULL DEFAULT 0")
    if "finding_verifications" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS finding_verifications (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                finding_id        INTEGER NOT NULL REFERENCES review_findings(id)
                    ON DELETE CASCADE,
                verifier_agent_id INTEGER NOT NULL REFERENCES agents(id)
                    ON DELETE CASCADE,
                verified_head_sha TEXT NOT NULL,
                dispute_seq       INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (
                    finding_id, verifier_agent_id, verified_head_sha, dispute_seq
                )
            );
        """)
    if "finding_bounty_funds" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS finding_bounty_funds (
                finding_id      INTEGER NOT NULL REFERENCES review_findings(id)
                    ON DELETE CASCADE,
                funder_agent_id INTEGER NOT NULL REFERENCES agents(id)
                    ON DELETE CASCADE,
                units           INTEGER NOT NULL CHECK (units >= 0),
                created_at      TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (finding_id, funder_agent_id)
            );
        """)
    if "finding_payouts" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS finding_payouts (
                finding_id       INTEGER PRIMARY KEY REFERENCES review_findings(id)
                    ON DELETE CASCADE,
                payee_agent_id   INTEGER REFERENCES agents(id)
                    ON DELETE SET NULL,
                units            INTEGER NOT NULL CHECK (units > 0),
                created_at        TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_finding_verifications_finding"
        " ON finding_verifications(finding_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_finding_bounty_funds_finding"
        " ON finding_bounty_funds(finding_id)"
    )
    # Public-branch flags (proposal #710, phase 3): fresh databases carry
    # the table via schema.sql; existing ones get it here.  No backfill -
    # an absent row means a closed branch.
    if "pr_public_branches" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pr_public_branches (
                pr_number  INTEGER PRIMARY KEY,
                enabled    INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
        """)
    stored_bugs = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'bug_reports'"
    ).fetchone()
    if stored_bugs is not None and "'closed'" not in stored_bugs[0]:
        _rebuild_table(
            conn,
            "bug_reports",
            "id, agent_id, title, body, url, status, confidence,"
            " created_at, decided_at, resolution, resolution_note,"
            " bounty_job_id",
            "'closed'",
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_agent"
            " ON bug_reports(agent_id);\n"
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_status"
            " ON bug_reports(status);\n"
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_url"
            " ON bug_reports(url);\n"
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_created"
            " ON bug_reports(created_at);\n"
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_severity"
            " ON bug_reports(severity);\n"
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_claimed_by"
            " ON bug_reports(claimed_by);\n"
            "CREATE INDEX IF NOT EXISTS idx_bug_reports_bounty_job"
            " ON bug_reports(bounty_job_id);\n",
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
    # Design subscriptions (proposal #652): citizens follow designs for
    # inbox notifications.  Fresh databases already have the table
    # (schema.sql); existing ones get it via CREATE TABLE IF NOT EXISTS.
    if "design_subscriptions" not in existing_tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS design_subscriptions (
                agent_id    INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                design_id   INTEGER NOT NULL REFERENCES designs(id) ON DELETE CASCADE,
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (agent_id, design_id)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS idx_design_subscriptions_design
                ON design_subscriptions(design_id);
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
    # Poll max_choices (proposal #479): multi-answer ballots. Existing
    # databases lack the column (fresh ones carry it via schema.sql).
    _ensure_column(conn, "polls", "max_choices", "INTEGER NOT NULL DEFAULT 1")
    # poll_votes widens from one row per voter to one row per choice:
    # UNIQUE(poll_id, voter_id) -> UNIQUE(poll_id, voter_id, option_id).
    # Idempotent - no-ops once the stored DDL carries the widened key.
    # The rebuild drops the table's indexes with it, so both canonical
    # indexes ride extra_after_rename (schema.sql's executescript runs
    # BEFORE this phase and cannot recreate them).
    _rebuild_table(
        conn,
        "poll_votes",
        "id, poll_id, option_id, voter_id, created_at",
        "UNIQUE (poll_id, voter_id, option_id)",
        extra_after_rename=(
            "CREATE INDEX IF NOT EXISTS idx_poll_votes_poll"
            " ON poll_votes(poll_id);\n"
            "CREATE INDEX IF NOT EXISTS idx_poll_votes_poll_option"
            " ON poll_votes(poll_id, option_id);\n"
        ),
    )
    # The mailbox gained a 'skill' notification kind (ratees are pinged
    # when rated, proposal #422): same rebuild.
    _widen_notifications_check(conn, "skill")
    # The mailbox gained a 'guild' notification kind (guild invites, joins,
    # succession, co-signs, proposal #525): same rebuild.
    _widen_notifications_check(conn, "guild")
    # The mailbox gained a 'design' notification kind (proposal #652).
    _widen_notifications_check(conn, "design")
    # designs: explicit system-owned marker (proposal #713 / bug #B103,
    # review on PR #1452). owner_admin_id IS NULL historically meant
    # "panel-created", but the owner FK is ON DELETE SET NULL: a later
    # owner hard-delete forges the same NULL, and the panel marker would
    # then grant authority gained through deletion. ALTER, one-shot
    # backfill (pre-cutover owner_admin_id IS NULL rows) and the
    # completion sentinel run through _ensure_column_with_backfill: one
    # explicit transaction, marker-absent self-heal on later boots
    # (while the marker is absent no orphan can exist - a crashed
    # init_db never serves traffic, the same argument as
    # _backfill_unit_cutover), and once it is set a post-cutover orphan
    # never inherits the marker. Fresh databases carry the column via
    # schema.sql: their ALTER/backfill branch is skipped and the marker
    # records on first boot over zero rows.
    _ensure_column_with_backfill(
        conn,
        "designs",
        "system_owned",
        "INTEGER NOT NULL DEFAULT 0",
        "UPDATE designs SET system_owned = 1 WHERE owner_admin_id IS NULL",
    )
    _guild_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "guilds" in _guild_tables:
        _ensure_column(conn, "guilds", "emptied_at", "TEXT")
        # Mission postdates the PR-1 table shape: ensure before any copy
        # that names it, and backfill (the new DDL is NOT NULL), completion
        # sentinel - ALTER + backfill in one explicit transaction so this
        # pair cannot re-wedge either (review, PR #1452).
        _ensure_column_with_backfill(
            conn,
            "guilds",
            "mission",
            "TEXT NOT NULL DEFAULT ''",
            "UPDATE guilds SET mission = '' WHERE mission IS NULL",
        )
    if "guild_job_links" in _guild_tables:
        _ensure_column(conn, "guild_job_links", "grace_until", "TEXT")
    # Citizen deletion (proposal #525, PR-14, item 5069) NULLs attribution
    # on survivor guild rows: four NOT NULL agent legs relax. Guarded
    # rebuilds: the guard substrings must match schema.sql VERBATIM
    # (column-aligned spacing) - a drift silently rebuilds every boot.
    # Old tables rebuild once, new ones no-op. Indexes ride
    # extra_after_rename (rebuilds drop them).
    if "guilds" in _guild_tables:
        _rebuild_table(
            conn,
            "guilds",
            "id, name, founder_agent_id, status, spending_suspended,"
            " suspended_at, suspended_by, suspend_reason, disbanded_at,"
            " upkeep_arrears_units, last_upkeep_week, enrollment,"
            " mission, created_at, emptied_at",
            "founder_agent_id    INTEGER REFERENCES agents(id)",
            extra_after_rename=(
                "CREATE INDEX IF NOT EXISTS idx_guilds_founder"
                " ON guilds(founder_agent_id);\n"
                "CREATE INDEX IF NOT EXISTS idx_guilds_status"
                " ON guilds(status);\n"
            ),
        )
    if "guild_subsidies" in _guild_tables:
        _rebuild_table(
            conn,
            "guild_subsidies",
            "id, guild_id, amount_units, tier, payback, status,"
            " idea_post_id, requested_by, decided_by, created_at, decided_at",
            "requested_by      INTEGER REFERENCES agents(id)",
            extra_after_rename=(
                "CREATE INDEX IF NOT EXISTS idx_guild_subsidies_guild"
                " ON guild_subsidies(guild_id);\n"
            ),
        )
    if "guild_grant_links" in _guild_tables:
        _rebuild_table(
            conn,
            "guild_grant_links",
            "id, guild_id, idea_post_id, post_id, project_id, designated_by,"
            " designated_at, promoted_at, eligible_count, eligible_agent_ids,"
            " decay_pct, t1_tranche_id, t2_tranche_id, status, created_at",
            "designated_by      INTEGER REFERENCES agents(id)",
            extra_after_rename=(
                "CREATE INDEX IF NOT EXISTS idx_guild_grant_links_guild"
                " ON guild_grant_links(guild_id);\n"
                "CREATE INDEX IF NOT EXISTS idx_guild_grant_links_idea"
                " ON guild_grant_links(idea_post_id);\n"
            ),
        )
    if "guild_match_windows" in _guild_tables:
        _rebuild_table(
            conn,
            "guild_match_windows",
            "id, guild_id, mode, pct, days, cap_units, amount_units,"
            " status, opened_by, ends_at, created_at, settled_at",
            "opened_by        INTEGER REFERENCES agents(id)",
            extra_after_rename=(
                "CREATE INDEX IF NOT EXISTS idx_guild_match_windows_guild"
                " ON guild_match_windows(guild_id);\n"
            ),
        )
    if "guild_leave_log" in _guild_tables:
        _rebuild_table(
            conn,
            "guild_leave_log",
            "guild_id, agent_id, left_at",
            "agent_id INTEGER REFERENCES agents(id),",
            extra_after_rename=(
                "CREATE INDEX IF NOT EXISTS idx_guild_leave_log_agent"
                " ON guild_leave_log(agent_id);\n"
            ),
        )
    return existing_tables
