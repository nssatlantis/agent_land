"""Guild pool money (proposal #525, PR-3): L4 movement.

Covers deposits/withdrawals (+2% mover-pays fee, lock/velocity/co-sign
gates), pool-funded invoice payments, commissioned jobs (pool escrow,
floor bypass, fee-free commissions, single lock memo, cancel/expiry
refunds to pool), executor-taken jobs (wage to pool, detach on leave),
and voluntary disband (zero vs fee'd dissolve). Stakes variant + upkeep
ride PR-4.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_money_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "0"
os.environ["FORUM_INVOICE_MIN_KARMA"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, units: int):
    import db._credits as _cr

    with db._conn() as _c:
        ok = _cr.grant(
            agent_id,
            units,
            "guild_money_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )
    assert ok, "treasury could not fund the test seed"


def _bal(agent_id: int) -> int:
    import db._credits as _cr

    with db._conn() as conn:
        return _cr.balance_for(conn, agent_id)


def _found(name: str | None = None) -> tuple[dict, dict]:
    ag = _new_agent("gm-founder")
    _fund(ag["agent_id"], 600)
    return ag, db.found_guild(ag["token"], name or f"Money-{_SEQ[0]}")


def _guild_with_mate() -> tuple[dict, dict, dict]:
    founder, guild = _found()
    mate = _new_agent("gm-mate")
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    return founder, guild, mate


def _pool(guild_id: int) -> int:
    with db._conn() as conn:
        return db.guild_balance(conn, guild_id)


def _run_cycle(worker_token: str, creator_token: str, job_id: int):
    # Evidence carries no PR refs: the taker-deposit return gate reads
    # vacuous-merge and pays back deterministically (no network).
    job = db.get_job(job_id)
    for step in job["steps"]:
        db.tick_job_step(worker_token, job_id, step["id"], True)
    db.submit_job(worker_token, job_id, "done")
    return db.review_job(creator_token, job_id, "accept", "")


def _arm(env_key: str, value: str):
    from tests._setup import config as _cfg

    old = os.environ.get(env_key)
    os.environ[env_key] = value
    importlib.reload(_cfg)
    return old


def _unarm(old, env_key: str):
    from tests._setup import config as _cfg

    if old is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = old
    importlib.reload(_cfg)


def test_deposit_fee_and_memo():
    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    before = _bal(mate["agent_id"])
    out = db.guild_deposit(mate["token"], gid, 10.0)
    # 10cr = 200u + 2% fee (4u, ceil) debited; pool credited full 200.
    assert out["deposited_units"] == 200, out
    assert out["fee_units"] == 4, out
    assert _bal(mate["agent_id"]) == before - 204
    assert _pool(gid) == 200
    assert out["pool_balance"] == 200
    try:
        db.guild_deposit(mate["token"], gid, 0)
        raise AssertionError("zero deposit accepted")
    except Exception as exc:
        assert "positive" in str(exc), exc
    outsider = _new_agent("gm-out")
    try:
        db.guild_deposit(outsider["token"], gid, 1.0)
        raise AssertionError("outsider deposit accepted")
    except Exception as exc:
        assert "members" in str(exc), exc


def test_withdraw_gates_and_math():
    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    try:
        db.guild_withdraw(mate["token"], gid, 1.0)
        raise AssertionError("non-founder withdrawal accepted")
    except Exception as exc:
        assert "founder" in str(exc), exc
    try:
        db.guild_withdraw(founder["token"], gid, 1.0)
        raise AssertionError("empty-pool withdrawal accepted")
    except Exception as exc:
        assert "cover" in str(exc), exc
    db.guild_deposit(founder["token"], gid, 25.0)  # 500u pool
    try:
        db.guild_withdraw(founder["token"], gid, 10.0)
        raise AssertionError("velocity breach accepted")
    except Exception as exc:
        assert "velocity" in str(exc), exc
    # 30% of 100 = 30: 7.5cr = 30q passes; co-sign band (>15) needs record.
    try:
        db.guild_withdraw(founder["token"], gid, 5.0)
        raise AssertionError("un-cosigned big withdrawal accepted")
    except Exception as exc:
        assert "co-sign" in str(exc), exc
    cos = db.request_guild_cosign(founder["token"], gid, "ops", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    before = _bal(founder["agent_id"])
    out = db.guild_withdraw(founder["token"], gid, 5.0)
    # Pool memo -100u, founder +98u (2u fee stays pool-owned in the
    # wallet: wallet 402, memo 400 - proposal #611 retention).
    assert out["paid_units"] == 98, out
    assert out["fee_units"] == 2, out
    assert _bal(founder["agent_id"]) == before + 98
    assert _pool(gid) == 402
    # Solo guild: spending re-locked refuses even funded withdrawals.
    solo_f, solo_g = _found()
    db.guild_deposit(solo_f["token"], solo_g["id"], 10.0)
    try:
        db.guild_withdraw(solo_f["token"], solo_g["id"], 1.0)
        raise AssertionError("locked withdrawal accepted")
    except Exception as exc:
        assert "re-locked" in str(exc), exc


def test_invoice_pay_full_and_part():
    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    db.guild_deposit(founder["token"], gid, 25.0)
    creditor = _new_agent("gm-creditor")
    _fund(creditor["agent_id"], 250)
    inv = db.create_invoice(creditor["token"], founder["name"], 4.0, "consulting")
    db.accept_invoice(founder["token"], inv["invoice_id"])
    got = _bal(creditor["agent_id"])
    cos = db.request_guild_cosign(founder["token"], gid, "invoices", 120)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    out = db.guild_pay_invoice(founder["token"], inv["invoice_id"])
    assert out["paid_units"] == 80, out
    assert out["remaining_units"] == 0
    assert _bal(creditor["agent_id"]) == got + 80
    assert _pool(gid) == 500 - 80
    # Part-pay path.
    inv2 = db.create_invoice(creditor["token"], founder["name"], 8.0, "more work")
    db.accept_invoice(founder["token"], inv2["invoice_id"])
    part = db.guild_pay_invoice(founder["token"], inv2["invoice_id"], 2.0)
    assert part["paid_units"] == 40 and part["remaining_units"] == 120
    try:
        db.guild_pay_invoice(mate["token"], inv2["invoice_id"], 1.0)
        raise AssertionError("non-payer invoice pay accepted")
    except Exception as exc:
        assert "addressed" in str(exc), exc


def test_commission_escrow_and_accept_legs():
    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    db.guild_deposit(founder["token"], gid, 25.0)  # 500u
    worker = _new_agent("gm-worker")
    _fund(worker["agent_id"], 250)
    cos = db.request_guild_cosign(founder["token"], gid, "website", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    job = db.create_job(
        founder["token"],
        "Guild website",
        "build it",
        5.0,
        ["design", "ship"],
        guild_id=gid,
    )
    assert _pool(gid) == 500 - 100, _pool(gid)  # 100u escrow, fees 0 (rate 0)
    with db._conn() as conn:
        link = conn.execute(
            "SELECT * FROM guild_job_links WHERE job_id = ?", (job["job_id"],)
        ).fetchone()
    assert link is not None and link["role"] == "commissioned"
    claimed = db.claim_job(worker["token"], job["job_id"])
    assert claimed["status"] == "active"
    w_before = _bal(worker["agent_id"])
    f_before = _bal(founder["agent_id"])
    _run_cycle(worker["token"], founder["token"], job["job_id"])
    # Wage (100u) came from ledger escrow to the outside worker, plus the
    # 5u reward leg and the 10u taker-deposit return (vacuous merge).
    assert _bal(worker["agent_id"]) == w_before + 100 + 5 + 10, (
        _bal(worker["agent_id"]),
        w_before,
    )
    # ...and the founder's 0.25cr creator leg paid personally (v1
    # identical): the pool's single spend is the commission lock memo,
    # accepted wages draw that locked escrow down with no further memos.
    assert _bal(founder["agent_id"]) == f_before + 5, (
        _bal(founder["agent_id"]),
        f_before,
    )
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT kind, units, actor_agent_id FROM guild_ledger"
            " WHERE guild_id = ? ORDER BY id",
            (gid,),
        ).fetchall()
    kinds = [(r[0], r[1]) for r in rows]
    # Rows are unsigned (direction rides the kind): the 100u escrow lock
    # is the single spend - no per-cycle wage memo, no creator rebate.
    assert sum(q for k, q in kinds if k == "job_escrow") == 100, kinds
    assert sum(q for k, q in kinds if k == "job") == 0, kinds
    assert _pool(gid) == 500 - 100, _pool(gid)


def test_commission_fee_free_with_nonzero_job_fees():
    # Fail-before pin for the phantom-fee finding: with a 10% placement
    # fee armed, a 100u commission carries fees_q > 0, yet the pool takes
    # only the 100u lock memo - no 'fee' row, gate on escrow alone.
    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    db.guild_deposit(founder["token"], gid, 25.0)  # 500u
    old = _arm("FORUM_TX_FEE_PERCENT", "10")
    try:
        cos = db.request_guild_cosign(founder["token"], gid, "pricey", 110)
        db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
        db.create_job(
            founder["token"],
            "Pricey website",
            "build it",
            5.0,
            ["design", "ship"],
            guild_id=gid,
        )
    finally:
        _unarm(old, "FORUM_TX_FEE_PERCENT")
    assert _pool(gid) == 500 - 100, _pool(gid)
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT kind, units FROM guild_ledger WHERE guild_id = ?",
            (gid,),
        ).fetchall()
    kinds = [(r[0], r[1]) for r in rows]
    assert all(k != "fee" for k, _ in kinds), kinds
    assert sum(q for k, q in kinds if k == "job_escrow") == 100, kinds


def test_disband_cancel_actor_is_founder():
    # Fail-before pin for the event-actor finding: the disband resolver
    # attributes live commissioned cancels to the passed actor (the
    # disbanding founder on the voluntary path), never the worker.
    from db._guilds_money import resolve_guild_jobs_for_disband

    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    db.guild_deposit(founder["token"], gid, 25.0)
    worker = _new_agent("gm-cancelled")
    _fund(worker["agent_id"], 50)
    cos = db.request_guild_cosign(founder["token"], gid, "doomed", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    job = db.create_job(
        founder["token"], "Doomed", "never ships", 5.0, ["x"], guild_id=gid
    )
    db.claim_job(worker["token"], job["job_id"])
    with db._conn(immediate=True) as conn:
        out = resolve_guild_jobs_for_disband(
            conn, gid, actor_agent_id=founder["agent_id"]
        )
    assert out["cancelled"] == [job["job_id"]], out
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT actor_agent_id, target_id FROM events WHERE kind = ?"
            " AND target_id = ?",
            ("job_cancelled", job["job_id"]),
        ).fetchall()
    assert rows, "no job_cancelled event for the resolver cancel"
    assert all(r["actor_agent_id"] == founder["agent_id"] for r in rows), [
        dict(r) for r in rows
    ]


def test_commission_guards():
    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    low = _new_agent("gm-low")
    try:
        db.create_job(low["token"], "Sneak", "no karma", 1.0, ["x"], guild_id=gid)
        raise AssertionError("non-member commissioned")
    except Exception as exc:
        assert "member" in str(exc) or "founder" in str(exc), exc
    try:
        db.create_job(mate["token"], "Sneak", "not founder", 1.0, ["x"], guild_id=gid)
        raise AssertionError("non-founder commissioned")
    except Exception as exc:
        assert "founder" in str(exc), exc
    try:
        db.create_job(founder["token"], "Broke", "no pool", 5.0, ["x"], guild_id=gid)
        raise AssertionError("unfunded commission accepted")
    except Exception as exc:
        assert "cover" in str(exc), exc
    db.guild_deposit(founder["token"], gid, 25.0)
    try:
        db.create_job(
            founder["token"], "Big", "needs cosign", 10.0, ["x"], guild_id=gid
        )
        raise AssertionError("un-cosigned commission accepted")
    except Exception as exc:
        assert "co-sign" in str(exc), exc


def test_taken_wage_to_pool_and_detach():
    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    db.guild_deposit(founder["token"], gid, 10.0)
    outer = _new_agent("gm-outer")
    _fund(outer["agent_id"], 600)
    _fund(mate["agent_id"], 50)
    job = db.create_job(outer["token"], "Outer task", "do it", 4.0, ["go"])
    claimed = db.claim_job(mate["token"], job["job_id"], guild_id=gid)
    assert claimed["worker"]["agent_id"] == mate["agent_id"]
    m_before = _bal(mate["agent_id"])
    _run_cycle(mate["token"], outer["token"], job["job_id"])
    # Wage (80u) went poolward; executor kept the 5u reward leg plus the
    # 10u taker-deposit return (vacuous merge).
    assert _bal(mate["agent_id"]) == m_before + 5 + 10, (
        _bal(mate["agent_id"]),
        m_before,
    )
    assert _pool(gid) == 200 + 80
    # Leaving parks in successor grace (item 5009): the link lives with
    # a clock, and the pool keeps its wage claim until lapse/appointment.
    db.leave_guild(mate["token"], gid)
    with db._conn() as conn:
        link = conn.execute(
            "SELECT executor_agent_id, grace_until FROM guild_job_links"
            " WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()
    assert link is not None and link["grace_until"] is not None
    assert int(link["executor_agent_id"]) == mate["agent_id"]
    # Lapse detaches through the sweep: the next wage pays personally.
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_job_links SET grace_until = '2026-01-01T00:00:00.000Z'"
            " WHERE job_id = ?",
            (job["job_id"],),
        )
    report = db.sweep_guild_memberships()
    assert report["grace_expired"] == 1, report
    with db._conn() as conn:
        gone = conn.execute(
            "SELECT COUNT(*) FROM guild_job_links WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()[0]
    assert gone == 0
    outsider = _new_agent("gm-out2")
    assert outsider is not None


def test_cancel_refund_routes():
    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    db.guild_deposit(founder["token"], gid, 25.0)
    cos = db.request_guild_cosign(founder["token"], gid, "doomed", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    job = db.create_job(
        founder["token"], "Doomed", "cancel me", 5.0, ["x"], guild_id=gid
    )
    assert _pool(gid) == 400
    db.cancel_job(founder["token"], job["job_id"])
    assert _pool(gid) == 500, _pool(gid)
    # Personal jobs still refund the creator's wallet.
    outer = _new_agent("gm-pers")
    _fund(outer["agent_id"], 600)
    pj = db.create_job(outer["token"], "Personal", "mine", 2.0, ["x"])
    bal = _bal(outer["agent_id"])
    db.cancel_job(outer["token"], pj["job_id"])
    assert _bal(outer["agent_id"]) == bal + 40
    # Admin cancel of a commissioned job also routes poolward.
    cos2 = db.request_guild_cosign(founder["token"], gid, "doomed 2", 100)
    db.confirm_guild_cosign(founder["token"], cos2["cosign_id"])
    job2 = db.create_job(
        founder["token"], "Doomed 2", "cancel me", 5.0, ["x"], guild_id=gid
    )
    assert _pool(gid) == 400
    db.admin_cancel_job("admin", job2["job_id"])
    assert _pool(gid) == 500, _pool(gid)


def test_settle_refund_branches():
    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    db.guild_deposit(founder["token"], gid, 25.0)
    job = db.create_job(founder["token"], "Branch", "check", 1.0, ["x"], guild_id=gid)
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job["job_id"],)
        ).fetchone()
        assert db.settle_job_refund(conn, row, 0, "job_cancelled") == "none"
        assert db.settle_job_refund(conn, row, 4, "job_cancelled") == "pool"
    outer = _new_agent("gm-branch")
    _fund(outer["agent_id"], 600)
    pj = db.create_job(outer["token"], "PBranch", "check", 1.0, ["x"])
    with db._conn() as conn:
        prow = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (pj["job_id"],)
        ).fetchone()
        assert db.settle_job_refund(conn, prow, 4, "job_cancelled") == "creator"


def test_disband_zero_and_dissolve():
    founder, guild, mate = _guild_with_mate()
    gid = guild["id"]
    db.guild_deposit(founder["token"], gid, 10.0)
    db.guild_deposit(mate["token"], gid, 5.0)
    try:
        db.disband_guild(founder["token"], gid, mode="zero")
        raise AssertionError("funded zero-disband accepted")
    except Exception as exc:
        assert "dissolve" in str(exc), exc
    try:
        db.disband_guild(mate["token"], gid, mode="dissolve")
        raise AssertionError("non-founder disband accepted")
    except Exception as exc:
        assert "founder" in str(exc), exc
    try:
        db.disband_guild(founder["token"], gid, mode="explode")
        raise AssertionError("bad mode accepted")
    except Exception as exc:
        assert "mode" in str(exc), exc
    # Pool 300, shares F200/M100: founder min(200, 300*200//300=200)=200 fee 4 -> 196;
    # mate min(100, 100*100//300... recompute on lifetime nets.
    f_before = _bal(founder["agent_id"])
    m_before = _bal(mate["agent_id"])
    out = db.disband_guild(founder["token"], gid, mode="dissolve")
    # Founder: 200 - ceil(2%*200)=200-4=196. Mate: 100-2=98. Dust 0 to Treasury.
    assert out["paid"] == {founder["agent_id"]: 196, mate["agent_id"]: 98}, out
    assert _bal(founder["agent_id"]) == f_before + 196
    assert _bal(mate["agent_id"]) == m_before + 98
    assert _pool(gid) == 0
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM guilds WHERE id = ?", (gid,)
        ).fetchone()[0]
        members = conn.execute(
            "SELECT COUNT(*) FROM guild_members WHERE guild_id = ?", (gid,)
        ).fetchone()[0]
    assert status == "disbanded" and members == 0
    # Live commissioned jobs block both modes.
    f2, g2 = _found("Block Guild")
    m2 = _new_agent("gm-blockmate")
    inv = db.invite_guild_member(f2["token"], g2["id"], m2["name"])
    db.respond_guild_invite(m2["token"], inv["invite_id"], True)
    db.guild_deposit(f2["token"], g2["id"], 25.0)
    db.create_job(f2["token"], "Live", "blocks", 2.0, ["x"], guild_id=g2["id"])
    try:
        db.disband_guild(f2["token"], g2["id"], mode="dissolve")
        raise AssertionError("disband with live job accepted")
    except Exception as exc:
        assert "in flight" in str(exc), exc


def test_disband_zero_clean():
    founder, guild = _found("Zero Guild")
    gid = guild["id"]
    out = db.disband_guild(founder["token"], gid, mode="zero")
    assert out["paid"] == {}
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM guilds WHERE id = ?", (gid,)
        ).fetchone()[0]
    assert status == "disbanded"
    # Re-found cooldown starts ticking from this voluntary disband.
    try:
        db.found_guild(founder["token"], "Too Soon Zero")
        raise AssertionError("re-found cooldown not enforced")
    except Exception as exc:
        assert "cooldown" in str(exc), exc


def test_new_table_upgrade():
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS guild_job_links")
    db.init_db()
    with db._conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert "guild_job_links" in tables
    assert "idx_guild_job_links_guild" in indexes


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} guilds-money tests passed")
