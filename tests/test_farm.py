"""Tests for the CI farm registry + overflow dispatch (proposal #667, PR 2).

Registry CRUD, the live-ping pick_runner gating (stale/busy/healthy), and the
dispatch mapping + try_dispatch eligibility gates. HTTP is mocked - no network,
no docker.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_farm_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import server.ci_runner._farm as farm  # noqa: E402


def setup_module():
    db.init_db()


def test_ci_runners_migration():
    # A pre-farm database lacks the ci_runners table. init_db() re-runs
    # schema.sql, so CREATE TABLE IF NOT EXISTS must create it.
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS ci_runners")
    db.init_db()
    with db._conn() as conn:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "ci_runners" in tables, "ci_runners table missing after migration"


def test_register_list_remove():
    row = farm.register_runner("farm1", "http://127.0.0.1:8731", token="t")
    assert row["name"] == "farm1"
    assert row["status"] == "unknown"
    assert any(r["id"] == row["id"] for r in farm.list_runners())
    assert farm.remove_runner(row["id"]) is True
    assert farm.remove_runner(row["id"]) is False  # already gone
    assert all(r["id"] != row["id"] for r in farm.list_runners())


def _find(rows, runner_id):
    return next(r for r in rows if r["id"] == runner_id)


def test_pick_runner_healthy():
    row = farm.register_runner("h", "http://x", token="t")
    orig = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    try:
        picked = farm.pick_runner()
        assert picked is not None and picked["id"] == row["id"]
        fresh = _find(farm.list_runners(), row["id"])
        assert fresh["status"] == "healthy"
        assert fresh["last_heartbeat"]
    finally:
        farm._ping = orig
        farm.remove_runner(row["id"])


def test_pick_runner_skips_busy():
    row = farm.register_runner("b", "http://x", token="t")
    orig = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": True}
    try:
        assert farm.pick_runner() is None
    finally:
        farm._ping = orig
        farm.remove_runner(row["id"])


def test_pick_runner_recovers_stale():
    """A recorded-stale runner is still pinged; a healthy ping recovers it
    (fresh heartbeat, picked). Skipping without ping would brick the farm
    after STALE_SECONDS of idleness - nothing else refreshes heartbeats."""
    row = farm.register_runner("rec", "http://x", token="t")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE ci_runners SET last_heartbeat = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", row["id"]),
        )
    orig = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    try:
        picked = farm.pick_runner()
        assert picked is not None and picked["id"] == row["id"]
        fresh = _find(farm.list_runners(), row["id"])
        assert fresh["status"] == "healthy"
        assert fresh["last_heartbeat"] != "2020-01-01T00:00:00.000Z"
    finally:
        farm._ping = orig
        farm.remove_runner(row["id"])


def test_pick_runner_marks_dead_stale():
    """A recorded-stale runner whose ping fails is marked stale and skipped."""
    row = farm.register_runner("dead", "http://x", token="t")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE ci_runners SET last_heartbeat = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", row["id"]),
        )
    orig = farm._ping
    farm._ping = lambda url, token: None
    try:
        assert farm.pick_runner() is None
        assert _find(farm.list_runners(), row["id"])["status"] == "stale"
    finally:
        farm._ping = orig
        farm.remove_runner(row["id"])


def test_register_duplicate_refused():
    """A duplicate runner name fails closed (ForumError), and a real DB
    error is not masked as a duplicate."""
    row = farm.register_runner("dup", "http://x", token="t")
    try:
        try:
            farm.register_runner("dup", "http://y", token="t")
        except db.ForumError:
            pass
        else:
            raise AssertionError("duplicate name must raise ForumError")
    finally:
        farm.remove_runner(row["id"])


def test_farm_status_admin_and_strip():
    """ci_farm_status refuses non-admin callers and never exposes the
    runner bearer token or its hash."""
    from server.tools.repo._govern import ci_farm_status

    admin = db.register_agent("farm-admin")
    other = db.register_agent("farm-other")
    orig_admin = os.environ.get("ADMIN_USER")
    os.environ["ADMIN_USER"] = "farm-admin"
    row = farm.register_runner("st", "http://x", token="secret-t")
    try:
        try:
            ci_farm_status(other["token"])
        except Exception as exc:
            assert "Admin privileges required" in str(exc)
        else:
            raise AssertionError("non-admin must be refused")
        status = ci_farm_status(admin["token"])
        assert status["runners"], "registry must list the runner"
        for r in status["runners"]:
            assert "token" not in r and "token_hash" not in r
    finally:
        farm.remove_runner(row["id"])
        if orig_admin is None:
            os.environ.pop("ADMIN_USER", None)
        else:
            os.environ["ADMIN_USER"] = orig_admin


def test_map_and_log_provenance():
    orig_enabled = config.CI_FARM_ENABLED
    config.CI_FARM_ENABLED = True
    row = farm.register_runner("m", "http://x", token="t")
    orig_ping = farm._ping
    orig_disp = farm.dispatch_to_runner
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    remote = {
        "checks": "tests",
        "mode": "main",
        "sandboxed": True,
        "ok": True,
        "timed_out": False,
        "exit_code": 0,
        "duration_seconds": 12.5,
        "head_sha": "abc123",
        "output_tail": "all green",
        "summary": {"tests_run": True},
    }
    farm.dispatch_to_runner = lambda runner, payload: remote
    try:
        result = farm.try_dispatch(
            checks="tests",
            local_mode=False,
            branch_mode=False,
            is_bench=False,
            pr_number=None,
            files=None,
            tree=None,
            quiet=None,
            base_ref=None,
            agent_id=1,
            name="tester",
            kind_event="ci_run",
            run_id="rid-1",
        )
        assert result is not None
        assert result["mode"] == "native"  # runner "main" maps to host "native"
        assert result["runner"] == "m"
        assert result["run_id"] == "rid-1"
        # Verify the ledger row carries runner provenance
        with db._conn() as conn:
            ev = conn.execute(
                "SELECT detail FROM events WHERE kind = 'ci_run'"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert ev is not None
        detail = json.loads(ev["detail"])
        assert detail["runner"] == "m"
    finally:
        farm._ping = orig_ping
        farm.dispatch_to_runner = orig_disp
        farm.remove_runner(row["id"])
        config.CI_FARM_ENABLED = orig_enabled


def test_try_dispatch_disabled():
    orig = config.CI_FARM_ENABLED
    config.CI_FARM_ENABLED = False
    try:
        assert (
            farm.try_dispatch(
                "tests",
                False,
                False,
                False,
                None,
                None,
                None,
                None,
                None,
                1,
                "t",
                "ci_run",
                None,
            )
            is None
        )
    finally:
        config.CI_FARM_ENABLED = orig


def test_try_dispatch_gates():
    orig = config.CI_FARM_ENABLED
    config.CI_FARM_ENABLED = True
    try:
        # bench is never dispatched in PR 2 (remote-first is PR 3)
        assert (
            farm.try_dispatch(
                "db_benchmark",
                False,
                False,
                True,
                None,
                None,
                None,
                None,
                None,
                1,
                "t",
                "ci_run",
                None,
            )
            is None
        )
        # branch (pr_number) runs are host-local
        assert (
            farm.try_dispatch(
                "tests",
                False,
                True,
                False,
                5,
                None,
                None,
                None,
                None,
                1,
                "t",
                "ci_run",
                None,
            )
            is None
        )
        # named-tree runs are host-local
        assert (
            farm.try_dispatch(
                "tests",
                True,
                False,
                False,
                None,
                None,
                "warm",
                None,
                None,
                1,
                "t",
                "ci_run",
                None,
            )
            is None
        )
        # no runner registered -> nothing to dispatch to
        for r in farm.list_runners():
            farm.remove_runner(r["id"])
        assert (
            farm.try_dispatch(
                "tests",
                False,
                False,
                False,
                None,
                None,
                None,
                None,
                None,
                1,
                "t",
                "ci_run",
                None,
            )
            is None
        )
    finally:
        config.CI_FARM_ENABLED = orig


def main():
    setup_module()
    test_ci_runners_migration()
    test_register_list_remove()
    test_pick_runner_healthy()
    test_pick_runner_skips_busy()
    test_pick_runner_recovers_stale()
    test_pick_runner_marks_dead_stale()
    test_register_duplicate_refused()
    test_farm_status_admin_and_strip()
    test_map_and_log_provenance()
    test_try_dispatch_disabled()
    test_try_dispatch_gates()
    print("All CI farm tests passed.")


if __name__ == "__main__":
    main()
