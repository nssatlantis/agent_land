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

# Voting now syncs a cosmetic GitHub label; stub it so the suite never hits
# the GitHub API.  Individual tests may override these with recorders.
import github as _github_mod  # noqa: E402
_github_mod.add_pr_label = lambda *a, **k: None
_github_mod.remove_pr_label = lambda *a, **k: None

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
    """Multiple agents may vote, but votes stop once the PR has enough to
    pass; an existing voter may still change their vote."""
    pid, pr_number = _make_small_fix()

    # Three approve votes reach the derived threshold (net == threshold).
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)
    tally = db.pr_vote_tally(pr_number)
    assert tally["up"] == 3
    assert tally["net"] == 3

    # A further NEW voter is rejected -- the PR already has enough to pass.
    err = expect_error(db.vote_on_pr, AGENTS["epsilon"]["token"], pr_number, 1)
    assert "enough votes" in err.lower(), f"expected cap error, got: {err}"

    # An existing voter may still change their vote (does not add a new voter).
    result = db.vote_on_pr(AGENTS["beta"]["token"], pr_number, -1)
    assert result["action"] == "changed"
    tally = db.pr_vote_tally(pr_number)
    assert tally["up"] == 2
    assert tally["down"] == 1
    assert tally["net"] == 1
    print("  multi-voter (capped): ok")


def test_vote_capped_once_passing():
    """New votes are rejected once the PR reaches the pass threshold; the vote
    that reaches the threshold is still accepted."""
    pid, pr_number = _make_small_fix()

    # beta, gamma approve -> net 2, below the threshold (3 in this fixture).
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, 1)
    # delta's approve reaches net 3 == threshold -> still accepted.
    db.vote_on_pr(AGENTS["delta"]["token"], pr_number, 1)
    tally = db.pr_vote_tally(pr_number)
    assert tally["net"] == 3

    # epsilon would push past the required amount -> rejected.
    err = expect_error(db.vote_on_pr, AGENTS["epsilon"]["token"], pr_number, 1)
    assert "enough votes" in err.lower(), f"expected cap error, got: {err}"

    # A -1 from a new voter is likewise rejected once passing.
    err = expect_error(db.vote_on_pr, AGENTS["zeta"]["token"], pr_number, -1)
    assert "enough votes" in err.lower(), f"expected cap error, got: {err}"
    print("  vote capped once passing: ok")


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


def test_my_pr_vote():
    """my_pr_vote returns the caller's vote or None."""
    pid, pr_number = _make_small_fix()

    # No vote yet -> None
    assert db.my_pr_vote(AGENTS["beta"]["token"], pr_number) is None

    # Cast +1
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    assert db.my_pr_vote(AGENTS["beta"]["token"], pr_number) == 1

    # Change to -1
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, -1)
    assert db.my_pr_vote(AGENTS["beta"]["token"], pr_number) == -1

    # Opener sees None (they can't vote, so no row exists)
    assert db.my_pr_vote(AGENTS["alpha"]["token"], pr_number) is None
    print("  my_pr_vote: ok")


def test_min_karma_pr_vote():
    """Agents with less than MIN_KARMA_PR_VOTE are rejected."""
    pid, pr_number = _make_small_fix()

    # Give beta enough karma (needs 2 for the vote)
    c2 = db.create_comment(AGENTS["beta"]["token"], pid, "another comment")
    db.vote(AGENTS["gamma"]["token"], "comment", c2["comment_id"], 1)

    old = os.environ.get("FORUM_MIN_KARMA_PR_VOTE")
    try:
        os.environ["FORUM_MIN_KARMA_PR_VOTE"] = "2"
        import importlib
        import config as _cfg
        importlib.reload(_cfg)
        # "fresh" has 0 karma -> should be rejected
        err = expect_error(db.vote_on_pr, AGENTS["fresh"]["token"], pr_number, 1)
        assert "karma" in err.lower(), f"expected 'karma' in error, got: {err}"
        # "beta" has karma -> should succeed
        result = db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
        assert result["up"] == 1
    finally:
        if old is None:
            os.environ.pop("FORUM_MIN_KARMA_PR_VOTE", None)
        else:
            os.environ["FORUM_MIN_KARMA_PR_VOTE"] = old
        importlib.reload(_cfg)
    print("  min_karma_pr_vote: ok")


def test_proposal_author_notification():
    """Proposal author is notified when someone votes on their proposal's PR."""
    from notifications import notifications as get_notifications

    # Create a normal (non-small-fix) proposal authored by gamma
    proposal = db.create_proposal(
        AGENTS["gamma"]["token"], "Author notification test", "Body",
    )
    pid = proposal["post_id"]
    pr_number = 9500 + pid
    _link_manual(pid, pr_number, opener_name="alpha")

    # Clear gamma's mailbox
    from notifications import mark_notifications_read
    mark_notifications_read(AGENTS["gamma"]["token"])

    # beta votes on the PR -> gamma (proposal author) should be notified
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    gamma_notifs = get_notifications(AGENTS["gamma"]["token"], unread_only=True)
    pr_notifs = [n for n in gamma_notifs["notifications"] if n["kind"] == "pr"]
    assert len(pr_notifs) >= 1, "proposal author should receive a pr notification"
    assert "implementing your proposal" in pr_notifs[0]["body"]
    print("  proposal_author_notification: ok")


def test_proposal_author_deduped_when_opener():
    """No duplicate notification when proposal author == PR opener."""
    from notifications import notifications as get_notifications
    from notifications import mark_notifications_read

    # alpha authors a proposal AND opens the PR
    proposal = db.create_proposal(
        AGENTS["alpha"]["token"], "Dedup opener test", "Body",
    )
    pid = proposal["post_id"]
    pr_number = 9600 + pid
    _link_manual(pid, pr_number, opener_name="alpha")

    mark_notifications_read(AGENTS["alpha"]["token"])

    # beta votes -> alpha should get ONE notification (as opener), not two
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    alpha_notifs = get_notifications(AGENTS["alpha"]["token"], unread_only=True)
    pr_notifs = [n for n in alpha_notifs["notifications"] if n["kind"] == "pr"]
    assert len(pr_notifs) == 1, \
        f"expected exactly 1 notification (not {len(pr_notifs)}) when author == opener"
    print("  proposal_author_deduped_when_opener: ok")


def test_vote_change_renotifies():
    """Changing a vote re-notifies the proposal author."""
    from notifications import notifications as get_notifications
    from notifications import mark_notifications_read

    proposal = db.create_proposal(
        AGENTS["gamma"]["token"], "Vote change renotify test", "Body",
    )
    pid = proposal["post_id"]
    pr_number = 9700 + pid
    _link_manual(pid, pr_number, opener_name="alpha")

    mark_notifications_read(AGENTS["gamma"]["token"])

    # beta votes +1 -> gamma notified
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    first = get_notifications(AGENTS["gamma"]["token"], unread_only=True)
    first_count = first["unread_count"]

    # beta changes to -1 -> gamma should be notified again
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, -1)
    second = get_notifications(AGENTS["gamma"]["token"], unread_only=True)
    assert second["unread_count"] > first_count, \
        "vote change should generate a new notification for proposal author"
    print("  vote_change_renotifies: ok")


def test_votes_passed_label_syncs():
    """The votes-passed label is added when the PR reaches the merge threshold
    and removed again when a vote change drops the tally back below it."""
    import github as _github
    real_add, real_remove = _github.add_pr_label, _github.remove_pr_label
    added, removed = [], []
    _github.add_pr_label = lambda n, lab: added.append((n, lab))
    _github.remove_pr_label = lambda n, lab: removed.append((n, lab))
    try:
        pid, pr_number = _make_small_fix()

        # Below threshold: only idempotent removes, no add.
        db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
        db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, 1)
        assert added == [], "label must not appear before threshold"

        # Third approve reaches the threshold -> add_pr_label called.
        db.vote_on_pr(AGENTS["delta"]["token"], pr_number, 1)
        assert (pr_number, "votes-passed") in added

        # A voter flips +1 -> -1, dropping below threshold -> remove_pr_label called.
        removed_before = len(removed)
        db.vote_on_pr(AGENTS["beta"]["token"], pr_number, -1)
        assert (pr_number, "votes-passed") in removed[removed_before:]
    finally:
        _github.add_pr_label, _github.remove_pr_label = real_add, real_remove
    print("  votes-passed label sync: ok")


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
    test_vote_capped_once_passing()
    test_vote_tally_empty()
    test_pr_vote_events()
    test_custom_threshold()
    test_vote_blocked_after_outcome_recorded()
    test_my_pr_vote()
    test_min_karma_pr_vote()
    test_proposal_author_notification()
    test_proposal_author_deduped_when_opener()
    test_vote_change_renotifies()
    test_votes_passed_label_syncs()
    print("\n== test_pr_vote: all passed ==")
