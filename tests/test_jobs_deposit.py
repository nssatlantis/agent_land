"""Regression test for the taker-deposit double-payment bug.

When a job with a taker deposit completes, the deposit's escrow half used
to be paid back TWICE: _check_deposit_return returned it via
return_principal, then _maybe_pay_bonus read a stale in-memory copy of the
job row (whose deposit_bonus_quarters the first function had already zeroed
in the DB but not in the Row) and granted it again. This suite arms a
nonzero deposit - the one thing every other jobs suite deliberately sets
to 0 - and asserts the worker is paid exactly the deposit back (plus the
wage and the participation reward), with no duplicate job_deposit_bonus
credit entry.
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_jobsdep_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"
# Arm the deposit path (every other jobs suite zeroes these).
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "1.0"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING"] = "0.25"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()

from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(60000, "test_suite_topup", admin="test-suite", conn=_c)


def _ledger_reasons(agent_id: int) -> list[str]:
    rows = db.credit_history(agent_id=agent_id, limit=100)["entries"]
    return [r["reason"] for r in rows]


def _balance(agent_id: int) -> int:
    from db._credits import balance_for

    with db._conn() as _c:
        return balance_for(_c, agent_id)


def test_one_time_deposit_returns_exactly_once():
    creator = db.register_agent("dep-c")
    worker = db.register_agent("dep-w")
    with db._conn() as _c:
        from db._credits import grant

        grant(creator["agent_id"], 400, "test_seed", conn=_c)
        grant(worker["agent_id"], 400, "test_seed", conn=_c)
    p = db.create_post(creator["token"], "dep t", "b")
    db.vote(AGENTS["beta"]["token"], "post", p["post_id"], 1)

    # Deposit of 1.0 credit = 4 quarters (D=4). Worker pays 2 to treasury,
    # 2 into the escrow pool. On completion the worker should get the whole
    # 4 back - the 2 treasury half via grant and the 2 escrow half via
    # return_principal - and NOT a duplicate job_deposit_bonus for the 2.
    job = db.create_job(
        creator["token"], "deposit job", "d", 1.0, ["s"],
        taker_deposit_credits=1.0,
    )
    with db._conn() as _c:
        assert _c.execute(
            "SELECT taker_deposit_quarters FROM jobs WHERE id = ?",
            (job["job_id"],),
        ).fetchone()["taker_deposit_quarters"] == 4

    before = _balance(worker["agent_id"])
    db.claim_job(worker["token"], job["job_id"])
    after_claim = _balance(worker["agent_id"])
    # Deposit debits 4 quarters from the worker at claim.
    assert before - after_claim == 4

    db.submit_job(worker["token"], job["job_id"], "#P")
    db.review_job(creator["token"], job["job_id"], "accept")

    with db._conn() as _c:
        row = _c.execute(
            "SELECT taker_deposit_quarters, deposit_bonus_quarters,"
            " status FROM jobs WHERE id = ?",
            (job["job_id"],),
        ).fetchone()

    # Deposit fully returned; pool zeroed; job completed.
    assert row["taker_deposit_quarters"] == 0
    assert row["deposit_bonus_quarters"] == 0
    assert row["status"] == "completed"

    reasons = _ledger_reasons(worker["agent_id"])
    ret_cnt = sum(1 for r in reasons if r == "job_deposit_return_escrow")
    tre_cnt = sum(1 for r in reasons if r == "job_deposit_return_treasury")
    bonus_cnt = sum(1 for r in reasons if r == "job_deposit_bonus")
    # Escrow half returned exactly once, treasury half granted once,
    # and NO duplicate bonus for the worker's own escrow half.
    assert ret_cnt == 1, f"escrow return count {ret_cnt} != 1"
    assert tre_cnt == 1, f"treasury return count {tre_cnt} != 1"
    assert bonus_cnt == 0, f"unexpected bonus payment count {bonus_cnt}"

    # Total change = deposit 4 back + wage 4 (1.0) + reward (credits ~1).
    total_gain = _balance(worker["agent_id"]) - after_claim
    # Wage is 4 quarters; reward is roughly the participation credits.
    assert total_gain >= 8, f"worker gained only {total_gain} after deposit+all"
    assert bonus_cnt == 0, (
        "double-payment regression: worker received a duplicate deposit bonus"
    )


def test_recurring_does_not_pay_bonus_before_completion():
    creator = db.register_agent("dep-c2")
    worker = db.register_agent("dep-w2")
    with db._conn() as _c:
        from db._credits import grant

        grant(creator["agent_id"], 400, "test_seed", conn=_c)
        grant(worker["agent_id"], 400, "test_seed", conn=_c)
    p = db.create_post(creator["token"], "dep t2", "b")
    db.vote(AGENTS["beta"]["token"], "post", p["post_id"], 1)

    job = db.create_job(
        creator["token"], "recurring deposit", "d", 1.0, ["s"],
        kind="recurring", cycles=2, taker_deposit_credits=1.0,
    )
    with db._conn() as _c:
        assert _c.execute(
            "SELECT taker_deposit_quarters FROM jobs WHERE id = ?",
            (job["job_id"],),
        ).fetchone()["taker_deposit_quarters"] == 4

    before = _balance(worker["agent_id"])
    db.claim_job(worker["token"], job["job_id"])
    after_claim = _balance(worker["agent_id"])
    assert before - after_claim == 4

    # First cycle accept (NOT the final) must not pay any bonus yet.
    db.submit_job(worker["token"], job["job_id"], "#P")
    db.review_job(creator["token"], job["job_id"], "accept")
    reasons = _ledger_reasons(worker["agent_id"])
    assert "job_deposit_bonus" not in reasons, (
        "bonus paid on a non-final cycle - deposit should return on completion"
    )
    with db._conn() as _c:
        row = _c.execute(
            "SELECT deposit_bonus_quarters, taker_deposit_quarters,"
            " cycles_done FROM jobs WHERE id = ?",
            (job["job_id"],),
        ).fetchone()
    # Pool intact (deposit not returned mid-way), deposit still held.
    assert row["deposit_bonus_quarters"] == 2
    assert row["taker_deposit_quarters"] == 4

    # Final cycle: deposit returns exactly once.
    db.submit_job(worker["token"], job["job_id"], "#P")
    db.review_job(creator["token"], job["job_id"], "accept")
    reasons = _ledger_reasons(worker["agent_id"])
    ret_cnt = sum(1 for r in reasons if r == "job_deposit_return_escrow")
    bonus_cnt = sum(1 for r in reasons if r == "job_deposit_bonus")
    assert ret_cnt == 1, f"escrow return count {ret_cnt} != 1"
    assert bonus_cnt == 0, f"unexpected bonus count {bonus_cnt}"


if __name__ == "__main__":
    test_one_time_deposit_returns_exactly_once()
    test_recurring_does_not_pay_bonus_before_completion()
    print("All taker deposit regression tests passed.")
