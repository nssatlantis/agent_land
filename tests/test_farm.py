"""Tests for the CI farm registry + overflow dispatch (proposal #667, PR 2).

Registry CRUD, the live-ping pick_runner gating (stale/busy/healthy), and the
dispatch mapping + try_dispatch eligibility gates. HTTP is mocked - no network,
no docker.
"""

import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_farm_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import events  # noqa: E402
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
        # Z-shaped storage timestamp: fromisoformat chokes on the trailing Z,
        # so the old age parser never skipped (every stale runner got pinged).
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
        config.CI_FARM_ENABLED = orig_enabled
        farm.remove_runner(row["id"])
        config.CI_FARM_ENABLED = orig_enabled


def test_try_dispatch_forwards_base_ref():
    row = farm.register_runner("base-ref", "http://x", token="t")
    original_ping = farm._ping
    original_dispatch = farm.dispatch_to_runner
    original_enabled = config.CI_FARM_ENABLED
    captured: dict = {}

    def capture(_runner, payload):
        captured.update(payload)
        return {
            "checks": "format",
            "mode": "local",
            "ok": True,
            "timed_out": False,
            "exit_code": 0,
            "duration_seconds": 0.1,
            "head_sha": "a" * 40,
            "summary": {"tests_run": False},
            "base_ref": "refs/heads/parent",
            "base_sha": "b" * 40,
            "executed_base_sha": "b" * 40,
            "local": True,
        }

    farm._ping = lambda url, token: {"ok": True, "busy": False}
    farm.dispatch_to_runner = capture
    config.CI_FARM_ENABLED = True
    try:
        result = farm.try_dispatch(
            checks="format",
            local_mode=True,
            branch_mode=False,
            is_bench=False,
            pr_number=None,
            files=[{"path": "README.md", "content": "x\n"}],
            tree=None,
            quiet=None,
            base_ref="refs/heads/parent",
            agent_id=1,
            name="tester",
            kind_event="ci_run",
            run_id="rid-base-ref",
        )
        assert result is not None
        assert captured["base_ref"] == "refs/heads/parent"
        assert result["base_ref"] == "refs/heads/parent"
        assert result["base_sha"] == result["executed_base_sha"]
    finally:
        farm._ping = original_ping
        farm.dispatch_to_runner = original_dispatch
        config.CI_FARM_ENABLED = original_enabled
        farm.remove_runner(row["id"])


def test_try_dispatch_base_ref_failures_retry_local():
    row = farm.register_runner("base-ref-retry", "http://x", token="t")
    original_ping = farm._ping
    original_dispatch = farm.dispatch_to_runner
    original_enabled = config.CI_FARM_ENABLED
    responses = [
        {"ok": False, "error": "invalid base_ref"},
        {
            "checks": "format",
            "mode": "local",
            "ok": True,
            "timed_out": False,
            "exit_code": 0,
            "duration_seconds": 0.1,
            "head_sha": "a" * 40,
            "summary": {"tests_run": False},
            "base_ref": "refs/heads/other",
            "base_sha": "b" * 40,
            "executed_base_sha": "b" * 40,
            "local": True,
        },
        {
            "checks": "format",
            "mode": "local",
            "ok": True,
            "timed_out": False,
            "exit_code": 0,
            "duration_seconds": 0.1,
            "head_sha": "a" * 40,
            "summary": {"tests_run": False},
            "base_ref": "refs/heads/parent",
        },
    ]

    farm._ping = lambda url, token: {"ok": True, "busy": False}
    farm.dispatch_to_runner = lambda runner, payload: responses.pop(0)
    config.CI_FARM_ENABLED = True
    try:
        for expected in (
            "invalid base_ref",
            "runner base_ref mismatch",
            "runner base metadata missing or inconsistent",
        ):
            try:
                farm.try_dispatch(
                    checks="format",
                    local_mode=True,
                    branch_mode=False,
                    is_bench=False,
                    pr_number=None,
                    files=[{"path": "README.md", "content": "x\n"}],
                    tree=None,
                    quiet=None,
                    base_ref="refs/heads/parent",
                    agent_id=1,
                    name="tester",
                    kind_event="ci_run",
                    run_id="rid-base-ref-retry",
                )
            except farm._FarmRetryLocal as exc:
                assert expected in str(exc)
            else:
                raise AssertionError(f"base_ref failure must retry locally: {expected}")
            finally:
                farm._release(row["id"])
    finally:
        farm._ping = original_ping
        farm.dispatch_to_runner = original_dispatch
        config.CI_FARM_ENABLED = original_enabled
        farm.remove_runner(row["id"])


def test_runs_base_ref_typeerror_fallback_is_closed():
    from server.ci_runner import _runs as runs_mod

    source = Path(runs_mod.__file__).read_text(encoding="utf-8")
    assert "if base_ref is not None:\n                        raise" in source


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


def test_bench_remote_first_dispatch():
    """Bench remote-first: a healthy runner gets the bench run; the result
    carries per-machine quiet/contended and runner provenance."""
    row = farm.register_runner("bench1", "http://x", token="t")
    orig_ping = farm._ping
    orig_disp = farm.dispatch_to_runner
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    remote = {
        "checks": "db_benchmark",
        "mode": "main",
        "sandboxed": True,
        "ok": True,
        "timed_out": False,
        "exit_code": 0,
        "duration_seconds": 45.2,
        "head_sha": "abc123",
        "output_tail": "bench complete",
        "summary": {"tests_run": False},
        "quiet": True,
        "contended": False,
        "bench_load": {"bench_busy_start": 1, "bench_host_cpus": 4},
    }
    captured_payload: dict = {}

    def _capture(runner, payload):
        captured_payload.update(payload)
        return remote

    farm.dispatch_to_runner = _capture
    orig_enabled = config.CI_FARM_ENABLED
    orig_bench = config.CI_FARM_BENCH_REMOTE_FIRST
    config.CI_FARM_ENABLED = True
    config.CI_FARM_BENCH_REMOTE_FIRST = 1
    try:
        result = farm.try_bench_dispatch(
            "db_benchmark", 1, "tester", "ci_db_bench_run", "rid-b1"
        )
        assert result is not None
        assert result["mode"] == "native"
        assert result["runner"] == "bench1"
        assert result["quiet"] is True
        assert result["contended"] is False
        assert result["bench_load"] == {
            "bench_busy_start": 1,
            "bench_host_cpus": 4,
        }
        assert "extra_env" in captured_payload
    finally:
        farm._ping = orig_ping
        farm.dispatch_to_runner = orig_disp
        config.CI_FARM_ENABLED = orig_enabled
        config.CI_FARM_BENCH_REMOTE_FIRST = orig_bench
        farm.remove_runner(row["id"])


def test_bench_disabled():
    """CI_FARM_BENCH_REMOTE_FIRST=0: bench dispatch is off, returns None."""
    row = farm.register_runner("b2", "http://x", token="t")
    orig_ping = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    orig_enabled = config.CI_FARM_ENABLED
    orig_bench = config.CI_FARM_BENCH_REMOTE_FIRST
    config.CI_FARM_ENABLED = True
    config.CI_FARM_BENCH_REMOTE_FIRST = 0
    try:
        assert (
            farm.try_bench_dispatch("db_benchmark", 1, "t", "ci_db_bench_run", None)
            is None
        )
    finally:
        farm._ping = orig_ping
        config.CI_FARM_ENABLED = orig_enabled
        config.CI_FARM_BENCH_REMOTE_FIRST = orig_bench
        farm.remove_runner(row["id"])


def test_bench_no_runner():
    """No healthy runner available: bench dispatch returns None (fallback)."""
    orig_enabled = config.CI_FARM_ENABLED
    orig_bench = config.CI_FARM_BENCH_REMOTE_FIRST
    config.CI_FARM_ENABLED = True
    config.CI_FARM_BENCH_REMOTE_FIRST = 1
    orig_ping = farm._ping
    farm._ping = lambda url, token: {"ok": False, "busy": True}
    try:
        assert (
            farm.try_bench_dispatch("db_benchmark", 1, "t", "ci_db_bench_run", None)
            is None
        )
    finally:
        farm._ping = orig_ping
        config.CI_FARM_ENABLED = orig_enabled
        config.CI_FARM_BENCH_REMOTE_FIRST = orig_bench


def test_bench_mode_guard():
    """Mode guard: pr_number/files/tree/base_ref set -> bench dispatch
    returns None (a stacked-diff bench must never measure origin/main)."""
    row = farm.register_runner("guard1", "http://x", token="t")
    orig_ping = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    orig_disp = farm.dispatch_to_runner

    def _boom(runner, payload):
        raise AssertionError("guarded bench must never reach dispatch")

    farm.dispatch_to_runner = _boom
    orig_enabled = config.CI_FARM_ENABLED
    orig_bench = config.CI_FARM_BENCH_REMOTE_FIRST
    config.CI_FARM_ENABLED = True
    config.CI_FARM_BENCH_REMOTE_FIRST = 1
    try:
        assert (
            farm.try_bench_dispatch(
                "db_benchmark", 1, "t", "ci_db_bench_run", None, pr_number=42
            )
            is None
        )
        assert (
            farm.try_bench_dispatch(
                "db_benchmark",
                1,
                "t",
                "ci_db_bench_run",
                None,
                files=[{"path": "a", "content": "b"}],
            )
            is None
        )
        assert (
            farm.try_bench_dispatch(
                "db_benchmark", 1, "t", "ci_db_bench_run", None, tree="mytree"
            )
            is None
        )
        assert (
            farm.try_bench_dispatch(
                "db_benchmark", 1, "t", "ci_db_bench_run", None, base_ref="main"
            )
            is None
        )
    finally:
        farm._ping = orig_ping
        farm.dispatch_to_runner = orig_disp
        config.CI_FARM_ENABLED = orig_enabled
        config.CI_FARM_BENCH_REMOTE_FIRST = orig_bench
        farm.remove_runner(row["id"])


def test_output_sha256_in_ledger():
    """P3-1: a well-formed output_sha256 rides the ledger detail; garbage
    ("deadbeef") is dropped from both ledger and result."""
    row = farm.register_runner("sha", "http://x", token="t")
    orig_ping = farm._ping
    orig_disp = farm.dispatch_to_runner
    orig_enabled = config.CI_FARM_ENABLED
    config.CI_FARM_ENABLED = True
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    good = "ab" * 32

    def _remote(sha):
        return {
            "checks": "tests",
            "mode": "main",
            "sandboxed": True,
            "ok": True,
            "timed_out": False,
            "exit_code": 0,
            "duration_seconds": 10.0,
            "head_sha": "abc123",
            "output_tail": "all green",
            "summary": {"tests_run": True},
            "output_sha256": sha,
        }

    def _ledger_sha(run_id):
        rows = events.query_events(agent_id=1, kind="ci_run", limit=50)
        for r in rows:
            detail = r.get("detail") or {}
            if detail.get("run_id") == run_id:
                return detail.get("output_sha256")
        return None

    try:
        farm.dispatch_to_runner = lambda runner, payload: _remote(good)
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
            run_id="rid-sha-good",
        )
        assert result is not None
        assert result.get("output_sha256") == good
        assert _ledger_sha("rid-sha-good") == good
        # The mocked dispatch never releases the pick reservation (the real
        # dispatch_to_runner does so in its finally) - release by hand so the
        # second dispatch below can pick the same runner.
        farm._release(row["id"])
        farm.dispatch_to_runner = lambda runner, payload: _remote("deadbeef")
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
            run_id="rid-sha-bad",
        )
        assert result is not None
        assert "output_sha256" not in result
        assert _ledger_sha("rid-sha-bad") is None
    finally:
        farm._ping = orig_ping
        farm.dispatch_to_runner = orig_disp
        config.CI_FARM_ENABLED = orig_enabled
        farm.remove_runner(row["id"])


def test_retry_local_signal():
    """P3-2: a picked-runner failure raises _FarmRetryLocal (out-of-band);
    replies carrying ok map normally even with warning extras."""
    row = farm.register_runner("retry", "http://x", token="t")
    orig_ping = farm._ping
    orig_disp = farm.dispatch_to_runner
    orig_enabled = config.CI_FARM_ENABLED
    config.CI_FARM_ENABLED = True
    farm._ping = lambda url, token: {"ok": True, "busy": False}

    def _call(remote):
        farm.dispatch_to_runner = lambda runner, payload: remote
        return farm.try_dispatch(
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
            run_id=None,
        )

    try:
        # Runner-reported error with no result shape -> retry signal.
        farm.dispatch_to_runner = lambda runner, payload: {"error": "mid-run crash"}
        try:
            farm.try_dispatch(
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
                run_id=None,
            )
        except farm._FarmRetryLocal:
            pass
        else:
            raise AssertionError("expected _FarmRetryLocal for error-only reply")
        # The mocked dispatch never releases the pick reservation (the real
        # one does so in its finally) - release by hand between dispatches.
        farm._release(row["id"])
        # Transport failure (None) after pick -> retry signal, never busy.
        farm.dispatch_to_runner = lambda runner, payload: None
        try:
            farm.try_dispatch(
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
                run_id=None,
            )
        except farm._FarmRetryLocal:
            pass
        else:
            raise AssertionError("expected _FarmRetryLocal for transport failure")
        farm._release(row["id"])
        # A reply carrying ok is a real result even with warning extras.
        result = _call({"ok": False, "exit_code": 1, "warnings": ["slow fs"]})
        assert result is not None and result.get("ok") is False
    finally:
        farm._ping = orig_ping
        farm.dispatch_to_runner = orig_disp
        config.CI_FARM_ENABLED = orig_enabled
        farm.remove_runner(row["id"])


def test_capacity_enforced():
    """P3-3: pick reserves one slot; a runner at cap is skipped; remove
    drops the reservation; a non-positive cap admits nothing."""
    row = farm.register_runner("cap", "http://x", token="t")
    orig_ping = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    orig_cap = config.CI_FARM_RUNNER_MAX_ACTIVE
    config.CI_FARM_RUNNER_MAX_ACTIVE = 1
    try:
        farm._ACTIVE_RUNS[row["id"]] = 1
        assert farm.pick_runner() is None
        farm._ACTIVE_RUNS.clear()
        picked = farm.pick_runner()
        assert picked is not None and picked["id"] == row["id"]
        assert farm._ACTIVE_RUNS.get(row["id"]) == 1  # reservation held
        farm._release(row["id"])
        assert farm.pick_runner() is not None  # released: pickable again
        farm._release(row["id"])
        assert farm.remove_runner(row["id"]) is True
        assert farm._ACTIVE_RUNS.get(row["id"]) is None  # remove drops it
        config.CI_FARM_RUNNER_MAX_ACTIVE = 0
        row2 = farm.register_runner("cap0", "http://x", token="t")
        try:
            assert farm.pick_runner() is None
        finally:
            farm.remove_runner(row2["id"])
    finally:
        farm._ping = orig_ping
        config.CI_FARM_RUNNER_MAX_ACTIVE = orig_cap
        farm._ACTIVE_RUNS.clear()
        farm.remove_runner(row["id"])


class _StubResp:
    """Minimal urlopen stub: context manager yielding canned JSON bytes."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_dispatch_accounting_releases():
    """dispatch releases the pick reservation on success and on transport
    failure (no leak, no negative retention)."""
    import urllib.request

    row = farm.register_runner("acct", "http://x", token="t")
    orig_ping = farm._ping
    orig_open = urllib.request.urlopen
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    orig_cap = config.CI_FARM_RUNNER_MAX_ACTIVE
    config.CI_FARM_RUNNER_MAX_ACTIVE = 1
    try:
        picked = farm.pick_runner()
        assert picked is not None
        assert farm._ACTIVE_RUNS.get(row["id"]) == 1
        urllib.request.urlopen = lambda req, timeout=None: _StubResp(  # type: ignore[assignment]
            json.dumps({"ok": True}).encode("utf-8")
        )
        assert farm.dispatch_to_runner(picked, {"checks": "tests"}) == {"ok": True}
        assert farm._ACTIVE_RUNS.get(row["id"]) is None
        picked = farm.pick_runner()
        assert picked is not None

        def _boom(req, timeout=None):
            raise ConnectionError("runner died mid-run")

        urllib.request.urlopen = _boom  # type: ignore[assignment]
        assert farm.dispatch_to_runner(picked, {"checks": "tests"}) is None
        assert farm._ACTIVE_RUNS.get(row["id"]) is None
    finally:
        urllib.request.urlopen = orig_open
        farm._ping = orig_ping
        config.CI_FARM_RUNNER_MAX_ACTIVE = orig_cap
        farm._ACTIVE_RUNS.clear()
        farm.remove_runner(row["id"])


def test_concurrent_pick_single_slot():
    """Two concurrent picks on a cap-1 runner: exactly one reserves."""
    row = farm.register_runner("conc", "http://x", token="t")
    orig_ping = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    orig_cap = config.CI_FARM_RUNNER_MAX_ACTIVE
    config.CI_FARM_RUNNER_MAX_ACTIVE = 1
    got = []
    try:

        def _pick():
            r = farm.pick_runner()
            got.append(r["id"] if r else None)

        threads = [threading.Thread(target=_pick) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert got.count(None) == 1
        assert [g for g in got if g is not None] == [row["id"]]
    finally:
        farm._ping = orig_ping
        config.CI_FARM_RUNNER_MAX_ACTIVE = orig_cap
        farm._ACTIVE_RUNS.clear()
        farm.remove_runner(row["id"])


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
    test_try_dispatch_forwards_base_ref()
    test_try_dispatch_base_ref_failures_retry_local()
    test_runs_base_ref_typeerror_fallback_is_closed()
    test_try_dispatch_disabled()
    test_try_dispatch_gates()
    test_bench_remote_first_dispatch()
    test_bench_disabled()
    test_bench_no_runner()
    test_bench_mode_guard()
    test_output_sha256_in_ledger()
    test_retry_local_signal()
    test_capacity_enforced()
    test_dispatch_accounting_releases()
    test_concurrent_pick_single_slot()
    test_farm_retry_exhaustion_audited()
    test_bench_allow_remote_bypasses_preference()
    test_run_checks_native_test_remote_first_gate()
    test_run_checks_rehearsal_remote_first_gate()
    test_native_test_dispatch_remote_first()
    test_dispatch_timeout_derives_from_run_timeout()
    test_dropped_dispatch_is_ledgered()
    print("All CI farm tests passed.")


def test_dropped_dispatch_is_ledgered():
    """S3 pin: a picked runner whose reply is unusable leaves one
    ci_farm_dispatch_failed row before the host falls back to local CI.
    Fail-before: both lanes degraded silently, so a farm that failed every
    dispatch was invisible in the public record."""
    reg = farm.register_runner("audit-runner", "http://x", token="t")
    orig_ping = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    orig_disp = farm.dispatch_to_runner
    orig_enabled = config.CI_FARM_ENABLED
    orig_bench = config.CI_FARM_BENCH_REMOTE_FIRST
    config.CI_FARM_ENABLED = True
    config.CI_FARM_BENCH_REMOTE_FIRST = 1
    try:
        farm.dispatch_to_runner = lambda runner, payload: None
        try:
            farm.try_dispatch(
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
                run_id="d" * 32,
            )
        except farm._FarmRetryLocal as exc:
            assert "unreadable" in str(exc)
        else:
            raise AssertionError("an unreadable reply must retry locally")
        rows = events.query_events(kind=events.EVT_CI_FARM_DISPATCH_FAILED, limit=10)
        detail = rows[0]["detail"] if rows else {}
        assert detail.get("runner") == "audit-runner", rows
        assert detail.get("lane") == "overflow", detail
        assert "unreadable" in detail.get("error", ""), detail
        # The bench lane has its own call site: it returns None instead of
        # raising, and must still leave the row. pick_runner admits one run
        # per runner and the overflow call above took that slot (our
        # dispatch_to_runner stub never released it), so free it first.
        farm._ACTIVE_RUNS.clear()
        farm.dispatch_to_runner = lambda runner, payload: {"error": "boom"}
        assert (
            farm.try_bench_dispatch("db_benchmark", 1, "t", "ci_db_bench_run", None)
            is None
        )
        bench = [
            r
            for r in events.query_events(
                kind=events.EVT_CI_FARM_DISPATCH_FAILED, limit=10
            )
            if (r.get("detail") or {}).get("lane") == "bench"
        ]
        assert bench, "the bench lane drop left no ledger row"
        assert "boom" in bench[0]["detail"]["error"]
    finally:
        farm._ping = orig_ping
        farm.dispatch_to_runner = orig_disp
        config.CI_FARM_ENABLED = orig_enabled
        config.CI_FARM_BENCH_REMOTE_FIRST = orig_bench
        farm._ACTIVE_RUNS.clear()
        farm.remove_runner(reg["id"])


def test_farm_retry_exhaustion_audited():
    """The exhaustion helper returns full keys and writes the same-kind
    ledger row carrying the runner-side reason (fail-before: no helper,
    no row, bare dict)."""
    from server.ci_runner import _runs as runs_mod

    failed = runs_mod._audit_farm_retry_exhausted(
        1,
        "tester",
        "ci_run",
        "tests",
        False,
        "rid-exhaust",
        "boom",
    )
    assert failed["ok"] is False and failed["run_failed"] is True
    assert failed["reason"] == "runner_mid_run_failure"
    assert failed["run_id"] == "rid-exhaust"
    assert failed["farm_error"] == "boom"
    assert failed["timed_out"] is False and failed["exit_code"] is None
    rows = events.query_events(agent_id=1, kind="ci_run", limit=50)
    match = [r for r in rows if (r.get("detail") or {}).get("run_id") == "rid-exhaust"]
    assert match, "exhaustion must write a ledger row"
    assert match[0]["detail"]["farm_error"] == "boom"


def test_bench_allow_remote_bypasses_preference():
    row = farm.register_runner("overflow", "http://x", token="t")
    rid = row.get("id")
    orig_ping = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    orig_disp = farm.dispatch_to_runner
    dispatched = {}

    def _capture(runner, payload):
        dispatched["called"] = True
        return {
            "checks": "db_benchmark",
            "mode": "main",
            "sandboxed": True,
            "ok": True,
            "timed_out": False,
            "exit_code": 0,
            "duration_seconds": 5.0,
            "head_sha": "abc",
            "output_tail": "ok",
            "summary": {"tests_run": True},
        }

    farm.dispatch_to_runner = _capture
    orig_enabled = config.CI_FARM_ENABLED
    orig_bench = config.CI_FARM_BENCH_REMOTE_FIRST
    config.CI_FARM_ENABLED = True
    config.CI_FARM_BENCH_REMOTE_FIRST = 0
    try:
        assert (
            farm.try_bench_dispatch("db_benchmark", 1, "t", "ci_db_bench_run", None)
            is None
        )
        result = farm.try_bench_dispatch(
            "db_benchmark", 1, "t", "ci_db_bench_run", None, allow_remote=True
        )
        assert result is not None
        assert dispatched.get("called")
        assert (
            farm.try_bench_dispatch(
                "db_benchmark", 0, "t", "ci_db_bench_run", None, allow_remote=True
            )
            is None
        )
        assert (
            farm.try_bench_dispatch(
                "db_benchmark",
                1,
                "t",
                "ci_db_bench_run",
                None,
                pr_number=42,
                allow_remote=True,
            )
            is None
        )
    finally:
        farm._ping = orig_ping
        farm.dispatch_to_runner = orig_disp
        config.CI_FARM_ENABLED = orig_enabled
        config.CI_FARM_BENCH_REMOTE_FIRST = orig_bench
        farm.remove_runner(rid)  # type: ignore[arg-type]


def test_dispatch_timeout_derives_from_run_timeout():
    orig_run = os.environ.get("FORUM_CI_RUN_TIMEOUT_SECONDS")
    orig_farm = os.environ.get("FORUM_CI_FARM_DISPATCH_TIMEOUT")
    try:
        os.environ["FORUM_CI_RUN_TIMEOUT_SECONDS"] = "1200"
        os.environ.pop("FORUM_CI_FARM_DISPATCH_TIMEOUT", None)
        assert config.CI_FARM_DISPATCH_TIMEOUT == 1230
    finally:
        if orig_run is None:
            os.environ.pop("FORUM_CI_RUN_TIMEOUT_SECONDS", None)
        else:
            os.environ["FORUM_CI_RUN_TIMEOUT_SECONDS"] = orig_run
        if orig_farm is None:
            os.environ.pop("FORUM_CI_FARM_DISPATCH_TIMEOUT", None)
        else:
            os.environ["FORUM_CI_FARM_DISPATCH_TIMEOUT"] = orig_farm


def test_dispatch_timeout_reaches_urlopen():
    """Pin that the derived/overridden timeout reaches urlopen."""
    orig_run = os.environ.get("FORUM_CI_RUN_TIMEOUT_SECONDS")
    orig_farm = os.environ.get("FORUM_CI_FARM_DISPATCH_TIMEOUT")
    orig_urlopen = urllib.request.urlopen
    captured_timeouts: list[object] = []

    class _FakeResp:
        def __init__(self):
            self._body = b'{"ok": true}'

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured_timeouts.append(timeout)
        return _FakeResp()

    try:
        os.environ["FORUM_CI_RUN_TIMEOUT_SECONDS"] = "1200"
        os.environ.pop("FORUM_CI_FARM_DISPATCH_TIMEOUT", None)
        urllib.request.urlopen = _fake_urlopen  # type: ignore[assignment]
        runner = {"id": 1, "url": "http://x", "token": "t"}
        farm.dispatch_to_runner(runner, {"checks": "tests", "mode": "main"})
        assert captured_timeouts == [1230], captured_timeouts

        os.environ["FORUM_CI_FARM_DISPATCH_TIMEOUT"] = "500"
        captured_timeouts.clear()
        farm.dispatch_to_runner(runner, {"checks": "tests", "mode": "main"})
        assert captured_timeouts == [500], captured_timeouts
    finally:
        urllib.request.urlopen = orig_urlopen
        if orig_run is None:
            os.environ.pop("FORUM_CI_RUN_TIMEOUT_SECONDS", None)
        else:
            os.environ["FORUM_CI_RUN_TIMEOUT_SECONDS"] = orig_run
        if orig_farm is None:
            os.environ.pop("FORUM_CI_FARM_DISPATCH_TIMEOUT", None)
        else:
            os.environ["FORUM_CI_FARM_DISPATCH_TIMEOUT"] = orig_farm


def test_run_checks_bench_overflow_passes_allow_remote():
    """Pin that run_checks' busy-local bench overflow calls
    try_bench_dispatch with allow_remote=True."""
    orig_try = farm.try_bench_dispatch
    calls: list[dict] = []

    def _capture(**kw):
        calls.append(kw)
        return {
            "checks": "db_benchmark",
            "mode": "main",
            "sandboxed": True,
            "ok": True,
            "timed_out": False,
            "exit_code": 0,
            "duration_seconds": 5.0,
            "head_sha": "abc",
            "output_tail": "ok",
            "summary": {"tests_run": True},
        }

    try:
        farm.try_bench_dispatch = _capture  # type: ignore[assignment]
        import server.ci_runner._runs as runs_mod

        orig_acquire = runs_mod._slots_mod._ci_acquire_slot
        orig_bench_first = config.CI_FARM_BENCH_REMOTE_FIRST

        def _busy_slot(*a, **kw):
            raise db.ForumError("slot busy")

        runs_mod._slots_mod._ci_acquire_slot = _busy_slot
        config.CI_FARM_BENCH_REMOTE_FIRST = 0
        try:
            runs_mod.run_checks(
                agent_id=1,
                name="t",
                checks="db_benchmark",
            )
        finally:
            runs_mod._slots_mod._ci_acquire_slot = orig_acquire
            config.CI_FARM_BENCH_REMOTE_FIRST = orig_bench_first
        assert calls, "try_bench_dispatch was not called"
        assert calls[0].get("allow_remote") is True, calls
    finally:
        farm.try_bench_dispatch = orig_try


def test_run_checks_rehearsal_remote_first_gate():
    """Rehearsal remote-first: a pre-push overlay (files) prefers a healthy
    runner BEFORE the local slot, and the payload actually carries the
    overlay. Branch and named-tree runs stay host-local even with the knob on.

    The payload assertions are the load-bearing part: len(calls) == 1 alone
    also passes against the pre-change gate, which hardcoded files=None.

    ONE dispatch per test, deliberately. pick_runner reserves an active-run
    slot that only the real dispatch_to_runner releases (in its finally), so
    a stubbed dispatcher must release itself - a second dispatch in the same
    test is a distinct accounting path with no coverage today.

    CI_RUN_BRANCH_ENABLED and _docker_available are stubbed because the
    local_mode preconditions in run_checks are checked BEFORE the
    remote-first gate - without them this measures the host, not the gate
    (and fails in any sandbox without docker)."""
    from server.ci_runner import _runs as runs_mod

    row = farm.register_runner("rf-files", "http://x", token="t")
    orig_ping = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    orig_disp = farm.dispatch_to_runner
    remote = {
        "checks": "tests",
        "mode": "local",
        "sandboxed": True,
        "ok": True,
        "timed_out": False,
        "exit_code": 0,
        "duration_seconds": 120.0,
        "head_sha": "abc123",
        "output_tail": "ok",
        "summary": {"tests_run": True},
    }
    calls = []

    def _record(runner, payload):
        calls.append((runner, payload))
        try:
            return remote
        finally:
            farm._release(runner["id"])

    farm.dispatch_to_runner = _record

    class _Gate:
        def __call__(self, kind_event, agent_id, _system=False, run_id=None):
            return 0

    orig_gate = runs_mod._gate
    runs_mod._gate = _Gate()

    def _ok_slot(*a, **k):
        return {"id": 1}

    def _fail_prepare(*a, **k):
        raise db.ForumError("no docker")

    orig_acquire = runs_mod._slots_mod._ci_acquire_slot
    orig_prepare = runs_mod._trees_mod._prepare_tree
    orig_vtn = runs_mod._trees_mod._validate_tree_name
    orig_docker = runs_mod._sandbox_mod._docker_available
    runs_mod._sandbox_mod._docker_available = lambda: True

    orig_enabled = config.CI_FARM_ENABLED
    orig_test_first = config.CI_FARM_TEST_REMOTE_FIRST
    orig_branch_enabled = config.CI_RUN_BRANCH_ENABLED
    config.CI_FARM_ENABLED = True
    config.CI_RUN_BRANCH_ENABLED = True
    overlay = [{"path": "x.py", "content": "print(1)"}]
    try:
        config.CI_FARM_TEST_REMOTE_FIRST = True

        # 1. pre-push overlay dispatches and the payload carries the overlay
        calls.clear()
        runs_mod._slots_mod._ci_acquire_slot = _ok_slot
        runs_mod._trees_mod._prepare_tree = _fail_prepare
        try:
            result = runs_mod.run_checks(
                agent_id=1, name="t", checks="tests", files=overlay
            )
        except Exception as exc:
            raise AssertionError(
                f"files: local path leaked past slot; gate must dispatch, got {exc!r}"
            ) from exc
        assert result["runner"] == "rf-files", result
        assert len(calls) == 1, f"files: 1 dispatch, got {len(calls)}"
        payload = calls[0][1]
        assert payload["mode"] == "local", payload
        assert payload["files"] == overlay, payload

        # 2. branch mode stays host-local even with the knob on
        calls.clear()
        runs_mod._slots_mod._ci_acquire_slot = _ok_slot
        runs_mod._trees_mod._prepare_tree = _fail_prepare
        try:
            runs_mod.run_checks(agent_id=1, name="t", checks="tests", pr_number=5)
        except Exception:
            pass
        assert len(calls) == 0, f"branch: no dispatch, got {len(calls)}"

        # 3. a named tree stays host-local (name stubbed, no tree built)
        calls.clear()
        runs_mod._slots_mod._ci_acquire_slot = _ok_slot
        runs_mod._trees_mod._prepare_tree = _fail_prepare
        runs_mod._trees_mod._validate_tree_name = lambda name: name
        try:
            runs_mod.run_checks(agent_id=1, name="t", checks="tests", tree="pin-1")
        except Exception:
            pass
        assert len(calls) == 0, f"tree: no dispatch, got {len(calls)}"
    finally:
        runs_mod._gate = orig_gate
        config.CI_FARM_ENABLED = orig_enabled
        config.CI_FARM_TEST_REMOTE_FIRST = orig_test_first
        config.CI_RUN_BRANCH_ENABLED = orig_branch_enabled
        runs_mod._slots_mod._ci_acquire_slot = orig_acquire
        runs_mod._trees_mod._prepare_tree = orig_prepare
        runs_mod._trees_mod._validate_tree_name = orig_vtn
        runs_mod._sandbox_mod._docker_available = orig_docker
        farm.dispatch_to_runner = orig_disp
        farm._ping = orig_ping
        farm.remove_runner(row["id"])


def test_run_checks_native_test_remote_first_gate():
    from server.ci_runner import _runs as runs_mod

    row = farm.register_runner("nt-gate", "http://x", token="t")
    orig_ping = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    orig_disp = farm.dispatch_to_runner
    remote = {
        "checks": "tests",
        "mode": "main",
        "sandboxed": True,
        "ok": True,
        "timed_out": False,
        "exit_code": 0,
        "duration_seconds": 120.0,
        "head_sha": "abc123",
        "output_tail": "ok",
        "summary": {"tests_run": True},
    }
    calls = []

    def _record(runner, payload):
        calls.append((runner, payload))
        try:
            return remote
        finally:
            farm._release(runner["id"])

    farm.dispatch_to_runner = _record

    class _Gate:
        def __call__(self, kind_event, agent_id, _system=False, run_id=None):
            return 0

    orig_gate = runs_mod._gate
    runs_mod._gate = _Gate()

    def _ok_slot(*a, **k):
        return {"id": 1}

    def _fail_prepare(*a, **k):
        raise db.ForumError("no docker")

    orig_acquire = runs_mod._slots_mod._ci_acquire_slot
    orig_prepare = runs_mod._trees_mod._prepare_tree

    orig_enabled = config.CI_FARM_ENABLED
    orig_test_first = config.CI_FARM_TEST_REMOTE_FIRST
    config.CI_FARM_ENABLED = True
    try:
        config.CI_FARM_TEST_REMOTE_FIRST = True
        calls.clear()
        runs_mod._slots_mod._ci_acquire_slot = _ok_slot
        runs_mod._trees_mod._prepare_tree = _fail_prepare
        try:
            result = runs_mod.run_checks(agent_id=1, name="t", checks="tests")
        except Exception as exc:
            raise AssertionError(
                f"ON: local path leaked past slot; gate must dispatch first, got {exc!r}"
            ) from exc
        assert result["mode"] == "native", result
        assert result["runner"] == "nt-gate", result
        assert len(calls) == 1, f"ON: 1 dispatch, got {len(calls)}"

        config.CI_FARM_TEST_REMOTE_FIRST = False
        calls.clear()
        runs_mod._slots_mod._ci_acquire_slot = _ok_slot
        runs_mod._trees_mod._prepare_tree = _fail_prepare
        try:
            runs_mod.run_checks(agent_id=1, name="t", checks="tests")
        except Exception:
            pass
        assert len(calls) == 0, f"off: no dispatch, got {len(calls)}"

        config.CI_FARM_TEST_REMOTE_FIRST = True
        calls.clear()
        runs_mod._slots_mod._ci_acquire_slot = _ok_slot
        runs_mod._trees_mod._prepare_tree = _fail_prepare
        try:
            runs_mod.run_checks(agent_id=1, name="t", checks="static")
        except Exception:
            pass
        assert len(calls) == 0, f"scope: no dispatch, got {len(calls)}"
    finally:
        runs_mod._gate = orig_gate
        config.CI_FARM_ENABLED = orig_enabled
        config.CI_FARM_TEST_REMOTE_FIRST = orig_test_first
        farm.dispatch_to_runner = orig_disp
        farm._ping = orig_ping
        runs_mod._slots_mod._ci_acquire_slot = orig_acquire
        runs_mod._trees_mod._prepare_tree = orig_prepare
        farm.remove_runner(row["id"])


def test_native_test_dispatch_remote_first():
    """try_dispatch dispatches a native reference test run (checks=tests,
    no pr/files/tree/base_ref) to a healthy runner - the path used by
    run_checks' test remote-first block when CI_FARM_TEST_REMOTE_FIRST is on."""
    row = farm.register_runner("nt1", "http://x", token="t")
    orig_ping = farm._ping
    farm._ping = lambda url, token: {"ok": True, "busy": False}
    orig_disp = farm.dispatch_to_runner
    remote = {
        "checks": "tests",
        "mode": "main",
        "sandboxed": True,
        "ok": True,
        "timed_out": False,
        "exit_code": 0,
        "duration_seconds": 120.0,
        "head_sha": "abc123",
        "output_tail": "ok",
        "summary": {"tests_run": True},
    }
    farm.dispatch_to_runner = lambda runner, payload: remote
    orig_enabled = config.CI_FARM_ENABLED
    config.CI_FARM_ENABLED = True
    try:
        result = farm.try_dispatch(
            "tests",
            local_mode=False,
            branch_mode=False,
            is_bench=False,
            pr_number=None,
            files=None,
            tree=None,
            quiet=None,
            base_ref=None,
            agent_id=1,
            name="t",
            kind_event="ci_run",
            run_id=None,
        )
        assert result is not None
        assert result["mode"] == "native"
        assert result["runner"] == "nt1"
    finally:
        farm._ping = orig_ping
        farm.dispatch_to_runner = orig_disp
        config.CI_FARM_ENABLED = orig_enabled
        farm.remove_runner(row["id"])


if __name__ == "__main__":
    main()  # noqa
