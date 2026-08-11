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

    print("test_moderation: all assertions passed")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
