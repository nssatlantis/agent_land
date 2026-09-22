"""Tests for Term Savings Bonds (proposal #552): escrow-paired buys keep
supply fixed, buy fees ride their own excluded reason, same-day buys
accrue zero (bond-day weighting), distribution math is exact with
remainder carryover, dry treasuries hold maturities, early redemption
takes the haircut, forfeiture releases-then-splits, sweeps are
idempotent, caps refuse, and boot creates the tables.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bonds_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402,I001

db.init_db()

AGENTS, BASE_POST = setup()

from db._bonds import (  # noqa: E402
    _trailing_fee_intake_units,
    bond_series_open,
    buy_bond,
    list_bond_series,
    my_bonds,
    sweep_bond_day,
)
from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(200000, "test_suite_topup", admin="test-suite", conn=_c)


def _make_holder(name: str, seed_units: int = 4000):
    ag = db.register_agent(name)
    with db._conn() as conn:
        from db._credits import grant

        grant(ag["agent_id"], seed_units, "test_seed", conn=conn)
    return ag


def _supply() -> int:
    with db._conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries",
        ).fetchone()[0]


def _escrow() -> int:
    with db._conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE account = 'escrow'",
        ).fetchone()[0]


def _bal(agent_id: int) -> int:
    from db._credits import balance_for

    with db._conn() as c:
        return balance_for(c, agent_id)


def _arm_fee(pct: float):
    old = config.TX_FEE_PERCENT
    config.TX_FEE_PERCENT = float(pct)
    return old


def _unarm_fee(old):
    config.TX_FEE_PERCENT = old


def _reset_sweep_day():
    with db._conn(immediate=True) as c:
        c.execute("DELETE FROM economy_meta WHERE key = 'bond_last_sweep_day'")


def _backdate(bid: int, bought: str | None = None, matures: str | None = None):
    with db._conn(immediate=True) as c:
        if bought is not None:
            c.execute(
                "UPDATE treasury_bonds SET bought_at = ? WHERE id = ?",
                (bought, bid),
            )
        if matures is not None:
            c.execute(
                "UPDATE treasury_bonds SET matures_at = ? WHERE id = ?",
                (matures, bid),
            )


def _base_now() -> int:
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    ) + ".000Z"
    with db._conn() as conn:
        return _trailing_fee_intake_units(conn, since)


def test_accrual_math_exact_and_same_day_zero():
    holder = _make_holder("bd-accrue")
    peer = _make_holder("bd-accrue-peer")
    saved = _arm_fee(10.0)
    try:
        sid = bond_series_open("accrue-7", 7)["series_id"]
        db.transfer_credits(holder["agent_id"], peer["agent_id"], 1000)
        old = buy_bond(holder["token"], sid, 10.0)
        _backdate(old["bond_id"], bought="2020-01-01T00:00:00.000Z")
        new = buy_bond(holder["token"], sid, 10.0)
        base = _base_now()
        assert base == 100, base
        _reset_sweep_day()
        out = sweep_bond_day()
        assert out["swept"] is True
        pool = int(base * 15.0 / 700)
        assert pool == 2, pool
        got_old = my_bonds(holder["token"])["bonds"]
        acc_old = [b for b in got_old if b["id"] == old["bond_id"]][0]
        acc_new = [b for b in got_old if b["id"] == new["bond_id"]][0]
        assert acc_old["accrued_units"] == 2, acc_old
        assert acc_new["accrued_units"] == 0, acc_new
    finally:
        _unarm_fee(saved)
    assert db.economy_overview()["conservation"]["ok"] is True


def test_zz_boot_creates_tables():
    # Runs last by name: DROP resets the bond id sequence while ledger
    # legs persist, so earlier target_id-scoped assertions need stable ids.
    with db._conn(immediate=True) as c:
        c.execute("DROP TABLE IF EXISTS treasury_bonds")
        c.execute("DROP TABLE IF EXISTS bond_series")
    db.init_db()
    assert list_bond_series() == []
    holder = _make_holder("bd-boot")
    sid = bond_series_open("boot-7", 7)["series_id"]
    out = buy_bond(holder["token"], sid, 1.0)
    assert out["face_units"] == 20


def test_buy_pairs_legs_supply_neutral():
    holder = _make_holder("bd-buy")
    sid = bond_series_open("buy-7", 7)["series_id"]
    s0, e0 = _supply(), _escrow()
    out = buy_bond(holder["token"], sid, 10.0)
    assert out["face_units"] == 200
    assert out["fee_units"] == 0
    assert _supply() == s0, "parking principal never moves supply"
    assert _escrow() == e0 + 200
    with db._conn() as conn:
        legs = conn.execute(
            "SELECT account, delta_units FROM credit_entries"
            " WHERE reason IN ('bond_principal', 'bond_principal_held')"
            " AND target_id = ? ORDER BY id",
            (out["bond_id"],),
        ).fetchall()
    assert [(r["account"], r["delta_units"]) for r in legs] == [
        ("agent", -200),
        ("escrow", 200),
    ]
    assert db.economy_overview()["conservation"]["ok"] is True


def test_buy_fee_excluded_from_yield_base():
    holder = _make_holder("bd-fee")
    sid = bond_series_open("fee-7", 7)["series_id"]
    saved = _arm_fee(10.0)
    try:
        before = _base_now()
        out = buy_bond(holder["token"], sid, 10.0)
        assert out["fee_units"] == 20, out
        with db._conn() as conn:
            legs = conn.execute(
                "SELECT account, delta_units, reason FROM credit_entries"
                " WHERE target_id = ? AND reason LIKE 'bond_buy_fee%'"
                " ORDER BY id",
                (out["bond_id"],),
            ).fetchall()
        assert [(r["account"], r["delta_units"]) for r in legs] == [
            ("agent", -20),
            ("treasury", 20),
        ]
        assert _base_now() == before, "buy fees never fund the yield base"
    finally:
        _unarm_fee(saved)


def test_caps_and_closed_refuse():
    holder = _make_holder("bd-caps")
    sid = bond_series_open(
        "caps-7", 7, series_cap_credits=2.0, citizen_cap_credits=2.0
    )["series_id"]
    buy_bond(holder["token"], sid, 2.0)
    msg = expect_error(buy_bond, holder["token"], sid, 1.0)
    assert "cap" in msg, msg
    msg = expect_error(buy_bond, holder["token"], sid, 0.5)
    assert "at least" in msg, msg
    from db._bonds import bond_series_close

    bond_series_close(sid)
    msg = expect_error(buy_bond, holder["token"], sid, 1.0)
    assert "closed" in msg, msg


def test_carryover_holds_remainder():
    holder = _make_holder("bd-carry")
    sid = bond_series_open("carry-7", 7)["series_id"]
    saved = _arm_fee(10.0)
    try:
        peer = _make_holder("bd-carry-peer")
        db.transfer_credits(holder["agent_id"], peer["agent_id"], 1500)
        b1 = buy_bond(holder["token"], sid, 10.0)
        b2 = buy_bond(holder["token"], sid, 10.0)
        _backdate(b1["bond_id"], bought="2020-01-01T00:00:00.000Z")
        _backdate(b2["bond_id"], bought="2020-01-01T00:00:00.000Z")
        # Expectation mirrors the sweep's clamp (proposal #604): only
        # post-open intake funds this series, so earlier tests' leftover
        # intake never leaks in - hermetic by construction, not by slate.
        with db._conn() as conn:
            opened = conn.execute(
                "SELECT created_at FROM bond_series WHERE id = ?", (sid,)
            ).fetchone()[0]
            base = _trailing_fee_intake_units(conn, max(_since_7d(), opened))
        assert base == 150, base
        pool = int(base * 15.0 / 700)
        assert pool == 3, pool
        _reset_sweep_day()
        sweep_bond_day()
        with db._conn() as conn:
            carry = conn.execute(
                "SELECT value FROM economy_meta WHERE key = ?",
                (f"bond_carry_{sid}",),
            ).fetchone()[0]
        got = {x["id"]: x for x in my_bonds(holder["token"])["bonds"]}
        acc = got[b1["bond_id"]]["accrued_units"] + got[b2["bond_id"]]["accrued_units"]
        assert (acc, int(carry)) == (2, 1), (acc, carry)
        assert int(carry) == pool - acc, (carry, pool, acc)
    finally:
        _unarm_fee(saved)


def test_double_sweep_idempotent():
    holder = _make_holder("bd-idem")
    sid = bond_series_open("idem-7", 7)["series_id"]
    b = buy_bond(holder["token"], sid, 2.0)
    _backdate(b["bond_id"], bought="2020-01-01T00:00:00.000Z")
    _reset_sweep_day()
    first = sweep_bond_day()
    assert first["swept"] is True
    acc1 = [x for x in my_bonds(holder["token"])["bonds"]][0]["accrued_units"]
    second = sweep_bond_day()
    assert second["swept"] is False
    acc2 = [x for x in my_bonds(holder["token"])["bonds"]][0]["accrued_units"]
    assert acc1 == acc2


def test_dry_treasury_holds_maturity():
    holder = _make_holder("bd-dry")
    sid = bond_series_open("dry-7", 7)["series_id"]
    bal0 = _bal(holder["agent_id"])
    esc0 = db.economy_overview()["held_in_bond_escrow_units"]
    b = buy_bond(holder["token"], sid, 2.0)
    assert db.economy_overview()["held_in_bond_escrow_units"] == esc0 + 40, (
        "the buy's face (2.0 credits = 40 units) parks in escrow"
    )
    _backdate(b["bond_id"], matures="2020-01-01T00:00:00.000Z")
    with db._conn() as conn:
        conn.execute(
            "UPDATE treasury_bonds SET accrued_units = 10000000 WHERE id = ?",
            (b["bond_id"],),
        )
    _reset_sweep_day()
    out = sweep_bond_day()
    assert out["released"] == 0, out
    got = [x for x in my_bonds(holder["token"])["bonds"]][0]
    assert got["status"] == "matured", got
    assert got["accrued_units"] == 10000000, got
    assert _bal(holder["agent_id"]) == bal0, "principal out, yield held"
    assert db.economy_overview()["conservation"]["ok"] is True, (
        "dry-held face is out of escrow and out of the recompute"
    )
    assert db.economy_overview()["held_in_bond_escrow_units"] == esc0, (
        "the matured row leaves the locked-in-bonds card - its face is back in the wallet"
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE treasury_bonds SET accrued_units = 40 WHERE id = ?",
            (b["bond_id"],),
        )
    _reset_sweep_day()
    out = sweep_bond_day()
    assert out["released"] >= 1, out
    got = [x for x in my_bonds(holder["token"])["bonds"]][0]
    assert got["status"] == "released", got
    assert _bal(holder["agent_id"]) == bal0 + 40, "refill retries the yield"


def test_early_redeem_haircut():
    holder = _make_holder("bd-early")
    sid = bond_series_open("early-7", 7)["series_id"]
    bal0 = _bal(holder["agent_id"])
    b = buy_bond(holder["token"], sid, 10.0)
    out = db.redeem_bond(holder["token"], b["bond_id"])
    assert out["haircut_units"] == 10, out
    assert out["returned_units"] == 190
    assert _bal(holder["agent_id"]) == bal0 - 10, "haircut is the only loss"
    with db._conn() as conn:
        txs = conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE reason IN"
            " ('bond_redeem', 'bond_redeem_release',"
            " 'bond_early_haircut_intake') AND target_id = ?",
            (b["bond_id"],),
        ).fetchone()[0]
    assert txs == 0, "redeem legs sum to zero"
    assert db.economy_overview()["conservation"]["ok"] is True


def test_forfeit_releases_then_splits():
    holder = _make_holder("bd-forfeit")
    sid = bond_series_open("forfeit-7", 7)["series_id"]
    b = buy_bond(holder["token"], sid, 10.0)
    assert b["face_units"] == 200
    res = db.forfeit_agent(holder["agent_id"])
    assert res["forfeited_units"] >= 200, res
    got = [x for x in my_bonds(holder["token"])["bonds"]][0]
    assert got["status"] == "forfeited", got
    assert db.economy_overview()["conservation"]["ok"] is True


def test_maturity_releases_principal_and_yield():
    holder = _make_holder("bd-mat")
    sid = bond_series_open("mat-7", 7)["series_id"]
    bal0 = _bal(holder["agent_id"])
    b = buy_bond(holder["token"], sid, 10.0)
    _backdate(b["bond_id"], matures="2020-01-01T00:00:00.000Z")
    with db._conn() as conn:
        conn.execute(
            "UPDATE treasury_bonds SET accrued_units = 30 WHERE id = ?",
            (b["bond_id"],),
        )
    _reset_sweep_day()
    out = sweep_bond_day()
    assert out["released"] >= 1, out
    assert _bal(holder["agent_id"]) == bal0 + 30, "principal + yield landed"
    got = [x for x in my_bonds(holder["token"])["bonds"]][0]
    assert got["status"] == "released", got
    assert db.economy_overview()["conservation"]["ok"] is True


def test_yield_base_counts_store_and_stake_fees():
    from db._credits import _insert_entry, _new_tx_id

    before = _base_now()
    with db._conn(immediate=True) as c:
        tx = _new_tx_id(c)
        _insert_entry(
            c,
            None,
            "treasury",
            60,
            "store_vote_boost_intake",
            "store",
            None,
            tx_id=tx,
        )
        tx2 = _new_tx_id(c)
        _insert_entry(
            c,
            None,
            "treasury",
            40,
            "stake_fee_intake",
            "stake",
            None,
            tx_id=tx2,
        )
    assert _base_now() - before == 100


def test_closed_series_still_accrues():
    from db._bonds import bond_series_close

    holder = _make_holder("bd-closed")
    peer = _make_holder("bd-closed-peer")
    saved = _arm_fee(10.0)
    try:
        sid = bond_series_open("closed-7", 7)["series_id"]
        db.transfer_credits(holder["agent_id"], peer["agent_id"], 1000)
        b = buy_bond(holder["token"], sid, 10.0)
        bond_series_close(sid)
        _backdate(b["bond_id"], bought="2020-01-01T00:00:00.000Z")
        _reset_sweep_day()
        sweep_bond_day()
        got = [x for x in my_bonds(holder["token"])["bonds"]][0]
        assert got["accrued_units"] > 0, got
    finally:
        _unarm_fee(saved)


def test_redeem_refuses_foreign_and_dead():
    holder = _make_holder("bd-ref")
    other = _make_holder("bd-ref-other")
    sid = bond_series_open("ref-7", 7)["series_id"]
    b = buy_bond(holder["token"], sid, 2.0)
    msg = expect_error(db.redeem_bond, other["token"], b["bond_id"])
    assert "own bonds" in msg, msg
    db.redeem_bond(holder["token"], b["bond_id"])
    msg = expect_error(db.redeem_bond, holder["token"], b["bond_id"])
    assert "only active" in msg, msg


def test_series_open_notifies_active_citizens():
    holder = _make_holder("bd-nudge")
    peer = _make_holder("bd-nudge-peer")
    out = bond_series_open("nudge-7", 7)
    sid = out["series_id"]
    assert out["notified"] >= 2, out
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT agent_id, kind, ref_type, ref_id, body FROM notifications"
            " WHERE kind = 'economy' AND ref_type = 'bond_series' AND ref_id = ?",
            (sid,),
        ).fetchall()
    got = {r["agent_id"] for r in rows}
    assert holder["agent_id"] in got and peer["agent_id"] in got, got
    assert "nudge-7" in rows[0]["body"], rows[0]["body"]


def test_close_notifies_live_holders_only():
    from db._bonds import bond_series_close

    holder = _make_holder("bd-close")
    stranger = _make_holder("bd-close-out")
    sid = bond_series_open("close-7", 7)["series_id"]
    buy_bond(holder["token"], sid, 2.0)
    out = bond_series_close(sid)
    assert out["status"] == "closed", out
    assert out["notified"] == 1, out
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT agent_id, body FROM notifications"
            " WHERE kind = 'economy' AND ref_type = 'bond_series' AND ref_id = ?"
            " AND body LIKE '%closed to new buys%'",
            (sid,),
        ).fetchall()
    got = {r["agent_id"] for r in rows}
    assert got == {holder["agent_id"]}, got
    with db._conn() as conn:
        opened = conn.execute(
            "SELECT COUNT(*) FROM notifications"
            " WHERE kind = 'economy' AND ref_type = 'bond_series' AND ref_id = ?"
            " AND body LIKE '%New bond series%'",
            (sid,),
        ).fetchone()[0]
    assert opened >= 2, (opened, holder["agent_id"], stranger["agent_id"])


def test_bonds_check_in_line():
    from db._bonds import bond_series_close
    from db._nudges import _bonds_nudge

    for s in list_bond_series():
        if s["status"] == "open":
            bond_series_close(s["series_id"])
    holder = _make_holder("bd-checkin")
    sid = bond_series_open("checkin-7", 7)["series_id"]
    buy_bond(holder["token"], sid, 2.0)
    with db._conn() as conn:
        note = _bonds_nudge(conn, holder["agent_id"])
    assert note and "checkin-7" in note["bonds_note"], note
    assert "my_bonds()" in note["bonds_note"], note
    ci = db.check_in(holder["token"])
    assert any(a.startswith("Bonds:") for a in ci["suggested_actions"]), ci[
        "suggested_actions"
    ]


def test_quiet_bonds_line_when_no_open_series():
    from db._bonds import bond_series_close
    from db._nudges import _bonds_nudge

    holder = _make_holder("bd-quiet")
    for s in list_bond_series():
        if s["status"] == "open":
            bond_series_close(s["series_id"])
    with db._conn() as conn:
        assert _bonds_nudge(conn, holder["agent_id"]) == {}


def _since_7d() -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    ) + ".000Z"


def _seed_store_intake(units: int):
    with db._conn(immediate=True) as c:
        c.execute(
            "INSERT INTO credit_entries (agent_id, delta_units, reason,"
            " account) VALUES (NULL, ?, 'store_src_gadget_intake', 'treasury')",
            (units,),
        )


def test_sources_transfer_only_ignores_store():
    from db._bonds import _trailing_intake_units

    holder = _make_holder("bd-src-t")
    peer = _make_holder("bd-src-t-peer")
    saved = _arm_fee(10.0)
    try:
        with db._conn() as conn:
            since = _since_7d()
            t0 = _trailing_intake_units(conn, since, ("transfer_fee",))
            s0 = _trailing_intake_units(conn, since, ("store",))
        db.transfer_credits(holder["agent_id"], peer["agent_id"], 1000)
        _seed_store_intake(500)
        with db._conn() as conn:
            since = _since_7d()
            t1 = _trailing_intake_units(conn, since, ("transfer_fee",))
            s1 = _trailing_intake_units(conn, since, ("store",))
        assert t1 - t0 == 100, (t0, t1)
        assert s1 - s0 == 500, (s0, s1)
    finally:
        _unarm_fee(saved)


def test_sources_overlap_counts_once():
    from db._bonds import _trailing_intake_units

    with db._conn() as conn:
        since = _since_7d()
        store0 = _trailing_intake_units(conn, since, ("store",))
        both0 = _trailing_intake_units(conn, since, ("store", "spend_all"))
        all0 = _trailing_intake_units(conn, since, ("spend_all",))
    _seed_store_intake(400)
    with db._conn() as conn:
        since = _since_7d()
        store1 = _trailing_intake_units(conn, since, ("store",))
        both1 = _trailing_intake_units(conn, since, ("store", "spend_all"))
        all1 = _trailing_intake_units(conn, since, ("spend_all",))
    assert store1 - store0 == 400, (store0, store1)
    assert both1 - both0 == 400, (both0, both1)
    assert all1 - all0 == 400, (all0, all1)


def test_sources_default_trio_matches_wrapper():
    from db._bonds import _trailing_fee_intake_units, _trailing_intake_units

    with db._conn() as conn:
        since = _since_7d()
        old = _trailing_fee_intake_units(conn, since)
        new = _trailing_intake_units(
            conn, since, ("transfer_fee", "stake_fee", "store")
        )
    assert old == new, (old, new)


def test_sources_invalid_refused():
    assert "at least one" in expect_error(
        db.bond_series_open, "src-bad-1", 7, yield_sources=[]
    )
    assert "unknown yield source" in expect_error(
        db.bond_series_open, "src-bad-2", 7, yield_sources=["forfeit"]
    )
    assert "unknown yield source" in expect_error(
        db.bond_series_open, "src-bad-3", 7, yield_sources=["store", "nope"]
    )
    assert "retired" in expect_error(
        db.bond_series_open, "src-bad-4", 7, yield_sources=["spend_all"]
    )
    assert "retired" in expect_error(
        db.bond_series_open, "src-bad-5", 7, yield_sources=["store", "spend_all"]
    )
    # Structural exclusions are refused as unknown: forfeit (punishment
    # is never yield), custody (parked principal is never revenue).
    for bad in ("forfeit", "guild_deposit", "job_deposit_treasury"):
        assert "unknown yield source" in expect_error(
            db.bond_series_open, f"src-bad-{bad}-7", 7, yield_sources=[bad]
        ), bad


def test_sources_open_returns_canonical_order():
    from db._bonds import bond_series_detail

    out = db.bond_series_open("src-ord-7", 7, yield_sources=["store", "transfer_fee"])
    assert out["yield_sources"] == ["transfer_fee", "store"], out
    assert bond_series_detail(out["series_id"])["yield_sources"] == [
        "transfer_fee",
        "store",
    ]


def test_sources_sweep_respects_selection():
    from db._bonds import _trailing_intake_units

    holder = _make_holder("bd-src-sw")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "DELETE FROM credit_entries WHERE account = 'treasury'"
            " AND substr(reason, -7) = '_intake'"
        )
    with db._conn() as conn:
        base_stake = _trailing_intake_units(conn, _since_7d(), ("stake_fee",))
    assert base_stake == 0, (
        "hermetic slate: intake legs wiped above, nothing reseeds stake"
        f" fees before this assert (got {base_stake})"
    )
    sid_stake = db.bond_series_open("src-sw-st-7", 7, yield_sources=["stake_fee"])[
        "series_id"
    ]
    sid_store = db.bond_series_open("src-sw-so-7", 7, yield_sources=["store"])[
        "series_id"
    ]
    _seed_store_intake(1000)
    b_stake = buy_bond(holder["token"], sid_stake, 2.0)
    b_store = buy_bond(holder["token"], sid_store, 2.0)
    _backdate(b_stake["bond_id"], bought="2020-01-01T00:00:00.000Z")
    _backdate(b_store["bond_id"], bought="2020-01-01T00:00:00.000Z")
    _reset_sweep_day()
    out = sweep_bond_day()
    assert out["swept"] is True
    got = {b["id"]: b for b in my_bonds(holder["token"])["bonds"]}
    assert got[b_stake["bond_id"]]["accrued_units"] == 0
    assert got[b_store["bond_id"]]["accrued_units"] > 0


def test_sources_close_preserves_and_legacy_defaults():
    from db._bonds import bond_series_close, bond_series_detail

    out = db.bond_series_open("src-close-7", 7, yield_sources=["tags", "guild_fees"])
    sid = out["series_id"]
    assert out["yield_sources"] == ["tags", "guild_fees"], out
    bond_series_close(sid)
    assert bond_series_detail(sid)["yield_sources"] == ["tags", "guild_fees"]
    with db._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bond_series)")}
        assert "yield_sources" in cols
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO bond_series (name, term_days, revenue_share_pct,"
            " min_face_units, series_cap_units, citizen_cap_units)"
            " VALUES ('src-legacy-7', 7, 10.0, 20, 2000, 600)"
        )
    got = [s for s in list_bond_series() if s["name"] == "src-legacy-7"][0]
    assert got["yield_sources"] == ["transfer_fee", "stake_fee", "store"], got


def test_list_row_carries_terms():
    out = db.bond_series_open(
        "terms-row-7",
        14,
        min_face_credits=2.0,
        series_cap_credits=50.0,
        citizen_cap_credits=10.0,
    )
    sid = out["series_id"]
    got = [s for s in list_bond_series() if s["series_id"] == sid][0]
    assert got["min_face_units"] == 40, got
    assert got["series_cap_units"] == 1000, got
    assert got["citizen_cap_units"] == 200, got
    assert got["created_at"], got
    assert got["closed_at"] is None, got


def test_admin_sources_checkboxes_match():
    import re

    from db._bonds import _SELECTABLE_SOURCES, DEFAULT_YIELD_SOURCES
    from server.admin._economy import _bond_source_boxes

    boxes = re.findall(
        r'name="yield_sources" value="([^"]+)"( checked)?>', _bond_source_boxes()
    )
    assert [v for v, _ in boxes] == list(_SELECTABLE_SOURCES), boxes
    assert [v for v, c in boxes if c] == list(DEFAULT_YIELD_SOURCES), boxes
    assert "spend_all" not in _bond_source_boxes(), "retired family not offered"


def test_sources_like_escape_rejects_near_miss():
    from db._bonds import _trailing_intake_units

    with db._conn() as conn:
        since = _since_7d()
        store0 = _trailing_intake_units(conn, since, ("store",))
        all0 = _trailing_intake_units(conn, since, ("spend_all",))
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_units, reason,"
            " account) VALUES (NULL, 777, 'storeX_intake', 'treasury')"
        )
    with db._conn() as conn:
        since = _since_7d()
        store1 = _trailing_intake_units(conn, since, ("store",))
        all1 = _trailing_intake_units(conn, since, ("spend_all",))
    assert store1 - store0 == 0, (store0, store1)
    assert all1 - all0 == 777, (all0, all1)


def test_admin_series_table_shows_sources():
    from server.admin._economy import _bond_series_table

    sid = db.bond_series_open("src-tbl-7", 7, yield_sources=["tags", "guild_fees"])[
        "series_id"
    ]
    html = _bond_series_table()
    assert f"<td>{sid}</td>" in html, html
    assert "<th>sources</th>" in html, html
    assert "tags+guild_fees" in html, html


def test_sources_migration_readds_column_and_readers_degrade():
    trio = ["transfer_fee", "stake_fee", "store"]
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "bond_sources_migration.db")
        db.init_db()
        mig = db.register_agent("srcmig-holder")
        with db._conn() as conn:
            from db._credits import grant

            grant(mig["agent_id"], 4000, "srcmig_seed", conn=conn)
        sid = db.bond_series_open("srcmig-7", 7)["series_id"]
        buy_bond(mig["token"], sid, 2.0)
        with db._conn() as conn:
            conn.execute("ALTER TABLE bond_series DROP COLUMN yield_sources")
        from db._bonds import bond_series_detail

        assert my_bonds(mig["token"])["bonds"], "bonds visible w/o column"
        assert list_bond_series()[0]["yield_sources"] == trio
        assert bond_series_detail(sid)["yield_sources"] == trio
        db.init_db()
        with db._conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(bond_series)")}
        assert "yield_sources" in cols, "init_db() re-adds the column"
        out = db.bond_series_open("srcmig-2-7", 7, yield_sources=["store"])
        assert out["yield_sources"] == ["store"], "open works post-migration"
    finally:
        db.DB_PATH = saved


def test_sources_bond_reasons_never_feed_base():
    from db._bonds import _trailing_fee_intake_units, _trailing_intake_units

    with db._conn() as conn:
        since = _since_7d()
        trio0 = _trailing_fee_intake_units(conn, since)
        all0 = _trailing_intake_units(conn, since, ("spend_all",))
    with db._conn(immediate=True) as conn:
        for reason, units in (
            ("bond_buy_fee_intake", 300),
            ("bond_early_haircut_intake", 200),
        ):
            conn.execute(
                "INSERT INTO credit_entries (agent_id, delta_units, reason,"
                " account) VALUES (NULL, ?, ?, 'treasury')",
                (units, reason),
            )
    with db._conn() as conn:
        since = _since_7d()
        assert _trailing_intake_units(conn, since, ("spend_all",)) - all0 == 0
        assert _trailing_fee_intake_units(conn, since) - trio0 == 0


def test_sources_parse_fallback():
    from db._bonds import _parse_sources_str

    assert list(_parse_sources_str("nope")) == []
    assert list(_parse_sources_str("")) == []
    assert list(_parse_sources_str(None)) == []
    assert list(_parse_sources_str("store,UNKNOWN")) == ["store"]


def test_admin_sources_form_multivalue():
    from starlette.datastructures import FormData

    multi = FormData([("yield_sources", "store"), ("yield_sources", "stake_fee")])
    assert multi.getlist("yield_sources") == ["store", "stake_fee"]
    assert FormData([]).getlist("yield_sources") == []


def test_admin_bonds_open_post_subset_and_empty():
    import asyncio
    import os
    from types import SimpleNamespace

    from starlette.datastructures import FormData

    from server.admin._economy import bond_series_open as admin_open

    saved_pw = os.environ.pop("ADMIN_PASSWORD", None)
    try:

        def _admin_req(pairs):
            req = SimpleNamespace(
                headers={},
                cookies={},
                state=SimpleNamespace(csrf_token="tok"),
            )

            async def _form():
                return FormData(pairs)

            req.form = _form
            return req

        resp = asyncio.run(
            admin_open(
                _admin_req(
                    [
                        ("csrf", "tok"),
                        ("name", "srcadm-post-7"),
                        ("term_days", "7"),
                        ("yield_sources", "store"),
                        ("yield_sources", "stake_fee"),
                        ("yield_sources", "tags"),
                    ]
                )
            )
        )
        assert resp.status_code == 200, resp.status_code
        got = [s for s in list_bond_series() if s["name"] == "srcadm-post-7"][0]
        assert got["yield_sources"] == ["stake_fee", "store", "tags"], got
        bad = asyncio.run(
            admin_open(
                _admin_req(
                    [("csrf", "tok"), ("name", "srcadm-nope-7"), ("term_days", "7")]
                )
            )
        )
        assert bad.status_code == 200
        assert "at least one" in bad.body.decode("utf-8")
    finally:
        if saved_pw is not None:
            os.environ["ADMIN_PASSWORD"] = saved_pw


_V2_FAMILY_REASONS = {
    "transfer_fee": ["transfer_fee_intake"],
    "stake_fee": ["stake_fee_intake"],
    "store": ["store_vote_intake"],
    "tags": ["tag_create_intake", "tag_apply_intake"],
    "jobs": ["job_fee_intake"],
    "skills": ["skill_rate_intake"],
    "invoices": ["invoice_create_intake"],
    "services": ["service_listing_fee_intake"],
    "guild_fees": [
        "guild_deposit_fee_intake",
        "guild_found_cost_intake",
        "guild_upkeep_fee_intake",
        "guild_debt_pay_intake",
    ],
}
_V2_EXCLUDED_REASONS = [
    "forfeit_intake",
    "transfer_intake",
    "bond_buy_fee_intake",
    "bond_early_haircut_intake",
    "guild_deposit_intake",
    "job_deposit_treasury_intake",
    "guild_bond_payout_intake",
    "guild_stake_conduit_revert_intake",
]


def _seed_intake(reason: str, units: int):
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_units, reason,"
            " account) VALUES (NULL, ?, ?, 'treasury')",
            (units, reason),
        )


def test_sources_v2_families_count_their_rows():
    from db._bonds import _SELECTABLE_SOURCES, _trailing_intake_units

    with db._conn() as conn:
        since = _since_7d()
        before = {
            s: _trailing_intake_units(conn, since, (s,)) for s in _SELECTABLE_SOURCES
        }
    seeded = {}
    for i, s in enumerate(_SELECTABLE_SOURCES):
        for j, reason in enumerate(_V2_FAMILY_REASONS[s]):
            units = 1000 + 100 * i + j
            _seed_intake(reason, units)
            seeded[s] = seeded.get(s, 0) + units
    with db._conn() as conn:
        since = _since_7d()
        for s in _SELECTABLE_SOURCES:
            assert _trailing_intake_units(conn, since, (s,)) - before[s] == seeded[s], s


def test_sources_v2_structural_exclusions_yield_zero():
    from db._bonds import _EXCLUDED_REASONS, _SELECTABLE_SOURCES, _trailing_intake_units

    # The product tuple and this pin's list must move together: a new
    # exclusion without a zero-pin fails here, not silently in prod.
    assert set(_EXCLUDED_REASONS) <= set(_V2_EXCLUDED_REASONS), set(
        _EXCLUDED_REASONS
    ) - set(_V2_EXCLUDED_REASONS)

    with db._conn() as conn:
        since = _since_7d()
        before_all = {
            s: _trailing_intake_units(conn, since, (s,)) for s in _SELECTABLE_SOURCES
        }
        before_legacy = _trailing_intake_units(conn, since, ("spend_all",))
    for i, reason in enumerate(_V2_EXCLUDED_REASONS):
        _seed_intake(reason, 500 + i)
    with db._conn() as conn:
        since = _since_7d()
        for s in _SELECTABLE_SOURCES:
            assert _trailing_intake_units(conn, since, (s,)) - before_all[s] == 0, s
        assert _trailing_intake_units(conn, since, ("spend_all",)) - before_legacy == 0


def test_sources_v2_legacy_spend_all_frozen():
    from db._bonds import _series_sources, _trailing_intake_units, bond_series_detail

    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO bond_series (name, term_days, revenue_share_pct,"
            " min_face_units, series_cap_units, citizen_cap_units,"
            " yield_sources) VALUES ('src-legacy-all-7', 7, 10.0, 20, 2000,"
            " 600, 'spend_all')"
        )
    sid = next(
        s["series_id"] for s in list_bond_series() if s["name"] == "src-legacy-all-7"
    )
    assert bond_series_detail(sid)["yield_sources"] == ["spend_all"]
    with db._conn() as conn:
        since = _since_7d()
        before = _trailing_intake_units(conn, since, ("spend_all",))
    # Frozen clause: guild fee rows count, transfer fees and custody do not.
    _seed_intake("guild_upkeep_fee_intake", 400)
    _seed_intake("transfer_fee_intake", 400)
    _seed_intake("guild_deposit_intake", 400)
    with db._conn() as conn:
        assert _trailing_intake_units(conn, _since_7d(), ("spend_all",)) - before == 400
        row = conn.execute("SELECT * FROM bond_series WHERE id = ?", (sid,)).fetchone()
        assert list(_series_sources(row)) == ["spend_all"]


def test_sources_v2_canonical_order_all():
    from db._bonds import _SELECTABLE_SOURCES, bond_series_detail

    rev = list(reversed(_SELECTABLE_SOURCES))
    out = db.bond_series_open("src-ord-all-7", 7, yield_sources=rev)
    assert out["yield_sources"] == list(_SELECTABLE_SOURCES), out
    assert bond_series_detail(out["series_id"])["yield_sources"] == list(
        _SELECTABLE_SOURCES
    )


def test_bond_family_trailing_keys():
    from db._bonds import _SELECTABLE_SOURCES

    got = db.bond_family_trailing()
    assert set(got) == set(_SELECTABLE_SOURCES), got
    assert all(isinstance(v, int) for v in got.values()), got
    # Value pin, not just shape: the helper's own window/loop wiring
    # must move when traffic lands (an all-zeros stub passes the above).
    before = got["tags"]
    _seed_intake("tag_apply_intake", 321)
    assert db.bond_family_trailing()["tags"] - before == 321


def test_clamp_since_unit():
    from db._bonds import _clamp_since

    assert (
        _clamp_since(
            "2020-01-01T00:00:00.000Z", {"created_at": "2020-06-01T00:00:00.000Z"}
        )
        == "2020-06-01T00:00:00.000Z"
    )
    assert (
        _clamp_since(
            "2020-06-01T00:00:00.000Z", {"created_at": "2020-01-01T00:00:00.000Z"}
        )
        == "2020-06-01T00:00:00.000Z"
    )
    assert _clamp_since("2020-01-01T00:00:00.000Z", {}) == "2020-01-01T00:00:00.000Z"
    assert (
        _clamp_since("2020-01-01T00:00:00.000Z", {"created_at": ""})
        == "2020-01-01T00:00:00.000Z"
    )


def _set_series_opened(sid: int, created_at: str):
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE bond_series SET created_at = ? WHERE id = ?", (created_at, sid)
        )


def test_series_cutoff_future_open_counts_zero():
    holder = _make_holder("bd-cut-fut")
    sid = db.bond_series_open("cut-fut-7", 7, yield_sources=["store"])["series_id"]
    _set_series_opened(sid, "2030-01-01T00:00:00.000Z")
    _seed_store_intake(1000)
    b = buy_bond(holder["token"], sid, 2.0)
    _backdate(b["bond_id"], bought="2020-01-01T00:00:00.000Z")
    _reset_sweep_day()
    out = sweep_bond_day()
    assert out["swept"] is True
    got = {x["id"]: x for x in my_bonds(holder["token"])["bonds"]}
    assert got[b["bond_id"]]["accrued_units"] == 0


def test_series_cutoff_backdated_open_counts():
    holder = _make_holder("bd-cut-old")
    sid = db.bond_series_open("cut-old-7", 7, yield_sources=["store"])["series_id"]
    _set_series_opened(sid, "2020-01-01T00:00:00.000Z")
    _seed_store_intake(1000)
    b = buy_bond(holder["token"], sid, 2.0)
    _backdate(b["bond_id"], bought="2020-01-01T00:00:00.000Z")
    _reset_sweep_day()
    out = sweep_bond_day()
    assert out["swept"] is True
    got = {x["id"]: x for x in my_bonds(holder["token"])["bonds"]}
    assert got[b["bond_id"]]["accrued_units"] > 0


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} bond tests passed")
