"""test_misc shard E: S36-S40 (governance knobs, dotenv reload, db helpers, events
shape, workflow-kind migration). Split of tests/test_misc.py; section bodies
byte-verbatim."""

import asyncio
import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_misc_e_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    aggregates,
    config,
    db,
    expect_error,
    moderation,
    reports,
    setup,
)


def main():
    agents, post_id = setup()

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
    # sort=top net tally: the grouped JOIN must match hand-counted nets and
    # order net DESC (270:4911 refactor of the correlated subquery).
    top_a = db.create_post(agents["alpha"]["token"], "top sort one", "b")["post_id"]
    top_b = db.create_post(agents["beta"]["token"], "top sort two", "b")["post_id"]
    db.vote(agents["gamma"]["token"], "post", top_a, 1)
    db.vote(agents["delta"]["token"], "post", top_a, 1)
    db.vote(agents["epsilon"]["token"], "post", top_a, -1)
    db.vote(agents["gamma"]["token"], "post", top_b, -1)
    top_rows = aggregates.recent_activity(kind="posts", sort="top", limit=50)
    top_nets = {r["target_id"]: r.get("net") for r in top_rows}
    assert top_nets[top_a] == 1 and top_nets[top_b] == -1, top_nets
    top_ids = [r["target_id"] for r in top_rows]
    assert top_ids.index(top_a) < top_ids.index(top_b), "net DESC ordering holds"
    print("  recent_activity sort=top nets: ok")
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

    print("test_misc_e: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
