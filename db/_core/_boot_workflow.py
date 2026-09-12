"""db._core._boot_workflow — init_db phase: workflow_runs, links, sweeps, actor names, tags (moved verbatim from db/_core.py:1142-1388)."""

from __future__ import annotations

import config

from ._paths import SCHEMA_PATH


def run(conn, existing_tables) -> None:
    # workflow_runs lifecycle (part 2): the status CHECK gained
    # 'completed' (the CI-green auto-close), and the single start-race
    # index became two partial UNIQUE indexes — one open run per UNBOUND
    # proposal AND one open run per bound PR. CREATE TABLE IF NOT EXISTS
    # can't widen a CHECK on an existing table and SQLite has no ALTER for
    # CHECK constraints, so this is the standard table rebuild reusing the
    # schema file's own DDL (the notifications rebuilds above). The swap
    # drops every index on the old table — including the two partial
    # uniques the schema executescript just created against it — so the
    # full schema index set is recreated after the rename. Guarded on the
    # stored DDL; idempotent once migrated. Row ids survive (all columns
    # copied), so run history is stable across the upgrade.
    stored_workflows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'workflow_runs'"
    ).fetchone()
    if stored_workflows is not None and "'completed'" not in stored_workflows[0]:
        _schema_text = SCHEMA_PATH.read_text()
        _start = _schema_text.index("CREATE TABLE IF NOT EXISTS workflow_runs")
        _end = _schema_text.index(");\n", _start) + 3
        _new_ddl = _schema_text[_start:_end].replace(
            "CREATE TABLE IF NOT EXISTS workflow_runs",
            "CREATE TABLE workflow_runs_new",
        )
        conn.executescript(
            "PRAGMA foreign_keys = OFF;\n"
            "BEGIN;\n" + _new_ddl + "\n"
            "INSERT INTO workflow_runs_new"
            " (id, workflow_path, workflow_sha, proposal_id, pr_number,"
            " agent_id, status, created_at, decided_at, expires_at)\n"
            "SELECT id, workflow_path, workflow_sha, proposal_id, pr_number,"
            " agent_id, status, created_at, decided_at, expires_at\n"
            "FROM workflow_runs;\n"
            "DROP TABLE workflow_runs;\n"
            "ALTER TABLE workflow_runs_new RENAME TO workflow_runs;\n"
            "CREATE INDEX idx_workflow_runs_proposal"
            " ON workflow_runs(proposal_id);\n"
            "CREATE INDEX idx_workflow_runs_pr ON workflow_runs(pr_number);\n"
            "CREATE INDEX idx_workflow_runs_path_sha"
            " ON workflow_runs(workflow_path, workflow_sha);\n"
            "CREATE INDEX idx_workflow_runs_agent_status"
            " ON workflow_runs(agent_id, status);\n"
            "CREATE UNIQUE INDEX idx_workflow_runs_open_unbound"
            " ON workflow_runs(workflow_path, proposal_id)"
            " WHERE status = 'open' AND pr_number IS NULL;\n"
            "CREATE UNIQUE INDEX idx_workflow_runs_open_pr"
            " ON workflow_runs(workflow_path, pr_number)"
            " WHERE status = 'open' AND pr_number IS NOT NULL;\n"
            "CREATE UNIQUE INDEX idx_workflow_runs_open_personal"
            " ON workflow_runs(workflow_path, agent_id)"
            " WHERE status = 'open' AND proposal_id IS NULL"
            " AND pr_number IS NULL;\n"
            "CREATE INDEX idx_workflow_runs_path_proposal_status"
            " ON workflow_runs(workflow_path, proposal_id, status);\n"
            "COMMIT;\n"
            "PRAGMA foreign_keys = ON;\n"
        )
    # Per-agent workflow ownership widens the open-unbound UNIQUE index
    # from (workflow_path, proposal_id) to (workflow_path, proposal_id,
    # agent_id) so each citizen owns at most one open run of their own per
    # proposal (FORUM_WORKFLOW_PER_AGENT). The schema.sql executescript
    # runs BEFORE this migration with CREATE UNIQUE INDEX IF NOT EXISTS,
    # which is a no-op on an existing database that already has the index
    # in the old two-column shape - so an existing DB keeps the old index
    # here unless we drop and recreate it. Guarded on the stored index DDL:
    # a fresh DB (already the new shape) or one still on the old shape both
    # drop + recreate to the new columns, idempotently.
    try:
        _stored_unbound = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index'"
            " AND name = 'idx_workflow_runs_open_unbound'"
        ).fetchone()
        _old_unbound = _stored_unbound is not None and "agent_id" not in (
            _stored_unbound[0] or ""
        )
        if _old_unbound:
            conn.execute("DROP INDEX idx_workflow_runs_open_unbound")
        if _old_unbound or _stored_unbound is None:
            conn.executescript(
                "CREATE UNIQUE INDEX IF NOT EXISTS"
                " idx_workflow_runs_open_unbound"
                " ON workflow_runs(workflow_path, proposal_id, agent_id)"
                " WHERE status = 'open' AND pr_number IS NULL;"
            )
    except Exception:  # domain:degrade-silently - index enrichment only
        pass
    # proposal_links.opened_by_agent_id becomes anonymizable: a NOT
    # NULL owner would force deleting the link row itself when its
    # opener is deleted - taking the PR-to-proposal history with it.
    # Nullable + NULL-on-delete keeps the trail (same deprecate-
    # don't-delete policy as credit_entries). Rebuild guarded on the
    # stored DDL; idempotent once migrated. Note: the actor_name-
    # style denormalization does not exist here, so the docket shows
    # deleted openers as system-opened - acceptable for a ghost.
    stored_links = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'proposal_links'"
    ).fetchone()
    if (
        stored_links is not None
        and "opened_by_agent_id  INTEGER NOT NULL" in stored_links[0]
    ):
        schema_text = SCHEMA_PATH.read_text()
        start = schema_text.index("CREATE TABLE IF NOT EXISTS proposal_links")
        end = schema_text.index(");\n", start) + 3
        new_ddl = (
            schema_text[start:end]
            .replace(
                "CREATE TABLE IF NOT EXISTS proposal_links",
                "CREATE TABLE proposal_links_new",
            )
            .replace(
                "opened_by_agent_id  INTEGER NOT NULL REFERENCES agents(id),",
                "opened_by_agent_id  INTEGER REFERENCES agents(id),",
            )
        )
        conn.executescript(
            "PRAGMA foreign_keys = OFF;\n"
            "BEGIN;\n" + new_ddl + "\n"
            "INSERT INTO proposal_links_new"
            " (pr_number, post_id, opened_by_agent_id, created_at)\n"
            "SELECT pr_number, post_id, opened_by_agent_id, created_at\n"
            "FROM proposal_links;\n"
            "DROP TABLE proposal_links;\n"
            "ALTER TABLE proposal_links_new RENAME TO proposal_links;\n"
            "CREATE INDEX idx_proposal_links_post_pr"
            " ON proposal_links(post_id, pr_number);\n"
            "CREATE INDEX idx_proposal_links_opener"
            " ON proposal_links(opened_by_agent_id);\n"
            "COMMIT;\n"
            "PRAGMA foreign_keys = ON;\n"
        )
    # Stale subscription sweep: remove subscriptions to posts with no
    # comments in FORUM_SUBSCRIPTION_EXPIRE_DAYS.  Cheap on startup.
    if "post_subscriptions" in {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }:
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=config.SUBSCRIPTION_EXPIRE_DAYS)
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
    if "actor_name" not in {
        row[1] for row in conn.execute("PRAGMA table_info(notifications)")
    }:
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
    if "actor_name" not in {
        row[1] for row in conn.execute("PRAGMA table_info(events)")
    }:
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
            row[1]: row[3] for row in conn.execute(f"PRAGMA table_info({_tbl})")
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
                "CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag_id);\n"
            )
        conn.executescript(
            "PRAGMA foreign_keys = OFF;\n"
            "BEGIN;\n" + _new_ddl + "\n"
            f"INSERT INTO {_tbl}_new ({_copy_cols})\n"
            f"SELECT {_copy_cols} FROM {_tbl};\n"
            f"DROP TABLE {_tbl};\n"
            f"ALTER TABLE {_tbl}_new RENAME TO {_tbl};\n" + _index_ddl + "COMMIT;\n"
            "PRAGMA foreign_keys = ON;\n"
        )
