"""Tests for the PR voting system (db._pr_vote)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_pr_vote_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, expect_error, setup  # noqa: E402
import events  # noqa: E402

# -- shared setup: agents + base post --
AGENTS, _ = setup()

# -- helpers --

def _link_manual(proposal_id, pr_number, opener_name="alpha"):
    """Manually link a PR to a proposal for testing (bypasses GitHub)."""
    db.link_pr_to_proposal(pr_number, proposal_id, AGENTS[opener_name]["agent_id"])


def _make_small_fix(opener_name="alpha"):
    """Create a small-fix proposal and return (proposal_id, pr_number)."""
    proposal = db.create_proposal(
        AGENTS[opener_name]["token"],
        f"Fix thing {proposal_id_counter[0]}",
        "Body",
        small_fix=True,
    )
    proposal_id_counter[0] += 1
    pid = proposal["post_id"]
    pr_number = 9000 + pid
    _link_manual(pid, pr_number, opener_name)
    return pid, pr_number


proposal_id_counter = [0]

# -- tests --

def test_pr_vote_schema():
    """pr_votes table exists with correct structure."""
    with db._conn() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()}
    assert "pr_votes" in tables, "pr_votes table missing"
    print("  pr_votes schema: ok")


def test_vote_on_pr_happy():
    """Basic PR voting: cast, tally, change."""
    pid, pr_number = _make_small_fix()

    # beta votes +1
    result = db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    assert result["up"] == 1
    assert result["down"] == 0
    assert result["net"] == 1
    assert result["action"] == "cast"

    # gamma votes +1
    result = db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, 1)
    assert result["up"] == 2
    assert result["net"] == 2

    # delta votes -1
    result = db.vote_on_pr(AGENTS["delta"]["token"], pr_number, -1)
    assert result["up"] == 2
    assert result["down"] == 1
    assert result["net"] == 1

    # Verify tally
    tally = db.pr_vote_tally(pr_number)
    assert tally["up"] == 2
    assert tally["down"] == 1
    assert tally["net"] == 1
    assert len(tally["voters"]) == 3
    print("  vote_on_pr happy: ok")


def test_vote_on_pr_change():
    """Changing a vote replaces the earlier one."""
    pid, pr_number = _make_small_fix()

    # beta votes +1
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    tally = db.pr_vote_tally(pr_number)
    assert tally["net"] == 1

    # beta changes to -1
    result = db.vote_on_pr(AGENTS["beta"]["token"], pr_number, -1)
    assert result["action"] == "changed"
    assert result["value"] == -1
    tally = db.pr_vote_tally(pr_number)
    assert tally["net"] == -1
    print("  vote_on_pr change: ok")


def test_vote_on_pr_self_vote():
    """PR opener cannot vote on their own PR."""
    pid, pr_number = _make_small_fix()

    err = expect_error(db.vote_on_pr, AGENTS["alpha"]["token"], pr_number, 1)
    assert "own" in err.lower()
    print("  vote_on_pr self-vote: ok")


def test_vote_on_pr_invalid_value():
    """Vote value must be +1 or -1."""
    pid, pr_number = _make_small_fix()

    err = expect_error(db.vote_on_pr, AGENTS["beta"]["token"], pr_number, 0)
    assert "1" in err
    err = expect_error(db.vote_on_pr, AGENTS["beta"]["token"], pr_number, 2)
    assert "1" in err
    print("  vote_on_pr invalid value: ok")


def test_vote_on_pr_same_vote():
    """Casting the same vote twice is an error."""
    pid, pr_number = _make_small_fix()

    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    err = expect_error(db.vote_on_pr, AGENTS["beta"]["token"], pr_number, 1)
    assert "already" in err.lower()
    print("  vote_on_pr same vote: ok")


def test_eligible_for_merge():
    """PR is eligible for merge when net >= threshold."""
    pid, pr_number = _make_small_fix()

    with db._conn() as conn:
        assert not db.pr_eligible_for_merge(conn, pr_number)

    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    with db._conn() as conn:
        assert not db.pr_eligible_for_merge(conn, pr_number)  # net=1, threshold=2

    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, 1)
    with db._conn() as conn:
        assert not db.pr_eligible_for_merge(conn, pr_number)  # net=2, dynamic threshold=3

    db.vote_on_pr(AGENTS["delta"]["token"], pr_number, 1)
    with db._conn() as conn:
        assert db.pr_eligible_for_merge(conn, pr_number)  # net=3, dynamic threshold=3
    print("  eligible_for_merge: ok")


def test_eligible_for_decline():
    """PR is eligible for decline when net <= -threshold."""
    pid, pr_number = _make_small_fix()

    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, -1)
    with db._conn() as conn:
        assert not db.pr_eligible_for_decline(conn, pr_number)  # net=-1

    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, -1)
    with db._conn() as conn:
        assert not db.pr_eligible_for_decline(conn, pr_number)  # net=-2, dynamic threshold=3

    db.vote_on_pr(AGENTS["delta"]["token"], pr_number, -1)
    with db._conn() as conn:
        assert db.pr_eligible_for_decline(conn, pr_number)  # net=-3, dynamic threshold=3
    print("  eligible_for_decline: ok")


def test_multi_voter():
    """Multiple agents can vote on the same PR."""
    pid, pr_number = _make_small_fix()

    for name in ("beta", "gamma", "delta", "epsilon", "zeta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)
    tally = db.pr_vote_tally(pr_number)
    assert tally["up"] == 5
    assert tally["net"] == 5
    print("  multi-voter: ok")


def test_vote_tally_empty():
    """Tally for a PR with no votes returns zeros."""
    tally = db.pr_vote_tally(99999)
    assert tally["up"] == 0
    assert tally["down"] == 0
    assert tally["net"] == 0
    assert tally["voters"] == []
    print("  tally empty: ok")


def test_pr_vote_events():
    """Voting logs the correct event kinds."""
    pid, pr_number = _make_small_fix()

    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    evts = events.query_events(kind="pr_vote_cast", target_id=pr_number)
    assert len(evts) == 1
    assert evts[0]["actor_agent_id"] == AGENTS["beta"]["agent_id"]

    # Change vote
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, -1)
    evts = events.query_events(kind="pr_vote_changed", target_id=pr_number)
    assert len(evts) == 1
    print("  pr_vote events: ok")


def test_custom_threshold():
    """Eligibility checks respect custom threshold parameter."""
    pid, pr_number = _make_small_fix()

    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    with db._conn() as conn:
        # With threshold=1, net=1 is enough
        assert db.pr_eligible_for_merge(conn, pr_number, threshold=1)
        # With threshold=3, net=1 is not enough
        assert not db.pr_eligible_for_merge(conn, pr_number, threshold=3)

    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, -1)
    with db._conn() as conn:
        # net=0, threshold=1 for decline
        assert not db.pr_eligible_for_decline(conn, pr_number, threshold=1)
    print("  custom threshold: ok")


def test_vote_blocked_after_outcome_recorded():
    """Voting is blocked once proposal_outcomes records 'merged' (before pr_merges)."""
    pid, pr_number = _make_small_fix()

    # Record the proposal outcome as "merged" — this is what the outcome
    # poller does BEFORE awarding pr_merges karma, creating a window where
    # pr_merges has no row yet.
    db.record_proposal_outcome(pr_number, pid, "merged", "2026-08-20T00:00:00.000Z")

    # Attempting to vote should be blocked by the proposal_outcomes check.
    err = expect_error(db.vote_on_pr, AGENTS["beta"]["token"], pr_number, 1)
    assert "decided" in err.lower(), f"expected 'decided' in error, got: {err}"
    print("  vote blocked after outcome recorded: ok")


# -- run all --
if __name__ == "__main__":
    test_pr_vote_schema()
    test_vote_on_pr_happy()
    test_vote_on_pr_change()
    test_vote_on_pr_self_vote()
    test_vote_on_pr_invalid_value()
    test_vote_on_pr_same_vote()
    test_eligible_for_merge()
    test_eligible_for_decline()
    test_multi_voter()
    test_vote_tally_empty()
    test_pr_vote_events()
    test_custom_threshold()
    test_vote_blocked_after_outcome_recorded()
    print("\n== test_pr_vote: all passed ==")
