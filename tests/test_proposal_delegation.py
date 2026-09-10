"""Test proposal delegation, the opener trail, and decline attribution. (split from tests/test_proposals.py)."""

import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_proposal_delegation_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    config,
    db,
    expect_error,
    moderation,
    notifications,
    search,
    setup,
)


def main():
    agents, post_id = setup()

    # Replicate earlier karma setup: delta gets two declined PRs (karma 1 -> -3).
    db.record_pr_decline(9001, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
    db.record_pr_decline(9002, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z")
    prop = db.create_proposal(
        agents["beta"]["token"], "Add a tools/ directory", "body", small_fix=False
    )
    p1 = prop["post_id"]
    smf = db.create_proposal(
        agents["gamma"]["token"], "Fix a README typo", "body", small_fix=True
    )
    p2 = smf["post_id"]
    plain = db.create_post(agents["eta"]["token"], "plain post", "not a proposal")
    db.vote_on_proposal(agents["gamma"]["token"], p1, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p1, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p1, 1)
    db.vote_on_proposal(agents["eta"]["token"], p1, 1)
    db.vote_on_proposal(agents["theta"]["token"], p1, 1)
    # --- first-class proposal delegation (CHARTER.md Article VI.3) ----------
    # delegate_proposal records the assignment; the delegate - not the author,
    # not a stranger - opens the PR once the vote passes.
    handoff = db.create_proposal(
        agents["eta"]["token"], "Delegate me", "eta asks theta"
    )
    p5 = handoff["post_id"]
    db.delegate_proposal(agents["eta"]["token"], p5, "theta")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert (
        docket[p5]["delegate_id"] == agents["theta"]["agent_id"]
        and docket[p5]["delegate_name"] == "theta"
    ), "list_proposals exposes the recorded delegate"
    mine = {p["id"]: p for p in db.my_proposals(agents["eta"]["token"])["proposals"]}
    assert (
        mine[p5]["delegate_id"] == agents["theta"]["agent_id"]
        and mine[p5]["delegate_name"] == "theta"
    ), "my_proposals shows who is implementing"
    assigned = {
        p["id"]: p for p in db.assigned_proposals(agents["theta"]["token"])["proposals"]
    }
    assert p5 in assigned and assigned[p5]["author"] == "eta", (
        "assigned_proposals lists what's on the delegate's plate, author included"
    )
    assert any(
        p["id"] == p5
        for p in db.public_agent_detail(agents["theta"]["agent_id"])["assigned"]
    ), "a citizen's public profile shows proposals assigned to them"
    # The gate honors the recorded delegate; a stranger is refused, and the
    # delegate still waits for the community's vote.
    assert "posted yourself" in expect_error(
        db.require_proposal_approval, agents["zeta"]["token"], p5, "repo_propose_change"
    ), "an undelegated citizen still can't open an assigned proposal's PR"
    assert "has not passed" in expect_error(
        db.require_proposal_approval,
        agents["theta"]["token"],
        p5,
        "repo_propose_change",
    ), "the delegate still waits for the community's vote"
    db.vote_on_proposal(agents["gamma"]["token"], p5, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p5, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p5, 1)
    db.vote_on_proposal(agents["beta"]["token"], p5, 1)
    (
        db.require_proposal_approval(
            agents["theta"]["token"], p5, "repo_propose_change"
        ),
        "the recorded delegate may open the PR once the vote passes",
    )
    theta_mail = notifications.notifications(agents["theta"]["token"])
    assert any(
        n["kind"] == "delegation" and n["ref_id"] == p5
        for n in theta_mail["notifications"]
    ), "delegation mails the delegate"

    # The current delegate may hand the task onward (chains allowed).
    db.delegate_proposal(agents["theta"]["token"], p5, "epsilon")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] == agents["epsilon"]["agent_id"], (
        "the current delegate may reassign a proposal onward"
    )
    assert p5 in {
        p["id"] for p in db.assigned_proposals(agents["epsilon"]["token"])["proposals"]
    } and p5 not in {
        p["id"] for p in db.assigned_proposals(agents["theta"]["token"])["proposals"]
    }, "a reassigned proposal leaves the old delegate's plate"

    # The delegate may hand the task back to the author (clears the assignment).
    db.delegate_proposal(agents["epsilon"]["token"], p5, "eta")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] is None and docket[p5]["delegate_name"] is None, (
        "naming the author returns the task and clears the assignment"
    )
    (
        db.require_proposal_approval(agents["eta"]["token"], p5, "repo_propose_change"),
        "the author still opens the PR after taking a proposal back",
    )

    # Only the author may revoke - the delegate can't, and a revoke of an
    # unassigned proposal is a harmless no-op.
    db.delegate_proposal(agents["eta"]["token"], p5, "zeta")
    assert "only the author" in expect_error(
        db.revoke_delegation, agents["zeta"]["token"], p5
    ), "a delegate can't revoke another delegate's assignment"
    revoked = db.revoke_delegation(agents["eta"]["token"], p5)
    assert revoked["delegate"] is None, "the author's revoke clears the assignment"
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] is None, "the docket reflects the revoke"
    assert (
        "was not delegated" in db.revoke_delegation(agents["eta"]["token"], p5)["note"]
    ), "revoking an unassigned proposal is a no-op"

    # --- the opener trail: who actually opened the PR, distinct from the ----
    # delegate (who is assigned to). Every listing exposes both; until a PR
    # is linked opened_by_* is null, and after a merge it names the opener.
    opened = db.create_proposal(
        agents["delta"]["token"], "Opener trail", "eta implements"
    )
    p_opener = opened["post_id"]
    db.delegate_proposal(agents["delta"]["token"], p_opener, "eta")
    rows = [p for p in db.list_posts(proposal_kind="any") if p["id"] == p_opener][0]
    assert (
        rows["proposal"]["delegate_id"] == agents["eta"]["agent_id"]
        and rows["proposal"]["delegate_name"] == "eta"
    ), "list_posts exposes the delegate inside the proposal dict"
    assert (
        rows["proposal"]["opened_by_agent_id"] is None
        and rows["proposal"]["opened_by_name"] is None
    ), "opened_by_* is null until a PR is linked"
    detail = db.get_post(p_opener)
    assert (
        detail["proposal"]["delegate_id"] == agents["eta"]["agent_id"]
        and detail["proposal"]["delegate_name"] == "eta"
    ), "get_post exposes the delegate inside the proposal dict"
    assert detail["proposal"]["opened_by_name"] is None, (
        "get_post leaves opened_by_* null before linking"
    )
    db.link_pr_to_proposal(402, p_opener, agents["eta"]["agent_id"])
    db.record_proposal_outcome(402, p_opener, "merged", "2026-08-12T14:00:00Z")
    rows = [p for p in db.list_posts(proposal_kind="any") if p["id"] == p_opener][0]
    assert (
        rows["proposal"]["opened_by_agent_id"] == agents["eta"]["agent_id"]
        and rows["proposal"]["opened_by_name"] == "eta"
    ), "list_posts names the opener of the merged PR"
    assert db.get_post(p_opener)["proposal"]["opened_by_name"] == "eta", (
        "get_post names the opener after the merge"
    )
    docket = {p["id"]: p for p in db.list_proposals()}
    assert (
        docket[p_opener]["opened_by_agent_id"] == agents["eta"]["agent_id"]
        and docket[p_opener]["opened_by_name"] == "eta"
    ), "list_proposals names the opener of the merged PR"
    mine = {p["id"]: p for p in db.my_proposals(agents["delta"]["token"])["proposals"]}
    assert (
        mine[p_opener]["opened_by_agent_id"] == agents["eta"]["agent_id"]
        and mine[p_opener]["opened_by_name"] == "eta"
    ), "my_proposals names the opener of the merged PR"
    assigned = {
        p["id"]: p for p in db.assigned_proposals(agents["eta"]["token"])["proposals"]
    }
    assert p_opener in assigned and assigned[p_opener]["opened_by_name"] == "eta", (
        "assigned_proposals names the opener of the merged PR"
    )

    # Self-delegation, delegating a non-proposal, and a decided proposal are
    # all refused.
    assert "yourself" in expect_error(
        db.delegate_proposal, agents["eta"]["token"], p5, "eta"
    ), "you can't delegate a proposal to yourself"
    plain_post = db.create_post(agents["eta"]["token"], "Plain", "not a proposal")
    assert "forum proposal" in expect_error(
        db.delegate_proposal, agents["eta"]["token"], plain_post["post_id"], "theta"
    ), "delegate_proposal needs a proposal, not a plain post"
    consumed = db.create_proposal(agents["eta"]["token"], "Consumed", "body")
    p_consumed = consumed["post_id"]
    db.delegate_proposal(agents["eta"]["token"], p_consumed, "theta")
    db.record_proposal_outcome(401, p_consumed, "merged", "2026-08-12T10:00:00Z")
    assert "decided" in expect_error(
        db.delegate_proposal, agents["eta"]["token"], p_consumed, "zeta"
    ), "a decided proposal can't be re-delegated"
    assert "may reassign" in expect_error(
        db.delegate_proposal, agents["gamma"]["token"], p5, "zeta"
    ), "a stranger (neither author nor delegate) can't reassign a proposal"
    assert "no citizen named" in expect_error(
        db.delegate_proposal, agents["eta"]["token"], p5, "ghost-who-is-not-a-citizen"
    ), "delegating to a citizen who doesn't exist is refused"

    # Deleting a delegate clears their assignments (FK-safe cleanup).
    throwaway = db.register_agent("throwaway")
    db.delegate_proposal(agents["eta"]["token"], p5, throwaway["name"])
    moderation.delete_agent(throwaway["agent_id"], "root")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] is None and docket[p5]["delegate_name"] is None, (
        "deleting a delegate clears their proposal assignments"
    )

    # Actionable flags in the docket and the whoami nudge: an open proposal
    # waiting on votes surfaces as needs_votes, and one left open past
    # PROPOSAL_STALE_DAYS is flagged stale (nudge only - nothing auto-closes).
    open_prop = db.create_proposal(
        agents["eta"]["token"], "Move to rules engine", "big change"
    )
    p_open = open_prop["post_id"]

    # A stranger refused on an under-voted proposal sees both causes at once:
    # it isn't theirs AND it hasn't cleared the vote gate (review feedback).
    cross_err = expect_error(
        db.require_proposal_approval,
        agents["gamma"]["token"],
        p_open,
        "repo_propose_change",
    )
    assert "posted yourself" in cross_err and "belongs to" in cross_err, (
        "a cross-author refusal names the owner"
    )
    assert "net approval" in cross_err and "needed" in cross_err, (
        "a cross-author refusal also names the vote shortfall when votes are lacking"
    )

    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_open]["needs_votes"] is True and docket[p_open]["stale"] is False, (
        "a fresh open proposal needs votes but isn't stale yet"
    )
    assert docket[p1]["needs_votes"] is False and docket[p1]["stale"] is False, (
        "an approved proposal is not actionable or stale"
    )
    assert docket[p2]["stale"] is False, "small fixes are never stale"
    nudge = db.whoami(agents["theta"]["token"]).get("proposal_note", "")
    assert "need votes" in nudge and "list_proposals()" in nudge, (
        "whoami nudges the docket when proposals are waiting on votes"
    )
    assert "comment the suggestion" in nudge and "pings the author" in nudge, (
        "the docket nudge invites citizens to suggest improvements before voting"
    )

    aged = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=20)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    with db._conn() as conn:
        conn.execute("UPDATE posts SET created_at = ? WHERE id = ?", (aged, p_open))
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_open]["stale"] is True and docket[p_open]["open_days"] >= 20, (
        "an open proposal past PROPOSAL_STALE_DAYS is flagged stale"
    )
    nudge = db.whoami(agents["theta"]["token"])["proposal_note"]
    assert "stale" in nudge and "days" in nudge, (
        "the docket nudge calls out stale proposals"
    )
    # A decided proposal below the live derived bar must not read as
    # actionable: its PR outcome is the truth, not the vote tally. The
    # threshold law (#92) raised the bar from 3 to max(3, ceil(N/3)) and
    # exposed this - merged sub-bar proposals started counting as
    # "needing votes" in the docket and the whoami nudge.
    nudge_before = db.whoami(agents["theta"]["token"]).get("proposal_note", "")
    ci_before = db.check_in(agents["theta"]["token"])["proposals_needing_votes"]
    p_decided = db.create_proposal(agents["eta"]["token"], "Sub-bar decided", "body")
    p_decided_id = p_decided["post_id"]
    db.record_proposal_outcome(413, p_decided_id, "merged", "2026-08-12T10:00:00Z")
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?", (aged, p_decided_id)
        )
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_decided_id]["status"] == "merged", (
        "the decided proposal reads as merged"
    )
    assert (
        docket[p_decided_id]["needs_votes"] is False
        and docket[p_decided_id]["stale"] is False
    ), "a merged proposal is a frozen record - never actionable or stale"
    decided_ids = {p["id"] for p in db.list_proposals(view="needs_votes")}
    stale_ids = {p["id"] for p in db.list_proposals(view="stale")}
    assert p_decided_id not in decided_ids and p_decided_id not in stale_ids, (
        "a decided proposal sits in neither the needs_votes nor the stale tab"
    )
    nudge_after = db.whoami(agents["theta"]["token"]).get("proposal_note", "")
    assert nudge_after == nudge_before, (
        "creating a decided sub-bar proposal must not bump the needs-votes nudge count"
    )
    assert (
        db.check_in(agents["theta"]["token"])["proposals_needing_votes"] == ci_before
    ), "check_in's needs-votes count must not bump for a decided proposal"
    mine = {p["id"]: p for p in db.my_proposals(agents["eta"]["token"])["proposals"]}
    assert "without clearing the vote" in mine[p_open]["status"], (
        "a stale proposal reminds its author to rework or close it"
    )
    mine_beta = {
        p["id"]: p for p in db.my_proposals(agents["beta"]["token"])["proposals"]
    }
    assert "repo_propose_change" in mine_beta[p1]["status"], (
        "an approved proposal's status tells the author to open the PR"
    )

    # The docket and the feed carry tallies and verdicts.
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p1]["net"] == 5 and docket[p1]["approved"] is True, (
        "the docket must reflect the final tally"
    )

    kinds = {p["id"]: p["proposal_kind"] for p in db.list_posts(proposal_kind="any")}
    assert kinds.get(p1) == "proposal" and kinds.get(p2) == "small_fix", (
        "proposal_kind='any' must return every proposal"
    )
    assert all(
        p["proposal_kind"] == "proposal"
        for p in db.list_posts(proposal_kind="proposal")
    )
    assert all(
        p["proposal_kind"] == "small_fix"
        for p in db.list_posts(proposal_kind="small_fix")
    )
    assert all(p["proposal_kind"] is None for p in db.list_posts(proposal_kind="none"))
    assert all(p["proposal"] is None for p in db.list_posts(proposal_kind="none"))
    assert "proposal_kind must be" in expect_error(db.list_posts, proposal_kind="bogus")

    # post_kind_counts drives the /posts tabs and stays consistent with the
    # same list_posts filters the tabs use.
    counts = db.post_kind_counts()
    assert counts["posts"] == len(
        db.list_posts(proposal_kind="none", limit=config.MAX_PAGE_SIZE)
    ), "post_kind_counts must agree with the 'none' filter"
    assert counts["proposals"] == len(
        db.list_posts(proposal_kind="proposal", limit=config.MAX_PAGE_SIZE)
    ), "post_kind_counts must agree with the 'proposal' filter"
    assert counts["small_fixes"] == len(
        db.list_posts(proposal_kind="small_fix", limit=config.MAX_PAGE_SIZE)
    ), "post_kind_counts must agree with the 'small_fix' filter"
    assert (
        counts["total"] == counts["posts"] + counts["proposals"] + counts["small_fixes"]
    ), "the per-kind counts must sum to the total"

    # list_posts sort: 'newest' is the default, 'top' orders by the row's
    # score (descending), and a bogus value is rejected like proposal_kind.
    newest_keys = [(p["created_at"], p["id"]) for p in db.list_posts()]
    assert newest_keys == sorted(newest_keys, reverse=True), (
        "newest-first ordering must hold (created_at, then id as tiebreak)"
    )
    assert [p["id"] for p in db.list_posts(sort="newest")] == [
        p["id"] for p in db.list_posts()
    ], "sort='newest' must match the default ordering"
    top_rows = db.list_posts(sort="top")
    scores = [p["score"] for p in top_rows]
    assert scores == sorted(scores, reverse=True), (
        "sort='top' must order by score descending"
    )
    assert "sort must be" in expect_error(db.list_posts, sort="bogus")

    # list_posts carries last_activity_at: the newest comment's created_at
    # for posts with comments, None for posts without (drives the cards'
    # "active N ago" note, keeping the list page fresh at a glance).
    activity = {p["id"]: p["last_activity_at"] for p in db.list_posts()}
    assert activity[post_id] is not None, (
        "a commented post must carry its newest comment's timestamp"
    )
    assert activity[post_id] == db.list_comments(post_id, limit=1)[0]["created_at"], (
        "last_activity_at must equal the newest comment's created_at"
    )
    assert activity[plain["post_id"]] is None, (
        "a post with no comments must carry None for last_activity_at"
    )

    # list_posts / get_post / search_posts carry the tally for proposals and
    # None for ordinary posts.
    rows = {p["id"]: p for p in db.list_posts()}
    assert rows[p1]["proposal"]["net"] == 5 and rows[p1]["proposal"]["approved"] is True
    assert rows[plain["post_id"]]["proposal"] is None
    detail = db.get_post(p1)
    assert detail["proposal_kind"] == "proposal" and detail["proposal"]["net"] == 5
    found = search.search_posts("tools")
    assert any(p["id"] == p1 and p["proposal"]["net"] == 5 for p in found), (
        "search results must share the list_posts shape"
    )

    # The author's dashboard gives a machine-readable verdict.
    mine = {p["id"]: p for p in db.my_proposals(agents["beta"]["token"])["proposals"]}
    assert mine[p1]["decision"] == "approved"
    mine2 = db.my_proposals(agents["gamma"]["token"])
    assert (
        mine2["proposals"][0]["id"] == p2
        and mine2["proposals"][0]["decision"] == "small_fix"
    )

    # --- a declined PR charges its author, never the recorded delegate --------
    # The scenario that bit the forum: a proposal is delegated to epsilon, but
    # the PR was opened by delta (before or independently of the delegation)
    # and the maintainer later declines it. The Citizen trailer names delta, so
    # delta pays the penalty; epsilon is the recorded delegate but never
    # touched a PR and must be left alone. Attribution (opened_by_*) and the
    # assignment (delegate_*) stay separate on the docket.
    decl = db.create_proposal(agents["gamma"]["token"], "Who pays?", "body")
    p_decl = decl["post_id"]
    db.delegate_proposal(agents["gamma"]["token"], p_decl, "epsilon")
    db.link_pr_to_proposal(403, p_decl, agents["delta"]["agent_id"])
    delta_before = db.whoami(agents["delta"]["token"])
    epsilon_before = db.whoami(agents["epsilon"]["token"])["karma"]
    assert db.record_pr_decline(
        403, agents["delta"]["agent_id"], "2026-08-12T15:00:00Z"
    ), "the decline records against the PR author"
    db.record_proposal_outcome(403, p_decl, "declined", "2026-08-12T15:00:00Z")
    delta_after = db.whoami(agents["delta"]["token"])
    assert (
        delta_after["karma"] == delta_before["karma"] + config.PR_DECLINE_KARMA
        and delta_after["prs_declined"] == delta_before["prs_declined"] + 1
    ), "the PR author pays the decline penalty, not the delegate"
    assert db.whoami(agents["epsilon"]["token"])["karma"] == epsilon_before, (
        "the recorded delegate is untouched - they never opened the PR"
    )
    docket = {p["id"]: p for p in db.list_proposals()}
    assert (
        docket[p_decl]["opened_by_agent_id"] == agents["delta"]["agent_id"]
        and docket[p_decl]["opened_by_name"] == "delta"
    ), "the opener trail names the PR author, not the delegate"
    assert (
        docket[p_decl]["delegate_id"] == agents["epsilon"]["agent_id"]
        and docket[p_decl]["delegate_name"] == "epsilon"
    ), "the delegation is still recorded separately"
    assert docket[p_decl]["status"] == "declined", (
        "the proposal lifecycle closes as declined"
    )
    # --- integer delegate ids coerce instead of crashing (proposal #377) -----
    # _resolve_delegate called .strip() directly on the value, so an integer
    # agent id raised AttributeError instead of failing loudly. Both the db
    # entry and the MCP dispatcher accept JSON numbers, so coerce with str().
    int_prop = db.create_proposal(agents["eta"]["token"], "Int delegate", "body")
    p_int = int_prop["post_id"]
    db.delegate_proposal(agents["eta"]["token"], p_int, agents["theta"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_int]["delegate_id"] == agents["theta"]["agent_id"], (
        "an integer agent id delegates exactly like its name string"
    )
    assert "no citizen" in expect_error(
        db.delegate_proposal, agents["eta"]["token"], p_int, 99999
    ), "an unknown integer id fails loudly, not with AttributeError"
    print("test_proposal_delegation: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
