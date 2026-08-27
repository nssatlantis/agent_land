"""Tests for the collaborative-proposal settling window (maintainer issue
#451): a fresh collaborative proposal opens for development only once BOTH
its community vote has passed AND a short settling window (anchored on
posts.created_at, so it restarts per version) has elapsed - giving citizens
time to join and claim lists/items before anyone rushes a PR.

The gate lives in db.require_proposal_approval (cheap to enforce server-side
on the repo_propose_change path) and is collaborative-only: regular
proposals are not gated on the window at all.

Covers:
- pending vote + window active   -> refuse (the "vote hasn't passed" case)
- passed vote + window active    -> refuse (the "vote passed, window open" case)
- passed vote + window elapsed   -> allow (development opens)
- allow_pending is bypassed while the window is open (the time gate, not the
  WIP shortcut, governs a fresh collaborative proposal)
- a regular (non-collaborative) proposal is not window-gated
- my_proposals' status note says the window is open when the vote passed
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_settle_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_COLLAB_SETTLE_SECONDS"] = "3600"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, setup, expect_error, proposal_need  # noqa: E402

AGENTS, _BASE_POST = setup()

# A timestamp far in the past: older than any settling window, so a
# proposal backdated to it counts as fully elapsed.
_PAST = "2020-01-01T00:00:00.000Z"
_WINDOW = 3600


_counter = [0]


def _collab_proposal(opener="alpha"):
    """A fresh collaborative proposal with one to-do list (so it can be
    joined), made by alpha."""
    _counter[0] += 1
    p = db.create_proposal(
        AGENTS[opener]["token"], f"Settle test {_counter[0]}", "Body",
        collaborative=True,
    )
    pid = p["post_id"]
    db.set_todos_for_post(
        AGENTS[opener]["token"], pid,
        [{"title": "W", "items": [{"text": "task"}]}],
    )
    return pid


def _join_beta(pid):
    db.join_proposal(AGENTS["beta"]["token"], pid)


def _pass(pid):
    """Cast enough +1s from other citizens to clear the live bar."""
    need = proposal_need()
    for name in ("gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        if need <= 0:
            return
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
        need -= 1


def _backdate(pid):
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?", (_PAST, pid)
        )


def test_pending_vote_and_window_active_refuses():
    pid = _collab_proposal()
    _join_beta(pid)
    # Fresh proposal (window open), no votes yet.
    err = expect_error(
        db.require_proposal_approval,
        AGENTS["beta"]["token"], pid, "repo_propose_change",
    )
    assert "settling window" in (err or ""), err
    assert "vote hasn't passed" in err, err


def test_passed_vote_but_window_open_refuses():
    pid = _collab_proposal()
    _join_beta(pid)
    _pass(pid)
    # Vote passed, but the window (anchored at created_at) is still open.
    err = expect_error(
        db.require_proposal_approval,
        AGENTS["beta"]["token"], pid, "repo_propose_change",
    )
    assert "settling window" in (err or ""), err
    assert "vote has passed" in err, err


def test_passed_vote_and_window_elapsed_allows():
    pid = _collab_proposal()
    _join_beta(pid)
    _pass(pid)
    _backdate(pid)  # elapsed long past the window
    got = db.require_proposal_approval(
        AGENTS["beta"]["token"], pid, "repo_propose_change",
    )
    assert got == pid, "vote passed + window elapsed lets the PR open"


def test_allow_pending_bypassed_while_window_open():
    pid = _collab_proposal()
    _join_beta(pid)
    # Even allow_pending (the WIP shortcut) cannot open a PR on a fresh
    # collaborative proposal - the settling window is the time gate.
    err = expect_error(
        db.require_proposal_approval,
        AGENTS["beta"]["token"], pid, "repo_propose_change",
        allow_pending=True,
    )
    assert "settling window" in (err or ""), err


def test_regular_proposal_not_window_gated():
    pid = db.create_proposal(
        AGENTS["alpha"]["token"], "Regular settle", "Body",
    )["post_id"]
    # A regular proposal with a fresh created_at is NOT window-gated: it
    # opens under the plain hold path (allow_pending) instead.
    got = db.require_proposal_approval(
        AGENTS["alpha"]["token"], pid, "repo_propose_change",
        allow_pending=True,
    )
    assert got == pid, "regular proposals are not subject to the settling window"


def test_status_note_says_window_open_after_vote_passes():
    pid = _collab_proposal()
    _pass(pid)
    mine = db.my_proposals(AGENTS["alpha"]["token"])["proposals"]
    row = next(p for p in mine if p["id"] == pid)
    assert "settling window" in row["status"], row["status"]
    assert "approved" in row["status"], row["status"]


if __name__ == "__main__":
    test_pending_vote_and_window_active_refuses()
    print("  pending vote + window active refuses: ok")
    test_passed_vote_but_window_open_refuses()
    print("  passed vote + window open refuses: ok")
    test_passed_vote_and_window_elapsed_allows()
    print("  passed vote + window elapsed allows: ok")
    test_allow_pending_bypassed_while_window_open()
    print("  allow_pending bypassed while window open: ok")
    test_regular_proposal_not_window_gated()
    print("  regular proposal not window-gated: ok")
    test_status_note_says_window_open_after_vote_passes()
    print("  status note says window open after vote passes: ok")
    print("\n== test_settle_window: all passed ==")
