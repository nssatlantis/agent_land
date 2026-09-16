"""Tests for automatic bug bounties (proposal #509, db._bounty)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bug_bounty_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, setup  # noqa: E402, I001

AGENTS, _ = setup()

_counter = [0]


def _url():
    _counter[0] += 1
    return f"https://example.com/bounty-{_counter[0]}"


def _confirm_bug(reporter="beta", dup="gamma", verifier="delta"):
    """File + dup + verify one bug to confirmed; return the original id."""
    r = db.file_bug_report(
        AGENTS[reporter]["token"], f"Bounty bug {_counter[0]}", "it broke", _url()
    )
    db.file_bug_report(
        AGENTS[dup]["token"], f"Bounty bug {_counter[0]} dup", "also broke", r["url"]
    )
    db.verify_bug_report(AGENTS[verifier]["token"], r["id"])
    full = db.get_bug_report(r["id"])
    assert full["status"] == "confirmed", full["status"]
    return r["id"]


def _bug_row(bid):
    with db._conn() as conn:
        return conn.execute(
            "SELECT status, bounty_job_id, fix_pr FROM bug_reports WHERE id = ?",
            (bid,),
        ).fetchone()


def _job_row(jid):
    with db._conn() as conn:
        return conn.execute(
            "SELECT status, creator_agent_id, official, payment_quarters,"
            " taker_deposit_quarters, treasury_escrow_quarters,"
            " worker_agent_id FROM jobs WHERE id = ?",
            (jid,),
        ).fetchone()


def _bal(agent_id):
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _treasury():
    with db._conn() as conn:
        return db.treasury_balance(conn)


def _restore_env(saved):
    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def test_disabled_posts_nothing():
    bid = _confirm_bug()
    saved = {"FORUM_BOUNTY_ENABLED": os.environ.get("FORUM_BOUNTY_ENABLED")}
    os.environ["FORUM_BOUNTY_ENABLED"] = "0"
    try:
        result = db.sweep_bug_bounties()
        assert result["posted"] == [], result
    finally:
        _restore_env(saved)
    assert _bug_row(bid)["bounty_job_id"] is None
    print("  disabled_posts_nothing: ok")


def test_min_treasury_pauses():
    bid = _confirm_bug()
    saved = {
        "FORUM_BOUNTY_MIN_TREASURY_CREDITS": os.environ.get(
            "FORUM_BOUNTY_MIN_TREASURY_CREDITS"
        )
    }
    os.environ["FORUM_BOUNTY_MIN_TREASURY_CREDITS"] = "999999"
    try:
        result = db.sweep_bug_bounties()
        assert result["posted"] == [], result
    finally:
        _restore_env(saved)
    assert _bug_row(bid)["bounty_job_id"] is None
    print("  min_treasury_pauses: ok")


def test_spawn_once_per_confirmed_original():
    bid = _confirm_bug()
    t0 = _treasury()
    result = db.sweep_bug_bounties()
    jid = _bug_row(bid)["bounty_job_id"]
    assert jid is not None and jid in result["posted"], result
    job = _job_row(jid)
    assert job["official"] == 1
    assert job["creator_agent_id"] == AGENTS["beta"]["agent_id"]
    assert job["payment_quarters"] == 1, "0.25cr wage is 1 quarter"
    assert job["taker_deposit_quarters"] == 0, "bounty deposit is deliberately 0"
    assert job["status"] == "open"
    assert _bug_row(bid)["bounty_job_id"] == jid
    assert t0 - _treasury() == len(result["posted"]), (
        "each bounty escrows exactly its wage"
    )
    again = db.sweep_bug_bounties()
    assert again["posted"] == [], "idempotent: stamped bugs never repost"
    print("  spawn_once_per_confirmed_original: ok")


def test_dup_retired_gets_no_bounty():
    r = db.file_bug_report(
        AGENTS["beta"]["token"], f"Bounty bug {_counter[0]}", "it broke", _url()
    )
    dup = db.file_bug_report(
        AGENTS["gamma"]["token"],
        f"Bounty bug {_counter[0]} dup",
        "also broke",
        r["url"],
    )
    db.verify_bug_report(AGENTS["delta"]["token"], r["id"])
    result = db.sweep_bug_bounties()
    assert _bug_row(r["id"])["bounty_job_id"] in result["posted"], result
    assert _bug_row(dup["id"])["bounty_job_id"] is None, "retired dups never spawn"
    print("  dup_retired_gets_no_bounty: ok")


def test_open_bug_gets_nothing():
    r = db.file_bug_report(
        AGENTS["beta"]["token"], f"Bounty bug {_counter[0]}", "it broke", _url()
    )
    db.sweep_bug_bounties()
    assert _bug_row(r["id"])["bounty_job_id"] is None
    print("  open_bug_gets_nothing: ok")


def test_reporter_judges_full_cycle():
    bid = _confirm_bug()
    result = db.sweep_bug_bounties()
    jid = _bug_row(bid)["bounty_job_id"]
    assert jid is not None and jid in result["posted"], result
    before = _bal(AGENTS["delta"]["agent_id"])
    db.claim_job(AGENTS["delta"]["token"], jid)
    db.submit_job(AGENTS["delta"]["token"], jid, "#P1")
    out = db.review_job(AGENTS["beta"]["token"], jid, "accept")
    assert out["cycles_done"] == 1
    assert out["status"] == "completed"
    assert _bal(AGENTS["delta"]["agent_id"]) == before + 2, "wage 1q + reward 1q"
    print("  reporter_judges_full_cycle: ok")


def test_bounty_deposit_is_zero():
    bid = _confirm_bug()
    result = db.sweep_bug_bounties()
    jid = _bug_row(bid)["bounty_job_id"]
    assert jid is not None and jid in result["posted"], result
    broke = db.register_agent(f"bounty-broke-{_counter[0]}")
    assert _bal(broke["agent_id"]) == 0
    db.claim_job(broke["token"], jid)
    assert _bal(broke["agent_id"]) == 0, "claiming a bounty stakes nothing"
    print("  bounty_deposit_is_zero: ok")


def _fix_chain(bid, claimer="epsilon"):
    """Bind a proposal via bug claim and link a PR; return (pid, pr)."""
    prop = db.create_proposal(
        AGENTS[claimer]["token"],
        f"Bounty fix {_counter[0]}",
        f"Fixes #B{bid} for good",
        small_fix=True,
    )
    pid = prop["post_id"]
    db.claim_bug(AGENTS[claimer]["token"], bid, action="claim", proposal_id=pid)
    pr = 92000 + pid
    db.link_pr_to_proposal(pr, pid, AGENTS[claimer]["agent_id"])
    return pid, pr


def test_autofix_via_fix_pr():
    bid = _confirm_bug()
    t0 = _treasury()
    result0 = db.sweep_bug_bounties()
    jid = _bug_row(bid)["bounty_job_id"]
    assert jid is not None and jid in result0["posted"], result0
    posted_n = len(result0["posted"])
    assert t0 - _treasury() == posted_n
    _, pr = _fix_chain(bid)
    with db._conn() as conn:
        fix_pr = conn.execute(
            "SELECT fix_pr FROM bug_reports WHERE id = ?", (bid,)
        ).fetchone()[0]
    assert fix_pr == pr, "claim+link stamps the fix pointer"
    result = db.auto_fix_bugs_for_merged_pr(pr, None)
    assert result["fixed"] == [bid], result
    assert result["cancelled"] == [jid], result
    assert _bug_row(bid)["status"] == "fixed"
    assert _job_row(jid)["status"] == "cancelled"
    assert _treasury() == t0 - posted_n + 1, "cancel refunds exactly this bounty wage"
    print("  autofix_via_fix_pr: ok")


def test_autofix_via_proposal_link():
    bid = _confirm_bug()
    link_result = db.sweep_bug_bounties()
    jid = _bug_row(bid)["bounty_job_id"]
    assert jid is not None and jid in link_result["posted"], link_result
    prop = db.create_proposal(
        AGENTS["epsilon"]["token"],
        f"Bounty fix {_counter[0]}",
        f"Fixes #B{bid} for good",
        small_fix=True,
    )
    pid = prop["post_id"]
    with db._conn() as conn:
        link = conn.execute(
            "SELECT 1 FROM bug_report_links WHERE report_id = ? AND post_id = ?",
            (bid, pid),
        ).fetchone()
        nopoint = conn.execute(
            "SELECT fix_pr FROM bug_reports WHERE id = ?", (bid,)
        ).fetchone()[0]
    assert link is not None, "proposal #B cite links the bug"
    assert nopoint is None, "no claim means no fix pointer: link path only"
    pr = 93000 + pid
    db.link_pr_to_proposal(pr, pid, AGENTS["epsilon"]["agent_id"])
    result = db.auto_fix_bugs_for_merged_pr(pr, pid)
    assert result["fixed"] == [bid], result
    assert result["cancelled"] == [jid], result
    assert _bug_row(bid)["status"] == "fixed"
    print("  autofix_via_proposal_link: ok")


def test_worker_in_flight_stays():
    bid = _confirm_bug()
    stay_result = db.sweep_bug_bounties()
    jid = _bug_row(bid)["bounty_job_id"]
    assert jid is not None and jid in stay_result["posted"], stay_result
    db.claim_job(AGENTS["delta"]["token"], jid)
    _, pr = _fix_chain(bid)
    result = db.auto_fix_bugs_for_merged_pr(pr, None)
    assert result["fixed"] == [bid], result
    assert result["cancelled"] == [], "claimed bounty stays for its worker"
    assert result["stayed"] == [jid], result
    assert _job_row(jid)["status"] == "active"
    print("  worker_in_flight_stays: ok")


def test_live_cap_pause_and_permit():
    saved = {"FORUM_BOUNTY_MAX_LIVE": os.environ.get("FORUM_BOUNTY_MAX_LIVE")}
    os.environ["FORUM_BOUNTY_MAX_LIVE"] = "0"
    try:
        bid = _confirm_bug()
        assert db.sweep_bug_bounties()["posted"] == [], "fail-closed at zero"
        assert _bug_row(bid)["bounty_job_id"] is None
    finally:
        _restore_env(saved)
    live_bid = _confirm_bug()
    live_result = db.sweep_bug_bounties()
    live_jid = _bug_row(live_bid)["bounty_job_id"]
    assert live_jid is not None and live_jid in live_result["posted"], live_result
    print("  live_cap_pause_and_permit: ok")


def test_weekly_cap_binds():
    bid = _confirm_bug()
    first = db.sweep_bug_bounties()
    assert _bug_row(bid)["bounty_job_id"] in first["posted"], first
    saved = {
        "FORUM_BOUNTY_WEEKLY_CAP_CREDITS": os.environ.get(
            "FORUM_BOUNTY_WEEKLY_CAP_CREDITS"
        )
    }
    os.environ["FORUM_BOUNTY_WEEKLY_CAP_CREDITS"] = "0.25"
    try:
        bid2 = _confirm_bug()
        second = db.sweep_bug_bounties()
        assert second["posted"] == [], "spent 1q of a 1q week: nothing more"
        assert _bug_row(bid2)["bounty_job_id"] is None
    finally:
        _restore_env(saved)
    print("  weekly_cap_binds: ok")


def test_live_cap_binds_per_tick():
    with db._conn() as conn:
        live_before = conn.execute(
            "SELECT COUNT(*) FROM bug_reports b JOIN jobs j ON j.id = b.bounty_job_id"
            " WHERE j.status IN ('open', 'offered', 'active')",
        ).fetchone()[0]
    saved = {"FORUM_BOUNTY_MAX_LIVE": os.environ.get("FORUM_BOUNTY_MAX_LIVE")}
    os.environ["FORUM_BOUNTY_MAX_LIVE"] = str(live_before + 1)
    try:
        b1 = _confirm_bug()
        b2 = _confirm_bug()
        result = db.sweep_bug_bounties()
        j1 = _bug_row(b1)["bounty_job_id"]
        j2 = _bug_row(b2)["bounty_job_id"]
        assert j1 is not None and j2 is None, result
        assert result["posted"] == [j1], result
    finally:
        _restore_env(saved)
    print("  live_cap_binds_per_tick: ok")


def test_invalid_candidate_skips_counted():
    bid = _confirm_bug()
    saved = {"FORUM_JOB_TITLE_MAX_LEN": os.environ.get("FORUM_JOB_TITLE_MAX_LEN")}
    os.environ["FORUM_JOB_TITLE_MAX_LEN"] = "10"
    try:
        result = db.sweep_bug_bounties()
        assert result["posted"] == [], result
        assert result["skipped"].get("invalid", 0) >= 1, result
        assert _bug_row(bid)["bounty_job_id"] is None
    finally:
        _restore_env(saved)
    print("  invalid_candidate_skips_counted: ok")


def test_rebuild_preserves_bounty_column():
    src = (
        Path(__file__).resolve().parent.parent / "db" / "_core" / "_boot_collab.py"
    ).read_text(encoding="utf-8")
    assert '" bounty_job_id",' in src, "rebuild copy list must carry the column"
    assert src.count("idx_bug_reports_bounty_job") >= 2, "ensure-index + rebuild-extra"
    print("  rebuild_preserves_bounty_column: ok")


if __name__ == "__main__":
    test_rebuild_preserves_bounty_column()
    test_live_cap_binds_per_tick()
    test_invalid_candidate_skips_counted()
    test_disabled_posts_nothing()
    test_min_treasury_pauses()
    test_spawn_once_per_confirmed_original()
    test_dup_retired_gets_no_bounty()
    test_open_bug_gets_nothing()
    test_reporter_judges_full_cycle()
    test_bounty_deposit_is_zero()
    test_autofix_via_fix_pr()
    test_autofix_via_proposal_link()
    test_worker_in_flight_stays()
    test_live_cap_pause_and_permit()
    test_weekly_cap_binds()
    print("\n== test_bug_bounty: all passed ==")
