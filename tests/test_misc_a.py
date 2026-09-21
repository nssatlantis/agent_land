"""test_misc shard A: early migrations S1-S10 (notifications CHECK, escrow backfill,
actor_name, pr binding, tag attribution, mentions, quotes, timestamps, bug links,
proposal_kind/idea). Split of tests/test_misc.py; section bodies byte-verbatim."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_misc_a_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, notifications, search, setup  # noqa: E402


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

    # --- migration: escrow backfill on a tuple-factory boot conn (prod 09-07) --
    # init_db() opens its migration connection with plain sqlite3.connect
    # (no row_factory), and backfill_escrow_account runs on that same conn.
    # With a live job present the mapping reads crashed boot with
    # "TypeError: tuple indices must be integers" - every fresh test DB
    # boots jobless, so no suite ever tripped it. Reproduce with a raw
    # tuple conn plus one live official holding.
    esc_sponsor = db.register_agent("esc-boot-mig")
    db.create_job_official(
        "m",
        esc_sponsor["name"],
        "Standing role",
        "d",
        1.0,
        ["s"],
        kind="one_time",
        cycles=1,
    )
    with db._conn() as conn:
        conn.execute("DELETE FROM economy_meta WHERE key = 'escrow_account_live'")
    import sqlite3

    boot_conn = sqlite3.connect(os.environ["FORUM_DB_PATH"])
    try:
        assert boot_conn.row_factory is None, "mimics init_db's raw boot conn"
        db._economy.backfill_escrow_account(boot_conn)
        boot_conn.commit()
        assert boot_conn.row_factory is None, "factory restored for the caller"
    finally:
        boot_conn.close()
    with db._conn() as conn:
        held = conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE account = 'escrow'"
        ).fetchone()[0]
    assert held == 20, f"the live 1.0cr holding is present, got {held}u"
    again = db._economy.backfill_escrow_account()
    assert again["already_live"] is True and again["backfilled_units"] == 0, (
        "second run is a no-op"
    )

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
                    tag_id     INTEGER NOT NULL REFERENCES tags(id),
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
        assert version == 4, "a booted database lands on the latest user_version"
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
        assert version == 4, "the timestamp migration stamps PRAGMA user_version"
        db.init_db()  # idempotent: a second boot truncates nothing
        with db._conn() as conn:
            again = conn.execute(
                "SELECT last_seen_at FROM agents WHERE id = ?", (legacy["agent_id"],)
            ).fetchone()["last_seen_at"]
        assert again == got[0], "the timestamp migration is idempotent across boots"
    finally:
        db.DB_PATH = saved_db_path

    # --- migration: bug_report_links backfill (small_fix #444) ---
    # A pre-444 database has no bug_report_links table. init_db() must create
    # the table + index and backfill links for existing proposal bodies that
    # reference #B<id>. The backfill is behind user_version 4, so a DB at
    # version 3 with a raw proposal body must land on 4 with the link present.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "bug_links_migration.db")
        db.init_db()
        bug_agent = db.register_agent("buglinks-legacy")
        bug = db.file_bug_report(bug_agent["token"], "Bug links mig", "body", None)
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO posts (agent_id, title, body, proposal_kind) VALUES (?, ?, ?, 'proposal')",
                (bug_agent["agent_id"], "Fix bug links", f"Fixes #B{bug['id']}"),
            )
            conn.execute("DELETE FROM bug_report_links")
            conn.execute("PRAGMA user_version = 3")
        db.init_db()
        with db._conn() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 4, "bug links migration stamps version 4"
            has = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bug_report_links'"
            ).fetchone()
            assert has is not None, "bug_report_links table exists after migration"
            idx = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_bug_report_links_post'"
            ).fetchone()
            assert idx is not None, "bug_report_links index exists after migration"
        links = db.get_bug_report(bug["id"])["linked_proposals"]
        assert any(p["title"] == "Fix bug links" for p in links), (
            "backfill links pre-migration proposal bodies"
        )
        db.init_db()
        with db._conn() as conn:
            version2 = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version2 == 4, "bug links migration is idempotent"
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
            "idx_posts_created",
            "idx_posts_agent_created",
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

    print("test_misc_a: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
