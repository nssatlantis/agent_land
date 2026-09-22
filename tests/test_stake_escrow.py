"""Tests for escrow-paired admin stake locks (proposal #644): the lock
parks treasury funds in escrow under one tx (supply-invariant), pay
releases escrow -> winner, refunds and dupe-guard reverts return escrow
-> treasury, and the one-time backfill repairs pre-pairing locks. The
live incident: single-sided treasury debits dropped supply 1000 -> 999.5
on stake #6 (ledger #2143/#2144)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_stake_escrow_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()

from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(40000, "test_suite_topup", admin="test-suite", conn=_c)


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


def _treasury() -> int:
    with db._conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE account = 'treasury'",
        ).fetchone()[0]


def _bal(agent_id: int) -> int:
    from db._credits import balance_for

    with db._conn() as c:
        return balance_for(c, agent_id)


def _admin_stake(title: str, per_pr: float = 0.25, max_prs: int = 2) -> int:
    # No votes needed: admin_stake only requires an open proposal.
    # (Proposal votes need earned karma, which fresh fixture agents
    # lack - voting here would refuse with ForumError.)
    pid = db.create_proposal(AGENTS["beta"]["token"], title, "Body")["post_id"]
    db.admin_stake("admin", pid, per_pr=per_pr, max_prs=max_prs, currency="credits")
    with db._conn() as conn:
        return conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (pid,),
        ).fetchone()["id"]


def _legs(stake_id: int) -> list:
    with db._conn() as conn:
        return conn.execute(
            "SELECT account, delta_units, reason, tx_id FROM credit_entries"
            " WHERE target_type = 'proposal_stake' AND target_id = ?"
            " ORDER BY id",
            (stake_id,),
        ).fetchall()


def test_admin_lock_pairs_legs_supply_neutral():
    sid = _admin_stake("Escrow Lock")
    pid = _pid_of(sid)
    s0, t0, e0 = _supply(), _treasury(), _escrow()
    locked = db.lock_stakes_for_pr(None, pid, 97200, AGENTS["gamma"]["agent_id"])
    assert locked == 1, locked
    assert _supply() == s0, "paired escrow lock never moves supply"
    assert _treasury() == t0 - 5, "treasury funds the lock"
    assert _escrow() == e0 + 5, "the lock parks in escrow"
    legs = _legs(sid)
    assert [(r["account"], r["delta_units"], r["reason"]) for r in legs] == [
        ("treasury", -5, "stake_lock"),
        ("escrow", 5, "stake_lock_held"),
    ]
    assert legs[0]["tx_id"] is not None
    assert legs[0]["tx_id"] == legs[1]["tx_id"], "one tx, zero-sum"
    assert db.economy_overview()["conservation"]["ok"] is True


def test_admin_pay_moves_escrow_to_winner():
    sid = _admin_stake("Escrow Pay")
    pid = _pid_of(sid)
    opener = AGENTS["gamma"]["agent_id"]
    w0 = _bal(opener)
    s0, t0, e0 = _supply(), _treasury(), _escrow()
    db.lock_stakes_for_pr(None, pid, 97201, opener)
    paid = db.pay_stake_rewards(None, 97201)
    assert paid == 1, paid
    assert _supply() == s0, "escrow payout never moves supply"
    assert _escrow() == e0, "the holding drew down"
    assert _treasury() == t0 - 5, "the community paid the winner"
    assert _bal(opener) == w0 + 5, "the winner nets the stake"
    legs = _legs(sid)
    assert ("agent", 5, "stake_paid") in [
        (r["account"], r["delta_units"], r["reason"]) for r in legs
    ]
    assert db.economy_overview()["conservation"]["ok"] is True


def test_admin_refund_returns_escrow_to_treasury():
    sid = _admin_stake("Escrow Refund")
    pid = _pid_of(sid)
    s0, t0, e0 = _supply(), _treasury(), _escrow()
    db.lock_stakes_for_pr(None, pid, 97202, AGENTS["gamma"]["agent_id"])
    refunded = db.refund_stake_locks(None, 97202)
    assert refunded == 1, refunded
    assert _supply() == s0, "refund never moves supply"
    assert _treasury() == t0, "treasury is whole again"
    assert _escrow() == e0, "escrow is whole again"
    legs = _legs(sid)
    assert ("treasury", 5, "stake_refund") in [
        (r["account"], r["delta_units"], r["reason"]) for r in legs
    ]
    assert db.economy_overview()["conservation"]["ok"] is True


def test_dupe_lock_reverts_paired():
    sid = _admin_stake("Escrow Dupe")
    pid = _pid_of(sid)
    db.lock_stakes_for_pr(None, pid, 97203, AGENTS["gamma"]["agent_id"])
    s1, t1, e1 = _supply(), _treasury(), _escrow()
    locked2 = db.lock_stakes_for_pr(None, pid, 97203, AGENTS["gamma"]["agent_id"])
    assert locked2 == 0, locked2
    assert _supply() == s1, "dupe revert never moves supply"
    assert _treasury() == t1, "dupe revert nets zero on treasury"
    assert _escrow() == e1, "dupe revert nets zero on escrow"
    with db._conn() as conn:
        bare = conn.execute(
            "SELECT COUNT(*) FROM credit_entries"
            " WHERE target_type = 'proposal_stake' AND target_id = ?"
            " AND tx_id IS NULL",
            (sid,),
        ).fetchone()[0]
    assert bare == 0, "every stake leg rides a tx"
    assert db.economy_overview()["conservation"]["ok"] is True


def test_backfill_repairs_legacy_lock():
    sid = _admin_stake("Escrow Backfill")
    pid = _pid_of(sid)
    db.lock_stakes_for_pr(None, pid, 97204, AGENTS["gamma"]["agent_id"])
    s0 = _supply()
    with db._conn(immediate=True) as c:
        c.execute(
            "DELETE FROM credit_entries WHERE reason = 'stake_lock_held'"
            " AND target_id = ?",
            (sid,),
        )
        c.execute(
            "UPDATE credit_entries SET tx_id = NULL WHERE reason = 'stake_lock'"
            " AND target_id = ?",
            (sid,),
        )
        c.execute("DELETE FROM economy_meta WHERE key = 'stake_escrow_live'")
    assert _supply() == s0 - 5, "the legacy shape destroyed supply"
    assert db.economy_overview()["conservation"]["ok"] is False
    res = db._economy.backfill_stake_escrow()
    assert res["backfilled_units"] == 5 and res["stakes"] == 1, res
    assert _supply() == s0, "the repair restores destroyed supply"
    assert db.economy_overview()["conservation"]["ok"] is True
    res2 = db._economy.backfill_stake_escrow()
    assert res2["already_live"] is True
    assert res2["backfilled_units"] == 0


def test_overview_identity_holds_with_open_admin_lock():
    sid = _admin_stake("Escrow Identity")
    pid = _pid_of(sid)
    db.lock_stakes_for_pr(None, pid, 97205, AGENTS["gamma"]["agent_id"])
    assert sid > 0
    ov = db.economy_overview()
    assert ov["conservation"]["ok"], ov["conservation"]
    assert ov["total_supply_units"] == (
        ov["treasury_units"]
        + ov["circulating_units"]
        + ov["held_in_job_escrow_units"]
        + ov["held_in_guild_pools_units"]
        + ov["held_in_bond_escrow_units"]
    )


def _pid_of(stake_id: int) -> int:
    with db._conn() as conn:
        return conn.execute(
            "SELECT proposal_id FROM proposal_stakes WHERE id = ?",
            (stake_id,),
        ).fetchone()["proposal_id"]


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} stake escrow tests passed")
