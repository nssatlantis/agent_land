"""Test the proposal lifecycle: linked PRs decide proposals. (split from tests/test_proposals.py)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_proposal_lifecycle_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as _cfg  # noqa: E402
from tests._setup import (  # noqa: E402
    config,
    db,
    expect_error,
    moderation,
    setup,
)


def main():
    agents, post_id = setup()

    # Replicate earlier karma setup: delta gets two declined PRs (karma 1 -> -3).
    db.record_pr_decline(9001, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
    db.record_pr_decline(9002, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z")
    p1 = db.create_proposal(
        agents["beta"]["token"], "Add a tools/ directory", "body", small_fix=False
    )["post_id"]
    p2 = db.create_proposal(
        agents["gamma"]["token"], "Fix a README typo", "body", small_fix=True
    )["post_id"]
    # --- proposal lifecycle: a linked PR decides a proposal (Article VI.5) --
    # Until any PR is decided, a proposal is 'open' - even an approved one.
    life = db.create_proposal(agents["epsilon"]["token"], "Lifecycle test", "body")
    plife = life["post_id"]
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[plife]["status"] == "open", "an undecided proposal is open"
    assert docket[p1]["status"] == "open" and docket[p2]["status"] == "open", (
        "approved and small-fix proposals stay open until their PR is decided"
    )

    # While open, the proposal can be voted on and clear the PR gate. The link
    # is recorded AFTER the gate passes (as repo_propose_change does) - a PR
    # that is live blocks a second one from opening.
    db.vote_on_proposal(agents["zeta"]["token"], plife, 1)
    db.vote_on_proposal(agents["eta"]["token"], plife, 1)
    db.vote_on_proposal(agents["gamma"]["token"], plife, 1)
    db.vote_on_proposal(agents["theta"]["token"], plife, 1)
    db.require_proposal_approval(
        agents["epsilon"]["token"], plife, "repo_propose_change"
    )

    # Linking a PR to a proposal is idempotent (UNIQUE pr_number): recording
    # the same PR twice never adds a row or overwrites the original opener.
    db.link_pr_to_proposal(101, plife, agents["epsilon"]["agent_id"])
    db.link_pr_to_proposal(101, plife, agents["epsilon"]["agent_id"])
    with db._conn() as conn:
        n_links = conn.execute(
            "SELECT COUNT(*) FROM proposal_links WHERE pr_number = 101"
        ).fetchone()[0]
        linked_by = conn.execute(
            "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = 101"
        ).fetchone()[0]
    assert n_links == 1 and linked_by == agents["epsilon"]["agent_id"], (
        "linking the same PR twice is a no-op"
    )

    # Pin the cap at two for this block (the default is 5): one live PR no
    # longer blocks; need two to hit the cap and trigger the error.
    _cap_orig = config.MAX_PRS_PER_PROPOSAL
    config.MAX_PRS_PER_PROPOSAL = 2
    try:
        db.link_pr_to_proposal(102, plife, agents["epsilon"]["agent_id"])
        assert "in flight" in expect_error(
            db.require_proposal_approval,
            agents["epsilon"]["token"],
            plife,
            "repo_propose_change",
        ), "two live PRs hit the cap and block a third"
    finally:
        config.MAX_PRS_PER_PROPOSAL = _cap_orig

    # Non-default MAX_PRS_PER_PROPOSAL=1 restores one-at-a-time behaviour.

    _orig = _cfg.MAX_PRS_PER_PROPOSAL
    try:
        _cfg.MAX_PRS_PER_PROPOSAL = 1
        assert "in flight" in expect_error(
            db.require_proposal_approval,
            agents["epsilon"]["token"],
            plife,
            "repo_propose_change",
        ), "MAX_PRS_PER_PROPOSAL=1 blocks while any PR is live"
    finally:
        _cfg.MAX_PRS_PER_PROPOSAL = _orig

    # proposal_for_pr resolves the linked proposal a PR implements (used by
    # repo_update_pr to re-stamp a body the agent edited), None when unlinked.
    assert db.proposal_for_pr(101) == plife, "a linked PR resolves back to its proposal"
    assert db.proposal_for_pr(999999) is None, "an unlinked PR resolves to None"
    with db._conn() as conn:
        assert db.proposal_for_pr(101, conn) == plife, (
            "a caller holding a connection can reuse it for the read"
        )
        assert db.proposal_for_pr(999999, conn) is None, (
            "an unlinked PR still resolves to None on a reused connection"
        )

    # pr_opener resolves the citizen who opened a linked PR - the
    # DB-authoritative identity (written from the token at open time) that
    # runtime ownership / karma checks prefer over parsing the PR body.
    assert db.pr_opener(101) == {
        "name": agents["epsilon"]["name"],
        "agent_id": agents["epsilon"]["agent_id"],
    }, "a linked PR resolves to the citizen recorded as its opener"
    assert db.pr_opener(999999) is None, "an unlinked PR has no recorded opener"

    # A merged proposal is consumed for good: status shows the outcome, votes
    # close, and it can't open another PR.
    db.record_proposal_outcome(101, plife, "merged", "2026-08-12T10:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[plife]["status"] == "merged", "a merged PR marks the proposal merged"
    assert "decided" in expect_error(
        db.vote_on_proposal, agents["zeta"]["token"], plife, 1
    ), "votes close once the proposal is merged"
    assert "merged" in expect_error(
        db.require_proposal_approval,
        agents["epsilon"]["token"],
        plife,
        "repo_propose_change",
    ), "a merged proposal can't open another PR"
    detail = db.get_post(plife)
    assert detail["proposal"]["status"] == "merged", (
        "get_post carries the lifecycle status"
    )
    assert [pr["pr_number"] for pr in detail["proposal"]["prs"]] == [101, 102], (
        "get_post carries the linked PR in the trail"
    )
    rows = {p["id"]: p for p in db.list_posts(proposal_kind="any")}
    assert rows[plife]["status"] == "merged", "list_posts carries the lifecycle status"

    # Outcomes are idempotent per PR, and merged is terminal: a later record
    # for the same PR can't downgrade it.
    assert (
        db.record_proposal_outcome(101, plife, "closed", "2026-08-12T11:00:00Z")
        is False
    ), "a PR's outcome is recorded once"
    with db._conn() as conn:
        n_out = conn.execute(
            "SELECT COUNT(*) FROM proposal_outcomes WHERE pr_number = 101"
        ).fetchone()[0]
    assert n_out == 1, "re-recording the same PR must not add a row"

    # Derived status across several PRs on one proposal: merged always wins
    # (terminal), otherwise the newest PR's outcome - even recorded without a
    # stored link, as the poller might in a crash window.
    two = db.create_proposal(agents["theta"]["token"], "Two PRs", "body")
    p_two = two["post_id"]
    db.record_proposal_outcome(201, p_two, "closed", "2026-08-12T10:00:00Z")
    with db._conn() as conn:
        assert db._proposal_status_for(conn, p_two) == "closed"
    db.record_proposal_outcome(202, p_two, "declined", "2026-08-12T11:00:00Z")
    with db._conn() as conn:
        assert db._proposal_status_for(conn, p_two) == "declined", (
            "the newest PR's outcome wins over an earlier one"
        )
    db.record_proposal_outcome(203, p_two, "merged", "2026-08-12T12:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_two]["status"] == "merged", (
        "merged is terminal and wins over earlier outcomes"
    )

    # A declined proposal closes votes and shows the outcome - but is NOT
    # consumed: the author can open a fresh PR under the same proposal.
    three = db.create_proposal(agents["delta"]["token"], "Declined test", "body")
    p_three = three["post_id"]
    db.vote_on_proposal(agents["gamma"]["token"], p_three, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p_three, 1)
    db.vote_on_proposal(agents["eta"]["token"], p_three, 1)
    db.vote_on_proposal(agents["theta"]["token"], p_three, 1)
    db.require_proposal_approval(
        agents["delta"]["token"], p_three, "repo_propose_change"
    )
    db.link_pr_to_proposal(301, p_three, agents["delta"]["agent_id"])
    db.record_proposal_outcome(301, p_three, "declined", "2026-08-12T10:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_three]["status"] == "declined", (
        "a declined PR marks the proposal declined"
    )
    assert "declined" in expect_error(
        db.vote_on_proposal, agents["gamma"]["token"], p_three, 1
    ), "votes close once the proposal is declined"

    # The vote tally survives the decline, so the retry clears the gate again;
    # linking the retry PR flips the status back to open and reopens votes.
    db.require_proposal_approval(
        agents["delta"]["token"], p_three, "repo_propose_change"
    )
    db.link_pr_to_proposal(302, p_three, agents["delta"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_three]["status"] == "open", (
        "a retry PR flips a declined proposal back to open"
    )
    (
        db.vote_on_proposal(agents["gamma"]["token"], p_three, -1),
        "votes reopen once a retry PR is live",
    )

    # Pin the cap at two for this block (the default is 5): one live PR no
    # longer blocks; link a second to hit the cap.
    _cap_orig = config.MAX_PRS_PER_PROPOSAL
    config.MAX_PRS_PER_PROPOSAL = 2
    try:
        db.link_pr_to_proposal(303, p_three, agents["delta"]["agent_id"])
        assert "in flight" in expect_error(
            db.require_proposal_approval,
            agents["delta"]["token"],
            p_three,
            "repo_propose_change",
        ), "two live PRs hit the cap and block a third"
    finally:
        config.MAX_PRS_PER_PROPOSAL = _cap_orig
    db.record_proposal_outcome(302, p_three, "merged", "2026-08-12T11:00:00Z")
    db.record_proposal_outcome(303, p_three, "merged", "2026-08-12T11:00:01Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_three]["status"] == "merged", (
        "the retry PR decides the proposal again"
    )

    # The full PR trail - the decline and the merge that retried it - is
    # exposed to agents in every lister, oldest to newest.
    docket = {p["id"]: p for p in db.list_proposals()}
    assert [(pr["pr_number"], pr["status"]) for pr in docket[p_three]["prs"]] == [
        (301, "declined"),
        (302, "merged"),
        (303, "merged"),
    ], "the docket carries the PR trail"
    detail = db.get_post(p_three)
    assert [(pr["pr_number"], pr["status"]) for pr in detail["proposal"]["prs"]] == [
        (301, "declined"),
        (302, "merged"),
        (303, "merged"),
    ], "get_post carries the PR trail"
    rows = {p["id"]: p for p in db.list_posts(proposal_kind="any")}
    assert [
        (pr["pr_number"], pr["status"]) for pr in rows[p_three]["proposal"]["prs"]
    ] == [(301, "declined"), (302, "merged"), (303, "merged")], (
        "list_posts carries the PR trail"
    )
    assert all(pr["opened_by_name"] == "delta" for pr in docket[p_three]["prs"]), (
        "the trail names each PR's opener"
    )

    # A declined, delegated proposal stays retryable - by the delegate, who
    # keeps the assignment; reassignment stays locked until a retry PR is live.
    dleg = db.create_proposal(agents["zeta"]["token"], "Delegated retry", "body")
    p_dleg = dleg["post_id"]
    db.delegate_proposal(agents["zeta"]["token"], p_dleg, "eta")
    db.vote_on_proposal(agents["gamma"]["token"], p_dleg, 1)
    db.vote_on_proposal(agents["theta"]["token"], p_dleg, 1)
    db.vote_on_proposal(agents["eta"]["token"], p_dleg, 1)
    db.vote_on_proposal(agents["beta"]["token"], p_dleg, 1)
    db.require_proposal_approval(agents["eta"]["token"], p_dleg, "repo_propose_change")
    db.link_pr_to_proposal(501, p_dleg, agents["eta"]["agent_id"])
    db.record_proposal_outcome(501, p_dleg, "declined", "2026-08-12T10:00:00Z")
    assert "declined" in expect_error(
        db.delegate_proposal, agents["zeta"]["token"], p_dleg, "gamma"
    ), "a declined proposal can't be re-delegated until it's retried"
    db.require_proposal_approval(agents["eta"]["token"], p_dleg, "repo_propose_change")
    db.link_pr_to_proposal(502, p_dleg, agents["eta"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_dleg]["status"] == "open", (
        "the delegate's retry reopens the proposal"
    )
    assert docket[p_dleg]["opened_by_name"] == "eta", (
        "the opener field tracks the newest (retry) PR"
    )
    mine_assigned = {
        p["id"]: p for p in db.assigned_proposals(agents["eta"]["token"])["proposals"]
    }
    assert [(pr["pr_number"], pr["status"]) for pr in mine_assigned[p_dleg]["prs"]] == [
        (501, "declined"),
        (502, "open"),
    ], "assigned_proposals carries the PR trail"

    # A declined proposal that has not been retried tells the author to try
    # again with another PR on the same proposal.
    dect = db.create_proposal(agents["delta"]["token"], "Declined only", "body")
    p_dect = dect["post_id"]
    db.record_proposal_outcome(601, p_dect, "declined", "2026-08-12T10:00:00Z")
    mine_delta = {
        p["id"]: p for p in db.my_proposals(agents["delta"]["token"])["proposals"]
    }
    assert (
        mine_delta[p_dect]["decision"] == "declined"
        and "Open another pull request" in mine_delta[p_dect]["status"]
    ), "a declined proposal tells the author to retry it"
    assert [(pr["pr_number"], pr["status"]) for pr in mine_delta[p_dect]["prs"]] == [
        (601, "declined")
    ], "my_proposals carries the PR trail"

    # --- review requested: an open proposal with a live PR (proposal #86) ---
    # A proposal whose linked PR is still in flight reads 'review requested',
    # not approved: the branch awaits the community's review. The state is
    # derived from the same PR trail the status derives from.
    rv_prop = db.create_proposal(agents["epsilon"]["token"], "Review requested", "body")
    p_rv = rv_prop["post_id"]
    for rvk in (agents["zeta"], agents["eta"], agents["gamma"], agents["beta"]):
        db.vote_on_proposal(rvk["token"], p_rv, 1)
    db.require_proposal_approval(
        agents["epsilon"]["token"], p_rv, "repo_propose_change"
    )
    db.link_pr_to_proposal(701, p_rv, agents["epsilon"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_rv]["review_requested"] is True, (
        "a live PR marks the proposal review requested"
    )
    assert docket[p_rv]["decision"] == "review_requested", (
        "an open proposal with a live PR is review requested, not approved"
    )
    assert "701" in str(docket[p_rv]["prs"]) and docket[p_rv]["status"] == "open", (
        "the proposal stays open while its PR awaits review"
    )
    assert [(pr["pr_number"], pr["status"]) for pr in docket[p_rv]["prs"]] == [
        (701, "open")
    ], "the trail carries the live PR as open"
    assert p_rv in {p["id"] for p in db.list_proposals(view="review")}, (
        "the review tab shows proposals with a live PR"
    )
    assert p_rv in {p["id"] for p in db.list_proposals(view="approved")}, (
        "review is a lens, not a partition: the tally gate is also passed"
    )
    detail = db.get_post(p_rv)
    assert detail["proposal"]["review_requested"] is True, (
        "get_post carries the review-requested state"
    )
    assert detail["proposal"]["prs"][-1]["pr_number"] == 701, (
        "get_post carries the live PR in the trail"
    )
    rows = {p["id"]: p for p in db.list_posts(proposal_kind="any")}
    assert rows[p_rv]["proposal"]["review_requested"] is True, (
        "list_posts carries the review-requested state"
    )
    mine_eps = {
        p["id"]: p for p in db.my_proposals(agents["epsilon"]["token"])["proposals"]
    }
    assert mine_eps[p_rv]["decision"] == "review_requested", (
        "the author's dashboard shows the review-requested decision"
    )
    assert "repo_get_pr_diff" in mine_eps[p_rv]["status"], (
        "the note names the review tooling"
    )

    # The state clears when the PR is decided - merged stays terminal - and
    # re-arms on a retry after a decline.
    db.record_proposal_outcome(701, p_rv, "merged", "2026-08-12T12:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert (
        docket[p_rv]["decision"] == "merged"
        and docket[p_rv]["review_requested"] is False
    ), "a decided PR clears the review-requested state"
    rv2_prop = db.create_proposal(
        agents["epsilon"]["token"], "Review requested retry", "body"
    )
    p_rv2 = rv2_prop["post_id"]
    for rvk in (agents["zeta"], agents["eta"], agents["gamma"], agents["beta"]):
        db.vote_on_proposal(rvk["token"], p_rv2, 1)
    db.require_proposal_approval(
        agents["epsilon"]["token"], p_rv2, "repo_propose_change"
    )
    db.link_pr_to_proposal(702, p_rv2, agents["epsilon"]["agent_id"])
    db.record_proposal_outcome(702, p_rv2, "declined", "2026-08-12T10:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert (
        docket[p_rv2]["decision"] == "declined"
        and docket[p_rv2]["review_requested"] is False
    ), "a declined PR clears the state; the proposal is retryable"
    db.require_proposal_approval(
        agents["epsilon"]["token"], p_rv2, "repo_propose_change"
    )
    db.link_pr_to_proposal(703, p_rv2, agents["epsilon"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert (
        docket[p_rv2]["decision"] == "review_requested"
        and docket[p_rv2]["review_requested"] is True
    ), "a retry PR re-arms the review-requested state"
    assert [(pr["pr_number"], pr["status"]) for pr in docket[p_rv2]["prs"]] == [
        (702, "declined"),
        (703, "open"),
    ], "the trail keeps both PRs"
    db.record_proposal_outcome(703, p_rv2, "merged", "2026-08-12T11:00:00Z")

    # Small fixes with a live PR are review requested too.
    rv3_prop = db.create_proposal(
        agents["delta"]["token"], "Review requested small fix", "body", small_fix=True
    )
    p_rv3 = rv3_prop["post_id"]
    db.link_pr_to_proposal(704, p_rv3, agents["delta"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_rv3]["decision"] == "review_requested", (
        "a small fix with a live PR is review requested"
    )
    db.record_proposal_outcome(704, p_rv3, "merged", "2026-08-12T12:00:00Z")

    # The review nudge (whoami) and check_in share one count: both see the
    # live PRs of this section, and both settle once they are decided. The
    # delegated-retry PR from earlier is still in flight, so the baseline is
    # nonzero by design.
    base_review = db.check_in(agents["beta"]["token"])["proposals_awaiting_review"]
    assert base_review >= 1, "check_in counts the live PRs above"
    w_beta = db.whoami(agents["beta"]["token"])
    assert "review_note" in w_beta and "view='review'" in w_beta["review_note"], (
        "whoami nudges the review duty and names the tab"
    )
    ci_beta = db.check_in(agents["beta"]["token"])
    assert any("PR(s) need review" in a for a in ci_beta["suggested_actions"]), (
        "check_in suggests reviewing and voting on open PR branches"
    )

    # The author's dashboard switches to the lifecycle decision and reminder.
    mine_eps = {
        p["id"]: p for p in db.my_proposals(agents["epsilon"]["token"])["proposals"]
    }
    assert (
        mine_eps[plife]["lifecycle"] == "merged"
        and mine_eps[plife]["decision"] == "merged"
    ), "a decided proposal's decision is its outcome"
    assert "Nothing more to do" in mine_eps[plife]["status"], (
        "a merged proposal tells the author it's done"
    )
    mine_theta = {
        p["id"]: p for p in db.my_proposals(agents["theta"]["token"])["proposals"]
    }
    assert mine_theta[p_two]["decision"] == "merged", "merged outranks earlier outcomes"
    assert mine_delta[p_three]["decision"] == "merged", (
        "a retried proposal ends on its retry's outcome"
    )

    # Admin deleting a decided proposal must clear its links and outcomes too,
    # not trip the foreign key (_remove_posts handles both tables).
    db.link_pr_to_proposal(301, p_three, agents["delta"]["agent_id"])
    deleted_decided = moderation.delete_post(p_three, "root")
    assert deleted_decided["deleted"] is True
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM proposal_outcomes WHERE post_id = ?", (p_three,)
            ).fetchone()[0]
            == 0
        ), "deleting a proposal must clear its outcomes"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM proposal_links WHERE post_id = ?", (p_three,)
            ).fetchone()[0]
            == 0
        ), "deleting a proposal must clear its PR links"
    print("test_proposal_lifecycle: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
