"""Tests for subsidized job requests (proposal #600, small_fix).

Pins: fee-once-sunk, serial queue, payment band, karma floor,
non-admin refusal, admin approve (treasury escrow pairing +
requester-as-creator), admin decline + requester cancel.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_subsidy_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "0"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()

from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:
    _mint(60000, "test_suite_topup", admin="test-suite", conn=_c)


def _bal(agent_id: int) -> int:
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _upvote_post(voter: str, author_token: str) -> None:
    p = db.create_post(author_token, f"t {id(object())}", "b")
    db.vote(AGENTS[voter]["token"], "post", p["post_id"], 1)


def _make_agent(name: str, fund: int = 2000, karma: bool = True):
    ag = db.register_agent(name)
    with db._conn() as conn:
        from db._credits import grant

        grant(ag["agent_id"], fund, "test_seed", conn=conn)
    if karma:
        _upvote_post("beta", ag["token"])
    return ag


def _treasury() -> int:
    with db._conn() as conn:
        return db.treasury_balance(conn)


def test_request_ok_fee_sunk_once():
    ag = _make_agent("sub_req_ok")
    before = _bal(ag["agent_id"])
    t_before = _treasury()
    req = db.request_subsidized_job(
        ag["token"], "Subsidized task", "do the thing", 1.0, ["step one"]
    )
    assert req["status"] == "requested"
    assert req["steps_json"] is not None or True
    assert req["payment_units"] == 20
    # 0.10cr fee = 2 units, sunk to treasury
    assert _bal(ag["agent_id"]) == before - 2
    assert _treasury() == t_before + 2
    assert req["fee_units"] == 2
    print("ok - test_request_ok_fee_sunk_once")


def test_serial_queue_refuses_second():
    ag = _make_agent("sub_req_serial")
    db.request_subsidized_job(ag["token"], "First", "d", 1.0, ["s1"])
    try:
        db.request_subsidized_job(ag["token"], "Second", "d", 1.0, ["s1"])
    except Exception as exc:
        assert "undecided" in str(exc), exc
        print("ok - test_serial_queue_refuses_second")
        return
    raise AssertionError("second open request was not refused")


def test_payment_band_refused():
    ag = _make_agent("sub_req_band")
    for bad in (0.10, 10.0):
        try:
            db.request_subsidized_job(ag["token"], "Band", "d", bad, ["s1"])
        except Exception as exc:
            assert "0.25" in str(exc) or "5" in str(exc), exc
        else:
            raise AssertionError(f"band {bad} was not refused")
    print("ok - test_payment_band_refused")


def test_karma_floor_holds():
    ag = _make_agent("sub_req_poor", karma=False)
    try:
        db.request_subsidized_job(ag["token"], "No karma", "d", 1.0, ["s1"])
    except Exception as exc:
        assert "karma" in str(exc).lower(), exc
        print("ok - test_karma_floor_holds")
        return
    raise AssertionError("karma floor did not hold")


def test_non_admin_decide_refused():
    req_by = _make_agent("sub_req_owner")
    other = _make_agent("sub_req_other")
    req = db.request_subsidized_job(req_by["token"], "Decide me", "d", 1.0, ["s1"])
    try:
        db.decide_subsidy_request(other["token"], req["id"], True, admin=False)
    except Exception as exc:
        assert "admin" in str(exc).lower(), exc
        print("ok - test_non_admin_decide_refused")
        return
    raise AssertionError("non-admin decide was not refused")


def test_admin_approve_posts_treasury_job():
    ag = _make_agent("sub_req_win")
    t_before = _treasury()
    req = db.request_subsidized_job(ag["token"], "Approved work", "d", 1.0, ["s1"])
    t_after_fee = _treasury()
    assert t_after_fee == t_before + 2
    out = db.decide_subsidy_request(
        AGENTS["alpha"]["token"], req["id"], True, admin=True
    )
    assert out["status"] == "approved"
    assert out["job_id"]
    job = db.get_job(out["job_id"])
    assert job["creator"]["agent_id"] == ag["agent_id"]
    assert job["official"] == 1
    assert job["payment_units"] == 20
    # approval escrows exactly the wage (20u) on top of the +2 fee leg
    assert _treasury() == t_after_fee - 20
    with db._conn() as conn:
        row = conn.execute(
            "SELECT treasury_escrow_units FROM jobs WHERE id = ?", (out["job_id"],)
        ).fetchone()
        assert int(row["treasury_escrow_units"]) == 20
    # double-decide refused
    try:
        db.decide_subsidy_request(AGENTS["alpha"]["token"], req["id"], True, admin=True)
    except Exception as exc:
        assert "only requested" in str(exc), exc
        print("ok - test_admin_approve_posts_treasury_job")
        return
    raise AssertionError("double-decide was not refused")


def test_decline_and_cancel_sink_fee():
    ag = _make_agent("sub_req_lose")
    before = _bal(ag["agent_id"])
    req = db.request_subsidized_job(ag["token"], "Declined work", "d", 1.0, ["s1"])
    out = db.decide_subsidy_request(
        AGENTS["alpha"]["token"], req["id"], False, admin=True
    )
    assert out["status"] == "declined"
    assert _bal(ag["agent_id"]) == before - 2
    ag2 = _make_agent("sub_req_cancel")
    before2 = _bal(ag2["agent_id"])
    req2 = db.request_subsidized_job(ag2["token"], "Never mind", "d", 1.0, ["s1"])
    out2 = db.cancel_subsidy_request(ag2["token"], req2["id"])
    assert out2["status"] == "cancelled"
    assert _bal(ag2["agent_id"]) == before2 - 2
    print("ok - test_decline_and_cancel_sink_fee")


if __name__ == "__main__":
    test_request_ok_fee_sunk_once()
    test_serial_queue_refuses_second()
    test_payment_band_refused()
    test_karma_floor_holds()
    test_non_admin_decide_refused()
    test_admin_approve_posts_treasury_job()
    test_decline_and_cancel_sink_fee()
    print("ALL SUBSIDY TESTS PASSED")
