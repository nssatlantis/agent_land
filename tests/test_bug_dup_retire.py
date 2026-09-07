"""Tests for duplicate retirement: confirming or fixing a report retires
its duplicate rows to the same status, and the orphan sweep repairs
pre-helper dead letters."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bugretire_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()
ALPHA = AGENTS["alpha"]["token"]
BETA = AGENTS["beta"]["token"]
GAMMA = AGENTS["gamma"]["token"]


def _status(rid):
    with db._conn() as conn:
        return conn.execute(
            "SELECT status, confidence, decided_at FROM bug_reports WHERE id = ?",
            (rid,),
        ).fetchone()


def _open_count():
    with db._conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM bug_reports WHERE status = 'open'"
        ).fetchone()[0]


def test_crossing_retires_parent_and_trigger_dup():
    url = "https://example.com/bug/retire-cross"
    orig = db.file_bug_report(ALPHA, "Retire cross", "body", url=url)["id"]
    d1 = db.file_bug_report(BETA, "Retire cross dup", "body", url=url)
    assert d1["duplicate_of"] == orig
    assert d1["status"] == "open"
    assert _status(orig)["status"] == "open"
    d2 = db.file_bug_report(GAMMA, "Retire cross dup2", "body", url=url)
    assert d2["duplicate_of"] == orig
    assert d2["status"] == "confirmed"
    assert _status(orig)["status"] == "confirmed"
    assert _status(orig)["confidence"] == 3
    for rid in (d1["id"], d2["id"]):
        st = _status(rid)
        assert st["status"] == "confirmed", (rid, dict(st))
        assert st["decided_at"] is not None
    assert _open_count() == 0


def test_admin_confirm_retires_duplicates():
    url = "https://example.com/bug/retire-confirm"
    orig = db.file_bug_report(ALPHA, "Retire confirm", "body", url=url)["id"]
    d1 = db.file_bug_report(BETA, "Retire confirm dup", "body", url=url)["id"]
    db.confirm_bug_report(orig, admin="testadmin")
    assert _status(orig)["status"] == "confirmed"
    st = _status(d1)
    assert st["status"] == "confirmed"
    assert st["decided_at"] is not None


def test_admin_fix_retires_duplicates():
    url = "https://example.com/bug/retire-fix"
    orig = db.file_bug_report(ALPHA, "Retire fix", "body", url=url)["id"]
    d1 = db.file_bug_report(BETA, "Retire fix dup", "body", url=url)["id"]
    db.fix_bug_report(orig, admin="testadmin")
    assert _status(orig)["status"] == "fixed"
    st = _status(d1)
    assert st["status"] == "fixed"
    assert st["decided_at"] is not None


def test_sweep_repairs_orphans_and_is_idempotent():
    url = "https://example.com/bug/retire-orphan"
    orig = db.file_bug_report(ALPHA, "Retire orphan", "body", url=url)["id"]
    d1 = db.file_bug_report(BETA, "Retire orphan dup", "body", url=url)["id"]
    # Simulate a pre-helper dead letter: parent resolved, dup left open.
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE bug_reports SET status = 'fixed',"
            " decided_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (orig,),
        )
    with db._conn() as conn:
        assert db.sweep_retire_duplicates(conn) == 1
    st = _status(d1)
    assert st["status"] == "fixed"
    assert st["decided_at"] is not None
    with db._conn() as conn:
        assert db.sweep_retire_duplicates(conn) == 0


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} dup-retire tests passed")
