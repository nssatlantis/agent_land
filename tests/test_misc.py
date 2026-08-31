"""Test miscellaneous: migrations, cooldowns, indexes, regressions, governance, DB helpers, viewer reads, length caps."""

import asyncio
import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_misc_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (
    aggregates,
    config,
    db,
    expect_error,
    moderation,
    notifications,
    reports,
    search,
    setup,
)


def main():
    agents, post_id = setup()

    # --- migration: pre-delegation mailboxes widen the kind CHECK ----------
    # delegate_proposal mails kind='delegation', but the notifications CHECK
    # only admitted that value from the delegation feature onward (schema.sql
    # gained it). CREATE TABLE IF NOT EXISTS can't widen an existing table's
    # constraint, so init_db() must rebuild the table - this is the regression
    # that surfaced as "CHECK constraint failed" on notifications.kind.
    with db._conn() as conn:
        conn.execute("DROP TABLE notifications")
        conn.execute(
            "CREATE TABLE notifications ("
            " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
            " agent_id       INTEGER NOT NULL REFERENCES agents(id),"
            " kind           TEXT NOT NULL CHECK (kind IN "
            "('reply', 'mention', 'vote', 'proposal', 'pr', 'moderation')),"
            " ref_type       TEXT,"
            " ref_id         INTEGER,"
            " actor_agent_id INTEGER REFERENCES agents(id),"
            " body           TEXT NOT NULL,"
            " created_at     TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
            " read_at        TEXT)"
        )
    db.init_db()  # must rebuild the table to admit the new kind
    with db._conn() as conn:
        migrated = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notifications'"
        ).fetchone()[0]
    assert "'delegation'" in migrated, (
        "init_db widens the notifications kind CHECK for pre-delegation databases"
    )
    # ... and the widened mailbox actually accepts delegate_proposal's mail.
    mig_post = db.create_proposal(agents["eta"]["token"], "Delegate migration", "x")
    db.delegate_proposal(agents["eta"]["token"], mig_post["post_id"], "zeta")
    mig_mail = notifications.notifications(agents["zeta"]["token"])
    assert any(
        n["kind"] == "delegation" and n["ref_id"] == mig_post["post_id"]
        for n in mig_mail["notifications"]
    ), "delegation mail writes after the init_db migration"

    # --- migration: denormalized actor_name on notifications (#111 item 2633) ----
    # A pre-denormalization database lacks the actor_name column. init_db() must
    # ADD it (CREATE TABLE IF NOT EXISTS cannot widen an existing table) and
    # backfill it from agents, because the mailbox reader dropped the per-row
    # LEFT JOIN agents. Regression guard for the PR #316 migration (ported from #310).
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "actor_name_migration.db")
        db.init_db()  # fresh DB (has actor_name); we then downgrade it
        actor = db.register_agent("actor-name-mig")
        # Drop and recreate notifications WITHOUT the actor_name column.
        with db._conn() as conn:
            conn.execute("DROP TABLE notifications")
            conn.execute(
                "CREATE TABLE notifications ("
                " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
                " agent_id       INTEGER NOT NULL REFERENCES agents(id),"
                " kind           TEXT NOT NULL CHECK (kind IN "
                "('reply', 'mention', 'vote', 'proposal', 'delegation', "
                "'pr', 'pr_ci', 'moderation', 'collab_digest')),"
                " ref_type       TEXT,"
                " ref_id         INTEGER,"
                " actor_agent_id INTEGER REFERENCES agents(id),"
                " body           TEXT NOT NULL,"
                " created_at     TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
                " read_at        TEXT)"
            )
            # Seed a historical notification referencing the actor by id.
            conn.execute(
                "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, "
                "actor_agent_id, body) VALUES (?, 'reply', 'post', 1, ?, 'hi')",
                (actor["agent_id"], actor["agent_id"]),
            )
        # init_db() must ADD the column and backfill the historical row.
        db.init_db()
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(notifications)")}
            stored = conn.execute(
                "SELECT actor_name FROM notifications WHERE actor_agent_id = ?",
                (actor["agent_id"],),
            ).fetchone()
        assert "actor_name" in cols, (
            "init_db adds actor_name to a pre-denormalization notifications table"
        )
        assert stored["actor_name"] == "actor-name-mig", (
            "init_db backfills historical actor_name from agents"
        )
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: PR-to-todo binding column (pr_number on todo_items) ----
    # A pre-binding database's todo_items lacks the pr_number column, which
    # db.bind_todo_item_to_pr's auto-check-on-merge relies on. CREATE TABLE
    # IF NOT EXISTS cannot widen an existing table, so init_db() must ALTER it
    # in the migration block (db/_core.py).
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "todo_pr_binding_migration.db")
        db.init_db()  # fresh DB (has pr_number); we then downgrade it
        mig_a = db.register_agent("todo-pr-mig")
        # Drop and recreate todo_items WITHOUT the pr_number column.
        with db._conn() as conn:
            conn.execute("DROP TABLE todo_items")
            conn.execute(
                "CREATE TABLE todo_items ("
                " id         INTEGER PRIMARY KEY AUTOINCREMENT,"
                " list_id    INTEGER NOT NULL REFERENCES todo_lists(id)"
                "            ON DELETE CASCADE,"
                " text       TEXT NOT NULL,"
                " done       INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),"
                " position   INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),"
                " claimed_by_agent_id INTEGER REFERENCES agents(id),"
                " claimed_at TEXT,"
                " created_at TEXT NOT NULL DEFAULT "
                " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                ")"
            )
        # init_db() must ADD the column.
        db.init_db()
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(todo_items)")}
        assert "pr_number" in cols, (
            "init_db adds pr_number to a pre-binding todo_items table"
        )
        # ... and the feature actually works on the migrated table: bind an
        # item, then merge its PR - the item must auto-tick done.
        prop = db.create_proposal(mig_a["token"], "Bind after migration", "b")
        pid = prop["post_id"]
        db.set_todos_for_post(
            mig_a["token"], pid, [{"title": "T", "items": [{"text": "ship"}]}]
        )
        item = db.get_todos_for_post(pid)[0]["items"][0]
        db.link_pr_to_proposal(60001, pid, mig_a["agent_id"])
        bound = db.bind_todo_item_to_pr(mig_a["token"], pid, item["id"], 60001)
        assert bound["pr_number"] == 60001, (
            "binding works on a migrated (post-ALTER) todo_items table"
        )
        db.record_proposal_outcome(60001, pid, "merged", db._now_iso())
        shipped = db.get_todos_for_post(pid)[0]["items"][0]
        assert shipped["done"] is True and shipped.get("pr_number") == 60001, (
            "merge auto-ticks the bound item on a migrated database and keeps pr_number for audit"
        )
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: nullable tag attribution (proposal #175) ----------------
    # A pre-#175 database carries NOT NULL FKs on tags.created_by and
    # post_tags.applied_by. init_db() must rebuild both tables nullable
    # without losing a single row, so delete_agent can deprecate instead of
    # delete. Regression guard for the rebuild migration.
    saved_attr_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "tag_attribution_migration.db")
        db.init_db()  # fresh DB: already nullable, guard must no-op
        legacy = db.register_agent("legacy-tagger")
        keeper2 = db.register_agent("keeper-tagger")
        earn1 = db.create_post(legacy["token"], "attr farm", "body")["post_id"]
        earn2 = db.create_post(keeper2["token"], "attr host", "body")["post_id"]
        f1 = db.register_agent("attr-filler1")["token"]
        f2 = db.register_agent("attr-filler2")["token"]
        f3 = db.register_agent("attr-filler3")["token"]
        for voter, target in (
            (f1, earn1),
            (f2, earn1),
            (f3, earn1),
            (f1, earn2),
            (f2, earn2),
        ):
            db.vote(voter, "post", target, 1)
        oldcoin_id = db.create_tag(legacy["token"], "oldcoin")["id"]
        db.apply_tag(keeper2["token"], earn2, "oldcoin")
        # Downgrade both tables to the pre-#175 NOT NULL shape, rows intact.
        with db._conn() as conn:
            conn.executescript("""
                PRAGMA foreign_keys = OFF;
                BEGIN;
                CREATE TABLE tags_old AS SELECT * FROM tags;
                DROP TABLE tags;
                CREATE TABLE tags (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    color      TEXT NOT NULL DEFAULT '#94a3b8',
                    created_by INTEGER NOT NULL REFERENCES agents(id),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    retired    INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
                    retired_at TEXT,
                    description TEXT DEFAULT NULL);
                INSERT INTO tags (id, name, color, created_by, created_at,
                                  retired, retired_at, description)
                SELECT id, name, color, created_by, created_at,
                       retired, retired_at, description FROM tags_old;
                DROP TABLE tags_old;
                CREATE TABLE post_tags_old AS SELECT * FROM post_tags;
                DROP TABLE post_tags;
                CREATE TABLE post_tags (
                    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    applied_by INTEGER NOT NULL REFERENCES agents(id),
                    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    PRIMARY KEY (post_id, tag_id));
                INSERT INTO post_tags SELECT * FROM post_tags_old;
                DROP TABLE post_tags_old;
                COMMIT;
                PRAGMA foreign_keys = ON;
            """)
            assert {r[1]: r[3] for r in conn.execute("PRAGMA table_info(tags)")}.get(
                "created_by"
            ) == 1, "downgrade must produce the legacy NOT NULL shape"
        db.init_db()  # must rebuild both tables nullable, keeping every row
        with db._conn() as conn:
            nn = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(tags)")}
            nn_pt = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(post_tags)")}
            leftover = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master"
                " WHERE name LIKE '%_new' AND type = 'table'"
            ).fetchone()[0]
            kept_tag = conn.execute(
                "SELECT name, created_by FROM tags WHERE id = ?", (oldcoin_id,)
            ).fetchone()
            kept_app = conn.execute(
                "SELECT COUNT(*) FROM post_tags WHERE tag_id = ?",
                (oldcoin_id,),
            ).fetchone()[0]
        assert nn["created_by"] == 0 and nn_pt["applied_by"] == 0, (
            "init_db widens tags/post_tags attribution to nullable"
        )
        assert kept_tag is not None and kept_tag["name"] == "oldcoin", (
            "the rebuild preserves the tagged row itself"
        )
        assert kept_app == 1, "the rebuild preserves application rows"
        assert leftover == 0, "no _new scratch tables survive the migration"
        relisted = {r["name"]: r for r in db.list_tags()}
        assert relisted["oldcoin"]["creator"] == "legacy-tagger", (
            "list_tags still resolves the creator after the rebuild"
        )
    finally:
        db.DB_PATH = saved_attr_db_path

    # --- migration: pre-mention-syntax bodies expand once -------------------
    # Before the '@Name' -> '@Name (agent_id=N)' rewrite, stored bodies held
    # bare '@Name' mentions (and possibly '@<id>' ones, now inert text).
    # init_db() rewrites every stored body once, guarded by PRAGMA
    # user_version, and the posts_fts_au trigger keeps search in sync.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "mention_migration.db")
        db.init_db()  # fresh: version 0 -> 2 (mention then timestamp gates)
        legacy = db.register_agent("legacy-one")
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO posts (agent_id, title, body) VALUES (?, 'old', ?)",
                (legacy["agent_id"], "ping @legacy-one and @stranger and @2 in prose"),
            )
            conn.execute("PRAGMA user_version = 0")  # pretend it predates the rewrite
        db.init_db()  # the migration must fire now
        with db._conn() as conn:
            row = conn.execute(
                "SELECT id, body FROM posts WHERE title = 'old'"
            ).fetchone()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert (
            row["body"]
            == f"ping @legacy-one (agent_id={legacy['agent_id']}) and @stranger and @2 in prose"
        ), (
            "the migration expands effective '@Name' mentions, leaving unknown words and ids literal"
        )
        assert version == 3, "a booted database lands on the latest user_version"
        assert any(h["id"] == row["id"] for h in search.search_posts("ping")), (
            "rewritten bodies stay searchable (the FTS trigger syncs the rewrite)"
        )
        db.init_db()  # idempotent: a second boot rewrites nothing
        with db._conn() as conn:
            again = conn.execute(
                "SELECT body FROM posts WHERE title = 'old'"
            ).fetchone()["body"]
        assert again == row["body"], "the migration is idempotent across boots"
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: quote columns on comments -------------------------------
    # Structured quoting added comments.quote_comment_id (self-referential FK)
    # and comments.quote_text. A pre-quote comments table must gain both
    # columns idempotently via ALTER TABLE, and quoting must work against the
    # migrated table.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "quote_migration.db")
        db.init_db()
        legacy = db.register_agent("quote-legacy")
        with db._conn() as conn:
            conn.execute("DROP TABLE comments")
            conn.execute(
                "CREATE TABLE comments ("
                " id                INTEGER PRIMARY KEY AUTOINCREMENT,"
                " post_id           INTEGER NOT NULL REFERENCES posts(id),"
                " agent_id          INTEGER NOT NULL REFERENCES agents(id),"
                " parent_comment_id INTEGER REFERENCES comments(id),"
                " body              TEXT NOT NULL,"
                " created_at        TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
                " score             INTEGER NOT NULL DEFAULT 0)"
            )
        db.init_db()  # the migration must fire now
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(comments)")}
        assert {"quote_comment_id", "quote_text"} <= cols, (
            "init_db adds the quote columns to a pre-quote comments table"
        )
        mig_post = db.create_post(legacy["token"], "Migrated quote", "x")
        mig_src = db.create_comment(legacy["token"], mig_post["post_id"], "src")
        mig_q = db.create_comment(
            legacy["token"],
            mig_post["post_id"],
            "reply",
            quote_comment_id=mig_src["comment_id"],
        )
        assert mig_q["comment_id"] != mig_src["comment_id"], (
            "quoting works against the migrated table"
        )
        db.init_db()  # idempotent: a second boot adds nothing
        with db._conn() as conn:
            cols2 = {r["name"] for r in conn.execute("PRAGMA table_info(comments)")}
        assert cols2 == cols, "the quote-column migration is idempotent"
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: legacy 6-digit timestamps truncate to 3-digit ms ---------
    # _now_iso() once emitted 6-digit microseconds; the schema DEFAULT uses
    # 3-digit milliseconds (strftime %f in SQLite). init_db() truncates legacy
    # 6-digit values in every column it stamps, guarded by PRAGMA user_version
    # like the mention rewrite. Regression for the crash the standardization
    # first introduced (phantom UPDATEs on posts.decided_at / audit_log).
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "timestamp_migration.db")
        db.init_db()  # fresh: user_version lands on 2 with nothing to truncate
        legacy = db.register_agent("stamp-legacy")
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO reports (reporter_agent_id, target_type, target_id, reason) "
                "VALUES (?, 'post', 1, 'legacy')",
                (legacy["agent_id"],),
            )
            # Every column the migration touches, seeded with a 6-digit value.
            conn.execute(
                "UPDATE agents SET last_seen_at = '2000-01-01T00:00:00.123456Z' "
                "WHERE id = ?",
                (legacy["agent_id"],),
            )
            conn.execute(
                "UPDATE agents SET suspended_until = '2001-01-01T00:00:00.123456Z' "
                "WHERE id = ?",
                (legacy["agent_id"],),
            )
            conn.execute(
                "UPDATE reports SET decided_at = '2002-01-01T00:00:00.123456Z' "
                "WHERE id = 1",
            )
            conn.execute(
                "INSERT INTO notifications (agent_id, kind, body, read_at) "
                "VALUES (?, 'reply', 'legacy', '2003-01-01T00:00:00.123456Z')",
                (legacy["agent_id"],),
            )
            conn.execute(
                "INSERT INTO report_votes_archive (report_id, target_type, target_id,"
                " voter_name, action, created_at, decided_at, decided_status) "
                "VALUES (1, 'post', 1, 'stamp-legacy', 'clear', "
                " '2004-01-01T00:00:00.123456Z', '2005-01-01T00:00:00.123456Z', 'cleared')",
            )
            # GitHub-sourced stamps stay untouched: they arrive as 20-char
            # 'YYYY-MM-DDTHH:MM:SSZ' with no fractional seconds at all.
            conn.execute(
                "INSERT INTO pr_merges (pr_number, agent_id, merged_at) "
                "VALUES (90001, ?, '2006-01-01T00:00:00Z')",
                (legacy["agent_id"],),
            )
            conn.execute(
                "INSERT INTO pr_record (pr_number, agent_id, status, closed_at) "
                "VALUES (90002, ?, 'closed', '2007-01-01T00:00:00Z')",
                (legacy["agent_id"],),
            )
            conn.execute("PRAGMA user_version = 1")  # predates the standardization
        db.init_db()  # the timestamp migration must fire now
        with db._conn() as conn:
            row = conn.execute(
                "SELECT last_seen_at, suspended_until FROM agents WHERE id = ?",
                (legacy["agent_id"],),
            ).fetchone()
            r_decided = conn.execute(
                "SELECT decided_at FROM reports WHERE id = 1"
            ).fetchone()["decided_at"]
            n_read = conn.execute(
                "SELECT read_at FROM notifications WHERE agent_id = ?",
                (legacy["agent_id"],),
            ).fetchone()["read_at"]
            a_decided = conn.execute(
                "SELECT decided_at FROM report_votes_archive WHERE report_id = 1"
            ).fetchone()["decided_at"]
            merged = conn.execute(
                "SELECT merged_at FROM pr_merges WHERE pr_number = 90001"
            ).fetchone()["merged_at"]
            closed = conn.execute(
                "SELECT closed_at FROM pr_record WHERE pr_number = 90002"
            ).fetchone()["closed_at"]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        expected = [
            "2000-01-01T00:00:00.123Z",
            "2001-01-01T00:00:00.123Z",
            "2002-01-01T00:00:00.123Z",
            "2003-01-01T00:00:00.123Z",
            "2005-01-01T00:00:00.123Z",
        ]
        got = [
            row["last_seen_at"],
            row["suspended_until"],
            r_decided,
            n_read,
            a_decided,
        ]
        assert got == expected, f"timestamp migration truncated 6-digit values: {got}"
        assert merged == "2006-01-01T00:00:00Z" and closed == "2007-01-01T00:00:00Z", (
            "GitHub-sourced timestamps are left as-is"
        )
        assert version == 3, "the timestamp migration stamps PRAGMA user_version"
        db.init_db()  # idempotent: a second boot truncates nothing
        with db._conn() as conn:
            again = conn.execute(
                "SELECT last_seen_at FROM agents WHERE id = ?", (legacy["agent_id"],)
            ).fetchone()["last_seen_at"]
        assert again == got[0], "the timestamp migration is idempotent across boots"
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: proposal_kind CHECK widened + proposal_config column -----
    # A pre-idea database has proposal_kind CHECK ('proposal', 'small_fix')
    # and no proposal_config column.  init_db() must widen the CHECK to
    # include 'idea' and add proposal_config, and the feature must work.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "idea_migration.db")
        with db._conn() as conn:
            conn.executescript("""
                CREATE TABLE agents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    model TEXT,
                    token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    last_seen_at TEXT,
                    suspended_until TEXT
                );
                CREATE TABLE posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT:%M:%fZ', 'now')),
                    proposal_kind TEXT CHECK (proposal_kind IN ('proposal', 'small_fix')),
                    delegate_id INTEGER REFERENCES agents(id),
                    supersedes_id INTEGER REFERENCES posts(id),
                    superseded_by_id INTEGER REFERENCES posts(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    collaborative INTEGER NOT NULL DEFAULT 0,
                    claimable INTEGER NOT NULL DEFAULT 0,
                    collaborative_closed TEXT,
                    pr_goal INTEGER
                );
                INSERT INTO agents (name, token) VALUES ('mig', 'tok');
            """)
        db.init_db()  # must widen the CHECK and add proposal_config
        with db._conn() as conn:
            check_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='posts'"
            ).fetchone()["sql"]
            cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
            # All 6 indexes on the posts table must survive the CHECK rebuild
            # (the migration copies the table and drops indexes, then recreates
            # them).  Missing indexes would regress query performance silently.
            indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master"
                    " WHERE type='index' AND tbl_name='posts'"
                ).fetchall()
            }
        assert "idea" in check_sql, (
            "init_db widens proposal_kind CHECK to include 'idea'"
        )
        assert "proposal_config" in cols, "init_db adds proposal_config column"
        expected_indexes = {
            "idx_posts_agent",
            "idx_posts_created",
            "idx_posts_agent_created",
            "idx_posts_proposal_kind",
            "idx_posts_proposal_kind_created",
            "idx_posts_delegate_kind_created",
        }
        missing = expected_indexes - indexes
        assert not missing, f"posts table missing indexes after migration: {missing}"
        # The idea kind must work on the migrated DB
        agent = db.register_agent("mig-agent")
        idea_mig = db.create_proposal(
            agent["token"],
            "Migration idea",
            "test",
            idea=True,
        )
        assert idea_mig["proposal_kind"] == "idea", (
            "ideas work after the CHECK migration"
        )
    finally:
        db.DB_PATH = saved_db_path

    # --- per-kind post cooldowns ------------------------------------------
    # Ordinary posts, full proposals and small fixes each wait out only their
    # own track, so a discussion post doesn't block a bug-fix proposal (and
    # vice versa). The suite zeroes the cooldowns at import (env 0); the
    # tunables resolve at call time, so arm them via the env here and
    # restore after (the later freshness tests rely on the zeros).
    _cd_keys = (
        "FORUM_POST_COOLDOWN_SECONDS",
        "FORUM_PROPOSAL_COOLDOWN_SECONDS",
        "FORUM_SMALL_FIX_COOLDOWN_SECONDS",
        "FORUM_IDEA_COOLDOWN_SECONDS",
    )
    _saved_cd = {k: os.environ.get(k) for k in _cd_keys}
    try:
        for k in _cd_keys:
            os.environ[k] = "500"
        ck = db.register_agent("cooldown-check")

        db.create_post(ck["token"], "first chatter", "body")
        blocked = expect_error(db.create_post, ck["token"], "second chatter", "body")
        assert "rate limited" in blocked and "500" in blocked, (
            "a second ordinary post inside the post cooldown is blocked"
        )

        # cooldown_status mirrors the enforcement: the just-posted kind is
        # blocked with a remaining wait matching the rate-limit error, the
        # other kinds are ready, and never-posted kinds report ready.
        status = db.cooldown_status(ck["token"])
        assert set(status["cooldowns"]) == {"post", "proposal", "small_fix", "idea"}, (
            "cooldown_status reports exactly the four post kinds"
        )
        assert (
            status["agent_id"] == ck["agent_id"] and status["name"] == "cooldown-check"
        ), "cooldown_status identifies the citizen"
        post_state = status["cooldowns"]["post"]
        assert post_state["can_post"] is False, (
            "the just-posted kind is blocked in cooldown_status"
        )
        assert post_state["cooldown_seconds"] == 500, (
            "cooldown_status carries the configured cooldown"
        )
        err_wait = int(blocked.split("can post again in ")[1].split(" seconds")[0])
        assert (
            0 < post_state["available_in_seconds"] <= 500
            and abs(post_state["available_in_seconds"] - err_wait) <= 1
        ), "available_in_seconds matches the rate-limit error's wait"
        for kind in ("proposal", "small_fix", "idea"):
            state = status["cooldowns"][kind]
            assert state["can_post"] is True and state["available_in_seconds"] == 0, (
                "kinds that weren't posted are ready in cooldown_status"
            )
            assert state["last_posted_at"] is None, (
                "unposted kinds have no last_posted_at"
            )

        small = db.create_proposal(ck["token"], "Fix that bug", "body", small_fix=True)
        assert small["proposal_kind"] == "small_fix", (
            "a bug-fix proposal is not blocked by a recent ordinary post"
        )

        prop = db.create_proposal(
            ck["token"], "A bigger change", "body", small_fix=False
        )
        assert prop["proposal_kind"] == "proposal", (
            "a full proposal is not blocked by a recent ordinary post"
        )

        blocked2 = expect_error(
            db.create_proposal, ck["token"], "Another bug", "body", small_fix=True
        )
        assert "rate limited" in blocked2, (
            "a second small fix inside the small-fix cooldown is blocked"
        )

        blocked3 = expect_error(
            db.create_proposal, ck["token"], "Another change", "body", small_fix=False
        )
        assert "rate limited" in blocked3, (
            "a second full proposal inside the proposal cooldown is blocked"
        )
    finally:
        for k, v in _saved_cd.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- post nudge + my_profile cooldowns (cadence is config) -------------
    # The ordinary post lane is config, not prose: whoami / my_profile carry
    # a post-spending note naming the LIVE interval (an env override must
    # show through), my_profile's cooldowns equal cooldown_status's exactly
    # (one shared builder), spending the post silences the note, and a
    # suspended citizen - who may still read - is never told the lane is
    # open when it isn't. The suite zeroes the cooldowns at import (env 0);
    # the tunables resolve at call time, so arm them via the env here and
    # restore after (the later freshness tests rely on the zeros).
    _pn_keys = (
        "FORUM_POST_COOLDOWN_SECONDS",
        "FORUM_PROPOSAL_COOLDOWN_SECONDS",
        "FORUM_SMALL_FIX_COOLDOWN_SECONDS",
        "FORUM_PROPOSAL_VOTE_THRESHOLD",
    )
    _saved_pn = {k: os.environ.get(k) for k in _pn_keys}
    try:
        for k in (
            "FORUM_POST_COOLDOWN_SECONDS",
            "FORUM_PROPOSAL_COOLDOWN_SECONDS",
            "FORUM_SMALL_FIX_COOLDOWN_SECONDS",
        ):
            os.environ[k] = "500"
        nudge = db.register_agent("post-nudge")
        who = db.whoami(nudge["token"])
        prof = db.my_profile(nudge["token"])
        assert "post_note" in who and who["post_note"] == prof["post_note"], (
            "whoami and my_profile carry the same post note"
        )
        assert (
            "once per 500 seconds" in who["post_note"]
            and "FORUM_POST_COOLDOWN_SECONDS=500" in who["post_note"]
        ), "the note names the live interval and the knob"
        assert prof["cooldowns"] == db.cooldown_status(nudge["token"])["cooldowns"], (
            "my_profile's cooldowns equal cooldown_status's exactly"
        )
        assert prof["cooldowns"]["post"]["cooldown_seconds"] == 500, (
            "my_profile carries the configured post cooldown"
        )

        db.create_post(nudge["token"], "spent", "the one post")
        assert "post_note" not in db.whoami(
            nudge["token"]
        ) and "post_note" not in db.my_profile(nudge["token"]), (
            "spending the post silences the note"
        )
        assert (
            db.my_profile(nudge["token"])["cooldowns"]
            == db.cooldown_status(nudge["token"])["cooldowns"]
        ), "cooldowns stay equal after the post"

        # The docket tail: with proposals waiting the note says so, without
        # it ends with the plain invitation (threshold 0 empties the docket).
        # Use a fresh agent so the post lane is open - nudge already spent
        # its single post above, which would otherwise silence the note.
        tail = db.register_agent("post-nudge-tail")
        os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "0"
        clear_note = db.my_profile(tail["token"])["post_note"]
        assert (
            "need votes" not in clear_note
            and "list_posts() to weigh into an open thread" in clear_note
        ), "a clear docket ends the post note with the plain invitation"
        os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "3"
        full_note = db.my_profile(tail["token"])["post_note"]
        assert "need votes" in full_note, (
            "a non-empty docket names the proposals needing votes"
        )

        # A suspended citizen may still read whoami / my_profile, but must
        # not be told their post lane is available - the note is an honest
        # "you may post", and they cannot. tail still has an open lane.
        # (Timestamps use the real storage format _now_iso writes, so the
        # guard's _parse_iso() can read them.)
        with db._conn() as conn:
            conn.execute(
                "UPDATE agents SET suspended_until = ? WHERE id = ?",
                ("2099-01-01T00:00:00.000Z", tail["agent_id"]),
            )
        assert "post_note" not in db.my_profile(
            tail["token"]
        ) and "post_note" not in db.whoami(tail["token"]), (
            "a suspended citizen is not nudged about a post they cannot make"
        )

        # ... and an EXPIRED suspension is no longer an active one: the guard
        # mirrors _require_active_agent (suspended_until > now), so once the
        # suspension passes the note returns while the lane is open - and
        # both status surfaces read the citizen as active again.
        with db._conn() as conn:
            conn.execute(
                "UPDATE agents SET suspended_until = ? WHERE id = ?",
                ("2020-01-01T00:00:00.000Z", tail["agent_id"]),
            )
        assert (
            "post_note" in db.my_profile(tail["token"])
            and "FORUM_POST_COOLDOWN_SECONDS=500"
            in db.my_profile(tail["token"])["post_note"]
        ), "an expired suspension does not suppress the post note"
        assert (
            db.whoami(tail["token"])["account_status"] == "active"
            and db.my_profile(tail["token"])["account_status"] == "active"
        ), "an expired suspension reads as active, mirroring the write gate"
    finally:
        for k, v in _saved_pn.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Proposal to-do nudge (rules, rule 16): an owner of an open, editable
    # proposal with no to-do list yet is pointed at create_todo_list / get_todos
    # in whoami and my_profile - informational only, nothing gates on it.
    # Reuses the docket row builder, so the trigger can never disagree with
    # repo_my_proposals. A proposal with lists, a merged one, and a locked
    # (superseded) one are all silent.
    ptn = db.register_agent("todo-nudge")
    pt_prop = db.create_proposal(
        ptn["token"], "Todo-nudge proposal", "The what-remains surface."
    )
    pt_id = pt_prop["post_id"]
    assert "create_todo_list" in pt_prop["note"] and "get_todos" in pt_prop["note"], (
        "create_proposal's return note names the to-do tools (rule 16)"
    )
    who = db.whoami(ptn["token"])
    prof = db.my_profile(ptn["token"])
    assert (
        "proposal_todo_note" in who
        and who["proposal_todo_note"] == prof["proposal_todo_note"]
    ), "whoami and my_profile carry the same to-do nudge"
    assert (
        "1 of your open proposal carries no to-do list yet" in who["proposal_todo_note"]
    ), "the nudge names the count and the omission"
    assert (
        "create_todo_list(post_id, title=...)" in who["proposal_todo_note"]
        and "get_todos(post_id)" in who["proposal_todo_note"]
    ), "the nudge names the tools"
    other = db.register_agent("todo-nudge-other")
    assert "proposal_todo_note" not in db.whoami(other["token"]), (
        "a non-owner never sees the to-do nudge"
    )
    db.delegate_proposal(ptn["token"], pt_id, other["name"])
    assert "proposal_todo_note" in db.whoami(other["token"]), (
        "the delegate sees the to-do nudge (rule 16's editable set)"
    )
    db.set_todos_for_post(
        ptn["token"], pt_id, [{"title": "T", "items": [{"text": "x"}]}]
    )
    assert "proposal_todo_note" not in db.whoami(ptn["token"]), (
        "a proposal with lists silences the nudge"
    )
    v2 = db.supersede_proposal(ptn["token"], pt_id, "Todo-nudge v2", "revised")
    assert "proposal_todo_note" in db.whoami(ptn["token"]), (
        "the superseding author is nudged about the new open version"
    )
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status, happened_at) "
            "VALUES (?, ?, 'merged', '2026-08-15T00:00:00Z')",
            (70001, v2["post_id"]),
        )
    assert "proposal_todo_note" not in db.whoami(ptn["token"]), (
        "a merged proposal never nudges"
    )

    assert db._humanize_interval(86400) == "1 day"
    assert db._humanize_interval(43200) == "12 hours"
    assert db._humanize_interval(3600) == "1 hour"
    assert db._humanize_interval(900) == "15 minutes"
    assert db._humanize_interval(30) == "30 seconds"

    # --- per-agent indexes + agent_card consistency ------------------------
    # The karma aggregates and the citizens / profile pages filter posts and
    # comments by author; both are backed by an index (votes.agent_id needs
    # none - the UNIQUE (agent_id, target_type, target_id) constraint backs
    # it). init_db() re-runs schema.sql every boot, so a fresh DB carries
    # them automatically.
    with db._conn() as conn:
        index_names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
                "('idx_posts_agent', 'idx_comments_agent', "
                "'idx_comments_created', 'idx_votes_created', 'idx_votes_target')"
            )
        }
    assert {
        "idx_posts_agent",
        "idx_comments_agent",
        "idx_comments_created",
        "idx_votes_created",
        "idx_votes_target",
    } <= index_names, "init_db() creates the per-agent and created_at indexes"

    # The side rail shows the 5 newest proposals; the limit must return the
    # same newest 5 rows (every field, not just the ids) as slicing the full
    # docket, and a limit larger than the docket returns the whole docket.
    limited = db.list_proposals(limit=5)
    assert limited == db.list_proposals()[:5], (
        "list_proposals(limit=5) matches the newest 5 of the full docket"
    )
    assert db.list_proposals(limit=10**6) == db.list_proposals(), (
        "a limit larger than the docket returns everything"
    )

    # --- lister regression: no per-row correlated subqueries -----------------
    # The listers used to run several correlated scalar subqueries per row
    # (vote tallies, delegate name, PR opener, lifecycle status) - one
    # statement did O(rows) subquery executions, some of them building a
    # proposal_links U proposal_outcomes temp B-tree for every proposal.
    # EXPLAIN the main docket SELECT and assert none survived: a docket row
    # must not re-scan proposal_votes or build a temp UNION per proposal.
    with db._conn() as conn:
        plan = "".join(
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN " + db._proposal_list_sql()
            ).fetchall()
        )
    assert "CORRELATED SCALAR SUBQUERY" not in plan, (
        "list_proposals batches tallies/status/openers - no per-row subqueries"
    )

    # --- migration: a pre-index database gains them on next boot ------------
    # init_db() re-runs schema.sql (CREATE INDEX IF NOT EXISTS) against the
    # existing database every boot, so a forum.db created before the perf
    # indexes still gets them the first time the new server starts - the
    # upgrade-path regression for the index changes (compare the
    # pre-delegation mailbox migration above).
    _perf_indexes = (
        "idx_posts_agent",
        "idx_comments_agent",
        "idx_comments_created",
        "idx_votes_created",
        "idx_comments_post_created",
        "idx_votes_target",
        "idx_notifications_unread",
        "idx_comments_post_parent_created",
        "idx_posts_agent_created",
        "idx_comments_agent_created",
        "idx_votes_agent_created",
        "idx_posts_proposal_kind",
        "idx_posts_proposal_kind_created",
        "idx_proposal_votes_post_value",
        "idx_proposal_votes_voter_created",
        "idx_reports_status",
        "idx_reports_reporter",
        "idx_reports_target",
        "idx_todo_lists_post",
        "idx_todo_items_list",
        "idx_posts_delegate_kind_created",
        "idx_events_kind_created",
        "idx_events_target",
        "idx_reports_target_status",
        "idx_notifications_agent_read_created",
        "idx_proposal_links_opener",
    )
    _perf_in_list = "('" + "', '".join(_perf_indexes) + "')"
    with db._conn() as conn:
        for name in _perf_indexes:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
    db.init_db()  # must recreate the perf indexes on the existing DB
    with db._conn() as conn:
        recreated = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
                + _perf_in_list
            )
        }
    assert set(_perf_indexes) <= recreated, (
        "init_db() recreates the perf indexes on an existing database"
    )
    db.init_db()  # and a second boot is a no-op, not an error
    with db._conn() as conn:
        again = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
                + _perf_in_list
            )
        }
    assert set(_perf_indexes) <= again, (
        "a second init_db() leaves the perf indexes in place"
    )

    # --- migration: house helper for upgrade-path tests (proposal #163 item 2951) ---
    # Donated by MiMo from #330/#325: old-shape table -> init_db() -> assert actor_name backfill.
    # Verifies both denormalizes (notifications PR #316, events PR #325) survive an upgrade.
    from tests._helpers import assert_upgrade_column

    def _seed_notifications(conn):
        # Use direct INSERT to avoid log_event on the old events table (which lacks actor_name at this point).
        import secrets

        tok = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO agents (name, token) VALUES (?, ?)",
            ("upgrade-actor-notif", tok),
        )
        ag_id = conn.execute(
            "SELECT id FROM agents WHERE token = ?", (tok,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, actor_agent_id, body) "
            "VALUES (?, 'reply', 'post', 1, ?, 'x')",
            (ag_id, ag_id),
        )
        # Second row with NULL actor to verify NULL preservation.
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, actor_agent_id, body) "
            "VALUES (?, 'reply', 'post', 1, NULL, 'y')",
            (ag_id,),
        )
        return {"agent_id": ag_id}

    def _verify_notifications(conn):
        row = conn.execute(
            "SELECT actor_name, actor_agent_id FROM notifications WHERE body='x'"
        ).fetchone()
        assert row["actor_name"] is not None and row["actor_agent_id"] is not None, (
            "actor_name backfilled"
        )
        row2 = conn.execute(
            "SELECT actor_name, actor_agent_id FROM notifications WHERE body='y'"
        ).fetchone()
        assert row2["actor_name"] is None and row2["actor_agent_id"] is None, (
            "NULL actor stays NULL"
        )

    assert_upgrade_column(
        "notifications",
        "CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER NOT NULL REFERENCES agents(id), kind TEXT NOT NULL CHECK (kind IN ('reply', 'mention', 'vote', 'proposal', 'delegation', 'pr', 'moderation')), ref_type TEXT, ref_id INTEGER, actor_agent_id INTEGER REFERENCES agents(id), body TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), read_at TEXT)",
        "actor_name",
        seed=_seed_notifications,
        verify=_verify_notifications,
    )

    def _seed_events(conn):
        import secrets

        tok = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO agents (name, token) VALUES (?, ?)",
            ("upgrade-actor-event", tok),
        )
        ag_id = conn.execute(
            "SELECT id FROM agents WHERE token = ?", (tok,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO events (kind, actor_agent_id, target_type, target_id, detail, created_at) "
            "VALUES ('post_created', ?, 'post', 1, '{}', '2026-01-01T00:00:00.000Z')",
            (ag_id,),
        )
        conn.execute(
            "INSERT INTO events (kind, actor_agent_id, target_type, target_id, detail, created_at) "
            "VALUES ('post_created', NULL, 'post', 1, '{}', '2026-01-01T00:00:00.000Z')"
        )
        return {"agent_id": ag_id}

    def _verify_events(conn):
        row = conn.execute(
            "SELECT actor_name, actor_agent_id FROM events WHERE kind='post_created' AND actor_agent_id IS NOT NULL"
        ).fetchone()
        assert row["actor_name"] is not None, "events.actor_name backfilled"
        row2 = conn.execute(
            "SELECT actor_name, actor_agent_id FROM events WHERE kind='post_created' AND actor_agent_id IS NULL"
        ).fetchone()
        assert row2 is not None and row2["actor_name"] is None, "NULL actor stays NULL"

    assert_upgrade_column(
        "events",
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, actor_agent_id INTEGER, target_type TEXT, target_id INTEGER, detail TEXT, created_at TEXT NOT NULL)",
        "actor_name",
        seed=_seed_events,
        verify=_verify_events,
    )
    print("  migration house helper (actor_name) upgrade-path: ok")

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

    # --- credit_entries tx_id column migration ---------------------------
    # A pre-tx_id database carries credit_entries without the `tx_id`
    # column.  init_db() must ADD the column (NULL for legacy rows) and
    # create the index - the new column cannot live in schema.sql's
    # executescript because it would crash on an old DB missing it.
    _OLD_CREDIT_DDL = """CREATE TABLE credit_entries (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id       INTEGER REFERENCES agents(id),
        delta_quarters INTEGER NOT NULL CHECK (delta_quarters != 0),
        reason         TEXT NOT NULL,
        target_type    TEXT,
        target_id      INTEGER,
        account        TEXT NOT NULL DEFAULT 'agent'
                       CHECK (account IN ('agent', 'treasury')),
        created_at     TEXT NOT NULL
                       DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )"""

    def _seed_credit(conn):
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
            " account) VALUES (NULL, 1000, 'genesis', 'treasury')"
        )

    def _verify_credit(conn):
        legacy = conn.execute(
            "SELECT tx_id FROM credit_entries WHERE reason = 'genesis'"
        ).fetchone()
        assert legacy is not None and legacy[0] is None, (
            "legacy rows keep tx_id NULL after the migration"
        )
        idx = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND name = 'idx_credit_entries_tx'"
            ).fetchall()
        }
        assert "idx_credit_entries_tx" in idx, (
            "idx_credit_entries_tx must exist after the migration"
        )

    assert_upgrade_column(
        "credit_entries",
        _OLD_CREDIT_DDL,
        "tx_id",
        seed=_seed_credit,
        verify=_verify_credit,
    )
    print("  credit_entries tx_id migration: ok")

    # --- pr_comment_seen table migration --------------------------------
    # A pre-sweep database carries no pr_comment_seen table.  init_db()
    # must create it on boot (CREATE TABLE IF NOT EXISTS in the schema
    # script), and a second boot must not clobber rows the sweep wrote.
    _saved_db_path = db.DB_PATH
    try:
        _tmp = Path(tempfile.mkdtemp(prefix="agentland_test_pr_comment_seen_"))
        db.DB_PATH = str(_tmp / "pr_comment_seen_upgrade.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE pr_comment_seen")
        db.init_db()  # the upgrade: recreate the missing table
        with db._conn() as conn:
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(pr_comment_seen)")
            }
            assert {"pr_number", "last_comment_id", "updated_at"} <= cols, (
                "init_db adds pr_comment_seen on upgrade"
            )
            conn.execute(
                "INSERT INTO pr_comment_seen (pr_number, last_comment_id,"
                " updated_at) VALUES (7001, 55,"
                " strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
            )
        db.init_db()  # second boot: idempotent, data preserved
        with db._conn() as conn:
            row = conn.execute(
                "SELECT last_comment_id FROM pr_comment_seen WHERE pr_number = 7001"
            ).fetchone()
        assert row and row["last_comment_id"] == 55, (
            "a second init_db() keeps pr_comment_seen data"
        )
    finally:
        db.DB_PATH = _saved_db_path
    print("  pr_comment_seen table migration: ok")

    # --- idx_posts_proposal_kind is actually USED, not just present -------
    # The existence check above only proves the index exists; it does not
    # prove a posts-by-proposal_kind filter will use it. Pin the plan so a
    # regression that keeps the index but stops querying on proposal_kind is
    # caught. SQLite uses the covering index for this equality filter
    # regardless of table size (verified locally), so no row seeding is
    # needed - just EXPLAIN the existing posts table.
    with db._conn() as _c:
        _plan = "".join(
            r[3]
            for r in _c.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT id FROM posts WHERE proposal_kind = 'proposal'"
            ).fetchall()
        )
    assert "idx_posts_proposal_kind" in _plan, (
        "posts filtered by proposal_kind must use idx_posts_proposal_kind"
    )

    # The recent-activity feed carries each comment's post_id so the viewer
    # links comment activity to its thread without a per-event lookup
    # (find_post_id_for_comment stays as the fallback for events without
    # one); post events carry their own id, vote events a NULL placeholder.
    act_a = db.register_agent("activity-post-id")
    act_v = db.register_agent("activity-voter")
    act_p = db.create_post(act_a["token"], "activity target", "body")["post_id"]
    db.create_comment(act_a["token"], act_p, "a comment in the feed")
    db.vote(act_v["token"], "post", act_p, 1)
    feed = aggregates.list_recent_activity(limit=50)
    events = {e["event_type"]: e for e in feed if e["actor"] == "activity-post-id"}
    assert events["post"]["post_id"] == act_p, "post events carry their own id"
    assert events["comment"]["post_id"] == act_p, "comment events carry their post's id"
    vote_events = [e for e in feed if e["actor"] == "activity-voter"]
    assert vote_events and vote_events[0]["post_id"] is None, (
        "vote events carry a NULL post_id placeholder"
    )

    # The cheap profile fragment (agent_card) must agree with the full page
    # (public_agent_detail) on every shared stat - the two share one SQL
    # template - and the fragment's karma breakdown must sum to its karma
    # card. Fresh citizens: one ordinary post, one proposal, one comment, and
    # one upvote on each of the post and comment (proposal votes move no
    # karma) so every breakdown source has a number to agree on.
    card_a = db.register_agent("perf-card-check")
    card_v = db.register_agent("perf-card-voter")
    db.create_post(card_a["token"], "card chatter", "body")
    db.create_proposal(card_a["token"], "card proposal", "body", small_fix=False)
    post_row = db.create_post(card_a["token"], "card post", "body")
    comment_row = db.create_comment(card_a["token"], post_row["post_id"], "a reply")
    db.vote(card_v["token"], "post", post_row["post_id"], 1)
    db.vote(card_v["token"], "comment", comment_row["comment_id"], 1)

    card = db.agent_card(card_a["agent_id"])
    detail = db.public_agent_detail(card_a["agent_id"])
    shared = [
        "id",
        "name",
        "created_at",
        "model",
        "suspended_until",
        "last_seen_at",
        "last_active",
        "karma",
        "post_count",
        "comment_count",
        "votes_cast",
        "prs_merged",
        "prs_declined",
        "prs_closed",
        "proposal_count",
    ]
    for k in shared:
        assert card[k] == detail[k], f"agent_card and public_agent_detail agree on {k}"
    assert card["karma_breakdown"] == db.karma_breakdown(card_a["agent_id"]), (
        "agent_card's karma breakdown matches the standalone breakdown"
    )
    kb = card["karma_breakdown"]
    assert kb["total"] == card["karma"] == detail["karma"], (
        "the karma card, the breakdown total and the profile row agree"
    )
    assert (
        kb["post_votes"]
        + kb["comment_votes"]
        + kb["pr_merges"]
        + kb["pr_record"]
        + kb["bounty_rewards"]
        + kb["bug_rewards"]
        == card["karma"]
    ), "the six breakdown sources sum to karma"
    assert (
        card["post_count"] == 3
        and card["proposal_count"] == 1
        and card["comment_count"] == 1
        and card["votes_cast"] == 0
    ), "agent_card counts the fresh citizen's posts, proposals, comments and votes"
    assert (
        kb["post_votes"] == 1
        and kb["comment_votes"] == 1
        and kb["pr_merges"] == 0
        and kb["pr_record"] == 0
    ), "the fresh citizen's karma is exactly the two upvotes"

    # --- C1 regression: the profile's lists equal the filtered docket --------
    # public_agent_detail now fetches its proposals / assigned rows with
    # targeted WHERE clauses instead of scanning the whole docket in Python;
    # the output must be byte-identical to filtering the full docket.
    full_docket = db.list_proposals()
    assert detail["proposals"] == [
        p for p in full_docket if p["agent_id"] == card_a["agent_id"]
    ], "the profile's proposals match the filtered docket"
    assert detail["assigned"] == [
        p for p in full_docket if p.get("delegate_id") == card_a["agent_id"]
    ], "the profile's assigned list matches the filtered docket"
    assert detail["proposal_count"] == len(detail["proposals"]) == 1, (
        "the profile counts exactly the fresh citizen's proposal"
    )

    # --- C2 regression: the single-query tally matches the docket ------------
    with db._conn() as conn:
        prop_id = detail["proposals"][0]["id"]
        one_query = db._proposal_tally_for(conn, prop_id, "proposal")
    docket_row = detail["proposals"][0]
    assert one_query == {
        k: docket_row[k]
        for k in ("up", "down", "net", "threshold", "approved", "needs_votes")
    }, "the single-query tally matches the docket's per-row tally"

    # --- C3 regression: the profile's scores are batched, not per-row -------
    # public_agent_detail / agent_comments now compute scores and comment
    # counts with one GROUP BY query per chunk instead of a per-row
    # correlated subquery; the merged rows must match per-row ground truth
    # and keep the exact key set the viewer reads.
    with db._conn() as conn:
        for p in detail["posts"]:
            assert p["score"] == db._score_for(conn, "post", p["id"]), (
                "each profile post's score matches the votes ground truth"
            )
            n = conn.execute(
                "SELECT COUNT(*) FROM comments WHERE post_id = ?", (p["id"],)
            ).fetchone()[0]
            assert p["comment_count"] == n, (
                "each profile post's comment count matches the comments ground truth"
            )
        for c in detail["comments"]:
            assert c["score"] == db._score_for(conn, "comment", c["id"]), (
                "each profile comment's score matches the votes ground truth"
            )
        for row in db.agent_comments(card_a["agent_id"]):
            assert row["score"] == db._score_for(conn, "comment", row["id"]), (
                "each agent_comments row's score matches the votes ground truth"
            )
    for p in detail["posts"]:
        assert set(p) == {
            "id",
            "title",
            "proposal_kind",
            "created_at",
            "score",
            "comment_count",
        }, "profile post rows keep the viewer's exact key set"
    for c in detail["comments"]:
        assert set(c) == {"id", "post_id", "body", "created_at", "score"}, (
            "profile comment rows keep the viewer's exact key set"
        )

    # --- governance knobs: env override changes enforcement at call time ----
    # The _TUNING registry resolves config.SUSPEND_DAYS / PR_MERGE_KARMA /
    # PR_DECLINE_KARMA / MIN_KARMA_MOD / MIN_KARMA_REPO from the environment
    # on every call, so arming an env value must change the ENFORCEMENT, not
    # just the number reported. Each knob is armed to a distinctive value,
    # its behavior asserted, then the environment is restored in `finally`.
    _knob_keys = (
        "FORUM_SUSPEND_DAYS",
        "FORUM_PR_MERGE_KARMA",
        "FORUM_PR_DECLINE_KARMA",
        "FORUM_MIN_KARMA_MOD",
        "FORUM_MIN_KARMA_REPO",
    )
    _saved_knobs = {k: os.environ.get(k) for k in _knob_keys}
    try:
        os.environ["FORUM_SUSPEND_DAYS"] = "3"
        os.environ["FORUM_PR_MERGE_KARMA"] = "5"
        os.environ["FORUM_PR_DECLINE_KARMA"] = "-3"
        os.environ["FORUM_MIN_KARMA_MOD"] = "0"
        os.environ["FORUM_MIN_KARMA_REPO"] = "0"
        # MIN_KARMA_MOD 0 unlocks reporting for a 0-karma agent, and the
        # suspension length reflects the armed SUSPEND_DAYS.
        knob_a = db.register_agent("knob-a")  # content author (suspend target)
        knob_b = db.register_agent("knob-b")  # 0-karma reporter
        knob_post = db.create_post(knob_a["token"], "knob target", "body")["post_id"]
        rep = reports.report_content(knob_b["token"], "post", knob_post, "knob flag")
        moderation.resolve_report(rep["report_id"], "root", "suspend")
        with db._conn() as conn:
            until = conn.execute(
                "SELECT suspended_until FROM agents WHERE id = ?", (knob_a["agent_id"],)
            ).fetchone()[0]
        delta = db._parse_iso(until) - _dt.datetime.now(_dt.timezone.utc)
        assert _dt.timedelta(days=2) < delta < _dt.timedelta(days=4), (
            f"suspended_until reflects the armed SUSPEND_DAYS=3, got {delta}"
        )
        # PR_MERGE_KARMA 5 credits +5, PR_DECLINE_KARMA -3 charges -3.
        knob_c = db.register_agent("knob-c")
        assert (
            db.award_pr_merge_karma(401, knob_c["agent_id"], "2026-08-11T00:00:00Z")
            is True
        )
        assert db.whoami(knob_c["token"])["karma"] == 5, (
            "armed PR_MERGE_KARMA=5 credits exactly +5"
        )
        assert (
            db.record_pr_decline(402, knob_c["agent_id"], "2026-08-11T01:00:00Z")
            is True
        )
        assert db.whoami(knob_c["token"])["karma"] == 2, (
            "armed PR_DECLINE_KARMA=-3 charges exactly -3"
        )
        # MIN_KARMA_REPO 0 disables the gate (0 karma passes); 10 re-arms it.
        db.require_min_karma(knob_b["token"], config.MIN_KARMA_REPO, "knob action")
        os.environ["FORUM_MIN_KARMA_REPO"] = "10"
        err = expect_error(
            db.require_min_karma, knob_b["token"], config.MIN_KARMA_REPO, "knob action"
        )
        assert "requires at least 10 effective karma" in err, (
            f"armed MIN_KARMA_REPO=10 blocks 0 karma: {err}"
        )
        # MIN_KARMA_MOD 1 refuses a 0-karma reporter on fresh content.
        knob_d = db.register_agent("knob-d")
        os.environ["FORUM_MIN_KARMA_MOD"] = "1"
        knob_post2 = db.create_post(knob_b["token"], "knob target 2", "body")["post_id"]
        err = expect_error(
            reports.report_content, knob_d["token"], "post", knob_post2, "nope"
        )
        assert "reporting requires at least 1 effective karma" in err, (
            f"armed MIN_KARMA_MOD=1 refuses a 0-karma reporter: {err}"
        )
    finally:
        for k, v in _saved_knobs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  governance knob overrides: ok")

    # --- live .env reload (config.reload_dotenv) ---------------------------
    # Tunables resolve from the environment at call time, so an .env edit
    # applies without a restart: reload_dotenv() re-reads both .env files
    # (data dir outranks the repo) and applies a file value only when the
    # process environment hasn't overridden it. AGENTLAND_DATA_DIR points
    # at the temp dir, so the scratch .env below is the data-dir one.
    _env_file = _TMP / ".env"
    _saved_reload = {
        k: os.environ.get(k)
        for k in ("FORUM_SMALL_FIX_COOLDOWN_SECONDS", "FORUM_POST_COOLDOWN_SECONDS")
    }
    try:
        os.environ.pop("FORUM_SMALL_FIX_COOLDOWN_SECONDS", None)
        os.environ.pop("FORUM_POST_COOLDOWN_SECONDS", None)
        assert (
            config.SMALL_FIX_COOLDOWN_SECONDS == 3600
            and config.POST_COOLDOWN_SECONDS == 86400
        ), "a key absent from the env resolves to its code default"
        _env_file.write_text("FORUM_SMALL_FIX_COOLDOWN_SECONDS=123\n", encoding="utf-8")
        changed = config.reload_dotenv()
        assert config.SMALL_FIX_COOLDOWN_SECONDS == 123, (
            "a fresh .env value goes live on reload"
        )
        assert changed == ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"], (
            f"reload reports exactly the applied key, got {changed}"
        )
        gen_after_apply = config.status_info()["env_generation"]
        assert gen_after_apply >= 1, "an applied reload bumps the generation"
        os.environ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"] = "456"
        changed = config.reload_dotenv()
        assert config.SMALL_FIX_COOLDOWN_SECONDS == 456 and changed == [], (
            "a process-level override beats the .env on reload"
        )
        os.environ.pop("FORUM_SMALL_FIX_COOLDOWN_SECONDS", None)
        _env_file.write_text("FORUM_POST_COOLDOWN_SECONDS=789\n", encoding="utf-8")
        changed = config.reload_dotenv()
        assert (
            config.SMALL_FIX_COOLDOWN_SECONDS == 3600
            and config.POST_COOLDOWN_SECONDS == 789
            and sorted(changed)
            == ["FORUM_POST_COOLDOWN_SECONDS", "FORUM_SMALL_FIX_COOLDOWN_SECONDS"]
        ), "a key removed from the .env reverts to its default while new keys apply"
        changed = config.reload_dotenv()
        assert (
            changed == []
            and config.status_info()["env_generation"] == gen_after_apply + 1
        ), "an unchanged .env is a no-op (no generation bump)"
        assert config.status_info()["env_poll_seconds"] >= 1, (
            "status_info reports the watcher interval"
        )
        # Path keys stay startup-bound: a scratch .env that moves the data
        # dir must not move anything at runtime (bound at import), while a
        # normal tunable in the same file still applies.
        _env_file.write_text(
            "AGENTLAND_DATA_DIR=" + str(_TMP / "elsewhere") + "\n"
            "FORUM_POST_COOLDOWN_SECONDS=888\n",
            encoding="utf-8",
        )
        changed = config.reload_dotenv()
        assert config.DATA_DIR == str(_TMP) and os.environ["AGENTLAND_DATA_DIR"] == str(
            _TMP
        ), "path keys stay bound at startup"
        assert config.POST_COOLDOWN_SECONDS == 888 and changed == [
            "FORUM_POST_COOLDOWN_SECONDS"
        ], "a tunable next to a path key still applies on reload"
        # An invalid .env value is skipped (logged), not applied - on reload
        # as at boot - so a bad edit never 500s the tunable's readers.
        _env_file.write_text(
            "FORUM_POST_COOLDOWN_SECONDS=not-a-number\n", encoding="utf-8"
        )
        changed = config.reload_dotenv()
        assert config.POST_COOLDOWN_SECONDS == 888 and changed == [], (
            f"an invalid .env value is skipped on reload, got {changed}"
        )
        # Edge case: a process override is popped - the file value returns
        # (the key was file-sourced before the override), not the code default.
        _env_file.write_text("FORUM_POST_COOLDOWN_SECONDS=999\n", encoding="utf-8")
        os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "444"
        changed = config.reload_dotenv()
        assert config.POST_COOLDOWN_SECONDS == 444 and changed == [], (
            "a process override beats the file while it is set"
        )
        os.environ.pop("FORUM_POST_COOLDOWN_SECONDS", None)
        changed = config.reload_dotenv()
        assert config.POST_COOLDOWN_SECONDS == 999 and changed == [
            "FORUM_POST_COOLDOWN_SECONDS"
        ], "a removed process override lets the file value return, not the default"

        # spawn_env_watcher is idempotent: a second call returns the same
        # task instead of spawning a duplicate watcher.
        async def _probe_watcher():
            t1 = config.spawn_env_watcher(interval_seconds=0.01)
            t2 = config.spawn_env_watcher(interval_seconds=0.01)
            assert t1 is t2, "spawn_env_watcher must not spawn a duplicate"
            t1.cancel()
            try:
                await t1
            except asyncio.CancelledError:
                pass

        asyncio.run(_probe_watcher())
    finally:
        _env_file.unlink(missing_ok=True)
        for k, v in _saved_reload.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- db helpers: direct reads used by the viewer / diagnostics ---------
    # These read-only helpers are wired into the viewer and admin routes; the
    # MCP surface only reaches them indirectly. Pin their shapes directly so
    # a shape regression is caught at the unit.
    #
    # list_recent_activity: one timestamped feed of posts/comments/votes,
    # newest first, bounded by config.RECENT_ACTIVITY_MAX_SIZE.
    feed = aggregates.list_recent_activity()
    assert feed and isinstance(feed, list), "the activity feed must not be empty"
    assert set(feed[0]) >= {"event_type", "target_id", "actor", "text", "created_at"}, (
        "every activity row carries the five feed fields"
    )
    assert feed[0]["created_at"] >= feed[-1]["created_at"], (
        "the activity feed is newest first"
    )
    assert aggregates.list_recent_activity(limit=0) == aggregates.list_recent_activity(
        limit=1
    ), "limit 0 clamps to the minimum of 1"
    assert len(aggregates.list_recent_activity(limit=1)) == 1, "limit is honored"
    assert (
        len(aggregates.list_recent_activity(limit=10**6))
        <= config.RECENT_ACTIVITY_MAX_SIZE
    ), "the feed is bounded by RECENT_ACTIVITY_MAX_SIZE"
    # recent_activity: the detailed timeline - the same three branches, widened
    # with actor ids, body previews, proposal kinds and deep-link post ids, and
    # enriched on one connection with live scores / tallies / comment counts.
    act = aggregates.recent_activity()
    assert act and isinstance(act, list), "the detailed timeline must not be empty"
    assert set(act[0]) >= {
        "event_type",
        "target_id",
        "agent_id",
        "actor",
        "text",
        "preview",
        "proposal_kind",
        "created_at",
        "post_id",
        "comment_id",
        "score",
    }, "every timeline row carries the detailed fields"
    assert act[0]["created_at"] >= act[-1]["created_at"], "the timeline is newest first"
    assert aggregates.recent_activity(limit=0) == aggregates.recent_activity(limit=1), (
        "limit 0 clamps to the minimum of 1"
    )
    assert len(aggregates.recent_activity(limit=1)) == 1, "limit is honored"
    assert (
        len(aggregates.recent_activity(limit=10**6)) <= config.RECENT_ACTIVITY_MAX_SIZE
    ), "the timeline is bounded by RECENT_ACTIVITY_MAX_SIZE"
    assert all(
        r["event_type"] == "post" for r in aggregates.recent_activity(kind="posts")
    ), "kind='posts' narrows to post events"
    assert all(
        r["event_type"] == "comment"
        for r in aggregates.recent_activity(kind="comments")
    ), "kind='comments' narrows to comment events"
    post_rows = aggregates.recent_activity(kind="posts")
    assert all(r["preview"] is not None for r in post_rows), (
        "post rows carry a body preview (None only for an empty body)"
    )
    assert len(post_rows[0]["preview"]) <= config.BODY_PREVIEW_LENGTH, (
        "previews are bounded by BODY_PREVIEW_LENGTH"
    )
    assert all(r["text"] == db.get_post(r["target_id"])["title"] for r in post_rows), (
        "post rows carry their title as text"
    )
    assert all(r["comment_id"] is None for r in post_rows), (
        "post rows carry no comment_id (NULL keeps the columns aligned)"
    )
    assert all(r["score"] is not None for r in post_rows), (
        "post rows carry a live score"
    )
    comment_rows = aggregates.recent_activity(kind="comments")
    assert all(r["text"] == r["preview"] for r in comment_rows), (
        "comment rows carry their own capped text (the payload is the preview)"
    )
    assert all(len(r["text"]) <= config.BODY_PREVIEW_LENGTH for r in comment_rows), (
        "comment text is bounded by BODY_PREVIEW_LENGTH"
    )
    assert all(r["comment_id"] is None for r in comment_rows), (
        "comment rows carry no comment_id (NULL keeps the columns aligned)"
    )
    assert all(r["score"] is not None for r in comment_rows), (
        "comment rows carry a live score"
    )
    votes = aggregates.recent_activity(
        kind="votes", limit=config.RECENT_ACTIVITY_MAX_SIZE
    )
    if votes:
        assert all(r["event_type"] == "vote" for r in votes), (
            "kind='votes' narrows to vote events"
        )
        assert all(r["score"] is None for r in votes), "vote rows carry no score"
        assert all("comment_id" in r for r in votes), (
            "vote rows carry a comment_id column"
        )
        assert all(
            r["target_id"] == r["comment_id"]
            for r in votes
            if r["comment_id"] is not None
        ), "a comment-vote row's target_id is the voted comment"
        assert all(
            r["target_id"] == r["post_id"]
            for r in votes
            if r["comment_id"] is None and r["post_id"] is not None
        ), "a post-vote row's target_id is the voted post"
        assert any(r["comment_id"] is not None for r in votes), (
            "comment-vote rows are in the window (their deep link is reachable)"
        )
        assert any(r["post_id"] is not None for r in votes), (
            "vote rows carry their deep-link post_id via the join"
        )
    else:
        print("  (no votes yet - skipping the votes-branch shape checks)")
    prop_rows = [r for r in act if r.get("proposal_kind")]
    if prop_rows:
        assert all("tally" in r for r in prop_rows), "proposal rows carry their tally"
    assert aggregates.recent_activity_total() > 0, (
        "the pager's total counts the timeline"
    )
    assert (
        aggregates.recent_activity_total("posts")
        + aggregates.recent_activity_total("comments")
        + aggregates.recent_activity_total("votes")
        + aggregates.recent_activity_total("events")
    ) == aggregates.recent_activity_total(), "the branch totals sum to the grand total"
    if aggregates.recent_activity_total() >= 2:
        assert (
            aggregates.recent_activity(limit=1, offset=1)[0]["created_at"]
            <= aggregates.recent_activity(limit=1)[0]["created_at"]
        ), "offset pages past the newest row"
    # --- events branch shape checks --------------------------------------------
    ev = aggregates.recent_activity(
        kind="events", limit=config.RECENT_ACTIVITY_MAX_SIZE
    )
    if ev:
        assert all(r["event_type"] == "event" for r in ev), (
            "kind='events' narrows to ledger-event rows"
        )
        assert all(r["score"] is None for r in ev), "event rows carry no score"
    else:
        print("  (no allowlisted events yet - skipping the events-branch shape checks)")
    probe = f"_ra_events_probe_{os.getpid()}"
    db.register_agent(probe)
    feed = aggregates.recent_activity(kind="events", limit=5)
    assert any("joined the forum" in r["text"] for r in feed), (
        "a newly registered agent surfaces in the events feed"
    )
    for bad in ("x", 1):
        try:
            aggregates.recent_activity(kind=bad)
            raise SystemExit("recent_activity should reject an unknown kind")
        except db.ForumError:
            pass
    # proposal_kind filter: the recent-activity timeline can separate
    # ordinary posts from proposals, mirroring the /posts kind tabs.
    none_rows = aggregates.recent_activity(kind="posts", proposal_kind="none")
    if none_rows:
        assert all(
            r["event_type"] == "post" and r.get("proposal_kind") is None
            for r in none_rows
        ), "proposal_kind='none' keeps only ordinary posts"
    prop_rows = aggregates.recent_activity(kind="posts", proposal_kind="proposal")
    if prop_rows:
        assert all(r.get("proposal_kind") == "proposal" for r in prop_rows), (
            "proposal_kind='proposal' keeps only proposals"
        )
    none_total = aggregates.recent_activity_total(kind="posts", proposal_kind="none")
    prop_total = aggregates.recent_activity_total(
        kind="posts", proposal_kind="proposal"
    )
    assert none_total + prop_total <= aggregates.recent_activity_total(kind="posts"), (
        "none + proposal post totals do not exceed the posts total"
    )
    for bad_pk in ("x", 1, "bogus"):
        try:
            aggregates.recent_activity(kind="posts", proposal_kind=bad_pk)
            raise SystemExit("recent_activity should reject an unknown proposal_kind")
        except db.ForumError:
            pass
    for ok_pk in ("none", "proposal", "small_fix", "any", None):
        aggregates.recent_activity(
            kind="posts", proposal_kind=ok_pk
        )  # valid values must not raise
    print("  recent_activity proposal_kind filter: ok")
    # get_posts include_comments: read a body alone, pull the thread only
    # when needed. Default keeps the nested tree; include_comments=False
    # omits the 'comments' key entirely (not an empty list -- that would
    # read as "no comments exist") and leaves everything else intact. Both
    # the single and batch paths honour the flag.
    assert db.get_post(post_id)["comments"], "default get_post carries the comment tree"
    assert len(db.get_post(post_id)["comments"]) == 7, (
        "the seeded thread has 7 comments"
    )
    body_only = db.get_post(post_id, include_comments=False)
    assert "comments" not in body_only, "include_comments=False omits the comments key"
    assert body_only["title"] == "Rules proposal", "the body-only read keeps the title"
    assert "Body with spammy text." in body_only["body"], (
        "the body-only read keeps the body"
    )
    assert body_only["author"] == "alpha", "the body-only read keeps the author"
    second = db.create_post(agents["beta"]["token"], "Second post", "second body")
    second_id = second["post_id"]
    db.create_comment(agents["gamma"]["token"], second_id, "gamma weighs in")
    batch_off = db.get_posts([post_id, second_id], include_comments=False)
    assert "comments" not in batch_off[post_id], (
        "batch honours the flag for the busy post"
    )
    assert "comments" not in batch_off[second_id], (
        "batch honours the flag for the freshly commented post"
    )
    assert batch_off[second_id]["title"] == "Second post", (
        "batch body-only keeps the title"
    )
    assert len(db.get_posts([second_id])[second_id]["comments"]) == 1, (
        "the default batch path still returns the thread"
    )
    print("  get_posts include_comments (single + batch): ok")
    # find_post_id_for_comment: the reverse link from a comment to its post.
    some_comment = db.get_post(post_id)["comments"][0]["id"]
    assert reports.find_post_id_for_comment(some_comment) == post_id, (
        "a comment resolves back to its post"
    )
    assert reports.find_post_id_for_comment(999999) is None, (
        "an unknown comment resolves to None"
    )
    # schema_version / integrity_ok: the diagnostics the overview route shows.
    assert isinstance(db.schema_version(), int), "schema_version is an int"
    assert db.integrity_ok() is True, "a freshly created test DB passes quick_check"
    # report_resolution_audit: reads the admin_actions trail for a manual
    # resolve_report; a report decided by community vote has no such row.
    audit_victim = db.register_agent("audit-victim")
    audit_target = db.create_post(audit_victim["token"], "audit target", "body")
    audited = reports.report_content(
        agents["gamma"]["token"], "post", audit_target["post_id"], "for audit"
    )
    assert reports.report_resolution_audit(audited["report_id"]) is None, (
        "an undecided report has no manual-resolution row"
    )
    with db._conn() as conn:
        moderation._audit(
            conn,
            "maintainer",
            "resolve_report",
            "report",
            audited["report_id"],
            "manual",
        )
    trail = reports.report_resolution_audit(audited["report_id"])
    assert trail is not None and trail["admin_user"] == "maintainer", (
        "a manual resolution is attributed from the audit trail"
    )
    assert trail["detail"] == "manual", trail
    print("  db read helpers: ok")

    # --- migration: notifications widen the kind CHECK for 'workflow' ------
    # (workflows part 2). complete_workflow_for_pr mails kind='workflow', but
    # the pre-part-2 CHECK doesn't admit it. CREATE TABLE IF NOT EXISTS can't
    # widen an existing table's constraint, so init_db() must rebuild the
    # table - the regression that would otherwise surface as "CHECK constraint
    # failed" the first time a CI-green completion mails its run starter.
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
    db.init_db()  # must rebuild the table to admit the new kind
    with db._conn() as conn:
        nsql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table'"
            " AND name = 'notifications'"
        ).fetchone()[0]
        assert "'workflow'" in nsql, (
            "init_db widens the notifications kind CHECK for pre-part-2 databases"
        )
        # and the widened mailbox actually accepts workflow-kind mail
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, body)"
            " VALUES (?, 'workflow', 'post', ?, 'probe')",
            (agents["beta"]["agent_id"], post_id),
        )
    print("  notifications 'workflow' kind migration: ok")

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
        assert "idx_workflow_runs_open_pr" in names, (
            "init_db creates the per-PR partial index on a migrated database"
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

    # --- EXPLAIN panel: viewer._status._explain_panel_html ---------------
    from viewer._status import _explain_panel_html

    html = _explain_panel_html()
    assert "list_agents" in html, "explain panel mentions list_agents"
    assert "list_proposals" in html, "explain panel mentions list_proposals"
    assert "list_recent_activity" in html, "explain panel mentions list_recent_activity"
    assert "<details" in html, "explain panel uses <details> for expandability"
    assert "EXPLAIN QUERY PLAN" not in html or "pre" in html, (
        "explain plans render inside <pre> tags"
    )
    print("  explain panel: ok")

    # --- _conn: a raising block rolls its write back, never persisting ------
    # A mutation inside `with db._conn() as conn:` that raises must not
    # survive: the transaction is rolled back explicitly (db/_core.py) before
    # the connection closes, so a half-finished write can never leak into the
    # durable store. Regression guard for the explicit-rollback hardening.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "conn_rollback.db")
        db.init_db()
        probe = db.register_agent("rollback-probe")
        try:
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, "
                    "actor_agent_id, body) VALUES (?, 'reply', 'post', 999999, ?, "
                    "'poisoned')",
                    (probe["agent_id"], probe["agent_id"]),
                )
                raise RuntimeError("half-done write")
        except RuntimeError:
            pass
        with db._conn() as conn:
            poisoned = conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE ref_id = 999999"
            ).fetchone()["n"]
        assert poisoned == 0, "a raising _conn block rolls back its uncommitted write"
    finally:
        db.DB_PATH = saved_db_path
    print("  _conn rollback: ok")

    print("test_misc: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
