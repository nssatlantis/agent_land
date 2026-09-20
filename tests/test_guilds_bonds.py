"""Guild-owned bonds (proposal #598, pool conduit): founder-only buys,
pool coverage + exposure caps, co-sign band, escrow pairing with link,
sweep-to-pool maturity, forfeit redirect. Hermetic per test (fresh
guilds + series); branch CI is the first execution (built with zero
local budget - see #598).
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_bonds_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()

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
            "guild_bonds_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )
    assert ok, "treasury could not fund the test seed"


def _rich_guild(pool_cr: float = 25.0):
    founder = _new_agent("gb-founder")
    _fund(founder["agent_id"], 600)
    guild = db.found_guild(founder["token"], f"Bonds-{_SEQ[0]}")
    mate = _new_agent("gb-mate")
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.guild_deposit(founder["token"], guild["id"], pool_cr)
    return founder, guild, mate


def _series(tag: str) -> int:
    return db.bond_series_open(f"gbond-{tag}-{_SEQ[0]}", 7)["series_id"]


def _pool(guild_id: int) -> int:
    with db._conn() as conn:
        return db.guild_balance(conn, guild_id)


def _link(bond_id: int):
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_bond_links WHERE bond_id = ?", (bond_id,)
        ).fetchone()
        return dict(row) if row else None


def test_guild_buy_founder_only():
    founder, guild, mate = _rich_guild()
    sid = _series("own")
    msg = expect_error(db.guild_buy_bond, mate["token"], guild["id"], sid, 2.0)
    assert "only the guild founder" in msg, msg


def test_guild_buy_pool_cover_and_link():
    founder, guild, _mate = _rich_guild()
    sid = _series("cover")
    before = _pool(guild["id"])
    out = db.guild_buy_bond(founder["token"], guild["id"], sid, 2.0)
    assert out["face_units"] == 40, out
    assert _link(out["bond_id"])["guild_id"] == guild["id"]
    # Pool down by face + live fee; founder nets zero (grant in, v1
    # spend out); bond row stays founder-owned v1.
    from db._credits import fee_units

    assert _pool(guild["id"]) == before - 40 - fee_units(40), (
        _pool(guild["id"]),
        before,
    )
    with db._conn() as conn:
        row = conn.execute(
            "SELECT owner_id FROM treasury_bonds WHERE id = ?",
            (out["bond_id"],),
        ).fetchone()
    assert int(row["owner_id"]) == founder["agent_id"]
    assert db.guild_bonds(guild["id"])[0]["id"] == out["bond_id"]


def test_guild_buy_single_series_cap():
    founder, guild, _mate = _rich_guild()
    sid = _series("cap")
    # Pool ~500u: 33% = 165u. A 9cr face (180u) breaches; 2cr (40u)
    # fits (and stays under the 15% co-sign band: 41u vs 75u).
    msg = expect_error(db.guild_buy_bond, founder["token"], guild["id"], sid, 9.0)
    assert "33%" in msg, msg
    out = db.guild_buy_bond(founder["token"], guild["id"], sid, 2.0)
    assert _link(out["bond_id"]) is not None


def test_guild_buy_cosign_band():
    founder, guild, _mate = _rich_guild()
    sid = _series("cosign")
    # Pool ~500u: 15% band = 75u. A 4cr buy (80 + 1 fee = 81u) trips it
    # while fitting the 33% series cap (80u vs 165u).
    msg = expect_error(db.guild_buy_bond, founder["token"], guild["id"], sid, 4.0)
    assert "co-sign" in msg, msg


def test_guild_buy_closed_series_refused():
    from db._bonds import bond_series_close

    founder, guild, _mate = _rich_guild()
    sid = _series("shut")
    bond_series_close(sid)
    msg = expect_error(db.guild_buy_bond, founder["token"], guild["id"], sid, 2.0)
    assert "closed to new buys" in msg, msg


def test_guild_sweep_routes_poolward():
    founder, guild, _mate = _rich_guild()
    sid = _series("sweep")
    out = db.guild_buy_bond(founder["token"], guild["id"], sid, 2.0)
    bid = out["bond_id"]
    with db._conn(immediate=True) as c:
        c.execute(
            "UPDATE treasury_bonds SET bought_at = '2020-01-01T00:00:00.000Z',"
            " matures_at = '2020-01-02T00:00:00.000Z' WHERE id = ?",
            (bid,),
        )
        c.execute("DELETE FROM economy_meta WHERE key = 'bond_last_sweep_day'")
    founder_before = _fund_balance(founder["agent_id"])
    pool_before = _pool(guild["id"])
    swept = db.sweep_bond_day()
    assert swept["swept"] is True, swept
    # Founder wallet untouched by maturity (received, then redirected);
    # the pool took face + share.
    assert _fund_balance(founder["agent_id"]) == founder_before
    assert _pool(guild["id"]) == pool_before + 40, (
        _pool(guild["id"]),
        pool_before,
    )
    with db._conn() as conn:
        memo = conn.execute(
            "SELECT kind, units FROM guild_ledger WHERE guild_id = ?"
            " AND kind = 'bond' ORDER BY id DESC LIMIT 1",
            (guild["id"],),
        ).fetchone()
    assert memo is not None and memo["units"] >= 40, dict(memo)


def _fund_balance(agent_id: int) -> int:
    import db._credits as _cr

    with db._conn() as conn:
        return _cr.balance_for(conn, agent_id)


def test_guild_forfeit_redirects_poolward():
    from db._credits import forfeit_agent

    founder, guild, _mate = _rich_guild()
    sid = _series("forfeit")
    out = db.guild_buy_bond(founder["token"], guild["id"], sid, 2.0)
    pool_before = _pool(guild["id"])
    with db._conn() as c:
        forfeit_agent(founder["agent_id"], conn=c)
    # The linked face (40u) routed poolward before the split read the
    # wallet; the pool is up by exactly the face.
    assert _pool(guild["id"]) == pool_before + 40, (
        _pool(guild["id"]),
        pool_before,
    )
    assert _link(out["bond_id"]) is not None


def _cosign(founder, guild, units: int):
    req = db.request_guild_cosign(founder["token"], guild["id"], "bond buy", units)
    return db.confirm_guild_cosign(founder["token"], req["cosign_id"])


def test_guild_buy_cosigned_caps():
    founder, guild, _mate = _rich_guild()
    _cosign(founder, guild, 400)
    sid_a = _series("capA")
    sid_b = _series("capB")
    sid_c = _series("capC")
    # Pool ~500u: two 5cr buys (100u each, co-signed) fit every band;
    # a third 4cr buy trips the 75% total cap (380u vs 375u).
    db.guild_buy_bond(founder["token"], guild["id"], sid_a, 5.0)
    db.guild_buy_bond(founder["token"], guild["id"], sid_b, 5.0)
    msg = expect_error(db.guild_buy_bond, founder["token"], guild["id"], sid_c, 4.0)
    assert "75%" in msg, msg


def test_guild_redeem_routes_poolward():
    founder, guild, _mate = _rich_guild()
    sid = _series("redeem")
    out = db.guild_buy_bond(founder["token"], guild["id"], sid, 2.0)
    pool_before = _pool(guild["id"])
    ret = db.redeem_bond(founder["token"], out["bond_id"])
    assert _pool(guild["id"]) == pool_before + ret["returned_units"], (
        _pool(guild["id"]),
        pool_before,
        ret,
    )
    assert _link(out["bond_id"]) is not None  # kept as audit trail


def test_guild_disband_detaches_links():
    founder, guild, _mate = _rich_guild()
    sid = _series("disband")
    out = db.guild_buy_bond(founder["token"], guild["id"], sid, 2.0)
    db.disband_guild(founder["token"], guild["id"], "dissolve")
    assert _link(out["bond_id"]) is None
    with db._conn() as conn:
        row = conn.execute(
            "SELECT status, owner_id FROM treasury_bonds WHERE id = ?",
            (out["bond_id"],),
        ).fetchone()
    assert row["status"] == "active", dict(row)
    assert int(row["owner_id"]) == founder["agent_id"]


def test_guild_forfeit_failure_keeps_bond_live():
    from db._credits import forfeit_agent

    founder, guild, _mate = _rich_guild()
    sid = _series("forfeitlive")
    out = db.guild_buy_bond(founder["token"], guild["id"], sid, 2.0)
    with db._conn(immediate=True) as c:
        c.execute(
            "UPDATE treasury_bonds SET bought_at = '2020-01-01T00:00:00.000Z',"
            " matures_at = '2020-01-02T00:00:00.000Z' WHERE id = ?",
            (out["bond_id"],),
        )
    # Drain the founder below one face so the pool redirect cannot fund;
    # the bond must stay live (retryable) with the pool untouched.
    db.transfer_credits(founder["agent_id"], _mate["agent_id"], 60)
    pool_before = _pool(guild["id"])
    with db._conn() as c:
        forfeit_agent(founder["agent_id"], conn=c)
    with db._conn() as conn:
        row = conn.execute(
            "SELECT status FROM treasury_bonds WHERE id = ?", (out["bond_id"],)
        ).fetchone()
    assert row["status"] == "matured", dict(row)
    assert _pool(guild["id"]) == pool_before


def test_guild_buy_citizen_cap_strict():
    founder = _new_agent("gb-capf")
    _fund(founder["agent_id"], 3000)
    guild = db.found_guild(founder["token"], f"Cappool-{_SEQ[0]}")
    mate = _new_agent("gb-capm")
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.guild_deposit(founder["token"], guild["id"], 100.0)
    sid = _series("citcap")
    # Founder holds 28cr personally; the pool's 4cr buy would take the
    # founder-owned total past the 30cr citizen cap - refused even though
    # every pool-side gate (cover, 33%, 75%, co-sign) fits.
    db.buy_bond(founder["token"], sid, 28.0)
    msg = expect_error(db.guild_buy_bond, founder["token"], guild["id"], sid, 4.0)
    assert "per-citizen cap" in msg, msg


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} guild bond tests passed")
