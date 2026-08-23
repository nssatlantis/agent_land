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
_github_mod.list_pr_labels = lambda *a, **k: []

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
    that reaches the threshold is still accepted.  The response now carries
    threshold and eligible_for_merge for agent visibility."""
    pid, pr_number = _make_small_fix()

    # beta, gamma approve -> net 2, below the threshold (3 in this fixture).
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, 1)
    # delta's approve reaches net 3 == threshold -> still accepted.
    result = db.vote_on_pr(AGENTS["delta"]["token"], pr_number, 1)
    assert result["net"] == 3
    assert result["threshold"] == 3
    assert result["eligible_for_merge"] is True

    # epsilon would push past the required amount -> rejected.
    err = expect_error(db.vote_on_pr, AGENTS["epsilon"]["token"], pr_number, 1)
    assert "enough votes" in err.lower(), f"expected cap error, got: {err}"

    # A -1 from a new voter IS still allowed (oppose votes past threshold).
    result = db.vote_on_pr(AGENTS["zeta"]["token"], pr_number, -1)
    assert result["action"] == "cast"
    tally = db.pr_vote_tally(pr_number)
    assert tally["up"] == 3
    assert tally["down"] == 1
    assert tally["net"] == 2
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
    """The dynamic vote-tally label tracks the current up/down count and
    colour-codes it by net score and merge eligibility."""
    import github as _github
    real_add, real_remove, real_list = (
        _github.add_pr_label,
        _github.remove_pr_label,
        _github.list_pr_labels,
    )
    added: list[tuple[int, str, str | None]] = []
    removed: list[tuple[int, str]] = []
    # Simulate a live label set: starts empty, tracks adds/removes.
    live_labels: dict[int, list[str]] = {}

    def _fake_list(n: int) -> list[str]:
        return list(live_labels.get(n, []))

    def _fake_add(n: int, lab: str, color: str | None = None) -> None:
        added.append((n, lab, color))
        live_labels.setdefault(n, []).append(lab)

    def _fake_remove(n: int, lab: str) -> None:
        removed.append((n, lab))
        labels = live_labels.get(n, [])
        if lab in labels:
            labels.remove(lab)

    _github.add_pr_label = _fake_add
    _github.remove_pr_label = _fake_remove
    _github.list_pr_labels = _fake_list
    try:
        pid, pr_number = _make_small_fix()

        # First vote (1-0): below threshold -> label added with green colour.
        db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
        assert added[-1] == (pr_number, "votes: [+1 | -0]", "0d6838")

        # Second vote (2-0): still below threshold -> old label removed, new added.
        db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, 1)
        assert (pr_number, "votes: [+1 | -0]") in removed[-2:]
        assert added[-1] == (pr_number, "votes: [+2 | -0]", "0d6838")

        # Third approve reaches threshold (3-0) -> bright green.
        db.vote_on_pr(AGENTS["delta"]["token"], pr_number, 1)
        assert (pr_number, "votes: [+2 | -0]") in removed[-2:]
        assert added[-1] == (pr_number, "votes: [+3 | -0]", "1a7f37")

        # Voter flips +1 -> -1 (2-1): drops below threshold -> green again.
        db.vote_on_pr(AGENTS["beta"]["token"], pr_number, -1)
        assert (pr_number, "votes: [+3 | -0]") in removed[-2:]
        assert added[-1] == (pr_number, "votes: [+2 | -1]", "0d6838")

        # Flip again to -1 (1-2): net negative -> red.
        db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, -1)
        assert added[-1] == (pr_number, "votes: [+1 | -2]", "b62324")

        # All votes removed (0-0): no label added (zero votes omitted).
        db.vote_on_pr(AGENTS["delta"]["token"], pr_number, -1)
        assert (pr_number, "votes: [+1 | -2]") in removed[-2:]
        # The add list should NOT contain a zero-vote label.
        assert not any(n == pr_number and "0 | -0" in lab for n, lab, _ in added)
    finally:
        _github.add_pr_label = real_add
        _github.remove_pr_label = real_remove
        _github.list_pr_labels = real_list
    print("  votes label sync: ok")


def test_remove_pr_label_encodes_url():
    """remove_pr_label percent-encodes the label name in the DELETE URL
    so labels containing '/', ':', '[', ']' etc. are sent correctly."""
    import github as _github
    import urllib.parse
    real_request = _github._request
    captured_paths: list[str] = []

    def _spy_request(method, path, body=None, ok_404=False):
        captured_paths.append(path)
        return real_request(method, path, body=body, ok_404=ok_404)

    _github._request = _spy_request
    try:
        label = "votes: [+3 | -1]"
        _github.remove_pr_label(42, label)
        assert len(captured_paths) == 1
        path = captured_paths[0]
        # The label must be percent-encoded in the path.
        assert urllib.parse.quote(label, safe="") in path, (
            f"label not encoded in path: {path}"
        )
        # Raw special chars must not appear in the path.
        assert "/" not in path.split("labels/", 1)[1].split("/")[0].replace(
            urllib.parse.quote("/", safe=""), ""
        ), f"unencoded / in label path segment: {path}"
    finally:
        _github._request = real_request
    print("  remove_pr_label URL encoding: ok")


def test_pr_vote_tally():
    """pr_vote_tally returns a tally dict with up/down/net/voters."""
    from db._pr_vote import pr_vote_tally
    # Non-existent PR should return empty tally
    tally = pr_vote_tally(99999)
    assert tally["pr_number"] == 99999
    assert tally["up"] == 0 and tally["down"] == 0 and tally["net"] == 0
    assert tally["voters"] == []
    print("  pr_vote_tally empty ok")
    # Create a proposal + link + vote, then check tally
    prop = db.create_proposal(
        AGENTS["alpha"]["token"], "Tally test", "body", small_fix=True
    )
    pid = prop["post_id"]
    db.link_pr_to_proposal(5001, pid, AGENTS["alpha"]["agent_id"])
    db.vote_on_pr(AGENTS["beta"]["token"], 5001, 1)
    tally = pr_vote_tally(5001)
    assert tally["pr_number"] == 5001
    assert tally["up"] == 1 and tally["net"] == 1
    assert len(tally["voters"]) == 1
    assert tally["voters"][0]["value"] == 1
    print("  pr_vote_tally with votes ok")


def test_vote_response_includes_threshold():
    """vote_on_pr response includes threshold and eligible_for_merge."""
    pid, pr_number = _make_small_fix()

    result = db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    assert "threshold" in result, "response must include threshold"
    assert "eligible_for_merge" in result, "response must include eligible_for_merge"
    assert result["threshold"] == 3
    assert result["eligible_for_merge"] is False  # net=1 < threshold=3
    print("  vote response threshold: ok")


def test_post_insert_rollback_on_threshold_race():
    """Post-insert guard rolls back a +1 vote that would push net above the
    threshold, without affecting existing tally or event log."""
    pid, pr_number = _make_small_fix()

    # Reach the threshold with three votes.
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)
    tally = db.pr_vote_tally(pr_number)
    assert tally["net"] == 3

    # A new voter's +1 is rejected and the tally is unchanged.
    err = expect_error(db.vote_on_pr, AGENTS["epsilon"]["token"], pr_number, 1)
    assert "enough votes" in err.lower()
    tally = db.pr_vote_tally(pr_number)
    assert tally["net"] == 3
    assert tally["up"] == 3

    # No event was logged for the rolled-back vote.
    evts = events.query_events(kind="pr_vote_cast", target_id=pr_number)
    assert len(evts) == 3, f"expected 3 vote events, got {len(evts)}"
    print("  post-insert rollback: ok")


def test_existing_voter_change_past_threshold():
    """An existing voter changing their vote from -1 to +1 past the threshold
    is rolled back (the change would push net above the bar)."""
    pid, pr_number = _make_small_fix()

    # Reach threshold with three +1 votes.
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)
    # zeta opposes, bringing net to 2.
    db.vote_on_pr(AGENTS["zeta"]["token"], pr_number, -1)
    tally = db.pr_vote_tally(pr_number)
    assert tally["net"] == 2

    # zeta changes to +1 -> net would be 4 (above threshold 3) -> rolled back.
    err = expect_error(db.vote_on_pr, AGENTS["zeta"]["token"], pr_number, 1)
    assert "enough votes" in err.lower()
    tally = db.pr_vote_tally(pr_number)
    assert tally["net"] == 2, "vote change past threshold should be rolled back"
    print("  existing voter change rollback: ok")


def test_existing_voter_change_within_threshold():
    """An existing voter changing their vote within the threshold is allowed."""
    pid, pr_number = _make_small_fix()

    # beta +1, gamma +1 -> net=2 (below threshold 3).
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, 1)
    # beta changes to -1 -> net=0, still within threshold.
    result = db.vote_on_pr(AGENTS["beta"]["token"], pr_number, -1)
    assert result["action"] == "changed"
    assert result["net"] == 0
    # gamma changes to -1 -> net=-2, still within threshold.
    result = db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, -1)
    assert result["action"] == "changed"
    assert result["net"] == -2
    print("  existing voter change within threshold: ok")


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
    test_pr_vote_tally()
    test_vote_response_includes_threshold()
    test_post_insert_rollback_on_threshold_race()
    test_existing_voter_change_past_threshold()
    test_existing_voter_change_within_threshold()
    print("\n== test_pr_vote: all passed ==")
