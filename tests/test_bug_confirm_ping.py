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

import config  # noqa: E402
from tests._setup import db, setup  # noqa: E402

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


if __name__ == "__main__":
    test_confirmation_pings_filers_once()
    print("\n== test_bug_confirm_ping: all passed ==")
