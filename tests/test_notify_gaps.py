"""Regression tests for the notification-gaps bundle (small_fix #348).

One mailbox assertion per fix: transfer recipient, stake payout to
staker, delegate reassignment, poll conclusion to author, boot bug
auto-confirm, proposal verdict fan-out, invoice nudge on whoami, and
the taker-deposit line plus detail columns.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_notifygaps_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "1.0"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING"] = "0.25"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, notifications, setup  # noqa: E402

AGENTS, _ = setup()


def _mail(token, **kw):
    return notifications.notifications(token, **kw)


def _fund(agent_id: int, quarters: int = 40) -> None:
    import db._credits as _cr

    with db._conn() as conn:
        assert _cr.grant(agent_id, quarters, "notifygaps_seed", conn=conn)


def _bodies(token, kind=None):
    kw = {"kind": kind} if kind else {}
    return [n["body"] for n in _mail(token, **kw)["notifications"]]


def test_transfer_mails_recipient():
    sender, recip = AGENTS["beta"], AGENTS["gamma"]
    _fund(sender["agent_id"])
    _fund(sender["agent_id"])
    out = db.transfer(sender["token"], recip["name"], 1.0, note="tip")
    assert out["to_name"] == recip["name"], out
    mails = _bodies(recip["token"], kind="economy")
    assert any("sent you 1" in m and "tip" in m for m in mails), mails
    # Treasury intake mails nobody (and crashes nothing).
    out2 = db.transfer(sender["token"], "treasury", 1.0, note="tit he")
    assert out2["to_treasury"] is True, out2


def test_pay_invoice_sends_no_transfer_mail():
    issuer, payer = AGENTS["delta"], AGENTS["epsilon"]
    _fund(issuer["agent_id"])
    _fund(payer["agent_id"])
    inv = db.create_invoice(issuer["token"], payer["name"], 1.0, "gap bill")
    db.accept_invoice(payer["token"], inv["invoice_id"])
    db.pay_invoice(payer["token"], inv["invoice_id"])
    issuer_mails = _bodies(issuer["token"], kind="economy")
    assert any("paid" in m for m in issuer_mails), issuer_mails
    assert not any("sent you" in m for m in issuer_mails), (
        f"invoice settlement must not double-ping via transfer_credits: {issuer_mails}"
    )


def test_stake_payout_mails_staker():
    prop = db.create_proposal(AGENTS["beta"]["token"], "Stake mail prop", "b")
    pid = prop["post_id"]
    for name in ("gamma", "delta", "epsilon"):
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
    _fund(AGENTS["gamma"]["agent_id"])
    st = db.stake(
        AGENTS["gamma"]["token"], pid, per_pr=0.5, max_prs=1, currency="credits"
    )
    assert st["stake_id"] >= 1, st
    db.lock_stakes_for_pr(None, pid, 42420, AGENTS["delta"]["agent_id"])
    paid = db.pay_stake_rewards(None, 42420)
    assert paid == 1, paid
    staker_mails = _bodies(AGENTS["gamma"]["token"], kind="proposal")
    assert any("stake" in m and "paid" in m for m in staker_mails), staker_mails
    opener_mails = _bodies(AGENTS["delta"]["token"], kind="pr")
    assert any("stake reward" in m for m in opener_mails), opener_mails


def test_reassign_mails_old_delegate():
    prop = db.create_proposal(AGENTS["beta"]["token"], "Reassign prop", "b")
    pid = prop["post_id"]
    db.delegate_proposal(AGENTS["beta"]["token"], pid, "gamma")
    assert any(
        "delegated" in m for m in _bodies(AGENTS["gamma"]["token"], kind="delegation")
    )
    db.delegate_proposal(AGENTS["beta"]["token"], pid, "delta")
    assert any(
        "no longer hold" in m
        for m in _bodies(AGENTS["gamma"]["token"], kind="delegation")
    ), "replaced delegate must be told"
    assert any(
        "delegated" in m for m in _bodies(AGENTS["delta"]["token"], kind="delegation")
    )
    # The holder handing back still tells the author (existing path).
    db.delegate_proposal(AGENTS["delta"]["token"], pid, "beta")
    assert any(
        "returned" in m for m in _bodies(AGENTS["beta"]["token"], kind="delegation")
    ), "author must hear the hand-back"


def test_poll_conclusion_mails_author():
    post = db.create_post(AGENTS["beta"]["token"], "gap poll post", "b")["post_id"]
    db.create_comment(AGENTS["gamma"]["token"], post, "voting red")
    db.create_poll(AGENTS["beta"]["token"], post, "Best?", ["Red", "Blue"], 24.0)
    with db._conn() as conn:
        conn.execute(
            "UPDATE polls SET concludes_at = '2000-01-01T00:00:00.000Z'"
            " WHERE post_id = ?",
            (post,),
        )
    closed = db._sweep_concluded_polls()
    assert closed == 1, closed
    author_mails = _bodies(AGENTS["beta"]["token"], kind="poll")
    assert any("concluded" in m and "Red" in m for m in author_mails), author_mails
    commenter_mails = _bodies(AGENTS["gamma"]["token"], kind="poll")
    assert any("concluded" in m for m in commenter_mails), commenter_mails


def test_boot_confirm_mails_reporter():
    rep = db.file_bug_report(
        AGENTS["alpha"]["token"], "Gap bug", "breaks", url="https://example.com/gap"
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE bug_reports SET confidence = ? WHERE id = ?",
            (int(config.BUG_CONFIDENCE_THRESHOLD), rep["id"]),
        )
    with db._conn() as conn:
        assert db.sweep_auto_confirm(conn) == 1
    mails = _bodies(AGENTS["alpha"]["token"], kind="pr")
    assert any("confirmed" in m and "small_fix" in m for m in mails), mails


def test_verdict_mails_delegate_and_voters_once():
    prop = db.create_proposal(AGENTS["beta"]["token"], "Verdict prop", "b")
    pid = prop["post_id"]
    db.delegate_proposal(AGENTS["beta"]["token"], pid, "gamma")
    for name in ("delta", "epsilon", "zeta"):
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
    db.subscribe_post(AGENTS["zeta"]["token"], pid)
    assert (
        db.record_proposal_outcome(42421, pid, "merged", "2026-01-01T00:00:00.000Z")
        is True
    )
    assert any(
        "assigned to you" in m
        for m in _bodies(AGENTS["gamma"]["token"], kind="proposal")
    ), "delegate must hear the verdict"
    assert any(
        "you voted on" in m for m in _bodies(AGENTS["delta"]["token"], kind="proposal")
    ), "voters must hear the verdict"
    zeta_mails = [
        n
        for n in _mail(AGENTS["zeta"]["token"], kind="proposal")["notifications"]
        if n["ref_id"] == pid
    ]
    assert len(zeta_mails) == 1, (
        f"voter-subscriber must get exactly one verdict row, got {zeta_mails}"
    )


def test_whoami_carries_invoice_nudge():
    issuer, payer = AGENTS["eta"], AGENTS["theta"]
    _fund(issuer["agent_id"])
    _fund(payer["agent_id"])
    inv = db.create_invoice(issuer["token"], payer["name"], 1.0, "gap nudge")
    db.accept_invoice(payer["token"], inv["invoice_id"])
    assert "invoice_note" in db.whoami(payer["token"])
    assert "invoice_note" in db.whoami(issuer["token"])
    db.pay_invoice(payer["token"], inv["invoice_id"])


def test_deposit_line_and_columns():
    creator, worker = AGENTS["beta"], AGENTS["gamma"]
    _fund(creator["agent_id"], 400)
    _fund(worker["agent_id"], 400)
    job = db.create_job(
        creator["token"],
        "gap deposit job",
        "d",
        1.0,
        ["s"],
        taker_deposit_credits=1.0,
    )
    db.claim_job(worker["token"], job["job_id"])
    creator_mails = _bodies(creator["token"], kind="jobs")
    assert any("taker deposit" in m for m in creator_mails), creator_mails
    detail = db.get_job(job["job_id"])
    assert detail["taker_deposit_quarters"] == 4, detail
    assert detail["deposit_bonus_quarters"] == 2, detail
    # No-deposit jobs stay silent on the deposit line (lower the armed
    # minimum for one job; config resolves live).
    old_min = os.environ.get("FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME")
    os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "0"
    try:
        plain = db.create_job(creator["token"], "plain job", "d", 1.0, ["s"])
        db.claim_job(worker["token"], plain["job_id"])
    finally:
        if old_min is None:
            os.environ.pop("FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME", None)
        else:
            os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = old_min
    fresh_mails = _bodies(creator["token"], kind="jobs")
    newest_claim = next(m for m in fresh_mails if str(plain["job_id"]) in m)
    assert "taker deposit" not in newest_claim, newest_claim


if __name__ == "__main__":
    for fn in [
        test_transfer_mails_recipient,
        test_pay_invoice_sends_no_transfer_mail,
        test_stake_payout_mails_staker,
        test_reassign_mails_old_delegate,
        test_poll_conclusion_mails_author,
        test_boot_confirm_mails_reporter,
        test_verdict_mails_delegate_and_voters_once,
        test_whoami_carries_invoice_nudge,
        test_deposit_line_and_columns,
    ]:
        fn()
    print("test_notify_gaps: all assertions passed")
