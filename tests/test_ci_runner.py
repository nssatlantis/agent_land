"""Tests for the server-side CI runner (repo_ci_run): guardrail gating,
main-only tree refresh seams, sanitized child environments, timeout kill,
output tailing, and the events-ledger audit trail."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
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
        self._orig = ci_runner._prepare_tree
        self._saved_native = config.CI_RUN_NATIVE_SANDBOX
        config.CI_RUN_NATIVE_SANDBOX = 0
        ci_runner._prepare_tree = lambda: (str(self.dir), "deadbeefcafe")

    def cleanup(self):
        ci_runner._prepare_tree = self._orig
        config.CI_RUN_NATIVE_SANDBOX = self._saved_native


def test_knob_defaults():
    assert config.CI_RUN_ENABLED == 1
    assert config.CI_RUN_TIMEOUT_SECONDS == 600
    assert config.CI_RUN_COOLDOWN_SECONDS == 60
    assert config.CI_RUN_DAILY_CAP == 10
    assert config.CI_RUN_TAIL_BYTES == 16 * 1024


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
    saved_prepare = ci_runner._prepare_tree
    real_git = ci_runner._git

    def raising_git(tree, *args):
        if args and args[0] == "gc":
            raise subprocess.TimeoutExpired(cmd="git gc", timeout=180)
        return real_git(tree, *args)

    ci_runner._prepare_tree = lambda: (str(stub.dir), "f" * 40)
    ci_runner._git = raising_git
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        assert result["ok"] is True, "gc failure must not fail the run"
    finally:
        ci_runner._prepare_tree = saved_prepare
        ci_runner._git = real_git
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
        "prepare": ci_runner._prepare_tree,
        "image": ci_runner._ensure_image,
        "argv": ci_runner._sandbox_argv,
        "docker": ci_runner._docker_available,
        "traversable": ci_runner._ensure_tree_traversable,
        "register": ci_runner._register_active,
    }
    ci_runner._prepare_tree = lambda: (str(tree), "refreshed1234")
    ci_runner._docker_available = lambda: True
    ci_runner._ensure_image = lambda tree_, rev: (
        holder.update(image_calls=holder["image_calls"] + 1, rev=rev) or "fake:tag"
    )
    ci_runner._sandbox_argv = lambda tree_, image_tag, script_rel: (
        [sys.executable, "-c", "print('ok')"],
        "agentland-ci-native",
    )
    ci_runner._ensure_tree_traversable = lambda tree_: None
    ci_runner._register_active = lambda *a, **k: None
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
        for name, fn in saved.items():
            setattr(ci_runner, name, fn)
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
        "prepare": ci_runner._prepare_tree,
        "image": ci_runner._ensure_image,
        "docker": ci_runner._docker_available,
    }
    ci_runner._prepare_tree = lambda: (str(tree), "refreshed1234")
    ci_runner._docker_available = lambda: True
    ci_runner._ensure_image = lambda tree_, rev: (
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
        "prepare": ci_runner._prepare_tree,
        "image": ci_runner._ensure_image,
        "docker": ci_runner._docker_available,
    }
    ci_runner._prepare_tree = lambda: (str(tree), "refreshed1234")
    ci_runner._docker_available = lambda: True
    ci_runner._ensure_image = lambda tree_, rev: (
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


def main():
    test_knob_defaults()
    test_unknown_checks_rejected()
    test_disabled_flag_refuses()
    test_busy_lock_refuses()
    test_success_run_parses_summary_and_logs_event()
    test_failing_run_lists_failed_files()
    test_parse_summary_db_benchmark_median_parsed()
    test_run_ci_static_summary_parsed()
    test_parse_static_summary_absent_when_not_static()
    test_timeout_kills_and_reports()
    test_child_env_is_sanitized()
    test_cooldown_gate()
    test_daily_cap_gate()
    test_output_tail_truncation()
    test_output_retained_bytes_capped_against_host_memory()
    test_multibyte_tail_is_byte_exact()
    test_env_keep_carries_docker_daemon_config()
    test_prune_filter_is_docker_glob_not_regex()
    test_drain_bounded_and_tail_contiguous()
    test_gc_sweep_survives_timeout_exception()
    test_suspended_citizen_cannot_run_ci()
    test_native_sandbox_routes_through_docker()
    test_native_host_fallback_when_knob_off()
    test_native_host_fallback_with_static_tools_is_parity()
    print("test_ci_runner: all ok")


if __name__ == "__main__":
    main()
