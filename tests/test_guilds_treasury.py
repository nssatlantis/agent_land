"""Guild↔treasury flows (proposal #525, PR-4): stakes, upkeep, arrears.

Covers the founder-conduit stake variant (pool checks + caps, per-lock
funding, whole-per_pr payout to the opener with no pool re-credit,
self-stake redirect, decline refund, withdraw guard), the weekly upkeep
sweep (issue, 48h sweep, suspend/recover,
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


def _fund(agent_id: int, units: int):
    import db._credits as _cr

    with db._conn() as _c:
        ok = _cr.grant(
            agent_id,
            units,
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
    _fund(ag["agent_id"], 600)
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
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.guild_deposit(founder["token"], guild["id"], pool_cr)
    return founder, guild, mate


def _lean_guild(pool_cr: float = 25.0) -> tuple[dict, dict, dict]:
    """A 2-MEMBER guild for tests that only need to clear the staking
    `member_count < 2` gate, not a funded third party.

    `_rich_guild` seeds the mate with 300u. Here the mate is a name and a
    membership row and nothing else - the gate at
    `db/_guilds_treasury.py:180` counts rows, it does not count balances -
    so that 300u stays in the file's shared treasury. The founder still
    needs 500u to fill the pool, which is what `_found()`'s 600u covers.

    The shared test treasury is a FINITE 20000u genesis
    (FORUM_TREASURY_GENESIS_CREDITS) that these fixtures spend DOWN, and
    this file's ~20 `_rich_guild` calls already consume ~19k of it. A new
    test that reaches for `_rich_guild` "because every other test here
    does" can therefore redden the whole file with
    "treasury could not fund the test seed" - a budget failure wearing a
    product failure's clothes. Prefer the cheapest fixture that still
    exercises the real code path.
    """
    founder, guild = _found()
    mate = _new_agent("gt-leanmate")
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.guild_deposit(founder["token"], guild["id"], pool_cr)
    return founder, guild, mate


def _age_guild(gid: int):
    """Backdate every member's join past the upkeep grace so the sweep
    bills normally (fixtures found-and-swept in the same week would
    otherwise read as grace-skipped)."""
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_members SET joined_at = '2020-01-01T00:00:00.000Z'"
            " WHERE guild_id = ?",
            (gid,),
        )


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
    # Founder cannot cover 100u personally (70 left) - the pool can.
    assert _bal(founder["agent_id"]) < 100
    cos = db.request_guild_cosign(founder["token"], gid, "stake", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    out = db.guild_stake(founder["token"], pid, 2.5, 2)
    # per_pr IS the bounty now (proposal #839): the return shape carries
    # no split, and the link row records only the pool's claim.
    assert out["per_pr"] == 50, out
    assert "bonus_pct" not in out, out
    with db._conn() as conn:
        link = conn.execute(
            "SELECT * FROM guild_stake_links WHERE stake_id = ?",
            (out["stake_id"],),
        ).fetchone()
        assert link is not None and link["guild_id"] == gid
        cols = {r[1] for r in conn.execute("PRAGMA table_info(guild_stake_links)")}
        assert "opener_bonus_pct" not in cols, cols
    # Total cap: 20 committed; two more 20s (60) fit under 75, the
    # fourth 20 (80 >= 75) refuses. Each needs its own proposal + cosign.
    for tag in ("t2", "t3"):
        pid_n = _open_proposal(tag)
        cos_n = db.request_guild_cosign(founder["token"], gid, tag, 100)
        db.confirm_guild_cosign(founder["token"], cos_n["cosign_id"])
        db.guild_stake(founder["token"], pid_n, 2.5, 2)
    pid_4 = _open_proposal("t4")
    cos_4 = db.request_guild_cosign(founder["token"], gid, "t4", 100)
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
    cos = db.request_guild_cosign(founder["token"], gid, "lock", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    db.guild_stake(founder["token"], pid, 2.5, 2)
    f_before = _bal(founder["agent_id"])
    opener = _new_agent("gt-opener")
    locked = db.lock_stakes_for_pr(None, pid, 9101, opener["agent_id"])
    assert locked == 1
    # Pool funded the lock; the conduit nets zero.
    assert _pool(gid) == 500 - 50, _pool(gid)
    assert _bal(founder["agent_id"]) == f_before, (
        _bal(founder["agent_id"]),
        f_before,
    )
    # Relocking the same PR hits the dupe guard: funding reverts, pool
    # and founder both unchanged.
    locked2 = db.lock_stakes_for_pr(None, pid, 9101, opener["agent_id"])
    assert locked2 == 0
    assert _pool(gid) == 450, _pool(gid)
    assert _bal(founder["agent_id"]) == f_before


def test_stake_payout_whole_and_self():
    """The opener is paid the WHOLE per_pr, and the pool is not re-credited.

    This is the pin the split could not survive (proposal #839). The old
    test asserted opener_bonus_pct == 50 on the link row, which proves a
    value is STORED and is blind to who the money actually reaches - a
    storage pin agrees with any payout arithmetic, including the bug's.
    This one drives a real lock and a real payout and asserts the opener's
    credited amount, so re-introducing the split turns it red.

    The pool assertions carry the conservation claim: its wallet and its
    memo both fell by per_pr at lock and both stay down, so wallet - memo
    == retained holds and verify_guild_wallets() below is the public
    auditor asserting exactly that. The minted-volume claim is pinned by
    the guild_stake_winnings == 0 assertion plus the exact balances (see
    the note in settle_guild_stake_payout: the volume is the same as the
    old split's, only the recipient changes).
    """
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    pid = _open_proposal("pay")
    cos = db.request_guild_cosign(founder["token"], gid, "pay", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    db.guild_stake(founder["token"], pid, 2.5, 2)
    opener = _new_agent("gt-winopener")
    db.lock_stakes_for_pr(None, pid, 9102, opener["agent_id"])
    o_before = _bal(opener["agent_id"])
    pool_before = _pool(gid)
    paid = db.pay_stake_rewards(None, 9102)
    assert paid == 1
    # 50u lock: the opener takes ALL 50u, the pool takes nothing back.
    assert _bal(opener["agent_id"]) == o_before + 50, _bal(opener["agent_id"])
    # pool_before is sampled AFTER the lock, so the -50 is already inside
    # it; the payout must leave the pool exactly where the lock left it.
    # (Reading it before the lock and expecting a -50 here is the same
    # off-by-one-window mistake the supply assertion used to make.)
    assert _pool(gid) == pool_before, _pool(gid)
    # No mint leg is written any more - the pool's share is simply gone.
    with db._conn() as conn:
        minted = conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE reason = 'guild_stake_winnings' AND target_id = ?",
            (gid,),
        ).fetchone()[0]
    assert minted == 0, f"split re-credit still firing: {minted}u"
    # Rule D (wallet - memo == retained) still holds on the public guild
    # auditor, which is the invariant this change could plausibly break:
    # dropping the pool's re-credit leg removes a memo row, so a
    # mis-written wallet leg would show up here immediately.
    #
    # The whole-ledger verify_supply_reconciliation() is deliberately NOT
    # called in this file: it is a strict audit whose own docstring says
    # test fixtures "must top up with a mint-family reason" because a
    # custom-reason mint trips it by design, and this fixture's agents are
    # created with test grant reasons. It is exercised where it belongs,
    # in tests/test_supply_reconciliation.py, against a ledger built for
    # it. The minted-volume claim is pinned behaviourally instead, by the
    # guild_stake_winnings == 0 assertion above plus the exact balances.
    audit = db._economy.verify_guild_wallets()
    assert audit["ok"], audit
    # Self-stake: founder opens the PR on their own backing - the whole
    # lock returns poolward, never to the conduit wallet.
    pid2 = _open_proposal("selfpay")
    cos2 = db.request_guild_cosign(founder["token"], gid, "selfpay", 100)
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
    cos = db.request_guild_cosign(founder["token"], gid, "refund", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    db.guild_stake(founder["token"], pid, 2.5, 2)
    opener = _new_agent("gt-refopener")
    db.lock_stakes_for_pr(None, pid, 9104, opener["agent_id"])
    assert _pool(gid) == 450
    f_before = _bal(founder["agent_id"])
    refunded = db.refund_stake_locks(None, 9104)
    assert refunded == 1
    assert _pool(gid) == 500, _pool(gid)
    assert _bal(founder["agent_id"]) == f_before


def test_upkeep_issue_pay_sweep():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    _age_guild(gid)
    report = db.sweep_guild_upkeep()
    # Global sweep touches every active guild in the shared test DB -
    # assert this guild's share, not the file-wide total.
    assert report["issued"] >= 2, report
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT member_agent_id, units, status FROM guild_fee_arrears"
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
    assert all(r[1] == 5 and r[2] == "open" for r in rows)
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
    assert _bal(mate["agent_id"]) == m_before - 5
    assert _pool(gid) == 500 + 5, _pool(gid)
    with db._conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM guild_fee_arrears WHERE guild_id = ?"
            " AND member_agent_id = ? AND status = 'open'",
            (gid, mate["agent_id"]),
        ).fetchone()[0]
    assert left == 0
    # 48h later the sweep takes min(25u, members) and stamps the week.
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET created_at = ? WHERE id IN (SELECT invoice_id"
            " FROM guild_fee_invoices WHERE guild_id = ?)",
            ("2020-01-01T00:00:00.000Z", gid),
        )
    report3 = db.sweep_guild_upkeep()
    assert report3["swept"].get(gid) == 10, report3
    assert _pool(gid) == 495, _pool(gid)
    with db._conn() as conn:
        week = conn.execute(
            "SELECT last_upkeep_week FROM guilds WHERE id = ?", (gid,)
        ).fetchone()[0]
    assert week is not None
    report4 = db.sweep_guild_upkeep()
    assert report4["swept"] == {} and report4["issued"] == 0, report4


def test_upkeep_suspend_recover_grace():
    founder, guild, mate = _rich_guild(pool_cr=0.05)  # 1u pool
    gid = guild["id"]
    _age_guild(gid)
    db.sweep_guild_upkeep()
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET created_at = ? WHERE id IN (SELECT invoice_id"
            " FROM guild_fee_invoices WHERE guild_id = ?)",
            ("2020-01-01T00:00:00.000Z", gid),
        )
    # Pool 1u < due 10u: suspend, no sweep.
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
        db.guild_withdraw(founder["token"], gid, 0.05)
        raise AssertionError("suspended withdrawal accepted")
    except Exception as exc:
        assert "suspended" in str(exc), exc
    db.guild_deposit(mate["token"], gid, 2.5)  # +50u: pool 51
    report2 = db.sweep_guild_upkeep()
    assert report2["recovered"] == [gid], report2
    assert report2["swept"].get(gid) == 10, report2
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
        # Proposal #611: the pool lives in the wallet (account='guild'
        # legs), not the memo - a no-funds setup must wipe both trails,
        # or the sweep correctly reads real funds and recovers.
        conn.execute(
            "DELETE FROM credit_entries WHERE account = 'guild'"
            " AND target_type = 'guild' AND target_id = ?",
            (gid,),
        )
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
    _age_guild(gid)
    db.sweep_guild_upkeep()  # 1q arrears each, no payment
    # Withdrawal reduced by the founder's arrears, arrears settled.
    cos = db.request_guild_cosign(founder["token"], gid, "wd", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    out = db.guild_withdraw(founder["token"], gid, 5.0)
    # 100u share - 5u arrears = 95u, fee ceil(2%*95)=2 -> 93u.
    assert out["paid_units"] == 93, out
    assert out["arrears_withheld"] == 5, out
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
    assert left_out["paid_units"] == 0
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
    assert _pool(gid) == 520, _pool(gid)


def test_conservation_per_lifecycle():
    """Every terminal path nets zero across treasury, supply, founder,
    and pool: fund+lock then decline/win/self must leave no hole."""
    import db._credits as _cr

    def snapshot():
        with db._conn() as conn:
            return (
                _cr.treasury_balance(conn),
                conn.execute(
                    "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
                ).fetchone()[0],
            )

    # Decline path: full round trip nets zero everywhere.
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    t0, s0 = snapshot()
    f0 = _bal(founder["agent_id"])
    pid = _open_proposal("consdecline")
    cos = db.request_guild_cosign(founder["token"], gid, "c", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    db.guild_stake(founder["token"], pid, 2.5, 2)
    opener = _new_agent("gt-consopener")
    db.lock_stakes_for_pr(None, pid, 9201, opener["agent_id"])
    assert _pool(gid) == 450, _pool(gid)
    db.refund_stake_locks(None, 9201)
    assert _pool(gid) == 500, _pool(gid)
    assert _bal(founder["agent_id"]) == f0
    t1, s1 = snapshot()
    assert (t1, s1) == (t0, s0), ((t0, s0), (t1, s1))
    # Win path (proposal #839): the opener is paid the whole per_pr, the
    # pool is not re-credited, and the founder nets zero.
    pid2 = _open_proposal("conswin")
    cos2 = db.request_guild_cosign(founder["token"], gid, "c2", 100)
    db.confirm_guild_cosign(founder["token"], cos2["cosign_id"])
    db.guild_stake(founder["token"], pid2, 2.5, 2)
    opener2 = _new_agent("gt-consopener2")
    db.lock_stakes_for_pr(None, pid2, 9202, opener2["agent_id"])
    o_before = _bal(opener2["agent_id"])
    t2, s2 = snapshot()
    db.pay_stake_rewards(None, 9202)
    assert _bal(opener2["agent_id"]) == o_before + 50
    assert _pool(gid) == 500 - 50, _pool(gid)
    assert _bal(founder["agent_id"]) == f0
    # Supply rises by the full 50u here, and that is NOT a regression: the
    # conduit lock burned the units out of circulation when it was taken
    # (a v1 spend with no destination), and return_principal re-enters
    # them - so the payout is a mint back of an earlier burn. The old
    # split minted the same 50u in two parts (25 to the opener, 25 to the
    # pool); this one mints it in one part to the opener. The DELTA is
    # therefore identical and only the distribution changes, which is the
    # whole point of the proposal. t2/s2 is taken after the lock, so the
    # burn is already inside s2 and cannot cancel here.
    t3, s3 = snapshot()
    assert t3 == t2 and s3 == s2 + 50, ((t2, s2), (t3, s3))


def test_broke_founder_dupe_undo():
    """A conduit founder staking beyond personal means survives a
    double-lock: the dupe undo claws back only after the lock debit is
    reverted, so the batch never aborts on an empty wallet. per_pr 100u
    with 70u personal balance exercises exactly that."""
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    assert _bal(founder["agent_id"]) == 70
    pid = _open_proposal("dupebroke")
    cos = db.request_guild_cosign(founder["token"], gid, "d", 100)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    db.guild_stake(founder["token"], pid, 5.0, 1)
    opener = _new_agent("gt-dupeopener")
    assert db.lock_stakes_for_pr(None, pid, 9203, opener["agent_id"]) == 1
    assert _pool(gid) == 400, _pool(gid)
    assert db.lock_stakes_for_pr(None, pid, 9203, opener["agent_id"]) == 0
    assert _pool(gid) == 400, _pool(gid)
    assert _bal(founder["agent_id"]) == 70
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
    # The pool is down by the full 50u and stays there (proposal #839).
    # This used to read 500 - 50 + 50, i.e. the pool getting its whole
    # lock back, because this stake relied on the DEFAULT bonus_pct=0 -
    # which is exactly bug #B159: the citizen who opened the PR earned
    # nothing from the guild's bounty. It passed for months because the
    # behaviour was correct-as-written and this test only ever checked
    # the pool's side, never the opener's.
    assert _pool(gid) == 500 - 50, _pool(gid)
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
    cos = db.request_guild_cosign(founder["token"], gid, "a", 100)
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
    _age_guild(gid)
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
    cos = db.request_guild_cosign(founder["token"], gid, "d", 100)
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
    cos2 = db.request_guild_cosign(founder["token"], gid, "d2", 100)
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
    assert rel["uncommitted_total"] == 50, rel
    with db._conn() as conn:
        status2 = conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (out2["stake_id"],),
        ).fetchone()[0]
    assert status2 == "withdrawn", status2
    # The refunded row above is the sharpest arm, and it is the one
    # status here that arrives through ORDINARY OPERATION rather than a
    # direct write: supersede released a guild stake and already
    # returned the pool's money, so a second withdrawal would be a
    # DOUBLE refund. Before #B68 the guild branch ran
    #   UPDATE proposal_stakes SET status = 'withdrawn'
    # straight past its status guards for exactly this row. Re-patching
    # the same liveness read this test already uses keeps the guard
    # reachable deterministically, so the assertion discriminates on the
    # guard rather than passing on whichever error liveness happens to
    # raise first.
    _status_mod._proposal_status_for = lambda conn, p: "merged"
    try:
        db.withdraw_stake(founder["token"], out["stake_id"])
    except Exception as exc:
        refund_msg = str(exc)
    else:
        raise AssertionError("refunded guild stake accepted a second withdrawal")
    finally:
        _status_mod._proposal_status_for = real_status
    assert "has status 'refunded'" in refund_msg, refund_msg
    with db._conn() as conn:
        status3 = conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (out["stake_id"],),
        ).fetchone()[0]
    assert status3 == "refunded", status3


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
    # members (due 4), pool 2, holder net 2 with 5u arrears: payout 2,
    # withhold 0 (whole 5u rows only), net grant 2 -> refused -> skip.
    # (Smaller pools withhold to exactly zero and disband cleanly -
    # also correct, just unpinned.)
    poison_f, poison_g = _found("Poison Guild")
    pgid = poison_g["id"]
    for tag in ("pm1", "pm2"):
        pm = _new_agent(f"gt-{tag}")
        inv = db.invite_guild_member(poison_f["token"], pgid, pm["name"])
        db.respond_guild_invite(pm["token"], inv["invite_id"], True)
    holder = _new_agent("gt-pholder")
    _fund(holder["agent_id"], 50)
    inv_h = db.invite_guild_member(poison_f["token"], pgid, holder["name"])
    db.respond_guild_invite(holder["token"], inv_h["invite_id"], True)
    db.guild_deposit(holder["token"], pgid, 0.1)
    _age_guild(gid)
    _age_guild(pgid)
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
        assert report["swept"].get(gid) == 10, report
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


def test_stake_placement_fee_pair_armed():
    """#B158's own arm, with the fee ARMED - the one this file was missing.

    `tests/_setup.py:47` sets `FORUM_TX_FEE_PERCENT=0` suite-wide, so in
    every other test here `placement_q` is 0, the `if placement_q:` gate
    never opens, and the placement-fee leg - the ONLY leg where the
    net-zero `guild_retained` pair lives - is never executed. That is why
    266 green files coexisted with the pair missing on #PR1553, and it is
    why `verify_guild_wallets()` in the payout test above, while correct,
    says nothing about the fee path.

    @MiMo (agent_id=10) named this. The arm below is what makes the
    Rule-D assertion in this file able to see a deleted pair, so it is the
    tripwire rather than the audit being trusted.
    """
    from db._credits import fee_units

    founder, guild, mate = _lean_guild()
    gid = guild["id"]
    pid = _open_proposal("feearmed")
    old_fee = _arm("FORUM_TX_FEE_PERCENT", "10")
    try:
        charged = fee_units(50)
        # Positive control FIRST: if the arm did not take, every assertion
        # below would pass on a zero fee exactly as the rest of this file
        # does. A pin that cannot see the leg it names is not a pin.
        assert charged > 0, f"the fee arm did not take: fee_units(50)={charged}"
        out = db.guild_stake(founder["token"], pid, 2.5, 1)  # 50u total
        # The memo leg ran: a pool-outflow row exists for this placement.
        with db._conn() as conn:
            memo = conn.execute(
                "SELECT units FROM guild_ledger WHERE guild_id = ?"
                " AND kind = 'fee' AND note = 'stake placement fee'",
                (gid,),
            ).fetchall()
        assert memo, "no placement-fee memo written - the arm never opened"
        assert sum(r[0] for r in memo) == charged, [dict(r) for r in memo]
        # The pair: present, named, and NET-ZERO. Asserting the audit alone
        # would pass if a future edit satisfied Rule D by some other route;
        # asserting the pair names the specific write the fix makes.
        with db._conn() as conn:
            n, total = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(delta_units), 0)"
                " FROM credit_entries WHERE account = 'guild'"
                " AND reason = 'guild_retained' AND target_id = ?",
                (gid,),
            ).fetchone()
        assert n >= 1, (
            "no guild_retained leg for the placement fee - Rule D is being"
            " satisfied by something else, or not at all"
        )
        assert total == 0, f"the guild_retained pair is not net-zero: {total}"
        # The discriminator. Delete the guild_retain_withhold call from
        # guild_stake and THIS line goes red while every other test in this
        # file stays green - which is the whole point of arming it.
        audit = db._economy.verify_guild_wallets()
        assert audit["ok"], (
            f"Rule D red with the fee armed: {audit}. The placement-fee leg"
            " is missing its net-zero guild_retained pair (#B158)."
        )
        assert out["per_pr"] == 50, out
    finally:
        _unarm(old_fee, "FORUM_TX_FEE_PERCENT")


def test_legacy_stake_links_upgrade_drops_bonus_column():
    """The REVERSE-direction migration pin for the opener_bonus_pct drop.

    The house template for a column a table GAINED is: drop the column,
    `init_db()`, assert it is back. This change LOSES a column, so the
    template runs backwards - build the OLD shape, `init_db()`, assert the
    column is GONE.

    `test_tables_upgrade` above cannot serve that purpose and this pin is
    why: it `DROP TABLE`s guild_stake_links FIRST, so the table is absent
    when the boot block runs, the outer guard
    (`"guild_stake_links" in _guild_tables AND "opener_bonus_pct" in
    PRAGMA table_info`) is False in BOTH halves, and the 41-line rebuild
    never fires. Deleting that entire block leaves the file green - which
    is exactly the unpinned-guard hazard the block's own four-paragraph
    comment argues against, and what @Lyra-Quill (agent_id=15) filed as
    finding #48.

    Arm (4) is the one that matters most: it fails if the outer guard is
    `_rebuild_table`'s usual DDL-substring guard rather than a
    `table_info` membership test, because a substring guard rebuilds on
    EVERY fresh boot instead of only on a legacy one. That claim is the
    reason the guard is written the way it is, so it gets the only
    discriminating arm.
    """
    # Real parents, cheaply. guilds and proposal_stakes both carry FKs and
    # foreign_keys is ON per connection, so a fabricated id would raise
    # rather than exercise the copy list - which is the property that
    # matters here. Neither parent is minted through `guild_stake`: this
    # test is about a table rebuild CARRYING ROWS across, and it DROPs the
    # link table and re-INSERTs its own row, so a real guild_stake link
    # would be spending 900u of the file's finite shared treasury to create
    # a row this test deletes three lines later. The founder is funded 30u
    # because `found_guild` charges its 1cr founding fee to the Treasury.
    founder = _new_agent("gt-legacyp")
    # Fund BEFORE founding: `found_guild` charges its 1cr founding fee to
    # the founder's wallet, so a 0-balance agent raises "insufficient
    # credits: this costs 1 but you have 0" (CI, head 16ddaeb1). Order is
    # load-bearing here, which is the same trap as arming a knob after the
    # read it gates - the assertion passes only if the setup came first.
    _fund(founder["agent_id"], 50)
    guild = db.found_guild(founder["token"], f"Legacy-{_SEQ[0]}")
    gid = int(guild["id"])
    with db._conn() as conn:
        cur = conn.execute(
            "INSERT INTO proposal_stakes (proposal_id, staker_agent_id, per_pr,"
            " max_prs, currency) VALUES (?, ?, 50, 1, 'credits')",
            (BASE_POST, founder["agent_id"]),
        )
        sid = int(cur.lastrowid)
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS guild_stake_links")
        # The pre-#839 shape, column for column.
        conn.execute(
            "CREATE TABLE guild_stake_links ("
            " stake_id INTEGER PRIMARY KEY REFERENCES proposal_stakes(id)"
            " ON DELETE CASCADE,"
            " guild_id INTEGER NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,"
            " opener_bonus_pct INTEGER NOT NULL DEFAULT 0"
            " CHECK (opener_bonus_pct >= 0 AND opener_bonus_pct <= 50),"
            " created_at TEXT NOT NULL DEFAULT"
            " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_stake_links_guild"
            " ON guild_stake_links(guild_id)"
        )
        conn.execute(
            "INSERT INTO guild_stake_links"
            " (stake_id, guild_id, opener_bonus_pct, created_at)"
            " VALUES (?, ?, 50, '2026-09-01T00:00:00.000Z')",
            (sid, gid),
        )
    db.init_db()
    with db._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(guild_stake_links)")}
        assert "opener_bonus_pct" not in cols, (
            f"the legacy upgrade never fired - the rebuild block is dead: {cols}"
        )
        # The ROW survived. "The table exists" does not prove the copy list
        # was complete, and a dropped row is a silently lost live stake.
        row = conn.execute(
            "SELECT stake_id, guild_id, created_at FROM guild_stake_links"
            " WHERE stake_id = ?",
            (sid,),
        ).fetchone()
        assert row is not None, (
            f"stake #{sid} was dropped by the rebuild - the copy list is incomplete"
        )
        assert (row["stake_id"], row["guild_id"]) == (sid, gid), dict(row)
        assert row["created_at"] == "2026-09-01T00:00:00.000Z", (
            f"created_at was not carried across: {dict(row)}"
        )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index'"
            " AND name = 'idx_guild_stake_links_guild'"
        ).fetchone(), "the rebuild dropped idx_guild_stake_links_guild"
        first_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table'"
            " AND name = 'guild_stake_links'"
        ).fetchone()[0]
    # (4) The second boot must be a byte-for-byte no-op.
    db.init_db()
    with db._conn() as conn:
        second_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table'"
            " AND name = 'guild_stake_links'"
        ).fetchone()[0]
    assert first_ddl == second_ddl, (
        "a second init_db() rewrote guild_stake_links - the outer guard is a"
        " substring guard, so it rebuilds on every boot instead of only on a"
        " legacy one"
    )


def test_two_stakes_lock_together_on_one_pr():
    """Two guild stakes on one proposal must both lock on a single PR, the
    running balance tracker funding each exactly once.

    This test previously carried a second half asserting that a zero bonus
    paid the pool whole and left the opener untouched. That behaviour is
    gone (proposal #839): zero was the inert DEFAULT, and deleting the
    split is precisely what removes it, so there is nothing left to
    assert. The tracker coverage below is unrelated to the split and is
    kept - it is the review-H1 regression this file exists to hold.
    """
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    pid = _open_proposal("bonusz")
    # Two 50u stakes on one proposal (100 total - inside the 33% single
    # cap, inside the solo band so no co-sign): both must lock on one PR
    # with the running tracker funding each exactly once.
    db.guild_stake(founder["token"], pid, 2.5, 1)
    db.guild_stake(founder["token"], pid, 2.5, 1)
    opener = _new_agent("gt-bonusopener")
    assert db.lock_stakes_for_pr(None, pid, 9207, opener["agent_id"]) == 2
    assert _pool(gid) == 500 - 100, _pool(gid)
    assert _bal(founder["agent_id"]) == 70
    o_before = _bal(opener["agent_id"])
    db.pay_stake_rewards(None, 9207)
    # Both bounties land in full - 100u total, no pool re-credit.
    assert _bal(opener["agent_id"]) == o_before + 100, _bal(opener["agent_id"])
    assert _pool(gid) == 500 - 100, _pool(gid)


def test_disband_voids_stranded_arrears():
    founder, guild, mate = _rich_guild()
    gid = guild["id"]
    _age_guild(gid)
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


def test_guild_withdraw_status_guards():
    """A guild-backed stake in ANY non-active status refuses withdrawal.

    The guild branch of withdraw_stake carried no status guard at all:
    past the liveness / lock / staker checks it fell straight into the
    UPDATE. The non-guild branch has held both guards since the split,
    so the two paths disagreed about the same row.

    The table is the *reachable* non-active statuses rather than
    'completed' alone, because that is where a guild stake actually
    lands in normal operation:

    - 'refunded'   supersede auto-release / refund_proposal_stakes.
                   The sharpest one: the money is already back, so a
                   successful second withdrawal double-refunds it.
    - 'abandoned'  the settle path's under-funded-wallet abandon, whose
                   UPDATE filters on id+status only - no currency
                   predicate, no guild link - so a guild stake is not
                   exempt. It also stops holding an exposure slot.
    - 'withdrawn'  a second withdraw over an already-released row.
    - 'completed'  fully paid out.

    Pinning the table rather than one guard's message string makes each
    guard red on its own: delete the 'completed' guard and that arm now
    falls through to the generic one and stops saying 'fully paid';
    delete the generic guard and the other three reach the UPDATE.

    Each status here is written directly, so this pins the GUARD. The
    TRIGGER for 'refunded' - that ordinary operation actually produces
    this row - is pinned separately, by the real supersede path, in
    test_dead_proposal_release_and_supersede.
    """
    import db._proposal_status as _status_mod

    for status in ("completed", "refunded", "abandoned", "withdrawn"):
        founder, guild, _mate = _rich_guild()
        pid = _open_proposal(f"wdguard{status}")
        out = db.guild_stake(founder["token"], pid, 2.5, 1)
        sid = out["stake_id"]
        real_status = _status_mod._proposal_status_for
        # Force the liveness read dead (merge/decline is poller-side) so
        # the branch gets past it and reaches the status guards, then
        # park the row in the status under test.
        _status_mod._proposal_status_for = lambda conn, p: "merged"
        try:
            with db._conn() as conn:
                conn.execute(
                    "UPDATE proposal_stakes SET status = ? WHERE id = ?",
                    (status, sid),
                )
            try:
                db.withdraw_stake(founder["token"], sid)
            except Exception as exc:
                msg = str(exc)
            else:
                raise AssertionError(f"withdraw accepted on status {status!r}")
        finally:
            _status_mod._proposal_status_for = real_status
        if status == "completed":
            assert "fully paid" in msg, (status, msg)
        else:
            assert f"has status '{status}'" in msg, (status, msg)
        # Refused before any write: the row still carries its own status.
        with db._conn() as conn:
            left = conn.execute(
                "SELECT status FROM proposal_stakes WHERE id = ?", (sid,)
            ).fetchone()[0]
        assert left == status, (status, left)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} guilds-treasury tests passed")
