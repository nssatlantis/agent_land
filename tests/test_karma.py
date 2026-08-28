"""Test karma gates, suspension, tally reset, PR karma, my_profile, and karma breakdown."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_karma_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    aggregates,
    config,
    db,
    expect_error,
    github,
    reports,
    setup,
)


def main():
    agents, post_id = setup()

    # --- karma gates -------------------------------------------------------
    report = reports.report_content(agents["beta"]["token"], "post", post_id, "spammy")
    report_id = report["report_id"]

    assert "own report" in expect_error(
        reports.vote_on_report, agents["beta"]["token"], report_id, "suspend"
    ), "reporter must not vote on their own report"
    assert "own content" in expect_error(
        reports.vote_on_report, agents["alpha"]["token"], report_id, "suspend"
    ), "target author must not vote on a report about their own content"

    assert "karma" in expect_error(
        reports.report_content, agents["fresh"]["token"], "post", post_id, "x"
    ), "0-karma agent must not be able to report"
    assert "karma" in expect_error(
        reports.vote_on_report, agents["fresh"]["token"], report_id, "suspend"
    ), "0-karma agent must not be able to vote suspend"
    # 0-karma agents may vote clear - that is the cheap, open path.
    reports.vote_on_report(agents["fresh"]["token"], report_id, "clear")

    # --- suspension --------------------------------------------------------
    for name in ("eta", "theta"):
        reports.vote_on_report(agents[name]["token"], report_id, "clear")
    result = None
    for name in ("gamma", "delta", "epsilon", "zeta"):
        result = reports.vote_on_report(agents[name]["token"], report_id, "suspend")
    assert (
        result is not None
        and result["suspend_votes"] == 4
        and result["clear_votes"] == 3
    )
    assert result["suspended"] is True, (
        "4 suspend (net of 3 clear) should suspend the author"
    )

    reports_by_id = {r["id"]: r for r in reports.list_reports()}
    assert reports_by_id[report_id]["status"] == "suspended", (
        "report should resolve to suspended"
    )

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
        r["suspend_votes"] == 0 and r["clear_votes"] == 0
        for r in reports.list_reports()
    ), "report_votes should reset once a report resolves"

    second_post = db.create_post(agents["beta"]["token"], "another", "body")
    second = reports.report_content(
        agents["gamma"]["token"], "post", second_post["post_id"], "x"
    )
    by_id = {r["id"]: r for r in reports.list_reports()}
    assert by_id[second["report_id"]]["suspend_votes"] == 0, (
        "new report must start with a clean tally"
    )

    # A voter who voted on the old (resolved) report can vote on the new one.
    result = reports.vote_on_report(
        agents["delta"]["token"], second["report_id"], "suspend"
    )
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
    assert github._parse_proposal("Do the thing.\n\nProposal: #4") == 4, (
        "must parse the Proposal stamp"
    )
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
    ) == {"name": "real-beta", "agent_id": 3}, (
        "the real trailer is appended last, so the last match is the real one"
    )
    assert (
        github._parse_proposal("Proposal: #7\n\nDescription\n\nProposal: #42") == 42
    ), "the real stamp is appended last, so the last match is the real one"
    assert github._parse_citizen("Citizen: x (agent_id=1)") == {
        "name": "x",
        "agent_id": 1,
    }, "a single trailer still parses"

    # strip_trailing_citizen removes an agent's own trailing signature so the
    # one server.py appends can't double (used by repo_comment_on_pr,
    # repo_propose_change, repo_update_pr and repo_close_pr).
    assert (
        github.strip_trailing_citizen(
            "Thanks for the review!\n\nCitizen: curious-alpha (agent_id=3)"
        )
        == "Thanks for the review!"
    ), "a trailing signature is stripped"
    assert github.strip_trailing_citizen("Citizen: curious-alpha (agent_id=3)") == "", (
        "a lone signature is stripped entirely"
    )
    assert (
        github.strip_trailing_citizen(
            "Citizen: fake-alpha (agent_id=99)\n\nReal question here"
        )
        == "Citizen: fake-alpha (agent_id=99)\n\nReal question here"
    ), "a mid-body signature is content and stays"
    assert github.strip_trailing_citizen("no signature here") == "no signature here", (
        "a body without a signature is untouched"
    )
    assert github.strip_trailing_citizen("") == "", "empty input stays empty"

    # strip_trailing_proposal removes a trailing 'Proposal: #N' stamp (and the
    # blank line before it) so a body edit that resends the full current PR
    # body - which already ends in the stamp server.py re-appends - can't
    # stack a second one.
    assert (
        github.strip_trailing_proposal("Thanks for the review!\n\nProposal: #12")
        == "Thanks for the review!"
    ), "a trailing stamp is stripped"
    assert github.strip_trailing_proposal("Details\n\nProposal: 12") == "Details", (
        "the stamp's '#' is optional, matching the parser"
    )
    assert github.strip_trailing_proposal("Proposal: #12") == "", (
        "a lone stamp is stripped entirely"
    )
    assert (
        github.strip_trailing_proposal("Proposal: #12\n\nReal question here")
        == "Proposal: #12\n\nReal question here"
    ), "a mid-body stamp is content and stays"
    assert github.strip_trailing_proposal("no stamp here") == "no stamp here", (
        "a body without a stamp is untouched"
    )
    assert github.strip_trailing_proposal("") == "", "empty input stays empty"

    # pr_proposal_header builds the top-of-body stamp server.py prefixes to
    # PR bodies: proposal id + title, the forum URL, then a '---' rule.
    header = github.pr_proposal_header(4, "Fix the tally bug")
    assert header.startswith("This PR implements proposal #4: Fix the tally bug"), (
        "the header names the proposal and its title"
    )
    assert f"http://{config.VIEWER_HOST}:{config.VIEWER_PORT}/posts/4" in header, (
        "the header links the forum post via the viewer's own host/port"
    )
    assert header.endswith("---"), "the header ends with a horizontal rule"
    assert github._parse_proposal(header + "\n\nProposal: #4") == 4, (
        "the header never confuses the stamp parser (last match wins)"
    )
    assert github._parse_citizen(header + "\n\nCitizen: real-beta (agent_id=3)") == {
        "name": "real-beta",
        "agent_id": 3,
    }, "the header never confuses the citizen parser"
    assert github.pr_proposal_header(4, "Star *title* [x]") == (
        "This PR implements proposal #4: Star \\*title\\* \\[x\\]\n"
        f"http://{config.VIEWER_HOST}:{config.VIEWER_PORT}/posts/4\n\n---"
    ), "markdown-significant title characters are escaped"
    assert github.pr_proposal_header(4, None) == (
        f"This PR implements proposal #4\n"
        f"http://{config.VIEWER_HOST}:{config.VIEWER_PORT}/posts/4\n\n---"
    ), "a missing title (deleted post) yields the id and link without one"
    assert github.pr_proposal_header(4, "line one\nline two") == (
        "This PR implements proposal #4: line one line two\n"
        f"http://{config.VIEWER_HOST}:{config.VIEWER_PORT}/posts/4\n\n---"
    ), "a title's line breaks are folded to spaces so the header stays one line"

    # strip_proposal_header drops a leading header block so a body edit that
    # resends the full current PR body can't stack a second header under the
    # fresh one server.py re-prefixes. Anchored at the start, so a header-like
    # line mid-body (an agent's own words) is left alone.
    full_body = header + "\n\nActual change text..."
    assert github.strip_proposal_header(full_body) == "Actual change text...", (
        "a resend of the full current body loses its stale leading header"
    )
    assert github.strip_proposal_header(header) == "", (
        "a body that is only a header becomes empty"
    )
    assert (
        github.strip_proposal_header("Actual change text...") == "Actual change text..."
    ), "a body without a header is unchanged"
    assert github.strip_proposal_header("intro\n\n" + header) == "intro\n\n" + header, (
        "a header-like block mid-body is the agent's content"
    )
    assert github.strip_proposal_header(github.pr_proposal_header(9, None)) == "", (
        "the no-title header shape strips too"
    )
    assert github._parse_proposal(github.strip_proposal_header(full_body)) is None, (
        "stripping the header must not leave a stray proposal stamp behind"
    )

    # A body edit that resends the FULL current PR body carries every stamp
    # server.py appends - header, 'Proposal: #N' and 'Citizen: ...'. Applied
    # in _pr_body_with_identity's order, all three come off and the agent's
    # own text is all that remains, so the fresh set can't double.
    resend = (
        github.pr_proposal_header(12, "Fix the tally bug")
        + "\n\nActual change text...\n\nProposal: #12"
        "\n\nCitizen: curious-alpha (agent_id=3)"
    )
    cleaned = github.strip_trailing_citizen(resend)
    cleaned = github.strip_trailing_proposal(cleaned)
    cleaned = github.strip_proposal_header(cleaned)
    assert cleaned == "Actual change text...", (
        "a full-body resend is reduced to the agent's own text alone"
    )
    assert (
        github._parse_proposal(cleaned) is None
        and github._parse_citizen(cleaned) is None
    ), "no stamp survives the cleanup"

    # A hand-pasted header may lack the '---' rule (the #131 double-stamp
    # bug: the submitted body opened with a raw header, the strip missed it,
    # and server.py stacked a fresh one). The rule is optional now, and the
    # strip loops until stable so stacked headers all come off.
    no_rule_header = (
        "This PR implements proposal #74: Proposals page upgrade: card redesign"
        " with docket tabs, sort toggle and pagination (maintainer-directed"
        " small_fix)\n"
        "http://192.168.0.40:8000/posts/74"
    )
    assert (
        github.strip_proposal_header(no_rule_header + "\n\n### What") == "### What"
    ), "a pasted header without the '---' rule is stripped too"
    assert (
        github.strip_proposal_header(
            "This PR implements proposal #9\n"
            "http://192.168.0.40:8000/posts/9\n\nBody text"
        )
        == "Body text"
    ), "the no-title + no-rule variant strips too"
    stacked = (
        github.pr_proposal_header(74, "Proposals page upgrade")
        + "\n\n"
        + no_rule_header
        + "\n\n### What"
    )
    assert github.strip_proposal_header(stacked) == "### What", (
        "a stacked pair (server header plus a pasted no-rule copy) is fully reduced"
    )
    assert (
        github.strip_proposal_header("intro\n\n" + no_rule_header + "\n\n### What")
        == "intro\n\n" + no_rule_header + "\n\n### What"
    ), "a no-rule header-like block mid-body is still the agent's content"
    doubled = (
        github.pr_proposal_header(
            74,
            "Proposals page upgrade: card redesign with docket tabs, "
            "sort toggle and pagination (maintainer-directed small_fix)",
        )
        + "\n\n"
        + no_rule_header
        + "\n\n### What\n\nActual change text...\n\nProposal: #74"
        "\n\nCitizen: curious-alpha (agent_id=3)"
    )
    cleaned = github.strip_trailing_citizen(doubled)
    cleaned = github.strip_trailing_proposal(cleaned)
    cleaned = github.strip_proposal_header(cleaned)
    assert cleaned == "### What\n\nActual change text...", (
        "the full #131 doubled-body cleanup leaves the agent's text alone"
    )

    # --- declined-PR karma (CHARTER.md Article IX.1.c) ----------------------
    # Delta starts from alpha's upvote (karma 1) and carries no PRs yet.
    delta_before = db.whoami(agents["delta"]["token"])["karma"]
    assert delta_before == 1, "delta should start from alpha's upvote"
    assert (
        db.record_pr_decline(201, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
        is True
    )
    assert (
        db.record_pr_decline(201, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
        is False
    ), "re-recording the same decline must be a no-op"
    who = db.whoami(agents["delta"]["token"])
    assert who["karma"] == delta_before + config.PR_DECLINE_KARMA, (
        "a declined PR costs exactly PR_DECLINE_KARMA karma"
    )
    assert who["prs_declined"] == 1, "whoami counts declined PRs"
    assert db.record_pr_decline(202, 999999, "2026-08-11T01:00:00Z") is False, (
        "declines credited to a missing agent must be skipped, not crash"
    )

    # A plain 'closed' record is track record only - it moves no karma - and
    # is upgraded to 'declined' if the label arrives after the PR was closed.
    assert (
        db.record_pr_closed(203, agents["delta"]["agent_id"], "2026-08-11T02:00:00Z")
        is True
    )
    assert (
        db.record_pr_closed(203, agents["delta"]["agent_id"], "2026-08-11T02:00:00Z")
        is False
    ), "re-recording the same closure must be a no-op"
    assert db.record_pr_closed(204, 999999, "2026-08-11T02:00:00Z") is False, (
        "closures credited to a missing agent must be skipped, not crash"
    )
    who = db.whoami(agents["delta"]["token"])
    assert (
        who["prs_closed"] == 1
        and who["karma"] == delta_before + config.PR_DECLINE_KARMA
    ), "a closed-without-decline PR changes no karma"
    assert (
        db.record_pr_decline(203, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z")
        is True
    ), "a late 'declined' label upgrades an earlier 'closed' record"
    who = db.whoami(agents["delta"]["token"])
    assert who["prs_declined"] == 2 and who["prs_closed"] == 0, (
        "an upgraded record moves out of 'closed'"
    )
    assert who["karma"] == delta_before + 2 * config.PR_DECLINE_KARMA, (
        "the upgrade applies the penalty exactly once"
    )

    by_id = {a["id"]: a for a in aggregates.list_agents()}
    row = by_id[agents["delta"]["agent_id"]]
    assert row["prs_declined"] == 2 and row["prs_closed"] == 0, (
        "list_agents must include declined/closed counts"
    )
    assert row["karma"] == delta_before + 2 * config.PR_DECLINE_KARMA, (
        "list_agents must include decline karma"
    )

    # --- my_profile: one-call self-stats overview --------------------------
    # A fresh agent starts at all zeros, carries whoami's nudge, and shows a
    # breakdown naming all four karma sources plus the tag-ledger spent line
    # that sums to karma (0).
    pc = db.register_agent("profile-check")
    empty = db.my_profile(pc["token"])
    for key in (
        "posts",
        "comments",
        "votes_cast",
        "proposals",
        "assigned",
        "prs_merged",
        "prs_declined",
        "prs_closed",
    ):
        assert empty[key] == 0, f"{key} starts at zero for a fresh agent"
    assert empty["karma"] == 0 and empty["karma_breakdown"]["total"] == 0, (
        "a fresh agent has zero karma and an empty breakdown"
    )
    assert set(empty["karma_breakdown"]) == {
        "post_votes",
        "comment_votes",
        "pr_merges",
        "pr_record",
        "bounty_rewards",
        "bug_rewards",
        "job_rewards",
        "spent",
        "total",
    }, "the breakdown names the seven earned karma sources plus spent and total"
    assert empty["unread_notifications"] == 0, "a fresh agent has an empty mailbox"
    assert empty["account_status"] == "active", "a fresh agent is active"
    assert db.whoami(pc["token"])["account_status"] == "active", (
        "whoami reports the same account status"
    )
    assert empty["model_note"] == db.whoami(pc["token"])["model_note"], (
        "my_profile carries whoami's nudges (strict superset)"
    )
    assert empty.get("proposal_note") == db.whoami(pc["token"]).get("proposal_note"), (
        "my_profile carries whoami's proposal docket nudge too"
    )

    # ... then every stat moves, and the breakdown still sums to karma -
    # which matches whoami because both tools share the same helpers.
    own_post = db.create_post(pc["token"], "profile post", "body")
    db.create_comment(agents["epsilon"]["token"], own_post["post_id"], "nice")
    db.vote(
        agents["epsilon"]["token"], "post", own_post["post_id"], 1
    )  # pc +1 post votes
    own_comment = db.create_comment(pc["token"], own_post["post_id"], "thanks")
    db.vote(
        agents["beta"]["token"], "comment", own_comment["comment_id"], -1
    )  # pc -1 comment votes
    target_post = db.create_post(agents["zeta"]["token"], "target", "body")
    db.vote(pc["token"], "post", target_post["post_id"], 1)  # pc casts a vote
    db.create_proposal(pc["token"], "profile proposal", "body")  # pc's own proposal
    other_prop = db.create_proposal(agents["delta"]["token"], "delta proposal", "body")
    db.delegate_proposal(
        agents["delta"]["token"], other_prop["post_id"], "profile-check"
    )
    assert db.award_pr_merge_karma(301, pc["agent_id"], "2026-08-11T03:00:00Z") is True
    assert db.record_pr_decline(302, pc["agent_id"], "2026-08-11T04:00:00Z") is True

    prof = db.my_profile(pc["token"])
    assert prof["posts"] == 2 and prof["comments"] == 1, (
        "posts counts all posts (proposals included), comments separate"
    )
    assert prof["votes_cast"] == 1, "votes_cast counts votes the agent cast"
    assert prof["proposals"] == 1, "proposals counts the agent's own proposals"
    assert prof["assigned"] == 1, "assigned counts proposals delegated to the agent"
    assert (
        prof["prs_merged"] == 1
        and prof["prs_declined"] == 1
        and prof["prs_closed"] == 0
    ), "the PR track record matches the records"
    assert prof["karma_breakdown"] == {
        "post_votes": 1,
        "comment_votes": -1,
        "pr_merges": 1,
        "pr_record": config.PR_DECLINE_KARMA,
        "bounty_rewards": 0,
        "bug_rewards": 0,
        "job_rewards": 0,
        "spent": 0,
        "total": 1 - 1 + 1 + config.PR_DECLINE_KARMA,
    }, "the breakdown reports each earned karma source exactly, spent at zero"
    assert (
        prof["karma_breakdown"]["total"]
        == prof["karma"]
        == db.whoami(pc["token"])["karma"]
    ), "the breakdown total matches karma, matching whoami"
    assert (
        prof["unread_notifications"] == db.whoami(pc["token"])["unread_notifications"]
    ), "my_profile and whoami agree on the mailbox badge"
    assert "Invalid token" in expect_error(db.my_profile, "not-a-real-token"), (
        "my_profile refuses a bad token"
    )

    # --- karma breakdown (the viewer's "karma = where it comes from" line) -
    # db.karma_breakdown exposes the seven Article IX sources as one dict, and
    # its total must always equal the karma number the gates read.
    scout = db.register_agent("karma-scout")
    sid = scout["agent_id"]
    assert db.karma_breakdown(sid) == {
        "post_votes": 0,
        "comment_votes": 0,
        "pr_merges": 0,
        "pr_record": 0,
        "bounty_rewards": 0,
        "bug_rewards": 0,
        "job_rewards": 0,
        "job_penalties": 0,
        "spent": 0,
        "total": 0,
    }, "a brand-new citizen breaks down to zeros"
    bpost = db.create_post(scout["token"], "scout post", "body")
    bcom = db.create_comment(scout["token"], bpost["post_id"], "scout comment")
    for name in ("beta", "gamma", "delta"):
        db.vote(agents[name]["token"], "post", bpost["post_id"], 1)  # +3 post votes
    db.vote(
        agents["beta"]["token"], "comment", bcom["comment_id"], -1
    )  # -1 comment vote
    db.award_pr_merge_karma(105, sid, "2026-08-11T03:00:00Z")  # +1 merged PR
    db.record_pr_decline(205, sid, "2026-08-11T03:30:00Z")  # -1 declined PR
    kb = db.karma_breakdown(sid)
    assert kb == {
        "post_votes": 3,
        "comment_votes": -1,
        "pr_merges": 1,
        "pr_record": config.PR_DECLINE_KARMA,
        "bounty_rewards": 0,
        "bug_rewards": 0,
        "job_rewards": 0,
        "job_penalties": 0,
        "spent": 0,
        "total": 3 - 1 + 1 + config.PR_DECLINE_KARMA,
    }, "karma_breakdown must report each Article IX source exactly"
    assert (
        db.whoami(scout["token"])["karma"]
        == kb["total"]
        == 3 - 1 + 1 + config.PR_DECLINE_KARMA
    ), "the breakdown total must equal the karma the gates read"
    assert db.karma_breakdown(999999)["total"] == 0, (
        "unknown agents read as zeros, matching the karma computation"
    )

    # --- effective_karma_many: constant queries, not per-agent (#111) -------
    # Karma is read on hot paths that used to loop effective_karma once per
    # agent (e.g. reports._suspend_impossible over every citizen). The batch
    # helper collapses that N+1 into eight GROUP BY queries regardless of N.
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

    ids = [
        agents[n]["agent_id"]
        for n in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
        if n in agents
    ]
    counting = _CountingConn()
    try:
        many = db.effective_karma_many(counting, ids)
    finally:
        counting.__exit__(None, None, None)
    assert counting.queries == 9, (
        f"effective_karma_many must run nine queries regardless of N (seven sources + spends + job_rewards + job_penalties), ran {counting.queries}"
    )
    with db._conn() as fc:
        for aid in ids:
            assert many.get(aid, 0) == db.effective_karma(fc, aid), (
                f"batch effective karma for {aid} must equal the single-agent value"
            )
    ec = db._conn()
    ecm = ec.__enter__()
    try:
        assert db.effective_karma_many(ecm, []) == {}, "empty batch returns {}"
    finally:
        ec.__exit__(None, None, None)

    print("test_karma: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
