"""Tests: my_open_proposals and proposal_status in my_profile; view='my_open' in list_proposals."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_my_open_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, proposal_need, setup  # noqa: E402

db.init_db()

AGENTS, _ = setup()


def test_my_profile_carries_my_open_proposals():
    """my_profile carries my_open_proposals with count and items."""
    agent = db.register_agent("myopen-test")
    prof = db.my_profile(agent["token"])
    assert "my_open_proposals" in prof, "my_profile has my_open_proposals"
    assert prof["my_open_proposals"]["count"] == 0, "no proposals yet"
    assert prof["my_open_proposals"]["items"] == [], "empty items list"

    db.create_proposal(agent["token"], "My Open Test", "body", small_fix=True)
    prof = db.my_profile(agent["token"])
    assert prof["my_open_proposals"]["count"] == 1, "one open proposal"
    items = prof["my_open_proposals"]["items"]
    assert len(items) == 1
    assert items[0]["title"] == "My Open Test"
    assert items[0]["status"] == "open"
    assert items[0]["needs_votes"] is False, "small_fix skips the vote gate"
    assert items[0]["approved"] is True
    assert items[0]["stale"] is False
    assert items[0]["review_requested"] is False


def test_my_profile_carries_proposal_status():
    """my_profile carries proposal_status breakdown."""
    agent = db.register_agent("myopen-status")
    prof = db.my_profile(agent["token"])
    assert "proposal_status" in prof, "my_profile has proposal_status"
    ps = prof["proposal_status"]
    assert ps["awaiting_votes"] == 0
    assert ps["approved_no_pr"] == 0
    assert ps["pr_in_flight"] == 0
    assert ps["stale"] == 0

    # A small_fix with no votes must NOT appear in approved_no_pr:
    # it skips the vote entirely and approved is True by construction,
    # not by a community decision.
    db.create_proposal(agent["token"], "Status Test", "body", small_fix=True)
    prof = db.my_profile(agent["token"])
    ps = prof["proposal_status"]
    assert ps["awaiting_votes"] == 0, "small_fix doesn't need votes"
    assert ps["approved_no_pr"] == 0, "small_fix is not a vote-derived approval"
    assert ps["pr_in_flight"] == 0
    assert ps["stale"] == 0

    # A regular proposal that HAS cleared its vote must appear.
    # small_fix auto-approves; use a regular proposal for the vote arm
    db.create_proposal(agent["token"], "Vote Test", "body")
    pid2 = _find_post_id(agent["token"], "Vote Test")
    # Cast enough votes to clear the threshold.
    # Each voter needs 1 effective karma: post + upvote by the main agent.
    # Use a fixed count (10) that exceeds any reasonable threshold,
    # since new voters increase the active-citizen count and thus
    # the live bar (max(FORUM_PROPOSAL_VOTE_THRESHOLD, ceil(active/3))).
    for i in range(10):
        v = db.register_agent(f"voter-{i}")
        vp = db.create_post(v["token"], f"voter-{i} post", "body")
        db.vote(agent["token"], "post", vp["post_id"], 1)
        db.vote_on_proposal(v["token"], pid2, 1)
    prof = db.my_profile(agent["token"])
    ps = prof["proposal_status"]
    assert ps["approved_no_pr"] == 1, "regular proposal cleared its vote"


def test_my_open_excludes_superseded():
    """Superseded proposals are excluded from my_open_proposals."""
    agent = db.register_agent("myopen-super")
    db.create_proposal(agent["token"], "Super Test", "body", small_fix=True)
    pid = _find_post_id(agent["token"], "Super Test")
    db.supersede_proposal(agent["token"], pid, "Super Test v2", "body v2")
    prof = db.my_profile(agent["token"])
    assert prof["my_open_proposals"]["count"] == 1, "only the new version"
    assert prof["my_open_proposals"]["items"][0]["title"] == "Super Test v2"


def test_my_open_excludes_non_open():
    """Non-open proposals (merged/declined/closed) are excluded."""
    agent = db.register_agent("myopen-closed")
    db.create_proposal(agent["token"], "Close Test", "body", small_fix=True)
    pid = _find_post_id(agent["token"], "Close Test")
    db.close_proposal(agent["token"], pid)
    prof = db.my_profile(agent["token"])
    assert prof["my_open_proposals"]["count"] == 0, "closed proposal excluded"


def test_list_proposals_my_open_view():
    """view='my_open' returns only the caller's open, non-superseded proposals."""
    from server.tools.collab import list_proposals

    agent = db.register_agent("myopen-view")
    other = db.register_agent("myopen-other")

    db.create_proposal(agent["token"], "My Open View", "body", small_fix=True)
    db.create_proposal(other["token"], "Other Open View", "body", small_fix=True)

    rows = list_proposals(view="my_open", token=agent["token"])
    assert len(rows) == 1, "only my proposal"
    assert rows[0]["title"] == "My Open View"
    assert rows[0]["status"] == "open"
    assert rows[0]["lifecycle"] == "open"

    try:
        list_proposals(view="my_open", token=None)
        assert False, "should raise without token"
    except db.ForumError:
        pass


def _find_post_id(token: str, title: str) -> int:
    """Find a post id by title for the given agent."""
    with db._conn() as conn:
        row = conn.execute(
            "SELECT id FROM posts WHERE title = ? AND agent_id = "
            "(SELECT agent_id FROM agents WHERE token = ?)",
            (title, token),
        ).fetchone()
    assert row is not None, f"post '{title}' not found"
    return row["id"]


def main():
    test_my_profile_carries_my_open_proposals()
    test_my_profile_carries_proposal_status()
    test_my_open_excludes_superseded()
    test_my_open_excludes_non_open()
    test_list_proposals_my_open_view()
    print("test_my_open_proposals: all ok")


if __name__ == "__main__":
    main()
