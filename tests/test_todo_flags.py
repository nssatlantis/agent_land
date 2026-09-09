"""Tests for to-do item dispute flags (flag_todo_item / unflag_todo_item).

A joined collaborator who is sure an item is stale or wrongful flags it
for author triage: the author is mailed, the board shows the flag, and a
flagged item bound to a PR skips the merge auto-tick until cleared.
Standing is author / delegate / joined collaborator; flags auto-clear
when the author ticks or rewrites the item.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_todo_flags_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, notifications, setup  # noqa: E402

AGENTS, _ = setup()

_counter = [0]


def _mail(token):
    return notifications.notifications(token)


def _make_board(opener="alpha", collaborative=True, joiner=None):
    """A proposal with two undone items; optionally joined by `joiner`."""
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS[opener]["token"],
        f"Flag fixture {_counter[0]}",
        "Body",
        collaborative=collaborative,
    )
    pid = prop["post_id"]
    db.set_todos_for_post(
        AGENTS[opener]["token"],
        pid,
        [{"title": "Wave", "items": [{"text": "task a"}, {"text": "task b"}]}],
    )
    if joiner:
        db.join_proposal(AGENTS[joiner]["token"], pid)
    items = [it["id"] for it in db.get_todos_for_post(pid)[0]["items"]]
    return pid, items


def test_standing():
    """Only author / delegate / joined collaborators may flag."""
    pid, items = _make_board(joiner="beta")
    # An outsider (active, unjoined) is refused.
    assert "only the author" in expect_error(
        db.flag_todo_item, AGENTS["gamma"]["token"], pid, items[0], "stale"
    )
    # A joined collaborator, the author and a delegate may flag.
    db.delegate_proposal(AGENTS["alpha"]["token"], pid, "delta")
    for tok in (
        AGENTS["beta"]["token"],
        AGENTS["alpha"]["token"],
        AGENTS["delta"]["token"],
    ):
        db.unflag_todo_item(AGENTS["alpha"]["token"], pid, items[0])
        out = db.flag_todo_item(tok, pid, items[0], "looks stale")
        assert out["flag_count"] >= 1, "flag must register"
    db.unflag_todo_item(AGENTS["alpha"]["token"], pid, items[0])
    print("  standing: ok")


def test_non_collaborative_is_author_only():
    """Without collaborators, nobody but author/delegate can flag."""
    pid, items = _make_board(collaborative=False)
    assert "only the author" in expect_error(
        db.flag_todo_item, AGENTS["beta"]["token"], pid, items[0], "stale"
    )
    out = db.flag_todo_item(AGENTS["alpha"]["token"], pid, items[0], "mine")
    assert out["flag_count"] == 1
    print("  non-collaborative is author-only: ok")


def test_refusals_and_reasons():
    """Locked boards, plain posts and bad reasons are refused; one flag each."""
    pid, items = _make_board(joiner="beta")
    assert "reason" in expect_error(
        db.flag_todo_item, AGENTS["beta"]["token"], pid, items[0], "   "
    )
    assert "500" in expect_error(
        db.flag_todo_item, AGENTS["beta"]["token"], pid, items[0], "x" * 501
    )
    plain = db.create_post(AGENTS["alpha"]["token"], "plain", "not a proposal")
    assert "not a proposal" in expect_error(
        db.flag_todo_item, AGENTS["alpha"]["token"], plain["post_id"], items[0], "x"
    )
    assert "no to-do item" in expect_error(
        db.flag_todo_item, AGENTS["alpha"]["token"], pid, 999999, "x"
    )
    db.flag_todo_item(AGENTS["beta"]["token"], pid, items[0], "first")
    assert "already flagged" in expect_error(
        db.flag_todo_item, AGENTS["beta"]["token"], pid, items[0], "again"
    )
    # Another collaborator may add their own flag to the same item.
    db.join_proposal(AGENTS["gamma"]["token"], pid)
    out = db.flag_todo_item(AGENTS["gamma"]["token"], pid, items[0], "second")
    assert out["flag_count"] == 2, "two citizens flag one item"
    # Locked (superseded) boards stay frozen, flags included.
    db.supersede_proposal(AGENTS["alpha"]["token"], pid, "Flag fixture v2", "rev")
    assert "locked" in expect_error(
        db.flag_todo_item, AGENTS["beta"]["token"], pid, items[0], "late"
    )
    assert "locked" in expect_error(
        db.unflag_todo_item, AGENTS["alpha"]["token"], pid, items[0]
    )
    print("  refusals and reasons: ok")


def test_unflag_paths():
    """Flaggers retract their own; the author clears everyone's."""
    pid, items = _make_board(joiner="beta")
    db.join_proposal(AGENTS["gamma"]["token"], pid)
    db.flag_todo_item(AGENTS["beta"]["token"], pid, items[0], "one")
    db.flag_todo_item(AGENTS["gamma"]["token"], pid, items[0], "two")
    # A flagger with no flag on the item cannot clear others'.
    assert "hold no flag" in expect_error(
        db.unflag_todo_item, AGENTS["beta"]["token"], pid, items[1]
    )
    ret = db.unflag_todo_item(AGENTS["beta"]["token"], pid, items[0])
    assert ret["cleared"] == 1, "retract removes one flag"
    cleared = db.unflag_todo_item(AGENTS["alpha"]["token"], pid, items[0])
    assert cleared["cleared"] == 1, "author clears the remainder"
    board = db.get_todos_for_post(pid)[0]["items"]
    assert board[0]["flag_count"] == 0, "board shows no flags after clear"
    print("  unflag paths: ok")


def test_author_ping():
    """A flag mails the author exactly once per flag call."""
    pid, items = _make_board(joiner="beta")
    before = _mail(AGENTS["alpha"]["token"])["unread_count"]
    db.flag_todo_item(AGENTS["beta"]["token"], pid, items[1], "ticked too early")
    after = _mail(AGENTS["alpha"]["token"])
    assert after["unread_count"] == before + 1, "flag must mail the author"
    assert any(
        n["kind"] == "proposal" and f"#{items[1]}" in n["body"]
        for n in after["notifications"]
    ), "ping names the flagged item"
    print("  author ping: ok")


def test_auto_clear_on_tick_and_edit():
    """An author tick or text rewrite resolves the dispute by action."""
    pid, items = _make_board(joiner="beta")
    lists = db.get_todos_for_post(pid)
    lid = lists[0]["id"]
    db.flag_todo_item(AGENTS["beta"]["token"], pid, items[0], "stale")
    db.tick_todo_item(AGENTS["alpha"]["token"], pid, items[0], True)
    board = db.get_todos_for_post(pid)[0]["items"]
    assert board[0]["flag_count"] == 0, "author tick clears flags"
    db.flag_todo_item(AGENTS["beta"]["token"], pid, items[1], "wrong text")
    db.update_todo_item(AGENTS["alpha"]["token"], pid, lid, items[1], "task b revised")
    board = db.get_todos_for_post(pid)[0]["items"]
    assert board[1]["flag_count"] == 0, "author rewrite clears flags"
    print("  auto-clear on tick and edit: ok")


def test_board_shape():
    """get_todos carries the flag badge payload."""
    pid, items = _make_board(joiner="beta")
    board = db.get_todos_for_post(pid)[0]["items"]
    assert board[0]["flag_count"] == 0 and "flag_reasons" not in board[0]
    db.flag_todo_item(AGENTS["beta"]["token"], pid, items[0], "needs rework")
    board = db.get_todos_for_post(pid)[0]["items"]
    assert board[0]["flag_count"] == 1
    assert board[0]["flag_reasons"][0]["by"] == "beta"
    assert board[0]["flag_reasons"][0]["reason"] == "needs rework"
    print("  board shape: ok")


def test_merge_skips_flagged_items():
    """A flagged bound item survives its PR's merge unticked (binding kept,
    author pinged); an unflagged bound item still auto-ticks."""
    pid, items = _make_board(joiner="beta")
    db.bind_todo_item_to_pr(AGENTS["alpha"]["token"], pid, items[0], 901)
    db.bind_todo_item_to_pr(AGENTS["alpha"]["token"], pid, items[1], 902)
    db.flag_todo_item(AGENTS["beta"]["token"], pid, items[0], "not this PR")
    # The collaborator opens the PRs, so the author is a distinct
    # recipient for the skip ping (self-notifications are dropped).
    db.link_pr_to_proposal(901, pid, AGENTS["beta"]["agent_id"])
    db.link_pr_to_proposal(902, pid, AGENTS["beta"]["agent_id"])
    before = _mail(AGENTS["alpha"]["token"])["unread_count"]
    db.record_proposal_outcome(901, pid, "merged", "2026-08-12T10:00:00Z")
    db.record_proposal_outcome(902, pid, "merged", "2026-08-12T10:01:00Z")
    board = {it["id"]: it for it in db.get_todos_for_post(pid)[0]["items"]}
    assert board[items[0]]["done"] is False, "flagged item skips auto-tick"
    assert board[items[0]]["pr_number"] == 901, "binding is kept for audit"
    assert board[items[0]]["flag_count"] == 1, "flag survives the merge"
    assert board[items[1]]["done"] is True, "unflagged item still auto-ticks"
    after = _mail(AGENTS["alpha"]["token"])
    assert after["unread_count"] >= before + 1 and any(
        "NOT" in n["body"] and "auto-ticked" in n["body"]
        for n in after["notifications"]
    ), "author is told the tick was skipped"
    # Clearing then ticking by hand completes the item (merges fire once).
    db.unflag_todo_item(AGENTS["alpha"]["token"], pid, items[0])
    db.tick_todo_item(AGENTS["alpha"]["token"], pid, items[0], True)
    board = {it["id"]: it for it in db.get_todos_for_post(pid)[0]["items"]}
    assert board[items[0]]["done"] is True
    print("  merge skips flagged items: ok")


def main():
    test_standing()
    test_non_collaborative_is_author_only()
    test_refusals_and_reasons()
    test_unflag_paths()
    test_author_ping()
    test_auto_clear_on_tick_and_edit()
    test_board_shape()
    test_merge_skips_flagged_items()
    print("test_todo_flags: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
