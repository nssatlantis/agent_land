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
    # The same DELTA form, on the pre-existing twin of this PR's fee
    # writer: both are shape (a) - paired -guild/+treasury plus a `fee`
    # memo row, no withhold pair. Pinning only the new writer would leave
    # the instance that already ships on main open (finding #138).
    assert _pool(gid) - 500 == _memo(gid) - 500, (_pool(gid), _memo(gid))
    _rule_d(gid)
    print("  sweep travels to treasury: ok")


def test_listing_fee_moves_both_trails_by_the_same_delta():
    """138 - a `fee` memo row must move BOTH readers by the same amount.

    `kind='fee'` is absent from `_INFLOW_KINDS` (db/_guilds.py:36 - deposit,
    grant_t1, grant_t2, subsidy, match, stake, job, bond), so the memo reader
    weights a fee row as -units. This writer is shape (a): the value LEAVES the
    pool, so `spend_from_guild` writes a paired -guild/+treasury leg (the wallet
    drops) and the `fee` memo row drops the memo by the same amount. Rule D
    (`wallet - memo == retained`) therefore holds with NO
    `guild_retain_withhold` pair, and writing one would double-count the
    departure - that primitive is for a WITHHOLDING, where the pool KEEPS the
    value and only the memo is extinguished. `guild_stake`'s placement fee is
    that other shape, and it is why it writes one.

    The existing fee pins assert ABSOLUTE values. Absolute values are the weak
    form: drop the memo row and the wallet still drops, so a lockstep edit
    keeps the two equal and the suite stays green. The delta is the property
    that actually catches it, and it is the one assertion #B211's census found
    nowhere in the repo.

    Driven through the real listing, not `db.spend_from_guild`: that helper
    writes the CREDIT leg only, and the memo row is a separate INSERT inside
    create_service. Calling it directly would exercise one of the two writes
    and pass either way - the mirror of pinning a function while the caller
    supplies an input production never produces.

    The sign is asserted separately, and it is the non-obvious half: the row
    stores POSITIVE units and the READER supplies the minus. Pinning
    `units == -amount` would encode the opposite convention and redden a
    correct writer.
    """
    founder, guild = _found()
    gid = guild["id"]
    # prepare_guild_commission re-locks below 2 members, so a collective
    # listing needs a mate. The mate moves no money, so the trails below are
    # still the founder's deposit alone.
    _add_mate(founder, gid)
    db.guild_deposit(founder["token"], gid, 25.0)
    before_pool, before_memo = _pool(gid), _memo(gid)
    assert before_pool == before_memo == 500, (before_pool, before_memo)

    db.create_service(
        founder["token"],
        "collective listing",
        "shelf space for a member's work",
        1.0,
        steps=["do the thing", "report back"],
        guild_id=gid,
    )
    after_pool, after_memo = _pool(gid), _memo(gid)

    # THE arm: one equality - the one #B211's census shows is absent repo-wide.
    drop_pool = before_pool - after_pool
    drop_memo = before_memo - after_memo
    assert drop_pool == drop_memo, (
        "both readers must move by the SAME delta: wallet "
        f"{before_pool}->{after_pool} (drop {drop_pool}), memo "
        f"{before_memo}->{after_memo} (drop {drop_memo})"
    )
    assert drop_pool > 0, (drop_pool, drop_memo)
    # The value LEFT, so the treasury is the counterparty - not a member.
    with db._conn() as conn:
        t_rows = conn.execute(
            "SELECT COUNT(*) FROM credit_entries WHERE account = 'treasury'"
            " AND reason LIKE 'service_listing_fee%'"
        ).fetchone()[0]
    assert t_rows == 1, t_rows
    with db._conn() as conn:
        row = conn.execute(
            "SELECT kind, units FROM guild_ledger WHERE guild_id = ?"
            " AND kind = 'fee' ORDER BY id DESC LIMIT 1",
            (gid,),
        ).fetchone()
    assert row is not None, "no fee memo row was written"
    assert row["units"] > 0, (
        f"units are stored POSITIVE and the reader negates; got {dict(row)}"
    )
    assert row["units"] == drop_pool, (dict(row), drop_pool)
    # No withhold is owed here, and _rule_d is the house instrument that says
    # so: a spurious retain pair would make retained non-zero and turn
    # wallet - memo == retained red on exactly this guild.
    _rule_d(gid)
    print("  listing fee moves both trails by one delta: ok")


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


def test_verify_ignores_disbanded_with_retained():
    # Review #1344 BLOCKER: the disband waterfall zeroes both trails
    # while append-only retained legs persist - auditing the dead guild
    # would fail 0 - 0 == R forever, so it must be out of scope.
    founder, guild = _found()
    gid = guild["id"]
    _add_mate(founder, gid)
    db.guild_deposit(founder["token"], gid, 25.0)
    db.guild_withdraw(founder["token"], gid, 2.0)
    with db._conn() as conn:
        retained = conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE account = 'guild' AND reason = 'guild_retained'"
            " AND target_type = 'guild' AND target_id = ? AND delta_units > 0",
            (gid,),
        ).fetchone()[0]
    assert retained > 0, retained
    db.disband_guild(founder["token"], gid, "dissolve")
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM guilds WHERE id = ?", (gid,)
        ).fetchone()[0]
    assert status == "disbanded", status
    rep = db._economy.verify_guild_wallets()
    assert rep["ok"], rep
    assert all(r["guild_id"] != gid for r in rep["guilds"]), rep
    print("  verify ignores disbanded with retained: ok")


def test_verify_scopes_suspended_live():
    # Review #1344 BLOCKER companion: suspended pools still hold live
    # custody, so they stay inside the audit scope.
    founder, guild = _found()
    gid = guild["id"]
    _add_mate(founder, gid)
    db.guild_deposit(founder["token"], gid, 5.0)
    with db._conn() as conn:
        conn.execute("UPDATE guilds SET status = 'suspended' WHERE id = ?", (gid,))
    rep = db._economy.verify_guild_wallets()
    assert rep["ok"], rep
    row = [r for r in rep["guilds"] if r["guild_id"] == gid]
    assert len(row) == 1 and row[0]["ok"], rep
    print("  verify scopes suspended live: ok")


def test_backfill_shortfall_skips_without_negative():
    # Review #1344 MED-1: a treasury that cannot cover the whole seed
    # skips (never negative); the live flag stays unset so a later boot
    # retries, and a fundable seed completes afterwards.
    founder, guild = _found()
    gid = guild["id"]
    _add_mate(founder, gid)
    import db._credits as _cr

    with db._conn() as conn:
        treasury = _cr.treasury_balance(conn)
        probe = treasury + 1000
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units, note)"
            " VALUES (?, 'deposit', ?, 'shortfall probe')",
            (gid, probe),
        )
        conn.execute("DELETE FROM economy_meta WHERE key = 'guild_wallet_live'")
    with db._conn() as conn:
        t_before = _cr.treasury_balance(conn)
    out = db._economy.backfill_guild_wallets()
    assert out["backfilled_units"] == 0, out
    assert out["skipped_shortfall_units"] == probe, out
    with db._conn() as conn:
        assert _cr.treasury_balance(conn) == t_before
        assert _pool(gid) == 0
        live = conn.execute(
            "SELECT value FROM economy_meta WHERE key = 'guild_wallet_live'"
        ).fetchone()
    assert live is None or live[0] != "1", live
    with db._conn() as conn:
        conn.execute(
            "DELETE FROM guild_ledger WHERE guild_id = ? AND note = 'shortfall probe'",
            (gid,),
        )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units, note)"
            " VALUES (?, 'deposit', 100, 'fundable probe')",
            (gid,),
        )
        conn.execute("DELETE FROM economy_meta WHERE key = 'guild_wallet_live'")
    out2 = db._economy.backfill_guild_wallets()
    assert out2["backfilled_units"] == 100 and out2["guilds"] == 1, out2
    assert _pool(gid) == 100, _pool(gid)
    assert _memo(gid) == 100, _memo(gid)
    _rule_d(gid)
    print("  backfill shortfall skips without negative: ok")


def _guild_intake():
    with db._conn() as conn:
        return db._economy._summarize_flows(
            db._economy._flow_rows(conn, None),
            db._economy._flow_rows_guild(conn, None),
        )["guild_intake_units"]


def test_backfill_seed_excluded_from_guild_intake():
    # Review #1344 MED-2: the one-time backfill seed is a custody move,
    # not income - the intake bucket must not move when it lands, while
    # a real deposit still lands in it.
    founder, guild = _found()
    gid = guild["id"]
    _add_mate(founder, gid)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units, note)"
            " VALUES (?, 'deposit', 100, 'intake probe')",
            (gid,),
        )
        conn.execute("DELETE FROM economy_meta WHERE key = 'guild_wallet_live'")
    before = _guild_intake()
    out = db._economy.backfill_guild_wallets()
    assert out["backfilled_units"] == 100, out
    assert _guild_intake() == before, (_guild_intake(), before)
    dep = db.guild_deposit(founder["token"], gid, 5.0)
    assert _guild_intake() - before == 100 + dep["fee_units"], _guild_intake()
    _rule_d(gid)
    print("  backfill seed excluded from guild intake: ok")


def test_guild_intake_allowlist_ignores_custody_moves():
    # Review #1344 MED-2, pure-function pin: every positive guild leg
    # outside the external-income allowlist stays out of the bucket -
    # backfill, winnings, refunds, returns, reverts and retention pairs.
    gf = {
        "guild_wallet_backfill": 100,
        "guild_stake_winnings": 50,
        "guild_stake_refund": 40,
        "guild_job_return_intake": 30,
        "guild_taken_wage_intake": 20,
        "guild_disband_cancel_intake": 10,
        "guild_bond_payout_intake": 60,
        "guild_stake_conduit_revert_intake": 70,
        "guild_retained": 5,
        "guild_deposit_intake": 100,
    }
    got = db._economy._summarize_flows({}, gf)["guild_intake_units"]
    assert got == 100, got
    print("  guild intake allowlist ignores custody moves: ok")


def _stray(gid, aid, reason, units):
    """A guild credit_entries leg with no guild_ledger twin.

    That is the real shape behind the divergence this field reports:
    guild_wallet_balance sums credit_entries, guild_memo_balance sums
    guild_ledger, so a leg present in only one of them moves one trail.
    """
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO credit_entries"
            " (agent_id, delta_units, reason, account, target_type, target_id)"
            f" VALUES (?, {units}, ?, 'guild', 'guild', ?)",
            (aid, reason, gid),
        )


def _unstray(gid):
    with db._conn() as conn:
        conn.execute(
            "DELETE FROM credit_entries"
            " WHERE target_type = 'guild' AND target_id = ?"
            " AND reason IN ('guild_retained', 'test_stray_leg')",
            (gid,),
        )


def test_divergence_names_the_two_trails_disagreeing():
    # wallet - memo != 0, and that difference is not the retained figure.
    # This is the live shape: the two trails disagree with EACH OTHER.
    founder, guild = _found()
    gid = guild["id"]
    _add_mate(founder, gid)
    db.guild_deposit(founder["token"], gid, 5.0)
    _stray(gid, founder["agent_id"], "test_stray_leg", 3)
    rep = db._economy.verify_guild_wallets()
    row = [r for r in rep["guilds"] if r["guild_id"] == gid][0]
    assert not row["ok"], row
    assert row["divergence"] == "wallet_vs_memo", row
    assert row["trail_delta_units"] == 3, row
    _unstray(gid)
    assert db._economy.verify_guild_wallets()["ok"]
    _rule_d(gid)
    print("  divergence names the two trails disagreeing: ok")


def test_divergence_names_retained_disagreeing_with_the_trails():
    # The other failure: the two trails agree exactly and the retained
    # figure is what breaks the identity. A lone retained leg cannot
    # reach this arm on its own - it moves the wallet too, so the
    # identity holds - which is why the offsetting stray is here rather
    # than a simpler fixture. This arm is NOT the live instance, so
    # without its own fixture it would ship untested.
    founder, guild = _found()
    gid = guild["id"]
    _add_mate(founder, gid)
    db.guild_deposit(founder["token"], gid, 5.0)
    _stray(gid, founder["agent_id"], "guild_retained", 2)
    _stray(gid, founder["agent_id"], "test_stray_leg", -2)
    rep = db._economy.verify_guild_wallets()
    row = [r for r in rep["guilds"] if r["guild_id"] == gid][0]
    assert not row["ok"], row
    assert row["divergence"] == "retained_vs_trails", row
    assert row["trail_delta_units"] == 0, row
    _unstray(gid)
    assert db._economy.verify_guild_wallets()["ok"]
    _rule_d(gid)
    print("  divergence names retained disagreeing with the trails: ok")


def test_healthy_row_reports_no_divergence_explicitly():
    # The negative control: a green row must SAY so. If the key were
    # absent in the healthy case a reader could not tell "no
    # divergence" from "not computed", which is the same unreadable
    # shape this field exists to remove.
    founder, guild = _found()
    gid = guild["id"]
    _add_mate(founder, gid)
    db.guild_deposit(founder["token"], gid, 5.0)
    rep = db._economy.verify_guild_wallets()
    row = [r for r in rep["guilds"] if r["guild_id"] == gid][0]
    assert row["ok"], row
    assert "divergence" in row, row
    assert row["divergence"] is None, row
    assert row["trail_delta_units"] == 0, row
    _rule_d(gid)
    print("  healthy row reports no divergence explicitly: ok")


if __name__ == "__main__":
    test_deposit_holds_in_wallet()
    test_upkeep_pays_poolward_not_treasury()
    test_sweep_travels_to_treasury()
    test_withdraw_draws_wallet()
    test_multi_guild_isolation()
    test_guild_leg_target_invariant()
    test_backfill_seeds_and_idempotent()
    test_overview_supply_identity()
    test_verify_ignores_disbanded_with_retained()
    test_verify_scopes_suspended_live()
    test_backfill_shortfall_skips_without_negative()
    test_backfill_seed_excluded_from_guild_intake()
    test_guild_intake_allowlist_ignores_custody_moves()
    test_divergence_names_the_two_trails_disagreeing()
    test_divergence_names_retained_disagreeing_with_the_trails()
    test_healthy_row_reports_no_divergence_explicitly()
    test_listing_fee_moves_both_trails_by_the_same_delta()
    print("\n== test_guild_wallet: all passed ==")
