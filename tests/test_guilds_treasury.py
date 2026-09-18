"""Guild↔treasury flows (proposal #525, PR-4): stakes, upkeep, arrears.

Covers the founder-conduit stake variant (pool checks + caps, per-lock
funding, payout split, self-stake redirect, decline refund, withdraw
guard), the weekly upkeep sweep (issue, 48h sweep, suspend/recover,
14d grace disband), fee-invoice payment through pay_invoice, and the
arrears withhold on withdrawals and leave payouts. Stakes/grants/match
treasury-outflow programs beyond this file ride later PRs.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_treasury_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "0"
os.environ["FORUM_INVOICE_MIN_KARMA"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, quarters: int):
    import db._credits as _cr

    with db._conn() as _c:
        ok = _cr.grant(
            agent_id,
            quarters,
            "guild_treasury_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )
    assert ok, "treasury could not fund the test seed"


def _bal(agent_id: int) -> int:
    import db._credits as _cr

    with db._conn() as conn:
        return _cr.balance_for(conn, agent_id)


def _arm(env_key: str, value: str):
    old = os.environ.get(env_key)
    os.environ[env_key] = value
    importlib.reload(config)
    return old


def _unarm(old, env_key: str):
    if old is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = old
    importlib.reload(config)


def _found(name: str | None = None) -> tuple[dict, dict]:
    ag = _new_agent("gt-founder")
    _fund(ag["agent_id"], 120)
    return ag, db.found_guild(ag["token"], name or f"Treasury-{_SEQ[0]}")


def _pool(guild_id: int) -> int:
    with db._conn() as conn:
        return db.guild_balance(conn, guild_id)


def _open_proposal(tag: str) -> int:
    sponsor = _new_agent("gt-sponsor")
    post = db.create_post(sponsor["token"], f"Prop post {tag}", "Body text here.")
    for name in ("beta", "gamma", "delta", "epsilon", "zeta"):
        db.vote(AGENTS[name]["token"], "post", post["post_id"], 1)
    prop = db.create_proposal(sponsor["token"], f"Stake Prop {tag}", "Body")
    pid = prop["post_id"]
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
    return pid


def _rich_guild(pool_cr: float = 25.0) -> tuple[dict, dict, dict]:
    founder, guild = _found()
    mate = _new_agent("gt-mate")
    _fund(mate["agent_id"], 60)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.guild_deposit(founder["token"], guild["id"], pool_cr)
    return founder, guild, mate


def test_tables_upgrade():
    with db._conn() as conn:
        for table in (
            "guild_stake_links",
            "guild_fee_arrears",
            "guild_fee_invoices",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
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
    for table in (
        "guild_stake_links",
        "guild_fee_arrears",
        "guild_fee_invoices",
    ):
        assert table in tables, f"{table} missing after init_db"
    for idx in (
        "idx_guild_stake_links_guild",
        "idx_guild_fee_arrears_member",
        "idx_guild_fee_invoices_guild",
    ):
        assert idx in indexes, f"{idx} missing after init_db"


def test_guild_stake_caps_and_link():
    founder, guild, mate = _rich_guild()  # 100q pool
    gid = guild["id"]
    pid = _open_proposal("cap")
    for bad_pct in (-1, 51):
        try:
            db.guild_stake(founder["token"], pid, 2.5, 2, bonus_pct=bad_pct)
            raise AssertionError(f"bonus {bad_pct} accepted")
        except Exception as exc:
            assert "bonus" in str(exc), exc
    try:
        db.guild_stake(mate["token"], pid, 2.5, 2)
        raise AssertionError("non-founder staked")
    except Exception as exc:
        assert "founder" in str(exc) or "steward" in str(exc), exc
    try:
        # 40q > 33% of 100: single-proposal cap.
        db.guild_stake(founder["token"], pid, 10.0, 1)
        raise AssertionError("single-cap breach accepted")
    except Exception as exc:
        assert "33%" in str(exc), exc
    # Founder cannot cover 20q personally (18 left) - the pool can.
    assert _bal(founder["agent_id"]) < 20
    cos = db.request_guild_cosign(founder["token"], gid, "stake", 20)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    out = db.guild_stake(founder["token"], pid, 2.5, 2, bonus_pct=50)
    assert out["per_pr"] == 10 and out["bonus_pct"] == 50
    with db._conn() as conn:
        link = conn.execute(
            "SELECT * FROM guild_stake_links WHERE stake_id = ?",
            (out["stake_id"],),
        ).fetchone()
        assert link is not None and link["guild_id"] == gid
        assert link["opener_bonus_pct"] == 50
    # Total cap: 20 committed; two more 20s (60) fit under 75, the
    # fourth 20 (80 >= 75) refuses. Each needs its own proposal + cosign.
    for tag in ("t2", "t3"):
        pid_n = _open_proposal(tag)
        cos_n = db.request_guild_cosign(founder["token"], gid, tag, 20)
        db.confirm_guild_cosign(founder["token"], cos_n["cosign_id"])
        db.guild_stake(founder["token"], pid_n, 2.5, 2)
    pid_4 = _open_proposal("t4")
    cos_4 = db.request_guild_cosign(founder["token"], gid, "t4", 20)
    db.confirm_guild_cosign(founder["token"], cos_4["cosign_id"])
    try:
        db.guild_stake(founder["token"], pid_4, 2.5, 2)
        raise AssertionError("total-cap breach accepted")
    except Exception as exc:
        assert "75%" in str(exc), exc
    try:
        db.withdraw_stake(founder["token"], out["stake_id"])
        raise AssertionError("linked withdraw accepted")
    except Exception as exc:
        assert "pool owns" in str(exc), exc


def test_stake_lock_funds_conduit():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    pid = _open_proposal("lock")
    cos = db.request_guild_cosign(founder["token"], gid, "lock", 20)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    db.guild_stake(founder["token"], pid, 2.5, 2)
    f_before = _bal(founder["agent_id"])
    opener = _new_agent("gt-opener")
    locked = db.lock_stakes_for_pr(None, pid, 9101, opener["agent_id"])
    assert locked == 1
    # Pool funded the lock; the conduit nets zero.
    assert _pool(gid) == 100 - 10, _pool(gid)
    assert _bal(founder["agent_id"]) == f_before, (
        _bal(founder["agent_id"]),
        f_before,
    )
    # Relocking the same PR hits the dupe guard: funding reverts, pool
    # and founder both unchanged.
    locked2 = db.lock_stakes_for_pr(None, pid, 9101, opener["agent_id"])
    assert locked2 == 0
    assert _pool(gid) == 90, _pool(gid)
    assert _bal(founder["agent_id"]) == f_before


def test_stake_payout_split_and_self():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    pid = _open_proposal("pay")
    cos = db.request_guild_cosign(founder["token"], gid, "pay", 20)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    db.guild_stake(founder["token"], pid, 2.5, 2, bonus_pct=50)
    opener = _new_agent("gt-winopener")
    db.lock_stakes_for_pr(None, pid, 9102, opener["agent_id"])
    o_before = _bal(opener["agent_id"])
    paid = db.pay_stake_rewards(None, 9102)
    assert paid == 1
    # 10q lock: 5q bonus to opener, 5q pool memo.
    assert _bal(opener["agent_id"]) == o_before + 5
    assert _pool(gid) == 100 - 10 + 5, _pool(gid)
    # Self-stake: founder opens the PR on their own backing - the whole
    # lock returns poolward, never to the conduit wallet.
    pid2 = _open_proposal("selfpay")
    cos2 = db.request_guild_cosign(founder["token"], gid, "selfpay", 20)
    db.confirm_guild_cosign(founder["token"], cos2["cosign_id"])
    db.guild_stake(founder["token"], pid2, 2.5, 1)
    f_before = _bal(founder["agent_id"])
    db.lock_stakes_for_pr(None, pid2, 9103, founder["agent_id"])
    db.pay_stake_rewards(None, 9103)
    assert _bal(founder["agent_id"]) == f_before, (
        _bal(founder["agent_id"]),
        f_before,
    )


def test_stake_refund_to_pool():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    pid = _open_proposal("refund")
    cos = db.request_guild_cosign(founder["token"], gid, "refund", 20)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    db.guild_stake(founder["token"], pid, 2.5, 2)
    opener = _new_agent("gt-refopener")
    db.lock_stakes_for_pr(None, pid, 9104, opener["agent_id"])
    assert _pool(gid) == 90
    f_before = _bal(founder["agent_id"])
    refunded = db.refund_stake_locks(None, 9104)
    assert refunded == 1
    assert _pool(gid) == 100, _pool(gid)
    assert _bal(founder["agent_id"]) == f_before


def test_upkeep_issue_pay_sweep():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    report = db.sweep_guild_upkeep()
    # Global sweep touches every active guild in the shared test DB -
    # assert this guild's share, not the file-wide total.
    assert report["issued"] >= 2, report
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT member_agent_id, quarters, status FROM guild_fee_arrears"
            " WHERE guild_id = ?",
            (gid,),
        ).fetchall()
        invs = conn.execute(
            "SELECT invoice_id, member_agent_id FROM guild_fee_invoices"
            " WHERE guild_id = ?",
            (gid,),
        ).fetchall()
        pings = conn.execute(
            "SELECT agent_id FROM notifications WHERE ref_type = 'invoice'"
            " AND agent_id IN (?, ?)",
            (founder["agent_id"], mate["agent_id"]),
        ).fetchall()
    assert sorted(r[0] for r in rows) == sorted([founder["agent_id"], mate["agent_id"]])
    assert all(r[1] == 1 and r[2] == "open" for r in rows)
    assert len(invs) == 2
    assert {r[0] for r in pings} == {founder["agent_id"], mate["agent_id"]}
    # Idempotent within the week: no second arrears, no second invoice.
    report2 = db.sweep_guild_upkeep()
    assert report2["issued"] == 0, report2
    # Member pays through the standard tool: poolward, arrears settled.
    mate_inv = [i for i in invs if i[1] == mate["agent_id"]][0][0]
    db.accept_invoice(mate["token"], mate_inv)
    m_before = _bal(mate["agent_id"])
    out = db.pay_invoice(mate["token"], mate_inv)
    assert out["status"] == "paid"
    assert _bal(mate["agent_id"]) == m_before - 1
    assert _pool(gid) == 100 + 1, _pool(gid)
    with db._conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM guild_fee_arrears WHERE guild_id = ?"
            " AND member_agent_id = ? AND status = 'open'",
            (gid, mate["agent_id"]),
        ).fetchone()[0]
    assert left == 0
    # 48h later the sweep takes min(5q, members) and stamps the week.
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET created_at = ? WHERE id IN (SELECT invoice_id"
            " FROM guild_fee_invoices WHERE guild_id = ?)",
            ("2020-01-01T00:00:00.000Z", gid),
        )
    report3 = db.sweep_guild_upkeep()
    assert report3["swept"].get(gid) == 2, report3
    assert _pool(gid) == 99, _pool(gid)
    with db._conn() as conn:
        week = conn.execute(
            "SELECT last_upkeep_week FROM guilds WHERE id = ?", (gid,)
        ).fetchone()[0]
    assert week is not None
    report4 = db.sweep_guild_upkeep()
    assert report4["swept"] == {} and report4["issued"] == 0, report4


def test_upkeep_suspend_recover_grace():
    founder, guild, mate = _rich_guild(pool_cr=0.25)  # 1q pool
    gid = guild["id"]
    db.sweep_guild_upkeep()
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET created_at = ? WHERE id IN (SELECT invoice_id"
            " FROM guild_fee_invoices WHERE guild_id = ?)",
            ("2020-01-01T00:00:00.000Z", gid),
        )
    # Pool 1q < due 2q: suspend, no sweep.
    report = db.sweep_guild_upkeep()
    assert report["suspended"] == [gid], report
    assert report["swept"] == {}
    with db._conn() as conn:
        flag = conn.execute(
            "SELECT spending_suspended FROM guilds WHERE id = ?", (gid,)
        ).fetchone()[0]
    assert flag == 1
    # Locked guild refuses spends but still takes deposits.
    try:
        db.guild_withdraw(founder["token"], gid, 0.25)
        raise AssertionError("suspended withdrawal accepted")
    except Exception as exc:
        assert "suspended" in str(exc), exc
    db.guild_deposit(mate["token"], gid, 2.5)  # +10q: pool 11
    report2 = db.sweep_guild_upkeep()
    assert report2["recovered"] == [gid], report2
    assert report2["swept"].get(gid) == 2, report2
    with db._conn() as conn:
        flag2 = conn.execute(
            "SELECT spending_suspended FROM guilds WHERE id = ?", (gid,)
        ).fetchone()[0]
    assert flag2 == 0
    # Grace lapse with no recovery disbands.
    with db._conn() as conn:
        conn.execute(
            "UPDATE guilds SET spending_suspended = 1, suspended_at = ?,"
            " last_upkeep_week = NULL WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", gid),
        )
        conn.execute("DELETE FROM guild_ledger WHERE guild_id = ?", (gid,))
        conn.execute(
            "UPDATE invoices SET created_at = ? WHERE id IN (SELECT invoice_id"
            " FROM guild_fee_invoices WHERE guild_id = ?)",
            ("2020-01-01T00:00:00.000Z", gid),
        )
    report3 = db.sweep_guild_upkeep()
    assert report3["disbanded"] == [gid], report3
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM guilds WHERE id = ?", (gid,)
        ).fetchone()[0]
    assert status == "disbanded"


def test_arrears_withhold_on_payouts():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    db.sweep_guild_upkeep()  # 1q arrears each, no payment
    # Withdrawal reduced by the founder's arrears, arrears settled.
    cos = db.request_guild_cosign(founder["token"], gid, "wd", 20)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    out = db.guild_withdraw(founder["token"], gid, 5.0)
    # 20q share - 1q arrears = 19q, fee ceil(2%*19)=1 -> 18q.
    assert out["paid_quarters"] == 18, out
    assert out["arrears_withheld"] == 1, out
    with db._conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM guild_fee_arrears WHERE guild_id = ?"
            " AND member_agent_id = ? AND status = 'open'",
            (gid, founder["agent_id"]),
        ).fetchone()[0]
    assert left == 0
    # Leave payout reduced the same way.
    m_before = _bal(mate["agent_id"])
    left_out = db.leave_guild(mate["token"], gid)
    # Mate net 0 (never deposited): nothing to withhold from, nothing paid.
    assert left_out["paid_quarters"] == 0
    assert _bal(mate["agent_id"]) == m_before


def test_suspended_blocks_spends_not_deposits():
    founder, guild, mate = _rich_guild()  # 100q pool already
    gid = guild["id"]
    with db._conn() as conn:
        conn.execute(
            "UPDATE guilds SET spending_suspended = 1, suspended_at = ? WHERE id = ?",
            ("2026-09-17T00:00:00.000Z", gid),
        )
    pid = _open_proposal("susp")
    try:
        db.create_job(founder["token"], "Blocked", "nope", 1.0, ["x"], guild_id=gid)
        raise AssertionError("suspended commission accepted")
    except Exception as exc:
        assert "suspended" in str(exc), exc
    try:
        db.guild_stake(founder["token"], pid, 2.5, 1)
        raise AssertionError("suspended stake accepted")
    except Exception as exc:
        assert "suspended" in str(exc), exc
    # Inflows still legal.
    db.guild_deposit(mate["token"], gid, 1.0)
    assert _pool(gid) == 104, _pool(gid)


def test_conservation_per_lifecycle():
    """Every terminal path nets zero across treasury, supply, founder,
    and pool: fund+lock then decline/win/self must leave no hole."""
    import db._credits as _cr

    def snapshot():
        with db._conn() as conn:
            return (
                _cr.treasury_balance(conn),
                conn.execute(
                    "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
                ).fetchone()[0],
            )

    # Decline path: full round trip nets zero everywhere.
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    t0, s0 = snapshot()
    f0 = _bal(founder["agent_id"])
    pid = _open_proposal("consdecline")
    cos = db.request_guild_cosign(founder["token"], gid, "c", 20)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    db.guild_stake(founder["token"], pid, 2.5, 2)
    opener = _new_agent("gt-consopener")
    db.lock_stakes_for_pr(None, pid, 9201, opener["agent_id"])
    assert _pool(gid) == 90, _pool(gid)
    db.refund_stake_locks(None, 9201)
    assert _pool(gid) == 100, _pool(gid)
    assert _bal(founder["agent_id"]) == f0
    t1, s1 = snapshot()
    assert (t1, s1) == (t0, s0), ((t0, s0), (t1, s1))
    # Win path with 50% bonus: treasury funds exactly the bonus, the
    # pool keeps the rest, founder nets zero.
    pid2 = _open_proposal("conswin")
    cos2 = db.request_guild_cosign(founder["token"], gid, "c2", 20)
    db.confirm_guild_cosign(founder["token"], cos2["cosign_id"])
    db.guild_stake(founder["token"], pid2, 2.5, 2, bonus_pct=50)
    opener2 = _new_agent("gt-consopener2")
    db.lock_stakes_for_pr(None, pid2, 9202, opener2["agent_id"])
    o_before = _bal(opener2["agent_id"])
    t2, s2 = snapshot()
    db.pay_stake_rewards(None, 9202)
    assert _bal(opener2["agent_id"]) == o_before + 5
    assert _pool(gid) == 100 - 10 + 5, _pool(gid)
    assert _bal(founder["agent_id"]) == f0
    # Bonus minted to opener (+5 supply), pool share minted back to the
    # treasury (+5): the lock's burn is exactly unwound.
    t3, s3 = snapshot()
    assert t3 == t2 + 5 and s3 == s2 + 10, ((t2, s2), (t3, s3))


def test_broke_founder_dupe_undo():
    """A conduit founder staking beyond personal means survives a
    double-lock: the dupe undo claws back only after the lock debit is
    reverted, so the batch never aborts on an empty wallet. per_pr 20q
    with 14q personal balance exercises exactly that."""
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    assert _bal(founder["agent_id"]) == 14
    pid = _open_proposal("dupebroke")
    cos = db.request_guild_cosign(founder["token"], gid, "d", 20)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    db.guild_stake(founder["token"], pid, 5.0, 1)
    opener = _new_agent("gt-dupeopener")
    assert db.lock_stakes_for_pr(None, pid, 9203, opener["agent_id"]) == 1
    assert _pool(gid) == 80, _pool(gid)
    assert db.lock_stakes_for_pr(None, pid, 9203, opener["agent_id"]) == 0
    assert _pool(gid) == 80, _pool(gid)
    assert _bal(founder["agent_id"]) == 14
    with db._conn() as conn:
        locks = conn.execute(
            "SELECT COUNT(*) FROM stake_locks WHERE pr_number = 9203"
            " AND status = 'locked'"
        ).fetchone()[0]
    assert locks == 1


def test_guard_path_undo():
    """A lock landing on a just-completed stake rolls everything back:
    no lock row, pool and founder untouched."""
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    pid = _open_proposal("guardpath")
    db.guild_stake(founder["token"], pid, 2.5, 1)
    opener = _new_agent("gt-guardopener")
    db.lock_stakes_for_pr(None, pid, 9204, opener["agent_id"])
    db.pay_stake_rewards(None, 9204)
    f_before = _bal(founder["agent_id"])
    # max_prs=1 is now fully paid; a second lock hits the guard path.
    db.lock_stakes_for_pr(None, pid, 9205, opener["agent_id"])
    assert _pool(gid) == 100 - 10 + 10, _pool(gid)
    assert _bal(founder["agent_id"]) == f_before
    with db._conn() as conn:
        locks = conn.execute(
            "SELECT COUNT(*) FROM stake_locks WHERE pr_number = 9205"
        ).fetchone()[0]
    assert locks == 0


def test_admin_delete_linked_guarded():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    pid = _open_proposal("admindel")
    cos = db.request_guild_cosign(founder["token"], gid, "a", 20)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    out = db.guild_stake(founder["token"], pid, 2.5, 2)
    opener = _new_agent("gt-adminopener")
    db.lock_stakes_for_pr(None, pid, 9206, opener["agent_id"])
    try:
        db.admin_delete_stake("admin", out["stake_id"])
        raise AssertionError("admin delete with locks accepted")
    except Exception as exc:
        assert "locks in flight" in str(exc), exc
    db.refund_stake_locks(None, 9206)
    gone = db.admin_delete_stake("admin", out["stake_id"])
    assert gone["status"] == "withdrawn" if "status" in gone else True


def test_fee_bill_pool_refusal():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    db.sweep_guild_upkeep()
    with db._conn() as conn:
        inv = conn.execute(
            "SELECT invoice_id FROM guild_fee_invoices WHERE guild_id = ?"
            " AND member_agent_id = ?",
            (gid, founder["agent_id"]),
        ).fetchone()[0]
    db.accept_invoice(founder["token"], inv)
    try:
        db.guild_pay_invoice(founder["token"], inv)
        raise AssertionError("fee bill paid from pool")
    except Exception as exc:
        assert "arrears" in str(exc) or "upkeep" in str(exc), exc
    # The documented path settles personally and clears arrears.
    db.pay_invoice(founder["token"], inv)
    with db._conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM guild_fee_arrears WHERE guild_id = ?"
            " AND member_agent_id = ? AND status = 'open'",
            (gid, founder["agent_id"]),
        ).fetchone()[0]
    assert left == 0


def test_dead_proposal_release_and_supersede():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    pid = _open_proposal("deadprop")
    cos = db.request_guild_cosign(founder["token"], gid, "d", 20)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    out = db.guild_stake(founder["token"], pid, 2.5, 2)
    # Supersede auto-releases the linked stake with no money moving.
    db.supersede_proposal(_sponsor_token(pid), pid, "Stake Prop deadprop v2", "Body v2")
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (out["stake_id"],),
        ).fetchone()[0]
    assert status == "refunded", status
    # A declined (dead, unlocked) proposal's stake releases via withdraw:
    # force the status read dead (merge/decline machinery is poller-side)
    # while the row stays active with zero locks.
    pid2 = _open_proposal("deadprop2")
    cos2 = db.request_guild_cosign(founder["token"], gid, "d2", 20)
    db.confirm_guild_cosign(founder["token"], cos2["cosign_id"])
    out2 = db.guild_stake(founder["token"], pid2, 2.5, 1)
    import db._proposal_status as _status_mod

    real_status = _status_mod._proposal_status_for
    _status_mod._proposal_status_for = lambda conn, pid: "merged"
    try:
        rel = db.withdraw_stake(founder["token"], out2["stake_id"])
    finally:
        _status_mod._proposal_status_for = real_status
    assert rel["stake_id"] == out2["stake_id"]
    assert rel["uncommitted_total"] == 10, rel
    with db._conn() as conn:
        status2 = conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (out2["stake_id"],),
        ).fetchone()[0]
    assert status2 == "withdrawn", status2


def _sponsor_token(pid: int) -> str:
    with db._conn() as conn:
        author = conn.execute(
            "SELECT agent_id FROM posts WHERE id = ?", (pid,)
        ).fetchone()[0]
    with db._conn() as conn:
        tok = conn.execute(
            "SELECT token FROM agents WHERE id = ?", (author,)
        ).fetchone()[0]
    return tok


def test_sweep_isolation_poisoned_guild():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    db.sweep_guild_upkeep()
    # Poisoned guild: grace lapsed with a payable share behind it, so
    # its disband grant cannot land under CREDITS_ENABLED=0. Three
    # members (due 3), pool 2, holder net 2 with 1q arrears: payout 2,
    # withhold 1, net grant 1 -> refused -> skip. (Smaller pools withhold
    # to exactly zero and disband cleanly - also correct, just unpinned.)
    poison_f, poison_g = _found("Poison Guild")
    pgid = poison_g["id"]
    for tag in ("pm1", "pm2"):
        pm = _new_agent(f"gt-{tag}")
        inv = db.invite_guild_member(poison_f["token"], pgid, pm["name"])
        db.respond_guild_invite(pm["token"], inv["invite_id"], True)
    holder = _new_agent("gt-pholder")
    _fund(holder["agent_id"], 10)
    inv_h = db.invite_guild_member(poison_f["token"], pgid, holder["name"])
    db.respond_guild_invite(holder["token"], inv_h["invite_id"], True)
    db.guild_deposit(holder["token"], pgid, 0.5)
    db.sweep_guild_upkeep()
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET created_at = ? WHERE id IN (SELECT invoice_id"
            " FROM guild_fee_invoices WHERE guild_id IN (?, ?))",
            ("2020-01-01T00:00:00.000Z", gid, pgid),
        )
        conn.execute(
            "UPDATE guilds SET spending_suspended = 1, suspended_at = ?,"
            " last_upkeep_week = NULL WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", pgid),
        )
        conn.execute("UPDATE guilds SET last_upkeep_week = NULL WHERE id = ?", (gid,))
    old = _arm("FORUM_CREDITS_ENABLED", "0")
    try:
        report = db.sweep_guild_upkeep()
        # Healthy guild sweeps through (memos only, no grants needed);
        # the poisoned one skips without aborting the tick.
        assert report["swept"].get(gid) == 2, report
        assert pgid in report["skipped"], report
        with db._conn() as conn:
            status = conn.execute(
                "SELECT status FROM guilds WHERE id = ?", (pgid,)
            ).fetchone()[0]
            flag = conn.execute(
                "SELECT spending_suspended FROM guilds WHERE id = ?", (pgid,)
            ).fetchone()[0]
        assert status == "active" and flag == 1
    finally:
        _unarm(old, "FORUM_CREDITS_ENABLED")


def test_bonus_zero_and_tracker():
    """Two guild stakes on one proposal lock together on one PR (the
    running tracker funds both exactly), and a zero bonus pays the pool
    whole with the opener untouched."""
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    pid = _open_proposal("bonusz")
    # Two 10q stakes on one proposal (20 total - inside the 33% single
    # cap, inside the solo band so no co-sign): both must lock on one PR
    # with the running tracker funding each exactly once.
    db.guild_stake(founder["token"], pid, 2.5, 1, bonus_pct=0)
    db.guild_stake(founder["token"], pid, 2.5, 1, bonus_pct=0)
    opener = _new_agent("gt-bonusopener")
    assert db.lock_stakes_for_pr(None, pid, 9207, opener["agent_id"]) == 2
    assert _pool(gid) == 100 - 20, _pool(gid)
    assert _bal(founder["agent_id"]) == 14
    o_before = _bal(opener["agent_id"])
    db.pay_stake_rewards(None, 9207)
    assert _bal(opener["agent_id"]) == o_before
    assert _pool(gid) == 100, _pool(gid)


def test_disband_voids_stranded_arrears():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    db.sweep_guild_upkeep()  # both members owe 1q
    out = db.disband_guild(founder["token"], gid, mode="dissolve")
    assert out["mode"] == "dissolve"
    with db._conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM guild_fee_arrears WHERE guild_id = ?"
            " AND status = 'open'",
            (gid,),
        ).fetchone()[0]
        voided = conn.execute(
            "SELECT COUNT(*) FROM guild_fee_arrears WHERE guild_id = ?"
            " AND status = 'void'",
            (gid,),
        ).fetchone()[0]
    # Founder paid through withhold (settled); zero-net mate voids.
    assert left == 0 and voided == 1, (left, voided)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} guilds-treasury tests passed")
