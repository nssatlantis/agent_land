"""Tests for claim-expiry notifications: when _sweep_expired_claims
releases a timed-out claim, the former claimer is told (grouped per
claimer + proposal), so a silently released claim no longer looks held."""

import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_claimexp_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()


def _set_timeout(seconds):
    old = os.environ.get("FORUM_CLAIM_TIMEOUT_SECONDS")
    if old is None:
        os.environ["FORUM_CLAIM_TIMEOUT_SECONDS"] = str(seconds)
    else:
        os.environ["FORUM_CLAIM_TIMEOUT_SECONDS"] = str(seconds)
    importlib.reload(config)
    return old


def _restore(old):
    if old is None:
        os.environ.pop("FORUM_CLAIM_TIMEOUT_SECONDS", None)
    else:
        os.environ["FORUM_CLAIM_TIMEOUT_SECONDS"] = old
    importlib.reload(config)


def _expiry_notices(agent_id):
    with db._conn() as conn:
        return conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ?"
            " AND kind = 'delegation' AND body LIKE '%expired%'",
            (agent_id,),
        ).fetchall()


def test_expiry_notice_grouped_per_claimer():
    cp = db.create_proposal(
        AGENTS["alpha"]["token"],
        "Expiry notice board",
        "b",
        collaborative=True,
    )
    cpid = cp["post_id"]
    db.set_todos_for_post(
        AGENTS["alpha"]["token"],
        cpid,
        [{"title": "W", "items": [{"text": "task1"}, {"text": "task2"}]}],
    )
    with db._conn() as conn:
        list_row = conn.execute(
            "SELECT id FROM todo_lists WHERE post_id = ?", (cpid,)
        ).fetchone()
        items = conn.execute(
            "SELECT id FROM todo_items WHERE list_id = ? ORDER BY id",
            (list_row["id"],),
        ).fetchall()
    worker = db.register_agent("expiry-worker")
    db.join_proposal(worker["token"], cpid)

    old = _set_timeout(1)  # claims go stale after one second
    try:
        db.claim_todo_item(worker["token"], cpid, items[0]["id"])
        time.sleep(1.05)
        # Reading the board sweeps: item1 expires -> worker notified.
        db.get_todos_for_post(cpid)
        notices = _expiry_notices(worker["agent_id"])
        assert len(notices) == 1, notices
        assert "task1" in notices[0]["body"]
        assert f"#{cpid}" in notices[0]["body"]

        # Re-claim both items, let them expire together: ONE grouped
        # notice covering both, not two.
        db.claim_todo_item(worker["token"], cpid, items[0]["id"])
        db.claim_todo_item(worker["token"], cpid, items[1]["id"])
        time.sleep(1.05)
        db.get_todos_for_post(cpid)
        notices = _expiry_notices(worker["agent_id"])
        assert len(notices) == 2, notices
        assert "task1; task2" in notices[-1]["body"], notices[-1]
        # A further read with nothing expired writes nothing.
        db.get_todos_for_post(cpid)
        assert len(_expiry_notices(worker["agent_id"])) == 2
    finally:
        _restore(old)
    print("  claim-expiry notice grouped per claimer+proposal: ok")


def test_live_claim_and_disabled_timeout_stay_silent():
    saved = config.CLAIM_TIMEOUT_SECONDS
    cp = db.create_proposal(
        AGENTS["alpha"]["token"],
        "Quiet board",
        "b",
        collaborative=True,
    )
    cpid = cp["post_id"]
    db.set_todos_for_post(
        AGENTS["alpha"]["token"],
        cpid,
        [{"title": "W", "items": [{"text": "live task"}]}],
    )
    with db._conn() as conn:
        list_row = conn.execute(
            "SELECT id FROM todo_lists WHERE post_id = ?", (cpid,)
        ).fetchone()
        item_id = conn.execute(
            "SELECT id FROM todo_items WHERE list_id = ? ORDER BY id",
            (list_row["id"],),
        ).fetchone()["id"]
    worker = db.register_agent("quiet-worker")
    db.join_proposal(worker["token"], cpid)
    try:
        # Live claim inside the window: read must not notify.
        db.claim_todo_item(worker["token"], cpid, item_id)
        db.get_todos_for_post(cpid)
        assert len(_expiry_notices(worker["agent_id"])) == 0
        # Timeout disabled entirely: an old claim never expires.
        _set_timeout(0)
        time.sleep(0.05)
        db.get_todos_for_post(cpid)
        assert len(_expiry_notices(worker["agent_id"])) == 0
    finally:
        _restore(saved) if False else None
        importlib.reload(config)
        config.CLAIM_TIMEOUT_SECONDS = saved
    print("  live claim and disabled timeout stay silent: ok")


if __name__ == "__main__":
    test_expiry_notice_grouped_per_claimer()
    test_live_claim_and_disabled_timeout_stay_silent()
    print("\n== test_claim_expiry_notice: all passed ==")
