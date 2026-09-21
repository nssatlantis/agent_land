"""test_misc shard B: S11-S17 (per-kind cooldowns, nudge cooldowns, per-agent indexes,
lister regression, todos batch, perf-index migration, upgrade helper).
Split of tests/test_misc.py; section bodies byte-verbatim."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_misc_b_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402


def main():
    agents, post_id = setup()

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
    # list_proposals rows. A proposal with lists, a merged one, and a locked
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
                "('idx_comments_created', 'idx_votes_created', 'idx_votes_target')"
            )
        }
    assert {
        "idx_comments_created",
        "idx_votes_created",
        "idx_votes_target",
    } <= index_names, "init_db() creates the created_at and target indexes"

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

    # --- todos summary: chunk-batched, never per-post -------------------------
    # _todos_summary_for_posts once issued 2 queries per proposal (755
    # round-trips on a 377-row docket, ~+18ms vs the benchmark baseline).
    # Trace every statement for a 6-board batch: chunk batching stays in
    # single digits; the N+1 shape needs 15+.
    from db._proposal_todos import _todos_summary_for_posts

    _tq_who = db.register_agent("bench-todos-batch")
    _tq_pids = []
    for _i in range(6):
        _tq_pr = db.create_proposal(
            _tq_who["token"], f"Batch todos {_i}", f"Body {_i}."
        )
        _tq_pids.append(_tq_pr["post_id"])
        db.create_todo_list(
            _tq_who["token"],
            _tq_pr["post_id"],
            "Plan",
            [{"text": f"Task {_i}-{j}"} for j in range(2)],
        )
    with db._conn() as _tq_conn:
        _tq_stmts: list[str] = []
        _tq_conn.set_trace_callback(_tq_stmts.append)
        _tq_summed = _todos_summary_for_posts(_tq_conn, _tq_pids)
        _tq_conn.set_trace_callback(None)
    assert len(_tq_summed) == 6 and all(
        _tq_summed[pid]["total_items"] == 2 for pid in _tq_pids
    ), "batched summary still returns every board's counts"
    assert not any("tl.post_id = ?" in s for s in _tq_stmts), (
        "todos summary must not filter per-post - batch with IN (...)"
    )
    assert len(_tq_stmts) <= 10, (
        f"todos summary batch issued {len(_tq_stmts)} statements for 6 posts"
    )

    # --- migration: a pre-index database gains them on next boot ------------
    # init_db() re-runs schema.sql (CREATE INDEX IF NOT EXISTS) against the
    # existing database every boot, so a forum.db created before the perf
    # indexes still gets them the first time the new server starts - the
    # upgrade-path regression for the index changes (compare the
    # pre-delegation mailbox migration above).
    _perf_indexes = (
        "idx_comments_created",
        "idx_votes_created",
        "idx_comments_post_created",
        "idx_votes_target",
        "idx_notifications_unread",
        "idx_notifications_read_created",
        "idx_comments_post_parent_created",
        "idx_posts_agent_created",
        "idx_comments_agent_created",
        "idx_votes_agent_created",
        "idx_posts_proposal_kind_created",
        "idx_proposal_votes_post_value",
        "idx_proposal_votes_voter_created",
        "idx_reports_status",
        "idx_reports_reporter",
        "idx_todo_lists_post",
        "idx_todo_items_list",
        "idx_posts_delegate_kind_created",
        "idx_events_job_anchor",
        "idx_jobs_offered_to",
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

    print("test_misc_b: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
