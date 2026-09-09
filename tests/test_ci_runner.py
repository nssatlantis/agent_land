"""Tests for the server-side CI runner (repo_ci_run): guardrail gating,
main-only tree refresh seams, sanitized child environments, timeout kill,
output tailing, and the events-ledger audit trail."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_runner_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import events  # noqa: E402
import server.ci_runner as ci_runner  # noqa: E402
from tests._setup import config  # noqa: E402

db.init_db()

_ACTOR = 987654
_uid_counter = iter(range(9000, 9999))


def _uid() -> int:
    """Fresh actor id per executing scenario - shared ids would trip the
    real cooldown between tests."""
    return next(_uid_counter)


_SAVED: dict[str, object] = {}


def _shadow(name: str, value):
    _SAVED[name] = getattr(config, name)
    setattr(config, name, value)


def _restore():
    for name, value in _SAVED.items():
        setattr(config, name, value)
    _SAVED.clear()


class _StubTree:
    """Patches _prepare_tree to a throwaway dir holding a stub suite script.

    Also forces CI_RUN_NATIVE_SANDBOX off so these native-host tests stay
    deterministic host-interpreter runs regardless of whether the host (CI
    test job, dev box) has docker - they exercise the run plumbing, not the
    sandbox decision (covered separately in test_native_sandbox_*)."""

    def __init__(self, kind: str, body: str):
        self.dir = Path(tempfile.mkdtemp(prefix="agentland_ci_stub_"))
        rel = ci_runner._CHECKS[kind][1]
        script = self.dir / rel
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        self._orig = ci_runner._trees._prepare_tree
        self._saved_native = config.CI_RUN_NATIVE_SANDBOX
        config.CI_RUN_NATIVE_SANDBOX = 0
        ci_runner._trees._prepare_tree = lambda: (str(self.dir), "deadbeefcafe")

    def cleanup(self):
        ci_runner._trees._prepare_tree = self._orig
        config.CI_RUN_NATIVE_SANDBOX = self._saved_native


def test_knob_defaults():
    assert config.CI_RUN_ENABLED == 1
    assert config.CI_RUN_TIMEOUT_SECONDS == 600
    assert config.CI_RUN_RESPOND_SECONDS == 50
    assert config.CI_RUN_MAX_INFLIGHT == 1
    assert config.CI_RUN_COOLDOWN_SECONDS == 60
    assert config.CI_RUN_DAILY_CAP == 10
    assert config.CI_RUN_TAIL_BYTES == 16 * 1024
    assert config.CI_RUN_EVENT_TAIL_BYTES == 1536


def test_unknown_checks_rejected():
    try:
        ci_runner.run_checks(_ACTOR, "t", "deploy")
        raise AssertionError("expected ForumError")
    except db.ForumError as exc:
        assert "tests" in str(exc) and "benchmarks" in str(exc)


def test_disabled_flag_refuses():
    _shadow("CI_RUN_ENABLED", 0)
    try:
        ci_runner.run_checks(_ACTOR, "t", "tests")
        raise AssertionError("expected ForumError")
    except db.ForumError as exc:
        assert "disabled" in str(exc)
    finally:
        _restore()


def test_busy_lock_refuses():
    assert ci_runner._RUN_LOCK.acquire(blocking=False)
    try:
        ci_runner.run_checks(_ACTOR, "t", "tests")
        raise AssertionError("expected ForumError")
    except db.ForumError as exc:
        assert "already in progress" in str(exc)
    finally:
        ci_runner._RUN_LOCK.release()


def test_success_run_parses_summary_and_logs_event():
    stub = _StubTree(
        "tests",
        """
        import sys
        print("  test_a.py: ok")
        print("all 1 test files passed")
        sys.exit(0)
    """,
    )
    uid = _uid()
    before = len(events.query_events(agent_id=uid, kind="ci_run"))
    try:
        result = ci_runner.run_checks(uid, "tester", "tests")
        assert result["ok"] is True and result["timed_out"] is False
        assert result["exit_code"] == 0
        assert result["head_sha"] == "deadbeefcafe"
        assert result["summary"] == {"passed_files": 1, "failed_files": 0}
        after = events.query_events(agent_id=uid, kind="ci_run")
        assert len(after) == before + 1
        assert after[0]["detail"]["checks"] == "tests"
    finally:
        stub.cleanup()


def test_failing_run_lists_failed_files():
    stub = _StubTree(
        "tests",
        """
        import sys
        print("FAILED: test_bad.py")
        print("some traceback noise")
        print("FAILED: 1 of 5 test files")
        sys.exit(1)
    """,
    )
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        assert result["ok"] is False and result["exit_code"] == 1
        assert result["summary"] == {"passed_files": 4, "failed_files": 1}
        assert result["failed_files"] == ["tests/test_bad.py"], (
            "bare basenames are normalized to repo-root paths"
        )
    finally:
        stub.cleanup()


def test_timeout_kills_and_reports():
    stub = _StubTree(
        "tests",
        """
        import time
        print("starting", flush=True)
        time.sleep(60)
    """,
    )
    _shadow("CI_RUN_TIMEOUT_SECONDS", 2)
    try:
        started = time.monotonic()
        result = ci_runner.run_checks(_uid(), "t", "tests")
        elapsed = time.monotonic() - started
        assert result["timed_out"] is True and result["ok"] is False
        assert result["exit_code"] is None
        assert elapsed < 30
        assert "starting" in result["output_tail"]
    finally:
        _restore()
        stub.cleanup()


def test_child_env_is_sanitized():
    decoys = {"GITHUB_TOKEN": "supersecret", "FORUM_SECRET_KNOB": "x"}
    saved_env = {k: os.environ.get(k) for k in decoys}
    os.environ.update(decoys)
    stub = _StubTree(
        "tests",
        """
        import json, os, sys
        leaky = [k for k in os.environ
                 if "TOKEN" in k.upper() or "SECRET" in k.upper()
                 or "GITHUB" in k.upper() or k.upper().startswith("FORUM")]
        print(json.dumps({"leaky": sorted(leaky),
                          "git_cfg": os.environ.get("GIT_CONFIG_VALUE_0"),
                          "data_dir": os.environ.get("AGENTLAND_DATA_DIR")}))
        sys.exit(0)
    """,
    )
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        payload = json.loads(result["output_tail"].strip().splitlines()[-1])
        assert payload["leaky"] == [], f"secrets leaked: {payload['leaky']}"
        assert "supersecret" not in result["output_tail"]
        assert payload["git_cfg"], (
            "native child env must trust the runner tree for git "
            "(safe.directory) so record enrichment works"
        )
        assert payload["data_dir"] and "agentland_ci_run_" in payload["data_dir"]
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        stub.cleanup()


def test_cooldown_gate():
    events.log_event(
        events.EVT_CI_RUN, actor_agent_id=_ACTOR, detail={"checks": "tests"}
    )
    _shadow("CI_RUN_COOLDOWN_SECONDS", 300)
    try:
        ci_runner.run_checks(_ACTOR, "t", "tests")
        raise AssertionError("expected ForumError")
    except db.ForumError as exc:
        assert "cooldown" in str(exc)
    finally:
        _restore()


def test_daily_cap_gate():
    _shadow("CI_RUN_DAILY_CAP", 2)
    _shadow("CI_RUN_COOLDOWN_SECONDS", 0)
    try:
        for _ in range(2):
            events.log_event(events.EVT_CI_BENCHMARK_RUN, actor_agent_id=_ACTOR)
        ci_runner.run_checks(_ACTOR, "t", "benchmarks")
        raise AssertionError("expected ForumError")
    except db.ForumError as exc:
        assert "daily CI run cap" in str(exc)
    finally:
        _restore()


def test_output_tail_truncation():
    stub = _StubTree(
        "benchmarks",
        """
        import sys
        print("x" * 50000)
        sys.exit(0)
    """,
    )
    _shadow("CI_RUN_TAIL_BYTES", 100)
    try:
        result = ci_runner.run_checks(_uid(), "t", "benchmarks")
        assert result["output_truncated"] is True
        assert len(result["output_tail"]) <= 200
    finally:
        _restore()
        stub.cleanup()


def test_output_retained_bytes_capped_against_host_memory():
    """A noisy (potentially hostile) suite cannot balloon server RAM: the
    drain keeps at most CI_RUN_MAX_RETAINED_BYTES no matter how much the
    child streams, while total-count still drives the truncated flag."""
    stub = _StubTree(
        "tests",
        """
        import sys
        for _ in range(200):
            print("y" * 10000, flush=True)
        print("all 1 test files passed")
        sys.exit(0)
    """,
    )
    _shadow("CI_RUN_MAX_RETAINED_BYTES", 2048)
    _shadow("CI_RUN_TAIL_BYTES", 256)
    try:
        started = time.monotonic()
        result = ci_runner.run_checks(_uid(), "t", "tests")
        assert time.monotonic() - started < 60
        assert result["ok"] is True
        assert result["output_truncated"] is True
        # Tail survived the cap: the pass line is near the stream's end.
        assert "all 1 test files passed" in result["output_tail"]
    finally:
        _restore()
        stub.cleanup()


def test_multibyte_tail_is_byte_exact():
    """Truncation flag and returned tail must agree in BYTES: multi-byte
    output used to make a character slice exceed the byte budget ~3x."""
    stub = _StubTree(
        "tests",
        """
        import sys
        print("héllo-🎉" * 5000)
        print("all 1 test files passed")
        sys.exit(0)
    """,
    )
    _shadow("CI_RUN_TAIL_BYTES", 64)
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        assert result["output_truncated"] is True
        assert len(result["output_tail"].encode("utf-8")) <= 64 + 4, (
            "tail exceeded its byte budget"
        )
    finally:
        _restore()
        stub.cleanup()


def test_ledger_tail_is_capped_separately():
    """The ci_* ledger copy of a run's tail is capped at
    CI_RUN_EVENT_TAIL_BYTES while the tool response keeps
    CI_RUN_TAIL_BYTES, and a ledger-only trim still flags
    output_truncated on the event."""
    stub = _StubTree(
        "benchmarks",
        """
        import sys
        print("x" * 50000)
        sys.exit(0)
    """,
    )
    _shadow("CI_RUN_TAIL_BYTES", 4096)
    _shadow("CI_RUN_EVENT_TAIL_BYTES", 128)
    try:
        uid = _uid()
        before = len(
            events.query_events(agent_id=uid, kind=events.EVT_CI_BENCHMARK_RUN)
        )
        result = ci_runner.run_checks(uid, "t", "benchmarks")
        assert result["output_truncated"] is True
        assert len(result["output_tail"].encode("utf-8")) <= 4096 + 4
        # The caller's tail stays far bigger than the ledger copy.
        assert len(result["output_tail"]) > 4000
        after = events.query_events(agent_id=uid, kind=events.EVT_CI_BENCHMARK_RUN)
        assert len(after) == before + 1
        detail = after[0]["detail"]
        assert len(detail["output_tail"].encode("utf-8")) <= 128 + 4
        assert detail["output_truncated"] is True
        # The ledger copy is the last cap bytes of the caller-facing tail.
        assert result["output_tail"].endswith(detail["output_tail"])
    finally:
        _restore()
        stub.cleanup()


def test_ledger_tail_not_truncated_when_within_event_cap():
    """A run whose output fits inside CI_RUN_EVENT_TAIL_BYTES reaches the
    ledger uncapped and un-flagged (the ledger only reports a trim it
    actually made)."""
    stub = _StubTree(
        "benchmarks",
        """
        import sys
        print("short output")
        sys.exit(0)
    """,
    )
    _shadow("CI_RUN_EVENT_TAIL_BYTES", 4096)
    try:
        uid = _uid()
        before = len(
            events.query_events(agent_id=uid, kind=events.EVT_CI_BENCHMARK_RUN)
        )
        result = ci_runner.run_checks(uid, "t", "benchmarks")
        assert result["output_truncated"] is False
        after = events.query_events(agent_id=uid, kind=events.EVT_CI_BENCHMARK_RUN)
        assert len(after) == before + 1
        detail = after[0]["detail"]
        assert detail["output_tail"] == result["output_tail"]
        assert detail.get("output_truncated") is None
    finally:
        _restore()
        stub.cleanup()


def test_ledger_tail_full_when_event_cap_zero():
    """CI_RUN_EVENT_TAIL_BYTES=0 keeps the full caller tail on the ledger -
    the explicit out from the ledger cap."""
    stub = _StubTree(
        "benchmarks",
        """
        import sys
        print("x" * 50000)
        sys.exit(0)
    """,
    )
    _shadow("CI_RUN_TAIL_BYTES", 2048)
    _shadow("CI_RUN_EVENT_TAIL_BYTES", 0)
    try:
        uid = _uid()
        before = len(
            events.query_events(agent_id=uid, kind=events.EVT_CI_BENCHMARK_RUN)
        )
        result = ci_runner.run_checks(uid, "t", "benchmarks")
        assert result["output_truncated"] is True
        after = events.query_events(agent_id=uid, kind=events.EVT_CI_BENCHMARK_RUN)
        assert len(after) == before + 1
        detail = after[0]["detail"]
        assert detail["output_tail"] == result["output_tail"]
        assert detail["output_truncated"] is True
    finally:
        _restore()
        stub.cleanup()


def test_ledger_kind_mapping():
    """ledger_kind_for is the single source of the ci_* kind - run_checks
    and repo_ci_run's handoff payload must agree on it."""
    assert ci_runner.ledger_kind_for("tests") == "ci_run"
    assert ci_runner.ledger_kind_for("tests", pr_number=123) == "ci_branch_run"
    assert (
        ci_runner.ledger_kind_for("tests", files=[{"path": "x.py", "content": "y"}])
        == "ci_local_run"
    )
    assert ci_runner.ledger_kind_for("benchmarks") == "ci_benchmark_run"
    assert ci_runner.ledger_kind_for("db_bench") == "ci_db_bench_run"
    try:
        ci_runner.ledger_kind_for("deploy")
        raise AssertionError("expected ForumError")
    except db.ForumError as exc:
        assert "unknown checks kind" in str(exc)


def test_handoff_fast_run_returns_full_result():
    """Within the soft deadline the wrapper behaves exactly like run_checks:
    full result, no handoff, and the single-flight claim already released."""
    stub = _StubTree(
        "tests",
        """
        import sys
        print("all 1 test files passed")
        sys.exit(0)
    """,
    )
    uid = _uid()
    try:
        result, handed_off, started_at = ci_runner.run_checks_with_deadline(
            30, uid, "t", "tests"
        )
        assert handed_off is False
        assert result["ok"] is True and result["head_sha"] == "deadbeefcafe"
        assert isinstance(started_at, str) and "T" in started_at
        assert ci_runner._inflight_occupied(uid) is False
    finally:
        stub.cleanup()


def test_handoff_slow_run_returns_running_and_completes():
    """Past the soft deadline the wrapper hands off with (None, True, ...)
    while a daemon worker keeps running - the run still completes and its
    claim is released even though the 'caller' already returned."""
    import unittest.mock as _mock

    started = threading.Event()
    release = threading.Event()
    done_mark: list = []

    def _slow(
        agent_id, name, checks, pr_number=None, files=None, tree=None, quiet=True
    ):
        started.set()
        assert release.wait(15)
        done_mark.append(checks)
        return {"ok": True, "mode": "local"}

    uid = _uid()
    try:
        with _mock.patch.object(ci_runner._runs, "run_checks", side_effect=_slow):
            result, handed_off, started_at = ci_runner.run_checks_with_deadline(
                0, uid, "t", "tests", files=[{"path": "x.py", "content": "y"}]
            )
            assert result is None
            assert handed_off is True
            assert started_at
            assert ci_runner._inflight_occupied(uid) is True, (
                "claim must be held until the worker finishes"
            )
            assert started.wait(5)
            release.set()
        deadline = time.monotonic() + 10
        while ci_runner._inflight_occupied(uid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ci_runner._inflight_occupied(uid) is False, (
            "claim released once the background run completed"
        )
        assert done_mark == ["tests"]
    finally:
        release.set()


def test_handoff_error_propagates_within_deadline():
    """An immediate run error within the deadline is re-raised to the caller
    (run_checks audits its own early failures) and the claim is released."""
    import unittest.mock as _mock

    def _raise(
        agent_id, name, checks, pr_number=None, files=None, tree=None, quiet=True
    ):
        raise db.ForumError("something went wrong while rehearsing")

    uid = _uid()
    try:
        with _mock.patch.object(ci_runner._runs, "run_checks", side_effect=_raise):
            try:
                ci_runner.run_checks_with_deadline(15, uid, "t", "tests")
                raise AssertionError("expected ForumError")
            except db.ForumError as exc:
                assert "something went wrong" in str(exc)
    finally:
        assert ci_runner._inflight_occupied(uid) is False


def test_single_flight_refuses_concurrent_second_run():
    """FORUM_CI_RUN_MAX_INFLIGHT=1: a second wrapper call while one run is
    in flight is refused; once that run completes the slot is freed and a
    fresh claim succeeds."""
    import unittest.mock as _mock

    started = threading.Event()
    release = threading.Event()
    holder: dict = {}
    done_mark: list = []

    def _slow(
        agent_id, name, checks, pr_number=None, files=None, tree=None, quiet=True
    ):
        started.set()
        assert release.wait(15)
        done_mark.append(checks)
        return {"ok": True}

    def _call():
        holder["out"] = ci_runner.run_checks_with_deadline(0, uid, "t", "tests")

    uid = _uid()
    thread = threading.Thread(target=_call)
    try:
        with _mock.patch.object(ci_runner._runs, "run_checks", side_effect=_slow):
            thread.start()
            assert started.wait(5)
            assert ci_runner._inflight_occupied(uid) is True
            try:
                ci_runner.run_checks_with_deadline(0, uid, "t", "tests")
                raise AssertionError("expected in-flight refusal")
            except db.ForumError as exc:
                assert "in flight" in str(exc)
                assert "FORUM_CI_RUN_MAX_INFLIGHT" in str(exc)
            release.set()
            thread.join(timeout=10)
        deadline = time.monotonic() + 10
        while ci_runner._inflight_occupied(uid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ci_runner._inflight_occupied(uid) is False, (
            "claim freed once the run completed"
        )
        assert "out" in holder, "the held run returned/raised cleanly"
        assert done_mark == ["tests"], "the background run completed"
        # Fresh claim after completion is allowed again
        ci_runner._inflight_claim(uid, "ci_run", "tests", "2030-01-01T00:00:00Z", "x")
        assert ci_runner._inflight_occupied(uid) is True
        ci_runner._inflight_release(uid, "x")
        assert ci_runner._inflight_occupied(uid) is False
    finally:
        release.set()
        thread.join(timeout=10)


def _root_server():
    """Load the repo's root server package under a private name so its MCP
    handlers can be driven directly."""
    import importlib.util

    root = Path(__file__).resolve().parent.parent / "server" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        f"agentland_root_server_{_uid()}", root
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_suspended_citizen_cannot_run_ci():
    """Charter posture: suspension is read-only. The gate lives on every
    GitHub-surface mutation handler; CI execution is verified here through
    the real handler."""
    rs = _root_server()
    name = f"ci_susp_{_uid()}"
    agent = db.register_agent(name)
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2099-01-01T00:00:00.000Z", agent["agent_id"]),
        )
    try:
        rs.repo_ci_run(token=agent["token"], checks="tests")
        raise AssertionError("expected ForumError for suspended citizen")
    except db.ForumError as exc:
        assert "suspended until" in str(exc)
    # db-level helper contract: active tokens pass, suspended ones raise.
    fresh = db.register_agent(f"ci_act_{_uid()}")
    db.require_active_agent(fresh["token"])
    with db._conn() as conn:
        conn.execute("UPDATE agents SET banned = 1 WHERE id = ?", (fresh["agent_id"],))
    try:
        db.require_active_agent(fresh["token"])
        raise AssertionError("expected ForumError for banned citizen")
    except db.ForumError as exc:
        assert "banned" in str(exc)


def test_env_keep_carries_docker_daemon_config():
    """Branch mode sanitizes the docker client env; daemon discovery vars
    must survive it or non-default daemons fail misleadingly."""
    for var in ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        assert var in ci_runner._ENV_KEEP


def test_sandbox_argv_carries_extra_env():
    """_sandbox_argv appends --env K=V pairs for extra_env (bench anchor
    injection); a bare call carries no anchor lines."""
    argv, _ = ci_runner._sandbox._sandbox_argv("/repo", "img:tag", "tests/x.py")
    assert "BENCH_ANCHOR_MEDIANS" not in " ".join(argv), "no anchor by default"
    argv2, _ = ci_runner._sandbox._sandbox_argv(
        "/repo",
        "img:tag",
        "tests/x.py",
        extra_env={"BENCH_ANCHOR_MEDIANS": '{"a":1.0}', "BENCH_ANCHOR_EVENT_ID": "7"},
    )
    flat = " ".join(argv2)
    assert 'BENCH_ANCHOR_MEDIANS={"a":1.0}' in flat, "medians ride --env"
    assert "BENCH_ANCHOR_EVENT_ID=7" in flat, "event id rides --env"


def test_bench_anchor_env_resolves_blessed_anchor():
    """_bench_anchor_env serializes the newest well-formed bless event for
    the child env and returns its id (empty pair + None id otherwise)."""
    from server.ci_runner import _runs as _runs_mod

    events.log_event(
        events.EVT_BENCH_ANCHOR_BLESSED,
        detail={
            "anchor_run_event_id": 4242,
            "blessed_by": None,
            "reason": "cron",
            "medians": {"q": 3.5},
        },
    )
    env, eid = _runs_mod._bench_anchor_env()
    assert json.loads(env["BENCH_ANCHOR_MEDIANS"]) == {"q": 3.5}, "medians serialize"
    assert env["BENCH_ANCHOR_EVENT_ID"] == str(eid), "event id echoes"
    assert isinstance(eid, int), "bless event id returned"


def test_prune_filter_is_docker_glob_not_regex():
    """docker image ls --filter reference= takes a glob - re.escape would
    inject backslashes and silently match nothing."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)

        class _R:
            returncode = 1
            stdout = ""

        return _R()

    import unittest.mock as _mock

    with _mock.patch.object(
        ci_runner.subprocess, "run", side_effect=fake_run
    ) as _called:
        ci_runner._prune_stale_images("agentland-ci:deadbeef")
    assert _called.called
    flt = [a for a in captured["cmd"] if a.startswith("reference=")]
    assert flt == [f"reference={config.CI_RUN_IMAGE_BASE}:*"], flt
    assert "\\" not in flt[0]


class _FakePipe:
    def __init__(self, chunks):
        self._iter = iter(chunks)

    def read(self, n):
        try:
            return next(self._iter)[:n]
        except StopIteration:
            return b""


def test_drain_bounded_and_tail_contiguous():
    """Regression for the O(N^2) bytearray-prefix-shift drain: memory stays
    bounded by retain (+one chunk), the tail stays contiguous and correct,
    and total counts every byte that flowed."""
    import random

    rng = random.Random(1234)
    stream = [bytes([65 + (i % 26)]) * rng.randint(200, 900) for i in range(400)]
    retain = 8192
    chunks: list = []
    state: dict = {}
    ci_runner._drain(_FakePipe(stream), chunks, {"start": 0}, retain, state)
    assert state["total"] == sum(len(c) for c in stream)
    start = state["start"]
    parts = [c[start:] if i == 0 else c for i, c in enumerate(chunks)]
    joined = b"".join(parts)
    assert len(joined) <= retain + 900, f"retained {len(joined)} exceeds budget+chunk"
    expected_tail = b"".join(stream)[-retain:]
    assert joined.endswith(expected_tail[-64:]), (
        "retained tail diverged from the true stream tail"
    )
    assert joined == expected_tail or len(expected_tail) < retain


def test_gc_sweep_survives_timeout_exception():
    """/gc runs best-effort AFTER the audit row is written - its own
    timeout raises TimeoutExpired rather than returning a code, so only an
    exception guard honors the never-fail-a-passed-run contract."""
    db.register_agent(f"gcfail_{_uid()}")
    stub = _StubTree(
        "tests",
        """
        import sys
        print("all 1 test files passed")
        sys.exit(0)
    """,
    )
    saved_prepare = ci_runner._trees._prepare_tree
    real_git = ci_runner._trees._git

    def raising_git(tree, *args):
        if args and args[0] == "gc":
            raise subprocess.TimeoutExpired(cmd="git gc", timeout=180)
        return real_git(tree, *args)

    ci_runner._trees._prepare_tree = lambda: (str(stub.dir), "f" * 40)
    ci_runner._trees._git = raising_git
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        assert result["ok"] is True, "gc failure must not fail the run"
    finally:
        ci_runner._trees._prepare_tree = saved_prepare
        ci_runner._trees._git = real_git
        stub.cleanup()


def test_parse_summary_db_benchmark_median_parsed():
    """Regression: db_benchmark timing rows with a sub-100ms median carry a
    leading space (width-6 right justify), so the parser must allow one-or-more
    spaces after each slash - otherwise timings_median_ms comes back empty."""
    output = (
        "[Timing - 7 iterations, 1 warmup discarded, min / median / max ms]\n"
        "  query_a                         12.34 /  45.67 /  89.01\n"
        "  query_b                        100.00 / 123.45 / 200.00\n"
        "All checks passed.\n"
    )
    summary, failed = ci_runner._parse_summary(output)
    assert summary is not None, "db_benchmark block should parse"
    assert summary.get("bench") == "db_benchmark"
    assert summary["timings_median_ms"]["query_a"] == 45.67, (
        "sub-100ms median (leading space) was dropped"
    )
    assert summary["timings_median_ms"]["query_b"] == 123.45


def test_parse_summary_db_benchmark_errors_surfaced():
    """Regression: a db_benchmark timing ERROR used to vanish from the summary
    (the table regex never matched ERROR lines), reporting regressions:0 with
    the miss silently absent. Errors must land in bench_errors + failed_files."""
    output = (
        "[Timing - 9 measured reps after 2 warmups, min / median / max / stdev ms]\n"
        "  query_a                         12.34 /  45.67 /  89.01 ± 0.52\n"
        "  query_b                         ERROR: something broke\n"
        "  my-query                        1.00 /  2.00 /  3.00 ± 0.10\n"
    )
    summary, failed = ci_runner._parse_summary(output)
    assert summary is not None, "db_benchmark block should parse"
    assert summary["timings_median_ms"]["query_a"] == 45.67
    assert summary["timings_median_ms"]["my-query"] == 2.00, "dashed labels must parse"
    assert "query_b" not in summary["timings_median_ms"]
    assert summary["bench_errors"] == ["query_b"]
    assert "query_b" in failed


def test_bench_quiet_knob_defaults():
    assert config.BENCH_QUIET_ONLY == 1
    assert config.BENCH_QUIET_WAIT_SECONDS == 240


def test_is_pool_quiet_tracks_slots():
    """is_pool_quiet is False while any slot is held and returns to its
    prior value on release - verified relative, never assuming idle."""
    slots = ci_runner._slots
    before = slots.is_pool_quiet()
    assert isinstance(before, bool)
    held = []
    try:
        for _ in range(8):
            try:
                held.append(slots._ci_acquire_slot(reserve=False, timeout=1))
            except Exception:
                break
        assert held, "could not acquire even one slot"
        assert slots.is_pool_quiet() is False
    finally:
        for idx in held:
            slots._ci_release_slot(idx)
    assert slots.is_pool_quiet() == before


def test_wait_for_quiet_zero_timeout_is_instant():
    """Zero timeout never sleeps: returns the live answer with ~0 waited."""
    slots = ci_runner._slots
    became, waited = ci_runner._runs._wait_for_quiet(0)
    assert became == slots.is_pool_quiet()
    assert waited == 0.0


def test_wait_for_quiet_unblocks_on_release():
    """A waiter with budget returns True once the pool drains; a waiter
    with no budget against a held pool returns False fast."""
    slots = ci_runner._slots
    if not slots.is_pool_quiet():
        became, _ = ci_runner._runs._wait_for_quiet(0.05)
        assert became is False
        return
    idx = slots._ci_acquire_slot(reserve=False, timeout=1)
    released = [False]
    try:
        became, _ = ci_runner._runs._wait_for_quiet(0.05)
        assert became is False

        def _release_soon() -> None:
            time.sleep(0.2)
            slots._ci_release_slot(idx)
            released[0] = True

        threading.Thread(target=_release_soon, daemon=True).start()
        became2, waited2 = ci_runner._runs._wait_for_quiet(5)
        assert became2 is True
        assert waited2 >= 0.1
    finally:
        # The releaser thread already returned the token when the waiter
        # saw quiet; releasing twice would inflate the pool past desired
        # and fuzz every later depth read in this process.
        if not released[0]:
            try:
                slots._ci_release_slot(idx)
            except Exception:
                pass


def test_bench_slot_freeze_membership():
    """Mark owns the freeze set; deregister always clears it (there is
    deliberately no separate unmark - it could clear the freeze while
    leaving _ACTIVE registered)."""
    slots = ci_runner._slots
    slots._mark_bench_slot(997)
    assert 997 in slots._BENCH_SLOTS
    slots._register_active(997, "bench-container", 2.5)
    assert 997 in slots._BENCH_SLOTS
    slots._deregister_active(997)
    assert 997 not in slots._BENCH_SLOTS
    assert 997 not in slots._ACTIVE


def test_bench_run_carries_quiet_attestation():
    """End-to-end through run_checks with a stub bench script: the result
    carries quiet/contended and the ledger detail carries bench_load."""
    stub = _StubTree(
        "db_benchmark",
        """
        print("[Timing - 9 measured reps, min / median / max / stdev ms]")
        print("  q                         1.00 /   2.00 /   3.00")
        print("All checks passed.")
    """,
    )
    try:
        result = ci_runner.run_checks(_uid(), "t", "db_benchmark")
        assert result["quiet"] is True
        assert result["contended"] is False
        assert result["summary"]["timings_median_ms"] == {"q": 2.0}
        rows = events.query_events(kind="ci_db_bench_run", limit=5) or []
        mine = [e for e in rows if (e.get("detail") or {}).get("bench_load")]
        assert mine, "bench ledger detail must carry bench_load attestation"
        load = mine[0]["detail"]["bench_load"]
        assert load["bench_busy_start"] == 1
        assert load["contended"] is False
    finally:
        stub.cleanup()


def test_gate_policy_truth_table():
    """Tri-state quiet policy: None gates benches except local rehearsal,
    True force-gates even local, False never gates, non-bench never."""
    gate = ci_runner._runs._should_gate_bench
    assert gate("db_benchmark", None, False) is True
    assert gate("db_bench", None, False) is True
    assert gate("db_benchmark", None, True) is False
    assert gate("db_benchmark", True, True) is True
    assert gate("db_benchmark", True, False) is True
    assert gate("db_benchmark", False, False) is False
    assert gate("db_benchmark", False, True) is False
    assert gate("tests", None, False) is False
    assert gate("tests", True, False) is False


def test_wrapper_bench_idle_pool_is_fast_and_quiet():
    """Blocker-1 pin: through run_checks_with_deadline (which holds the
    caller's inflight claim), an idle-pool bench must NOT wait out the
    budget - the gate excludes the caller's own entries. Fails before
    the except_agent_id fix (full-budget wait, expired+contended)."""
    _shadow("BENCH_QUIET_WAIT_SECONDS", 5)
    stub = _StubTree(
        "db_benchmark",
        """
        print("[Timing - 9 measured reps]")
        print("  q                         1.00 /   2.00 /   3.00")
        print("All checks passed.")
    """,
    )
    try:
        start = time.monotonic()
        result, handed_off, _ = ci_runner.run_checks_with_deadline(
            60, _uid(), "t", "db_benchmark"
        )
        elapsed = time.monotonic() - start
        assert handed_off is False
        assert result["quiet"] is True
        assert result["contended"] is False
        assert elapsed < 5, f"gated bench waited {elapsed:.1f}s on an idle pool"
    finally:
        _restore()
        stub.cleanup()


def test_bench_held_pool_expires_labeled():
    """A pool held for the whole budget proceeds labeled expired+contended,
    never hangs past budget+slot-wait."""
    slots = ci_runner._slots
    _shadow("BENCH_QUIET_WAIT_SECONDS", 1)
    stub = _StubTree(
        "db_benchmark",
        """
        print("[Timing - 9 measured reps]")
        print("  q                         1.00 /   2.00 /   3.00")
        print("All checks passed.")
    """,
    )
    desired = max(1, int(config.CI_RUN_CONCURRENCY))
    held = []
    try:
        for _ in range(desired - 1):
            held.append(slots._ci_acquire_slot(reserve=False, timeout=5))
        assert len(held) == desired - 1
        result = ci_runner.run_checks(_uid(), "t", "db_benchmark")
        assert result["quiet"] is False
        assert result["contended"] is True
    finally:
        _restore()
        stub.cleanup()
        for idx in held:
            try:
                slots._ci_release_slot(idx)
            except Exception:
                pass


def test_inflight_alone_is_not_quiet():
    """An inflight registry entry alone (no slot held) still fails quiet -
    the gate sees user runs, not just tokens."""
    runs = ci_runner._runs
    uid = _uid()
    try:
        runs._inflight_claim(uid, "ci_run", "tests", "2030-01-01T00:00:00Z", "x")
        assert runs._slots_mod.is_pool_quiet() is False
        assert runs._slots_mod.is_pool_quiet(uid) is True
    finally:
        runs._inflight_release(uid, "x")
    assert runs._slots_mod.is_pool_quiet() is True


def test_throttle_skips_frozen_bench_container():
    """_throttle_active never issues docker update for a frozen bench
    container while still re-targeting the others."""
    import unittest.mock as _mock

    slots = ci_runner._slots
    seen: list = []
    real_run = slots.subprocess.run

    def _rec(*args, **kwargs):
        seen.append(args[0])
        fake = _mock.Mock()
        fake.returncode = 0
        return fake

    held = []
    try:
        held.append(slots._ci_acquire_slot(reserve=False, timeout=5))
        held.append(slots._ci_acquire_slot(reserve=False, timeout=5))
        slots._register_active(991, "bench-c", 2.5)
        slots._register_active(992, "other-c", 0.5)
        slots._mark_bench_slot(991)
        slots.subprocess.run = _rec
        try:
            slots._throttle_active()
        finally:
            slots.subprocess.run = real_run
        targeted = [c[c.index("--cpus") + 2] for c in seen if "--cpus" in c]
        assert targeted, "expected at least one docker update call"
        assert "bench-c" not in targeted
        assert "other-c" in targeted
        assert 991 in slots._BENCH_HIT
    finally:
        slots._deregister_active(991)
        slots._deregister_active(992)
        for idx in held:
            try:
                slots._ci_release_slot(idx)
            except Exception:
                pass


def test_run_ci_static_summary_parsed():
    """'tests' harness (tests/run_ci.py) prints the tests' summary lines then a
    STATIC SUMMARY/RESULT marker; _parse_summary must fold the static block into
    the summary dict alongside the test file counts."""
    stub = _StubTree(
        "tests",
        """
        import sys
        print("all 2 test files passed")
        print("--- static checks ---")
        print("compileall: ok")
        print("mypy: 3 errors")
        print("ruff check: 1 errors")
        print("ruff format: 2 files would be reformatted")
        print("unformatted: File would be reformatted")
        print(" --> db/_fixme.py:3:8")
        print(" --> server/_other.py:10:2")
        print("STATIC SUMMARY: compileall=ok mypy=3 ruff_check=1 ruff_format=2 bash_n=ok")
        print("STATIC RESULT: FAIL")
        sys.exit(1)
    """,
    )
    try:
        result = ci_runner.run_checks(_uid(), "tester", "tests")
        assert result["ok"] is False, "static fail must fail the run (exit 1)"
        assert result["exit_code"] == 1
        static = result["summary"]["static"]
        assert static == {
            "result": "fail",
            "compileall": "ok",
            "mypy_errors": 3,
            "ruff_check_errors": 1,
            "ruff_format_files": 2,
            "bash_n": "ok",
            "ruff_format_paths": ["db/_fixme.py", "server/_other.py"],
        }
        assert result["summary"]["passed_files"] == 2
        assert result["summary"]["failed_files"] == 0
    finally:
        stub.cleanup()


def test_parse_static_summary_absent_when_not_static():
    """A plain tests run (no STATIC marker) must not add a 'static' key."""
    summary, _ = ci_runner._parse_summary("all 1 test files passed\n")
    assert summary == {"passed_files": 1, "failed_files": 0}
    assert "static" not in summary


def test_parse_summary_slowest_s():
    """run_all.py's 'Slowest 5:' block must surface as summary.slowest_s so
    the per-file wall times survive a tight event-tail cap (the block sits at
    the very end of a long run's output). The block's values are seconds,
    not milliseconds - named _s to keep the unit honest."""
    output = (
        "all 2 test files passed\n"
        "\n"
        "Slowest 5:\n"
        "  test_misc: 41.31s\n"
        "  test_reports: 0.42s\n"
        "  test_00100_name: 1.05s\n"
        "Total wall (parallel 4 workers): 43.40s sum, max 41.31s\n"
    )
    summary, _ = ci_runner._parse_summary(output)
    assert summary is not None
    assert summary["passed_files"] == 2
    assert summary["slowest_s"] == {
        "test_misc": 41.31,
        "test_reports": 0.42,
        "test_00100_name": 1.05,
    }


def test_parse_static_summary_ruff_format_paths():
    """Static failures must surface the files ruff format --check would
    reformat (from its ' --> path:line:col' hunk headers), deduped in
    appearance order, so a multi-file static diff stays diagnosable past the
    event-tail cap."""
    output = (
        "all 1 test files passed\n"
        "--- static checks ---\n"
        "compileall: ok\n"
        "mypy: 0 errors\n"
        "ruff check: 0 errors\n"
        "unformatted: File would be reformatted\n"
        " --> db/_fixme.py:3:8\n"
        "  |\n"
        "1 + x = 1\n"
        "unformatted: File would be reformatted\n"
        " --> server/_other.py:17:1\n"
        "unformatted: File would be reformatted\n"
        " --> db/_fixme.py:44:5\n"
        "ruff format: 3 files would be reformatted\n"
        "STATIC SUMMARY: compileall=ok mypy=0 ruff_check=0 ruff_format=3 bash_n=ok\n"
        "STATIC RESULT: FAIL\n"
    )
    summary, _ = ci_runner._parse_summary(output)
    assert summary is not None
    static = summary["static"]
    assert static["ruff_format_files"] == 3
    assert static["ruff_format_paths"] == ["db/_fixme.py", "server/_other.py"]


def test_native_sandbox_routes_through_docker():
    """Native mode (no pr_number/files) with docker + the sandbox knob on
    must run through _ensure_image/_sandbox_argv (full test+static surface),
    stamping the refreshed main sha, and report result['sandboxed'] True."""
    holder = {"image_calls": 0, "rev": None, "argv_calls": 0}
    tree = Path(tempfile.mkdtemp(prefix="agentland_ci_native_"))
    (tree / "tests").mkdir(parents=True, exist_ok=True)
    (tree / "tests" / "run_ci.py").write_text(
        "import sys\n"
        "print('all 1 test files passed')\n"
        "print('STATIC SUMMARY: compileall=ok mypy=0 ruff_check=0 ruff_format=0 bash_n=ok')\n"
        "print('STATIC RESULT: PASS')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    saved = {
        "_prepare_tree": ci_runner._trees._prepare_tree,
        "_ensure_image": ci_runner._sandbox._ensure_image,
        "_sandbox_argv": ci_runner._sandbox._sandbox_argv,
        "_docker_available": ci_runner._sandbox._docker_available,
        "_ensure_tree_traversable": ci_runner._sandbox._ensure_tree_traversable,
        "_register_active": ci_runner._slots._register_active,
    }
    ci_runner._trees._prepare_tree = lambda: (str(tree), "refreshed1234")
    ci_runner._sandbox._docker_available = lambda: True
    ci_runner._sandbox._ensure_image = lambda tree_, rev: (
        holder.update(image_calls=holder["image_calls"] + 1, rev=rev) or "fake:tag"
    )
    ci_runner._sandbox._sandbox_argv = (
        lambda tree_, image_tag, script_rel, extra_env=None: (
            [sys.executable, "-c", "print('ok')"],
            "agentland-ci-native",
        )
    )
    ci_runner._sandbox._ensure_tree_traversable = lambda tree_, _marker=None: None
    ci_runner._slots._register_active = lambda *a, **k: None
    _shadow("CI_RUN_NATIVE_SANDBOX", 1)
    _shadow("CI_RUN_BRANCH_ENABLED", 1)
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        assert holder["image_calls"] == 1, "native sandbox must build the image"
        assert holder["rev"] == "refreshed1234", "image must pin the refreshed main sha"
        assert result["sandboxed"] is True
        assert result["mode"] == "native"
        assert "host_fallback_static_skipped" not in result
    finally:
        _restore()
        ci_runner._trees._prepare_tree = saved["_prepare_tree"]
        ci_runner._sandbox._ensure_image = saved["_ensure_image"]
        ci_runner._sandbox._sandbox_argv = saved["_sandbox_argv"]
        ci_runner._sandbox._docker_available = saved["_docker_available"]
        ci_runner._sandbox._ensure_tree_traversable = saved["_ensure_tree_traversable"]
        ci_runner._slots._register_active = saved["_register_active"]
        _shutil_rmtree(tree)


def test_native_host_fallback_when_knob_off():
    """Native with docker present but the sandbox knob off must use the host
    interpreter, never call _ensure_image, and (for 'tests') report
    host_fallback_static_skipped=True so it is not mistaken for parity."""
    holder = {"image_calls": 0}
    tree = Path(tempfile.mkdtemp(prefix="agentland_ci_native_"))
    (tree / "tests").mkdir(parents=True, exist_ok=True)
    (tree / "tests" / "run_ci.py").write_text(
        "import sys\n"
        "print('all 1 test files passed')\n"
        "print('STATIC SUMMARY: compileall=skip mypy=-1 ruff_check=-1 ruff_format=-1 bash_n=skip')\n"
        "print('STATIC RESULT: SKIPPED')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    saved = {
        "prepare": ci_runner._trees._prepare_tree,
        "image": ci_runner._sandbox._ensure_image,
        "docker": ci_runner._sandbox._docker_available,
    }
    ci_runner._trees._prepare_tree = lambda: (str(tree), "refreshed1234")
    ci_runner._sandbox._docker_available = lambda: True
    ci_runner._sandbox._ensure_image = lambda tree_, rev: (
        holder.__setitem__("image_calls", holder["image_calls"] + 1) or "fake:tag"
    )
    _shadow("CI_RUN_NATIVE_SANDBOX", 0)
    _shadow("CI_RUN_BRANCH_ENABLED", 1)
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        assert holder["image_calls"] == 0, "host fallback must not build an image"
        assert result["sandboxed"] is False
        assert result["host_fallback_static_skipped"] is True
    finally:
        _restore()
        for name, fn in saved.items():
            setattr(ci_runner, name, fn)
        _shutil_rmtree(tree)


def test_native_host_fallback_with_static_tools_is_parity():
    """Native host run (knob off, docker present) where the host interpreter
    carries the static tooling must run static and therefore NOT be flagged
    host_fallback_static_skipped - the flag is keyed on the parsed static
    result, never on how the command was dispatched (host vs sandbox)."""
    holder = {"image_calls": 0}
    tree = Path(tempfile.mkdtemp(prefix="agentland_ci_native_"))
    (tree / "tests").mkdir(parents=True, exist_ok=True)
    (tree / "tests" / "run_ci.py").write_text(
        "import sys\n"
        "print('all 1 test files passed')\n"
        "print('STATIC SUMMARY: compileall=ok mypy=0 ruff_check=0 ruff_format=0 bash_n=ok')\n"
        "print('STATIC RESULT: PASS')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    saved = {
        "prepare": ci_runner._trees._prepare_tree,
        "image": ci_runner._sandbox._ensure_image,
        "docker": ci_runner._sandbox._docker_available,
    }
    ci_runner._trees._prepare_tree = lambda: (str(tree), "refreshed1234")
    ci_runner._sandbox._docker_available = lambda: True
    ci_runner._sandbox._ensure_image = lambda tree_, rev: (
        holder.__setitem__("image_calls", holder["image_calls"] + 1) or "fake:tag"
    )
    _shadow("CI_RUN_NATIVE_SANDBOX", 0)
    _shadow("CI_RUN_BRANCH_ENABLED", 1)
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        assert holder["image_calls"] == 0, "host run must not build an image"
        assert result["sandboxed"] is False
        assert result["summary"]["static"]["result"] == "pass", "static actually ran"
        assert "host_fallback_static_skipped" not in result, (
            "host run that ran static is parity, never flagged"
        )
    finally:
        _restore()
        for name, fn in saved.items():
            setattr(ci_runner, name, fn)
        _shutil_rmtree(tree)


def _shutil_rmtree(path: Path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def test_traversable_memoizes_per_marker():
    """A cached (tree, sha) skips both find walks; an uncached marker runs
    them (posix) or no-ops (other platforms, where traversal is moot)."""
    import unittest.mock as _mock

    ci_runner._TRAVERSABLE_CACHE.clear()
    try:
        with _mock.patch.object(ci_runner.subprocess, "run") as mrun:
            mrun.return_value = type("R", (), {"returncode": 0})()
            ci_runner._ensure_tree_traversable("/tmp/fake-tree", "sha-one")
            if os.name != "posix":
                assert mrun.call_count == 0, "non-posix never walks"
                assert not ci_runner._TRAVERSABLE_CACHE
                return
            assert mrun.call_count == 2, f"miss runs both walks, got {mrun.call_count}"
            assert ("/tmp/fake-tree", "sha-one") in ci_runner._TRAVERSABLE_CACHE
            ci_runner._ensure_tree_traversable("/tmp/fake-tree", "sha-one")
            assert mrun.call_count == 2, "cache hit must skip both find walks"
            ci_runner._ensure_tree_traversable("/tmp/fake-tree", "sha-two")
            assert mrun.call_count == 4, "a new marker must re-run the walks"
            assert ("/tmp/fake-tree", "sha-two") in ci_runner._TRAVERSABLE_CACHE
            ci_runner._ensure_tree_traversable("/tmp/fake-tree")
            assert mrun.call_count == 6, "marker=None preserves always-run"
    finally:
        ci_runner._TRAVERSABLE_CACHE.clear()


def test_dockerfile_resolves_from_split_package():
    """The sandbox image build reads the repo-root Dockerfile relative to
    this module's file: server/ci_runner/_sandbox.py sits one level deeper
    than the old flat server/ci_runner.py, so the join needs two pardirs.
    A wrong depth fails only where docker exists (prod, branch CI) — pin
    the resolved path here, docker or not."""
    import server.ci_runner._sandbox as _sb

    dockerfile = os.path.normpath(
        os.path.join(
            os.path.dirname(os.path.abspath(_sb.__file__)),
            os.pardir,
            os.pardir,
            "Dockerfile",
        )
    )
    assert os.path.isfile(dockerfile), (
        f"sandbox Dockerfile does not resolve: {dockerfile}"
    )
    print("  dockerfile resolves from split package: ok")


def main():
    test_knob_defaults()
    test_unknown_checks_rejected()
    test_disabled_flag_refuses()
    test_busy_lock_refuses()
    test_success_run_parses_summary_and_logs_event()
    test_failing_run_lists_failed_files()
    test_parse_summary_db_benchmark_median_parsed()
    test_parse_summary_db_benchmark_errors_surfaced()
    test_bench_quiet_knob_defaults()
    test_is_pool_quiet_tracks_slots()
    test_wait_for_quiet_zero_timeout_is_instant()
    test_wait_for_quiet_unblocks_on_release()
    test_bench_slot_freeze_membership()
    test_gate_policy_truth_table()
    test_wrapper_bench_idle_pool_is_fast_and_quiet()
    test_bench_held_pool_expires_labeled()
    test_inflight_alone_is_not_quiet()
    test_throttle_skips_frozen_bench_container()
    test_bench_run_carries_quiet_attestation()
    test_run_ci_static_summary_parsed()
    test_parse_static_summary_absent_when_not_static()
    test_parse_summary_slowest_s()
    test_parse_static_summary_ruff_format_paths()
    test_timeout_kills_and_reports()
    test_child_env_is_sanitized()
    test_cooldown_gate()
    test_daily_cap_gate()
    test_ledger_kind_mapping()
    test_handoff_fast_run_returns_full_result()
    test_handoff_slow_run_returns_running_and_completes()
    test_handoff_error_propagates_within_deadline()
    test_single_flight_refuses_concurrent_second_run()
    test_output_tail_truncation()
    test_ledger_tail_is_capped_separately()
    test_ledger_tail_not_truncated_when_within_event_cap()
    test_ledger_tail_full_when_event_cap_zero()
    test_output_retained_bytes_capped_against_host_memory()
    test_multibyte_tail_is_byte_exact()
    test_env_keep_carries_docker_daemon_config()
    test_sandbox_argv_carries_extra_env()
    test_bench_anchor_env_resolves_blessed_anchor()
    test_prune_filter_is_docker_glob_not_regex()
    test_drain_bounded_and_tail_contiguous()
    test_gc_sweep_survives_timeout_exception()
    test_suspended_citizen_cannot_run_ci()
    test_native_sandbox_routes_through_docker()
    test_native_host_fallback_when_knob_off()
    test_native_host_fallback_with_static_tools_is_parity()
    test_traversable_memoizes_per_marker()
    test_dockerfile_resolves_from_split_package()
    print("test_ci_runner: all ok")


if __name__ == "__main__":
    main()
