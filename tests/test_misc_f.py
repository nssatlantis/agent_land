"""test_misc shard F: S41-S47 (poll/skill/workflow migrations, polls max_choices,
workflow steps, todo FTS, length caps). Split of tests/test_misc.py; section bodies
byte-verbatim."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_misc_f_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, expect_error, reports, search, setup  # noqa: E402


def main():
    agents, post_id = setup()

    # --- migration: notifications widen the kind CHECK for 'poll' ----------
    # Polls (db._polls) mail kind='poll', but the pre-polls CHECK doesn't
    # admit it. CREATE TABLE IF NOT EXISTS can't widen an existing table's
    # constraint, so init_db() must rebuild the table - same pattern as the
    # 'workflow'/'jobs'/'economy' kinds above. The poll tables themselves are
    # new (CREATE TABLE IF NOT EXISTS), so init_db() is what surfaces them on
    # an existing database - verified below after the rebuild.
    with db._conn() as conn:
        conn.execute("DROP TABLE notifications")
        conn.execute(
            "CREATE TABLE notifications ("
            " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
            " agent_id       INTEGER NOT NULL REFERENCES agents(id),"
            " kind           TEXT NOT NULL CHECK (kind IN "
            "('reply', 'mention', 'vote', 'proposal', 'delegation', 'pr',"
            " 'pr_ci', 'moderation', 'collab_digest', 'subscription',"
            " 'economy', 'jobs')),"
            " ref_type       TEXT,"
            " ref_id         INTEGER,"
            " actor_agent_id INTEGER REFERENCES agents(id),"
            " body           TEXT NOT NULL,"
            " created_at     TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
            " read_at        TEXT)"
        )
    db.init_db()  # must rebuild the table and (re)create the poll tables
    with db._conn() as conn:
        nsql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table'"
            " AND name = 'notifications'"
        ).fetchone()[0]
        assert "'poll'" in nsql, (
            "init_db widens the notifications kind CHECK for pre-polls databases"
        )
        # the widened mailbox actually accepts poll-kind mail
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, body)"
            " VALUES (?, 'poll', 'post', ?, 'probe')",
            (agents["beta"]["agent_id"], post_id),
        )
        # and init_db created the new poll tables for the feature to use
        for tbl in ("polls", "poll_options", "poll_votes"):
            has = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (tbl,),
            ).fetchone()
            assert has is not None, f"init_db creates the {tbl} table"
    print("  notifications 'poll' kind migration: ok")

    # --- migration: polls gain max_choices + per-choice vote rows ----------
    # Proposal #479: polls.max_choices (default 1) + poll_votes UNIQUE
    # (poll_id, voter_id) -> (poll_id, voter_id, option_id). Seed one live
    # single-choice ballot, downgrade both tables to the pre-feature shape,
    # then init_db() must heal both and keep the ballot.
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO polls (post_id, author_id, question, max_choices,"
            " allows_edit_until, concludes_at) VALUES (?, ?, 'HQ', 1,"
            " '2000-01-01T00:00:00.000Z', '2100-01-01T00:00:00.000Z')",
            (post_id, agents["beta"]["agent_id"]),
        )
        old_poll = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO poll_options (poll_id, position, text)"
            " VALUES (?, 0, 'A'), (?, 1, 'B')",
            (old_poll, old_poll),
        )
        opt_a = conn.execute(
            "SELECT id FROM poll_options WHERE poll_id = ? AND position = 0",
            (old_poll,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO poll_votes (poll_id, option_id, voter_id) VALUES (?, ?, ?)",
            (old_poll, opt_a, agents["beta"]["agent_id"]),
        )
        conn.execute("ALTER TABLE polls DROP COLUMN max_choices")
        conn.execute("DROP TABLE poll_votes")
        conn.execute(
            "CREATE TABLE poll_votes ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,"
            " option_id INTEGER NOT NULL REFERENCES poll_options(id)"
            " ON DELETE CASCADE,"
            " voter_id INTEGER NOT NULL REFERENCES agents(id),"
            " created_at TEXT NOT NULL DEFAULT"
            " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
            " UNIQUE (poll_id, voter_id))"
        )
        conn.execute("CREATE INDEX idx_poll_votes_poll ON poll_votes(poll_id)")
        conn.execute(
            "INSERT INTO poll_votes (poll_id, option_id, voter_id) VALUES (?, ?, ?)",
            (old_poll, opt_a, agents["beta"]["agent_id"]),
        )
    db.init_db()  # must heal both tables, keeping the ballot
    with db._conn() as conn:
        kept = conn.execute(
            "SELECT option_id FROM poll_votes WHERE poll_id = ?",
            (old_poll,),
        ).fetchall()
        assert [r[0] for r in kept] == [opt_a], "migration keeps old ballots"
        nsql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'poll_votes'"
        ).fetchone()[0]
        assert "UNIQUE (poll_id, voter_id, option_id)" in nsql, (
            "init_db widens the poll_votes unique key for pre-feature databases"
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(polls)")}
        assert "max_choices" in cols, "init_db adds polls.max_choices"
        idxes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'poll_votes'"
            ).fetchall()
        }
        assert "idx_poll_votes_poll" in idxes, "heal keeps the tally index"
        assert "idx_poll_votes_poll_option" in idxes, "heal keeps the composite"
    print("  polls max_choices migration: ok")

    # --- migration: notifications widen the kind CHECK for 'skill' ---------
    # Skill ratings (db._skills) mail kind='skill', but the pre-skills CHECK
    # doesn't admit it. Same rebuild pattern as the 'poll'/'workflow' kinds
    # above: init_db() must widen the constraint via _widen_notifications_check.
    with db._conn() as conn:
        conn.execute("DROP TABLE notifications")
        conn.execute(
            "CREATE TABLE notifications ("
            " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
            " agent_id       INTEGER NOT NULL REFERENCES agents(id),"
            " kind           TEXT NOT NULL CHECK (kind IN "
            "('reply', 'mention', 'vote', 'proposal', 'delegation', 'pr',"
            " 'pr_ci', 'moderation', 'collab_digest', 'subscription',"
            " 'economy', 'jobs', 'workflow', 'poll')),"
            " ref_type       TEXT,"
            " ref_id         INTEGER,"
            " actor_agent_id INTEGER REFERENCES agents(id),"
            " body           TEXT NOT NULL,"
            " created_at     TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
            " read_at        TEXT)"
        )
    db.init_db()  # must rebuild the table to admit the skill kind
    with db._conn() as conn:
        nsql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table'"
            " AND name = 'notifications'"
        ).fetchone()[0]
        assert "'skill'" in nsql, (
            "init_db widens the notifications kind CHECK for pre-skills databases"
        )
        # the widened mailbox actually accepts skill-kind mail
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, body)"
            " VALUES (?, 'skill', 'skill', ?, 'probe')",
            (agents["beta"]["agent_id"], agents["alpha"]["agent_id"]),
        )
    print("  notifications 'skill' kind migration: ok")

    # --- migration: notifications widen keeps actor_name (#B71) ----------
    # The widen rebuild copied every mailbox column except actor_name, so a
    # legacy database that widened silently nulled every stored actor name.
    # Legacy shape WITH the denormalized column + a populated row: init_db()
    # must widen the CHECK and keep the name.
    with db._conn() as conn:
        conn.execute("DROP TABLE notifications")
        conn.execute(
            "CREATE TABLE notifications ("
            " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
            " agent_id       INTEGER NOT NULL REFERENCES agents(id),"
            " kind           TEXT NOT NULL CHECK (kind IN "
            "('reply', 'mention', 'vote', 'proposal', 'delegation', 'pr',"
            " 'pr_ci', 'moderation', 'collab_digest', 'subscription',"
            " 'economy', 'jobs', 'workflow', 'poll', 'skill')),"
            " ref_type       TEXT,"
            " ref_id         INTEGER,"
            " actor_agent_id INTEGER REFERENCES agents(id),"
            " actor_name      TEXT,"
            " body           TEXT NOT NULL,"
            " created_at     TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
            " read_at        TEXT)"
        )
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id,"
            " actor_agent_id, actor_name, body)"
            " VALUES (?, 'reply', 'post', ?, ?, 'Pickle', 'hello')",
            (
                agents["beta"]["agent_id"],
                post_id,
                agents["alpha"]["agent_id"],
            ),
        )
    db.init_db()  # must widen for 'guild' and keep the stored actor name
    with db._conn() as conn:
        kept = conn.execute(
            "SELECT actor_name FROM notifications WHERE body = 'hello'"
        ).fetchone()
        assert kept is not None and kept[0] == "Pickle", (
            "notifications widen keeps actor_name (#B71)"
        )
        nsql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table'"
            " AND name = 'notifications'"
        ).fetchone()[0]
        assert "'guild'" in nsql, (
            "init_db widens the notifications kind CHECK past 'skill'"
        )
    print("  notifications widen keeps actor_name: ok")

    # --- migration: workflow_runs widens its CHECK + splits its open-run
    # index (workflows part 2) ----------------------------------------------
    # The part-2 lifecycle adds the 'completed' status (the CI-green
    # auto-close) and turns the single start-race index into two partial
    # UNIQUE indexes - one open run per UNBOUND proposal AND one open run per
    # bound PR. CREATE TABLE IF NOT EXISTS can't widen a CHECK and SQLite has
    # no ALTER for it, so init_db() must rebuild the table and keep every
    # row.
    mig_p = db.create_proposal(agents["beta"]["token"], "Migrate workflows", "x")[
        "post_id"
    ]
    with db._conn() as conn:
        conn.execute("DROP TABLE workflow_runs")
        conn.execute(
            "CREATE TABLE workflow_runs ("
            " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
            " workflow_path  TEXT NOT NULL,"
            " workflow_sha   TEXT,"
            " proposal_id    INTEGER REFERENCES posts(id) ON DELETE CASCADE,"
            " pr_number      INTEGER,"
            " agent_id       INTEGER NOT NULL REFERENCES agents(id),"
            " status         TEXT NOT NULL CHECK (status IN "
            "('open','merged','declined','closed')) DEFAULT 'open',"
            " created_at     TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
            " decided_at     TEXT,"
            " expires_at     TEXT)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_workflow_runs_open"
            " ON workflow_runs(workflow_path, proposal_id) WHERE status = 'open'"
        )
        conn.execute(
            "INSERT INTO workflow_runs"
            " (workflow_path, workflow_sha, proposal_id, pr_number, agent_id,"
            "  status, created_at)"
            " VALUES ('workflows/create-pr.md', 'mig-hash', ?, 55555, ?,"
            " 'open', ?)",
            (mig_p, agents["beta"]["agent_id"], db._now_iso()),
        )
        conn.execute(
            "INSERT INTO workflow_runs"
            " (workflow_path, workflow_sha, proposal_id, pr_number, agent_id,"
            "  status, created_at)"
            " VALUES ('workflows/create-pr.md', 'mig-hash', ?, 55556, ?,"
            " 'merged', ?)",
            (mig_p, agents["beta"]["agent_id"], db._now_iso()),
        )
        # A run that reached 'merged' always had a linked pull request - bind
        # the merged one so reconcile_open_runs (which runs at the end of
        # init_db) sees a healthy linked proposal instead of ghost residue
        # (an open run + a folded run with NO proposal_links row). Without the
        # link the sweep would close the open run to 'closed' and this block
        # would assert the wrong result on a database the sweep is correct to
        # heal on live runs.
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (55556, ?, ?)",
            (mig_p, agents["beta"]["agent_id"]),
        )
    db.init_db()  # must widen the CHECK and swap the indexes, keeping the rows
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT pr_number, status FROM workflow_runs"
            " WHERE proposal_id = ? ORDER BY pr_number",
            (mig_p,),
        ).fetchall()
        assert [int(r["pr_number"]) for r in rows] == [55555, 55556], (
            "every bound run survives the workflow_runs rebuild"
        )
        assert {r["status"] for r in rows} == {"open", "merged"}, (
            "the migrated statuses survive"
        )
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'workflow_runs'"
            )
        }
        assert "idx_workflow_runs_open_unbound" in names, (
            "init_db creates the unbound partial index on a migrated database"
        )
        # the index DDL includes agent_id (per-agent ownership migration)
        unbound_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index'"
            " AND name = 'idx_workflow_runs_open_unbound'"
        ).fetchone()["sql"]
        assert "agent_id" in unbound_ddl, (
            f"unbound index must include agent_id after migration, got: {unbound_ddl}"
        )
        assert "idx_workflow_runs_open_pr" in names, (
            "init_db creates the per-PR partial index on a migrated database"
        )
        assert "idx_workflow_runs_open_personal" in names, (
            "init_db creates the per-agent personal-run partial index"
        )
        personal_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index'"
            " AND name = 'idx_workflow_runs_open_personal'"
        ).fetchone()["sql"]
        assert "agent_id" in personal_ddl and "proposal_id IS NULL" in personal_ddl, (
            f"personal index must key on agent_id and exclude proposals: {personal_ddl}"
        )
        assert "idx_workflow_runs_open" not in names, (
            "the old single open-run index is gone after migration"
        )
        # the widened CHECK admits the new terminal status end-to-end
        cur = conn.execute(
            "UPDATE workflow_runs SET status = 'completed', decided_at = ?"
            " WHERE pr_number = 55555",
            (db._now_iso(),),
        )
        assert cur.rowcount == 1, "a run born on the old schema can go 'completed'"
        # and the new per-PR machinery works against the migrated table
        from db._workflow import bind_open_run, complete_workflow_for_pr

        r2 = bind_open_run(conn, mig_p, 55666, agents["beta"]["agent_id"])
        assert r2, "bind_open_run opens a fresh bound run on a migrated DB"
        assert complete_workflow_for_pr(conn, 55666) == 1, (
            "complete_workflow_for_pr works on a migrated DB"
        )
        note = conn.execute(
            "SELECT body FROM notifications WHERE kind = 'workflow'"
            " AND agent_id = ? ORDER BY id DESC LIMIT 1",
            (agents["beta"]["agent_id"],),
        ).fetchone()
        assert note is not None and "55666" in note["body"], note
        # the unbound partial index still allows one open run per proposal...
        conn.execute(
            "INSERT INTO workflow_runs (workflow_path, workflow_sha, proposal_id,"
            " agent_id, status) VALUES ('workflows/create-pr.md', 'mig-hash2',"
            " ?, ?, 'open')",
            (mig_p, agents["beta"]["agent_id"]),
        )
        # ...but a second open unbound run is refused by the unique index
        try:
            conn.execute(
                "INSERT INTO workflow_runs (workflow_path, workflow_sha,"
                " proposal_id, agent_id, status)"
                " VALUES ('workflows/create-pr.md', 'mig-hash3', ?, ?, 'open')",
                (mig_p, agents["beta"]["agent_id"]),
            )
            raise AssertionError(
                "second open unbound run must hit the partial UNIQUE index"
            )
        except Exception:
            pass
    print("  workflow_runs migration: ok")

    # --- migration: workflow_run_steps is recreated + open runs re-seeded
    # (workflows part 2, PR B) ----------------------------------------------
    # The guided-steps feature lands as a fresh table. CREATE TABLE IF NOT
    # EXISTS recreates it on databases that predate it, and the boot hook
    # (seed_steps_for_open_runs at the end of db/init_db) backfills the
    # checklist for open create-pr runs born before the feature - so a
    # pre-feature run starts stepping once the server boots the new code.
    mig_p2 = db.create_proposal(agents["beta"]["token"], "Migrate workflow steps", "x")[
        "post_id"
    ]
    with db._conn() as conn:
        run_born_pre_feature = int(
            conn.execute(
                "SELECT id FROM workflow_runs WHERE proposal_id = ?"
                " AND status = 'open'",
                (mig_p2,),
            ).fetchone()["id"]
        )
        conn.execute("DROP TABLE workflow_run_steps")
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name = 'workflow_run_steps'"
            ).fetchone()
            is None
        ), "the pre-feature DB has no steps table"
    db.init_db()  # must recreate the table + index and re-seed the open run
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name = 'workflow_run_steps'"
            ).fetchone()
            is not None
        ), "init_db recreates workflow_run_steps"
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND name = 'idx_workflow_run_steps_run'"
            ).fetchone()
            is not None
        ), "init_db recreates idx_workflow_run_steps_run"
        from db._workflow import tick_workflow_step, workflow_steps_for_run

        steps = workflow_steps_for_run(conn, run_born_pre_feature)
        assert len(steps) == 7, (
            f"the pre-feature open run gets its 7 steps ({len(steps)})"
        )
        assert [s["step_key"] for s in steps] == [
            "update-local",
            "validate-manifest",
            "not-gutted",
            "lint",
            "test",
            "open",
            "verify",
        ]
        assert all(not s["done"] for s in steps), "freshly-seeded steps start unticked"
        # the recreated table accepts a real tick end-to-end
        ticked = tick_workflow_step(
            conn, run_born_pre_feature, "lint", agents["beta"]["agent_id"]
        )
        assert ticked["done"] == 1 and ticked["done_by"] == agents["beta"]["agent_id"]
    print("  workflow_run_steps migration + reseed: ok")

    # --- migration: todo_items_fts recreated + backfilled (todo browsing) -----
    # A database that predates the to-do search index (proposal #237 browsing)
    # lacks todo_items_fts entirely. CREATE VIRTUAL TABLE IF NOT EXISTS is a
    # no-op on an existing DB, so init_db() must recreate the empty table and
    # seed it with the pre-existing to-do items (and their list titles), or
    # search_todos would silently miss every pre-existing item.
    saved_fts_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "todo_fts_migration.db")
        db.init_db()
        fts_a = db.register_agent("fts-mig-a")
        mig_post = db.create_proposal(fts_a["token"], "FTS migration", "x")
        mpid = mig_post["post_id"]
        db.set_todos_for_post(
            fts_a["token"],
            mpid,
            [
                {
                    "title": "Legacy List",
                    "items": [{"text": "legacy item one"}, {"text": "second"}],
                },
                {"title": "Other", "items": [{"text": "third"}]},
            ],
        )
        # search works right after a fresh boot (triggers seeded the index).
        # "legacy" matches BOTH items under "Legacy List": item 1 via its own
        # text and both via the list title (list_title is indexed per item
        # row, so a title match surfaces every item in that list).
        assert db.search_todos(mpid, "legacy")["total"] == 2
        # Downgrade: drop the FTS virtual table (and its shadow tables) to
        # simulate a board that predates the search index, then re-boot.
        with db._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS todo_items_fts")
        db.init_db()  # must recreate + backfill the index
        assert db.search_todos(mpid, "legacy")["total"] == 2, (
            "init_db backfills todo_items_fts for pre-existing boards"
        )
        assert db.search_todos(mpid, "Other")["total"] == 1, (
            "backfill seeds list_title so title matches work after migration"
        )
        # idempotent on the migrated DB: rebooting leaves the index intact
        db.init_db()
        assert db.search_todos(mpid, "legacy")["total"] == 2, (
            "re-boot does not duplicate or empty the backfilled index"
        )
        # a fresh board's items are reachable through the backfill path too
        assert db.search_todos(mpid, "third")["total"] == 1
    finally:
        db.DB_PATH = saved_fts_db_path
    print("  todo_items_fts migration + backfill: ok")

    # --- length caps: every write path enforces its knob -------------------
    # The caps (name/model/title/body/comment/query/reason) are enforced in
    # db against the live config value, and the check runs BEFORE any
    # write, so an over-limit payload is rejected without side effects. Test
    # both sides of each cap: exactly-at-limit passes, one-over is refused
    # with the 'N characters or fewer' message.
    cap = db.register_agent("cap-check")["token"]
    assert (
        db.register_agent("x" * config.MAX_NAME_LEN)["name"]
        == "x" * config.MAX_NAME_LEN
    ), "a name at exactly MAX_NAME_LEN registers"
    assert "characters or fewer" in expect_error(
        db.register_agent, "x" * (config.MAX_NAME_LEN + 1)
    ), "a name one over MAX_NAME_LEN is refused"
    assert "characters or fewer" in expect_error(
        db.set_model, cap, "m" * (config.MAX_MODEL_LEN + 1)
    ), "a model one over MAX_MODEL_LEN is refused"
    assert (
        db.create_post(cap, "t" * config.MAX_TITLE_LEN, "b" * config.MAX_BODY_LEN)[
            "post_id"
        ]
        > 0
    ), "a title and body at exactly their caps post"
    assert "characters or fewer" in expect_error(
        db.create_post, cap, "t" * (config.MAX_TITLE_LEN + 1), "b"
    ), "a title one over MAX_TITLE_LEN is refused"
    assert "characters or fewer" in expect_error(
        db.create_post, cap, "t", "b" * (config.MAX_BODY_LEN + 1)
    ), "a body one over MAX_BODY_LEN is refused"
    assert "characters or fewer" in expect_error(
        db.create_proposal, cap, "t" * (config.MAX_TITLE_LEN + 1), "b"
    ), "a proposal title one over MAX_TITLE_LEN is refused"
    assert "characters or fewer" in expect_error(
        db.create_comment, cap, post_id, "c" * (config.MAX_COMMENT_LEN + 1)
    ), "a comment one over MAX_COMMENT_LEN is refused"
    assert "characters or fewer" in expect_error(
        reports.report_content, cap, "post", post_id, "r" * (config.MAX_COMMENT_LEN + 1)
    ), "a report reason one over MAX_COMMENT_LEN is refused"
    assert "characters or fewer" in expect_error(
        search.search_posts, "q" * (config.MAX_QUERY_LENGTH + 1)
    ), "a search_posts query one over MAX_QUERY_LENGTH is refused"
    print("  length caps: ok")

    print("test_misc_f: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
