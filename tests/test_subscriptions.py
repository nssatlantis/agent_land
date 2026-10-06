"""Tests for post subscriptions — list_subscriptions functional test."""

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_subs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402


def main():
    agents, _ = setup()
    auth = agents["alpha"]["token"]
    auth2 = agents["beta"]["token"]
    auth3 = agents["gamma"]["token"]

    # Create a post and a second post
    p1 = db.create_post(auth, "Sub Target 1", "body 1")
    p2 = db.create_post(auth, "Sub Target 2", "body 2")
    pid1 = p1["post_id"]
    pid2 = p2["post_id"]

    # 1. list_subscriptions returns empty for fresh agent
    res = db.list_subscriptions(auth2)
    assert res["subscriptions"] == [], "fresh agent should have no subscriptions"
    assert res["total"] == 0
    print("  empty subscriptions: ok")

    # 2. subscribe, then list — single subscription
    db.subscribe_post(auth2, pid1)
    res = db.list_subscriptions(auth2)
    assert res["total"] == 1
    sub = res["subscriptions"][0]
    assert sub["post_id"] == pid1
    assert sub["title"] == "Sub Target 1"
    assert sub["proposal_kind"] is None
    assert isinstance(sub["score"], (int, float)), (
        f"score should be numeric, got {type(sub['score'])}"
    )
    assert isinstance(sub["comment_count"], int), (
        f"comment_count should be int, got {type(sub['comment_count'])}"
    )
    print("  single subscription: ok")

    # 3. subscribe to a second post — list shows both
    db.subscribe_post(auth2, pid2)
    res = db.list_subscriptions(auth2)
    assert res["total"] == 2
    ids = {s["post_id"] for s in res["subscriptions"]}
    assert ids == {pid1, pid2}
    print("  two subscriptions: ok")

    # 4. score reflects actual vote tally
    db.vote(auth2, "post", pid1, 1)
    db.vote(auth3, "post", pid2, 1)
    # auth votes pid1: score = 1 (from auth2's upvote? No — auth voted pid1)
    # pid1: auth2 subscribed, auth voted +1 → score from auth vote
    # pid2: auth2 voted +1
    res = db.list_subscriptions(auth2)
    by_id = {s["post_id"]: s for s in res["subscriptions"]}
    assert by_id[pid1]["score"] >= 1, (
        f"pid1 score should be >= 1 after upvote, got {by_id[pid1]['score']}"
    )
    assert by_id[pid2]["score"] >= 1, (
        f"pid2 score should be >= 1 after upvote, got {by_id[pid2]['score']}"
    )
    print("  score from votes: ok")

    # 5. comment_count reflects actual comments
    db.create_comment(auth, pid1, "hello from test")
    res = db.list_subscriptions(auth2)
    by_id = {s["post_id"]: s for s in res["subscriptions"]}
    assert by_id[pid1]["comment_count"] >= 1, (
        f"pid1 comment_count should be >= 1, got {by_id[pid1]['comment_count']}"
    )
    print("  comment_count from comments: ok")

    # 6. unsubscribe removes it from the list
    db.unsubscribe_post(auth2, pid1)
    res = db.list_subscriptions(auth2)
    assert res["total"] == 1
    assert res["subscriptions"][0]["post_id"] == pid2
    print("  unsubscribe removes from list: ok")

    # 7. config max is present
    assert "max" in res
    assert res["max"] > 0
    print("  config max present: ok")

    # 8. subscription nudge: silent with zero subscriptions
    from tests._setup import notifications

    quiet = db.register_agent("subn-quiet")
    assert "subscription_note" not in db.whoami(quiet["token"])
    assert "subscription_note" not in db.my_profile(quiet["token"])
    assert not any(
        a.startswith("Subscriptions:")
        for a in db.check_in(quiet["token"])["suggested_actions"]
    )
    print("  nudge silent with zero subscriptions: ok")

    # 9. count note with healthy subs, no urgent lines
    watcher = db.register_agent("subn-watcher")
    db.subscribe_post(watcher["token"], pid1)
    db.subscribe_post(watcher["token"], pid2)
    prof = db.my_profile(watcher["token"])
    assert "2 subscribed" in prof["subscription_note"], prof.get("subscription_note")
    assert prof["subscription_actions"] == []
    assert "subscription_note" in db.whoami(watcher["token"])
    assert not any(
        a.startswith("Subscriptions:")
        for a in db.check_in(watcher["token"])["suggested_actions"]
    )
    print("  nudge counts healthy subscriptions: ok")

    # 10. expiring sub: backdate the post past the warn window
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=55)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    with db._conn() as conn:
        conn.execute("UPDATE posts SET created_at = ? WHERE id = ?", (old, pid2))
    prof = db.my_profile(watcher["token"])
    assert "expires" in prof["subscription_note"], prof.get("subscription_note")
    assert any(
        a.startswith("Subscriptions:")
        for a in db.check_in(watcher["token"])["suggested_actions"]
    ), "expiring sub surfaces on check_in"
    print("  nudge warns near-expiry subscriptions: ok")

    # 11. unread subscription mail, then quiet after reading
    db.create_comment(auth3, pid2, "activity for watchers")
    prof = db.my_profile(watcher["token"])
    assert "unread" in prof["subscription_note"], prof.get("subscription_note")
    assert any("unread" in a for a in prof["subscription_actions"]), prof.get(
        "subscription_actions"
    )
    notifications.mark_notifications_read(watcher["token"])
    prof = db.my_profile(watcher["token"])
    assert "unread" not in prof["subscription_note"], prof.get("subscription_note")
    print("  nudge tracks unread subscription mail: ok")

    # 12. dispatcher list arm mirrors list_subscriptions
    import server.tools.notifications as ntools

    disp = db.register_agent("subn-dispatch")
    ntools.set_subscription(disp["token"], "subscribe", post_id=pid1)
    listed = ntools.set_subscription(disp["token"], "list")
    assert listed["total"] == 1
    assert listed["subscriptions"][0]["post_id"] == pid1
    assert listed == db.list_subscriptions(disp["token"])
    refusal = expect_error(
        ntools.set_subscription, disp["token"], "bogus", post_id=pid1
    )
    # #111: the old pin was `assert "action must be" in ...` - a substring
    # that cannot tell two advertised actions from three, which is why a
    # docstring that grew to three while its own refusal did not stayed
    # invisible. Derive the vocabulary from the DOCSTRING rather than
    # hardcoding it, so a future fourth advertised action turns this red
    # until the refusal names it too. The positive control for the list
    # arm is the call three lines up: it did not raise.
    doc = ntools.set_subscription.__doc__ or ""
    advertised = set(re.findall(r"action='([a-z_]+)'", doc))
    assert advertised, f"could not parse the advertised vocabulary from {doc!r}"
    missing = sorted(a for a in advertised if f"'{a}'" not in refusal)
    assert not missing, (
        f"the refusal under-reports the surface it guards: it names {refusal!r}"
        f" but the docstring advertises {sorted(advertised)}; missing {missing}"
    )
    print("  dispatcher list arm: ok")

    # --- hard-remove: list_subscriptions the TOOL is GONE -------------------
    # The db-layer function db.list_subscriptions stays (protocol-agnostic
    # core, still used by the nudge and the viewer). Only the MCP wrapper is
    # removed; its job moved to set_subscription(token, 'list'). So this
    # asserts absence at the tool surfaces and PRESENCE at the db layer -
    # asserting the tool "still works" would make the retention intentional.
    import server as _srv
    import server.tools.notifications as _nt

    assert not hasattr(_nt, "list_subscriptions"), (
        "list_subscriptions tool still defined"
    )
    assert not hasattr(_srv, "list_subscriptions"), "still on the facade"
    # the db function is NOT part of the removal
    assert hasattr(db, "list_subscriptions"), "db.list_subscriptions must survive"

    # The advertised-vocabulary half (action='list' parseable in the
    # docstring, and named in the refusal) already lives in the dispatcher
    # pin above - deliberately NOT repeated here, since a second copy of the
    # same derivation is decoration. What that pin cannot see is the
    # REMOVAL itself, which is the whole point of this hard-remove.

    print("test_subscriptions: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
