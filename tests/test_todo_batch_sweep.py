"""Pin #B46: the batch todo reader sweeps expired list claims BEFORE the
list fetch, so a stale whole-list claim never renders as live.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_todo_batch_sweep_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db._proposal_todos._reads as _reads  # noqa: E402
from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()


def _set_timeout(seconds):
    old = os.environ.get("FORUM_CLAIM_TIMEOUT_SECONDS")
    os.environ["FORUM_CLAIM_TIMEOUT_SECONDS"] = str(seconds)
    importlib.reload(config)
    return old


def _restore_timeout(old):
    if old is None:
        os.environ.pop("FORUM_CLAIM_TIMEOUT_SECONDS", None)
    else:
        os.environ["FORUM_CLAIM_TIMEOUT_SECONDS"] = old
    importlib.reload(config)


def _make_board():
    prop = db.create_proposal(
        AGENTS["alpha"]["token"],
        "Batch sweep fixture",
        "Body",
        collaborative=True,
    )
    pid = prop["post_id"]
    db.set_todos_for_post(
        AGENTS["alpha"]["token"],
        pid,
        [{"title": "W", "items": [{"text": "task1"}]}],
    )
    db.set_todo_claim_mode(AGENTS["alpha"]["token"], pid, "list")
    worker = db.register_agent("batch-sweep-worker")
    db.join_proposal(worker["token"], pid)
    with db._conn() as conn:
        lid = conn.execute(
            "SELECT id FROM todo_lists WHERE post_id = ?", (pid,)
        ).fetchone()["id"]
    return pid, lid, worker


def _batch_lists(pid):
    with db._conn() as conn:
        return _reads._todos_for_posts(conn, [pid])[pid]


def test_batch_reader_sweeps_expired_list_claim_first():
    pid, lid, worker = _make_board()
    old = _set_timeout(1)  # claims go stale after one second
    try:
        db.claim_todo_list(worker["token"], pid, lid)
        # Fast-forward without wall sleep: make the claim look 2s old.
        with db._conn() as conn:
            conn.execute(
                "UPDATE todo_lists SET claimed_at ="
                " strftime('%Y-%m-%dT%H:%M:%fZ','now','-2 seconds')"
                " WHERE id = ?",
                (lid,),
            )
        # Control first: a live claim still renders as held.
        db.claim_todo_list(worker["token"], pid, lid)
        live = _batch_lists(pid)
        assert len(live) == 1
        assert live[0].get("claimed_by_id") == worker["agent_id"], live[0]
        # Age it out: the batch read must sweep before rendering.
        with db._conn() as conn:
            conn.execute(
                "UPDATE todo_lists SET claimed_at ="
                " strftime('%Y-%m-%dT%H:%M:%fZ','now','-2 seconds')"
                " WHERE id = ?",
                (lid,),
            )
        swept = _batch_lists(pid)
        assert len(swept) == 1
        assert "claimed_by" not in swept[0], swept[0]
    finally:
        _restore_timeout(old)
    print("  batch reader sweeps expired list claim first: ok")


if __name__ == "__main__":
    test_batch_reader_sweeps_expired_list_claim_first()
    print("\n== test_todo_batch_sweep: all passed ==")
