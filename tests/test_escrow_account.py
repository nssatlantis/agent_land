"""Tests for the escrow bank account (proposal #319): paired-leg escrow
moves keep supply fixed through citizen and official lifecycles, the
conservation audit verifies holdings and per-tx zero-sum, the one-time
backfill repairs pre-cutover single-sided debits, and the watch trips
and resolves edge-triggered events."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_escrow_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "0"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()

from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(40000, "test_suite_topup", admin="test-suite", conn=_c)


def _make_creator(name: str):
    ag = db.register_agent(name)
    with db._conn() as conn:
        from db._credits import grant

        grant(ag["agent_id"], 400, "test_seed", conn=conn)
    p = db.create_post(ag["token"], f"t {name}", "b")
    db.vote(AGENTS["beta"]["token"], "post", p["post_id"], 1)
    return ag


def _supply() -> int:
    with db._conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries",
        ).fetchone()[0]


def _escrow() -> int:
    with db._conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
            " WHERE account = 'escrow'",
        ).fetchone()[0]


def _treasury() -> int:
    with db._conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
            " WHERE account = 'treasury'",
        ).fetchone()[0]


def _bal(agent_id: int) -> int:
    from db._credits import balance_for

    with db._conn() as c:
        return balance_for(c, agent_id)


def _post_citizen(creator, pay=2.0, cycles=3):
    return db.create_job(
        creator["token"],
        f"escrow job {creator['name']}",
        "d",
        pay,
        ["s"],
        kind="recurring",
        cycles=cycles,
    )


def test_citizen_post_pairs_legs_supply_neutral():
    creator = _make_creator("ea-post")
    s0, e0 = _supply(), _escrow()
    job = _post_citizen(creator)
    assert _supply() == s0, "posting into escrow never moves supply"
    assert _escrow() == e0 + 24, "the full wage x cycles sits in escrow"
    with db._conn() as conn:
        legs = conn.execute(
            "SELECT account, delta_quarters FROM credit_entries"
            " WHERE reason IN ('job_escrow', 'job_escrow_held')"
            " AND target_id = ? ORDER BY id",
            (job["job_id"],),
        ).fetchall()
    assert [(r["account"], r["delta_quarters"]) for r in legs] == [
        ("agent", -24),
        ("escrow", 24),
    ]
    assert db.economy_overview()["conservation"]["ok"] is True


def test_accept_draws_escrow_down():
    creator = _make_creator("ea-pay")
    worker = db.register_agent("ea-payw")
    s0, e0 = _supply(), _escrow()
    job = _post_citizen(creator)
    db.claim_job(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#P1")
    db.review_job(creator["token"], job["job_id"], "accept")
    assert _supply() == s0, "payout from escrow never moves supply"
    assert _escrow() == e0 + 16, "one wage drew the holding down"
    assert _bal(worker["agent_id"]) >= 8, "the wage landed"


def test_cancel_releases_to_creator():
    creator = _make_creator("ea-cancel")
    worker = db.register_agent("ea-cancelw")
    s0, e0 = _supply(), _escrow()
    job = _post_citizen(creator)
    db.claim_job(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#P1")
    db.review_job(creator["token"], job["job_id"], "accept")
    out = db.cancel_job(creator["token"], job["job_id"])
    assert out["status"] == "cancelled"
    assert _supply() == s0 and _escrow() == e0


def test_official_post_pairs_treasury_escrow():
    sponsor = _make_creator("ea-off")
    s0, e0, t0 = _supply(), _escrow(), _treasury()
    db.create_job_official(
        "m", sponsor["name"], "role", "d", 2.0, ["s"],
        kind="recurring", cycles=4,
    )
    assert _supply() == s0
    assert _escrow() == e0 + 32
    assert _treasury() == t0 - 32


def test_official_wage_and_cancel_settle():
    sponsor = _make_creator("ea-off2")
    worker = db.register_agent("ea-off2w")
    s0, e0, t0 = _supply(), _escrow(), _treasury()
    job = db.create_job_official(
        "m", sponsor["name"], "role", "d", 2.0, ["s"],
        kind="recurring", cycles=4, offer_to=worker["name"],
    )
    db.accept_job_offer(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#P1")
    db.review_job(sponsor["token"], job["job_id"], "accept")
    assert _supply() == s0 and _escrow() == e0 + 24
    assert _bal(worker["agent_id"]) >= 8
    db.admin_cancel_job("maintainer", job["job_id"])
    assert _supply() == s0 and _escrow() == e0
    assert _treasury() == t0 - 10, "only the two reward quarters left"


def test_reactivate_guard_refuses_stacked_escrow():
    from db._credits import escrow_to_treasury

    sponsor = _make_creator("ea-guard")
    job = db.create_job_official(
        "m", sponsor["name"], "guard role", "d", 2.0, ["s"],
        kind="recurring", cycles=4,
    )
    jid = job["job_id"]
    with db._conn(immediate=True) as c:
        c.execute("UPDATE jobs SET status = 'cancelled' WHERE id = ?", (jid,))
    msg = expect_error(db.admin_reactivate_job, "m", jid)
    assert "still holds" in msg
    # Resolve exactly like a cancel would, so later tests see a clean book.
    with db._conn(immediate=True) as c:
        escrow_to_treasury(
            32,
            "job_cancelled_treasury_return",
            target_type="job",
            target_id=jid,
            conn=c,
        )
        c.execute(
            "UPDATE jobs SET treasury_escrow_quarters = 0 WHERE id = ?",
            (jid,),
        )
    assert db._economy.verify_conservation()["ok"] is True


def test_backfill_repairs_legacy_holding():
    creator = _make_creator("ea-backfill")
    s_pre, e_pre = _supply(), _escrow()
    job = _post_citizen(creator)
    jid = job["job_id"]
    with db._conn(immediate=True) as c:
        c.execute(
            "DELETE FROM credit_entries WHERE reason = 'job_escrow_held'"
            " AND target_id = ?",
            (jid,),
        )
        c.execute(
            "UPDATE credit_entries SET tx_id = NULL WHERE reason = 'job_escrow'"
            " AND target_id = ?",
            (jid,),
        )
    assert _escrow() == e_pre, "the holding vanished with its leg"
    assert _supply() == s_pre - 24, "the legacy shape destroyed supply"
    with db._conn(immediate=True) as c:
        c.execute("DELETE FROM economy_meta WHERE key = 'escrow_account_live'")
    res = db._economy.backfill_escrow_account()
    assert res["backfilled_quarters"] == 24 and res["jobs"] == 1
    assert _supply() == s_pre, "the repair restores destroyed supply"
    assert _escrow() == e_pre + 24
    assert db._economy.verify_conservation()["ok"] is True
    res2 = db._economy.backfill_escrow_account()
    assert res2["already_live"] is True
    assert res2["backfilled_quarters"] == 0


def test_verifier_catches_unpaired_post_cutover_leg():
    from db._credits import _insert_entry, _new_tx_id

    db._economy.backfill_escrow_account()
    with db._conn(immediate=True) as c:
        tx = _new_tx_id(c)
        _insert_entry(c, None, "escrow", -8, "job_escrow", "job", 424242, tx_id=tx)
    try:
        con = db._economy.verify_conservation()
        assert con["ok"] is False
        assert tx in con["tx_violations"]
    finally:
        with db._conn(immediate=True) as c:
            c.execute("DELETE FROM credit_entries WHERE tx_id = ?", (tx,))
    assert db._economy.verify_conservation()["ok"] is True


def test_watch_trips_and_resolves():
    from db._credits import _insert_entry, _new_tx_id

    db._economy.backfill_escrow_account()
    with db._conn(immediate=True) as c:
        c.execute("DELETE FROM economy_meta WHERE key = 'conservation_last_ok'")
    first = db.conservation_watch_tick()
    assert first["event"] is None
    assert first["ok"] is True
    with db._conn(immediate=True) as c:
        tx = _new_tx_id(c)
        _insert_entry(c, None, "escrow", -8, "job_escrow", "job", 424243, tx_id=tx)
    try:
        tripped = db.conservation_watch_tick()
        assert tripped["ok"] is False
        assert tripped["event"] == "economy_conservation_tripped"
    finally:
        with db._conn(immediate=True) as c:
            c.execute("DELETE FROM credit_entries WHERE tx_id = ?", (tx,))
    resolved = db.conservation_watch_tick()
    assert resolved["ok"] is True
    assert resolved["event"] == "economy_conservation_resolved"
    with db._conn() as conn:
        last = conn.execute(
            "SELECT value FROM economy_meta WHERE key = 'conservation_last_ok'"
        ).fetchone()[0]
    assert last == "1"


def test_group_renders_escrow_parties():
    from db._credits import group_transactions

    post = group_transactions(
        [
            {
                "tx_id": 1,
                "account": "agent",
                "delta_quarters": -24,
                "reason": "job_escrow",
                "created_at": "2026-01-01T00:00:00.000Z",
                "agent_name": "alice",
            },
            {
                "tx_id": 1,
                "account": "escrow",
                "delta_quarters": 24,
                "reason": "job_escrow_held",
                "created_at": "2026-01-01T00:00:00.000Z",
                "agent_name": None,
            },
        ]
    )[0]
    assert post["from_name"] == "alice"
    assert post["to_name"] is None
    pay = group_transactions(
        [
            {
                "tx_id": 2,
                "account": "agent",
                "delta_quarters": 8,
                "reason": "job_payout",
                "created_at": "2026-01-01T00:00:00.000Z",
                "agent_name": "bob",
            },
            {
                "tx_id": 2,
                "account": "escrow",
                "delta_quarters": -8,
                "reason": "job_payout_release",
                "created_at": "2026-01-01T00:00:00.000Z",
                "agent_name": None,
            },
        ]
    )[0]
    assert pay["to_name"] == "bob"
    assert pay["from_name"] == "Escrow"
