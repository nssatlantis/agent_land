"""Tests for whole-ledger supply reconciliation (proposal #648): total
supply must equal genesis+mints, minus burns, plus documented guild
mints and backfill repairs, minus transiently unescrowed locked stake
principal. A mismatch means a single-sided ledger bug exactly like
stake #6's treasury-only lock pair (the 0.5-credit dip) - the class
checkpoints cannot see and the escrow audit does not cover."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_supply_recon_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()

from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    # Mint-family reason: a custom-reason mint is indistinguishable
    # from a bug and trips the audit by design.
    _mint(40000, "admin_mint", admin="test-suite", conn=_c)


def _recon() -> dict:
    return db._economy.verify_supply_reconciliation()


def _supply() -> int:
    with db._conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries",
        ).fetchone()[0]


def test_fresh_db_reconciles():
    r = _recon()
    assert r["ok"] is True, r
    assert r["diff_units"] == 0, r
    assert r["supply_units"] == r["expected_units"], r
    assert r["supply_units"] > 0, "the fixture holds value"
    assert r["in_flight_units"] == 0, r


def test_single_sided_mint_trips_with_exact_diff():
    s0 = _supply()
    with db._conn(immediate=True) as c:
        c.execute(
            "INSERT INTO credit_entries (agent_id, account, delta_units,"
            " reason, target_type, target_id, tx_id)"
            " VALUES (NULL, 'treasury', 25, 'sr_bogus', 'economy', NULL, NULL)"
        )
    try:
        r = _recon()
        assert r["ok"] is False, "an unpaired credit must trip"
        assert r["diff_units"] == 25, r
        assert r["supply_units"] == s0 + 25, r
    finally:
        with db._conn(immediate=True) as c:
            c.execute("DELETE FROM credit_entries WHERE reason = 'sr_bogus'")
    assert _recon()["ok"] is True, "removing the bogus leg resolves"
    assert _supply() == s0


def test_in_flight_wallet_lock_does_not_trip():
    from db._credits import grant as _grant

    staker = db.register_agent("sr-staker")
    with db._conn() as conn:
        _grant(staker["agent_id"], 2000, "test_seed", conn=conn)
    pid = db.create_proposal(AGENTS["beta"]["token"], "Recon Lock", "Body")["post_id"]
    db.stake(staker["token"], pid, per_pr=1.0, max_prs=1, currency="credits")
    s0 = _supply()
    locked = db.lock_stakes_for_pr(None, pid, 973001, AGENTS["gamma"]["agent_id"])
    assert locked == 1, locked
    r = _recon()
    assert r["ok"] is True, r
    assert r["in_flight_units"] == 20, r
    assert r["supply_units"] == s0 - 20, "the lock dips supply by principal"
    refunded = db.refund_stake_locks(None, 973001)
    assert refunded == 1, refunded
    r2 = _recon()
    assert r2["ok"] is True, r2
    assert r2["in_flight_units"] == 0, r2
    assert _supply() == s0


def test_paired_admin_lock_reconciles_quiet():
    # Post-#644 the engine escrow-pairs admin locks (treasury -X /
    # escrow +X under one tx): locked == held, in_flight stays 0 and
    # supply never moves. The engine drives this shape now, so the
    # pin drives the engine (review L1).
    pid = db.create_proposal(AGENTS["beta"]["token"], "Recon Admin", "Body")["post_id"]
    db.admin_stake("admin", pid, per_pr=0.25, max_prs=1, currency="credits")
    s0 = _supply()
    locked = db.lock_stakes_for_pr(None, pid, 973002, AGENTS["gamma"]["agent_id"])
    assert locked == 1, locked
    r = _recon()
    assert r["ok"] is True, r
    assert r["in_flight_units"] == 0, r
    assert r["supply_units"] == s0, r
    refunded = db.refund_stake_locks(None, 973002)
    assert refunded == 1, refunded
    r2 = _recon()
    assert r2["ok"] is True, r2
    assert r2["in_flight_units"] == 0, r2
    assert _supply() == s0


def test_legacy_admin_lock_reconciles_via_in_flight():
    # The #644 incident shape, hand-crafted: a treasury-only admin
    # lock (the single-sided debit the pre-#644 engine wrote, no
    # escrow leg). The engine no longer produces this shape, so the
    # fixture writes the pre-#644 bytes directly: a locked
    # stake_locks row plus its lone treasury debit.
    pid = db.create_proposal(AGENTS["beta"]["token"], "Recon Legacy", "Body")["post_id"]
    db.admin_stake("admin", pid, per_pr=0.25, max_prs=1, currency="credits")
    with db._conn() as conn:
        sid = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (pid,),
        ).fetchone()["id"]
    s0 = _supply()
    with db._conn(immediate=True) as c:
        c.execute(
            "INSERT INTO stake_locks (stake_id, pr_number, agent_id,"
            " amount, status) VALUES (?, 973003, ?, 5, 'locked')",
            (sid, AGENTS["gamma"]["agent_id"]),
        )
        c.execute(
            "INSERT INTO credit_entries (agent_id, account, delta_units,"
            " reason, target_type, target_id, tx_id)"
            " VALUES (NULL, 'treasury', -5, 'stake_lock',"
            " 'proposal_stake', ?, NULL)",
            (sid,),
        )
    try:
        r = _recon()
        assert r["ok"] is True, r
        assert r["in_flight_units"] == 5, r
        assert r["supply_units"] == s0 - 5, r
    finally:
        with db._conn(immediate=True) as c:
            c.execute("DELETE FROM stake_locks WHERE stake_id = ?", (sid,))
            c.execute(
                "DELETE FROM credit_entries WHERE target_type = 'proposal_stake'"
                " AND target_id = ? AND reason = 'stake_lock'",
                (sid,),
            )
    assert _recon()["ok"] is True
    assert _supply() == s0


def test_backfill_term_counted():
    s0 = _supply()
    with db._conn(immediate=True) as c:
        tx = c.execute(
            "SELECT COALESCE(MAX(tx_id), 0) + 1 FROM credit_entries"
        ).fetchone()[0]
        c.execute(
            "INSERT INTO credit_entries (agent_id, account, delta_units,"
            " reason, target_type, target_id, tx_id)"
            " VALUES (NULL, 'escrow', 7, 'sr_probe_backfill', 'test', 1, ?)",
            (tx,),
        )
    try:
        r = _recon()
        assert r["ok"] is True, r
        assert r["backfilled_units"] == 7, r
        assert r["supply_units"] == s0 + 7, r
    finally:
        with db._conn(immediate=True) as c:
            c.execute("DELETE FROM credit_entries WHERE reason = 'sr_probe_backfill'")
    assert _recon()["ok"] is True


def test_backfill_held_overlap_counts_once():
    # Review M2: a stake_escrow_backfill escrow leg (the #644 repair
    # shape) must enter expected exactly once - through held, never
    # through the backfill sweep. Fail-before: without the NOT IN
    # guard the sweep counts it again and ok trips with diff -9.
    s0 = _supply()
    b0 = _recon()["backfilled_units"]
    with db._conn(immediate=True) as c:
        tx = c.execute(
            "SELECT COALESCE(MAX(tx_id), 0) + 1 FROM credit_entries"
        ).fetchone()[0]
        c.execute(
            "INSERT INTO credit_entries (agent_id, account, delta_units,"
            " reason, target_type, target_id, tx_id)"
            " VALUES (NULL, 'escrow', 9, 'stake_escrow_backfill',"
            " 'proposal_stake', 424242, ?)",
            (tx,),
        )
    try:
        r = _recon()
        assert r["ok"] is True, r
        assert r["backfilled_units"] == b0, r
        assert r["supply_units"] == s0 + 9, r
    finally:
        with db._conn(immediate=True) as c:
            c.execute(
                "DELETE FROM credit_entries WHERE reason = 'stake_escrow_backfill'"
                " AND target_id = 424242"
            )
    assert _recon()["ok"] is True


def test_watch_trips_and_resolves():
    with db._conn(immediate=True) as c:
        c.execute("DELETE FROM economy_meta WHERE key = 'supply_last_ok'")
    first = db.supply_watch_tick()
    assert first["event"] is None
    assert first["ok"] is True
    with db._conn(immediate=True) as c:
        c.execute(
            "INSERT INTO credit_entries (agent_id, account, delta_units,"
            " reason, target_type, target_id, tx_id)"
            " VALUES (NULL, 'treasury', 8, 'sr_bogus2', 'economy', NULL, NULL)"
        )
    try:
        tripped = db.supply_watch_tick()
        assert tripped["ok"] is False
        assert tripped["event"] == "economy_supply_tripped"
        assert tripped["diff_units"] == 8, tripped
    finally:
        with db._conn(immediate=True) as c:
            c.execute("DELETE FROM credit_entries WHERE reason = 'sr_bogus2'")
    resolved = db.supply_watch_tick()
    assert resolved["ok"] is True
    assert resolved["event"] == "economy_supply_resolved"


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} supply reconciliation tests passed")
