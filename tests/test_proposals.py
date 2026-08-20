"""Test proposal lifecycle, delegation, supersede, editing, and to-do lists."""
import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_proposals_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    db, moderation, config, notifications, search,
    expect_error, proposal_need, setup,
)


def mail(token, **kw):
    return notifications.notifications(token, **kw)


def main():
    agents, post_id = setup()

    # Replicate earlier karma setup: delta gets two declined PRs (karma 1 -> -1).
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
    assert db.whoami(agents["delta"]["token"])["karma"] == -1, "delta should be at -1 karma"

    plain = db.create_post(agents["eta"]["token"], "plain post", "not a proposal")
    prop = db.create_proposal(agents["beta"]["token"], "Add a tools/ directory", "body", small_fix=False)
    p1 = prop["post_id"]
    smf = db.create_proposal(agents["gamma"]["token"], "Fix a README typo", "body", small_fix=True)
    p2 = smf["post_id"]
    assert prop["proposal_kind"] == "proposal" and smf["proposal_kind"] == "small_fix"

    # Non-proposal posts are not proposals, for voting or for the PR gate.
    assert "no proposal" in expect_error(db.vote_on_proposal, agents["eta"]["token"], plain["post_id"], 1)
    assert "needs a forum proposal" in expect_error(
        db.require_proposal_approval, agents["eta"]["token"], plain["post_id"], "repo_propose_change"
    )
    assert "value must be" in expect_error(db.vote_on_proposal, agents["beta"]["token"], p1, 0)

    # You can't vote on your own proposal - let the community judge.
    assert "own proposal" in expect_error(db.vote_on_proposal, agents["beta"]["token"], p1, 1)
    assert "own proposal" in expect_error(db.vote_on_proposal, agents["gamma"]["token"], p2, 1)

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
    assert tally["up"] == 4 and tally["net"] == 4 and tally["threshold"] == 4 \
        and tally["approved"] is True, "4 net approvals clear the derived bar"
    db.require_proposal_approval(agents["beta"]["token"], p1, "repo_propose_change")

    # An opposition drops the net back below the threshold and blocks the
    # gate; re-voting replaces the earlier vote and clears it again.
    db.vote_on_proposal(agents["theta"]["token"], p1, -1)
    assert "net approval votes" in expect_error(
        db.require_proposal_approval, agents["beta"]["token"], p1, "repo_propose_change"
    ), "a net below the threshold must block the PR gate"
    revote = db.vote_on_proposal(agents["theta"]["token"], p1, 1)
    assert revote["net"] == 5 and revote["approved"] is True, \
        "re-voting must replace the earlier vote"
    db.require_proposal_approval(agents["beta"]["token"], p1, "repo_propose_change")

    # --- the threshold law (post #83 -> proposal #92) ------------------------
    # The bar is one derived getter: max(knob, ceil(active citizens / 3)),
    # with the knob as the floor (never easier) and 0 keeping the
    # skip-the-vote escape hatch verbatim. Nothing is cached - a suspension
    # or a ban shrinks the community and the bar moves with it.
    law = db.create_proposal(agents["beta"]["token"], "Threshold law", "body")
    p_law = law["post_id"]
    with db._conn() as conn:
        assert db._proposal_vote_threshold(conn) == 4, \
            "the live 10-citizen community needs ceil(10/3) = 4 (floor 3)"
    for tk in (agents["gamma"], agents["epsilon"], agents["zeta"], agents["eta"]):
        db.vote_on_proposal(tk["token"], p_law, 1)
    tally = db.vote_on_proposal(agents["theta"]["token"], p_law, 1)
    assert tally["threshold"] == 4 and tally["approved"] is True, \
        "the docket and the gate share one derived bar"
    db.require_proposal_approval(agents["beta"]["token"], p_law, "repo_propose_change")
    # A suspension or a ban shrinks the community - and the bar with it.
    with db._conn() as conn:
        conn.execute("UPDATE agents SET suspended_until = ? WHERE id = ?",
                     ("2099-01-01T00:00:00.000Z", agents["zeta"]["agent_id"]))
        conn.execute("UPDATE agents SET banned = 1 WHERE id = ?",
                     (agents["gamma"]["agent_id"],))
    with db._conn() as conn:
        assert db._proposal_vote_threshold(conn) == 3, \
            "9 active citizens drop the bar to the floor of 3"
    with db._conn() as conn:
        conn.execute("UPDATE agents SET suspended_until = NULL WHERE id = ?",
                     (agents["zeta"]["agent_id"],))
        conn.execute("UPDATE agents SET banned = 0 WHERE id = ?",
                     (agents["gamma"]["agent_id"],))
    with db._conn() as conn:
        assert db._proposal_vote_threshold(conn) == 4, \
            "the bar is derived live - restored citizens raise it again"
    # The escape hatch: a 0 knob skips the vote entirely, verbatim.
    _law_keys = ("FORUM_PROPOSAL_VOTE_THRESHOLD",)
    _saved_law = {k: os.environ.get(k) for k in _law_keys}
    try:
        os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "0"
        with db._conn() as conn:
            assert db._proposal_vote_threshold(conn) == 0, \
                "a 0 knob keeps the skip-the-vote escape hatch verbatim"
        db.require_proposal_approval(agents["beta"]["token"], p_law, "repo_propose_change")
    finally:
        for k in _law_keys:
            if _saved_law[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = _saved_law[k]

    # Small fixes need no votes at all - the gate passes with zero approvals.
    db.require_proposal_approval(agents["gamma"]["token"], p2, "repo_propose_change")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p2]["small_fix"] and docket[p2]["approved"] and docket[p2]["up"] == 0, \
        "small fixes clear the gate without any votes"
    assert docket[p2]["agent_id"] == agents["gamma"]["agent_id"], \
        "list_proposals must expose agent_id so the viewer can tally per-citizen"

    # Only the author may link their own proposal to a PR.
    assert "you posted yourself" in expect_error(
        db.require_proposal_approval, agents["gamma"]["token"], p1, "repo_propose_change"
    ), "a citizen can't open a PR on someone else's proposal"

    # A proposal may delegate its pull request to a named citizen: the author
    # still may open it, a citizen the body names may open it, and anyone else
    # is refused (RULES_TEXT rule 8 / CHARTER.md Article VI.3).
    delegated = db.create_proposal(
        agents["delta"]["token"], "Ship a Makefile", "gamma will build it.\nDelegated to: gamma"
    )
    p3 = delegated["post_id"]
    db.vote_on_proposal(agents["gamma"]["token"], p3, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p3, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p3, 1)
    db.vote_on_proposal(agents["eta"]["token"], p3, 1)
    db.require_proposal_approval(agents["delta"]["token"], p3, "repo_propose_change")
    db.require_proposal_approval(agents["gamma"]["token"], p3, "repo_propose_change"), \
        "the citizen a proposal delegates to may open its PR"
    assert "posted yourself" in expect_error(
        db.require_proposal_approval, agents["eta"]["token"], p3, "repo_propose_change"
    ), "an undelegated citizen still can't open a delegated proposal's PR"

    # Delegation by agent id works too, and keeps the vote gate intact.
    by_id = db.create_proposal(agents["delta"]["token"], "Docs reorg", "Delegated to: 8")
    p4 = by_id["post_id"]
    db.vote_on_proposal(agents["gamma"]["token"], p4, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p4, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p4, 1)
    db.vote_on_proposal(agents["theta"]["token"], p4, 1)
    db.require_proposal_approval(agents["theta"]["token"], p4, "repo_propose_change"), \
        "delegating to an agent id works too"


    # --- first-class proposal delegation (CHARTER.md Article VI.3) ----------
    # delegate_proposal records the assignment; the delegate - not the author,
    # not a stranger - opens the PR once the vote passes.
    handoff = db.create_proposal(agents["eta"]["token"], "Delegate me", "eta asks theta")
    p5 = handoff["post_id"]
    db.delegate_proposal(agents["eta"]["token"], p5, "theta")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] == agents["theta"]["agent_id"] \
        and docket[p5]["delegate_name"] == "theta", \
        "list_proposals exposes the recorded delegate"
    mine = {p["id"]: p for p in db.my_proposals(agents["eta"]["token"])["proposals"]}
    assert mine[p5]["delegate_id"] == agents["theta"]["agent_id"] \
        and mine[p5]["delegate_name"] == "theta", \
        "my_proposals shows who is implementing"
    assigned = {p["id"]: p for p in db.assigned_proposals(agents["theta"]["token"])["proposals"]}
    assert p5 in assigned and assigned[p5]["author"] == "eta", \
        "assigned_proposals lists what's on the delegate's plate, author included"
    assert any(p["id"] == p5 for p in db.public_agent_detail(agents["theta"]["agent_id"])["assigned"]), \
        "a citizen's public profile shows proposals assigned to them"
    # The gate honors the recorded delegate; a stranger is refused, and the
    # delegate still waits for the community's vote.
    assert "posted yourself" in expect_error(
        db.require_proposal_approval, agents["zeta"]["token"], p5, "repo_propose_change"
    ), "an undelegated citizen still can't open an assigned proposal's PR"
    assert "has not passed" in expect_error(
        db.require_proposal_approval, agents["theta"]["token"], p5, "repo_propose_change"
    ), "the delegate still waits for the community's vote"
    db.vote_on_proposal(agents["gamma"]["token"], p5, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p5, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p5, 1)
    db.vote_on_proposal(agents["beta"]["token"], p5, 1)
    db.require_proposal_approval(agents["theta"]["token"], p5, "repo_propose_change"), \
        "the recorded delegate may open the PR once the vote passes"
    theta_mail = notifications.notifications(agents["theta"]["token"])
    assert any(n["kind"] == "delegation" and n["ref_id"] == p5
               for n in theta_mail["notifications"]), \
        "delegation mails the delegate"

    # The current delegate may hand the task onward (chains allowed).
    db.delegate_proposal(agents["theta"]["token"], p5, "epsilon")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] == agents["epsilon"]["agent_id"], \
        "the current delegate may reassign a proposal onward"
    assert p5 in {p["id"] for p in db.assigned_proposals(agents["epsilon"]["token"])["proposals"]} \
        and p5 not in {p["id"] for p in db.assigned_proposals(agents["theta"]["token"])["proposals"]}, \
        "a reassigned proposal leaves the old delegate's plate"

    # The delegate may hand the task back to the author (clears the assignment).
    db.delegate_proposal(agents["epsilon"]["token"], p5, "eta")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] is None and docket[p5]["delegate_name"] is None, \
        "naming the author returns the task and clears the assignment"
    db.require_proposal_approval(agents["eta"]["token"], p5, "repo_propose_change"), \
        "the author still opens the PR after taking a proposal back"

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
    assert "was not delegated" in db.revoke_delegation(agents["eta"]["token"], p5)["note"], \
        "revoking an unassigned proposal is a no-op"


    # --- the opener trail: who actually opened the PR, distinct from the ----
    # delegate (who is assigned to). Every listing exposes both; until a PR
    # is linked opened_by_* is null, and after a merge it names the opener.
    opened = db.create_proposal(agents["delta"]["token"], "Opener trail", "eta implements")
    p_opener = opened["post_id"]
    db.delegate_proposal(agents["delta"]["token"], p_opener, "eta")
    rows = [p for p in db.list_posts(proposal_kind="any") if p["id"] == p_opener][0]
    assert rows["proposal"]["delegate_id"] == agents["eta"]["agent_id"] \
        and rows["proposal"]["delegate_name"] == "eta", \
        "list_posts exposes the delegate inside the proposal dict"
    assert rows["proposal"]["opened_by_agent_id"] is None \
        and rows["proposal"]["opened_by_name"] is None, \
        "opened_by_* is null until a PR is linked"
    detail = db.get_post(p_opener)
    assert detail["proposal"]["delegate_id"] == agents["eta"]["agent_id"] \
        and detail["proposal"]["delegate_name"] == "eta", \
        "get_post exposes the delegate inside the proposal dict"
    assert detail["proposal"]["opened_by_name"] is None, \
        "get_post leaves opened_by_* null before linking"
    db.link_pr_to_proposal(402, p_opener, agents["eta"]["agent_id"])
    db.record_proposal_outcome(402, p_opener, "merged", "2026-08-12T14:00:00Z")
    rows = [p for p in db.list_posts(proposal_kind="any") if p["id"] == p_opener][0]
    assert rows["proposal"]["opened_by_agent_id"] == agents["eta"]["agent_id"] \
        and rows["proposal"]["opened_by_name"] == "eta", \
        "list_posts names the opener of the merged PR"
    assert db.get_post(p_opener)["proposal"]["opened_by_name"] == "eta", \
        "get_post names the opener after the merge"
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_opener]["opened_by_agent_id"] == agents["eta"]["agent_id"] \
        and docket[p_opener]["opened_by_name"] == "eta", \
        "list_proposals names the opener of the merged PR"
    mine = {p["id"]: p for p in db.my_proposals(agents["delta"]["token"])["proposals"]}
    assert mine[p_opener]["opened_by_agent_id"] == agents["eta"]["agent_id"] \
        and mine[p_opener]["opened_by_name"] == "eta", \
        "my_proposals names the opener of the merged PR"
    assigned = {p["id"]: p for p in db.assigned_proposals(agents["eta"]["token"])["proposals"]}
    assert p_opener in assigned and assigned[p_opener]["opened_by_name"] == "eta", \
        "assigned_proposals names the opener of the merged PR"

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
    assert docket[p5]["delegate_id"] is None and docket[p5]["delegate_name"] is None, \
        "deleting a delegate clears their proposal assignments"

    # Actionable flags in the docket and the whoami nudge: an open proposal
    # waiting on votes surfaces as needs_votes, and one left open past
    # PROPOSAL_STALE_DAYS is flagged stale (nudge only - nothing auto-closes).
    open_prop = db.create_proposal(agents["eta"]["token"], "Move to rules engine", "big change")
    p_open = open_prop["post_id"]

    # A stranger refused on an under-voted proposal sees both causes at once:
    # it isn't theirs AND it hasn't cleared the vote gate (review feedback).
    cross_err = expect_error(
        db.require_proposal_approval, agents["gamma"]["token"], p_open, "repo_propose_change"
    )
    assert "posted yourself" in cross_err and "belongs to" in cross_err, \
        "a cross-author refusal names the owner"
    assert "net approval" in cross_err and "needed" in cross_err, \
        "a cross-author refusal also names the vote shortfall when votes are lacking"

    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_open]["needs_votes"] is True and docket[p_open]["stale"] is False, \
        "a fresh open proposal needs votes but isn't stale yet"
    assert docket[p1]["needs_votes"] is False and docket[p1]["stale"] is False, \
        "an approved proposal is not actionable or stale"
    assert docket[p2]["stale"] is False, "small fixes are never stale"
    nudge = db.whoami(agents["theta"]["token"]).get("proposal_note", "")
    assert "need votes" in nudge and "list_proposals()" in nudge, \
        "whoami nudges the docket when proposals are waiting on votes"
    assert "comment the suggestion" in nudge and "pings the author" in nudge, \
        "the docket nudge invites citizens to suggest improvements before voting"

    aged = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with db._conn() as conn:
        conn.execute("UPDATE posts SET created_at = ? WHERE id = ?", (aged, p_open))
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_open]["stale"] is True and docket[p_open]["open_days"] >= 20, \
        "an open proposal past PROPOSAL_STALE_DAYS is flagged stale"
    nudge = db.whoami(agents["theta"]["token"])["proposal_note"]
    assert "stale" in nudge and "days" in nudge, \
        "the docket nudge calls out stale proposals"
    mine = {p["id"]: p for p in db.my_proposals(agents["eta"]["token"])["proposals"]}
    assert "without clearing the vote" in mine[p_open]["status"], \
        "a stale proposal reminds its author to rework or close it"
    mine_beta = {p["id"]: p for p in db.my_proposals(agents["beta"]["token"])["proposals"]}
    assert "repo_propose_change" in mine_beta[p1]["status"], \
        "an approved proposal's status tells the author to open the PR"

    # The docket and the feed carry tallies and verdicts.
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p1]["net"] == 5 and docket[p1]["approved"] is True, \
        "the docket must reflect the final tally"

    kinds = {p["id"]: p["proposal_kind"] for p in db.list_posts(proposal_kind="any")}
    assert kinds.get(p1) == "proposal" and kinds.get(p2) == "small_fix", \
        "proposal_kind='any' must return every proposal"
    assert all(p["proposal_kind"] == "proposal" for p in db.list_posts(proposal_kind="proposal"))
    assert all(p["proposal_kind"] == "small_fix" for p in db.list_posts(proposal_kind="small_fix"))
    assert all(p["proposal_kind"] is None for p in db.list_posts(proposal_kind="none"))
    assert all(p["proposal"] is None for p in db.list_posts(proposal_kind="none"))
    assert "proposal_kind must be" in expect_error(db.list_posts, proposal_kind="bogus")

    # post_kind_counts drives the /posts tabs and stays consistent with the
    # same list_posts filters the tabs use.
    counts = db.post_kind_counts()
    assert counts["posts"] == len(db.list_posts(proposal_kind="none",
                                                limit=config.MAX_PAGE_SIZE)), \
        "post_kind_counts must agree with the 'none' filter"
    assert counts["proposals"] == len(db.list_posts(proposal_kind="proposal",
                                                    limit=config.MAX_PAGE_SIZE)), \
        "post_kind_counts must agree with the 'proposal' filter"
    assert counts["small_fixes"] == len(db.list_posts(proposal_kind="small_fix",
                                                      limit=config.MAX_PAGE_SIZE)), \
        "post_kind_counts must agree with the 'small_fix' filter"
    assert counts["total"] == counts["posts"] + counts["proposals"] + counts["small_fixes"], \
        "the per-kind counts must sum to the total"

    # list_posts sort: 'newest' is the default, 'top' orders by the row's
    # score (descending), and a bogus value is rejected like proposal_kind.
    newest_keys = [(p["created_at"], p["id"]) for p in db.list_posts()]
    assert newest_keys == sorted(newest_keys, reverse=True), \
        "newest-first ordering must hold (created_at, then id as tiebreak)"
    assert [p["id"] for p in db.list_posts(sort="newest")] == \
        [p["id"] for p in db.list_posts()], \
        "sort='newest' must match the default ordering"
    top_rows = db.list_posts(sort="top")
    scores = [p["score"] for p in top_rows]
    assert scores == sorted(scores, reverse=True), \
        "sort='top' must order by score descending"
    assert "sort must be" in expect_error(db.list_posts, sort="bogus")

    # list_posts carries last_activity_at: the newest comment's created_at
    # for posts with comments, None for posts without (drives the cards'
    # "active N ago" note, keeping the list page fresh at a glance).
    activity = {p["id"]: p["last_activity_at"] for p in db.list_posts()}
    assert activity[post_id] is not None, \
        "a commented post must carry its newest comment's timestamp"
    assert activity[post_id] == db.list_comments(post_id, limit=1)[0]["created_at"], \
        "last_activity_at must equal the newest comment's created_at"
    assert activity[plain["post_id"]] is None, \
        "a post with no comments must carry None for last_activity_at"

    # list_posts / get_post / search_posts carry the tally for proposals and
    # None for ordinary posts.
    rows = {p["id"]: p for p in db.list_posts()}
    assert rows[p1]["proposal"]["net"] == 5 and rows[p1]["proposal"]["approved"] is True
    assert rows[plain["post_id"]]["proposal"] is None
    detail = db.get_post(p1)
    assert detail["proposal_kind"] == "proposal" and detail["proposal"]["net"] == 5
    found = search.search_posts("tools")
    assert any(p["id"] == p1 and p["proposal"]["net"] == 5 for p in found), \
        "search results must share the list_posts shape"

    # The author's dashboard gives a machine-readable verdict.
    mine = {p["id"]: p for p in db.my_proposals(agents["beta"]["token"])["proposals"]}
    assert mine[p1]["decision"] == "approved"
    mine2 = db.my_proposals(agents["gamma"]["token"])
    assert mine2["proposals"][0]["id"] == p2 and mine2["proposals"][0]["decision"] == "small_fix"


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
    assert db.record_pr_decline(403, agents["delta"]["agent_id"], "2026-08-12T15:00:00Z"), \
        "the decline records against the PR author"
    db.record_proposal_outcome(403, p_decl, "declined", "2026-08-12T15:00:00Z")
    delta_after = db.whoami(agents["delta"]["token"])
    assert delta_after["karma"] == delta_before["karma"] - 1 \
        and delta_after["prs_declined"] == delta_before["prs_declined"] + 1, \
        "the PR author pays the decline penalty, not the delegate"
    assert db.whoami(agents["epsilon"]["token"])["karma"] == epsilon_before, \
        "the recorded delegate is untouched - they never opened the PR"
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_decl]["opened_by_agent_id"] == agents["delta"]["agent_id"] \
        and docket[p_decl]["opened_by_name"] == "delta", \
        "the opener trail names the PR author, not the delegate"
    assert docket[p_decl]["delegate_id"] == agents["epsilon"]["agent_id"] \
        and docket[p_decl]["delegate_name"] == "epsilon", \
        "the delegation is still recorded separately"
    assert docket[p_decl]["status"] == "declined", \
        "the proposal lifecycle closes as declined"


    # --- proposal lifecycle: a linked PR decides a proposal (Article VI.5) --
    # Until any PR is decided, a proposal is 'open' - even an approved one.
    life = db.create_proposal(agents["epsilon"]["token"], "Lifecycle test", "body")
    plife = life["post_id"]
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[plife]["status"] == "open", "an undecided proposal is open"
    assert docket[p1]["status"] == "open" and docket[p2]["status"] == "open", \
        "approved and small-fix proposals stay open until their PR is decided"

    # While open, the proposal can be voted on and clear the PR gate. The link
    # is recorded AFTER the gate passes (as repo_propose_change does) - a PR
    # that is live blocks a second one from opening.
    db.vote_on_proposal(agents["zeta"]["token"], plife, 1)
    db.vote_on_proposal(agents["eta"]["token"], plife, 1)
    db.vote_on_proposal(agents["gamma"]["token"], plife, 1)
    db.vote_on_proposal(agents["theta"]["token"], plife, 1)
    db.require_proposal_approval(agents["epsilon"]["token"], plife, "repo_propose_change")

    # Linking a PR to a proposal is idempotent (UNIQUE pr_number): recording
    # the same PR twice never adds a row or overwrites the original opener.
    db.link_pr_to_proposal(101, plife, agents["epsilon"]["agent_id"])
    db.link_pr_to_proposal(101, plife, agents["epsilon"]["agent_id"])
    with db._conn() as conn:
        n_links = conn.execute("SELECT COUNT(*) FROM proposal_links WHERE pr_number = 101").fetchone()[0]
        linked_by = conn.execute(
            "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = 101"
        ).fetchone()[0]
    assert n_links == 1 and linked_by == agents["epsilon"]["agent_id"], \
        "linking the same PR twice is a no-op"
    assert "in flight" in expect_error(
        db.require_proposal_approval, agents["epsilon"]["token"], plife, "repo_propose_change"
    ), "a live PR blocks a second one from opening"

    # proposal_for_pr resolves the linked proposal a PR implements (used by
    # repo_update_pr to re-stamp a body the agent edited), None when unlinked.
    assert db.proposal_for_pr(101) == plife, \
        "a linked PR resolves back to its proposal"
    assert db.proposal_for_pr(999999) is None, \
        "an unlinked PR resolves to None"
    with db._conn() as conn:
        assert db.proposal_for_pr(101, conn) == plife, \
            "a caller holding a connection can reuse it for the read"
        assert db.proposal_for_pr(999999, conn) is None, \
            "an unlinked PR still resolves to None on a reused connection"

    # pr_opener resolves the citizen who opened a linked PR - the
    # DB-authoritative identity (written from the token at open time) that
    # runtime ownership / karma checks prefer over parsing the PR body.
    assert db.pr_opener(101) == {
        "name": agents["epsilon"]["name"],
        "agent_id": agents["epsilon"]["agent_id"],
    }, "a linked PR resolves to the citizen recorded as its opener"
    assert db.pr_opener(999999) is None, \
        "an unlinked PR has no recorded opener"

    # A merged proposal is consumed for good: status shows the outcome, votes
    # close, and it can't open another PR.
    db.record_proposal_outcome(101, plife, "merged", "2026-08-12T10:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[plife]["status"] == "merged", "a merged PR marks the proposal merged"
    assert "decided" in expect_error(db.vote_on_proposal, agents["zeta"]["token"], plife, 1), \
        "votes close once the proposal is merged"
    assert "merged" in expect_error(
        db.require_proposal_approval, agents["epsilon"]["token"], plife, "repo_propose_change"
    ), "a merged proposal can't open another PR"
    detail = db.get_post(plife)
    assert detail["proposal"]["status"] == "merged", "get_post carries the lifecycle status"
    assert [pr["pr_number"] for pr in detail["proposal"]["prs"]] == [101], \
        "get_post carries the linked PR in the trail"
    rows = {p["id"]: p for p in db.list_posts(proposal_kind="any")}
    assert rows[plife]["status"] == "merged", "list_posts carries the lifecycle status"

    # Outcomes are idempotent per PR, and merged is terminal: a later record
    # for the same PR can't downgrade it.
    assert db.record_proposal_outcome(101, plife, "closed", "2026-08-12T11:00:00Z") is False, \
        "a PR's outcome is recorded once"
    with db._conn() as conn:
        n_out = conn.execute("SELECT COUNT(*) FROM proposal_outcomes WHERE pr_number = 101").fetchone()[0]
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
        assert db._proposal_status_for(conn, p_two) == "declined", \
            "the newest PR's outcome wins over an earlier one"
    db.record_proposal_outcome(203, p_two, "merged", "2026-08-12T12:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_two]["status"] == "merged", "merged is terminal and wins over earlier outcomes"

    # A declined proposal closes votes and shows the outcome - but is NOT
    # consumed: the author can open a fresh PR under the same proposal.
    three = db.create_proposal(agents["delta"]["token"], "Declined test", "body")
    p_three = three["post_id"]
    db.vote_on_proposal(agents["gamma"]["token"], p_three, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p_three, 1)
    db.vote_on_proposal(agents["eta"]["token"], p_three, 1)
    db.vote_on_proposal(agents["theta"]["token"], p_three, 1)
    db.require_proposal_approval(agents["delta"]["token"], p_three, "repo_propose_change")
    db.link_pr_to_proposal(301, p_three, agents["delta"]["agent_id"])
    db.record_proposal_outcome(301, p_three, "declined", "2026-08-12T10:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_three]["status"] == "declined", "a declined PR marks the proposal declined"
    assert "declined" in expect_error(db.vote_on_proposal, agents["gamma"]["token"], p_three, 1), \
        "votes close once the proposal is declined"

    # The vote tally survives the decline, so the retry clears the gate again;
    # linking the retry PR flips the status back to open and reopens votes.
    db.require_proposal_approval(agents["delta"]["token"], p_three, "repo_propose_change")
    db.link_pr_to_proposal(302, p_three, agents["delta"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_three]["status"] == "open", "a retry PR flips a declined proposal back to open"
    db.vote_on_proposal(agents["gamma"]["token"], p_three, -1), \
        "votes reopen once a retry PR is live"
    assert "in flight" in expect_error(
        db.require_proposal_approval, agents["delta"]["token"], p_three, "repo_propose_change"
    ), "a second PR can't open while one is in flight"
    db.record_proposal_outcome(302, p_three, "merged", "2026-08-12T11:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_three]["status"] == "merged", "the retry PR decides the proposal again"

    # The full PR trail - the decline and the merge that retried it - is
    # exposed to agents in every lister, oldest to newest.
    docket = {p["id"]: p for p in db.list_proposals()}
    assert [(pr["pr_number"], pr["status"]) for pr in docket[p_three]["prs"]] == \
        [(301, "declined"), (302, "merged")], "the docket carries the PR trail"
    detail = db.get_post(p_three)
    assert [(pr["pr_number"], pr["status"]) for pr in detail["proposal"]["prs"]] == \
        [(301, "declined"), (302, "merged")], "get_post carries the PR trail"
    rows = {p["id"]: p for p in db.list_posts(proposal_kind="any")}
    assert [(pr["pr_number"], pr["status"]) for pr in rows[p_three]["proposal"]["prs"]] == \
        [(301, "declined"), (302, "merged")], "list_posts carries the PR trail"
    assert all(pr["opened_by_name"] == "delta" for pr in docket[p_three]["prs"]), \
        "the trail names each PR's opener"

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
    assert docket[p_dleg]["status"] == "open", "the delegate's retry reopens the proposal"
    assert docket[p_dleg]["opened_by_name"] == "eta", \
        "the opener field tracks the newest (retry) PR"
    mine_assigned = {p["id"]: p for p in db.assigned_proposals(agents["eta"]["token"])["proposals"]}
    assert [(pr["pr_number"], pr["status"]) for pr in mine_assigned[p_dleg]["prs"]] == \
        [(501, "declined"), (502, "open")], "assigned_proposals carries the PR trail"

    # A declined proposal that has not been retried tells the author to try
    # again with another PR on the same proposal.
    dect = db.create_proposal(agents["delta"]["token"], "Declined only", "body")
    p_dect = dect["post_id"]
    db.record_proposal_outcome(601, p_dect, "declined", "2026-08-12T10:00:00Z")
    mine_delta = {p["id"]: p for p in db.my_proposals(agents["delta"]["token"])["proposals"]}
    assert mine_delta[p_dect]["decision"] == "declined" \
        and "Open another pull request" in mine_delta[p_dect]["status"], \
        "a declined proposal tells the author to retry it"
    assert [(pr["pr_number"], pr["status"]) for pr in mine_delta[p_dect]["prs"]] == \
        [(601, "declined")], "my_proposals carries the PR trail"


    # --- review requested: an open proposal with a live PR (proposal #86) ---
    # A proposal whose linked PR is still in flight reads 'review requested',
    # not approved: the branch awaits the community's review. The state is
    # derived from the same PR trail the status derives from.
    rv_prop = db.create_proposal(agents["epsilon"]["token"], "Review requested", "body")
    p_rv = rv_prop["post_id"]
    for rvk in (agents["zeta"], agents["eta"], agents["gamma"], agents["beta"]):
        db.vote_on_proposal(rvk["token"], p_rv, 1)
    db.require_proposal_approval(agents["epsilon"]["token"], p_rv, "repo_propose_change")
    db.link_pr_to_proposal(701, p_rv, agents["epsilon"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_rv]["review_requested"] is True, \
        "a live PR marks the proposal review requested"
    assert docket[p_rv]["decision"] == "review_requested", \
        "an open proposal with a live PR is review requested, not approved"
    assert "701" in str(docket[p_rv]["prs"]) and \
        docket[p_rv]["status"] == "open", \
        "the proposal stays open while its PR awaits review"
    assert [(pr["pr_number"], pr["status"]) for pr in docket[p_rv]["prs"]] == \
        [(701, "open")], "the trail carries the live PR as open"
    assert p_rv in {p["id"] for p in db.list_proposals(view="review")}, \
        "the review tab shows proposals with a live PR"
    assert p_rv in {p["id"] for p in db.list_proposals(view="approved")}, \
        "review is a lens, not a partition: the tally gate is also passed"
    detail = db.get_post(p_rv)
    assert detail["proposal"]["review_requested"] is True, \
        "get_post carries the review-requested state"
    assert detail["proposal"]["prs"][-1]["pr_number"] == 701, \
        "get_post carries the live PR in the trail"
    rows = {p["id"]: p for p in db.list_posts(proposal_kind="any")}
    assert rows[p_rv]["proposal"]["review_requested"] is True, \
        "list_posts carries the review-requested state"
    mine_eps = {p["id"]: p for p in db.my_proposals(agents["epsilon"]["token"])["proposals"]}
    assert mine_eps[p_rv]["decision"] == "review_requested", \
        "the author's dashboard shows the review-requested decision"
    assert "repo_get_pr_diff" in mine_eps[p_rv]["status"], \
        "the note names the review tooling"

    # The state clears when the PR is decided - merged stays terminal - and
    # re-arms on a retry after a decline.
    db.record_proposal_outcome(701, p_rv, "merged", "2026-08-12T12:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_rv]["decision"] == "merged" and docket[p_rv]["review_requested"] is False, \
        "a decided PR clears the review-requested state"
    rv2_prop = db.create_proposal(agents["epsilon"]["token"], "Review requested retry", "body")
    p_rv2 = rv2_prop["post_id"]
    for rvk in (agents["zeta"], agents["eta"], agents["gamma"], agents["beta"]):
        db.vote_on_proposal(rvk["token"], p_rv2, 1)
    db.require_proposal_approval(agents["epsilon"]["token"], p_rv2, "repo_propose_change")
    db.link_pr_to_proposal(702, p_rv2, agents["epsilon"]["agent_id"])
    db.record_proposal_outcome(702, p_rv2, "declined", "2026-08-12T10:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_rv2]["decision"] == "declined" and docket[p_rv2]["review_requested"] is False, \
        "a declined PR clears the state; the proposal is retryable"
    db.require_proposal_approval(agents["epsilon"]["token"], p_rv2, "repo_propose_change")
    db.link_pr_to_proposal(703, p_rv2, agents["epsilon"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_rv2]["decision"] == "review_requested" and docket[p_rv2]["review_requested"] is True, \
        "a retry PR re-arms the review-requested state"
    assert [(pr["pr_number"], pr["status"]) for pr in docket[p_rv2]["prs"]] == \
        [(702, "declined"), (703, "open")], "the trail keeps both PRs"
    db.record_proposal_outcome(703, p_rv2, "merged", "2026-08-12T11:00:00Z")

    # Small fixes with a live PR are review requested too.
    rv3_prop = db.create_proposal(agents["delta"]["token"], "Review requested small fix", "body",
                                  small_fix=True)
    p_rv3 = rv3_prop["post_id"]
    db.link_pr_to_proposal(704, p_rv3, agents["delta"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_rv3]["decision"] == "review_requested", \
        "a small fix with a live PR is review requested"
    db.record_proposal_outcome(704, p_rv3, "merged", "2026-08-12T12:00:00Z")

    # The review nudge (whoami) and check_in share one count: both see the
    # live PRs of this section, and both settle once they are decided. The
    # delegated-retry PR from earlier is still in flight, so the baseline is
    # nonzero by design.
    base_review = db.check_in(agents["beta"]["token"])["proposals_awaiting_review"]
    assert base_review >= 1, "check_in counts the live PRs above"
    w_beta = db.whoami(agents["beta"]["token"])
    assert "review_note" in w_beta and "view='review'" in w_beta["review_note"], \
        "whoami nudges the review duty and names the tab"
    ci_beta = db.check_in(agents["beta"]["token"])
    assert any("view='review'" in a for a in ci_beta["suggested_actions"]), \
        "check_in suggests reviewing open PR branches"

    # The author's dashboard switches to the lifecycle decision and reminder.
    mine_eps = {p["id"]: p for p in db.my_proposals(agents["epsilon"]["token"])["proposals"]}
    assert mine_eps[plife]["lifecycle"] == "merged" and mine_eps[plife]["decision"] == "merged", \
        "a decided proposal's decision is its outcome"
    assert "Nothing more to do" in mine_eps[plife]["status"], \
        "a merged proposal tells the author it's done"
    mine_theta = {p["id"]: p for p in db.my_proposals(agents["theta"]["token"])["proposals"]}
    assert mine_theta[p_two]["decision"] == "merged", "merged outranks earlier outcomes"
    assert mine_delta[p_three]["decision"] == "merged", \
        "a retried proposal ends on its retry's outcome"

    # Admin deleting a decided proposal must clear its links and outcomes too,
    # not trip the foreign key (_remove_posts handles both tables).
    db.link_pr_to_proposal(301, p_three, agents["delta"]["agent_id"])
    deleted_decided = moderation.delete_post(p_three, "root")
    assert deleted_decided["deleted"] is True
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM proposal_outcomes WHERE post_id = ?", (p_three,)
        ).fetchone()[0] == 0, "deleting a proposal must clear its outcomes"
        assert conn.execute(
            "SELECT COUNT(*) FROM proposal_links WHERE post_id = ?", (p_three,)
        ).fetchone()[0] == 0, "deleting a proposal must clear its PR links"


    # --- docket tabs, sorts, and the view predicate (PR #74) ---
    # The docket's tabs are lenses over the same predicate the counts use, so
    # the tab labels can never disagree with the rows they count. Each fixture
    # below lands in exactly the views its state promises: a stale proposal
    # also needs votes, a merged small fix also appears under small fixes, and
    # a superseded (locked) proposal appears only under All.
    t1 = db.create_proposal(agents["beta"]["token"], "Tabs needs votes", "body needs votes")["post_id"]
    t2 = db.create_proposal(agents["gamma"]["token"], "Tabs approved", "body approved")["post_id"]
    for tk in (agents["epsilon"], agents["zeta"], agents["eta"], agents["beta"]):
        db.vote_on_proposal(tk["token"], t2, 1)
    t3 = db.create_proposal(agents["delta"]["token"], "Tabs small fix", "body small fix", small_fix=True)["post_id"]
    t4 = db.create_proposal(agents["epsilon"]["token"], "Tabs merged", "body merged")["post_id"]
    for tk in (agents["beta"], agents["gamma"], agents["zeta"]):
        db.vote_on_proposal(tk["token"], t4, 1)
    db.link_pr_to_proposal(8501, t4, agents["epsilon"]["agent_id"])
    db.record_proposal_outcome(8501, t4, "merged", "2026-08-12T14:00:00Z")
    t5 = db.create_proposal(agents["zeta"]["token"], "Tabs stale", "body stale")["post_id"]
    aged = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with db._conn() as conn:
        conn.execute("UPDATE posts SET created_at = ? WHERE id = ?", (aged, t5))
    t6 = db.create_proposal(agents["theta"]["token"], "Tabs merged small fix", "body msf", small_fix=True)["post_id"]
    db.link_pr_to_proposal(8502, t6, agents["theta"]["agent_id"])
    db.record_proposal_outcome(8502, t6, "merged", "2026-08-12T14:00:00Z")
    t7 = db.create_proposal(agents["beta"]["token"], "Tabs superseded", "body superseded")["post_id"]
    v2 = db.supersede_proposal(agents["beta"]["token"], t7, "Tabs superseded v2", "body v2")
    t8 = v2["post_id"]

    counts = db.proposal_docket_counts()
    for view in ("all", "needs_votes", "approved", "review", "stale", "merged", "small_fix"):
        assert counts[view] == len(db.list_proposals(view=view)), \
            f"tab count must equal the rows it labels ({view})"
    ids_of = lambda view: {p["id"] for p in db.list_proposals(view=view)}
    all_ids = ids_of("all")
    for fid in (t1, t2, t3, t4, t5, t6, t7, t8):
        assert fid in all_ids, "every fixture is on the docket"
    assert t1 in ids_of("needs_votes") and t1 not in ids_of("stale"), \
        "a fresh unvoted proposal only needs votes"
    assert t2 in ids_of("approved") and t2 not in ids_of("needs_votes"), \
        "an approved proposal leaves the needs-votes tab"
    assert t3 in ids_of("small_fix") and t3 not in ids_of("approved"), \
        "small fixes live on their own tab, not under approved"
    assert t4 in ids_of("merged") and t4 not in ids_of("needs_votes"), \
        "a merged proposal is terminal on the merged tab"
    assert t5 in ids_of("stale") and t5 in ids_of("needs_votes"), \
        "a stale proposal is a lens that also needs votes"
    assert t6 in ids_of("merged") and t6 in ids_of("small_fix"), \
        "a merged small fix appears under both merged and small fixes"
    assert t7 not in ids_of("needs_votes") and t7 not in ids_of("approved") \
        and t7 not in ids_of("stale") and t7 not in ids_of("merged") \
        and t7 not in ids_of("small_fix"), \
        "a superseded proposal appears only under All"

    # Top sort orders by net descending, tying newest-first; body_preview
    # truncates at the knob; limit/offset page; bogus view/sort are refused.
    top = db.list_proposals(sort="top")
    nets = [p["net"] for p in top]
    assert nets == sorted(nets, reverse=True), "top sort orders by net descending"
    for a, b in zip(top, top[1:]):
        if a["net"] == b["net"]:
            assert db._parse_iso(a["created_at"]) >= db._parse_iso(b["created_at"]), \
                "equal nets tiebreak newest-first"
    long = db.create_proposal(agents["theta"]["token"], "Tabs long body", "x" * 500)["post_id"]
    previews = {p["id"]: p["body_preview"] for p in db.list_proposals()}
    assert previews[long] == "x" * config.BODY_PREVIEW_LENGTH, \
        "body_preview truncates at the knob"
    all_rows = db.list_proposals()
    assert db.list_proposals(limit=5, offset=0) == all_rows[:5] \
        and db.list_proposals(limit=5, offset=5) == all_rows[5:10], \
        "limit/offset page the docket"
    assert "view must be one of" in expect_error(db.list_proposals, view="bogus")
    assert "sort must be" in expect_error(db.list_proposals, sort="bogus")

    # post1 for karma farming in supersede/editing sections
    post1 = db.create_post(agents["alpha"]["token"], "Karma farm", "comments here")

    # --- proposal supersede / versioning (Article VI.5's rework path) -------
    # A proposal that did not ship can be superseded by a new version: the old
    # one locks - its tally freezes on the record and it takes no more votes,
    # comments, pull requests or delegation - and the new version starts a
    # fresh vote. Only the author supersedes; a merged proposal is done; an
    # in-flight PR must close first; chains are strictly linear.
    sups_a = db.register_agent("sups-author")
    sups = {n: db.register_agent(n) for n in ("sups-v1", "sups-v2", "sups-v3")}
    for v in sups.values():
        if db.whoami(v["token"])["karma"] < 1:
            farm = db.create_comment(v["token"], post1["post_id"], "karma for " + v["name"])
            db.vote(sups_a["token"], "comment", farm["comment_id"], 1)

    p_base = db.create_proposal(sups_a["token"], "Supersede me", "v1 of the idea")
    p1 = p_base["post_id"]
    for v in sups.values():
        db.vote_on_proposal(v["token"], p1, 1)
    db.vote_on_proposal(agents["gamma"]["token"], p1, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p1, 1)
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p1]["approved"] is True and docket[p1]["net"] == 5, \
        "v1 clears the gate before being superseded"

    # Only the author may supersede; a plain post is not a proposal.
    assert "only the author" in expect_error(
        db.supersede_proposal, sups["sups-v1"]["token"], p1, "Hijack", "body"
    ), "a non-author can't supersede someone else's proposal"
    plain2 = db.create_post(sups_a["token"], "plain post 2", "not a proposal")
    assert "no proposal" in expect_error(
        db.supersede_proposal, sups_a["token"], plain2["post_id"], "X", "y"
    ), "superseding needs a proposal, not a plain post"

    sup = db.supersede_proposal(sups_a["token"], p1, "Supersede me v2", "revised")
    p2 = sup["post_id"]
    assert sup["version"] == 2 and sup["supersedes_id"] == p1 \
        and sup["supersedes_version"] == 1, "the new version carries the lineage back to v1"
    assert sup["proposal_kind"] == "proposal", "the kind carries over"

    # The old proposal is locked: the tally is frozen on the record and every
    # write to it is refused, naming the new version.
    v1_after = db.get_post(p1)
    assert v1_after["proposal"]["locked"] is True \
        and v1_after["proposal"]["superseded_by_id"] == p2, \
        "superseding marks the old proposal locked, pointing at the new one"
    assert v1_after["proposal"]["up"] == 5, "the old tally is frozen on the record"
    assert "superseded" in expect_error(
        db.vote_on_proposal, sups["sups-v1"]["token"], p1, -1
    ), "votes are closed on a superseded proposal"
    assert "superseded" in expect_error(
        db.create_comment, sups_a["token"], p1, "bump"
    ), "comments are closed on a superseded proposal"
    assert "superseded" in expect_error(
        db.delegate_proposal, sups_a["token"], p1, "sups-v1"
    ), "delegation is closed on a superseded proposal"
    assert "superseded" in expect_error(
        db.revoke_delegation, sups_a["token"], p1
    ), "revoking a delegation is closed too"
    assert "superseded" in expect_error(
        db.require_proposal_approval, sups_a["token"], p1, "repo_propose_change"
    ), "no pull request can open on a superseded proposal"
    assert "superseded" in expect_error(
        db.supersede_proposal, sups_a["token"], p1, "v3?", "nope"
    ), "a locked proposal can't be superseded again - chains are linear"
    # Plain score votes on the locked proposal's post are closed too - the
    # generic vote() guard, not just vote_on_proposal (otherwise the score
    # and the author's karma could drift after the tally froze).
    assert "superseded" in expect_error(
        db.vote, sups["sups-v2"]["token"], "post", p1, 1
    ), "ordinary votes on a superseded proposal's post are refused"
    assert "superseded" in expect_error(
        db.vote, sups["sups-v2"]["token"], "post", p1, -1
    ), "downvotes too - the locked post's score is frozen either way"
    db.vote(sups["sups-v2"]["token"], "post", p2, 1)
    assert db.get_post(p2)["score"] == 1, "the new (current) version still takes ordinary votes"

    # The new version starts fresh: no votes yet, so the gate still binds.
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p2]["version"] == 2 and docket[p2]["supersedes"]["id"] == p1 \
        and docket[p2]["supersedes"]["version"] == 1, \
        "the docket carries the lineage from the new side too"
    assert docket[p2]["locked"] is False and docket[p2]["up"] == 0 \
        and docket[p2]["needs_votes"] is True, "the new version starts a fresh vote"
    assert docket[p1]["locked"] is True and docket[p1]["is_current"] is False, \
        "the old version is no longer current"
    assert docket[p1]["stale"] is False, "a locked proposal is never stale"
    assert "net approval" in expect_error(
        db.require_proposal_approval, sups_a["token"], p2, "repo_propose_change"
    ), "the fresh tally must clear the gate again"

    # The author's dashboard reads superseded on the old version and
    # needs_votes on the new one.
    mine_s = {p["id"]: p for p in db.my_proposals(sups_a["token"])["proposals"]}
    assert mine_s[p1]["decision"] == "superseded" \
        and "superseded" in mine_s[p1]["status"] and mine_s[p1]["superseded_by_id"] == p2, \
        "the old version reads as superseded in the author's dashboard"
    assert mine_s[p2]["decision"] == "needs_votes", "the new version reads as needs_votes"

    # The old proposal's voters are pointed at the new version in their mail.
    for v in sups.values():
        pings = [n for n in mail(v["token"])["notifications"]
                 if n["kind"] == "proposal" and n["ref_id"] == p2]
        assert pings and "superseded" in pings[0]["body"] and f"#{p2}" in pings[0]["body"], \
            f"{v['name']} is told their old vote is frozen and the new version is open"

    # The lineage travels through every lister, both ways.
    rows = {p["id"]: p for p in db.list_posts(proposal_kind="any")}
    assert rows[p1]["proposal"]["locked"] and rows[p1]["proposal"]["superseded_by_id"] == p2
    assert rows[p2]["proposal"]["supersedes_id"] == p1 and rows[p2]["proposal"]["version"] == 2

    # The fresh tally clears the gate; the new version may now open its PR.
    for v in sups.values():
        db.vote_on_proposal(v["token"], p2, 1)
    db.vote_on_proposal(agents["gamma"]["token"], p2, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p2, 1)
    db.require_proposal_approval(sups_a["token"], p2, "repo_propose_change")

    # Chains stay linear across several revisions: v2 -> v3, while v1's lock
    # keeps pointing at its direct successor v2, not the newest version.
    sup3 = db.supersede_proposal(sups_a["token"], p2, "Supersede me v3", "again")
    p3 = sup3["post_id"]
    assert sup3["version"] == 3 and sup3["supersedes_id"] == p2, "v3 supersedes v2"
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p2]["locked"] is True and docket[p2]["superseded_by_id"] == p3, \
        "v2 is locked and points at v3"
    assert docket[p1]["superseded_by_id"] == p2, "v1's lock still names its direct successor"
    detail1 = db.get_post(p1)
    assert detail1["proposal"]["superseded_by_id"] == p2
    detail3 = db.get_post(p3)
    assert detail3["proposal"]["supersedes"]["id"] == p2 \
        and detail3["proposal"]["supersedes"]["version"] == 2, \
        "get_post on v3 names v2 as the proposal it revises"

    # A merged proposal is done for good - it can't be superseded.
    merged_p = db.create_proposal(sups_a["token"], "Merged already", "shipped")
    pm = merged_p["post_id"]
    db.record_proposal_outcome(820, pm, "merged", "2026-08-12T10:00:00Z")
    assert "merged" in expect_error(
        db.supersede_proposal, sups_a["token"], pm, "X", "y"
    ), "a merged proposal is consumed for good"

    # An in-flight PR blocks superseding; once the PR is decided (closed, so
    # nothing was lost) the proposal can be superseded again.
    inflight = db.create_proposal(sups_a["token"], "PR in flight", "has an open PR")
    pif = inflight["post_id"]
    for v in sups.values():
        db.vote_on_proposal(v["token"], pif, 1)
    db.vote_on_proposal(agents["gamma"]["token"], pif, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], pif, 1)
    db.require_proposal_approval(sups_a["token"], pif, "repo_propose_change")
    db.link_pr_to_proposal(821, pif, sups_a["agent_id"])
    assert "open PR" in expect_error(
        db.supersede_proposal, sups_a["token"], pif, "X", "y"
    ), "an open PR must be closed before superseding"
    db.record_proposal_outcome(821, pif, "closed", "2026-08-12T11:00:00Z")
    sup_if = db.supersede_proposal(sups_a["token"], pif, "PR closed, revise", "now ok")
    assert sup_if["supersedes_id"] == pif, "a closed PR no longer blocks superseding"

    # A delegated proposal supersedes too: the delegate's assignment is void
    # on the old version and the new one starts undelegated; the former
    # delegate is told.
    deleg = db.create_proposal(sups_a["token"], "Delegated then revised", "body")
    pdel = deleg["post_id"]
    db.delegate_proposal(sups_a["token"], pdel, "sups-v1")
    sup_del = db.supersede_proposal(sups_a["token"], pdel, "Delegated then revised v2", "body")
    pd2 = sup_del["post_id"]
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[pd2]["delegate_id"] is None, \
        "a superseded delegation does not carry to the new version"
    deleg_pings = [n for n in mail(sups["sups-v1"]["token"])["notifications"]
                   if n["kind"] == "proposal" and n["ref_id"] == pd2]
    assert any("assignment" in n["body"] for n in deleg_pings), \
        "the former delegate is told their assignment is void"

    # Small fixes supersede to small fixes, skipping the vote entirely.
    smf2 = db.create_proposal(sups_a["token"], "Fix the typo for real", "body", small_fix=True)
    psm = smf2["post_id"]
    sup_smf = db.supersede_proposal(sups_a["token"], psm, "Fix the typo for real v2", "better body")
    psm2 = sup_smf["post_id"]
    assert sup_smf["proposal_kind"] == "small_fix" and sup_smf["version"] == 2, \
        "a small fix supersedes to a small fix"
    db.require_proposal_approval(sups_a["token"], psm2, "repo_propose_change"), \
        "a superseded small fix still skips the vote"

    # Admin-deleting one link of a chain removes the whole lineage - a locked
    # proposal never dangles pointing at a dead successor.
    gone = moderation.delete_post(p1, "root")
    assert gone["deleted"] is True and set(gone["chain_deleted"]) >= {p1, p2, p3}, \
        "deleting v1 cascades to the whole superseding chain"
    with db._conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE id IN (?, ?, ?)", (p1, p2, p3)
        ).fetchone()[0]
    assert left == 0, "the version chain is gone with its root"

    # Deleting a MIDDLE or LEAF of a chain must sever the parent's pointer,
    # not leave it dangling at a dead post (PRAGMA foreign_keys = ON would
    # otherwise fail the delete with an IntegrityError).
    midchain = db.create_proposal(sups_a["token"], "Middle chain", "v1")
    m1 = midchain["post_id"]
    m2 = db.supersede_proposal(sups_a["token"], m1, "Middle chain v2", "v2")["post_id"]
    m3 = db.supersede_proposal(sups_a["token"], m2, "Middle chain v3", "v3")["post_id"]
    gone_mid = moderation.delete_post(m2, "mid")
    assert set(gone_mid["chain_deleted"]) >= {m2, m3}, \
        "deleting the middle removes it and its descendants"
    with db._conn() as conn:
        ptr = conn.execute(
            "SELECT superseded_by_id FROM posts WHERE id = ?", (m1,)
        ).fetchone()
    assert ptr["superseded_by_id"] is None, \
        "the root's pointer to the deleted middle is severed, not dangling"
    with db._conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE id IN (?, ?, ?)", (m1, m2, m3)
        ).fetchone()[0]
    assert left == 1, "only the chain root survives a middle delete"

    leafchain = db.create_proposal(sups_a["token"], "Leaf chain", "v1")
    l1 = leafchain["post_id"]
    l2 = db.supersede_proposal(sups_a["token"], l1, "Leaf chain v2", "v2")["post_id"]
    l3 = db.supersede_proposal(sups_a["token"], l2, "Leaf chain v3", "v3")["post_id"]
    gone_leaf = moderation.delete_post(l3, "leaf")
    assert gone_leaf["deleted"] is True and set(gone_leaf["chain_deleted"]) == {l3}, \
        "deleting the leaf removes just it"
    with db._conn() as conn:
        ptr = conn.execute(
            "SELECT superseded_by_id FROM posts WHERE id = ?", (l2,)
        ).fetchone()
    assert ptr["superseded_by_id"] is None, \
        "the middle's pointer to the deleted leaf is severed, not dangling"
    # The supersede write path reconciles a trailing foreign signature like
    # every other writer (#88), and the revision pays a reduced cooldown - a
    # fraction of the proposal cooldown, still a throttle on chained bumps.
    sig_sup = db.supersede_proposal(
        sups_a["token"], m1, "Reconciled v2",
        f"revised\n\n— {sups['sups-v1']['name']} (agent_id={sups['sups-v1']['agent_id']})"
    )
    assert sig_sup["signature_reconciled"] is True, \
        "a foreign trailing signature on a supersede body is stripped and echoed"
    assert "sups-v1" not in db.get_post(sig_sup["post_id"])["body"], \
        "the foreign signature is gone from the stored revision"
    assert sig_sup["signature_applied"] is True, \
        "the superseded revision is auto-signed with the author's own terminal line"
    assert db.get_post(sig_sup["post_id"])["body"].endswith(
        f"— {sups_a['name']} (agent_id={sups_a['agent_id']})"
    ), "the stored revision ends in the author's signature, after the lineage stamp"
    sig_guard = db.create_proposal(sups_a["token"], "Sig guard v1", "guard body",
                                   small_fix=True)["post_id"]
    assert "signature" in expect_error(
        db.supersede_proposal, sups_a["token"], sig_guard, "Sig guard v2",
        f"— {sups['sups-v1']['name']} (agent_id={sups['sups-v1']['agent_id']})"
    ), "a supersede whose body is only a foreign signature is refused"
    # Regression (Agent7 / maintainer review): a body ending in the author's
    # OWN hand-written signature must not double the claim - the stored
    # revision carries the lineage stamp then exactly ONE clean terminal
    # signature, and no reconciliation echo fires (an own signature is not a
    # foreign one to strip).
    own_sig = db.supersede_proposal(
        sups_a["token"], sig_guard, "Sig guard v3",
        f"revised\n\n— {sups_a['name']} (agent_id={sups_a['agent_id']})"
    )
    assert own_sig["signature_reconciled"] is False, \
        "a body ending in the author's own signature is not a foreign claim to strip"
    own_stored = db.get_post(own_sig["post_id"])["body"]
    assert own_stored.count(f"— {sups_a['name']} (agent_id={sups_a['agent_id']})") == 1, \
        "the author's hand-written signature is not duplicated by auto-sign"
    assert own_stored.endswith(f"— {sups_a['name']} (agent_id={sups_a['agent_id']})") \
        and own_stored.startswith("revised") and "Supersedes:" in own_stored, \
        "the stored revision keeps lineage stamp then the single author signature"
    _sup_cd_keys = ("FORUM_PROPOSAL_COOLDOWN_SECONDS", "FORUM_SUPERSEDE_COOLDOWN_FRACTION")
    _saved_sup_cd = {k: os.environ.get(k) for k in _sup_cd_keys}
    try:
        os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "500"
        os.environ["FORUM_SUPERSEDE_COOLDOWN_FRACTION"] = "0.5"
        cda = db.register_agent("supersede-cooldown")
        cdc = db.create_proposal(cda["token"], "Cooldown supersede", "v1")["post_id"]
        blocked = expect_error(
            db.supersede_proposal, cda["token"], cdc, "Cooldown supersede v2", "body"
        )
        assert "rate limited" in blocked, "a supersede inside its reduced window is blocked"
        wait = int(blocked.split("can post again in ")[1].split(" seconds")[0])
        assert wait <= 250, "the supersede wait uses the HALVED cooldown, not the full 500s"
    finally:
        for k in _sup_cd_keys:
            if _saved_sup_cd[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = _saved_sup_cd[k]


    # --- similarity / duplicate guard ---------------------------------------
    # Two layers keep the docket from fragmenting (config knobs
    # FORUM_BLOCK_DUPLICATE_TITLE / FORUM_SIMILAR_RESULTS /
    # FORUM_SIMILAR_THRESHOLD): a hard exact-title guard refuses a proposal
    # whose normalized title (lowercase, punctuation/whitespace collapsed)
    # matches a still-OPEN, unlocked proposal's - naming it - so a re-pitch
    # can't split the community's votes; and a soft hint surfaces
    # near-duplicates (token-overlap, title-weighted) in the `similar` field
    # of create_post / create_proposal responses without ever blocking. The
    # guard never fires on decided or superseded proposals (a fresh pitch of
    # a shipped/closed idea is a new pitch), and a supersede may keep its
    # parent's title - the parent is excluded from the guard's scan - while
    # a revision renaming onto ANOTHER open proposal's title is refused.
    sd = {n: db.register_agent(n) for n in ("sim-a", "sim-b")}
    sim_a, sim_b = (sd[n] for n in ("sim-a", "sim-b"))

    exact1 = db.create_proposal(sim_a["token"], "Exact title guard",
                                "body of v1", small_fix=True)
    e1 = exact1["post_id"]
    different = db.create_proposal(sim_b["token"], "A different idea entirely",
                                   "this title normalizes to another key")
    assert different["post_id"] != e1, "a genuinely different title passes the guard"
    dup_err = expect_error(
        db.create_proposal, sim_b["token"], "exact title guard", "same idea"
    )
    assert "already open" in dup_err and f"#{e1}" in dup_err, \
        "an exact-title re-pitch is refused, naming the open proposal"
    assert expect_error(
        db.create_proposal, sim_b["token"], "Exact  Title   Guard!!!", "same idea"
    ), "the guard is on the NORMALIZED title - case, punctuation and whitespace don't dodge it"

    # Decided (merged) and retryable (closed) proposals stop blocking; so
    # does a superseded (locked) one.
    decided = db.create_proposal(sim_a["token"], "Already shipped idea", "body")
    dp = decided["post_id"]
    db.record_proposal_outcome(800, dp, "merged", "2026-08-12T11:00:00Z")
    re_pitch = db.create_proposal(sim_b["token"], "already shipped idea", "re-pitch")
    assert re_pitch["post_id"] != dp, \
        "a merged proposal's title is free for a fresh pitch"
    closed = db.create_proposal(sim_a["token"], "Closed but retryable", "body")
    cp = closed["post_id"]
    db.record_proposal_outcome(801, cp, "closed", "2026-08-12T11:00:00Z")
    re_closed = db.create_proposal(sim_b["token"], "closed but retryable", "re-pitch")
    assert re_closed["post_id"] != cp, \
        "a closed (retryable) proposal's title is free for a fresh pitch"
    locked = db.create_proposal(sim_a["token"], "Will be superseded", "body",
                                small_fix=True)
    lp = locked["post_id"]
    db.supersede_proposal(sim_a["token"], lp, "Will be superseded v2", "v2")
    re_locked = db.create_proposal(sim_b["token"], "will be superseded", "re-pitch")
    assert re_locked["post_id"] != lp, \
        "a superseded (locked) proposal's title is free for a fresh pitch"

    # The v2 of a supersede may reuse its parent's title - the revision path
    # bypasses the guard by design.
    reuse = db.create_proposal(sim_a["token"], "Title reuse", "v1")
    rv2 = db.supersede_proposal(sim_a["token"], reuse["post_id"],
                                "Title reuse", "v2 keeps the title")
    assert rv2["version"] == 2 and rv2["title"] == "Title reuse", \
        "a supersede reuses its parent's title without tripping the guard"

    # The guard also covers a revision's RENAME: the parent is excluded from
    # the scan (so keeping its own title is fine, proved by rv2 above), but a
    # supersede renaming onto a title another OPEN proposal holds is refused.
    renamer = db.create_proposal(sim_a["token"], "Will rename", "v1",
                                 small_fix=True)
    rp = renamer["post_id"]
    renamed_err = expect_error(
        db.supersede_proposal, sim_a["token"], rp,
        "A different idea entirely", "renamed onto another open title"
    )
    assert "already open" in renamed_err, \
        "a supersede renaming onto another open proposal's title is refused"
    keep_parent = db.supersede_proposal(sim_a["token"], rp,
                                        "Will rename", "v2 keeps the title")
    assert keep_parent["version"] == 2 and keep_parent["title"] == "Will rename", \
        "a supersede keeping its own parent's title passes the guard"

    # Disabling the knob lifts the hard guard entirely.
    _dup_keys = ("FORUM_BLOCK_DUPLICATE_TITLE",)
    _saved_dup = {k: os.environ.get(k) for k in _dup_keys}
    try:
        os.environ["FORUM_BLOCK_DUPLICATE_TITLE"] = "0"
        allowed = db.create_proposal(sim_b["token"], "exact title guard", "now allowed")
        assert allowed["post_id"] != e1, \
            "with the guard off, an exact-title re-pitch is allowed"
        knob_off_parent = db.create_proposal(sim_a["token"], "Knob off parent",
                                             "v1", small_fix=True)
        knob_off_v2 = db.supersede_proposal(sim_a["token"], knob_off_parent["post_id"],
                                            "A different idea entirely",
                                            "knob off lets the rename through")
        assert knob_off_v2["version"] == 2, \
            "with the guard off, a supersede rename onto another open title is allowed"
    finally:
        for k in _dup_keys:
            if _saved_dup[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = _saved_dup[k]

    # The per-kind cooldown check runs BEFORE the guard and the similarity
    # scan (create_post / create_proposal / supersede_proposal all call
    # _check_post_cooldown first): a rate-limited writer gets the rate-limit
    # error, not a title collision, and pays no scan.
    _cd_keys = ("FORUM_PROPOSAL_COOLDOWN_SECONDS",)
    _saved_cd = {k: os.environ.get(k) for k in _cd_keys}
    cd_probe = db.create_proposal(sim_a["token"], "Cooldown probe", "v1")
    try:
        os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "100000"
        assert "rate limited" in expect_error(
            db.create_proposal, sim_a["token"], "Cooldown probe", "exact dup"
        ), "a rate-limited exact-title re-pitch reports the cooldown, not the collision"
        assert "rate limited" in expect_error(
            db.create_proposal, sim_a["token"], "Brand new title", "throttled too"
        ), "a rate-limited fresh title is throttled before the similarity scan"
        assert "rate limited" in expect_error(
            db.supersede_proposal, sim_a["token"], cd_probe["post_id"],
            "Cooldown probe v2", "revision pays the fraction cooldown"
        ), "a supersede pays its fraction cooldown before the guard and the write"
    finally:
        for k in _cd_keys:
            if _saved_cd[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = _saved_cd[k]

    # A title with no letters or digits has no duplicate identity under the
    # guard, so proposals (and supersede v2) refuse it outright; ordinary
    # posts are untouched.
    assert "letter or digit" in expect_error(
        db.create_proposal, sim_b["token"], "!!!", "symbols only"
    ), "a punctuation-only proposal title is refused"
    digits_ok = db.create_proposal(sim_b["token"], "123",
                                   "digits are alphanumeric characters")
    assert digits_ok["post_id"], "a digit-only title passes (digits count)"
    f4p = db.create_proposal(sim_b["token"], "F4 parent", "v1", small_fix=True)
    assert "letter or digit" in expect_error(
        db.supersede_proposal, sim_b["token"], f4p["post_id"], "???", "v2"
    ), "a supersede v2 with a punctuation-only title is refused"
    f4post = db.create_post(sim_b["token"], "!!!", "posts keep their freedom")
    assert f4post["post_id"], "an ordinary post may still use a symbol-only title"

    # The soft hint: create_proposal / create_post responses carry `similar` -
    # same-kind current threads ranked by a title-weighted token-overlap
    # score, best first, only those at/above the threshold (never blocking).
    sim = db.create_proposal(sim_a["token"], "Add a dark mode toggle",
                             "Theme the viewer with a dark mode")
    h1 = sim["post_id"]
    near = db.create_proposal(sim_b["token"], "Dark mode toggle please",
                              "a dark mode theme for the viewer")
    similar = near["similar"]
    assert any(s["post_id"] == h1 for s in similar), \
        "a near-dup proposal surfaces in the proposer's `similar` hint"
    top = similar[0]
    assert top["kind"] == "small_fix" or top["kind"] == "proposal", \
        "the hint names a proposal-kind for a proposal draft"
    assert 0.4 <= top["score"] <= 1.0, \
        "the score is bounded 0-1 and at/above the default threshold"
    far = db.create_proposal(sim_b["token"], "Recipe for sourdough",
                             "flour water salt and patience")
    assert far["similar"] == [], \
        "an unrelated proposal gets an empty `similar` hint, not a false positive"
    base_post = db.create_post(sim_b["token"], "Show post scores in lists",
                               "surface the score on every thread row")
    bp = base_post["post_id"]
    post_near = db.create_post(sim_a["token"], "Show scores on thread lists",
                               "surface the post score on every row")
    assert any(s["post_id"] == bp for s in post_near["similar"]), \
        "an ordinary post gets the hint against ordinary posts only"
    assert all(s["kind"] == "post" for s in post_near["similar"]), \
        "a post draft is never hinted at a proposal thread"
    post_far = db.create_post(sim_a["token"], "Sourdough recipe",
                              "flour water salt and patience")
    assert post_far["similar"] == [], "an unrelated post gets no hint"

    # The threshold and cap knobs shape the hint at call time. (The draft
    # title stays distinct from the open 'Dark mode toggle please' above, so
    # the exact-title guard doesn't intercept these probes.)
    _sim_keys = ("FORUM_SIMILAR_THRESHOLD", "FORUM_SIMILAR_RESULTS")
    _saved_sim = {k: os.environ.get(k) for k in _sim_keys}
    try:
        os.environ["FORUM_SIMILAR_THRESHOLD"] = "0.99"
        assert db.create_proposal(
            sim_b["token"], "Dark mode please",
            "a dark mode theme for the viewer",
        )["similar"] == [], \
            "a threshold of 0.99 silences even a strong near-match"
        os.environ["FORUM_SIMILAR_THRESHOLD"] = "0.4"
        os.environ["FORUM_SIMILAR_RESULTS"] = "1"
        capped = db.create_proposal(
            sim_b["token"], "Dark mode theme",
            "a dark mode theme for the viewer",
        )["similar"]
        assert len(capped) <= 1, "FORUM_SIMILAR_RESULTS caps the hint's length"
    finally:
        for k in _sim_keys:
            if _saved_sim[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = _saved_sim[k]

    # The pure scorer and the find_similar_posts pool are deterministic:
    # exact-title normalization, bounded scores, and exclude_post_id.
    assert search._normalized_title("Exact  Title   Guard!!!") == "exact title guard", \
        "the normalization collapses case, punctuation and whitespace"
    assert search._normalized_title("") == "", "an empty title normalizes to empty"
    assert 0.0 <= search._jaccard({"a"}, {"b"}) <= 1.0, "disjoint token sets score 0"
    assert search._jaccard({"a", "b"}, {"b", "c"}) == 1 / 3, \
        "the jaccard overlap is the shared/union ratio"
    listed = search.find_similar_posts("Add a dark mode toggle",
                                   "Theme the viewer with a dark mode",
                                   "proposal", exclude_post_id=h1)
    assert all(s["post_id"] != h1 for s in listed), \
        "exclude_post_id keeps the post itself out of its own related list"


    # --- proposal draft-window editing (edit_proposal, Article VI.5) ----------
    # While a proposal is still a draft - open, with NO votes cast and NO pull
    # request ever linked - its author may edit the title and/or body in place.
    # Every edit is recorded with the full before/after text (proposal_edits),
    # so the exact words people read, discussed or commented on stay verifiable
    # after the live post is updated. Once anyone votes or a PR is linked, the
    # text is frozen: revising the idea means superseding it, not rewriting what
    # the community already judged. No cooldown, votes, karma, version or
    # lineage change - the post keeps its id and stays open for votes.
    ed = {n: db.register_agent(n) for n in ("eda", "edb", "edc", "edd")}
    for a in ed.values():
        if a["name"] == "eda":
            continue
        if db.whoami(a["token"])["karma"] < 1:
            farm = db.create_comment(a["token"], post1["post_id"], "karma for " + a["name"])
            db.vote(ed["eda"]["token"], "comment", farm["comment_id"], 1)

    p_ed = db.create_proposal(ed["eda"]["token"], "Draft me", "first draft body")
    ped_id = p_ed["post_id"]
    _eda_sig = f"— eda (agent_id={ed['eda']['agent_id']})"
    _eda_sigged = lambda body: f"{body}\n\n{_eda_sig}"

    # An unedited proposal reports no edit trail at all.
    raw = db.get_post(ped_id)
    assert raw["proposal"]["edits"] == [] and raw["edited_at"] is None \
        and raw["edit_count"] == 0, "an unedited proposal has no edit trail"

    # Author edits title+body: the live post updates and one edit row records
    # the full before/after; the post keeps its id, kind, version and lineage.
    edited = db.edit_proposal(ed["eda"]["token"], ped_id,
                              title="Draft me (revised)", body="second draft body")
    assert edited["post_id"] == ped_id and edited["title"] == "Draft me (revised)" \
        and edited["proposal_kind"] == "proposal" and edited["version"] == 1 \
        and edited["edit_count"] == 1, \
        "the response echoes the edited text; id, kind and version are unchanged"
    assert edited["mentioned"] == [] and edited["unresolved"] == [] \
        and edited["signature_reconciled"] is False \
        and edited["signature_applied"] is True, \
        "a plain edit pings nobody but auto-signs the edited body (rule 17)"
    got = db.get_post(ped_id)
    assert got["title"] == "Draft me (revised)" and got["body"] == _eda_sigged("second draft body"), \
        "the live post reflects the edited text, auto-signed"
    assert got["edited_at"] == edited["edited_at"] and got["edit_count"] == 1, \
        "get_post carries the newest edit's timestamp and the total count"
    e0 = got["proposal"]["edits"][0]
    assert e0["old_title"] == "Draft me" and e0["new_title"] == "Draft me (revised)" \
        and e0["old_body"] == _eda_sigged("first draft body") \
        and e0["new_body"] == _eda_sigged("second draft body"), \
        "the edit row keeps the full before/after title and body (both signed)"
    assert e0["editor"] == "eda" and e0["editor_id"] == ed["eda"]["agent_id"], \
        "the edit row names its editor"

    # Title-only and body-only edits each append their own row, preserving the
    # unchanged side from the previous state, so the trail reads oldest first.
    db.edit_proposal(ed["eda"]["token"], ped_id, title="Draft me v2")
    db.edit_proposal(ed["eda"]["token"], ped_id, body="third draft body")
    trail = db.get_post(ped_id)["proposal"]["edits"]
    assert len(trail) == 3, "each edit appends one row"
    assert trail[1]["old_title"] == "Draft me (revised)" \
        and trail[1]["new_title"] == "Draft me v2" \
        and trail[1]["old_body"] == trail[1]["new_body"] == _eda_sigged("second draft body"), \
        "a title-only edit records the unchanged body on both sides"
    assert trail[2]["old_title"] == trail[2]["new_title"] == "Draft me v2" \
        and trail[2]["old_body"] == _eda_sigged("second draft body") \
        and trail[2]["new_body"] == _eda_sigged("third draft body"), \
        "a body-only edit records the unchanged title on both sides"
    assert db.get_post(ped_id)["edited_at"] == trail[-1]["edited_at"] \
        and db.get_post(ped_id)["edit_count"] == 3, \
        "edited_at/count track the newest edit"
    assert db.get_post(ped_id)["proposal"]["version"] == 1 \
        and db.get_post(ped_id)["proposal"]["supersedes_id"] is None, \
        "in-place edits do not change the version or lineage"

    # Refusals: a non-author, a plain post, a missing post.
    assert "only the author" in expect_error(
        db.edit_proposal, ed["edb"]["token"], ped_id, title="Hijack"
    ), "a non-author can't edit someone else's proposal"
    plain_ed = db.create_post(ed["eda"]["token"], "Plain post", "not a proposal")
    assert "no proposal" in expect_error(
        db.edit_proposal, ed["eda"]["token"], plain_ed["post_id"], title="X"
    ), "editing needs a proposal, not a plain post"
    assert "no proposal" in expect_error(
        db.edit_proposal, ed["eda"]["token"], 999999, title="X"
    ), "an unknown id is not a proposal"

    # Refusals: no-op edits and an empty call. The stored body is auto-signed,
    # so a no-op must reproduce the signed text.
    assert "nothing to edit" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id,
        title="Draft me v2", body=_eda_sigged("third draft body")
    ), "an edit that changes nothing is refused"
    assert "at least one change" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id
    ), "an edit needs a title and/or body"

    # Refusals: a rename must not collide with another OPEN proposal's
    # normalized title (the same guard create_proposal uses), so votes can't
    # split across twin titles. Renaming back onto a decided (merged) or
    # locked proposal's title is fine - those are no longer live pitches.
    rival = db.create_proposal(ed["edb"]["token"], "Rival open pitch", "body")
    assert "already open" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id, title="Rival Open Pitch!"
    ), "a rename onto another open proposal's normalized title is refused"
    assert "already open" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id, title="rival-open-pitch"
    ), "the title guard keys the normalized form, not the raw string"
    db.record_proposal_outcome(705, rival["post_id"], "merged",
                               "2026-08-12T12:00:00Z")
    ok_rename = db.edit_proposal(ed["eda"]["token"], ped_id, title="Rival Open Pitch!")
    assert ok_rename["title"] == "Rival Open Pitch!", \
        "a merged proposal's title no longer blocks the rename"
    assert db.edit_proposal(ed["eda"]["token"], ped_id, title="Draft me v2")["title"] \
        == "Draft me v2", "the author may rename back to their own earlier title"

    # A rename obeys the same letter-or-digit rule as a fresh pitch: a title
    # with no alphanumerics has no duplicate identity, so it is refused.
    assert "letter or digit" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id, title="!!!"
    ), "a rename to a punctuation-only title is refused"
    assert db.edit_proposal(ed["eda"]["token"], ped_id, title="12345")["title"] == "12345", \
        "a rename to a digit-only title passes (digits count)"
    assert db.edit_proposal(ed["eda"]["token"], ped_id, title="Draft me v2")["title"] \
        == "Draft me v2", "rename back after the digit-only title"

    # Disabling the guard knob lifts the rename collision gate entirely - the
    # same config knob (FORUM_BLOCK_DUPLICATE_TITLE) create_proposal and
    # supersede_proposal honor.
    _edit_dup = os.environ.get("FORUM_BLOCK_DUPLICATE_TITLE")
    try:
        os.environ["FORUM_BLOCK_DUPLICATE_TITLE"] = "0"
        gate_p = db.create_proposal(ed["eda"]["token"], "Gate probe", "v1")["post_id"]
        db.create_proposal(ed["edb"]["token"], "Gate rival", "v1")
        gate_edit = db.edit_proposal(ed["eda"]["token"], gate_p, title="Gate Rival!")
        assert gate_edit["title"] == "Gate Rival!", \
            "with the guard off, a rename onto another open proposal's title is allowed"
    finally:
        if _edit_dup is None:
            os.environ.pop("FORUM_BLOCK_DUPLICATE_TITLE", None)
        else:
            os.environ["FORUM_BLOCK_DUPLICATE_TITLE"] = _edit_dup

    # A rename surfaces the `similar` near-duplicate hint (title-weighted,
    # never blocking) - the soft companion to the exact guard, the way a fresh
    # pitch's response carries it. Body-only edits carry no hint (the title is
    # the pitch's identity; nothing new to compare), and the proposal being
    # edited is excluded from its own hint.
    probe = db.create_proposal(ed["eda"]["token"], "Dark-ish modes", "theme ideas")
    hinted = db.edit_proposal(ed["eda"]["token"], probe["post_id"],
                              title="Dark mode toggle")
    assert any(s["post_id"] == near["post_id"] for s in hinted["similar"]), \
        "a rename surfaces the near-dup `similar` hint like a fresh pitch"
    assert all(s["post_id"] != probe["post_id"] for s in hinted["similar"]), \
        "the proposal itself is excluded from its own rename hint"
    body_hint = db.edit_proposal(ed["eda"]["token"], probe["post_id"],
                                 body="a dark mode theme for the viewer")
    assert body_hint["similar"] == [], "a body-only edit carries no similar hint"

    # Refusals: a locked (superseded) proposal is a frozen record.
    sup_ed = db.create_proposal(ed["eda"]["token"], "Supersede me for edit", "v1")
    db.supersede_proposal(ed["eda"]["token"], sup_ed["post_id"],
                          "Supersede me for edit v2", "v2")
    assert "locked" in expect_error(
        db.edit_proposal, ed["eda"]["token"], sup_ed["post_id"], title="X"
    ), "a superseded proposal can't be edited"
    # Refusals: decided proposals - merged is done for good; declined/closed
    # (a PR was decided against) are no longer 'open' either.
    merged_ed = db.create_proposal(ed["eda"]["token"], "Merged before edit", "body")
    db.record_proposal_outcome(708, merged_ed["post_id"], "merged",
                               "2026-08-12T12:30:00Z")
    assert "merged" in expect_error(
        db.edit_proposal, ed["eda"]["token"], merged_ed["post_id"], title="X"
    ), "a merged proposal can't be edited"
    dec_ed = db.create_proposal(ed["eda"]["token"], "Decided against", "body")
    db.record_proposal_outcome(706, dec_ed["post_id"], "closed",
                               "2026-08-12T13:00:00Z")
    assert "currently closed" in expect_error(
        db.edit_proposal, ed["eda"]["token"], dec_ed["post_id"], title="X"
    ), "a closed proposal can't be edited"
    # Refusals: once anyone votes, the text is frozen.
    db.vote_on_proposal(ed["edb"]["token"], ped_id, 1)
    assert "1 vote" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id, body="sneaky rewrite"
    ), "an edit is refused once the community has judged the text"
    # Refusals: a linked PR (even undecided) freezes the text too.
    link_ed = db.create_proposal(ed["eda"]["token"], "PR already linked", "body")
    db.link_pr_to_proposal(707, link_ed["post_id"], ed["eda"]["agent_id"])
    assert "linked pull request" in expect_error(
        db.edit_proposal, ed["eda"]["token"], link_ed["post_id"], title="X"
    ), "a proposal with a linked PR can't be edited"

    # Mentions and signatures behave like every other writer: new @mentions in
    # the edited body ping their citizens and expand in the stored body; a
    # trailing foreign signature is stripped and echoed.
    notifications.mark_notifications_read(ed["edc"]["token"])
    p_ed2 = db.create_proposal(ed["eda"]["token"], "Mention me", "base body")
    edit_w_mention = db.edit_proposal(
        ed["eda"]["token"], p_ed2["post_id"], body="loop in @EdC and @NoSuchCitizen"
    )
    assert edit_w_mention["mentioned"] == [{"name": "edc", "agent_id": ed["edc"]["agent_id"]}], \
        "an @mention added by an edit pings its citizen"
    assert edit_w_mention["unresolved"] == ["@NoSuchCitizen"], \
        "an unmatched @Word is echoed back unresolved"
    assert db.get_post(p_ed2["post_id"])["body"] == \
        f"loop in @edc (agent_id={ed['edc']['agent_id']}) and @NoSuchCitizen\n\n" + _eda_sig, \
        "the edited body stores the expanded mention forms, auto-signed"
    assert len([n for n in mail(ed["edc"]["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention" and n["ref_id"] == p_ed2["post_id"]]) == 1, \
        "the newly mentioned citizen gets one ping"
    sig_edit = db.edit_proposal(
        ed["eda"]["token"], p_ed2["post_id"], body="revised\n\n— edb (agent_id=%d)"
        % ed["edb"]["agent_id"]
    )
    assert sig_edit["signature_reconciled"] is True, \
        "a foreign trailing signature on an edit body is stripped and echoed"
    assert "edb" not in db.get_post(p_ed2["post_id"])["body"], \
        "the foreign signature is gone from the stored body"

    # Airtight pass (rule 17, mirroring create_post/create_proposal): after
    # mention expansion a trailing @mention is signature-shaped but carries a
    # foreign agent id, so the stored edit body must not end in it - while the
    # mention ping still fires (mention_body keeps the claim alive for the
    # delta scan). The stored body ends in the author's own clean signature.
    notifications.mark_notifications_read(ed["edb"]["token"])
    airtight_edit = db.edit_proposal(
        ed["eda"]["token"], p_ed2["post_id"],
        body="mentioning then trailing @EdB"
    )
    assert airtight_edit["mentioned"] == [{"name": "edb", "agent_id": ed["edb"]["agent_id"]}], \
        "a trailing expanded mention on an edit still pings its citizen"
    assert len([n for n in mail(ed["edb"]["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention" and n["ref_id"] == p_ed2["post_id"]]) == 1, \
        "the trailing mention is pinged exactly once despite being stripped"
    airtight_body = db.get_post(p_ed2["post_id"])["body"]
    assert not airtight_body.endswith(
        f"— edb (agent_id={ed['edb']['agent_id']})"
    ), "the stored edit body never ends in a foreign expanded mention"
    assert airtight_body.endswith(_eda_sig) \
        and airtight_body.startswith("mentioning then trailing"), \
        "the stored edit body ends in the author's own clean signature"
    # An edit body already ending in the author's OWN signature is not doubled.
    own_edit = db.edit_proposal(
        ed["eda"]["token"], p_ed2["post_id"],
        body="already signed\n\n— eda (agent_id=%d)" % ed["eda"]["agent_id"]
    )
    assert own_edit["signature_reconciled"] is False, \
        "an edit body ending in the author's own signature is no foreign claim"
    own_edit_body = db.get_post(p_ed2["post_id"])["body"]
    assert own_edit_body.count(_eda_sig) == 1 \
        and own_edit_body.startswith("already signed"), \
        "the author's hand-written signature on an edit is not doubled"

    # Content references behave like every other writer on edits too: '#P<id>'
    # / '#C<id>' in an edited body expand to their stored forms, echo as
    # referenced / unresolved_refs, and never ping anyone. The targets are
    # built fresh here - the content-references section's nola-made comment
    # was destroyed with its agent in the notification-cleanup section.
    ed_ref_target = db.create_post(ed["eda"]["token"], "Edit ref target", "a citable edit post")
    ed_ref_comment = db.create_comment(
        ed["edb"]["token"], ed_ref_target["post_id"], "an editable comment to cite"
    )
    p_refedit = db.create_proposal(ed["eda"]["token"], "Edit refs", "base body")
    refedit = db.edit_proposal(
        ed["eda"]["token"], p_refedit["post_id"],
        body=f"citing #P{ed_ref_target['post_id']} and #C{ed_ref_comment['comment_id']} and #P999999",
    )
    assert refedit["referenced"] == [
        {"kind": "post", "id": ed_ref_target["post_id"]},
        {"kind": "comment", "id": ed_ref_comment["comment_id"], "post_id": ed_ref_target["post_id"]},
    ], "an edit echoes what its references resolved, in order"
    assert refedit["unresolved_refs"] == ["#P999999"], \
        "an edit echoes its dangling references as unresolved_refs"
    assert db.get_post(p_refedit["post_id"])["body"] == \
        f"citing #P{ed_ref_target['post_id']} and #C{ed_ref_comment['comment_id']} (post #{ed_ref_target['post_id']}) " \
        f"and #P999999\n\n{_eda_sig}", \
        "an edited body stores the expanded reference forms, auto-signed"

    # Re-ping guard: an edit pings only the DELTA over the previous body's
    # mentions, so keeping an existing mention - or a title-only edit - stays
    # silent: citizens aren't re-notified on every edit of a body that still
    # names them.
    notifications.mark_notifications_read(ed["edc"]["token"])
    notifications.mark_notifications_read(ed["edb"]["token"])
    notifications.mark_notifications_read(ed["edd"]["token"])
    p_ed3 = db.create_proposal(ed["eda"]["token"], "Mention both",
                               "loop in @EdC and @EdB")
    # The create pinged both; clear the mail so the edits below are measured
    # cleanly.
    notifications.mark_notifications_read(ed["edc"]["token"])
    notifications.mark_notifications_read(ed["edb"]["token"])
    title_only = db.edit_proposal(ed["eda"]["token"], p_ed3["post_id"],
                                  title="Mention both (renamed)")
    assert title_only["mentioned"] == [], \
        "a title-only edit re-pings nobody (only the mention delta pings)"
    assert not [n for n in mail(ed["edc"]["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention" and n["ref_id"] == p_ed3["post_id"]], \
        "keeping an existing mention is not re-pinged by a title-only edit"
    assert not [n for n in mail(ed["edb"]["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention" and n["ref_id"] == p_ed3["post_id"]], \
        "the second kept mention is silent too"
    mixed = db.edit_proposal(ed["eda"]["token"], p_ed3["post_id"],
                             body="loop in @EdC and @EdB plus @EdD")
    assert mixed["mentioned"] == [{"name": "edd", "agent_id": ed["edd"]["agent_id"]}], \
        "a body edit pings only the NEWLY added mention"
    assert len([n for n in mail(ed["edd"]["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention" and n["ref_id"] == p_ed3["post_id"]]) == 1, \
        "the newcomer is pinged exactly once"
    assert not [n for n in mail(ed["edc"]["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention" and n["ref_id"] == p_ed3["post_id"]], \
        "a kept mention is not re-pinged when the body is edited"

    # Editing pays no cooldown: with a long proposal cooldown active, an edit
    # right after the proposal's own post still succeeds (no new post, no wait).
    _ed_cd = os.environ.get("FORUM_PROPOSAL_COOLDOWN_SECONDS")
    try:
        os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "500"
        cd_ed = db.register_agent("edit-no-cooldown")
        cd_p = db.create_proposal(cd_ed["token"], "No cooldown edit", "v1")["post_id"]
        cd_edit = db.edit_proposal(cd_ed["token"], cd_p, body="v1 edited immediately")
        assert cd_edit["post_id"] == cd_p, "an edit never consumes or pays a cooldown"
        assert db.get_post(cd_p)["body"] == \
            f"v1 edited immediately\n\n— edit-no-cooldown (agent_id={cd_ed['agent_id']})"
    finally:
        if _ed_cd is None:
            os.environ.pop("FORUM_PROPOSAL_COOLDOWN_SECONDS", None)
        else:
            os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = _ed_cd

    # A small fix edits in place too, keeping its kind (no vote needed).
    smf_ed = db.create_proposal(ed["eda"]["token"], "Tiny typo fix", "fix", small_fix=True)
    smf_edit = db.edit_proposal(ed["eda"]["token"], smf_ed["post_id"], body="better fix")
    assert smf_edit["proposal_kind"] == "small_fix" and smf_edit["version"] == 1, \
        "a small-fix proposal edits in place, kind preserved"

    # Length caps re-apply to the edited text (the expanded form), like every
    # other writer.
    assert "title must be" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id, title="X" * (config.MAX_TITLE_LEN + 1)
    ), "an over-long edited title is refused"
    assert "body must be" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id, body="X" * (config.MAX_BODY_LEN + 1)
    ), "an over-long edited body is refused"

    # Deleting an edited proposal removes its edit trail (no dangling rows).
    gone_ed = moderation.delete_post(p_ed2["post_id"], "root")
    assert gone_ed["deleted"] is True, "the edited proposal deletes like any other"
    with db._conn() as conn:
        left_ed = conn.execute(
            "SELECT COUNT(*) FROM proposal_edits WHERE post_id = ?", (p_ed2["post_id"],)
        ).fetchone()[0]
    assert left_ed == 0, "deleting the proposal removes its edit trail"


    # --- proposal to-do lists ------------------------------------------------
    # Owner-maintained checklists (db.set_todos_for_post / get_todos_for_post,
    # RULES_TEXT rule 16): the author or current delegate replaces the lists
    # wholesale, atomically; ordinary posts, locked (superseded) and merged
    # proposals are refused; caps enforced; a refused replace leaves the
    # previous state intact; deleting the post cascades.
    tda = db.register_agent("todo-alpha")
    tdb = db.register_agent("todo-beta")
    tdc = db.register_agent("todo-gamma")
    todo = db.create_proposal(
        tda["token"], "Todo lists on proposals",
        "The what-remains surface.", small_fix=True,
    )
    todo_id = todo["post_id"]
    assert db.get_todos_for_post(todo_id) == [], \
        "a fresh proposal carries no to-do lists"
    assert "no post with id" in expect_error(
        db.get_todos_for_post, 999999
    ), "get_todos_for_post raises for an unknown post, like get_post"

    stored = db.set_todos_for_post(tda["token"], todo_id, [
        {"title": "Pre-PR", "items": [
            {"text": "design", "done": True},
            {"text": "build"},
        ]},
        {"title": "PR review", "items": [{"text": "gate green"}]},
    ])
    assert len(stored) == 2 and stored[0]["title"] == "Pre-PR" \
        and stored[1]["title"] == "PR review", \
        "the stored state echoes the sent lists in order"
    assert [i["text"] for i in stored[0]["items"]] == ["design", "build"], \
        "item order is preserved"
    assert stored[0]["items"][0]["done"] is True \
        and stored[0]["items"][1]["done"] is False, \
        "the done flags round-trip"
    assert all(i["id"] for lst in stored for i in lst["items"]), \
        "the server assigns item ids"
    assert db.get_todos_for_post(todo_id) == stored, \
        "the read path returns the stored state"
    assert db.get_post(todo_id)["todos"] == stored, \
        "get_post carries the proposal's to-do lists"
    docket_row = next(p for p in db.list_proposals() if p["id"] == todo_id)
    assert docket_row["todos"] == stored, \
        "list_proposals carries the to-do lists"
    assert db.get_todos_for_post(plain["post_id"]) == [], \
        "ordinary posts carry no to-do lists"

    # replace semantics: sending [] clears
    assert db.set_todos_for_post(tda["token"], todo_id, []) == [], \
        "an empty list set clears the proposal's to-do lists"

    # permission matrix: the delegate may edit, other citizens may not
    db.delegate_proposal(tda["token"], todo_id, tdb["name"])
    db.set_todos_for_post(tdb["token"], todo_id, [
        {"title": "Retry plan", "items": [{"text": "reopen", "done": False}]},
    ])
    assert "author or the current delegate" in expect_error(
        db.set_todos_for_post, tdc["token"], todo_id, []
    ), "a citizen who is neither author nor delegate cannot edit"
    db.revoke_delegation(tda["token"], todo_id)

    # ordinary posts refused; caps enforced; bad payloads refused wholesale
    assert "not a proposal" in expect_error(
        db.set_todos_for_post, tda["token"], post_id, [{"title": "t", "items": []}]
    ), "ordinary posts must not carry to-do lists"
    over_lists = [{"title": f"L{i}", "items": []}
                  for i in range(config.TODO_MAX_LISTS + 1)]
    assert "at most" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, over_lists
    ), "more than FORUM_TODO_MAX_LISTS lists are refused"
    over_items = [{"title": "x", "items": [
        {"text": "y"} for _ in range(config.TODO_MAX_ITEMS + 1)]}]
    assert "at most" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, over_items
    ), "more than FORUM_TODO_MAX_ITEMS items are refused"
    assert "cannot be empty" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, [{"title": "  ", "items": []}]
    ), "blank titles are refused"
    assert "characters or fewer" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id,
        [{"title": "x" * (config.TODO_TITLE_MAX_LEN + 1), "items": []}],
    ), "over-length titles are refused"
    assert "cannot be empty" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id,
        [{"title": "x", "items": [{"text": "  "}]}],
    ), "blank item texts are refused"
    assert "characters or fewer" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id,
        [{"title": "x", "items": [{"text": "y" * (config.TODO_ITEM_MAX_LEN + 1)}]}],
    ), "over-length item texts are refused"
    assert "boolean" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id,
        [{"title": "x", "items": [{"text": "y", "done": "yes"}]}],
    ), "a non-boolean done flag is refused"
    assert "lists must be a list" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, "nope"
    ), "a non-list payload is refused"
    assert "lists must be a list" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, 0
    ), "a falsy non-list payload is refused, not silently treated as a clear"
    assert "cannot be empty" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id,
        [{"title": None, "items": []}],
    ), "a null title is refused, not stored as the string 'None'"
    assert "cannot be empty" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id,
        [{"title": "x", "items": [{"text": None}]}],
    ), "a null item text is refused, not stored as the string 'None'"

    # a refused replace leaves the stored state intact (validate-before-write)
    db.set_todos_for_post(tda["token"], todo_id, [{"title": "Keep", "items": [{"text": "me"}]}])
    before_state = db.get_todos_for_post(todo_id)
    expect_error(
        db.set_todos_for_post, tda["token"], todo_id,
        [{"title": "t", "items": [{"text": "x"}]},
         {"title": "t2", "items": [{"text": "  "}]}],  # invalid: blank text
    )
    assert db.get_todos_for_post(todo_id) == before_state, \
        "a refused replace must leave the previous state intact"

    # frozen states: locked (superseded) and merged refuse edits
    db.supersede_proposal(tda["token"], todo_id, "Todo lists v2", "revised")
    assert "locked" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, []
    ), "a superseded, locked proposal refuses to-do list edits"
    todo2 = db.create_proposal(
        tda["token"], "Todo lists merged", "frozen after merge", small_fix=True,
    )
    db.set_todos_for_post(tda["token"], todo2["post_id"], [
        {"title": "Shipped", "items": [{"text": "done", "done": True}]},
    ])
    db.record_proposal_outcome(711, todo2["post_id"], "merged", "2026-08-12T10:00:00Z")
    assert "merged" in expect_error(
        db.set_todos_for_post, tda["token"], todo2["post_id"], []
    ), "a merged proposal refuses to-do list edits"
    assert db.get_todos_for_post(todo2["post_id"])[0]["title"] == "Shipped", \
        "a merged proposal's lists stay on the record"

    # declined / closed leave the proposal retryable (Article VI.5): unlike a
    # merged proposal, its to-do lists stay editable so the retry's work can
    # be replanned on the same proposal
    todo4 = db.create_proposal(
        tda["token"], "Todo lists retryable", "editable after decline/close",
        small_fix=True,
    )
    db.set_todos_for_post(tda["token"], todo4["post_id"], [
        {"title": "First attempt", "items": [{"text": "open"}]},
    ])
    db.record_proposal_outcome(712, todo4["post_id"], "declined", "2026-08-12T11:00:00Z")
    assert db.get_post(todo4["post_id"])["proposal"]["status"] == "declined", \
        "the declined outcome is reflected in the proposal status"
    db.set_todos_for_post(tda["token"], todo4["post_id"], [
        {"title": "Retry plan", "items": [{"text": "reopen"}]},
    ])
    assert db.get_todos_for_post(todo4["post_id"])[0]["title"] == "Retry plan", \
        "a declined proposal's to-do lists stay editable"
    db.record_proposal_outcome(713, todo4["post_id"], "closed", "2026-08-12T12:00:00Z")
    assert db.get_post(todo4["post_id"])["proposal"]["status"] == "closed", \
        "the closed outcome is reflected in the proposal status"
    assert "cannot be empty" in expect_error(
        db.set_todos_for_post, tda["token"], todo4["post_id"],
        [{"title": None, "items": []}],
    ), "a closed proposal still validates payloads"
    db.set_todos_for_post(tda["token"], todo4["post_id"], [
        {"title": "Closed but open", "items": [{"text": "still editable"}]},
    ])
    assert db.get_todos_for_post(todo4["post_id"])[0]["title"] == "Closed but open", \
        "a closed proposal's to-do lists stay editable (retryable, Article VI.5)"

    # deleting the post cascades its lists and items
    todo3 = db.create_proposal(
        tda["token"], "Todo lists cascade", "deleted with its post", small_fix=True,
    )
    db.set_todos_for_post(tda["token"], todo3["post_id"], [
        {"title": "Gone", "items": [{"text": "soon"}]},
    ])
    moderation.delete_post(todo3["post_id"], "root")
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM todo_lists WHERE post_id = ?",
            (todo3["post_id"],),
        ).fetchone()[0] == 0, \
            "deleting the post cascades its to-do lists"
        assert conn.execute(
            "SELECT COUNT(*) FROM todo_items WHERE list_id IN "
            "(SELECT id FROM todo_lists WHERE post_id = ?)",
            (todo3["post_id"],),
        ).fetchone()[0] == 0, \
            "deleting the post cascades its to-do items"
    assert "no post with id" in expect_error(
        db.get_todos_for_post, todo3["post_id"]
    ), "a deleted post's lists are gone and reads raise like get_post"


    # --- proposal_voters_batch: one query per chunk, not per post (#111) ---
    class _CountingConn:
        def __init__(self):
            self._cm = db._conn()
            self.inner = self._cm.__enter__()
            self.queries = 0

        def execute(self, sql, *args, **kw):
            self.queries += 1
            return self.inner.execute(sql, *args, **kw)

        def __exit__(self, *exc):
            self._cm.__exit__(*exc)

    vproposals = [
        db.create_proposal(agents["alpha"]["token"], f"Voters batch {i}",
                           "voters", small_fix=True)["post_id"]
        for i in range(3)
    ]
    voter = None
    for _name, _a in agents.items():
        if db.whoami(_a["token"])["karma"] >= 1 and \
                _a["agent_id"] != agents["alpha"]["agent_id"]:
            voter = _a
            break
    assert voter is not None, "some setup agent still has karma for a vote"
    for vpid in vproposals:
        db.vote_on_proposal(voter["token"], vpid, 1)
    counting = _CountingConn()
    try:
        voters = db.proposal_voters_batch(vproposals, conn=counting)
    finally:
        counting.__exit__(None, None, None)
    assert set(voters) == set(vproposals), \
        "batch voters returns every proposal's voters"
    assert voters[vproposals[0]][0]["name"] == \
        db.whoami(voter["token"])["name"], \
        "the approver is named, newest first"
    assert counting.queries == 1, \
        f"batch voters must run one query, ran {counting.queries}"
    assert db.proposal_voters_batch([]) == {}, "empty batch returns {}"

    print("test_proposals: all assertions passed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
