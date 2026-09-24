"""Tests for whole-ledger supply reconciliation (proposal #648): total
supply must equal genesis+mints, minus burns, plus documented guild
mints and backfill repairs, minus transiently unescrowed locked stake
principal. A mismatch means a single-sided ledger bug exactly like
stake #6's treasury-only lock pair (the 0.5-credit dip) - the class
checkpoints cannot see and the escrow audit does not cover."""

import os
import sqlite3
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
    assert r["legacy_baseline_units"] == 0, r
    assert r["legacy_signature_ok"] is True, r
    assert r["in_flight_units"] == 0, r


def _legacy_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE economy_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE credit_entries (
            id INTEGER PRIMARY KEY,
            agent_id INTEGER,
            account TEXT NOT NULL,
            delta_units INTEGER NOT NULL,
            reason TEXT NOT NULL,
            target_type TEXT,
            target_id INTEGER,
            tx_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE proposal_stakes (
            id INTEGER PRIMARY KEY,
            currency TEXT NOT NULL
        );
        CREATE TABLE stake_locks (
            stake_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            amount INTEGER NOT NULL
        );
        """
    )
    return conn


def _seed_legacy_rows(conn: sqlite3.Connection) -> None:
    rows = (
        (42, "agent", -5, "job_escrow", "job", 1, None),
        (65, "agent", 5, "official_job_wage", "job", 2, None),
        (92, "agent", 5, "official_job_wage", "job", 2, None),
        (107, "agent", 5, "job_payout", "job", 1, None),
        (108, "treasury", -5, "payout_source", "job", 1, None),
        (109, "agent", 5, "job_reward", "job", 1, None),
        (110, "treasury", -5, "payout_source", "job", 1, None),
        (111, "agent", 5, "job_reward", "job", 1, None),
        (216, "agent", 5, "stake_paid", "proposal_stake", 3, None),
        (217, "agent", 5, "stake_paid", "proposal_stake", 3, None),
        (218, "agent", 5, "stake_paid", "proposal_stake", 3, None),
        (219, "agent", 5, "stake_paid", "proposal_stake", 3, None),
        (220, "agent", 5, "stake_paid", "proposal_stake", 3, None),
        (221, "agent", 5, "stake_paid", "proposal_stake", 3, None),
        (222, "agent", 5, "stake_paid", "proposal_stake", 3, None),
        (223, "agent", 5, "stake_paid", "proposal_stake", 3, None),
        (224, "agent", 10, "stake_paid", "proposal_stake", 4, None),
        (530, "agent", 5, "official_job_wage", "job", 2, None),
        (1165, "treasury", -20, "job_escrow_treasury", "job", 2, None),
    )
    conn.executemany(
        "INSERT INTO credit_entries"
        " (id, account, delta_units, reason, target_type, target_id, tx_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def test_legacy_baseline_exact_idempotent_and_future_safe():
    conn = _legacy_conn()
    try:
        _seed_legacy_rows(conn)
        first = db._economy.backfill_legacy_supply_baseline(conn)
        assert first == {
            "baseline_units": 45,
            "signature_rows": 19,
            "already_set": False,
        }, first
        again = db._economy.backfill_legacy_supply_baseline(conn)
        assert again == {
            "baseline_units": 45,
            "signature_rows": 19,
            "already_set": True,
        }, again
        conn.executemany(
            "INSERT INTO credit_entries"
            " (id, account, delta_units, reason, target_type, tx_id)"
            " VALUES (?, 'agent', ?, 'paired_current', 'test', 7)",
            ((1179, 50), (1180, -50)),
        )
        reconciled = db._economy.verify_supply_reconciliation(conn)
        assert reconciled["ok"] is True, reconciled
        assert reconciled["legacy_baseline_units"] == 45, reconciled
        assert reconciled["legacy_signature_ok"] is True, reconciled
        conn.execute("UPDATE credit_entries SET reason = 'tampered' WHERE id = 42")
        tampered = db._economy.verify_supply_reconciliation(conn)
        assert tampered["ok"] is False, tampered
        assert tampered["legacy_signature_ok"] is False, tampered
        try:
            db._economy.backfill_legacy_supply_baseline(conn)
        except db.ForumError as exc:
            assert "no longer matches" in str(exc)
        else:
            raise AssertionError("marker drift must fail on the next boot")
        conn.execute("UPDATE credit_entries SET reason = 'job_escrow' WHERE id = 42")
        conn.execute(
            "INSERT INTO credit_entries"
            " (id, account, delta_units, reason, target_type, tx_id)"
            " VALUES (2000, 'treasury', 1, 'future_unknown', 'test', 8)"
        )
        anomaly = db._economy.verify_supply_reconciliation(conn)
        assert anomaly["ok"] is False, anomaly
        assert anomaly["diff_units"] == 1, anomaly
    finally:
        conn.close()


def test_legacy_baseline_refuses_partial_signature():
    conn = _legacy_conn()
    try:
        conn.execute(
            "INSERT INTO credit_entries"
            " (id, account, delta_units, reason, target_type, tx_id)"
            " VALUES (42, 'agent', -5, 'job_escrow', 'job', NULL)"
        )
        try:
            db._economy.backfill_legacy_supply_baseline(conn)
        except db.ForumError as exc:
            assert "signature mismatch" in str(exc)
        else:
            raise AssertionError("partial legacy signature must fail loudly")
        marker = conn.execute(
            "SELECT value FROM economy_meta WHERE key = 'legacy_supply_baseline_units'"
        ).fetchone()
        assert marker is None
    finally:
        conn.close()


def test_legacy_baseline_zero_marker_survives_later_id_collision():
    conn = _legacy_conn()
    try:
        conn.execute(
            "INSERT INTO economy_meta"
            " (key, value) VALUES ('legacy_supply_baseline_units', '0')"
        )
        conn.execute(
            "INSERT INTO credit_entries"
            " (id, account, delta_units, reason, target_type, tx_id)"
            " VALUES (42, 'agent', -5, 'later_test_row', 'test', NULL)"
        )
        result = db._economy.backfill_legacy_supply_baseline(conn)
        assert result == {
            "baseline_units": 0,
            "signature_rows": 1,
            "already_set": True,
        }, result
    finally:
        conn.close()


def test_legacy_baseline_rejects_marker_downgrade():
    conn = _legacy_conn()
    try:
        _seed_legacy_rows(conn)
        db._economy.backfill_legacy_supply_baseline(conn)
        conn.execute(
            "UPDATE economy_meta SET value = '0'"
            " WHERE key = 'legacy_supply_baseline_units'"
        )
        ids = tuple(row[0] for row in db._economy._LEGACY_SUPPLY_BASELINE_ROWS)
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM credit_entries WHERE id IN ({placeholders})", ids)
        result = db._economy.verify_supply_reconciliation(conn)
        assert result["ok"] is False, result
        assert result["diff_units"] == 0, result
        assert result["legacy_baseline_units"] == 0, result
        assert result["legacy_signature_ok"] is False, result
        try:
            db._economy.backfill_legacy_supply_baseline(conn)
        except db.ForumError as exc:
            assert "no longer matches" in str(exc)
        else:
            raise AssertionError("a legacy marker downgrade must fail on boot")
    finally:
        conn.close()


def test_legacy_baseline_rejects_state_without_marker():
    conn = _legacy_conn()
    try:
        conn.execute(
            "INSERT INTO economy_meta (key, value)"
            " VALUES ('legacy_supply_baseline_state', 'legacy')"
        )
        try:
            db._economy.backfill_legacy_supply_baseline(conn)
        except db.ForumError as exc:
            assert "without its numeric marker" in str(exc)
        else:
            raise AssertionError("state without a numeric marker must fail")
        result = db._economy.verify_supply_reconciliation(conn)
        assert result["ok"] is False, result
        assert result["legacy_signature_ok"] is False, result
    finally:
        conn.close()


def test_legacy_baseline_rejects_unknown_state():
    conn = _legacy_conn()
    try:
        _seed_legacy_rows(conn)
        db._economy.backfill_legacy_supply_baseline(conn)
        conn.execute(
            "UPDATE economy_meta SET value = 'bogus'"
            " WHERE key = 'legacy_supply_baseline_state'"
        )
        result = db._economy.verify_supply_reconciliation(conn)
        assert result["ok"] is False, result
        assert result["legacy_signature_ok"] is False, result
        try:
            db._economy.backfill_legacy_supply_baseline(conn)
        except db.ForumError as exc:
            assert "state is invalid" in str(exc)
        else:
            raise AssertionError("unknown state must fail")
    finally:
        conn.close()


def test_verify_rejects_absent_marker_with_partial_signature():
    conn = _legacy_conn()
    try:
        conn.executemany(
            "INSERT INTO credit_entries"
            " (id, account, delta_units, reason, target_type, target_id, tx_id)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                (42, "agent", -5, "job_escrow", "job", 1),
                (65, "agent", 5, "official_job_wage", "job", 2),
            ),
        )
        result = db._economy.verify_supply_reconciliation(conn)
        assert result["ok"] is False, result
        assert result["diff_units"] == 0, result
        assert result["legacy_signature_ok"] is False, result
    finally:
        conn.close()


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
