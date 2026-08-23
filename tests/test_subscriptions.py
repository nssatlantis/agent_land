"""Tests for post subscriptions — list_subscriptions functional test."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_subs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402


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

    print("test_subscriptions: all assertions passed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
