"""Tests for tool-call observability (db._tool_usage): the per-call ledger
recorded from the MCP _logged wrapper, the fold-and-prune sweep into the
long-term aggregate, and the readers that back the admin /admin/usage page."""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_tool_usage_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
# Deterministic window for the fold/prune sweep tests.
os.environ["FORUM_TOOL_USAGE_RETENTION_DAYS"] = "30"
os.environ["FORUM_TOOL_USAGE_NOTE_CAP"] = "200"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402, I001

db.init_db()

AGENTS, _ = setup()  # once per process - names are unique


def _iso(days_ago: int) -> str:
    """ISO timestamp `days_ago` UTC-days in the past (the app's storage
    format, %Y-%m-%dT%H:%M:%S.mmmZ)."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt.microsecond // 1000):03d}Z"


def _ledger_rows() -> list:
    with db._conn() as conn:
        return conn.execute(
            "SELECT tool, ok, agent_id, note, created_at FROM tool_calls"
        ).fetchall()


def _usage_rows() -> list:
    with db._conn() as conn:
        return conn.execute(
            "SELECT tool, day, calls, ok, failed, distinct_agents"
            " FROM tool_usage ORDER BY tool, day"
        ).fetchall()


def _wipe():
    """Clear both observability tables between tests (they are test-local)."""
    with db._conn() as conn:
        conn.execute("DELETE FROM tool_calls")
        conn.execute("DELETE FROM tool_usage")


def test_record_and_readers():
    _wipe()
    a = AGENTS["alpha"]["agent_id"]
    db.record_tool_call("create_post", ok=True, agent_id=a, duration_ms=5.0)
    db.record_tool_call(
        "create_post", ok=False, agent_id=a, duration_ms=1.5, note="ForumError: boom"
    )
    db.record_tool_call("vote", ok=True, agent_id=None, duration_ms=0.5)

    summary = db.tool_usage_summary()
    by_tool = {t["tool"]: t for t in summary}
    assert by_tool["create_post"]["calls"] == 2
    assert by_tool["create_post"]["ok"] == 1
    assert by_tool["create_post"]["failed"] == 1
    assert by_tool["vote"]["calls"] == 1

    fails = db.tool_usage_recent_failures()
    assert len(fails) == 1
    assert fails[0]["tool"] == "create_post"
    assert fails[0]["agent_id"] == a
    assert fails[0]["note"] == "ForumError: boom"

    by_agent = {x["agent_id"]: x for x in db.tool_usage_by_agent()}
    assert by_agent[a]["calls"] == 2
    assert by_agent[None]["calls"] == 1


def test_note_truncation():
    _wipe()
    db.record_tool_call("x", ok=False, note="n" * 500)
    fails = db.tool_usage_recent_failures()
    assert len(fails[0]["note"]) <= config.TOOL_USAGE_NOTE_CAP


def test_sweep_folds_and_prunes():
    _wipe()
    a = AGENTS["alpha"]["agent_id"]
    # Two old (folded-then-pruned) rows, older than the 30-day window.
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO tool_calls (tool, ok, agent_id, duration_ms, note, created_at)"
            " VALUES ('old_tool', 1, ?, 3, NULL, ?)",
            (a, _iso(40)),
        )
        conn.execute(
            "INSERT INTO tool_calls (tool, ok, agent_id, duration_ms, note, created_at)"
            " VALUES ('old_tool', 0, NULL, 7, 'old fail', ?)",
            (_iso(41),),
        )
        # One recent row inside the window - must survive the sweep intact.
        conn.execute(
            "INSERT INTO tool_calls (tool, ok, agent_id, duration_ms, note, created_at)"
            " VALUES ('new_tool', 1, ?, 1, NULL, ?)",
            (a, _iso(1)),
        )
    pruned = db.tool_usage_sweep()
    assert pruned == 2  # only the two aged-out rows were pruned
    rows = _ledger_rows()
    assert len(rows) == 1
    assert rows[0]["tool"] == "new_tool"

    # The two old rows were folded into the aggregate (one per UTC day).
    usage = _usage_rows()
    old_rows = [u for u in usage if u["tool"] == "old_tool"]
    assert len(old_rows) == 2  # 40d ago and 41d ago are different UTC days
    assert sum(u["calls"] for u in old_rows) == 2
    assert sum(u["ok"] for u in old_rows) == 1
    assert sum(u["failed"] for u in old_rows) == 1
    assert sum(u["distinct_agents"] for u in old_rows) == 1

    # Idempotent: a second sweep finds nothing new to fold.
    assert db.tool_usage_sweep() == 0
    assert len(_ledger_rows()) == 1
    assert len(_usage_rows()) == 2


def test_sweep_disabled():
    _wipe()
    saved = config.TOOL_USAGE_RETENTION_DAYS
    try:
        config.TOOL_USAGE_RETENTION_DAYS = 0  # 0 disables pruning
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO tool_calls (tool, ok, agent_id, duration_ms, note, created_at)"
                " VALUES ('old_tool', 1, NULL, 1, NULL, ?)",
                (_iso(40),),
            )
        assert db.tool_usage_sweep() == 0
        assert len(_ledger_rows()) == 1  # nothing pruned
    finally:
        config.TOOL_USAGE_RETENTION_DAYS = saved


if __name__ == "__main__":
    for fn in [
        test_record_and_readers,
        test_note_truncation,
        test_sweep_folds_and_prunes,
        test_sweep_disabled,
    ]:
        fn()
    print("test_tool_usage all passed")
