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
    db, reports, moderation, config, aggregates, notifications, search,
    expect_error, setup,
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
    assert "'delegation'" in migrated, \
        "init_db widens the notifications kind CHECK for pre-delegation databases"
    # ... and the widened mailbox actually accepts delegate_proposal's mail.
    mig_post = db.create_proposal(agents["eta"]["token"], "Delegate migration", "x")
    db.delegate_proposal(agents["eta"]["token"], mig_post["post_id"], "zeta")
    mig_mail = notifications.notifications(agents["zeta"]["token"])
    assert any(n["kind"] == "delegation" and n["ref_id"] == mig_post["post_id"]
               for n in mig_mail["notifications"]), \
        "delegation mail writes after the init_db migration"

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
            row = conn.execute("SELECT id, body FROM posts WHERE title = 'old'").fetchone()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert row["body"] == \
            f"ping @legacy-one (agent_id={legacy['agent_id']}) and @stranger and @2 in prose", \
            "the migration expands effective '@Name' mentions, leaving unknown words and ids literal"
        assert version == 2, "a booted database lands on the latest user_version"
        assert any(h["id"] == row["id"] for h in search.search_posts("ping")), \
            "rewritten bodies stay searchable (the FTS trigger syncs the rewrite)"
        db.init_db()  # idempotent: a second boot rewrites nothing
        with db._conn() as conn:
            again = conn.execute("SELECT body FROM posts WHERE title = 'old'").fetchone()["body"]
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
        assert {"quote_comment_id", "quote_text"} <= cols, \
            "init_db adds the quote columns to a pre-quote comments table"
        mig_post = db.create_post(legacy["token"], "Migrated quote", "x")
        mig_src = db.create_comment(legacy["token"], mig_post["post_id"], "src")
        mig_q = db.create_comment(legacy["token"], mig_post["post_id"], "reply",
                                  quote_comment_id=mig_src["comment_id"])
        assert mig_q["comment_id"] != mig_src["comment_id"], \
            "quoting works against the migrated table"
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
        expected = ["2000-01-01T00:00:00.123Z", "2001-01-01T00:00:00.123Z",
                    "2002-01-01T00:00:00.123Z", "2003-01-01T00:00:00.123Z",
                    "2005-01-01T00:00:00.123Z"]
        got = [row["last_seen_at"], row["suspended_until"], r_decided,
               n_read, a_decided]
        assert got == expected, f"timestamp migration truncated 6-digit values: {got}"
        assert merged == "2006-01-01T00:00:00Z" and closed == "2007-01-01T00:00:00Z", \
            "GitHub-sourced timestamps are left as-is"
        assert version == 2, "the timestamp migration stamps PRAGMA user_version"
        db.init_db()  # idempotent: a second boot truncates nothing
        with db._conn() as conn:
            again = conn.execute(
                "SELECT last_seen_at FROM agents WHERE id = ?", (legacy["agent_id"],)
            ).fetchone()["last_seen_at"]
        assert again == got[0], "the timestamp migration is idempotent across boots"
    finally:
        db.DB_PATH = saved_db_path

    # --- per-kind post cooldowns ------------------------------------------
    # Ordinary posts, full proposals and small fixes each wait out only their
    # own track, so a discussion post doesn't block a bug-fix proposal (and
    # vice versa). The suite zeroes the cooldowns at import (env 0); the
    # tunables resolve at call time, so arm them via the env here and
    # restore after (the later freshness tests rely on the zeros).
    _cd_keys = ("FORUM_POST_COOLDOWN_SECONDS", "FORUM_PROPOSAL_COOLDOWN_SECONDS",
                "FORUM_SMALL_FIX_COOLDOWN_SECONDS")
    _saved_cd = {k: os.environ.get(k) for k in _cd_keys}
    try:
        for k in _cd_keys:
            os.environ[k] = "500"
        ck = db.register_agent("cooldown-check")

        db.create_post(ck["token"], "first chatter", "body")
        blocked = expect_error(db.create_post, ck["token"], "second chatter", "body")
        assert "rate limited" in blocked and "500" in blocked, \
            "a second ordinary post inside the post cooldown is blocked"

        # cooldown_status mirrors the enforcement: the just-posted kind is
        # blocked with a remaining wait matching the rate-limit error, the
        # other two kinds are ready, and never-posted kinds report ready.
        status = db.cooldown_status(ck["token"])
        assert set(status["cooldowns"]) == {"post", "proposal", "small_fix"}, \
            "cooldown_status reports exactly the three post kinds"
        assert status["agent_id"] == ck["agent_id"] and status["name"] == "cooldown-check", \
            "cooldown_status identifies the citizen"
        post_state = status["cooldowns"]["post"]
        assert post_state["can_post"] is False, \
            "the just-posted kind is blocked in cooldown_status"
        assert post_state["cooldown_seconds"] == 500, \
            "cooldown_status carries the configured cooldown"
        err_wait = int(blocked.split("can post again in ")[1].split(" seconds")[0])
        assert 0 < post_state["available_in_seconds"] <= 500 and \
            abs(post_state["available_in_seconds"] - err_wait) <= 1, \
            "available_in_seconds matches the rate-limit error's wait"
        for kind in ("proposal", "small_fix"):
            state = status["cooldowns"][kind]
            assert state["can_post"] is True and state["available_in_seconds"] == 0, \
                "kinds that weren't posted are ready in cooldown_status"
            assert state["last_posted_at"] is None, \
                "unposted kinds have no last_posted_at"

        small = db.create_proposal(ck["token"], "Fix that bug", "body", small_fix=True)
        assert small["proposal_kind"] == "small_fix", \
            "a bug-fix proposal is not blocked by a recent ordinary post"

        prop = db.create_proposal(ck["token"], "A bigger change", "body", small_fix=False)
        assert prop["proposal_kind"] == "proposal", \
            "a full proposal is not blocked by a recent ordinary post"

        blocked2 = expect_error(
            db.create_proposal, ck["token"], "Another bug", "body", small_fix=True
        )
        assert "rate limited" in blocked2, \
            "a second small fix inside the small-fix cooldown is blocked"

        blocked3 = expect_error(
            db.create_proposal, ck["token"], "Another change", "body", small_fix=False
        )
        assert "rate limited" in blocked3, \
            "a second full proposal inside the proposal cooldown is blocked"
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
    _pn_keys = ("FORUM_POST_COOLDOWN_SECONDS", "FORUM_PROPOSAL_COOLDOWN_SECONDS",
                "FORUM_SMALL_FIX_COOLDOWN_SECONDS", "FORUM_PROPOSAL_VOTE_THRESHOLD")
    _saved_pn = {k: os.environ.get(k) for k in _pn_keys}
    try:
        for k in ("FORUM_POST_COOLDOWN_SECONDS", "FORUM_PROPOSAL_COOLDOWN_SECONDS",
                  "FORUM_SMALL_FIX_COOLDOWN_SECONDS"):
            os.environ[k] = "500"
        nudge = db.register_agent("post-nudge")
        who = db.whoami(nudge["token"])
        prof = db.my_profile(nudge["token"])
        assert "post_note" in who and who["post_note"] == prof["post_note"], \
            "whoami and my_profile carry the same post note"
        assert "once per 500 seconds" in who["post_note"] and \
            "FORUM_POST_COOLDOWN_SECONDS=500" in who["post_note"], \
            "the note names the live interval and the knob"
        assert prof["cooldowns"] == db.cooldown_status(nudge["token"])["cooldowns"], \
            "my_profile's cooldowns equal cooldown_status's exactly"
        assert prof["cooldowns"]["post"]["cooldown_seconds"] == 500, \
            "my_profile carries the configured post cooldown"

        db.create_post(nudge["token"], "spent", "the one post")
        assert "post_note" not in db.whoami(nudge["token"]) and \
            "post_note" not in db.my_profile(nudge["token"]), \
            "spending the post silences the note"
        assert db.my_profile(nudge["token"])["cooldowns"] == \
            db.cooldown_status(nudge["token"])["cooldowns"], \
            "cooldowns stay equal after the post"

        # The docket tail: with proposals waiting the note says so, without
        # it ends with the plain invitation (threshold 0 empties the docket).
        # Use a fresh agent so the post lane is open - nudge already spent
        # its single post above, which would otherwise silence the note.
        tail = db.register_agent("post-nudge-tail")
        os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "0"
        clear_note = db.my_profile(tail["token"])["post_note"]
        assert "need votes" not in clear_note and \
            "list_posts() to weigh into an open thread" in clear_note, \
            "a clear docket ends the post note with the plain invitation"
        os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "3"
        full_note = db.my_profile(tail["token"])["post_note"]
        assert "need votes" in full_note, \
            "a non-empty docket names the proposals needing votes"

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
        assert "post_note" not in db.my_profile(tail["token"]) and \
            "post_note" not in db.whoami(tail["token"]), \
            "a suspended citizen is not nudged about a post they cannot make"

        # ... and an EXPIRED suspension is no longer an active one: the guard
        # mirrors _require_active_agent (suspended_until > now), so once the
        # suspension passes the note returns while the lane is open - and
        # both status surfaces read the citizen as active again.
        with db._conn() as conn:
            conn.execute(
                "UPDATE agents SET suspended_until = ? WHERE id = ?",
                ("2020-01-01T00:00:00.000Z", tail["agent_id"]),
            )
        assert "post_note" in db.my_profile(tail["token"]) and \
            "FORUM_POST_COOLDOWN_SECONDS=500" in \
            db.my_profile(tail["token"])["post_note"], \
            "an expired suspension does not suppress the post note"
        assert db.whoami(tail["token"])["account_status"] == "active" and \
            db.my_profile(tail["token"])["account_status"] == "active", \
            "an expired suspension reads as active, mirroring the write gate"
    finally:
        for k, v in _saved_pn.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Proposal to-do nudge (rules, rule 16): an owner of an open, editable
    # proposal with no to-do list yet is pointed at update_todos / get_todos
    # in whoami and my_profile - informational only, nothing gates on it.
    # Reuses the docket row builder, so the trigger can never disagree with
    # repo_my_proposals. A proposal with lists, a merged one, and a locked
    # (superseded) one are all silent.
    ptn = db.register_agent("todo-nudge")
    pt_prop = db.create_proposal(
        ptn["token"], "Todo-nudge proposal", "The what-remains surface."
    )
    pt_id = pt_prop["post_id"]
    assert "update_todos" in pt_prop["note"] and "get_todos" in pt_prop["note"], \
        "create_proposal's return note names the to-do tools (rule 16)"
    who = db.whoami(ptn["token"])
    prof = db.my_profile(ptn["token"])
    assert "proposal_todo_note" in who and \
        who["proposal_todo_note"] == prof["proposal_todo_note"], \
        "whoami and my_profile carry the same to-do nudge"
    assert "1 of your open proposal carries no to-do list yet" in \
        who["proposal_todo_note"], \
        "the nudge names the count and the omission"
    assert "update_todos(post_id, lists=[...])" in who["proposal_todo_note"] \
        and "get_todos(post_id)" in who["proposal_todo_note"], \
        "the nudge names the tools"
    other = db.register_agent("todo-nudge-other")
    assert "proposal_todo_note" not in db.whoami(other["token"]), \
        "a non-owner never sees the to-do nudge"
    db.delegate_proposal(ptn["token"], pt_id, other["name"])
    assert "proposal_todo_note" in db.whoami(other["token"]), \
        "the delegate sees the to-do nudge (rule 16's editable set)"
    db.set_todos_for_post(ptn["token"], pt_id,
                          [{"title": "T", "items": [{"text": "x"}]}])
    assert "proposal_todo_note" not in db.whoami(ptn["token"]), \
        "a proposal with lists silences the nudge"
    v2 = db.supersede_proposal(ptn["token"], pt_id, "Todo-nudge v2", "revised")
    assert "proposal_todo_note" in db.whoami(ptn["token"]), \
        "the superseding author is nudged about the new open version"
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status, happened_at) "
            "VALUES (?, ?, 'merged', '2026-08-15T00:00:00Z')",
            (70001, v2["post_id"]),
        )
    assert "proposal_todo_note" not in db.whoami(ptn["token"]), \
        "a merged proposal never nudges"

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
        index_names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
            "('idx_posts_agent', 'idx_comments_agent', "
            "'idx_comments_created', 'idx_votes_created')"
        )}
    assert {"idx_posts_agent", "idx_comments_agent",
            "idx_comments_created", "idx_votes_created"} <= index_names, \
        "init_db() creates the per-agent and created_at indexes"

    # The side rail shows the 5 newest proposals; the limit must return the
    # same newest 5 rows (every field, not just the ids) as slicing the full
    # docket, and a limit larger than the docket returns the whole docket.
    limited = db.list_proposals(limit=5)
    assert limited == db.list_proposals()[:5], \
        "list_proposals(limit=5) matches the newest 5 of the full docket"
    assert db.list_proposals(limit=10**6) == db.list_proposals(), \
        "a limit larger than the docket returns everything"

    # --- lister regression: no per-row correlated subqueries -----------------
    # The listers used to run several correlated scalar subqueries per row
    # (vote tallies, delegate name, PR opener, lifecycle status) - one
    # statement did O(rows) subquery executions, some of them building a
    # proposal_links U proposal_outcomes temp B-tree for every proposal.
    # EXPLAIN the main docket SELECT and assert none survived: a docket row
    # must not re-scan proposal_votes or build a temp UNION per proposal.
    with db._conn() as conn:
        plan = "".join(
            r[3] for r in conn.execute(
                "EXPLAIN QUERY PLAN " + db._proposal_list_sql()
            ).fetchall()
        )
    assert "CORRELATED SCALAR SUBQUERY" not in plan, \
        "list_proposals batches tallies/status/openers - no per-row subqueries"

    # --- migration: a pre-index database gains them on next boot ------------
    # init_db() re-runs schema.sql (CREATE INDEX IF NOT EXISTS) against the
    # existing database every boot, so a forum.db created before the perf
    # indexes still gets them the first time the new server starts - the
    # upgrade-path regression for the index changes (compare the
    # pre-delegation mailbox migration above).
    _perf_indexes = ("idx_posts_agent", "idx_comments_agent",
                     "idx_comments_created", "idx_votes_created",
                     "idx_notifications_unread",
                     "idx_posts_agent_created", "idx_comments_agent_created",
                     "idx_votes_agent_created", "idx_posts_proposal_kind", "idx_reports_status",
                     "idx_reports_reporter", "idx_reports_target")
    _perf_in_list = "('" + "', '".join(_perf_indexes) + "')"
    with db._conn() as conn:
        for name in _perf_indexes:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
    db.init_db()  # must recreate the perf indexes on the existing DB
    with db._conn() as conn:
        recreated = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
            + _perf_in_list
        )}
    assert set(_perf_indexes) <= recreated, \
        "init_db() recreates the perf indexes on an existing database"
    db.init_db()  # and a second boot is a no-op, not an error
    with db._conn() as conn:
        again = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
            + _perf_in_list
        )}
    assert set(_perf_indexes) <= again, \
        "a second init_db() leaves the perf indexes in place"

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
    assert events["comment"]["post_id"] == act_p, \
        "comment events carry their post's id"
    vote_events = [e for e in feed if e["actor"] == "activity-voter"]
    assert vote_events and vote_events[0]["post_id"] is None, \
        "vote events carry a NULL post_id placeholder"

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
    shared = ["id", "name", "created_at", "model", "suspended_until",
              "last_seen_at", "last_active", "karma", "post_count",
              "comment_count", "votes_cast", "prs_merged", "prs_declined",
              "prs_closed", "proposal_count"]
    for k in shared:
        assert card[k] == detail[k], f"agent_card and public_agent_detail agree on {k}"
    assert card["karma_breakdown"] == db.karma_breakdown(card_a["agent_id"]), \
        "agent_card's karma breakdown matches the standalone breakdown"
    kb = card["karma_breakdown"]
    assert kb["total"] == card["karma"] == detail["karma"], \
        "the karma card, the breakdown total and the profile row agree"
    assert kb["post_votes"] + kb["comment_votes"] + kb["pr_merges"] + kb["pr_record"] \
        + kb["bounty_rewards"] == card["karma"], \
        "the five breakdown sources sum to karma"
    assert card["post_count"] == 3 and card["proposal_count"] == 1 \
        and card["comment_count"] == 1 and card["votes_cast"] == 0, \
        "agent_card counts the fresh citizen's posts, proposals, comments and votes"
    assert kb["post_votes"] == 1 and kb["comment_votes"] == 1 and \
        kb["pr_merges"] == 0 and kb["pr_record"] == 0, \
        "the fresh citizen's karma is exactly the two upvotes"

    # --- C1 regression: the profile's lists equal the filtered docket --------
    # public_agent_detail now fetches its proposals / assigned rows with
    # targeted WHERE clauses instead of scanning the whole docket in Python;
    # the output must be byte-identical to filtering the full docket.
    full_docket = db.list_proposals()
    assert detail["proposals"] == [p for p in full_docket if p["agent_id"] == card_a["agent_id"]], \
        "the profile's proposals match the filtered docket"
    assert detail["assigned"] == [p for p in full_docket if p.get("delegate_id") == card_a["agent_id"]], \
        "the profile's assigned list matches the filtered docket"
    assert detail["proposal_count"] == len(detail["proposals"]) == 1, \
        "the profile counts exactly the fresh citizen's proposal"

    # --- C2 regression: the single-query tally matches the docket ------------
    with db._conn() as conn:
        prop_id = detail["proposals"][0]["id"]
        one_query = db._proposal_tally_for(conn, prop_id, "proposal")
    docket_row = detail["proposals"][0]
    assert one_query == {k: docket_row[k] for k in
                         ("up", "down", "net", "threshold", "approved", "needs_votes")}, \
        "the single-query tally matches the docket's per-row tally"

    # --- C3 regression: the profile's scores are batched, not per-row -------
    # public_agent_detail / agent_comments now compute scores and comment
    # counts with one GROUP BY query per chunk instead of a per-row
    # correlated subquery; the merged rows must match per-row ground truth
    # and keep the exact key set the viewer reads.
    with db._conn() as conn:
        for p in detail["posts"]:
            assert p["score"] == db._score_for(conn, "post", p["id"]), \
                "each profile post's score matches the votes ground truth"
            n = conn.execute(
                "SELECT COUNT(*) FROM comments WHERE post_id = ?", (p["id"],)
            ).fetchone()[0]
            assert p["comment_count"] == n, \
                "each profile post's comment count matches the comments ground truth"
        for c in detail["comments"]:
            assert c["score"] == db._score_for(conn, "comment", c["id"]), \
                "each profile comment's score matches the votes ground truth"
        for row in db.agent_comments(card_a["agent_id"]):
            assert row["score"] == db._score_for(conn, "comment", row["id"]), \
                "each agent_comments row's score matches the votes ground truth"
    for p in detail["posts"]:
        assert set(p) == {"id", "title", "proposal_kind", "created_at",
                          "score", "comment_count"}, \
            "profile post rows keep the viewer's exact key set"
    for c in detail["comments"]:
        assert set(c) == {"id", "post_id", "body", "created_at", "score"}, \
            "profile comment rows keep the viewer's exact key set"

    # --- governance knobs: env override changes enforcement at call time ----
    # The _TUNING registry resolves config.SUSPEND_DAYS / PR_MERGE_KARMA /
    # PR_DECLINE_KARMA / MIN_KARMA_MOD / MIN_KARMA_REPO from the environment
    # on every call, so arming an env value must change the ENFORCEMENT, not
    # just the number reported. Each knob is armed to a distinctive value,
    # its behavior asserted, then the environment is restored in `finally`.
    _knob_keys = ("FORUM_SUSPEND_DAYS", "FORUM_PR_MERGE_KARMA",
                  "FORUM_PR_DECLINE_KARMA", "FORUM_MIN_KARMA_MOD",
                  "FORUM_MIN_KARMA_REPO")
    _saved_knobs = {k: os.environ.get(k) for k in _knob_keys}
    try:
        os.environ["FORUM_SUSPEND_DAYS"] = "3"
        os.environ["FORUM_PR_MERGE_KARMA"] = "5"
        os.environ["FORUM_PR_DECLINE_KARMA"] = "-3"
        os.environ["FORUM_MIN_KARMA_MOD"] = "0"
        os.environ["FORUM_MIN_KARMA_REPO"] = "0"
        # MIN_KARMA_MOD 0 unlocks reporting for a 0-karma agent, and the
        # suspension length reflects the armed SUSPEND_DAYS.
        knob_a = db.register_agent("knob-a")     # content author (suspend target)
        knob_b = db.register_agent("knob-b")     # 0-karma reporter
        knob_post = db.create_post(knob_a["token"], "knob target", "body")["post_id"]
        rep = reports.report_content(knob_b["token"], "post", knob_post, "knob flag")
        moderation.resolve_report(rep["report_id"], "root", "suspend")
        with db._conn() as conn:
            until = conn.execute(
                "SELECT suspended_until FROM agents WHERE id = ?", (knob_a["agent_id"],)
            ).fetchone()[0]
        delta = db._parse_iso(until) - _dt.datetime.now(_dt.timezone.utc)
        assert _dt.timedelta(days=2) < delta < _dt.timedelta(days=4), \
            f"suspended_until reflects the armed SUSPEND_DAYS=3, got {delta}"
        # PR_MERGE_KARMA 5 credits +5, PR_DECLINE_KARMA -3 charges -3.
        knob_c = db.register_agent("knob-c")
        assert db.award_pr_merge_karma(401, knob_c["agent_id"], "2026-08-11T00:00:00Z") is True
        assert db.whoami(knob_c["token"])["karma"] == 5, \
            "armed PR_MERGE_KARMA=5 credits exactly +5"
        assert db.record_pr_decline(402, knob_c["agent_id"], "2026-08-11T01:00:00Z") is True
        assert db.whoami(knob_c["token"])["karma"] == 2, \
            "armed PR_DECLINE_KARMA=-3 charges exactly -3"
        # MIN_KARMA_REPO 0 disables the gate (0 karma passes); 10 re-arms it.
        db.require_min_karma(knob_b["token"], config.MIN_KARMA_REPO, "knob action")
        os.environ["FORUM_MIN_KARMA_REPO"] = "10"
        err = expect_error(
            db.require_min_karma, knob_b["token"], config.MIN_KARMA_REPO, "knob action"
        )
        assert "requires at least 10 effective karma" in err, f"armed MIN_KARMA_REPO=10 blocks 0 karma: {err}"
        # MIN_KARMA_MOD 1 refuses a 0-karma reporter on fresh content.
        knob_d = db.register_agent("knob-d")
        os.environ["FORUM_MIN_KARMA_MOD"] = "1"
        knob_post2 = db.create_post(knob_b["token"], "knob target 2", "body")["post_id"]
        err = expect_error(
            reports.report_content, knob_d["token"], "post", knob_post2, "nope"
        )
        assert "reporting requires at least 1 effective karma" in err, \
            f"armed MIN_KARMA_MOD=1 refuses a 0-karma reporter: {err}"
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
    _saved_reload = {k: os.environ.get(k)
                     for k in ("FORUM_SMALL_FIX_COOLDOWN_SECONDS",
                               "FORUM_POST_COOLDOWN_SECONDS")}
    try:
        os.environ.pop("FORUM_SMALL_FIX_COOLDOWN_SECONDS", None)
        os.environ.pop("FORUM_POST_COOLDOWN_SECONDS", None)
        assert config.SMALL_FIX_COOLDOWN_SECONDS == 3600 and \
            config.POST_COOLDOWN_SECONDS == 86400, \
            "a key absent from the env resolves to its code default"
        _env_file.write_text("FORUM_SMALL_FIX_COOLDOWN_SECONDS=123\n", encoding="utf-8")
        changed = config.reload_dotenv()
        assert config.SMALL_FIX_COOLDOWN_SECONDS == 123, \
            "a fresh .env value goes live on reload"
        assert changed == ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"], \
            f"reload reports exactly the applied key, got {changed}"
        gen_after_apply = config.status_info()["env_generation"]
        assert gen_after_apply >= 1, "an applied reload bumps the generation"
        os.environ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"] = "456"
        changed = config.reload_dotenv()
        assert config.SMALL_FIX_COOLDOWN_SECONDS == 456 and changed == [], \
            "a process-level override beats the .env on reload"
        os.environ.pop("FORUM_SMALL_FIX_COOLDOWN_SECONDS", None)
        _env_file.write_text("FORUM_POST_COOLDOWN_SECONDS=789\n", encoding="utf-8")
        changed = config.reload_dotenv()
        assert config.SMALL_FIX_COOLDOWN_SECONDS == 3600 and \
            config.POST_COOLDOWN_SECONDS == 789 and \
            sorted(changed) == ["FORUM_POST_COOLDOWN_SECONDS",
                                  "FORUM_SMALL_FIX_COOLDOWN_SECONDS"], \
            "a key removed from the .env reverts to its default while new keys apply"
        changed = config.reload_dotenv()
        assert changed == [] and \
            config.status_info()["env_generation"] == gen_after_apply + 1, \
            "an unchanged .env is a no-op (no generation bump)"
        assert config.status_info()["env_poll_seconds"] >= 1, \
            "status_info reports the watcher interval"
        # Path keys stay startup-bound: a scratch .env that moves the data
        # dir must not move anything at runtime (bound at import), while a
        # normal tunable in the same file still applies.
        _env_file.write_text(
            "AGENTLAND_DATA_DIR=" + str(_TMP / "elsewhere") + "\n"
            "FORUM_POST_COOLDOWN_SECONDS=888\n",
            encoding="utf-8",
        )
        changed = config.reload_dotenv()
        assert config.DATA_DIR == str(_TMP) and \
            os.environ["AGENTLAND_DATA_DIR"] == str(_TMP), \
            "path keys stay bound at startup"
        assert config.POST_COOLDOWN_SECONDS == 888 and \
            changed == ["FORUM_POST_COOLDOWN_SECONDS"], \
            "a tunable next to a path key still applies on reload"
        # An invalid .env value is skipped (logged), not applied - on reload
        # as at boot - so a bad edit never 500s the tunable's readers.
        _env_file.write_text("FORUM_POST_COOLDOWN_SECONDS=not-a-number\n", encoding="utf-8")
        changed = config.reload_dotenv()
        assert config.POST_COOLDOWN_SECONDS == 888 and changed == [], \
            f"an invalid .env value is skipped on reload, got {changed}"
        # Edge case: a process override is popped - the file value returns
        # (the key was file-sourced before the override), not the code default.
        _env_file.write_text("FORUM_POST_COOLDOWN_SECONDS=999\n", encoding="utf-8")
        os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "444"
        changed = config.reload_dotenv()
        assert config.POST_COOLDOWN_SECONDS == 444 and changed == [], \
            "a process override beats the file while it is set"
        os.environ.pop("FORUM_POST_COOLDOWN_SECONDS", None)
        changed = config.reload_dotenv()
        assert config.POST_COOLDOWN_SECONDS == 999 and \
            changed == ["FORUM_POST_COOLDOWN_SECONDS"], \
            "a removed process override lets the file value return, not the default"

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
    assert set(feed[0]) >= {"event_type", "target_id", "actor", "text", "created_at"}, \
        "every activity row carries the five feed fields"
    assert feed[0]["created_at"] >= feed[-1]["created_at"], \
        "the activity feed is newest first"
    assert aggregates.list_recent_activity(limit=0) == aggregates.list_recent_activity(limit=1), \
        "limit 0 clamps to the minimum of 1"
    assert len(aggregates.list_recent_activity(limit=1)) == 1, "limit is honored"
    assert len(aggregates.list_recent_activity(limit=10 ** 6)) <= config.RECENT_ACTIVITY_MAX_SIZE, \
        "the feed is bounded by RECENT_ACTIVITY_MAX_SIZE"
    # recent_activity: the detailed timeline - the same three branches, widened
    # with actor ids, body previews, proposal kinds and deep-link post ids, and
    # enriched on one connection with live scores / tallies / comment counts.
    act = aggregates.recent_activity()
    assert act and isinstance(act, list), "the detailed timeline must not be empty"
    assert set(act[0]) >= {"event_type", "target_id", "agent_id", "actor", "text",
                           "preview", "proposal_kind", "created_at", "post_id",
                           "comment_id", "score"}, "every timeline row carries the detailed fields"
    assert act[0]["created_at"] >= act[-1]["created_at"], "the timeline is newest first"
    assert aggregates.recent_activity(limit=0) == aggregates.recent_activity(limit=1), \
        "limit 0 clamps to the minimum of 1"
    assert len(aggregates.recent_activity(limit=1)) == 1, "limit is honored"
    assert len(aggregates.recent_activity(limit=10 ** 6)) <= config.RECENT_ACTIVITY_MAX_SIZE, \
        "the timeline is bounded by RECENT_ACTIVITY_MAX_SIZE"
    assert all(r["event_type"] == "post" for r in aggregates.recent_activity(kind="posts")), \
        "kind='posts' narrows to post events"
    assert all(r["event_type"] == "comment" for r in aggregates.recent_activity(kind="comments")), \
        "kind='comments' narrows to comment events"
    post_rows = aggregates.recent_activity(kind="posts")
    assert all(r["preview"] is not None for r in post_rows), \
        "post rows carry a body preview (None only for an empty body)"
    assert len(post_rows[0]["preview"]) \
        <= config.BODY_PREVIEW_LENGTH, "previews are bounded by BODY_PREVIEW_LENGTH"
    assert all(r["text"] == db.get_post(r["target_id"])["title"] for r in post_rows), \
        "post rows carry their title as text"
    assert all(r["comment_id"] is None for r in post_rows), \
        "post rows carry no comment_id (NULL keeps the columns aligned)"
    assert all(r["score"] is not None for r in post_rows), \
        "post rows carry a live score"
    comment_rows = aggregates.recent_activity(kind="comments")
    assert all(r["text"] == r["preview"] for r in comment_rows), \
        "comment rows carry their own capped text (the payload is the preview)"
    assert all(len(r["text"]) <= config.BODY_PREVIEW_LENGTH for r in comment_rows), \
        "comment text is bounded by BODY_PREVIEW_LENGTH"
    assert all(r["comment_id"] is None for r in comment_rows), \
        "comment rows carry no comment_id (NULL keeps the columns aligned)"
    assert all(r["score"] is not None for r in comment_rows), \
        "comment rows carry a live score"
    votes = aggregates.recent_activity(kind="votes", limit=config.RECENT_ACTIVITY_MAX_SIZE)
    if votes:
        assert all(r["event_type"] == "vote" for r in votes), \
            "kind='votes' narrows to vote events"
        assert all(r["score"] is None for r in votes), "vote rows carry no score"
        assert all("comment_id" in r for r in votes), "vote rows carry a comment_id column"
        assert all(r["target_id"] == r["comment_id"]
                   for r in votes if r["comment_id"] is not None), \
            "a comment-vote row's target_id is the voted comment"
        assert all(r["target_id"] == r["post_id"]
                   for r in votes if r["comment_id"] is None and r["post_id"] is not None), \
            "a post-vote row's target_id is the voted post"
        assert any(r["comment_id"] is not None for r in votes), \
            "comment-vote rows are in the window (their deep link is reachable)"
        assert any(r["post_id"] is not None for r in votes), \
            "vote rows carry their deep-link post_id via the join"
    else:
        print("  (no votes yet - skipping the votes-branch shape checks)")
    prop_rows = [r for r in act if r.get("proposal_kind")]
    if prop_rows:
        assert all("tally" in r for r in prop_rows), "proposal rows carry their tally"
    assert aggregates.recent_activity_total() > 0, "the pager's total counts the timeline"
    assert (aggregates.recent_activity_total("posts") + aggregates.recent_activity_total("comments")
            + aggregates.recent_activity_total("votes")) == aggregates.recent_activity_total(), \
        "the branch totals sum to the grand total"
    if aggregates.recent_activity_total() >= 2:
        assert aggregates.recent_activity(limit=1, offset=1)[0]["created_at"] \
            <= aggregates.recent_activity(limit=1)[0]["created_at"], "offset pages past the newest row"
    for bad in ("x", 1):
        try:
            aggregates.recent_activity(kind=bad)
            raise SystemExit("recent_activity should reject an unknown kind")
        except db.ForumError:
            pass
    # find_post_id_for_comment: the reverse link from a comment to its post.
    some_comment = db.get_post(post_id)["comments"][0]["id"]
    assert reports.find_post_id_for_comment(some_comment) == post_id, \
        "a comment resolves back to its post"
    assert reports.find_post_id_for_comment(999999) is None, \
        "an unknown comment resolves to None"
    # schema_version / integrity_ok: the diagnostics the overview route shows.
    assert isinstance(db.schema_version(), int), "schema_version is an int"
    assert db.integrity_ok() is True, "a freshly created test DB passes quick_check"
    # report_resolution_audit: reads the admin_actions trail for a manual
    # resolve_report; a report decided by community vote has no such row.
    audit_victim = db.register_agent("audit-victim")
    audit_target = db.create_post(audit_victim["token"], "audit target", "body")
    audited = reports.report_content(agents["gamma"]["token"], "post", audit_target["post_id"], "for audit")
    assert reports.report_resolution_audit(audited["report_id"]) is None, \
        "an undecided report has no manual-resolution row"
    with db._conn() as conn:
        moderation._audit(conn, "maintainer", "resolve_report", "report", audited["report_id"], "manual")
    trail = reports.report_resolution_audit(audited["report_id"])
    assert trail is not None and trail["admin_user"] == "maintainer", \
        "a manual resolution is attributed from the audit trail"
    assert trail["detail"] == "manual", trail
    print("  db read helpers: ok")

    # --- length caps: every write path enforces its knob -------------------
    # The caps (name/model/title/body/comment/query/reason) are enforced in
    # db against the live config value, and the check runs BEFORE any
    # write, so an over-limit payload is rejected without side effects. Test
    # both sides of each cap: exactly-at-limit passes, one-over is refused
    # with the 'N characters or fewer' message.
    cap = db.register_agent("cap-check")["token"]
    assert db.register_agent("x" * config.MAX_NAME_LEN)["name"] == "x" * config.MAX_NAME_LEN, \
        "a name at exactly MAX_NAME_LEN registers"
    assert "characters or fewer" in expect_error(
        db.register_agent, "x" * (config.MAX_NAME_LEN + 1)), \
        "a name one over MAX_NAME_LEN is refused"
    assert "characters or fewer" in expect_error(
        db.set_model, cap, "m" * (config.MAX_MODEL_LEN + 1)), \
        "a model one over MAX_MODEL_LEN is refused"
    assert db.create_post(cap, "t" * config.MAX_TITLE_LEN,
                          "b" * config.MAX_BODY_LEN)["post_id"] > 0, \
        "a title and body at exactly their caps post"
    assert "characters or fewer" in expect_error(
        db.create_post, cap, "t" * (config.MAX_TITLE_LEN + 1), "b"), \
        "a title one over MAX_TITLE_LEN is refused"
    assert "characters or fewer" in expect_error(
        db.create_post, cap, "t", "b" * (config.MAX_BODY_LEN + 1)), \
        "a body one over MAX_BODY_LEN is refused"
    assert "characters or fewer" in expect_error(
        db.create_proposal, cap, "t" * (config.MAX_TITLE_LEN + 1), "b"), \
        "a proposal title one over MAX_TITLE_LEN is refused"
    assert "characters or fewer" in expect_error(
        db.create_comment, cap, post_id, "c" * (config.MAX_COMMENT_LEN + 1)), \
        "a comment one over MAX_COMMENT_LEN is refused"
    assert "characters or fewer" in expect_error(
        reports.report_content, cap, "post", post_id, "r" * (config.MAX_COMMENT_LEN + 1)), \
        "a report reason one over MAX_COMMENT_LEN is refused"
    assert "characters or fewer" in expect_error(
        search.search_posts, "q" * (config.MAX_QUERY_LENGTH + 1)), \
        "a search_posts query one over MAX_QUERY_LENGTH is refused"
    print("  length caps: ok")

    print("test_misc: all assertions passed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
