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
  non-proposals are rejected, only the author may link their own proposal
- the Citizen trailer parser used by the outcome poller
"""

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

    # Only the author may link their own proposal to a PR.
    assert "you posted yourself" in expect_error(
        db.require_proposal_approval, agents["gamma"]["token"], p1, "repo_propose_change"
    ), "a citizen can't open a PR on someone else's proposal"

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

    print("test_moderation: all assertions passed")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
