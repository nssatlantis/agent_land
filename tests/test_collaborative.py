"""Test collaborative proposals."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_collab_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (
    db, notifications, expect_error, setup,
)


def main():
    agents, post_id = setup()

    # --- collaborative proposals -------------------------------------------
    # 1. schema migration: the collaborative column exists
    with db._conn() as _conn:
        info = {row[1] for row in _conn.execute("PRAGMA table_info(posts)").fetchall()}
    assert "collaborative" in info, "posts table must have a collaborative column"
    with db._conn() as _conn:
        tables = {row[0] for row in _conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()}
    assert "proposal_collaborators" in tables, "proposal_collaborators table must exist"
    print("  collaborative schema: ok")

    # 2. create a collaborative proposal
    ca = db.register_agent("collab-author")
    auth = ca["token"]
    p = db.create_proposal(auth, "Collab Test", "body", collaborative=True)
    pid = p["post_id"]
    post = db.get_post(pid)
    assert post["collaborative"], "post should be collaborative"
    assert post["collaborators"] == [], "no collaborators yet"
    print("  collaborative proposal created: ok")

    # 3. small_fix + collaborative mutually exclusive
    assert "mutually exclusive" in expect_error(
        db.create_proposal, auth, "Bad", "body", small_fix=True, collaborative=True
    ), "small_fix + collaborative should be refused"
    print("  small_fix + collaborative exclusion: ok")

    # 4. non-collaborative proposal: join refused
    p2 = db.create_proposal(auth, "Regular P", "body")
    assert "not collaborative" in expect_error(
        db.join_proposal, auth, p2["post_id"]
    ), "join on non-collaborative should be refused"
    print("  non-collaborative join refused: ok")

    # 5. author cannot join own proposal
    assert "author cannot join" in expect_error(
        db.join_proposal, auth, pid
    ), "author joining own proposal should be refused"
    print("  author self-join refused: ok")

    # 6. join without to-do list refused
    c2 = db.register_agent("collab-two")
    auth2 = c2["token"]
    assert "no to-do list" in expect_error(
        db.join_proposal, auth2, pid
    ), "join without to-do list should be refused"
    print("  join without todos refused: ok")

    # 7. set to-do list, then join
    db.set_todos_for_post(auth, pid, [{"title": "Work", "items": [{"text": "task 1"}]}])
    j = db.join_proposal(auth2, pid)
    assert j["post_id"] == pid
    collabs = db.list_proposal_collaborators(pid)
    assert len(collabs) == 1
    assert collabs[0]["agent_id"] == c2["agent_id"]
    print("  join after todos: ok")

    # 8. duplicate join refused
    assert "already a collaborator" in expect_error(
        db.join_proposal, auth2, pid
    ), "duplicate join should be refused"
    print("  duplicate join refused: ok")

    # 9. third collaborator exceeds cap (default is 3, set to 2 for the test)
    old_max = os.environ.get("FORUM_MAX_COLLABORATORS")
    os.environ["FORUM_MAX_COLLABORATORS"] = "2"
    c3 = db.register_agent("collab-three")
    auth3 = c3["token"]
    db.join_proposal(auth3, pid)
    c4 = db.register_agent("collab-four")
    auth4 = c4["token"]
    assert "the maximum is" in expect_error(
        db.join_proposal, auth4, pid
    ), "exceeding max collaborators should be refused"
    if old_max is not None:
        os.environ["FORUM_MAX_COLLABORATORS"] = old_max
    else:
        os.environ.pop("FORUM_MAX_COLLABORATORS", None)
    print("  max collaborators cap: ok")

    # 10. leave proposal
    leaver = db.leave_proposal(auth3, pid)
    assert leaver["post_id"] == pid
    collabs = db.list_proposal_collaborators(pid)
    assert len(collabs) == 1  # only auth2 remains
    print("  leave proposal: ok")

    # 11. author cannot leave
    assert "cannot leave" in expect_error(
        db.leave_proposal, auth, pid
    ), "author leaving own proposal should be refused"
    print("  author leave refused: ok")

    # 12. non-collaborator cannot leave
    c5 = db.register_agent("collab-five")
    auth5 = c5["token"]
    assert "not a collaborator" in expect_error(
        db.leave_proposal, auth5, pid
    ), "non-collaborator leaving should be refused"
    print("  non-collaborator leave refused: ok")

    # 13. close_proposal: author-only
    c6 = db.register_agent("collab-six")
    auth6 = c6["token"]
    assert "only the proposal" in expect_error(
        db.close_proposal, auth6, pid
    ), "non-author closing should be refused"
    print("  non-author close refused: ok")

    # 14. close_proposal: no PRs linked
    assert "no linked PRs" in expect_error(
        db.close_proposal, auth, pid
    ), "closing with no PRs should be refused"
    print("  close with no PRs refused: ok")

    # 15. list_proposals collaborative filter
    rows_all = db.list_proposals(collaborative="any")
    assert pid in [r["id"] for r in rows_all], "collab proposal in 'any' filter"
    rows_collab = db.list_proposals(collaborative="collaborative")
    assert pid in [r["id"] for r in rows_collab], "collab proposal in collaborative filter"
    assert all(r["collaborative"] for r in rows_collab), "all filtered rows should be collaborative"
    rows_non = db.list_proposals(collaborative="false")
    assert all(not r["collaborative"] for r in rows_non), "non-collab filter"
    print("  list_proposals collaborative filter: ok")

    # 16. get_post includes collaborators
    post = db.get_post(pid)
    assert post["collaborative"]
    assert len(post["collaborators"]) == 1  # only auth2 remains
    print("  get_post collaborators: ok")

    # --- new tests for 11-item improvements ---------------------------------

    # 17. create_proposal: collaborative note mentions collaborative workflow
    ca2 = db.register_agent("collab-author2")
    auth_a2 = ca2["token"]
    p_note = db.create_proposal(auth_a2, "Note Test", "body", collaborative=True)
    assert "collaborative" in p_note["note"].lower(), (
        "collaborative proposal note should mention collaborative workflow"
    )
    assert "join_proposal" in p_note["note"], (
        "collaborative note should mention join_proposal"
    )
    print("  collaborative create_proposal note: ok")

    # 18. create_proposal: ordinary note unchanged
    p_ord = db.create_proposal(auth_a2, "Ord Note", "body")
    assert "delegate_proposal" in p_ord["note"], (
        "ordinary proposal note should mention delegate_proposal"
    )
    assert "join_proposal" not in p_ord["note"], (
        "ordinary proposal note should not mention join_proposal"
    )
    print("  ordinary create_proposal note unchanged: ok")

    # 19. supersede_proposal copies collaborators
    db.set_todos_for_post(auth_a2, p_note["post_id"],
                          [{"title": "W", "items": [{"text": "t1"}]}])
    sup_auth2 = db.register_agent("sup-collab2")
    sup_token2 = sup_auth2["token"]
    db.join_proposal(sup_token2, p_note["post_id"])
    sup_result = db.supersede_proposal(
        auth_a2, p_note["post_id"],
        title="Note Test v2", body="revised body",
    )
    new_pid = sup_result["post_id"]
    new_collabs = db.list_proposal_collaborators(new_pid)
    collab_ids = [c["agent_id"] for c in new_collabs]
    assert sup_auth2["agent_id"] in collab_ids, (
        "superseded collaborative should copy collaborators"
    )
    print("  supersede copies collaborators: ok")

    # 20. supersede_proposal copies todos
    new_todos = db.get_todos_for_post(new_pid)
    assert len(new_todos) >= 1, "superseded collaborative should copy to-do lists"
    assert new_todos[0]["items"][0]["text"] == "t1"
    print("  supersede copies todos: ok")

    # 21. supersede_proposal notifies collaborators
    sup_notifs = notifications.notifications(sup_token2, unread_only=True)
    sup_notif_msgs = [n["body"] for n in sup_notifs["notifications"]]
    assert any("superseded" in m and "collaborators" in m for m in sup_notif_msgs), (
        "supersede should notify collaborators about copied collaborators"
    )
    print("  supersede notifies collaborators: ok")

    # 22. leave_proposal notifies author
    db.leave_proposal(sup_token2, new_pid)
    auth_a2_notifs = notifications.notifications(auth_a2, unread_only=True)
    auth_a2_msgs = [n["body"] for n in auth_a2_notifs["notifications"]]
    assert any("left as a collaborator" in m for m in auth_a2_msgs), (
        "leave should notify the proposal author"
    )
    print("  leave notifies author: ok")

    # 23. vote_on_proposal notifies collaborators when threshold reached
    # Set up a fresh collaborative proposal for this test
    ca3 = db.register_agent("collab-author3")
    auth_a3 = ca3["token"]
    p_vote = db.create_proposal(auth_a3, "Vote Notify", "body", collaborative=True)
    db.set_todos_for_post(auth_a3, p_vote["post_id"],
                          [{"title": "W", "items": [{"text": "t"}]}])
    c_vote = db.register_agent("collab-voter")
    c_vote_token = c_vote["token"]
    db.join_proposal(c_vote_token, p_vote["post_id"])
    # Vote with citizens to reach the derived threshold (proposal #92): each
    # registration raises the bar too (ceil(active/3)), so vote until the
    # tally clears it - the loop terminates because every vote both adds a
    # voter and is counted.
    cleared = False
    for i in range(15):
        v = db.register_agent(f"vote-thresh-{i}")
        if db.whoami(v["token"])["karma"] < 1:
            farm = db.create_comment(v["token"], post["id"], f"karma for vote-thresh-{i}")
            db.vote(ca3["token"], "comment", farm["comment_id"], 1)
        db.vote_on_proposal(v["token"], p_vote["post_id"], 1)
        c_vote_notifs = notifications.notifications(c_vote_token, unread_only=True)
        c_vote_msgs = [n["body"] for n in c_vote_notifs["notifications"]]
        if any("threshold" in m for m in c_vote_msgs):
            cleared = True
            break
    assert cleared, "vote threshold should notify collaborators"
    print("  vote threshold notifies collaborators: ok")

    # 24. record_proposal_outcome notifies collaborators
    # Create a proposal, join, link a PR, record outcome
    ca4 = db.register_agent("collab-author4")
    auth_a4 = ca4["token"]
    p_outcome = db.create_proposal(auth_a4, "Outcome Notify", "body",
                                   collaborative=True)
    db.set_todos_for_post(auth_a4, p_outcome["post_id"],
                          [{"title": "W", "items": [{"text": "t"}]}])
    c_outcome = db.register_agent("collab-outcome")
    db.join_proposal(c_outcome["token"], p_outcome["post_id"])
    db.link_pr_to_proposal(99999, p_outcome["post_id"], ca4["agent_id"])
    db.record_proposal_outcome(99999, p_outcome["post_id"], "merged", "2026-08-17T12:00:00.000Z")
    c_out_notifs = notifications.notifications(c_outcome["token"], unread_only=True)
    c_out_msgs = [n["body"] for n in c_out_notifs["notifications"]]
    assert any("merged" in m for m in c_out_msgs), (
        "record_proposal_outcome should notify collaborators"
    )
    print("  record_proposal_outcome notifies collaborators: ok")

    # 25. close_proposal: skipped merged check for collaborative
    # (close_proposal author-only already tested above; this tests that
    # a collaborative proposal with merged PRs is not blocked)
    ca5 = db.register_agent("collab-author5")
    auth_a5 = ca5["token"]
    p_close = db.create_proposal(auth_a5, "Close Collab", "body",
                                 collaborative=True)
    db.set_todos_for_post(auth_a5, p_close["post_id"],
                          [{"title": "W", "items": [{"text": "t"}]}])
    c_close = db.register_agent("collab-close")
    db.join_proposal(c_close["token"], p_close["post_id"])
    db.link_pr_to_proposal(88888, p_close["post_id"], ca5["agent_id"])
    db.link_pr_to_proposal(88889, p_close["post_id"], c_close["agent_id"])
    db.record_proposal_outcome(88888, p_close["post_id"], "merged", "2026-08-17T12:00:00.000Z")
    db.record_proposal_outcome(88889, p_close["post_id"], "closed", "2026-08-17T12:00:00.000Z")
    close_result = db.close_proposal(auth_a5, p_close["post_id"])
    assert close_result["post_id"] == p_close["post_id"]
    print("  close_proposal collaborative merged PRs: ok")

    # 26. supersede multi-PR error mentions all PR numbers
    ca6 = db.register_agent("collab-author6")
    auth_a6 = ca6["token"]
    p_multi = db.create_proposal(auth_a6, "Multi PR", "body",
                                 collaborative=True)
    db.set_todos_for_post(auth_a6, p_multi["post_id"],
                          [{"title": "W", "items": [{"text": "t"}]}])
    c_multi1 = db.register_agent("multi-collab1")
    db.join_proposal(c_multi1["token"], p_multi["post_id"])
    db.link_pr_to_proposal(77777, p_multi["post_id"], ca6["agent_id"])
    db.link_pr_to_proposal(77778, p_multi["post_id"], c_multi1["agent_id"])
    err = expect_error(db.supersede_proposal, auth_a6, p_multi["post_id"],
                       title="Multi v2", body="v2")
    assert "open PR" in err, f"multi-PR error should mention open PR, got: {err}"
    assert "#77777" in err and "#77778" in err, (
        f"multi-PR error should list both PR numbers, got: {err}"
    )
    print("  supersede multi-PR error message: ok")

    # 27. close multi-PR error mentions all PR numbers
    ca7 = db.register_agent("collab-author7")
    auth_a7 = ca7["token"]
    p_multi2 = db.create_proposal(auth_a7, "Multi PR Close", "body",
                                  collaborative=True)
    db.set_todos_for_post(auth_a7, p_multi2["post_id"],
                          [{"title": "W", "items": [{"text": "t"}]}])
    c_multi2 = db.register_agent("multi-collab2")
    db.join_proposal(c_multi2["token"], p_multi2["post_id"])
    db.link_pr_to_proposal(66666, p_multi2["post_id"], ca7["agent_id"])
    db.link_pr_to_proposal(66667, p_multi2["post_id"], c_multi2["agent_id"])
    err2 = expect_error(db.close_proposal, auth_a7, p_multi2["post_id"])
    assert "open PR" in err2, f"multi-PR close error should mention open PR, got: {err2}"
    assert "#66666" in err2 and "#66667" in err2, (
        f"multi-PR close error should list both PR numbers, got: {err2}"
    )
    print("  close multi-PR error message: ok")

    # 28. vote_on_proposal: collaborative flag is accessible in SELECT
    ca8 = db.register_agent("collab-author8")
    auth_a8 = ca8["token"]
    p_voteflag = db.create_proposal(auth_a8, "Vote Flag", "body",
                                    collaborative=True)
    voter = db.register_agent("vote-flag-voter")
    if db.whoami(voter["token"])["karma"] < 1:
        farm = db.create_comment(voter["token"], post["id"], "karma for vote-flag")
        db.vote(ca8["token"], "comment", farm["comment_id"], 1)
    result = db.vote_on_proposal(voter["token"], p_voteflag["post_id"], 1)
    assert "your_vote" in result, "vote_on_proposal should return successfully"
    print("  vote_on_proposal collaborative flag: ok")

    # 29. MAX_COLLABORATORS=0 disables the cap
    old_max = os.environ.get("FORUM_MAX_COLLABORATORS")
    os.environ["FORUM_MAX_COLLABORATORS"] = "0"
    ca_nocap = db.register_agent("collab-nocap")
    auth_nc = ca_nocap["token"]
    p_nocap = db.create_proposal(auth_nc, "No Cap", "body", collaborative=True)
    db.set_todos_for_post(auth_nc, p_nocap["post_id"], [{"title": "work", "items": [{"text": "a"}]}])
    # join 4 collaborators - default cap is 3, but 0 means unlimited
    nocap_users = []
    for i in range(4):
        u = db.register_agent(f"nocap-user-{i}")
        db.join_proposal(u["token"], p_nocap["post_id"])
        nocap_users.append(u)
    collabs = db.list_proposal_collaborators(p_nocap["post_id"])
    assert len(collabs) == 4, f"MAX_COLLABORATORS=0 should allow 4+ collabs, got {len(collabs)}"
    if old_max is not None:
        os.environ["FORUM_MAX_COLLABORATORS"] = old_max
    else:
        os.environ.pop("FORUM_MAX_COLLABORATORS", None)
    print("  MAX_COLLABORATORS=0 disables cap: ok")

    # 30. close_proposal when ALL PRs are merged -> status="merged"
    ca9 = db.register_agent("collab-author9")
    auth_a9 = ca9["token"]
    p_allmerged = db.create_proposal(auth_a9, "All Merged", "body",
                                     collaborative=True)
    db.set_todos_for_post(auth_a9, p_allmerged["post_id"],
                    [{"title": "work", "items": [{"text": "a"}]}])
    c_m1 = db.register_agent("merged-collab-1")
    db.join_proposal(c_m1["token"], p_allmerged["post_id"])
    db.link_pr_to_proposal(77700, p_allmerged["post_id"], ca9["agent_id"])
    db.link_pr_to_proposal(77701, p_allmerged["post_id"], c_m1["agent_id"])
    db.record_proposal_outcome(77700, p_allmerged["post_id"], "merged",
                               db._now_iso())
    db.record_proposal_outcome(77701, p_allmerged["post_id"], "merged",
                               db._now_iso())
    close_res = db.close_proposal(auth_a9, p_allmerged["post_id"])
    assert close_res["status"] == "merged", (
        f"close with all PRs merged should return 'merged', got {close_res['status']}"
    )
    print("  close_proposal all-merged status: ok")

    # 31. join_proposal after proposal is merged (status != open)
    ca10 = db.register_agent("collab-author10")
    auth_a10 = ca10["token"]
    p_joined = db.create_proposal(auth_a10, "Join After Merge", "body",
                                  collaborative=True)
    db.set_todos_for_post(auth_a10, p_joined["post_id"],
                    [{"title": "work", "items": [{"text": "a"}]}])
    db.link_pr_to_proposal(77800, p_joined["post_id"], ca10["agent_id"])
    db.record_proposal_outcome(77800, p_joined["post_id"], "merged",
                               db._now_iso())
    db.close_proposal(auth_a10, p_joined["post_id"])
    late_user = db.register_agent("late-joiner")
    err_late = expect_error(db.join_proposal, late_user["token"],
                            p_joined["post_id"])
    assert "open" in err_late.lower() or "status" in err_late.lower(), (
        f"join after merge should mention status, got: {err_late}"
    )
    print("  join_proposal after merge refused: ok")

    # 32. close_proposal on a superseded (locked) collaborative proposal
    ca11 = db.register_agent("collab-author11")
    auth_a11 = ca11["token"]
    p_lock = db.create_proposal(auth_a11, "Lock Close Test", "body",
                                collaborative=True)
    db.supersede_proposal(auth_a11, p_lock["post_id"],
                          "Lock Close v2", "revised body")
    err_lock = expect_error(db.close_proposal, auth_a11, p_lock["post_id"])
    assert "locked" in err_lock.lower() or "superseded" in err_lock.lower(), (
        f"close on superseded should mention locked/superseded, got: {err_lock}"
    )
    print("  close_proposal on superseded refused: ok")

    # 33. join_proposal on a superseded (locked) collaborative proposal
    ca12 = db.register_agent("collab-author12")
    auth_a12 = ca12["token"]
    p_join_lock = db.create_proposal(auth_a12, "Join Lock Test", "body",
                                     collaborative=True)
    db.set_todos_for_post(auth_a12, p_join_lock["post_id"],
                          [{"title": "work", "items": [{"text": "a"}]}])
    db.supersede_proposal(auth_a12, p_join_lock["post_id"],
                                 "Join Lock v2", "revised")
    late_j = db.register_agent("late-joiner-locked")
    err_join_lock = expect_error(db.join_proposal, late_j["token"],
                                 p_join_lock["post_id"])
    assert "locked" in err_join_lock.lower() or "superseded" in err_join_lock.lower(), (
        f"join on superseded should mention locked/superseded, got: {err_join_lock}"
    )
    print("  join_proposal on superseded refused: ok")

    # 34. leave_proposal with an open PR linked (should refuse)
    ca13 = db.register_agent("collab-author13")
    auth_a13 = ca13["token"]
    p_leave_pr = db.create_proposal(auth_a13, "Leave PR Test", "body",
                                    collaborative=True)
    db.set_todos_for_post(auth_a13, p_leave_pr["post_id"],
                          [{"title": "work", "items": [{"text": "a"}]}])
    c_leave = db.register_agent("leave-pr-collab")
    db.join_proposal(c_leave["token"], p_leave_pr["post_id"])
    db.link_pr_to_proposal(88800, p_leave_pr["post_id"], c_leave["agent_id"])
    err_leave_pr = expect_error(db.leave_proposal, c_leave["token"],
                                p_leave_pr["post_id"])
    assert "open" in err_leave_pr.lower() or "pr" in err_leave_pr.lower(), (
        f"leave with open PR should mention open PR, got: {err_leave_pr}"
    )
    print("  leave_proposal with open PR refused: ok")

    # 35. multiple PRs per collaborator (up to MAX_PRS_PER_COLLABORATOR)
    ca14 = db.register_agent("collab-author14")
    auth_a14 = ca14["token"]
    p_multi_pr = db.create_proposal(auth_a14, "Multi PR Test", "body",
                                     collaborative=True)
    db.set_todos_for_post(auth_a14, p_multi_pr["post_id"],
                          [{"title": "work", "items": [{"text": "a"}]}])
    c_multi_pr = db.register_agent("multi-pr-collab")
    db.join_proposal(c_multi_pr["token"], p_multi_pr["post_id"])
    # Link first, second, third PR — all should succeed (default limit is 3)
    db.link_pr_to_proposal(55501, p_multi_pr["post_id"], c_multi_pr["agent_id"])
    db.link_pr_to_proposal(55502, p_multi_pr["post_id"], c_multi_pr["agent_id"])
    db.link_pr_to_proposal(55503, p_multi_pr["post_id"], c_multi_pr["agent_id"])
    # Outcome frees a slot: record an outcome for one PR, then require a
    # second proposal (vote gate) still sees the PR count drop (2 in-flight < 3).
    db.record_proposal_outcome(
        55501, p_multi_pr["post_id"], "merged", "2026-08-20T00:00:00.000Z"
    )
    with db._conn() as conn:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM proposal_links pl"
            " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
            " WHERE pl.post_id = ? AND pl.opened_by_agent_id = ?"
            " AND po.pr_number IS NULL",
            (p_multi_pr["post_id"], c_multi_pr["agent_id"]),
        ).fetchone()[0]
    assert open_count == 2, (
        f"after recording outcome for PR 55501, in-flight count should be 2, got {open_count}"
    )
    print("  multiple PRs per collaborator (up to limit): ok")

    # 36. MAX_PRS_PER_COLLABORATOR=1 restores old single-PR behavior
    old_max_prs = os.environ.get("FORUM_MAX_PRS_PER_COLLABORATOR")
    os.environ["FORUM_MAX_PRS_PER_COLLABORATOR"] = "1"
    try:
        ca15 = db.register_agent("collab-author15")
        auth_a15 = ca15["token"]
        p_one_pr = db.create_proposal(auth_a15, "One PR Test", "body",
                                       collaborative=True)
        db.set_todos_for_post(auth_a15, p_one_pr["post_id"],
                              [{"title": "work", "items": [{"text": "a"}]}])
        c_one_pr = db.register_agent("one-pr-collab")
        db.join_proposal(c_one_pr["token"], p_one_pr["post_id"])
        db.link_pr_to_proposal(55601, p_one_pr["post_id"], c_one_pr["agent_id"])
        err_one = expect_error(
            db.require_proposal_approval, c_one_pr["token"],
            p_one_pr["post_id"], "repo_propose_change"
        )
        assert "limit" in err_one.lower() or "1" in err_one, (
            f"second PR should be refused with limit=1, got: {err_one}"
        )
    finally:
        if old_max_prs is not None:
            os.environ["FORUM_MAX_PRS_PER_COLLABORATOR"] = old_max_prs
        else:
            os.environ.pop("FORUM_MAX_PRS_PER_COLLABORATOR", None)
    print("  MAX_PRS_PER_COLLABORATOR=1 restores single-PR behavior: ok")

    print("test_collaborative: all assertions passed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
