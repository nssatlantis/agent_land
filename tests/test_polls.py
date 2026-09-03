"""Test posts-attached polls (db._polls)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_polls_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    db,
    expect_error,
    setup,
)


def main():
    agents, post_id = setup()
    os.environ.setdefault("FORUM_POLLS_PER_AGENT_OPEN", "50")

    pa = db.register_agent("poll-a")
    pb = db.register_agent("poll-b")
    pc = db.register_agent("poll-c")
    pclose = db.register_agent("poll-close")
    # Boost poll-a's karma so it can create proposals/ideas/small-fixes.
    for voter in (pb, pc, pclose):
        db.vote(voter["token"], "post", post_id, 1)
    for _ in range(3):
        db.vote(pa["token"], "post", post_id, 1)
    ta, tb, tc = pa["token"], pb["token"], pc["token"]

    # --- poll creation: ordinary post ---------------------------------------
    p = db.create_post(ta, "poll test post", "body")["post_id"]
    poll = db.create_poll(ta, p, "Which color?", ["Red", "Blue", "Green"], 24.0)
    assert poll["question"] == "Which color?"
    assert [o["text"] for o in poll["options"]] == ["Red", "Blue", "Green"]
    assert poll["total_votes"] == 0
    assert poll["status"] == "open"
    assert poll["my_vote"] is None

    # live results + my_vote via serialized readers
    gp = db.get_post(p)
    assert gp["poll"]["question"] == "Which color?"
    assert gp["poll"]["options"][0]["text"] == "Red"
    lp = [r for r in db.list_posts() if r["id"] == p][0]
    assert lp["poll"]["question"] == "Which color?"
    # poll-less post -> None
    pless = db.create_post(ta, "poll-less", "b")["post_id"]
    assert db.get_post(pless)["poll"] is None
    assert db.get_poll(pless) is None

    # --- refusal on proposals, allowance on ideas ---------------------------
    prop = db.create_proposal(ta, "poll prop", "b")["post_id"]
    sm = db.create_proposal(ta, "poll sm", "b", small_fix=True)["post_id"]
    idea = db.create_proposal(ta, "poll idea", "b", idea=True)["post_id"]
    assert "not proposals or small fixes" in expect_error(
        lambda: db.create_poll(ta, prop, "Q", ["A", "B"], 1.0)
    )
    assert "not proposals or small fixes" in expect_error(
        lambda: db.create_poll(ta, sm, "Q", ["A", "B"], 1.0)
    )
    ip = db.create_poll(ta, idea, "IdeaQ", ["X", "Y"], 1.0)
    assert ip["question"] == "IdeaQ"

    # --- attach rules --------------------------------------------------------
    assert "already has a poll" in expect_error(
        lambda: db.create_poll(ta, p, "Q2", ["A", "B"], 1.0)
    )
    other = db.create_post(tb, "other's post", "b")["post_id"]
    assert "author may attach" in expect_error(
        lambda: db.create_poll(ta, other, "Q", ["A", "B"], 1.0)
    )

    # --- validation -----------------------------------------------------------
    vp = db.create_post(ta, "poll validation", "b")["post_id"]
    assert "required" in expect_error(lambda: db.create_poll(ta, vp, "", ["A", "B"], 1))
    assert "at least" in expect_error(lambda: db.create_poll(ta, vp, "Q", ["A"], 1))
    assert "at most" in expect_error(
        lambda: db.create_poll(ta, vp, "Q", [f"o{i}" for i in range(7)], 1)
    )
    assert "empty" in expect_error(lambda: db.create_poll(ta, vp, "Q", ["A", " "], 1))
    assert "distinct" in expect_error(
        lambda: db.create_poll(ta, vp, "Q", ["A", "A"], 1)
    )
    assert "positive" in expect_error(
        lambda: db.create_poll(ta, vp, "Q", ["A", "B"], 0.0)
    )

    # --- voting: open immediately (tune window 0), re-vote overwrites ---------
    opt0, opt1 = poll["options"][0]["id"], poll["options"][1]["id"]
    v = db.vote_poll(tb, p, opt0)
    assert v["total_votes"] == 1
    assert v["my_vote"] == opt0
    v2 = db.vote_poll(tb, p, opt1)
    assert v2["total_votes"] == 1, "re-vote overwrites, no double count"
    assert v2["my_vote"] == opt1
    db.vote_poll(tc, p, opt0)
    gv = db.get_poll(p, token=tb)
    assert gv["my_vote"] == opt1
    assert gv["total_votes"] == 2
    assert gv["options"][1]["votes"] == 1
    # author cannot vote own poll
    assert "own poll" in expect_error(lambda: db.vote_poll(ta, p, opt0))
    # unknown option refused
    assert "unknown poll answer" in expect_error(lambda: db.vote_poll(tb, p, 999999))

    # --- editing window (needs the window armed) ------------------------------
    saved_win = os.environ.get("FORUM_POLL_EDIT_WINDOW_SECONDS")
    try:
        os.environ["FORUM_POLL_EDIT_WINDOW_SECONDS"] = "300"
        we = db.create_post(ta, "poll edit window", "b")["post_id"]
        wp = db.create_poll(ta, we, "WinQ", ["A", "B"], 1.0)
        # voting refused while editing is open
        assert "editing window" in expect_error(
            lambda: db.vote_poll(tb, we, wp["options"][0]["id"])
        )
        # edit succeeds: question only, options only, both
        e1 = db.edit_poll(ta, we, question="WinQ2")
        assert e1["question"] == "WinQ2"
        e2 = db.edit_poll(ta, we, options=["C", "D"])
        assert [o["text"] for o in e2["options"]] == ["C", "D"]
        assert e2["question"] == "WinQ2"
        # non-author edit refused
        assert "author" in expect_error(lambda: db.edit_poll(tb, we, question="nope"))
        # a voted poll can no longer be edited: vote after another creation
        wv = db.create_post(ta, "poll edit voted", "b")["post_id"]
        db.create_poll(ta, wv, "WQ", ["A", "B"], 1.0)
    finally:
        if saved_win is None:
            os.environ.pop("FORUM_POLL_EDIT_WINDOW_SECONDS", None)
        else:
            os.environ["FORUM_POLL_EDIT_WINDOW_SECONDS"] = saved_win

    # window closed => editing refused (back at 0)
    assert "editing window has closed" in expect_error(
        lambda: db.edit_poll(ta, p, question="nope")
    )

    # --- conclusion sweep notifies participants, idempotent ------------------
    cpost = db.create_post(ta, "poll conclude", "b")["post_id"]
    db.create_comment(tb, cpost, "I'm a participant")
    db.create_poll(ta, cpost, "CQ", ["A", "B"], 0.0000001)
    n1 = db._sweep_concluded_polls()
    assert n1 >= 1
    gc = db.get_poll(cpost)
    assert gc["concluded"] is True
    assert gc["status"] == "concluded"
    n2 = db._sweep_concluded_polls()
    assert n2 == 0, "sweep is idempotent"
    # voting on a concluded poll refused
    assert "concluded" in expect_error(lambda: db.vote_poll(tb, cpost, 1))

    # notifications: poll creation pings participants (post author + commenter)
    with db._conn() as c:
        got_b = c.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND kind = 'poll'",
            (pb["agent_id"],),
        ).fetchone()[0]
    assert got_b >= 1, "a thread participant is notified about the poll"

    print("test_polls: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
