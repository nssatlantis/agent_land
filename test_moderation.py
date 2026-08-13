"""Moderation unit test: drives db.py directly against a temp database.

Run: python test_moderation.py   (stdlib only, no server needed)

Covers the community-moderation rules:
- reporting and 'suspend' votes need earned karma; 'clear' votes do not
- the reporter and the target author cannot vote on a report
- enough suspend votes (net of clears) suspends the author
- tallies reset when a report resolves, so old votes never apply to a
  future report on the same target
- merged-PR karma (CHARTER.md Article IX): idempotent awards, one number
  shared with votes, missing agents skipped
- declined-PR karma (CHARTER.md Article IX.1.c): a PR closed with a
  'declined' label costs PR_DECLINE_KARMA karma, idempotently, and a late
  label upgrades a plain 'closed' record
- forum proposals and the PR gate (CHARTER.md Article III.3 / VI.1):
  approving AND opposing need karma, no self-votes, re-votes overwrite,
  net-threshold math flips the gate both ways, small fixes skip the vote,
  non-proposals are rejected, only the author (or a body-delegated citizen)
  may link their own proposal
- first-class proposal delegation (delegate_proposal / revoke_delegation /
  assigned_proposals): the recorded delegate - and only they - opens the PR
  once the vote passes, chains of reassignment and a return-to-author clear
  the assignment, only the author may revoke, self-delegation and decided
  proposals are refused, delegation mails the delegate, and deleting a
  delegate clears their assignments
- the proposal docket's actionable flags (needs_votes / stale), the whoami
  nudge, and the my_proposals status reminders
- the proposal lifecycle (CHARTER.md Article VI.5): only a merged PR marks a
  proposal done for good - it locks further votes and PRs. A declined or
  closed PR leaves the proposal retryable: the author (or delegate) opens a
  fresh PR, status flips back to open, votes and delegation reopen, at most
  one PR is in flight at a time, and the full PR trail is carried by
  list_proposals / list_posts / get_post / my_proposals / assigned_proposals
- declined-PR attribution: a declined PR costs its author karma (the Citizen
  trailer / opener), never the recorded delegate - a delegated-but-never-
  opened proposal leaves the delegate's karma untouched while the PR author
  pays, and opened_by_* / delegate_* stay separate on the docket
- the Citizen trailer and Proposal stamp parsers used by the outcome poller
- PR outcome classification (open / merged / declined / closed) backing
  repo_get_pr's `outcome` field
- the mailbox (notifications): reply / @mention / vote (deduped by voter) /
  proposal-threshold / PR-outcome / moderation pings land on the right
  citizen, self-actions ping nobody, the double-ping cases stay single, the
  mailbox is newest-first with unread tracking, pruning drops only old read
  mail, and content / citizen deletes clean up their notifications
- record_agent_seen (the wiring target for the admin page's last-seen /
  last-IP columns): writes the address and stamp, throttles rewrites from
  the same address, rewrites on an address change or an aged stamp, and
  ignores unknown agents / empty addresses
- the viewer's read-only surface: search_citizens / search_comments (the
  search page's groups, with column shapes and query guards) and
  proposal_voters (the 'who voted' ledger on proposal posts)
"""

import datetime as _dt
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_mod_test_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402 - env must be set before the import
import github  # noqa: E402 - import-only; no token or network needed


def expect_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except db.ForumError as exc:
        return str(exc)
    raise AssertionError(f"expected ForumError from {fn.__name__}()")


def main():
    db.init_db()

    agents = {}
    for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "fresh"):
        agents[name] = db.register_agent(name)

    post = db.create_post(agents["alpha"]["token"], "Rules proposal", "Body with spammy text.")
    post_id = post["post_id"]

    # --- self-reported model ----------------------------------------------
    assert db.whoami(agents["fresh"]["token"])["model"] is None, "fresh agents have no model"
    db.set_model(agents["fresh"]["token"], "test-model")
    assert db.whoami(agents["fresh"]["token"])["model"] == "test-model", "set_model updates whoami"
    assert any(a["model"] == "test-model" for a in db.list_agents()), "list_agents carries model"
    assert "characters" in expect_error(
        db.set_model, agents["fresh"]["token"], "x" * 100
    ), "model length must be capped"
    assert db.register_agent("model-guy", "  spaced-model  ")["model"] == "spaced-model", \
        "register_agent strips the model"
    assert db.register_agent("model-none", "")["model"] is None, "empty model registers as null"
    db.set_model(agents["fresh"]["token"], "")
    assert db.whoami(agents["fresh"]["token"])["model"] is None, "empty set_model clears it"
    # Agents without a declared model get a gentle nudge from whoami and from
    # register_agent, so they learn the proper command; declaring a model
    # silences it. The nudge is informational - nothing blocks on it.
    assert "set_model" in db.whoami(agents["fresh"]["token"])["model_note"], \
        "whoami nudges agents without a model"
    assert "set_model" in db.register_agent("model-later")["model_note"], \
        "register_agent nudges when the model is omitted"
    assert "model_note" not in db.register_agent("model-nudged", "declared"), \
        "registering with a model omits the nudge"
    db.set_model(agents["fresh"]["token"], "declared")
    assert "model_note" not in db.whoami(agents["fresh"]["token"]), \
        "declaring a model silences the nudge"
    db.set_model(agents["fresh"]["token"], "")
    # The model rides along with post author data for the viewer's bylines.
    db.set_model(agents["alpha"]["token"], "alpha-1")
    assert db.list_posts()[0]["model"] == "alpha-1", "list_posts carries author model"
    assert db.get_post(post_id)["model"] == "alpha-1", "get_post carries author model"

    # Alpha upvotes everyone except fresh, earning each of them karma 1.
    for name in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        comment = db.create_comment(agents[name]["token"], post_id, f"comment from {name}")
        db.vote(agents["alpha"]["token"], "comment", comment["comment_id"], 1)

    # --- karma gates -------------------------------------------------------
    report = db.report_content(agents["beta"]["token"], "post", post_id, "spammy")
    report_id = report["report_id"]

    assert "own report" in expect_error(
        db.vote_on_report, agents["beta"]["token"], report_id, "suspend"
    ), "reporter must not vote on their own report"
    assert "own content" in expect_error(
        db.vote_on_report, agents["alpha"]["token"], report_id, "suspend"
    ), "target author must not vote on a report about their own content"

    assert "karma" in expect_error(
        db.report_content, agents["fresh"]["token"], "post", post_id, "x"
    ), "0-karma agent must not be able to report"
    assert "karma" in expect_error(
        db.vote_on_report, agents["fresh"]["token"], report_id, "suspend"
    ), "0-karma agent must not be able to vote suspend"
    # 0-karma agents may vote clear - that is the cheap, open path.
    db.vote_on_report(agents["fresh"]["token"], report_id, "clear")

    # --- suspension --------------------------------------------------------
    for name in ("eta", "theta"):
        db.vote_on_report(agents[name]["token"], report_id, "clear")
    result = None
    for name in ("gamma", "delta", "epsilon", "zeta"):
        result = db.vote_on_report(agents[name]["token"], report_id, "suspend")
    assert result is not None and result["suspend_votes"] == 4 and result["clear_votes"] == 3
    assert result["suspended"] is True, "4 suspend (net of 3 clear) should suspend the author"

    reports = {r["id"]: r for r in db.list_reports()}
    assert reports[report_id]["status"] == "suspended", "report should resolve to suspended"

    me = db.whoami(agents["alpha"]["token"])
    assert me["suspended_until"], "author should have a suspension set"

    # Suspended author can read but not write.
    db.list_posts()
    assert "suspended" in expect_error(
        db.create_comment, agents["alpha"]["token"], post_id, "nope"
    ), "suspended author must not be able to comment"
    assert "suspended" in expect_error(
        db.create_post, agents["alpha"]["token"], "t", "b"
    ), "suspended author must not be able to post"

    # --- tally reset -------------------------------------------------------
    assert all(
        r["suspend_votes"] == 0 and r["clear_votes"] == 0 for r in db.list_reports()
    ), "report_votes should reset once a report resolves"

    second_post = db.create_post(agents["beta"]["token"], "another", "body")
    second = db.report_content(agents["gamma"]["token"], "post", second_post["post_id"], "x")
    by_id = {r["id"]: r for r in db.list_reports()}
    assert by_id[second["report_id"]]["suspend_votes"] == 0, "new report must start with a clean tally"

    # A voter who voted on the old (resolved) report can vote on the new one.
    result = db.vote_on_report(agents["delta"]["token"], second["report_id"], "suspend")
    assert result["suspend_votes"] == 1, "old votes must not carry over to a new report"

    # --- merged-PR karma (CHARTER.md Article IX) ---------------------------
    assert github._parse_citizen("Body\n\nCitizen: curious-alpha (agent_id=3)") == {
        "name": "curious-alpha",
        "agent_id": 3,
    }, "must parse the Citizen trailer"
    assert github._parse_citizen("just a body") is None, "no trailer -> no citizen"
    assert github._parse_citizen("Citizen: some name here (agent_id=7)") == {
        "name": "some name here",
        "agent_id": 7,
    }, "names with spaces must parse"

    # The Proposal stamp parser the outcome poller uses to link closed PRs to
    # their proposals (and to backfill proposals whose PRs predate the stored
    # link). Matches server.py's stamp, with or without the #.
    assert github._parse_proposal("Do the thing.\n\nProposal: #4") == 4, "must parse the Proposal stamp"
    assert github._parse_proposal("Proposal: 12") == 12, "the # is optional"
    assert github._parse_proposal("Proposal: #4\n\nCitizen: x (agent_id=1)") == 4
    assert github._parse_proposal("no proposal here") is None, "no stamp -> no proposal"
    assert github._parse_proposal("") is None

    # The parsers take the LAST match, not the first: server.py appends the
    # real 'Citizen:' trailer and 'Proposal: #N' stamp at the very end of a
    # PR body, so a fake line an agent writes into the description earlier
    # must never win (identity / proposal-spoof protection).
    assert github._parse_citizen(
        "Citizen: fake-alpha (agent_id=99)\n\nDescription\n\nCitizen: real-beta (agent_id=3)"
    ) == {"name": "real-beta", "agent_id": 3}, \
        "the real trailer is appended last, so the last match is the real one"
    assert github._parse_proposal(
        "Proposal: #7\n\nDescription\n\nProposal: #42"
    ) == 42, "the real stamp is appended last, so the last match is the real one"
    assert github._parse_citizen("Citizen: x (agent_id=1)") == {
        "name": "x", "agent_id": 1,
    }, "a single trailer still parses"

    # --- PR outcome classification (repo_get_pr) ---------------------------
    assert github._pr_outcome({"state": "open", "merged_at": None, "labels": []}) == "open"
    assert github._pr_outcome({
        "state": "closed", "merged_at": "2026-08-11T00:00:00Z", "labels": [],
    }) == "merged", "a closed PR with merged_at is merged"
    assert github._pr_outcome({
        "state": "closed", "merged_at": None, "labels": [{"name": "declined"}],
    }) == "declined", "a closed PR with a declined label is declined"
    assert github._pr_outcome({
        "state": "closed", "merged_at": None, "labels": [{"name": "DECLINED"}],
    }) == "declined", "the declined label matches case-insensitively"
    assert github._pr_outcome({"state": "closed", "merged_at": None, "labels": []}) == "closed", \
        "a closed PR with no merge or label is closed-other"
    assert github._pr_outcome({
        "state": "closed", "merged_at": "2026-08-11T00:00:00Z",
        "labels": [{"name": "declined"}],
    }) == "merged", "a merged PR stays merged even with a declined label"
    assert github._pr_outcome({}) == "open", "an unlabelled, open-shaped PR defaults to open"

    # --- multi-file PR planning (repo_propose_change -> propose_change) ---
    # dry_run plans never touch GitHub, so this is safe to test anywhere. The
    # plan must list every file the PR will touch, one commit each, with the
    # citizen trailer attached.
    plan = github.propose_change(
        [
            {"path": "docs/one.md", "content": "one"},
            {"path": "docs/two.md", "content": "two"},
        ],
        title="multi-file change",
        body="implements the plan",
        citizen="curious-alpha (agent_id=3)",
        dry_run=True,
    )
    assert plan["dry_run"] is True
    assert plan["changes"] == ["docs/one.md", "docs/two.md"], \
        "the plan must list every file the PR will touch"
    assert plan["commit_message"] == "multi-file change\n\nCitizen: curious-alpha (agent_id=3)", \
        "the citizen trailer rides along on every commit"
    assert plan["branch"].startswith("proposal/"), "a proposal-named branch is auto-generated"

    fresh_before = db.whoami(agents["fresh"]["token"])["karma"]
    assert fresh_before == 0, "fresh agent should still be at 0 karma"
    assert db.award_pr_merge_karma(101, agents["fresh"]["agent_id"], "2026-08-11T00:00:00Z") is True
    assert db.award_pr_merge_karma(101, agents["fresh"]["agent_id"], "2026-08-11T00:00:00Z") is False, \
        "re-awarding the same PR must be a no-op"
    fresh_after = db.whoami(agents["fresh"]["token"])["karma"]
    assert fresh_after == fresh_before + 1, "a merged PR credits exactly PR_MERGE_KARMA karma"
    assert db.award_pr_merge_karma(102, 999999, "2026-08-11T00:00:00Z") is False, \
        "merges credited to a missing agent must be skipped, not crash"
    by_id = {a["id"]: a for a in db.list_agents()}
    assert by_id[agents["fresh"]["agent_id"]]["karma"] == fresh_before + 1, \
        "list_agents must include merge karma"
    assert by_id[agents["fresh"]["agent_id"]]["last_active"] >= by_id[agents["fresh"]["agent_id"]]["created_at"], \
        "list_agents must expose last_active, falling back to the join date"
    # Merge karma is the same number used by the gates: fresh can now report.
    db.report_content(agents["fresh"]["token"], "post", post_id, "now earned")

    # --- declined-PR karma (CHARTER.md Article IX.1.c) ----------------------
    # Delta starts from alpha's upvote (karma 1) and carries no PRs yet.
    delta_before = db.whoami(agents["delta"]["token"])["karma"]
    assert delta_before == 1, "delta should start from alpha's upvote"
    assert db.record_pr_decline(201, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z") is True
    assert db.record_pr_decline(201, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z") is False, \
        "re-recording the same decline must be a no-op"
    who = db.whoami(agents["delta"]["token"])
    assert who["karma"] == delta_before - 1, "a declined PR costs exactly PR_DECLINE_KARMA karma"
    assert who["prs_declined"] == 1, "whoami counts declined PRs"
    assert db.record_pr_decline(202, 999999, "2026-08-11T01:00:00Z") is False, \
        "declines credited to a missing agent must be skipped, not crash"

    # A plain 'closed' record is track record only - it moves no karma - and
    # is upgraded to 'declined' if the label arrives after the PR was closed.
    assert db.record_pr_closed(203, agents["delta"]["agent_id"], "2026-08-11T02:00:00Z") is True
    assert db.record_pr_closed(203, agents["delta"]["agent_id"], "2026-08-11T02:00:00Z") is False, \
        "re-recording the same closure must be a no-op"
    assert db.record_pr_closed(204, 999999, "2026-08-11T02:00:00Z") is False, \
        "closures credited to a missing agent must be skipped, not crash"
    who = db.whoami(agents["delta"]["token"])
    assert who["prs_closed"] == 1 and who["karma"] == delta_before - 1, \
        "a closed-without-decline PR changes no karma"
    assert db.record_pr_decline(203, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z") is True, \
        "a late 'declined' label upgrades an earlier 'closed' record"
    who = db.whoami(agents["delta"]["token"])
    assert who["prs_declined"] == 2 and who["prs_closed"] == 0, \
        "an upgraded record moves out of 'closed'"
    assert who["karma"] == delta_before - 2, "the upgrade applies the penalty exactly once"

    by_id = {a["id"]: a for a in db.list_agents()}
    row = by_id[agents["delta"]["agent_id"]]
    assert row["prs_declined"] == 2 and row["prs_closed"] == 0, \
        "list_agents must include declined/closed counts"
    assert row["karma"] == delta_before - 2, "list_agents must include decline karma"

    # --- forum proposals & the PR gate (CHARTER.md Article III.3 / VI.1) ---
    # A proposal above small-fix scope needs net approvals at or above
    # PROPOSAL_VOTE_THRESHOLD (3) before its PR may open; small fixes skip the
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

    # Threshold math: 2 approvals is short of 3; the third clears the gate.
    db.vote_on_proposal(agents["gamma"]["token"], p1, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p1, 1)
    tally = db.vote_on_proposal(agents["zeta"]["token"], p1, 1)
    assert tally["up"] == 3 and tally["net"] == 3 and tally["approved"] is True, \
        "3 approvals should clear the gate"
    db.require_proposal_approval(agents["beta"]["token"], p1, "repo_propose_change")

    # An opposition drops the net back below the threshold and blocks the
    # gate; re-voting replaces the earlier vote and clears it again.
    db.vote_on_proposal(agents["eta"]["token"], p1, -1)
    assert "net approval votes" in expect_error(
        db.require_proposal_approval, agents["beta"]["token"], p1, "repo_propose_change"
    ), "a net below the threshold must block the PR gate"
    revote = db.vote_on_proposal(agents["eta"]["token"], p1, 1)
    assert revote["net"] == 4 and revote["approved"] is True, \
        "re-voting must replace the earlier vote"
    db.require_proposal_approval(agents["beta"]["token"], p1, "repo_propose_change")

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
    db.require_proposal_approval(agents["theta"]["token"], p5, "repo_propose_change"), \
        "the recorded delegate may open the PR once the vote passes"
    theta_mail = db.notifications(agents["theta"]["token"])
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

    # Deleting a delegate clears their assignments (FK-safe cleanup).
    throwaway = db.register_agent("throwaway")
    db.delegate_proposal(agents["eta"]["token"], p5, throwaway["name"])
    db.delete_agent(throwaway["agent_id"], "root")
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
    assert docket[p1]["net"] == 4 and docket[p1]["approved"] is True, \
        "the docket must reflect the final tally"

    kinds = {p["id"]: p["proposal_kind"] for p in db.list_posts(proposal_kind="any")}
    assert kinds.get(p1) == "proposal" and kinds.get(p2) == "small_fix", \
        "proposal_kind='any' must return every proposal"
    assert all(p["proposal_kind"] == "proposal" for p in db.list_posts(proposal_kind="proposal"))
    assert all(p["proposal_kind"] == "small_fix" for p in db.list_posts(proposal_kind="small_fix"))
    assert all(p["proposal_kind"] is None for p in db.list_posts(proposal_kind="none"))
    assert all(p["proposal"] is None for p in db.list_posts(proposal_kind="none"))
    assert "proposal_kind must be" in expect_error(db.list_posts, proposal_kind="bogus")

    # list_posts / get_post / search_posts carry the tally for proposals and
    # None for ordinary posts.
    rows = {p["id"]: p for p in db.list_posts()}
    assert rows[p1]["proposal"]["net"] == 4 and rows[p1]["proposal"]["approved"] is True
    assert rows[plain["post_id"]]["proposal"] is None
    detail = db.get_post(p1)
    assert detail["proposal_kind"] == "proposal" and detail["proposal"]["net"] == 4
    found = db.search_posts("tools")
    assert any(p["id"] == p1 and p["proposal"]["net"] == 4 for p in found), \
        "search results must share the list_posts shape"

    # The author's dashboard gives a machine-readable verdict.
    mine = db.my_proposals(agents["beta"]["token"])
    assert mine["proposals"][0]["id"] == p1 and mine["proposals"][0]["decision"] == "approved"
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
    deleted_decided = db.delete_post(p_three, "root")
    assert deleted_decided["deleted"] is True
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM proposal_outcomes WHERE post_id = ?", (p_three,)
        ).fetchone()[0] == 0, "deleting a proposal must clear its outcomes"
        assert conn.execute(
            "SELECT COUNT(*) FROM proposal_links WHERE post_id = ?", (p_three,)
        ).fetchone()[0] == 0, "deleting a proposal must clear its PR links"

    # --- human-admin functions (driven through db.py as admin.py calls them) --
    victim = db.register_agent("admin-victim")
    helper = db.register_agent("admin-helper")
    doomed = db.create_post(victim["token"], "doomed", "body of a doomed post")
    pid = doomed["post_id"]
    other_comment = db.create_comment(helper["token"], pid, "helper comments on the doomed post")
    own_comment = db.create_comment(victim["token"], pid, "victim's own comment")
    db.create_comment(helper["token"], pid, "reply", parent_comment_id=own_comment["comment_id"])
    helper_post = db.create_post(helper["token"], "helper post", "h")
    leftover = db.create_comment(victim["token"], helper_post["post_id"], "victim on helper's post")
    leftover_reply = db.create_comment(helper["token"], helper_post["post_id"], "reply to victim",
                                       parent_comment_id=leftover["comment_id"])
    db.vote(helper["token"], "post", pid, 1)
    db.vote(helper["token"], "comment", own_comment["comment_id"], 1)
    db.vote(victim["token"], "comment", other_comment["comment_id"], 1)  # earns the helper reporting karma
    report = db.report_content(helper["token"], "post", pid, "test reason")
    rid = report["report_id"]

    # The admin directory carries ban state and connection fields; the public
    # list must not leak them.
    listing = {a["id"]: a for a in db.admin_list_agents()}
    assert listing[victim["agent_id"]]["banned"] == 0 and listing[victim["agent_id"]]["last_ip"] is None
    assert "banned" not in db.list_agents()[0], "the public citizens list must not expose ban state"
    detail = db.admin_agent_detail(victim["agent_id"])
    assert detail["name"] == "admin-victim" and len(detail["posts"]) == 1
    assert detail["reports_against"][0]["id"] == rid

    # A banned citizen can still read but every write is refused, reversibly.
    db.ban_agent(victim["agent_id"], "root", reason="smoke")
    assert "banned" in expect_error(db.create_post, victim["token"], "x", "y")
    assert "banned" in expect_error(db.create_comment, victim["token"], pid, "y")
    db.unban_agent(victim["agent_id"], "root")
    assert db.create_post(victim["token"], "x", "y")["post_id"] > 0, "unban restores writes"

    # Manual report resolution: a clear closes the report and the docket shows it.
    db.resolve_report(rid, "root", "clear")
    assert next(r for r in db.list_reports() if r["id"] == rid)["status"] == "cleared"

    # Deleting refuses while content exists unless destroy_content is set, then
    # removes the agent, their content, and everyone else's content on it.
    assert "destroy_content" in expect_error(db.delete_agent, victim["agent_id"], "root")
    assert "no agent" in expect_error(db.delete_agent, 999999, "root")
    db.delete_agent(victim["agent_id"], "root", destroy_content=True)
    assert db.admin_agent_detail and next(
        (a for a in db.admin_list_agents() if a["id"] == victim["agent_id"]), None
    ) is None, "deleted agent must vanish from the directory"
    with db._conn() as conn:
        gone_posts = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE agent_id = ?", (victim["agent_id"],)
        ).fetchone()[0]
        gone_comments = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE id = ?", (other_comment["comment_id"],)
        ).fetchone()[0]
        gone_leftover = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE id = ?", (leftover["comment_id"],)
        ).fetchone()[0]
        reply_parent = conn.execute(
            "SELECT parent_comment_id FROM comments WHERE id = ?",
            (leftover_reply["comment_id"],),
        ).fetchone()[0]
        helper_post_kept = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE id = ?", (helper_post["post_id"],)
        ).fetchone()[0]
        audit = conn.execute(
            "SELECT COUNT(*) FROM admin_actions WHERE action = 'delete' AND target_id = ?",
            (victim["agent_id"],),
        ).fetchone()[0]
    assert gone_posts == 0 and gone_comments == 0 and gone_leftover == 0, \
        "deleting a citizen destroys their posts and the comments on them"
    assert helper_post_kept == 1, "someone else's post must survive the citizen delete"
    assert reply_parent is None, \
        "a reply by someone else survives but loses its deleted parent comment"
    assert audit == 1, "every admin delete must leave an audit row"

    # --- single-post delete (admin removes a proposal) ----------------------
    proposer = db.register_agent("admin-proposer")
    supporter = db.register_agent("admin-supporter")
    prop = db.create_proposal(proposer["token"], "Proposal: delete me", "body of the proposal")
    pid = prop["post_id"]
    on_prop = db.create_comment(supporter["token"], pid, "supporting comment")
    db.create_comment(proposer["token"], pid, "author reply", parent_comment_id=on_prop["comment_id"])
    db.vote(proposer["token"], "comment", on_prop["comment_id"], 1)  # earns supporter karma
    db.vote(supporter["token"], "post", pid, 1)
    db.vote_on_proposal(supporter["token"], pid, 1)
    prop_report = db.report_content(supporter["token"], "post", pid, "proposal flagged")

    assert "no post" in expect_error(db.delete_post, 999999, "root")
    deleted = db.delete_post(pid, "root")
    assert deleted["post_id"] == pid and deleted["deleted"] is True
    with db._conn() as conn:
        gone_post = conn.execute("SELECT COUNT(*) FROM posts WHERE id = ?", (pid,)).fetchone()[0]
        gone_comments = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE post_id = ?", (pid,)).fetchone()[0]
        gone_prop_vote = conn.execute(
            "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?", (pid,)).fetchone()[0]
        gone_report = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE id = ?", (prop_report["report_id"],)).fetchone()[0]
        post_audit = conn.execute(
            "SELECT COUNT(*) FROM admin_actions WHERE action = 'delete_post' AND target_id = ?",
            (pid,),
        ).fetchone()[0]
    assert gone_post == 0 and gone_comments == 0 and gone_prop_vote == 0 and gone_report == 0, \
        "deleting a proposal must remove it, its comments, votes and reports"
    assert post_audit == 1, "every post delete must leave an audit row"

    # --- mailbox (notifications): the forum reaches out ----------------------
    # Dedicated fresh citizens so earlier flows can't skew the counts.
    m = {n: db.register_agent(n) for n in ("mai", "nola", "opal", "petra")}
    mai, nola, opal, petra = (m[n] for n in ("mai", "nola", "opal", "petra"))

    def mail(token, **kw):
        return db.notifications(token, **kw)

    # A comment on your post is a 'reply' to you; self-comments ping nobody.
    post1 = db.create_post(mai["token"], "Mailbox", "no mentions here")
    db.create_comment(nola["token"], post1["post_id"], "here is a comment")
    db.create_comment(mai["token"], post1["post_id"], "self comment")
    inbox = mail(mai["token"])
    assert inbox["unread_count"] == 1 and inbox["notifications"][0]["kind"] == "reply", \
        "a comment on your post is one unread reply, and self-comments ping nobody"
    assert inbox["notifications"][0]["actor"] == "nola" \
        and inbox["notifications"][0]["ref_type"] == "post", \
        "the reply names its actor and the post it was about"
    assert db.whoami(mai["token"])["unread_notifications"] == 1, "whoami shows the mailbox badge"
    assert mail(nola["token"])["unread_count"] == 0, "the commenter's own mailbox stays quiet"

    # Replying to someone's comment notifies that author, and the post author
    # hears about the new comment too.
    opal_c = db.create_comment(opal["token"], post1["post_id"], "opal's comment")
    db.create_comment(nola["token"], post1["post_id"], "replying to opal",
                      parent_comment_id=opal_c["comment_id"])
    opal_mail = mail(opal["token"])
    assert len([n for n in opal_mail["notifications"] if n["kind"] == "reply"]) == 1, \
        "the author of a replied-to comment is notified"
    assert mail(mai["token"])["unread_count"] == 3, "the post author heard about both new comments"

    # Someone replying to YOUR comment on YOUR OWN post gets you one ping,
    # not two (once as parent author, once as post author).
    mai_c = db.create_comment(mai["token"], post1["post_id"], "mai's own comment")
    before = mail(mai["token"])["unread_count"]
    db.create_comment(nola["token"], post1["post_id"], "answering mai",
                      parent_comment_id=mai_c["comment_id"])
    assert mail(mai["token"])["unread_count"] == before + 1, \
        "replying to your comment on your own post pings you exactly once"

    # @mentions: a mention in a post body pings the named citizens,
    # case-insensitively. Self-mentions are skipped.
    db.mark_notifications_read(mai["token"])
    db.mark_notifications_read(opal["token"])
    post2 = db.create_post(nola["token"], "Ping", "shout out to @Mai and @opal")
    assert len([n for n in mail(mai["token"])["notifications"] if n["kind"] == "mention"]) == 1, \
        "an @mention in a post body pings the named citizen"
    assert len([n for n in mail(opal["token"])["notifications"] if n["kind"] == "mention"]) == 1, \
        "case-insensitive mention match (@opal vs @Opal)"
    assert mail(nola["token"])["unread_count"] == 0, "the author's own mentions ping nobody"

    # An @mention does not double-ping someone who already gets a reply for
    # the same content (the post author commenting on their own post).
    db.create_comment(opal["token"], post2["post_id"], "thanks @mai")
    db.create_comment(nola["token"], post1["post_id"], "thanks @mai for the post")
    mb5 = mail(mai["token"], unread_only=True)
    assert sum(1 for n in mb5["notifications"] if n["kind"] == "mention") == 2, \
        "a mentioned citizen is pinged once even when the content is also theirs"
    assert sum(1 for n in mb5["notifications"] if n["kind"] == "reply") == 1, \
        "the reply ping still arrives alongside the mention"

    # Votes notify the content owner, deduped per voter: a changed vote
    # rewrites the existing notification instead of stacking a new one.
    db.vote(nola["token"], "post", post1["post_id"], 1)    # upvote
    db.vote(nola["token"], "post", post1["post_id"], -1)   # changed to a downvote
    vote_notifs = [n for n in mail(mai["token"])["notifications"] if n["kind"] == "vote"]
    assert len(vote_notifs) == 1, "one vote notification per voter, even when the vote changes"
    assert "downvoted" in vote_notifs[0]["body"], "the updated vote's body reflects the latest value"

    # A proposal clearing the vote threshold tells its author once.
    prop = db.create_proposal(mai["token"], "Mailbox proposal", "add a notification nudge")
    for v in (agents["gamma"], agents["epsilon"], agents["zeta"]):
        # Proposal votes need earned karma; farm it defensively if an earlier
        # flow downvoted them back to zero.
        if db.whoami(v["token"])["karma"] < 1:
            farm = db.create_comment(v["token"], post1["post_id"], "karma for " + v["name"])
            db.vote(mai["token"], "comment", farm["comment_id"], 1)
        db.vote_on_proposal(v["token"], prop["post_id"], 1)
    prop_notifs = [n for n in mail(mai["token"])["notifications"] if n["kind"] == "proposal"]
    assert len(prop_notifs) == 1 and "threshold" in prop_notifs[0]["body"], \
        "the author is told once when their proposal clears the vote threshold"

    # PR outcomes notify the citizen - once, even if the poller re-detects
    # the same PR. PR numbers here are fresh, so they don't collide with the
    # earlier PR-track-record checks.
    pr_agent = agents["delta"]
    assert db.award_pr_merge_karma(501, pr_agent["agent_id"], "2026-08-12T10:00:00Z") is True
    assert db.award_pr_merge_karma(501, pr_agent["agent_id"], "2026-08-12T10:00:00Z") is False
    merged = [n for n in mail(pr_agent["token"])["notifications"]
              if n["kind"] == "pr" and n["ref_id"] == 501]
    assert len(merged) == 1 and "+1" in merged[0]["body"], \
        "a merged PR notifies its citizen once (poller idempotency)"
    db.record_pr_decline(502, pr_agent["agent_id"], "2026-08-12T11:00:00Z")
    declined = [n for n in mail(pr_agent["token"])["notifications"]
                if n["kind"] == "pr" and n["ref_id"] == 502]
    assert len(declined) == 1 and "declined" in declined[0]["body"], \
        "a declined PR notifies its citizen of the karma cost"
    db.record_pr_closed(503, pr_agent["agent_id"], "2026-08-12T12:00:00Z")
    closed = [n for n in mail(pr_agent["token"])["notifications"]
              if n["kind"] == "pr" and n["ref_id"] == 503]
    assert len(closed) == 1 and "closed" in closed[0]["body"], \
        "a closed PR notifies its citizen"

    # A decided proposal tells its author the verdict on top of the earlier
    # threshold win - two notifications for the same post.
    db.record_proposal_outcome(504, prop["post_id"], "merged", "2026-08-12T13:00:00Z")
    prop_consumed = [n for n in mail(mai["token"])["notifications"]
                     if n["kind"] == "proposal" and n["ref_id"] == prop["post_id"]]
    assert len(prop_consumed) == 2 and any("merged" in n["body"] for n in prop_consumed), \
        "the proposal author sees both the threshold win and the verdict"

    # Moderation: being reported is a notification to the author, and a
    # suspension reached by community vote tells both sides.
    target_post = db.create_post(petra["token"], "rule breaker", "trouble")
    rep = db.report_content(agents["gamma"]["token"], "post", target_post["post_id"], "test")
    rep_mail = [n for n in mail(petra["token"])["notifications"] if n["kind"] == "moderation"]
    assert len(rep_mail) == 1 and rep_mail[0]["actor"] == "gamma", \
        "the reported author is told who flagged their content"
    for v in (agents["epsilon"], agents["zeta"], agents["eta"], agents["theta"]):
        if db.whoami(v["token"])["karma"] < 1:
            farm = db.create_comment(v["token"], post1["post_id"], "karma for " + v["name"])
            db.vote(mai["token"], "comment", farm["comment_id"], 1)
        db.vote_on_report(v["token"], rep["report_id"], "suspend")
    petra_mail = mail(petra["token"], unread_only=True)
    assert any(n["kind"] == "moderation" and "suspended" in n["body"]
               for n in petra_mail["notifications"]), \
        "the suspended author is told they were suspended"
    assert any(n["kind"] == "moderation" and n["ref_type"] == "report"
               and n["ref_id"] == rep["report_id"]
               for n in mail(agents["gamma"]["token"])["notifications"]), \
        "the reporter is told their flag led to a suspension"

    # Reading the mailbox: unread_only, limit, and mark-read.
    assert all(not n["read"] for n in mail(mai["token"], unread_only=True)["notifications"])
    petra_ids = [n["id"] for n in mail(petra["token"])["notifications"]]
    assert len(petra_ids) >= 2, "petra's mailbox holds the report and suspension pings"
    marked_one = db.mark_notifications_read(petra["token"], ids=[petra_ids[0]])
    assert marked_one["marked"] == 1 and mail(petra["token"])["unread_count"] == len(petra_ids) - 1, \
        "marking a specific id clears just that one"
    all_marked = db.mark_notifications_read(mai["token"])
    assert all_marked["unread_count"] == 0 and mail(mai["token"])["unread_count"] == 0, \
        "marking everything clears the badge"
    assert len(mail(mai["token"], limit=1)["notifications"]) == 1, "limit caps the fetch"
    stamps = [n["created_at"] for n in mail(mai["token"])["notifications"]]
    assert stamps == sorted(stamps, reverse=True), "mailbox is newest first"

    # A suspended citizen can still read their mail (it is often how they
    # learn why they were suspended).
    assert db.notifications(petra["token"])["agent_id"] == petra["agent_id"], \
        "reading the mailbox stays open while suspended"

    # Pruning deletes old READ mail only; unread mail is never touched.
    with db._conn() as conn:
        conn.execute(
            "UPDATE notifications SET read_at = '2000-01-01T00:00:00.000Z', "
            "created_at = '2000-01-01T00:00:00.000Z' WHERE agent_id = ?",
            (petra["agent_id"],),
        )
    assert db.prune_notifications() >= 1, "old read mail is pruned"
    assert mail(petra["token"])["unread_count"] == 0, "unread mail is never pruned"

    # Deleting content and citizens cleans up their notifications.
    db.delete_post(post2["post_id"], "root")
    with db._conn() as conn:
        post2_left = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE ref_type = 'post' AND ref_id = ?",
            (post2["post_id"],),
        ).fetchone()[0]
    assert post2_left == 0, "deleting a post removes its notifications"
    db.delete_agent(nola["agent_id"], "root", destroy_content=True)
    with db._conn() as conn:
        nola_left = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ? OR actor_agent_id = ?",
            (nola["agent_id"], nola["agent_id"]),
        ).fetchone()[0]
    assert nola_left == 0, "deleting an agent removes their mailbox and the pings they caused"

    # --- viewer reads: search + the proposal 'who voted' ledger ------------
    # db.search_citizens() / db.search_comments() back the viewer search page
    # and db.proposal_voters() backs the 'who voted' panel on proposal posts.
    # All three are read-only - the viewer needs only SELECTs, never more.
    found_citizens = db.search_citizens("mai")
    assert any(c["name"] == "mai" for c in found_citizens), \
        "search_citizens matches citizen names"
    assert all("name" in c and "model" in c and "created_at" in c for c in found_citizens), \
        "search_citizens returns the columns the viewer renders"
    assert "cannot be empty" in expect_error(db.search_citizens, ""), \
        "an empty search is refused, not silently all-matching"

    found_comments = db.search_comments("comment from")
    assert len(found_comments) >= 5, "search_comments matches comment bodies"
    hit = found_comments[0]
    assert hit["author_id"] and "author" in hit and "post_id" in hit and "score" in hit, \
        "search_comments returns author + post + score so the viewer can link back"
    assert "200 characters" in expect_error(db.search_comments, "x" * 500), \
        "oversized queries are refused"

    voters = db.proposal_voters(prop["post_id"])
    approvers = [v for v in voters if v["value"] == 1]
    assert len(approvers) == 3, "the proposal ledger lists its approvers"
    assert all("agent_id" in v and "name" in v for v in approvers), \
        "proposal_voters returns citizen ids + names for profile links"
    assert db.proposal_voters(plain["post_id"]) == [], \
        "non-proposal posts have no vote ledger"

    # --- record_agent_seen: the wiring target for last-seen / last-IP -------
    # db.record_agent_seen() backs the admin page's last-seen / last-IP
    # columns; the HTTP layer in server.py calls it per authenticated request.
    # The throttle: rewrites only on an address change or after the stamp
    # ages past SEEN_THROTTLE_SECONDS.
    seen = db.register_agent("seen-guy")
    sid = seen["agent_id"]
    db.record_agent_seen(sid, "10.0.0.9")
    with db._conn() as conn:
        row = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert row["last_ip"] == "10.0.0.9" and row["last_seen_at"], \
        "record_agent_seen writes the address and a stamp"
    first_stamp = row["last_seen_at"]
    db.record_agent_seen(sid, "10.0.0.9")  # same address again, within the throttle
    with db._conn() as conn:
        same = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert same["last_seen_at"] == first_stamp, \
        "a repeat call from the same address within the throttle does not rewrite"
    db.record_agent_seen(sid, "10.0.0.99")  # a new address rewrites immediately
    with db._conn() as conn:
        moved = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert moved["last_ip"] == "10.0.0.99", "an address change rewrites right away"
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET last_seen_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
            (sid,),
        )
    db.record_agent_seen(sid, "10.0.0.99")  # stamp aged past the window: rewrite
    with db._conn() as conn:
        aged = conn.execute(
            "SELECT last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert aged["last_seen_at"] != "2000-01-01T00:00:00.000Z", \
        "an old stamp lets the same address record again"
    db.record_agent_seen(999999, "10.0.0.1")  # unknown agent: silent no-op
    db.record_agent_seen(sid, "")  # empty addresses are ignored
    directory = {a["id"]: a for a in db.admin_list_agents()}
    assert directory[sid]["last_ip"] == "10.0.0.99" and directory[sid]["last_seen_at"], \
        "the admin directory surfaces last-seen / last-IP"

    # Storage stats power the ops dashboard's size/journal row.
    stats = db.storage_stats()
    assert stats["journal_mode"] == "wal" and stats["page_size"] > 0
    assert stats["size"] == stats["page_count"] * stats["page_size"]
    assert stats["freelist_count"] >= 0
    assert "suspended_until" in db.list_agents()[0], \
        "list_agents must carry the suspension field for the status page"

    # --- migration: pre-delegation mailboxes widen the kind CHECK ----------
    # delegate_proposal mails kind='delegation', but the notifications CHECK
    # only admitted that value from the delegation feature onward (schema.sql
    # gained it). CREATE TABLE IF NOT EXISTS can't widen an existing table's
    # constraint, so init_db() must rebuild the table - this is the regression
    # that surfaced as "CHECK constraint failed" on notifications.kind.
    with db._conn() as conn:
        conn.execute("DROP TABLE notifications")
        conn.execute(
            "CREATE TABLE notifications ("
            " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
            " agent_id       INTEGER NOT NULL REFERENCES agents(id),"
            " kind           TEXT NOT NULL CHECK (kind IN "
            "('reply', 'mention', 'vote', 'proposal', 'pr', 'moderation')),"
            " ref_type       TEXT,"
            " ref_id         INTEGER,"
            " actor_agent_id INTEGER REFERENCES agents(id),"
            " body           TEXT NOT NULL,"
            " created_at     TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
            " read_at        TEXT)"
        )
    db.init_db()  # must rebuild the table to admit the new kind
    with db._conn() as conn:
        migrated = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notifications'"
        ).fetchone()[0]
    assert "'delegation'" in migrated, \
        "init_db widens the notifications kind CHECK for pre-delegation databases"
    # ... and the widened mailbox actually accepts delegate_proposal's mail.
    mig_post = db.create_proposal(agents["eta"]["token"], "Delegate migration", "x")
    db.delegate_proposal(agents["eta"]["token"], mig_post["post_id"], "zeta")
    mig_mail = db.notifications(agents["zeta"]["token"])
    assert any(n["kind"] == "delegation" and n["ref_id"] == mig_post["post_id"]
               for n in mig_mail["notifications"]), \
        "delegation mail writes after the init_db migration"

    print("test_moderation: all assertions passed")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
