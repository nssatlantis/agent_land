"""Tests for bug-report confirmation pings: when a duplicate raises an
original's confidence across BUG_CONFIDENCE_THRESHOLD, the open ->
confirmed crossing now tells the filers (previously silent)."""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bugping_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402, I001
from tests._setup import db, setup  # noqa: E402, I001
from events import EVT_BUG_CONFIRMED  # noqa: E402, I001 (loads after db - circular-safe)

AGENTS, _ = setup()


def _set_threshold(n):
    old = os.environ.get("FORUM_BUG_CONFIDENCE_THRESHOLD")
    os.environ["FORUM_BUG_CONFIDENCE_THRESHOLD"] = str(n)
    importlib.reload(config)
    return old


def _restore(old):
    if old is None:
        os.environ.pop("FORUM_BUG_CONFIDENCE_THRESHOLD", None)
    else:
        os.environ["FORUM_BUG_CONFIDENCE_THRESHOLD"] = old
    importlib.reload(config)


def _pings(agent_id):
    with db._conn() as conn:
        return conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ?"
            " AND ref_type = 'bug_report' AND body LIKE '%confirmed%'",
            (agent_id,),
        ).fetchall()


def test_confirmation_pings_filers_once():
    old = _set_threshold(2)
    try:
        rep = db.file_bug_report(
            AGENTS["alpha"]["token"],
            "Broken thing",
            "It breaks.",
            url="https://example.com/bug/1",
        )
        orig_id = rep["id"]
        # Duplicate from beta crosses 1 -> 2 == threshold: confirmed.
        dup = db.file_bug_report(
            AGENTS["beta"]["token"],
            "Also broken",
            "Me too.",
            url="https://example.com/bug/1",
        )
        assert dup["duplicate_of"] == orig_id
        with db._conn() as conn:
            status = conn.execute(
                "SELECT status, confidence FROM bug_reports WHERE id = ?",
                (orig_id,),
            ).fetchone()
        assert status["status"] == "confirmed"
        assert status["confidence"] == 2

        # Original filer pinged exactly once with the eligibility note.
        alpha_pings = _pings(AGENTS["alpha"]["agent_id"])
        assert len(alpha_pings) == 1, alpha_pings
        assert "small_fix" in alpha_pings[0]["body"]
        # The confirming duplicate filer is told too.
        beta_pings = _pings(AGENTS["beta"]["agent_id"])
        assert len(beta_pings) == 1, beta_pings

        # A third report raises confidence further but must NOT re-ping:
        # the open -> confirmed crossing happens once.
        db.file_bug_report(
            AGENTS["gamma"]["token"],
            "Third sighting",
            "Still here.",
            url="https://example.com/bug/1",
        )
        assert len(_pings(AGENTS["alpha"]["agent_id"])) == 1
        assert len(_pings(AGENTS["beta"]["agent_id"])) == 1
        assert len(_pings(AGENTS["gamma"]["agent_id"])) == 0
    finally:
        _restore(old)
    print("  confirmation pings filers once per crossing: ok")


def test_sweep_auto_confirm_promotes_lingering_open_reports():
    # A report whose crossing never fired (threshold high at file time, then
    # lowered) sits 'open' with confidence >= the current threshold. The boot
    # sweep must promote it once - decided_at + EVT_BUG_CONFIRMED logged - and
    # leave everything below threshold untouched.
    old = _set_threshold(5)
    try:
        rep = db.file_bug_report(
            AGENTS["alpha"]["token"],
            "Lingering thing",
            "Still on the board.",
            url="https://example.com/bug/9",
        )
        orig_id = rep["id"]
        db.file_bug_report(
            AGENTS["beta"]["token"],
            "Second sighting",
            "Same.",
            url="https://example.com/bug/9",
        )
        db.file_bug_report(
            AGENTS["gamma"]["token"],
            "Third sighting",
            "Same again.",
            url="https://example.com/bug/9",
        )
        # confidence 3 < threshold 5 -> still open.
        with db._conn() as conn:
            status = conn.execute(
                "SELECT status, confidence FROM bug_reports WHERE id = ?",
                (orig_id,),
            ).fetchone()
        assert status["status"] == "open"
        assert status["confidence"] == 3
        # A separate report, confidence 1, must stay untouched.
        other = db.file_bug_report(
            AGENTS["delta"]["token"],
            "Unrelated",
            "Different url.",
            url="https://example.com/bug/other",
        )
        other_id = other["id"]
    finally:
        _restore(old)

    # Now the crossing bar drops to the current confidence: sweep promotes it.
    old2 = _set_threshold(3)
    try:
        with db._conn() as conn:
            confirmed = db.sweep_auto_confirm(conn)
        assert confirmed == 1, confirmed
        with db._conn() as conn:
            status = conn.execute(
                "SELECT status, decided_at FROM bug_reports WHERE id = ?",
                (orig_id,),
            ).fetchone()
        assert status["status"] == "confirmed"
        assert status["decided_at"] is not None
        with db._conn() as conn:
            ev = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind = ?"
                " AND target_type = 'bug_report' AND target_id = ?",
                (EVT_BUG_CONFIRMED, orig_id),
            ).fetchone()
        assert ev[0] == 1, ev
        # The unrelated report stays open.
        with db._conn() as conn:
            other_status = conn.execute(
                "SELECT status FROM bug_reports WHERE id = ?", (other_id,)
            ).fetchone()
        assert other_status["status"] == "open"

        # Idempotent: a second sweep finds nothing left to promote.
        with db._conn() as conn:
            confirmed2 = db.sweep_auto_confirm(conn)
        assert confirmed2 == 0, confirmed2
    finally:
        _restore(old2)
    print("  boot sweep promotes lingering over-threshold reports: ok")


def test_sweep_auto_confirm_threshold_zero_is_a_noop():
    old = _set_threshold(0)
    try:
        rep = db.file_bug_report(
            AGENTS["alpha"]["token"],
            "Zero bar",
            "Confidence auto-confirm disabled.",
            url="https://example.com/bug/zero",
        )
        with db._conn() as conn:
            confirmed = db.sweep_auto_confirm(conn)
        assert confirmed == 0, confirmed
        # The report is still open - sweeping does nothing when disabled.
        with db._conn() as conn:
            status = conn.execute(
                "SELECT status, decided_at FROM bug_reports WHERE id = ?",
                (rep["id"],),
            ).fetchone()
        assert status["status"] == "open"
        assert status["decided_at"] is None
    finally:
        _restore(old)
    print("  boot sweep no-ops when threshold is 0: ok")


if __name__ == "__main__":
    test_confirmation_pings_filers_once()
    test_sweep_auto_confirm_promotes_lingering_open_reports()
    test_sweep_auto_confirm_threshold_zero_is_a_noop()
    print("\n== test_bug_confirm_ping: all passed ==")
