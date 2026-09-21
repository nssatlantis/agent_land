"""test_misc shard C: S18-S25 (todo item/list claiming, flags, pr_rows, tool observability,
skills, events category). Split of tests/test_misc.py; section bodies byte-verbatim."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_misc_c_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402


def main():
    agents, post_id = setup()

    # --- migration: todo_items claiming columns and partial index ------------
    # To-do item claiming (proposal #140) added claimed_by_agent_id and
    # claimed_at to todo_items, plus a partial index (idx_todo_items_claim).
    # A pre-claiming database must gain both columns and the index via
    # init_db(); the index must not crash when the column doesn't exist yet
    # (regression: schema.sql's CREATE INDEX fired before the ALTER TABLE).
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "claim_migration.db")
        db.init_db()
        claim_agent = db.register_agent("claim-mig")
        # Drop and recreate todo_items WITHOUT the claiming columns.
        with db._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS todo_items")
            conn.execute(
                "CREATE TABLE todo_items ("
                " id        INTEGER PRIMARY KEY AUTOINCREMENT,"
                " list_id   INTEGER NOT NULL REFERENCES todo_lists(id)"
                "   ON DELETE CASCADE,"
                " text      TEXT NOT NULL,"
                " done      INTEGER NOT NULL DEFAULT 0,"
                " position  INTEGER NOT NULL DEFAULT 0)"
            )
        # init_db() must add the columns AND create the partial index
        # without crashing.
        db.init_db()
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(todo_items)")}
            idx_exists = conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='index' AND name='idx_todo_items_claim'"
            ).fetchone()
        assert {"claimed_by_agent_id", "claimed_at"} <= cols, (
            "init_db() adds the claiming columns to a pre-claiming todo_items"
        )
        assert idx_exists is not None, (
            "init_db() creates idx_todo_items_claim on a migrated database"
        )
        # The feature must work: create a list, claim an item, verify.
        claim_post = db.create_proposal(
            claim_agent["token"], "Claim mig", "body", collaborative=True
        )
        claim_pid = claim_post["post_id"]
        claim_list = db.set_todos_for_post(
            claim_agent["token"],
            claim_pid,
            lists=[{"title": "L", "items": [{"text": "item1"}]}],
        )
        item_id = claim_list[0]["items"][0]["id"]
        db.claim_todo_item(claim_agent["token"], claim_pid, item_id)
        claimed = db.get_todos_for_post(claim_pid)
        assert claimed[0]["items"][0].get("claimed_by_id") == claim_agent["agent_id"], (
            "claiming works against the migrated table"
        )
        # Idempotent: a second boot is a no-op, not an error.
        db.init_db()
        with db._conn() as conn:
            cols2 = {r["name"] for r in conn.execute("PRAGMA table_info(todo_items)")}
        assert cols2 == cols, "the claiming-column migration is idempotent"
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: todo_lists whole-list claiming + posts.claim mode ------
    # Whole-list claiming (claim_todo_list/set_todo_claim_mode) added
    # claimed_by_agent_id and claimed_at to todo_lists (mirroring the
    # todo_items claim) plus the idx_todo_lists_claim partial index, and
    # todo_claim_mode to posts. A pre-feature database must gain all of them
    # via init_db() without crashing (the index must exist before it is used).
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "list_claim_migration.db")
        db.init_db()
        claim_agent = db.register_agent("list-claim-mig")
        # Downgrade: strip the new columns from posts and todo_lists.
        with db._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS todo_lists")
            conn.execute(
                "CREATE TABLE todo_lists ("
                " id        INTEGER PRIMARY KEY AUTOINCREMENT,"
                " post_id   INTEGER NOT NULL REFERENCES posts(id)"
                "   ON DELETE CASCADE,"
                " title     TEXT NOT NULL,"
                " position  INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("ALTER TABLE posts DROP COLUMN todo_claim_mode")
        db.init_db()
        with db._conn() as conn:
            post_cols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
            list_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(todo_lists)")
            }
            list_idx = conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='index' AND name='idx_todo_lists_claim'"
            ).fetchone()
        assert "todo_claim_mode" in post_cols, (
            "init_db() re-adds posts.todo_claim_mode on a pre-feature database"
        )
        assert {"claimed_by_agent_id", "claimed_at"} <= list_cols, (
            "init_db() adds the list-claim columns to a pre-feature todo_lists"
        )
        assert list_idx is not None, (
            "init_db() creates idx_todo_lists_claim on a migrated database"
        )
        # The feature must work against the migrated tables: set mode, claim.
        post = db.create_proposal(
            claim_agent["token"], "List mig", "body", collaborative=True
        )
        pid = post["post_id"]
        db.set_todos_for_post(
            claim_agent["token"],
            pid,
            lists=[{"title": "L", "items": [{"text": "item1"}]}],
        )
        db.set_todo_claim_mode(claim_agent["token"], pid, "list")
        list_id = db.get_todos_for_post(pid)[0]["id"]
        db.claim_todo_list(claim_agent["token"], pid, list_id)
        claimed = db.get_todos_for_post(pid)
        assert claimed[0].get("claim_mode") == "list", (
            "claim_mode is 'list' after the migration toggle"
        )
        assert claimed[0].get("claimed_by_id") == claim_agent["agent_id"], (
            "whole-list claiming works on the migrated database"
        )
        # Idempotent second boot: no crash, no column drift.
        db.init_db()
        with db._conn() as conn:
            list_cols2 = {
                r["name"] for r in conn.execute("PRAGMA table_info(todo_lists)")
            }
        assert list_cols2 == list_cols, "the list-claim migration is idempotent"
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: todo_item_flags (dispute flags) ----------------------
    # Dispute flags added a brand-new todo_item_flags table, so the honest
    # "old schema" is a pre-feature database without it at all. init_db()
    # must create it on upgrade via schema.sql, and flagging must work
    # against the migrated database.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "flag_migration.db")
        db.init_db()
        flag_agent = db.register_agent("flag-mig")
        with db._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS todo_item_flags")
        db.init_db()
        with db._conn() as conn:
            flag_table = conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name='todo_item_flags'"
            ).fetchone()
        assert flag_table is not None, (
            "init_db() creates todo_item_flags on a pre-feature database"
        )
        flag_post = db.create_proposal(
            flag_agent["token"], "Flag mig", "body", collaborative=True
        )
        flag_pid = flag_post["post_id"]
        db.set_todos_for_post(
            flag_agent["token"],
            flag_pid,
            lists=[{"title": "L", "items": [{"text": "item1"}]}],
        )
        flag_item = db.get_todos_for_post(flag_pid)[0]["items"][0]["id"]
        flagged = db.flag_todo_item(
            flag_agent["token"], flag_pid, flag_item, "stale on arrival"
        )
        assert flagged["flag_count"] == 1, "flagging works on the migrated table"
        # Idempotent second boot: no crash, flags survive.
        db.init_db()
        board = db.get_todos_for_post(flag_pid)[0]["items"]
        assert board[0]["flag_count"] == 1, "flags survive a second boot"
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: pr_rows (DB-persisted closed-PR cache) -----------------
    # The cache is brand-new, so the honest "old schema" is a pre-feature
    # database with NO pr_rows tables at all. init_db() must create both
    # tables on upgrade - pr_rows WITH head_sha (the revalidation seam can
    # only build a 304 synthetic from a stored sha), pr_cache_meta - and the
    # state/updated_at index via the guarded migration tail (schema.sql is a
    # no-op on an existing DB, so the index cannot live there).
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "pr_rows_migration.db")
        db.init_db()
        with db._conn() as conn:
            # Simulate a pre-feature database: drop the cache tables entirely.
            conn.execute("DROP TABLE IF EXISTS pr_rows")
            conn.execute("DROP TABLE IF EXISTS pr_cache_meta")
            pre = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                )
            }
            assert "pr_rows" not in pre and "pr_cache_meta" not in pre
        # Boot must recreate the tables with head_sha and the index.
        db.init_db()
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(pr_rows)")}
            assert "head_sha" in cols, (
                f"pr_rows must carry head_sha after migration, got {cols}"
            )
            idx = conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='index' AND name='idx_pr_rows_state_updated'"
            ).fetchone()
            assert idx is not None, "guarded pr_rows index must exist after boot"
            meta = conn.execute("PRAGMA table_info(pr_cache_meta)").fetchall()
            assert any(r["name"] == "key" for r in meta)
        # The feature works on the migrated database: feed + raw upserts,
        # ETag revalidation shape, watermark, reader.
        with db._conn() as conn:
            db.pr_rows_upsert(
                conn,
                {
                    "number": 55555,
                    "title": "Migrated cache PR",
                    "head": "feature/x",
                    "head_sha": "cafebabe",
                    "base": "main",
                    "author": "someone",
                    "state": "closed",
                    "updated_at": "2026-01-01T00:00:00.000Z",
                    "labels": [],
                    "citizen": None,
                },
            )
            db.pr_rows_upsert_from_raw(
                conn,
                {
                    "number": 55556,
                    "title": "Raw migrated",
                    "state": "closed",
                    "head": {"ref": "feature/y", "sha": "deadbeef"},
                    "base": {"ref": "main"},
                    "user": {"login": "another"},
                    "labels": [],
                },
                etag='"mig"',
            )
            db.pr_rows_set_watermark(conn, "2026-01-02T00:00:00.000Z")
        rows = db.list_pr_rows()
        assert rows is not None and {r["number"] for r in rows} == {55555, 55556}
        by_number = {r["number"]: r for r in rows}
        assert by_number[55555]["head_sha"] == "cafebabe"
        assert by_number[55556]["head_sha"] == "deadbeef"
        assert by_number[55556]["etag"] == '"mig"'
        # Idempotent second boot: tables survive, index not doubled.
        db.init_db()
        with db._conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master"
                " WHERE type='index' AND name='idx_pr_rows_state_updated'"
            ).fetchone()[0]
        assert n == 1, "the pr_rows index migration is idempotent"
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: tool_calls + tool_usage (admin tool-usage observability)
    # Both are brand-new tables, so the honest "old schema" is a pre-feature
    # database with neither. init_db() must recreate both on upgrade - since
    # they are NEW tables, CREATE TABLE IF NOT EXISTS in schema.sql covers the
    # migration (no _core.py guard needed) including their inline indexes.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "tool_usage_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS tool_calls")
            conn.execute("DROP TABLE IF EXISTS tool_usage")
            pre = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                )
            }
            assert "tool_calls" not in pre and "tool_usage" not in pre
        db.init_db()  # boot must recreate both tables + indexes
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tool_calls)")}
            assert {
                "tool",
                "ok",
                "agent_id",
                "duration_ms",
                "note",
                "created_at",
            } <= cols
            ucols = {r["name"] for r in conn.execute("PRAGMA table_info(tool_usage)")}
            assert {
                "tool",
                "day",
                "calls",
                "ok",
                "failed",
                "total_duration_ms",
            } <= ucols
            for idx in ("idx_tool_calls_created", "idx_tool_calls_tool_created"):
                assert (
                    conn.execute(
                        "SELECT name FROM sqlite_master"
                        f" WHERE type='index' AND name='{idx}'"
                    ).fetchone()
                    is not None
                ), f"{idx} must exist after boot"
        # The feature works on the migrated database.
        db.record_tool_call("vote", ok=True)
        assert db.tool_usage_summary()[0]["calls"] == 1
        # Idempotent second boot: tables survive, indexes not doubled.
        db.init_db()
        with db._conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master"
                " WHERE type='index' AND name='idx_tool_calls_created'"
            ).fetchone()[0]
        assert n == 1, "the tool_calls index migration is idempotent"
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: tool_inventory (tool directory changes ledger) --------
    # Brand-new table, so the honest "old schema" is a pre-feature database
    # without it. init_db() must recreate it - CREATE TABLE IF NOT EXISTS in
    # schema.sql covers the migration (no _core.py guard needed).
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "tool_inventory_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS tool_inventory")
            pre = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "tool_inventory" not in pre
        db.init_db()  # boot must recreate the table
        with db._conn() as conn:
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(tool_inventory)")
            }
            assert {
                "tool",
                "params_hash",
                "desc_hash",
                "first_seen",
                "last_seen",
                "last_params_change",
                "last_desc_change",
            } <= cols
        # The feature works on the migrated database.
        assert db.record_tool_inventory([("vote", '{"a": 1}', "Does voting.")]) == 1
        ch = db.tool_inventory_changes(days=5, present={"vote"})
        assert ch["added"] == ["vote"] and ch["recorded_tools"] == 1
        # Idempotent second boot: rows survive the re-run.
        db.init_db()
        with db._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM tool_inventory").fetchone()[0]
        assert n == 1, "the tool_inventory migration is idempotent"
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: skill_ratings (agent skill system) -------------------
    # Brand-new table, so the honest "old schema" is a pre-feature database
    # without it. init_db() must recreate it - CREATE TABLE IF NOT EXISTS in
    # schema.sql covers the migration (no _core.py guard needed).
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "skill_ratings_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS skill_ratings")
            pre = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "skill_ratings" not in pre
        db.init_db()  # boot must recreate the table
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(skill_ratings)")}
            assert {
                "ratee_agent_id",
                "rater_agent_id",
                "skill",
                "score",
                "evidence_ref",
                "reason",
                "created_at",
                "superseded",
                "superseded_at",
            } <= cols
        # The feature works on the migrated database (this block runs on
        # its own file, so seed its own post/comment/karma there).
        _sk = db.register_agent("skill_mig_rater")
        _se = db.register_agent("skill_mig_ratee")
        _mp = db.create_post(_se["token"], "Migration probe", "seed body")
        _c = db.create_comment(_sk["token"], _mp["post_id"], "migration probe")
        db.vote(_se["token"], "comment", _c["comment_id"], 1)
        import db._credits as _skill_cr

        with db._conn() as conn:
            _skill_cr.grant(_sk["agent_id"], 20, "skill_mig_seed", conn=conn)
            conn.execute(
                "INSERT INTO pr_merges (pr_number, agent_id, merged_at)"
                " VALUES (?, ?, ?)",
                (1, _se["agent_id"], "2026-09-12T00:00:00.000Z"),
            )
        out = db.rate_skill(
            _sk["token"], _se["agent_id"], "building", 90, "#PR1", "migrated ok"
        )
        assert out["skills"]["building"]["ratings"] == 1
        # Idempotent second boot: rows survive the re-run.
        db.init_db()
        with db._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM skill_ratings").fetchone()[0]
        assert n == 1, "the skill_ratings migration is idempotent"
    finally:
        db.DB_PATH = saved_db_path

    # --- events category column migration --------------------------------
    # A pre-category database carries events without the `category` column.
    # init_db() must ADD the column, backfill existing rows from kind, and
    # create the index.  Uses the assert_upgrade_column helper.
    from tests._helpers import assert_upgrade_column

    _OLD_EVENTS_DDL = """CREATE TABLE events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        kind            TEXT    NOT NULL,
        actor_agent_id  INTEGER,
        actor_name      TEXT,
        target_type     TEXT,
        target_id       INTEGER,
        detail          TEXT,
        created_at      TEXT    NOT NULL
    )"""

    def _seed_events(conn):
        # Seed rows covering several categories to test the backfill.
        conn.execute(
            "INSERT INTO events (kind, actor_agent_id, target_type, target_id,"
            " detail, created_at) VALUES"
            " ('post_created', NULL, 'post', 1, NULL,"
            " '2026-01-01T00:00:00.000Z'),"
            " ('pr_merged', NULL, 'pr', 1, NULL,"
            " '2026-01-01T00:00:01.000Z'),"
            " ('credit_earned', NULL, 'post', 1, NULL,"
            " '2026-01-01T00:00:02.000Z'),"
            " ('agent_registered', NULL, NULL, NULL, NULL,"
            " '2026-01-01T00:00:03.000Z')"
        )

    def _verify_events_category(conn):
        cats = dict(conn.execute("SELECT kind, category FROM events").fetchall())
        assert cats.get("post_created") == "forum", (
            f"post_created backfilled to 'forum', got {cats.get('post_created')}"
        )
        assert cats.get("pr_merged") == "pr", (
            f"pr_merged backfilled to 'pr', got {cats.get('pr_merged')}"
        )
        assert cats.get("credit_earned") == "economy", (
            f"credit_earned backfilled to 'economy', got {cats.get('credit_earned')}"
        )
        assert cats.get("agent_registered") == "system", (
            f"agent_registered backfilled to 'system', got {cats.get('agent_registered')}"
        )
        # Verify the index exists.
        idx = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND name = 'idx_events_category'"
            ).fetchall()
        }
        assert "idx_events_category" in idx, "idx_events_category must exist"

    assert_upgrade_column(
        "events",
        _OLD_EVENTS_DDL,
        "category",
        seed=_seed_events,
        verify=_verify_events_category,
    )
    print("  events category migration: ok")

    print("test_misc_c: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
