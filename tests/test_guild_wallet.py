"""Guild wallet pins (proposal #611): per-guild custody in credit_entries.

Every guild keeps its own balance (account='guild' legs targeting their
guild); upkeep pays member -> guild (not Treasury); the sweep travels
-guild/+treasury paired; Rule-D (wallet - memo == retained) holds; the
backfill seeds pre-wallet pools supply-neutrally and is idempotent.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guild_wallet_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()

_SEQ = [0]
_OLD_JOIN = "2020-01-01T00:00:00.000Z"


def _new_agent(prefix):
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(aid, units):
    import db._credits as _cr

    with db._conn() as _c:
        assert _cr.grant(aid, units, "wallet_seed", conn=_c)


def _found(name=None):
    ag = _new_agent("wallet-founder")
    _fund(ag["agent_id"], 600)
    return ag, db.found_guild(ag["token"], name or f"Wallet-{_SEQ[0]}")


def _add_mate(founder, gid):
    mate = _new_agent("wallet-mate")
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], gid, mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    return mate


def _pool(gid):
    with db._conn() as conn:
        return db.guild_balance(conn, gid)


def _memo(gid):
    with db._conn() as conn:
        return db.guild_memo_balance(conn, gid)


def _age_guild(gid):
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_members SET joined_at = ? WHERE guild_id = ?",
            (_OLD_JOIN, gid),
        )


def _rule_d(gid):
    rep = db._economy.verify_guild_wallets()
    assert rep["ok"], rep
    row = [r for r in rep["guilds"] if r["guild_id"] == gid][0]
    assert row["ok"], row
    return row


def test_deposit_holds_in_wallet():
    founder, guild = _found()
    gid = guild["id"]
    mate = _add_mate(founder, gid)
    assert mate["agent_id"] != founder["agent_id"]
    db.guild_deposit(founder["token"], gid, 25.0)
    assert _pool(gid) == 500, _pool(gid)
    assert _memo(gid) == 500, _memo(gid)
    with db._conn() as conn:
        legs = conn.execute(
            "SELECT account, delta_units, target_type, target_id, reason"
            " FROM credit_entries WHERE account = 'guild'"
            " AND target_type = 'guild' AND target_id = ?",
            (gid,),
        ).fetchall()
    assert sum(r[1] for r in legs) == 500, [dict(r) for r in legs]
    assert any(r[4] == "guild_deposit_intake" for r in legs)
    _rule_d(gid)
    print("  deposit holds in wallet: ok")


def test_upkeep_pays_poolward_not_treasury():
    founder, guild = _found()
    gid = guild["id"]
    mate = _add_mate(founder, gid)
    _age_guild(gid)
    db.sweep_guild_upkeep()
    with db._conn() as conn:
        invs = conn.execute(
            "SELECT invoice_id, member_agent_id FROM guild_fee_invoices"
            " WHERE guild_id = ?",
            (gid,),
        ).fetchall()
    assert len(invs) == 2, [dict(r) for r in invs]
    by_member = {r[1]: r[0] for r in invs}
    db.accept_invoice(founder["token"], by_member[founder["agent_id"]])
    assert (
        db.pay_invoice(founder["token"], by_member[founder["agent_id"]])["status"]
        == "paid"
    )
    db.accept_invoice(mate["token"], by_member[mate["agent_id"]])
    assert (
        db.pay_invoice(mate["token"], by_member[mate["agent_id"]])["status"] == "paid"
    )
    assert _pool(gid) == 10, _pool(gid)
    assert _memo(gid) == 10, _memo(gid)
    # Guild-scoped history: per-agent views hide the counterparty leg,
    # so the agent -> Guild #gid grouping is asserted pool-side.
    pool_hist = db.credit_history(guild_id=gid, limit=10)
    grouped = db.group_transactions(pool_hist["entries"])
    assert any(g["to_name"] == f"Guild #{gid}" for g in grouped), grouped
    _rule_d(gid)
    print("  upkeep pays poolward: ok")


def test_sweep_travels_to_treasury():
    founder, guild = _found()
    gid = guild["id"]
    mate = _add_mate(founder, gid)
    assert mate["agent_id"] != founder["agent_id"]
    db.guild_deposit(founder["token"], gid, 25.0)
    _age_guild(gid)
    db.sweep_guild_upkeep()
    with db._conn() as conn:
        invs = conn.execute(
            "SELECT invoice_id FROM guild_fee_invoices WHERE guild_id = ?", (gid,)
        ).fetchall()
        for (inv_id,) in invs:
            conn.execute(
                "UPDATE invoices SET created_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00.000Z", inv_id),
            )
    import db._credits as _cr

    with db._conn() as conn:
        t_before = _cr.treasury_balance(conn)
    report = db.sweep_guild_upkeep()
    assert report["swept"].get(gid) == 10, report
    assert _pool(gid) == 490, _pool(gid)
    assert _memo(gid) == 490, _memo(gid)
    with db._conn() as conn:
        t_after = _cr.treasury_balance(conn)
    assert t_after - t_before == 10, (t_before, t_after)
    _rule_d(gid)
    print("  sweep travels to treasury: ok")


def test_withdraw_draws_wallet():
    founder, guild = _found()
    gid = guild["id"]
    _add_mate(founder, gid)
    db.guild_deposit(founder["token"], gid, 25.0)
    with db._conn() as conn:
        import db._credits as _cr

        f_before = _cr.balance_for(conn, founder["agent_id"])
    out = db.guild_withdraw(founder["token"], gid, 2.0)
    assert out["paid_units"] == 39, out
    assert out["fee_units"] == 1, out
    with db._conn() as conn:
        import db._credits as _cr

        assert _cr.balance_for(conn, founder["agent_id"]) == f_before + 39
    assert _pool(gid) == 461, _pool(gid)
    assert _memo(gid) == 460, _memo(gid)
    row = _rule_d(gid)
    assert row["retained_units"] == 1, row
    print("  withdraw draws wallet: ok")


def test_multi_guild_isolation():
    f1, g1 = _found("Iso-A")
    m1 = _add_mate(f1, g1["id"])
    f2, g2 = _found("Iso-B")
    _add_mate(f2, g2["id"])
    assert m1["agent_id"] != f1["agent_id"]
    db.guild_deposit(f1["token"], g1["id"], 25.0)
    db.guild_deposit(f2["token"], g2["id"], 10.0)
    assert _pool(g1["id"]) == 500, _pool(g1["id"])
    assert _pool(g2["id"]) == 200, _pool(g2["id"])
    db.guild_withdraw(f1["token"], g1["id"], 2.0)
    assert _pool(g1["id"]) == 461, _pool(g1["id"])
    assert _pool(g2["id"]) == 200, _pool(g2["id"])
    ov = db.economy_overview()
    assert ov["held_in_guild_pools_units"] >= 660, ov["held_in_guild_pools_units"]
    rep = db._economy.verify_guild_wallets()
    assert rep["ok"], rep
    by_id = {r["guild_id"]: r for r in rep["guilds"]}
    assert by_id[g1["id"]]["ok"] and by_id[g2["id"]]["ok"], rep
    print("  multi-guild isolation: ok")


def test_guild_leg_target_invariant():
    founder, guild = _found()
    gid = guild["id"]
    _add_mate(founder, gid)
    db.guild_deposit(founder["token"], gid, 5.0)
    with db._conn() as conn:
        bad = conn.execute(
            "SELECT id, target_type, target_id FROM credit_entries"
            " WHERE account = 'guild'"
            " AND (target_type IS NULL OR target_type != 'guild'"
            " OR target_id IS NULL)",
        ).fetchall()
    assert bad == [], [dict(r) for r in bad]
    print("  guild leg target invariant: ok")


def test_backfill_seeds_and_idempotent():
    founder, guild = _found()
    gid = guild["id"]
    _add_mate(founder, gid)
    db.guild_deposit(founder["token"], gid, 5.0)
    assert _pool(gid) == 100, _pool(gid)
    with db._conn() as conn:
        conn.execute(
            "DELETE FROM credit_entries WHERE account = 'guild'"
            " AND target_type = 'guild' AND target_id = ?",
            (gid,),
        )
        conn.execute(
            "DELETE FROM economy_meta WHERE key = 'guild_wallet_live'",
        )
    assert _pool(gid) == 0, _pool(gid)
    import db._credits as _cr

    with db._conn() as conn:
        supply_before = conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
        ).fetchone()[0]
        t_before = _cr.treasury_balance(conn)
    out = db._economy.backfill_guild_wallets()
    assert not out["already_live"], out
    assert _pool(gid) == 100, _pool(gid)
    with db._conn() as conn:
        supply_after = conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
        ).fetchone()[0]
        t_after = _cr.treasury_balance(conn)
    assert supply_after == supply_before, (supply_before, supply_after)
    assert t_before - t_after == 100, (t_before, t_after)
    out2 = db._economy.backfill_guild_wallets()
    assert out2["already_live"] and out2["backfilled_units"] == 0, out2
    _rule_d(gid)
    print("  backfill seeds and idempotent: ok")


def test_overview_supply_identity():
    ov = db.economy_overview()
    assert ov["conservation"]["ok"], ov["conservation"]
    assert ov["guild_conservation"]["ok"], ov["guild_conservation"]
    assert ov["total_supply_units"] == (
        ov["treasury_units"]
        + ov["circulating_units"]
        + ov["held_in_job_escrow_units"]
        + ov["held_in_guild_pools_units"]
        + ov["held_in_bond_escrow_units"]
    ), {
        k: ov[k]
        for k in (
            "total_supply_units",
            "treasury_units",
            "circulating_units",
            "held_in_job_escrow_units",
            "held_in_guild_pools_units",
            "held_in_bond_escrow_units",
        )
    }
    assert "guild_outflows_units" in ov["flows"]["day"], ov["flows"]["day"]
    print("  overview supply identity: ok")


if __name__ == "__main__":
    test_deposit_holds_in_wallet()
    test_upkeep_pays_poolward_not_treasury()
    test_sweep_travels_to_treasury()
    test_withdraw_draws_wallet()
    test_multi_guild_isolation()
    test_guild_leg_target_invariant()
    test_backfill_seeds_and_idempotent()
    test_overview_supply_identity()
    print("\n== test_guild_wallet: all passed ==")
