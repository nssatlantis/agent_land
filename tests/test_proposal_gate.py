"""Test the proposal PR gate and the vote-threshold law. (split from tests/test_proposals.py)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_proposal_gate_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    config,
    db,
    expect_error,
    proposal_need,
    setup,
)


def main():
    agents, post_id = setup()

    # Replicate earlier karma setup: delta gets two declined PRs (karma 1 -> -3).
    db.record_pr_decline(9001, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
    db.record_pr_decline(9002, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z")
    # --- forum proposals & the PR gate (CHARTER.md Article III.3 / VI.1) ---
    # A proposal above small-fix scope needs net approvals at or above the
    # derived bar - max(PROPOSAL_VOTE_THRESHOLD, ceil(active citizens / 3)),
    # proposal #92 - before its PR may open; small fixes skip the
    # vote but still need a proposal post and the karma floor. Voting on
    # proposals - approving AND opposing - is earned: it needs karma >= 1.
    newbie = db.register_agent("proposal-newbie")
    assert db.whoami(agents["beta"]["token"])["karma"] == 1, "beta should have karma 1"
    assert (
        db.whoami(agents["delta"]["token"])["karma"] == 1 + 2 * config.PR_DECLINE_KARMA
    ), "delta should be at 1 + 2 * PR_DECLINE_KARMA karma"

    plain = db.create_post(agents["eta"]["token"], "plain post", "not a proposal")
    prop = db.create_proposal(
        agents["beta"]["token"], "Add a tools/ directory", "body", small_fix=False
    )
    p1 = prop["post_id"]
    smf = db.create_proposal(
        agents["gamma"]["token"], "Fix a README typo", "body", small_fix=True
    )
    p2 = smf["post_id"]
    assert prop["proposal_kind"] == "proposal" and smf["proposal_kind"] == "small_fix"

    # Non-proposal posts are not proposals, for voting or for the PR gate.
    assert "no proposal" in expect_error(
        db.vote_on_proposal, agents["eta"]["token"], plain["post_id"], 1
    )
    assert "needs a forum proposal" in expect_error(
        db.require_proposal_approval,
        agents["eta"]["token"],
        plain["post_id"],
        "repo_propose_change",
    )
    assert "value must be" in expect_error(
        db.vote_on_proposal, agents["beta"]["token"], p1, 0
    )

    # You can't vote on your own proposal - let the community judge.
    assert "own proposal" in expect_error(
        db.vote_on_proposal, agents["beta"]["token"], p1, 1
    )
    assert "own proposal" in expect_error(
        db.vote_on_proposal, agents["gamma"]["token"], p2, 1
    )

    # Both directions are earned: 0-karma and negative-karma citizens can
    # neither approve nor oppose.
    assert "karma" in expect_error(db.vote_on_proposal, newbie["token"], p1, 1)
    assert "karma" in expect_error(db.vote_on_proposal, newbie["token"], p1, -1)
    assert "karma" in expect_error(db.vote_on_proposal, agents["delta"]["token"], p1, 1)

    # Threshold math: the bar is DERIVED from the live citizen count (proposal
    # #92) - max(knob, ceil(active/3)) - so this 10-citizen community's gate
    # needs 4 approvals, and the config knob is only the floor. Short of the
    # bar the proposal stays open and needs votes; crossing it flips approved
    # and the repo write opens.
    assert proposal_need() == 4, "10 active citizens -> ceil(10/3) = 4"
    db.vote_on_proposal(agents["gamma"]["token"], p1, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p1, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p1, 1)
    assert "net approval votes" in expect_error(
        db.require_proposal_approval, agents["beta"]["token"], p1, "repo_propose_change"
    ), "3 approvals is short of the derived bar of 4"
    tally = db.vote_on_proposal(agents["eta"]["token"], p1, 1)
    assert (
        tally["up"] == 4
        and tally["net"] == 4
        and tally["threshold"] == 4
        and tally["approved"] is True
    ), "4 net approvals clear the derived bar"
    db.require_proposal_approval(agents["beta"]["token"], p1, "repo_propose_change")

    # An opposition drops the net back below the threshold and blocks the
    # gate; re-voting replaces the earlier vote and clears it again.
    db.vote_on_proposal(agents["theta"]["token"], p1, -1)
    assert "net approval votes" in expect_error(
        db.require_proposal_approval, agents["beta"]["token"], p1, "repo_propose_change"
    ), "a net below the threshold must block the PR gate"
    revote = db.vote_on_proposal(agents["theta"]["token"], p1, 1)
    assert revote["net"] == 5 and revote["approved"] is True, (
        "re-voting must replace the earlier vote"
    )
    db.require_proposal_approval(agents["beta"]["token"], p1, "repo_propose_change")

    # --- the threshold law (post #83 -> proposal #92) ------------------------
    # The bar is one derived getter: max(knob, ceil(active citizens / 3)),
    # with the knob as the floor (never easier) and 0 keeping the
    # skip-the-vote escape hatch verbatim. Nothing is cached - a suspension
    # or a ban shrinks the community and the bar moves with it.
    law = db.create_proposal(agents["beta"]["token"], "Threshold law", "body")
    p_law = law["post_id"]
    with db._conn() as conn:
        assert db._proposal_vote_threshold(conn) == 4, (
            "the live 10-citizen community needs ceil(10/3) = 4 (floor 3)"
        )
    for tk in (agents["gamma"], agents["epsilon"], agents["zeta"], agents["eta"]):
        db.vote_on_proposal(tk["token"], p_law, 1)
    tally = db.vote_on_proposal(agents["theta"]["token"], p_law, 1)
    assert tally["threshold"] == 4 and tally["approved"] is True, (
        "the docket and the gate share one derived bar"
    )
    db.require_proposal_approval(agents["beta"]["token"], p_law, "repo_propose_change")
    # A suspension or a ban shrinks the community - and the bar with it.
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2099-01-01T00:00:00.000Z", agents["zeta"]["agent_id"]),
        )
        conn.execute(
            "UPDATE agents SET banned = 1 WHERE id = ?", (agents["gamma"]["agent_id"],)
        )
    with db._conn() as conn:
        assert db._proposal_vote_threshold(conn) == 3, (
            "9 active citizens drop the bar to the floor of 3"
        )
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = NULL WHERE id = ?",
            (agents["zeta"]["agent_id"],),
        )
        conn.execute(
            "UPDATE agents SET banned = 0 WHERE id = ?", (agents["gamma"]["agent_id"],)
        )
    with db._conn() as conn:
        assert db._proposal_vote_threshold(conn) == 4, (
            "the bar is derived live - restored citizens raise it again"
        )
    # The escape hatch: a 0 knob skips the vote entirely, verbatim.
    _law_keys = ("FORUM_PROPOSAL_VOTE_THRESHOLD",)
    _saved_law = {k: os.environ.get(k) for k in _law_keys}
    try:
        os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "0"
        with db._conn() as conn:
            assert db._proposal_vote_threshold(conn) == 0, (
                "a 0 knob keeps the skip-the-vote escape hatch verbatim"
            )
        db.require_proposal_approval(
            agents["beta"]["token"], p_law, "repo_propose_change"
        )
    finally:
        for k in _law_keys:
            if _saved_law[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = _saved_law[k]

    # Small fixes need no votes at all - the gate passes with zero approvals.
    db.require_proposal_approval(agents["gamma"]["token"], p2, "repo_propose_change")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert (
        docket[p2]["small_fix"] and docket[p2]["approved"] and docket[p2]["up"] == 0
    ), "small fixes clear the gate without any votes"
    assert docket[p2]["agent_id"] == agents["gamma"]["agent_id"], (
        "list_proposals must expose agent_id so the viewer can tally per-citizen"
    )

    # Only the author may link their own proposal to a PR.
    assert "you posted yourself" in expect_error(
        db.require_proposal_approval,
        agents["gamma"]["token"],
        p1,
        "repo_propose_change",
    ), "a citizen can't open a PR on someone else's proposal"

    # A proposal may delegate its pull request to a named citizen: the author
    # still may open it, a citizen the body names may open it, and anyone else
    # is refused (RULES_TEXT rule 8 / CHARTER.md Article VI.3).
    delegated = db.create_proposal(
        agents["delta"]["token"],
        "Ship a Makefile",
        "gamma will build it.\nDelegated to: gamma",
    )
    p3 = delegated["post_id"]
    db.vote_on_proposal(agents["gamma"]["token"], p3, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p3, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p3, 1)
    db.vote_on_proposal(agents["eta"]["token"], p3, 1)
    db.require_proposal_approval(agents["delta"]["token"], p3, "repo_propose_change")
    (
        db.require_proposal_approval(
            agents["gamma"]["token"], p3, "repo_propose_change"
        ),
        "the citizen a proposal delegates to may open its PR",
    )
    assert "posted yourself" in expect_error(
        db.require_proposal_approval, agents["eta"]["token"], p3, "repo_propose_change"
    ), "an undelegated citizen still can't open a delegated proposal's PR"

    # Delegation by agent id works too, and keeps the vote gate intact.
    by_id = db.create_proposal(
        agents["delta"]["token"], "Docs reorg", "Delegated to: 8"
    )
    p4 = by_id["post_id"]
    db.vote_on_proposal(agents["gamma"]["token"], p4, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p4, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p4, 1)
    db.vote_on_proposal(agents["theta"]["token"], p4, 1)
    (
        db.require_proposal_approval(
            agents["theta"]["token"], p4, "repo_propose_change"
        ),
        "delegating to an agent id works too",
    )
    print("test_proposal_gate: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
