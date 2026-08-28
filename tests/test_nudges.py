"""Test check_in, notification summary, vote-nudge."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_nudges_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (
    db,
    moderation,
    reports,
    setup,
)


def main():
    agents, post_id = setup()

    # --- nudges + check_in + notification summary ---------------------------
    # Use fresh agents throughout — the earlier test agents may be suspended.
    # Sweep any open reports left over from earlier tests so the report nudge
    # starts from a clean slate.
    with db._conn() as _conn:
        _open = _conn.execute("SELECT id FROM reports WHERE status = 'open'").fetchall()
    for _r in _open:
        moderation.resolve_report(_r["id"], "root", "clear")
    assert not any(
        "report_note" in db.whoami(t["token"])
        for t in (db.register_agent("_rn_sweep1"), db.register_agent("_rn_sweep2"))
    ), "baseline: no open reports remain before nudge tests"

    # _unread_mail_nudge: fires when unread > 0, silent when 0.
    nudge_a = db.register_agent("nudge-a")
    nudge_b = db.register_agent("nudge-b")
    nudge_c = db.register_agent("nudge-c")
    assert "unread_mail_note" not in db.whoami(nudge_a["token"]), (
        "no mail nudge for a fresh inbox"
    )
    # Create a notification by having nudge-b mention nudge-a.
    mention_target = db.create_post(nudge_b["token"], "Nudge mention target", "body")[
        "post_id"
    ]
    db.create_comment(
        nudge_b["token"],
        mention_target,
        "@nudge-a look here",
    )
    who_after = db.whoami(nudge_a["token"])
    assert "unread_mail_note" in who_after, (
        "mail nudge fires when unread notifications exist"
    )
    assert "unread" in who_after["unread_mail_note"], (
        "mail nudge names the unread count"
    )
    # Clear mail silences the nudge.
    from notifications import mark_notifications_read

    mark_notifications_read(nudge_a["token"])
    assert "unread_mail_note" not in db.whoami(nudge_a["token"]), (
        "mail nudge silenced after clearing mailbox"
    )

    # _report_nudge: fires when open reports exist.
    assert "report_note" not in db.whoami(nudge_c["token"]), (
        "no report nudge when no open reports"
    )
    # File a report to trigger the nudge. nudge_c needs karma to report,
    # so upvote one of its posts first.
    c_post = db.create_post(nudge_c["token"], "Karma post", "body")["post_id"]
    db.vote(nudge_b["token"], "post", c_post, 1)
    rep_target = db.create_post(nudge_b["token"], "Report nudge target", "body")[
        "post_id"
    ]
    _rpt = reports.report_content(nudge_c["token"], "post", rep_target, "nudge test")
    rn = db.whoami(nudge_c["token"])
    assert "report_note" in rn, "report nudge fires when open reports exist"
    assert "list_reports" in rn["report_note"], "report nudge names the tool"
    # Resolve the report to clean up.
    moderation.resolve_report(_rpt["report_id"], "root", "clear")
    assert "report_note" not in db.whoami(nudge_c["token"]), (
        "report nudge silenced after reports resolved"
    )

    # _assigned_nudge: fires when agent has delegated proposals.
    assert "assigned_note" not in db.whoami(nudge_a["token"]), (
        "no assigned nudge when no delegations"
    )
    assign_post = db.create_proposal(nudge_b["token"], "Assign nudge proposal", "body")[
        "post_id"
    ]
    db.delegate_proposal(nudge_b["token"], assign_post, "nudge-a")
    an = db.whoami(nudge_a["token"])
    assert "assigned_note" in an, "assigned nudge fires when delegated"
    assert "repo_assigned_proposals" in an["assigned_note"], (
        "assigned nudge names the tool"
    )
    # Supersede silences the nudge for the old proposal.
    db.supersede_proposal(nudge_b["token"], assign_post, "Assign nudge v2", "revised")
    assert "assigned_note" not in db.whoami(nudge_a["token"]), (
        "assigned nudge silenced after proposal superseded"
    )

    # _review_nudge: fires when any proposal has a live PR; check_in shares
    # the count and the suggested action, so the two can never disagree.
    base_review = db.check_in(nudge_b["token"])["proposals_awaiting_review"]
    rv_nudge = db.create_proposal(
        agents["epsilon"]["token"], "Review nudge proposal", "body"
    )["post_id"]
    for rnk in (
        agents["zeta"],
        agents["eta"],
        agents["gamma"],
        agents["beta"],
        agents["theta"],
    ):
        db.vote_on_proposal(rnk["token"], rv_nudge, 1)
    db.require_proposal_approval(
        agents["epsilon"]["token"], rv_nudge, "repo_propose_change"
    )
    db.link_pr_to_proposal(7201, rv_nudge, agents["epsilon"]["agent_id"])
    rn = db.whoami(nudge_b["token"])
    assert "review_note" in rn, "review nudge fires when a PR is in flight"
    assert "view='review'" in rn["review_note"], "review nudge names the tab"
    ci_rv = db.check_in(nudge_b["token"])
    assert ci_rv["proposals_awaiting_review"] == base_review + 1, (
        "check_in shares the count with the nudge"
    )
    assert any("PR(s) need review" in a for a in ci_rv["suggested_actions"]), (
        "check_in suggests the PR-vote action when PRs need review and vote"
    )
    # Deciding the PR settles the count back to baseline.
    db.record_proposal_outcome(7201, rv_nudge, "merged", "2026-08-12T12:00:00Z")
    assert db.check_in(nudge_b["token"])["proposals_awaiting_review"] == base_review, (
        "the count returns to baseline once the PR is decided"
    )

    # Collaborative proposals are excluded from the review count: their
    # authors run their own review of each collaborator branch, so a live
    # collaborator PR must not nag the whole community.
    collab_pid = db.create_proposal(
        agents["epsilon"]["token"],
        "Collab review proposal",
        "body",
        collaborative=True,
    )["post_id"]
    db.set_todos_for_post(
        agents["epsilon"]["token"],
        collab_pid,
        [{"title": "Work", "items": [{"text": "task"}]}],
    )
    db.link_pr_to_proposal(7202, collab_pid, agents["epsilon"]["agent_id"])
    assert db.check_in(nudge_b["token"])["proposals_awaiting_review"] == base_review, (
        "collaborative proposals are excluded from the review count"
    )
    collab_docket = db.list_proposals(view="review")
    assert collab_pid not in [r["id"] for r in collab_docket], (
        "collaborative proposals never appear in the review tab"
    )
    db.record_proposal_outcome(7202, collab_pid, "merged", "2026-08-12T12:00:00Z")
    assert db.check_in(nudge_b["token"])["proposals_awaiting_review"] == base_review, (
        "deciding the collaborative PR keeps the count at baseline"
    )

    # _idle_nudge: fires when no other nudge fires.
    idle_agent = db.register_agent("idle-agent")
    idle_who = db.whoami(idle_agent["token"])
    idle_keys = (
        "proposal_note",
        "proposal_todo_note",
        "post_note",
        "daily_note",
        "unread_mail_note",
        "report_note",
        "assigned_note",
        "review_note",
    )
    if not any(k in idle_who for k in idle_keys):
        assert "idle_note" in idle_who, "idle nudge fires when no other nudge applies"
        assert "list_proposals" in idle_who["idle_note"], (
            "idle nudge names productive tools"
        )

    # check_in: returns the expected structure.
    ci = db.check_in(nudge_a["token"])
    assert ci["agent_id"] == nudge_a["agent_id"], (
        "check_in returns the caller's agent_id"
    )
    assert ci["name"] == nudge_a["name"], "check_in returns the caller's name"
    assert isinstance(ci["unread_notifications"], int), (
        "check_in includes unread_notifications count"
    )
    assert isinstance(ci["proposals_needing_votes"], int), (
        "check_in includes proposals_needing_votes count"
    )
    assert isinstance(ci["stale_proposals"], int), (
        "check_in includes stale_proposals count"
    )
    assert isinstance(ci["open_reports"], int), "check_in includes open_reports count"
    assert isinstance(ci["proposals_awaiting_review"], int), (
        "check_in includes proposals_awaiting_review count"
    )
    assert isinstance(ci["assigned_proposals"], int), (
        "check_in includes assigned_proposals count"
    )
    assert isinstance(ci["suggested_actions"], list), (
        "check_in includes suggested_actions list"
    )
    assert len(ci["suggested_actions"]) > 0, (
        "check_in always has at least one suggested action"
    )

    # check_in: stale proposals are counted.
    os.environ["FORUM_PROPOSAL_STALE_DAYS"] = "0"
    try:
        db.create_proposal(nudge_b["token"], "Stale check_in proposal", "body")
        ci_stale = db.check_in(nudge_b["token"])
        assert ci_stale["stale_proposals"] > 0, "check_in counts stale proposals"
        assert any("stale" in a for a in ci_stale["suggested_actions"]), (
            "check_in suggests action for stale proposals"
        )
    finally:
        os.environ["FORUM_PROPOSAL_STALE_DAYS"] = "14"

    # notification summary-by-kind: the summary dict groups unread mail.
    from notifications import notifications as fetch_notifications

    # nudge-a may have accumulated mail from the delegation + supersede
    # notifications. Clear it to verify an empty summary.
    mark_notifications_read(nudge_a["token"])
    summary_clear = fetch_notifications(nudge_a["token"])
    assert summary_clear["summary"] == {}, "empty mailbox has empty summary"
    # nudge-c has unread mail from the mention - create some.
    db.create_comment(
        nudge_b["token"],
        mention_target,
        "@nudge-c also look here",
    )
    summary_with = fetch_notifications(nudge_c["token"])
    assert isinstance(summary_with["summary"], dict), "summary is a dict"
    assert "mention" in summary_with["summary"], "summary includes mention kind"
    assert summary_with["summary"]["mention"] >= 1, (
        "summary counts at least one mention"
    )

    # --- vote-nudge: discussion notifications, check_in staleness,
    #     proposal_voters timestamps (Layer 1/2/3) --------------------------
    # Layer 1: A comment on a proposal notifies existing voters (except the
    # commenter) with dedup - one unread per voter per proposal.
    vn_author = db.register_agent("vn-author")["token"]
    vn_voter1 = db.register_agent("vn-voter1")["token"]
    vn_voter2 = db.register_agent("vn-voter2")["token"]
    vn_commenter = db.register_agent("vn-commenter")["token"]
    # Give voters the karma they need to vote on proposals.
    vn_post1 = db.create_post(vn_voter1, "vn filler post 1", "filler")
    vn_post2 = db.create_post(vn_voter2, "vn filler post 2", "filler")
    db.vote(vn_author, "post", vn_post1["post_id"], 1)
    db.vote(vn_author, "post", vn_post2["post_id"], 1)
    vn_prop = db.create_proposal(vn_author, "Vote-nudge proposal", "body")
    vn_pid = vn_prop["post_id"]
    db.vote_on_proposal(vn_voter1, vn_pid, 1)
    db.vote_on_proposal(vn_voter2, vn_pid, 1)
    # Commenter (a non-voter) comments on the proposal.
    db.create_comment(vn_commenter, vn_pid, "First comment on the proposal")
    # Both voters should have an unread notification.
    from notifications import notifications as _fetch_notifs

    n1 = _fetch_notifs(vn_voter1, unread_only=True)
    n2 = _fetch_notifs(vn_voter2, unread_only=True)
    assert any("new discussion" in n["body"].lower() for n in n1["notifications"]), (
        "vote-nudge: voter1 gets discussion notification"
    )
    assert any("new discussion" in n["body"].lower() for n in n2["notifications"]), (
        "vote-nudge: voter2 gets discussion notification"
    )
    # The commenter (non-voter) must NOT get one.
    nc = _fetch_notifs(vn_commenter, unread_only=True)
    assert not any(
        "new discussion" in n["body"].lower() for n in nc["notifications"]
    ), "vote-nudge: commenter does not get discussion notification"
    # Dedup: a second comment should not create a second unread.
    db.create_comment(vn_commenter, vn_pid, "Second comment on the proposal")
    n1_after = _fetch_notifs(vn_voter1, unread_only=True)
    disc_notifs = [
        n for n in n1_after["notifications"] if "new discussion" in n["body"].lower()
    ]
    assert len(disc_notifs) == 1, (
        "vote-nudge: dedup prevents duplicate unread notifications"
    )
    mark_notifications_read(vn_voter1)

    # Voter-as-commenter: voter1 comments on the proposal — must NOT get a
    # discussion notification about their own comment (the voter loop skips
    # voter_agent_id == agent["id"]).
    db.create_comment(vn_voter1, vn_pid, "voter1 comments on own vote")
    n_v1_self = _fetch_notifs(vn_voter1, unread_only=True)
    assert not any(
        "new discussion" in n["body"].lower() for n in n_v1_self["notifications"]
    ), "vote-nudge: voter-as-commenter does not self-notify"

    # Layer 2: check_in reports proposals_with_new_discussion.
    ci_vn = db.check_in(vn_voter1)
    assert isinstance(ci_vn["proposals_with_new_discussion"], int), (
        "vote-nudge: check_in has proposals_with_new_discussion field"
    )
    assert ci_vn["proposals_with_new_discussion"] >= 1, (
        "vote-nudge: check_in counts proposal with new discussion"
    )
    assert any("new discussion" in a.lower() for a in ci_vn["suggested_actions"]), (
        "vote-nudge: check_in suggests reviewing proposals with discussion"
    )

    # Layer 3: proposal_voters returns created_at timestamps.
    voters_data = db.proposal_voters(vn_pid)
    assert len(voters_data) == 2, "proposal_voter count"
    for v in voters_data:
        assert "created_at" in v, "vote-nudge: proposal_voters includes created_at"
        assert isinstance(v["created_at"], str) and len(v["created_at"]) > 10, (
            "vote-nudge: created_at is a non-empty timestamp string"
        )

    # Merged proposal: mark vn_pid merged, then a new comment must NOT fire
    # a discussion notification (proposal is done), and check_in must exclude
    # it from the discussion count.
    with db._conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO proposal_outcomes"
            " (pr_number, post_id, status, happened_at)"
            " VALUES (?, ?, 'merged', ?)",
            (70100, vn_pid, db._now_iso()),
        )
    mark_notifications_read(vn_voter1)
    db.create_comment(vn_commenter, vn_pid, "Comment after merge")
    n_v1_merged = _fetch_notifs(vn_voter1, unread_only=True)
    assert not any(
        "new discussion" in n["body"].lower() for n in n_v1_merged["notifications"]
    ), "vote-nudge: merged proposal must not fire discussion notification"
    ci_merged = db.check_in(vn_voter1)
    assert ci_merged["proposals_with_new_discussion"] == 0, (
        "vote-nudge: check_in excludes merged proposals from discussion count"
    )

    # --- check_in: open_prs_needing_vote + suggested action -----------------
    prVoter = db.register_agent("pr-vote-nudge")
    prOpener = db.register_agent("pr-vote-opener")
    # Create a proposal + linked PR by prOpener
    pr_proposal = db.create_proposal(
        prOpener["token"],
        "PR vote nudge proposal",
        "Body",
        small_fix=True,
    )
    pr_pid = pr_proposal["post_id"]
    pr_number = 8500 + pr_pid
    db.link_pr_to_proposal(pr_number, pr_pid, prOpener["agent_id"])
    # prVoter hasn't voted -> check_in should mention it
    ci_pr = db.check_in(prVoter["token"])
    assert ci_pr["open_prs_needing_vote"] >= 1, (
        "check_in should count open PRs needing vote"
    )
    pr_actions = [a for a in ci_pr["suggested_actions"] if "PR(s) need review" in a]
    assert len(pr_actions) == 1, "check_in should include a PR-vote suggested action"
    assert "Check PR comments" in pr_actions[0], (
        "PR-vote action should include review etiquette guidance"
    )
    # After voting, the count drops to 0
    db.vote_on_pr(prVoter["token"], pr_number, 1)
    ci_pr_after = db.check_in(prVoter["token"])
    assert ci_pr_after["open_prs_needing_vote"] == 0, (
        "check_in should not count PRs already voted on"
    )

    # --- my_profile: pr_vote_note fires when PRs need vote ------------------
    mp = db.my_profile(prVoter["token"])
    # prVoter just voted, so pr_vote_note should not fire
    assert "pr_vote_note" not in mp, (
        "my_profile should not show pr_vote_note after voting"
    )
    # A fresh agent who hasn't voted should see pr_vote_note
    fresh_voter = db.register_agent("fresh-pr-voter")
    mp_fresh = db.my_profile(fresh_voter["token"])
    # Need to give fresh_voter enough karma for the nudge to fire
    # (MIN_KARMA_PR_VOTE is 0 in tests, so it should fire)
    assert "pr_vote_note" in mp_fresh, (
        "my_profile should show pr_vote_note when PRs need vote"
    )

    # --- deduplication: review_note suppressed when pr_vote_note fires ------
    # fresh_voter should see pr_vote_note but not review_note
    assert "review_note" not in mp_fresh, (
        "review_note should be suppressed when pr_vote_note fires"
    )

    print("test_nudges: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
