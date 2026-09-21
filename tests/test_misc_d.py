"""test_misc shard D: S26-S35 (events/job/bundle3 indexes, tx_id, pr_comment_seen,
proposal_kind pin, activity feed, profile pins C1-C3). Split of tests/test_misc.py;
section bodies byte-verbatim. The S32-S35 card/detail dependency stays atomic."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_misc_d_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import aggregates, db, setup  # noqa: E402


def main():
    agents, post_id = setup()

    # --- migration: drop the legacy 3-col events index --------------------
    # schema.sql (PR #409) replaced idx_events_kind_target with the covering
    # idx_events_kind_target_created; CREATE INDEX IF NOT EXISTS cannot drop
    # the redundant index on an upgraded database, so init_db() must. Seed a
    # pre-#409 database carrying the legacy index and one boot must drop it.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "events_legacy_index_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute(
                "CREATE INDEX idx_events_kind_target"
                " ON events(kind, target_type, target_id)"
            )
        db.init_db()  # the upgrade: drop the legacy index
        with db._conn() as conn:
            names = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND name LIKE 'idx_events_%'"
                )
            }
        assert "idx_events_kind_target" not in names, (
            "init_db drops the redundant 3-col events index"
        )
        assert "idx_events_kind_target_created" not in names, (
            "the events index prune also stops declaring the old covering index"
        )
        assert "idx_events_kind_created_id" in names, (
            "init_db keeps the events query index"
        )
        # Idempotent second boot: the drop is a no-op on an already-clean DB.
        db.init_db()
    finally:
        db.DB_PATH = saved_db_path
    print("  events legacy-index drop migration: ok")

    # --- migration: events index prune (down to the lean 4-index set) ------
    # schema.sql stopped declaring idx_events_kind / idx_events_kind_created
    # / idx_events_kind_target_created (all redundant with the covering
    # idx_events_kind_created_id), but an upgraded database still carries them
    # until boot drops them. Seed all three and require one init_db() to
    # remove them while keeping the covering index.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "events_index_prune_migration.db")
        db.init_db()
        with db._conn() as conn:
            for create in (
                "CREATE INDEX idx_events_kind ON events(kind)",
                "CREATE INDEX idx_events_kind_created ON events(kind, created_at)",
                "CREATE INDEX idx_events_kind_target_created"
                " ON events(kind, target_type, target_id, created_at)",
            ):
                conn.execute(create)
        db.init_db()  # the upgrade: prune to the lean set
        with db._conn() as conn:
            names = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND name LIKE 'idx_events_%'"
                )
            }
        for dropped in (
            "idx_events_kind",
            "idx_events_kind_created",
            "idx_events_kind_target_created",
        ):
            assert dropped not in names, f"init_db drops {dropped}"
        assert "idx_events_kind_created_id" in names, (
            "init_db keeps the covering events index"
        )
        # Idempotent second boot: the drops are no-ops on an already-clean DB.
        db.init_db()
    finally:
        db.DB_PATH = saved_db_path
    print("  events index-prune migration: ok")

    # --- migration: job-anchor index add + target drop, offered_to add ----
    # A pre-bundle database carries the subsumed idx_events_target and
    # lacks idx_events_job_anchor and idx_jobs_offered_to. One boot must
    # add both and drop the redundant one; the anchor then serves a
    # target-first probe and the offered_to index serves the digest's
    # offered-jobs lookup.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "jobs_anchor_index_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_events_job_anchor")
            conn.execute("DROP INDEX IF EXISTS idx_jobs_offered_to")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_target"
                " ON events(target_type, target_id)"
            )
        db.init_db()  # the upgrade: add anchor + offered_to, drop target
        with db._conn() as conn:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        assert "idx_events_job_anchor" in names, (
            "init_db creates the job-anchor index on upgrade"
        )
        assert "idx_jobs_offered_to" in names, (
            "init_db creates the offered_to index on upgrade"
        )
        assert "idx_events_target" not in names, (
            "init_db drops the subsumed target-only index on upgrade"
        )
        with db._conn() as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT MAX(created_at) FROM events"
                " WHERE target_type = 'job' AND target_id = ?"
                " AND kind IN ('job_claimed')",
                (1,),
            ).fetchall()
        assert any("idx_events_job_anchor" in r[-1] for r in plan), (
            "the anchor probe uses the new index"
        )
        db.init_db()  # second boot is a no-op, not an error
    finally:
        db.DB_PATH = saved_db_path
    print("  jobs anchor/offered_to index migration: ok")

    # --- migration: perf bundle 3 index overhaul --------------------------
    # schema.sql drops 13 redundant/subsumed/unused indexes and adds two
    # partial covering ledger indexes; init_db() must DROP the removed
    # ones on upgraded databases (schema.sql only adds) and CREATE the
    # new ones. Seed a pre-bundle database and one boot must converge.
    _DROPPED_B3 = (
        "idx_comments_post",
        "idx_posts_agent",
        "idx_comments_agent",
        "idx_posts_proposal_kind",
        "idx_reports_target",
        "idx_proposal_votes_post",
        "idx_proposal_links_post",
        "idx_proposal_outcomes_post",
        "idx_notifications_agent",
        "idx_tool_calls_tool",
        "idx_posts_title_nocase",
        "idx_comments_parent",
        "idx_credit_entries_agent",
    )
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "bundle3_index_migration.db")
        db.init_db()
        with db._conn() as conn:
            for _name in _DROPPED_B3:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {_name} ON posts(created_at)")
        db.init_db()  # the upgrade: drop the 13, add the 2 partials
        with db._conn() as conn:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        for _name in _DROPPED_B3:
            assert _name not in names, f"init_db drops redundant {_name}"
        assert "idx_credit_entries_agent_account" in names, (
            "init_db creates the agent-account covering index"
        )
        assert "idx_credit_entries_treasury_flows" in names, (
            "init_db creates the treasury-flows covering index"
        )
        with db._conn() as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT agent_id, SUM(delta_units)"
                " FROM credit_entries WHERE account = 'agent'"
                " GROUP BY agent_id",
            ).fetchall()
        assert any("idx_credit_entries_agent_account" in r[-1] for r in plan), (
            "the holders GROUP BY uses the new index"
        )
        db.init_db()  # second boot is a no-op, not an error
    finally:
        db.DB_PATH = saved_db_path
    print("  bundle 3 index overhaul migration: ok")

    # --- credit_entries tx_id column migration ---------------------------
    # A pre-tx_id database carries credit_entries without the `tx_id`
    # column.  init_db() must ADD the column (NULL for legacy rows) and
    # create the index - the new column cannot live in schema.sql's
    # executescript because it would crash on an old DB missing it.
    _OLD_CREDIT_DDL = """CREATE TABLE credit_entries (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id       INTEGER REFERENCES agents(id),
        delta_units INTEGER NOT NULL CHECK (delta_units != 0),
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
            "INSERT INTO credit_entries (agent_id, delta_units, reason,"
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

    from tests._helpers import assert_upgrade_column

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
    assert "idx_posts_proposal_kind_created" in _plan, (
        "posts filtered by proposal_kind must use idx_posts_proposal_kind_created"
    )
    assert "idx_posts_proposal_kind" not in _plan.replace(
        "idx_posts_proposal_kind_created", ""
    ), "the dropped single-column index must not serve the filter"

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

    print("test_misc_d: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
