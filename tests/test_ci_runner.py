"""Tests for the server-side CI runner (repo_ci_run): guardrail gating,
main-only tree refresh seams, sanitized child environments, timeout kill,
output tailing, and the events-ledger audit trail."""
import json
import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_runner_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config  # noqa: E402
import db  # noqa: E402
import events  # noqa: E402
import server.ci_runner as ci_runner  # noqa: E402

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
    """Patches _prepare_tree to a throwaway dir holding a stub suite script."""

    def __init__(self, kind: str, body: str):
        self.dir = Path(tempfile.mkdtemp(prefix="agentland_ci_stub_"))
        rel = ci_runner._CHECKS[kind][1]
        script = self.dir / rel
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        self._orig = ci_runner._prepare_tree
        ci_runner._prepare_tree = lambda: (str(self.dir), "deadbeefcafe")

    def cleanup(self):
        ci_runner._prepare_tree = self._orig


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
    stub = _StubTree("tests", """
        import sys
        print("  test_a.py: ok")
        print("all 1 test files passed")
        sys.exit(0)
    """)
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
    stub = _StubTree("tests", """
        import sys
        print("FAILED: tests/test_bad.py")
        print("some traceback noise")
        print("FAILED: 1 of 5 test files")
        sys.exit(1)
    """)
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        assert result["ok"] is False and result["exit_code"] == 1
        assert result["summary"] == {"passed_files": 4, "failed_files": 1}
        assert result["failed_files"] == ["tests/test_bad.py"]
    finally:
        stub.cleanup()


def test_timeout_kills_and_reports():
    stub = _StubTree("tests", """
        import time
        print("starting", flush=True)
        time.sleep(60)
    """)
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
    stub = _StubTree("tests", """
        import json, os, sys
        leaky = [k for k in os.environ
                 if "TOKEN" in k.upper() or "SECRET" in k.upper()
                 or "GITHUB" in k.upper() or k.upper().startswith("FORUM")]
        print(json.dumps({"leaky": sorted(leaky),
                          "data_dir": os.environ.get("AGENTLAND_DATA_DIR")}))
        sys.exit(0)
    """)
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        payload = json.loads(result["output_tail"].strip().splitlines()[-1])
        assert payload["leaky"] == [], f"secrets leaked: {payload['leaky']}"
        assert "supersecret" not in result["output_tail"]
        assert payload["data_dir"] and "agentland_ci_run_" in payload["data_dir"]
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        stub.cleanup()


def test_cooldown_gate():
    events.log_event(events.EVT_CI_RUN, actor_agent_id=_ACTOR,
                     detail={"checks": "tests"})
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
    stub = _StubTree("benchmarks", """
        import sys
        print("x" * 50000)
        sys.exit(0)
    """)
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
    stub = _StubTree("tests", """
        import sys
        for _ in range(200):
            print("y" * 10000, flush=True)
        print("all 1 test files passed")
        sys.exit(0)
    """)
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
    stub = _StubTree("tests", """
        import sys
        print("héllo-🎉" * 5000)
        print("all 1 test files passed")
        sys.exit(0)
    """)
    _shadow("CI_RUN_TAIL_BYTES", 64)
    try:
        result = ci_runner.run_checks(_uid(), "t", "tests")
        assert result["output_truncated"] is True
        assert len(result["output_tail"].encode("utf-8")) <= 64 + 4, \
            "tail exceeded its byte budget"
    finally:
        _restore()
        stub.cleanup()


def main():
    test_knob_defaults()
    test_unknown_checks_rejected()
    test_disabled_flag_refuses()
    test_busy_lock_refuses()
    test_success_run_parses_summary_and_logs_event()
    test_failing_run_lists_failed_files()
    test_timeout_kills_and_reports()
    test_child_env_is_sanitized()
    test_cooldown_gate()
    test_daily_cap_gate()
    test_output_tail_truncation()
    test_output_retained_bytes_capped_against_host_memory()
    test_multibyte_tail_is_byte_exact()
    print("test_ci_runner: all ok")


if __name__ == "__main__":
    main()
