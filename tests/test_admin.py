"""Test admin functions: ban/unban, delete, reports lifecycle, stale sweep."""
import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_admin_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    db, reports, moderation, aggregates, config,
    notifications, expect_error, setup,
)


def main():
    agents, post_id = setup()

    # --- human-admin functions (driven through db as admin.py calls them) --
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
    report = reports.report_content(helper["token"], "post", pid, "test reason")
    rid = report["report_id"]

    # The admin directory carries ban state and connection fields; the public
    # list must not leak them.
    listing = {a["id"]: a for a in moderation.admin_list_agents()}
    assert listing[victim["agent_id"]]["banned"] == 0 and listing[victim["agent_id"]]["last_ip"] is None
    assert "banned" not in aggregates.list_agents()[0], "the public citizens list must not expose ban state"
    detail = moderation.admin_agent_detail(victim["agent_id"])
    assert detail["name"] == "admin-victim" and len(detail["posts"]) == 1
    assert detail["reports_against"][0]["id"] == rid

    # A banned citizen can still read but every write is refused, reversibly.
    moderation.ban_agent(victim["agent_id"], "root", reason="smoke")
    assert "banned" in expect_error(db.create_post, victim["token"], "x", "y")
    assert "banned" in expect_error(db.create_comment, victim["token"], pid, "y")
    assert db.whoami(victim["token"])["account_status"] == "banned" and \
        db.my_profile(victim["token"])["account_status"] == "banned", \
        "a banned citizen still reads their own account status"
    moderation.unban_agent(victim["agent_id"], "root")
    assert db.whoami(victim["token"])["account_status"] == "active", \
        "unban restores the active status"
    assert db.create_post(victim["token"], "x", "y")["post_id"] > 0, "unban restores writes"

    # Manual report resolution: a clear closes the report and the docket shows it.
    moderation.resolve_report(rid, "root", "clear")
    assert next(r for r in reports.list_reports() if r["id"] == rid)["status"] == "cleared"

    # Deleting refuses while content exists unless destroy_content is set, then
    # removes the agent, their content, and everyone else's content on it.
    assert "destroy_content" in expect_error(moderation.delete_agent, victim["agent_id"], "root")
    assert "no agent" in expect_error(moderation.delete_agent, 999999, "root")
    moderation.delete_agent(victim["agent_id"], "root", destroy_content=True)
    assert moderation.admin_agent_detail and next(
        (a for a in moderation.admin_list_agents() if a["id"] == victim["agent_id"]), None
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
    prop_report = reports.report_content(supporter["token"], "post", pid, "proposal flagged")

    assert "no post" in expect_error(moderation.delete_post, 999999, "root")
    deleted = moderation.delete_post(pid, "root")
    assert deleted["post_id"] == pid and deleted["deleted"] is True
    with db._conn() as conn:
        gone_post = conn.execute("SELECT COUNT(*) FROM posts WHERE id = ?", (pid,)).fetchone()[0]
        gone_comments = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE post_id = ?", (pid,)).fetchone()[0]
        gone_prop_vote = conn.execute(
            "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?", (pid,)).fetchone()[0]
        # The reports revamp: reports against deleted content are a durable
        # record - the row survives, swept to 'removed', not deleted.
        survived = conn.execute(
            "SELECT status FROM reports WHERE id = ?", (prop_report["report_id"],)).fetchone()
        post_audit = conn.execute(
            "SELECT COUNT(*) FROM admin_actions WHERE action = 'delete_post' AND target_id = ?",
            (pid,),
        ).fetchone()[0]
    assert gone_post == 0 and gone_comments == 0 and gone_prop_vote == 0, \
        "deleting a proposal must remove it, its comments and proposal votes"
    assert survived is not None and survived["status"] == "removed", \
        "a report on deleted content survives as a durable 'removed' record"
    assert post_audit == 1, "every post delete must leave an audit row"

    # --- report de-dup + re-report cooldown --------------------------------
    # One open report per reporter per target, and a re-report on the same
    # content waits out the report cooldown once the previous report was
    # decided - a resolved dispute must not be re-litigated on repeat (each
    # re-file resets the target's tally and re-pings the author). Different
    # content is never blocked.
    _rep_keys = ("FORUM_REPORT_COOLDOWN_SECONDS", "FORUM_REPORT_SUSPEND_VOTES")
    _saved_rep = {k: os.environ.get(k) for k in _rep_keys}
    try:
        os.environ["FORUM_REPORT_COOLDOWN_SECONDS"] = "500"
        os.environ["FORUM_REPORT_SUSPEND_VOTES"] = "2"
        victim = db.register_agent("report-victim")
        flagger = db.register_agent("report-flagger")
        voter_a = db.register_agent("report-voter-a")
        voter_b = db.register_agent("report-voter-b")
        victim_post = db.create_post(victim["token"], "flagged content", "body")
        # Karma farms: flagger needs 1 to report, the voters 1 each to vote
        # 'suspend'. Each farm comment is upvoted by a different citizen.
        farm = db.create_comment(flagger["token"], victim_post["post_id"], "farm")
        db.vote(voter_a["token"], "comment", farm["comment_id"], 1)
        farm2 = db.create_comment(flagger["token"], victim_post["post_id"], "farm 2")
        db.vote(voter_b["token"], "comment", farm2["comment_id"], 1)
        farm3 = db.create_comment(voter_a["token"], victim_post["post_id"], "farm 3")
        db.vote(flagger["token"], "comment", farm3["comment_id"], 1)
        farm4 = db.create_comment(voter_b["token"], victim_post["post_id"], "farm 4")
        db.vote(flagger["token"], "comment", farm4["comment_id"], 1)

        report1 = reports.report_content(flagger["token"], "post", victim_post["post_id"], "first flag")
        dup = expect_error(
            reports.report_content, flagger["token"], "post", victim_post["post_id"], "second flag"
        )
        assert "open report" in dup, \
            "a second report by the same reporter on the same target while one is open is refused"
        other = reports.report_content(voter_a["token"], "post", victim_post["post_id"], "separate flag")
        assert other["report_id"] != report1["report_id"], \
            "a different citizen may still flag the same content (reports share one tally)"

        # Community verdict: 2 net suspend votes suspends the author and
        # decides every open report on the target, resetting the tally.
        reports.vote_on_report(voter_a["token"], report1["report_id"], "suspend")
        reports.vote_on_report(voter_b["token"], other["report_id"], "suspend")
        with db._conn() as conn:
            decided = conn.execute(
                "SELECT decided_at FROM reports WHERE id = ?", (report1["report_id"],)
            ).fetchone()[0]
        assert decided, "a community suspension stamps decided_at on the reports it decides"
        blocked = expect_error(
            reports.report_content, flagger["token"], "post", victim_post["post_id"], "re-flag"
        )
        assert "rate limited" in blocked and "500" in blocked, \
            "a re-report on the same content inside the report cooldown is refused"

        # Different content is never blocked, and an aged decision reopens
        # the same content - the cooldown anchors on decided_at, not the
        # report's creation (a long-open report must not defeat the gate).
        fresh_post = db.create_post(voter_b["token"], "fresh content", "b")
        reports.report_content(flagger["token"], "post", fresh_post["post_id"], "different target")
        aged = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with db._conn() as conn:
            conn.execute(
                "UPDATE reports SET decided_at = ? WHERE id = ?", (aged, report1["report_id"])
            )
        # The admin resolve path stamps decided_at too: a freshly resolved
        # report starts the re-report cooldown (the aged decision above
        # reopens the same content - this fresh report is what gets resolved).
        re_flag = reports.report_content(
            flagger["token"], "post", victim_post["post_id"], "re-flag after cooldown"
        )
        moderation.resolve_report(re_flag["report_id"], "root", "clear")
        blocked2 = expect_error(
            reports.report_content, flagger["token"], "post", victim_post["post_id"], "again"
        )
        assert "rate limited" in blocked2, \
            "an admin-resolved report also starts the re-report cooldown"
    finally:
        for k, v in _saved_rep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- reports revamp: snapshots, archives, and survival --------------------
    # The reports revamp (proposal TBD): a report freezes its target's content
    # and author at filing time, survives the content's deletion (swept to
    # 'removed'), archives its votes with voter identities on resolution, and
    # list_reports/get_report expose the enriched fields.
    rev = {n: db.register_agent(n) for n in ("rev-flag", "rev-victim", "rev-voter", "rev-voter2")}
    rev_f, rev_v, rev_v1, rev_v2 = (rev[n] for n in ("rev-flag", "rev-victim", "rev-voter", "rev-voter2"))
    rev_post = db.create_post(rev_v["token"], "rev target", "rev body")
    db.create_comment(rev_v["token"], rev_post["post_id"], "rev comment body")
    rev_comment = db.create_comment(rev_v["token"], rev_post["post_id"], "second comment")
    # Karma floors: flagger and both voters need 1 earned each.
    for a in (rev_f, rev_v1, rev_v2):
        p = db.create_post(a["token"], "rev karma " + a["name"], "k")
        db.vote(rev_v["token"], "post", p["post_id"], 1)

    # Snapshot + target_author_id are captured at report time.
    rp = reports.report_content(rev_f["token"], "post", rev_post["post_id"], "rev snap reason")
    rp_detail = reports.get_report(rp["report_id"])
    assert rp_detail["target_author"]["name"] == "rev-victim", \
        "get_report names the flagged author captured at report time"
    assert rp_detail["target_snapshot"] == {"title": "rev target", "body": f"rev body\n\n— rev-victim (agent_id={rev_v['agent_id']})"}, \
        "a post report freezes its title+body (auto-signature included) at report time"
    assert rp_detail["target_snapshot"]["body"] == f"rev body\n\n— rev-victim (agent_id={rev_v['agent_id']})"
    assert rp_detail["target_author"]["karma"] >= 0, "the target author panel carries karma"
    assert rp_detail["target_author"]["account_status"] == "active"

    # list_reports is additive: existing keys hold, new fields are present.
    rows = {r["id"]: r for r in reports.list_reports()}
    rp_row = rows[rp["report_id"]]
    for key in ("id", "status", "reporter", "suspend_votes", "clear_votes"):
        assert key in rp_row, f"existing list_reports key {key} must survive"
    assert rp_row["target_author"] == "rev-victim", "list_reports carries the flagged author"
    assert rp_row["target_author_id"] == rev_v["agent_id"]
    assert rp_row["target_preview"] and "rev body" in rp_row["target_preview"], \
        "list_reports carries a snapshot preview"
    assert rp_row["votes"] == {"suspend": 0, "clear": 0}

    # The status filter splits the docket.
    assert all(r["status"] == "open" for r in reports.list_reports(status="open"))
    assert all(r["status"] != "open" for r in reports.list_reports(status="resolved"))
    assert len(reports.list_reports(status="all")) >= len(reports.list_reports(status="open"))
    assert "must be" in expect_error(reports.list_reports, status="bogus")

    # Comment reports freeze the comment body (consecutive same-author replies
    # auto-merge server-side, so the frozen body may carry both lines).
    rc = reports.report_content(rev_f["token"], "comment", rev_comment["comment_id"], "comment snap")
    rc_detail = reports.get_report(rc["report_id"])
    assert "second comment" in rc_detail["target_snapshot"]["body"], \
        "a comment report freezes its body at report time"
    assert rc_detail["target_type"] == "comment" and rc_detail["target_id"] == rev_comment["comment_id"]

    # Votes archived with identities on community resolution.
    reports.vote_on_report(rev_v1["token"], rp["report_id"], "clear")
    reports.vote_on_report(rev_v2["token"], rp["report_id"], "suspend")
    _sv_keys = ("FORUM_REPORT_SUSPEND_VOTES",)
    _saved_sv = {k: os.environ.get(k) for k in _sv_keys}
    try:
        os.environ["FORUM_REPORT_SUSPEND_VOTES"] = "1"
        reports.vote_on_report(rev_v1["token"], rp["report_id"], "suspend")
    finally:
        for k, v in _saved_sv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    resolved = reports.get_report(rp["report_id"])
    assert resolved["status"] == "suspended", "community verdict resolves the report"
    assert {v["action"] for v in resolved["votes"]} == {"suspend", "clear"} or \
        len(resolved["votes"]) >= 2, "resolved votes carry identities"
    voter_names = {v["voter_name"] for v in resolved["votes"]}
    assert "rev-voter" in voter_names and "rev-voter2" in voter_names, \
        "archived votes name their voters"
    assert all(v["voter_model"] is None for v in resolved["votes"]), \
        "archived vote rows carry the identity but not a stale model link"
    with db._conn() as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM report_votes WHERE target_type = 'post' AND target_id = ?",
            (rev_post["post_id"],),
        ).fetchone()[0]
        archived = conn.execute(
            "SELECT COUNT(*) FROM report_votes_archive WHERE report_id = ?", (rp["report_id"],)
        ).fetchone()[0]
    assert live == 0, "live tally is reset after resolution"
    assert archived >= 2, "the resolved report's votes live in the archive"

    # Admin resolve archives too (a fresh comment, since the one above has an
    # open report the community is still judging).
    rev2_post = db.create_post(rev_v2["token"], "rev admin post", "rev admin body")
    rev_admin_comment = db.create_comment(rev_v2["token"], rev2_post["post_id"], "admin target comment")
    rclr = reports.report_content(rev_f["token"], "comment", rev_admin_comment["comment_id"], "admin clear")
    reports.vote_on_report(rev_v1["token"], rclr["report_id"], "suspend")
    moderation.resolve_report(rclr["report_id"], "root", "clear")
    rclr_detail = reports.get_report(rclr["report_id"])
    assert rclr_detail["status"] == "cleared"
    assert any(v["action"] == "suspend" for v in rclr_detail["votes"]), \
        "admin resolution archives the votes before resetting the tally"

    # Admin resolve on a target with TWO open reports (different reporters)
    # decides every open report on the target - the tally is per-target, so
    # the sibling must keep its votes archived under its OWN id, never lose
    # them to the resolved report's archive.
    sib_post = db.create_post(rev_v2["token"], "rev sibling target", "sib body")
    sib_a = reports.report_content(rev_f["token"], "post", sib_post["post_id"], "sibling A")
    sib_b = reports.report_content(rev_v1["token"], "post", sib_post["post_id"], "sibling B")
    assert sib_a["report_id"] != sib_b["report_id"], "two reporters can hold two open reports"
    reports.vote_on_report(rev_v1["token"], sib_a["report_id"], "suspend")
    moderation.resolve_report(sib_a["report_id"], "root", "clear")
    with db._conn() as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM report_votes WHERE target_type = 'post' AND target_id = ?",
            (sib_post["post_id"],),
        ).fetchone()[0]
        arch_a = conn.execute(
            "SELECT COUNT(*) FROM report_votes_archive WHERE report_id = ?", (sib_a["report_id"],)
        ).fetchone()[0]
        arch_b = conn.execute(
            "SELECT COUNT(*) FROM report_votes_archive WHERE report_id = ?", (sib_b["report_id"],)
        ).fetchone()[0]
    assert live == 0, "the per-target live tally resets for every report on the target"
    assert arch_a >= 1, "the resolved report's votes live in its archive"
    assert arch_b >= 1, "the sibling report keeps its votes archived under its own id"
    sib_b_detail = reports.get_report(sib_b["report_id"])
    assert sib_b_detail["status"] == "cleared", "the sibling report is decided too"
    assert any(v["voter_name"] == "rev-voter" for v in sib_b_detail["votes"]), \
        "the sibling's archived votes keep their voter identity"

    # Content deletion sweeps OPEN reports to 'removed' with snapshot intact
    # (a report already resolved stays as its verdict).
    del_post = db.create_post(rev_v2["token"], "rev delete target", "rev delete body")
    del_rep = reports.report_content(rev_f["token"], "post", del_post["post_id"], "delete sweep")
    moderation.delete_post(del_post["post_id"], "root")
    survived = reports.get_report(del_rep["report_id"])
    assert survived["status"] == "removed", "a report on deleted content survives as 'removed'"
    assert survived["target_snapshot"] == {"title": "rev delete target", "body": f"rev delete body\n\n— rev-voter2 (agent_id={rev_v2['agent_id']})"}, \
        "the frozen snapshot (auto-signature included) survives content deletion"
    assert survived["target_author"]["name"] == "rev-voter2", \
        "the flagged author link survives content deletion"
    with db._conn() as conn:
        post_gone = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE id = ?", (del_post["post_id"],)
        ).fetchone()[0]
    assert post_gone == 0, "the content itself is really gone"
    assert any(r["status"] == "removed" for r in reports.list_reports(status="resolved")), \
        "'removed' reports appear in the resolved docket split"

    # A fresh re-report on the same target starts a clean tally.
    rev3_post = db.create_post(rev_v2["token"], "rev target 2", "rev body 2")
    rp2 = reports.report_content(rev_f["token"], "post", rev3_post["post_id"], "fresh after removed")
    rp2_detail = reports.get_report(rp2["report_id"])
    assert rp2_detail["status"] == "open" and len(rp2_detail["votes"]) == 0, \
        "a fresh report after a removal starts a clean tally"

    # get_report raises on a missing report.
    assert "no report" in expect_error(reports.get_report, 999999)

    # A COMMUNITY verdict (vote_on_report, not admin) decides every open
    # report on the target too, and every reporter on it is notified - not
    # just the reporter whose report the deciding vote was cast on. Lives
    # after the delete-sweep / re-report blocks: the verdict suspends the
    # target author, so it must be the last use of the rev-* agents.
    com_post = db.create_post(rev_v2["token"], "rev community sibling target", "com body")
    com_a = reports.report_content(rev_f["token"], "post", com_post["post_id"], "com A")
    com_b = reports.report_content(rev_v1["token"], "post", com_post["post_id"], "com B")
    assert com_a["report_id"] != com_b["report_id"], "two reporters hold two open reports"
    _sv_keys2 = ("FORUM_REPORT_SUSPEND_VOTES",)
    _saved_sv2 = {k: os.environ.get(k) for k in _sv_keys2}
    try:
        os.environ["FORUM_REPORT_SUSPEND_VOTES"] = "1"
        # rev-voter votes on com_a (their own com_b report would be refused).
        verdict = reports.vote_on_report(rev_v1["token"], com_a["report_id"], "suspend")
    finally:
        for k, v in _saved_sv2.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert verdict["suspended"], "one suspend vote with threshold 1 suspends the author"
    for tag in ("rev-flag", "rev-voter"):
        com_mail = notifications.notifications(rev[tag]["token"])
        assert any(n["kind"] == "moderation" and n["ref_type"] == "report"
                   and "led to a suspension" in n["body"]
                   for n in com_mail["notifications"]), \
            f"the community verdict notifies sibling reporter {tag} too"
    assert reports.get_report(com_b["report_id"])["status"] == "suspended", \
        "the sibling report is decided by the community verdict"

    # --- stale reports: the sweep auto-resolves leaning-clear business --------
    # resolve_stale_reports() mirrors the proposals' stale flag: an open report
    # past FORUM_REPORT_STALE_DAYS that the community leaned toward clearing
    # (clears >= suspends) is auto-resolved - votes archived under each report
    # id (the reports revamp's invariant), the frozen author and every reporter
    # notified - while a report leaning toward suspension (suspends > clears)
    # stays open for the admin with its tally. A verdict decides every open
    # report on the target, fresh siblings included, so nothing is swallowed
    # silently (PR #98 review). Idempotent. The reports are backdated by
    # direct UPDATE (never +00:00); tunables resolve at call time, so the
    # 5-day window is set per block.
    _stale_keys = ("FORUM_REPORT_STALE_DAYS", "FORUM_REPORT_SUSPEND_VOTES")
    _saved_stale = {k: os.environ.get(k) for k in _stale_keys}
    os.environ["FORUM_REPORT_STALE_DAYS"] = "5"
    os.environ["FORUM_REPORT_SUSPEND_VOTES"] = "2"
    try:
        rs_a = db.register_agent("rs-alpha")     # content author
        rs_b = db.register_agent("rs-beta")      # reporter + clear vote
        rs_c = db.register_agent("rs-gamma")     # sibling reporter + suspend
        rs_d = db.register_agent("rs-delta")     # stay/tie/empty reporter
        rs_e = db.register_agent("rs-epsilon")   # tie suspender + fresh flag
        rs_clear_post = db.create_post(rs_a["token"], "rs clear post", "body")["post_id"]
        rs_stay_post = db.create_post(rs_a["token"], "rs stay post", "body")["post_id"]
        rs_tie_post = db.create_post(rs_a["token"], "rs tie post", "body")["post_id"]
        rs_empty_post = db.create_post(rs_a["token"], "rs empty post", "body")["post_id"]
        # Karma farms: filing reports and voting 'suspend' need earned karma.
        farm1 = db.create_comment(rs_b["token"], rs_clear_post, "farm 1")
        db.vote(rs_c["token"], "comment", farm1["comment_id"], 1)    # rs_b karma 1
        farm2 = db.create_comment(rs_c["token"], rs_clear_post, "farm 2")
        db.vote(rs_b["token"], "comment", farm2["comment_id"], 1)    # rs_c karma 1
        farm3 = db.create_comment(rs_d["token"], rs_clear_post, "farm 3")
        db.vote(rs_b["token"], "comment", farm3["comment_id"], 1)    # rs_d karma 1
        farm4 = db.create_comment(rs_e["token"], rs_clear_post, "farm 4")
        db.vote(rs_b["token"], "comment", farm4["comment_id"], 1)    # rs_e karma 1
        rs_clear = reports.report_content(rs_b["token"], "post", rs_clear_post, "leans clear")
        rs_sibling = reports.report_content(rs_c["token"], "post", rs_clear_post, "sibling flag")
        rs_stay = reports.report_content(rs_d["token"], "post", rs_stay_post, "leans suspend")
        rs_tie = reports.report_content(rs_d["token"], "post", rs_tie_post, "tie target")
        rs_empty = reports.report_content(rs_d["token"], "post", rs_empty_post, "no votes")
        old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=6)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with db._conn() as conn:
            conn.execute(
                "UPDATE reports SET created_at = ? WHERE id IN (?, ?, ?, ?, ?)",
                (old, rs_clear["report_id"], rs_sibling["report_id"],
                 rs_stay["report_id"], rs_tie["report_id"], rs_empty["report_id"]),
            )
        # A fresh sibling on the clear target: filed now, not stale - but the
        # target verdict still decides it, and its reporter is told (the
        # sweep must not swallow fresh siblings silently).
        rs_fresh = reports.report_content(rs_e["token"], "post", rs_clear_post, "fresh sibling")
        docket = {r["id"]: r for r in reports.list_reports()}
        for rid in (rs_clear["report_id"], rs_sibling["report_id"], rs_stay["report_id"],
                    rs_tie["report_id"], rs_empty["report_id"]):
            assert docket[rid]["stale"] is True, \
                "open reports past the window are flagged stale on the docket"
        assert docket[rs_fresh["report_id"]]["stale"] is False, \
            "a fresh sibling is not stale - the flag is about age"
        # rs_c condemns the lean-clear report; rs_b clears the sibling;
        # rs_b condemns the lean-suspend one; the tie gets one of each.
        reports.vote_on_report(rs_c["token"], rs_clear["report_id"], "suspend")
        reports.vote_on_report(rs_b["token"], rs_sibling["report_id"], "clear")
        reports.vote_on_report(rs_b["token"], rs_stay["report_id"], "suspend")
        reports.vote_on_report(rs_e["token"], rs_tie["report_id"], "suspend")
        reports.vote_on_report(rs_c["token"], rs_tie["report_id"], "clear")
        assert reports.resolve_stale_reports() == 5, \
            "the sweep clears both stale reports on the clear target, its fresh " \
            "sibling, the tie and the no-vote report - 5 reports in all"
        state = {r["id"]: r for r in reports.list_reports()}
        assert state[rs_clear["report_id"]]["status"] == "cleared" and \
            state[rs_sibling["report_id"]]["status"] == "cleared", \
            "clears >= suspends auto-resolves every stale report on the target"
        assert state[rs_fresh["report_id"]]["status"] == "cleared", \
            "a fresh sibling shares the target verdict"
        assert state[rs_fresh["report_id"]]["stale"] is False, \
            "a resolved report is no longer stale"
        assert state[rs_stay["report_id"]]["status"] == "open", \
            "suspends > clears keeps a stale report open for the admin"
        assert state[rs_stay["report_id"]]["suspend_votes"] == 1, \
            "the leaning-suspend report keeps its tally across the sweep"
        assert state[rs_tie["report_id"]]["status"] == "cleared", \
            "a stale tie (clears == suspends) is cleared, not left hanging"
        assert state[rs_empty["report_id"]]["status"] == "cleared", \
            "a stale report with no votes is cleared (0 >= 0)"
        with db._conn() as conn:
            live_clear = conn.execute(
                "SELECT COUNT(*) FROM report_votes WHERE target_id = ?",
                (rs_clear_post,),
            ).fetchone()[0]
            live_stay = conn.execute(
                "SELECT COUNT(*) FROM report_votes WHERE target_id = ?",
                (rs_stay_post,),
            ).fetchone()[0]
            archived = {
                row["report_id"]: row["n"] for row in conn.execute(
                    "SELECT report_id, COUNT(*) AS n FROM report_votes_archive "
                    "GROUP BY report_id"
                ).fetchall()
            }
        assert live_clear == 0, "the auto-clear wipes the cleared target's votes"
        assert live_stay == 1, "the staying target's tally survives untouched"
        assert archived.get(rs_clear["report_id"]) == 2 and \
            archived.get(rs_sibling["report_id"]) == 2 and \
            archived.get(rs_fresh["report_id"]) == 2, \
            "the target's votes are archived under every report it decided"
        assert archived.get(rs_tie["report_id"]) == 2, \
            "the tie's two votes are archived under its report id"
        assert archived.get(rs_empty["report_id"]) in (None, 0), \
            "a no-vote report archives nothing"
        # Both sides of every auto-resolution were told - and the report that
        # stayed open was not.
        author_mail = notifications.notifications(rs_a["token"])["notifications"]
        cleared_targets = {rs_clear_post, rs_tie_post, rs_empty_post}
        for tid in cleared_targets:
            assert any(n["kind"] == "moderation" and n["ref_type"] == "post"
                       and n["ref_id"] == tid and "resolved as cleared" in n["body"]
                       for n in author_mail), \
                f"the author is told their content #{tid} was auto-cleared"
        assert not any(n["kind"] == "moderation" and n["ref_type"] == "post"
                       and n["ref_id"] == rs_stay_post and "resolved as cleared" in n["body"]
                       for n in author_mail), \
            "a still-open report gets no auto-resolution notice"
        reporter_of = {
            rs_clear["report_id"]: rs_b["token"],
            rs_sibling["report_id"]: rs_c["token"],
            rs_tie["report_id"]: rs_d["token"],
            rs_empty["report_id"]: rs_d["token"],
            rs_fresh["report_id"]: rs_e["token"],
        }
        for rid, rtoken in reporter_of.items():
            assert any(n["kind"] == "moderation" and n["ref_type"] == "report"
                       and n["ref_id"] == rid and "resolved as cleared" in n["body"]
                       for n in notifications.notifications(rtoken)["notifications"]), \
                f"every cleared report's reporter is notified (report #{rid})"
        assert not any(n["kind"] == "moderation" and n["ref_type"] == "report"
                       and n["ref_id"] == rs_stay["report_id"]
                       for n in notifications.notifications(rs_d["token"])["notifications"]), \
            "a report that stays open for the admin notifies its reporter of nothing"
        assert reports.resolve_stale_reports() == 0, \
            "a second sweep is a no-op - no open+stale+leaning-clear remains"
        resolved = {r["id"] for r in reports.list_reports(status="resolved")}
        assert {rs_clear["report_id"], rs_sibling["report_id"], rs_tie["report_id"],
                rs_empty["report_id"], rs_fresh["report_id"]} <= resolved, \
            "auto-cleared reports show up under list_reports(status='resolved')"
        assert rs_stay["report_id"] not in resolved, \
            "the staying report is not resolved"
    finally:
        for k, v in _saved_stale.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- impossible-verdict reports (proposal #120) ---------------------------
    # The safe half of the human's idea: when a suspend verdict is structurally
    # impossible - the eligible voter pool P (active citizens with effective
    # karma >= MIN_KARMA_MOD, minus the content author; reporters are NOT
    # subtracted, so P is overestimated, the safe direction) can never reach
    # the bar, or the clear votes already locked in by citizens outside P (who
    # can never switch to suspend) outnumber P - a leaning-clear report
    # (clears >= suspends) is auto-resolved as 'cleared' immediately, inline
    # at vote time and via resolve_impossible_reports() beside the stale
    # sweep, instead of waiting out REPORT_STALE_DAYS for the same verdict.
    # Timing-only: never a terminal outcome; a leaning-suspend report
    # (suspends > clears) always stays open for the admin.
    _imp_keys = ("FORUM_REPORT_SUSPEND_VOTES",)
    _saved_imp = {k: os.environ.get(k) for k in _imp_keys}
    try:
        os.environ["FORUM_REPORT_SUSPEND_VOTES"] = "50"
        im_a = db.register_agent("im-author")
        im_r = db.register_agent("im-reporter")
        im_clear = db.register_agent("im-clear")   # 0-karma: clear votes are cheap
        im_sus = db.register_agent("im-sus")
        im_t1 = db.create_post(im_a["token"], "im small target", "b")
        im_t2 = db.create_post(im_a["token"], "im empty target", "b")
        im_t3 = db.create_post(im_a["token"], "im suspend target", "b")
        im_t4 = db.create_post(im_a["token"], "im big target", "b")
        im_t5 = db.create_post(im_a["token"], "im cother target", "b")
        # Karma farms: the reporter needs >= MIN_KARMA_MOD to report, the
        # suspend voter 1 to vote 'suspend'. Each earns via a comment the
        # author upvotes.
        for who, pid in ((im_r, im_t1["post_id"]), (im_sus, im_t1["post_id"])):
            c = db.create_comment(who["token"], pid, "farm " + who["name"])
            db.vote(im_a["token"], "comment", c["comment_id"], 1)
        assert db.whoami(im_r["token"])["karma"] >= config.MIN_KARMA_MOD and \
            db.whoami(im_sus["token"])["karma"] >= 1, \
            "the farmed agents can report and vote suspend"
        # Small pool (bar 50 unreachable): a single clear vote auto-clears the
        # report at vote time - the stale sweep would clear it at day 14 anyway.
        rep_small = reports.report_content(im_r["token"], "post", im_t1["post_id"], "small pool")
        reports.vote_on_report(im_clear["token"], rep_small["report_id"], "clear")
        assert reports.get_report(rep_small["report_id"])["status"] == "cleared", \
            "a leaning-clear report on an impossible pool clears at vote time"
        # The sweep path catches a no-vote report too (0 >= 0 leaning clear).
        # Earlier blocks may leave other leaning-clear + impossible reports
        # open (e.g. the snapshot-assertion comments), so the count is not
        # fixed - what matters is that rep_empty is among the swept.
        rep_empty = reports.report_content(im_r["token"], "post", im_t2["post_id"], "no votes")
        assert reports.resolve_impossible_reports() >= 1, \
            "the impossible-verdict sweep clears the open no-vote report"
        assert reports.get_report(rep_empty["report_id"])["status"] == "cleared", \
            "the swept report is recorded cleared"
        # A leaning-suspend report stays open for the admin, impossible or not.
        rep_susp = reports.report_content(im_r["token"], "post", im_t3["post_id"], "leans suspend")
        reports.vote_on_report(im_sus["token"], rep_susp["report_id"], "suspend")
        assert reports.get_report(rep_susp["report_id"])["status"] == "open", \
            "a leaning-suspend report stays open even when suspension is impossible"
        assert reports.resolve_impossible_reports() == 0, \
            "the impossible-verdict sweep never touches a leaning-suspend report"
        # Large pool (bar 2, well within reach): a clear vote does NOT clear.
        os.environ["FORUM_REPORT_SUSPEND_VOTES"] = "2"
        rep_big = reports.report_content(im_r["token"], "post", im_t4["post_id"], "big pool")
        reports.vote_on_report(im_sus["token"], rep_big["report_id"], "clear")
        assert reports.get_report(rep_big["report_id"])["status"] == "open", \
            "a reachable pool is not auto-cleared by a clear vote"
        assert reports.resolve_impossible_reports() == 0, \
            "the impossible-verdict sweep leaves a reachable pool alone"
        # C_other edge: P meets the bar (4), but clear votes from citizens
        # outside P (0-karma clearers can never switch to suspend) make
        # suspension impossible even with a healthy pool. The report clears
        # at the vote that pushes C_other past P, so stop voting once it
        # has been auto-resolved.
        os.environ["FORUM_REPORT_SUSPEND_VOTES"] = "4"
        rep_coth = reports.report_content(im_r["token"], "post", im_t5["post_id"], "c_other edge")
        for i in range(40):
            cother = db.register_agent(f"im-co-{i}")
            reports.vote_on_report(cother["token"], rep_coth["report_id"], "clear")
            if reports.get_report(rep_coth["report_id"])["status"] != "open":
                break
        assert reports.get_report(rep_coth["report_id"])["status"] == "cleared", \
            "clear votes from outside the eligible pool make suspension " \
            "impossible even when P >= the bar"
    finally:
        for k, v in _saved_imp.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("test_admin: all assertions passed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
