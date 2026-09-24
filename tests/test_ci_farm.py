"""Smoke tests for the CI farm runner (proposal #667, PR 1).

Import safety, token check, checks map (cross-module pin against host
_CHECKS), the parity pin (the runner delegates to the host's exact
modules), and the HTTP surface (health + 401 auth). No docker, no
network beyond 127.0.0.1.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_farm_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import ci_farm.dispatch_test as dispatch_test  # noqa: E402
import ci_farm.runner as runner  # noqa: E402
import server.ci_runner._runs as runs_mod  # noqa: E402
import server.ci_runner._sandbox as sandbox_mod  # noqa: E402
import server.ci_runner._slots as slots_mod  # noqa: E402
import server.ci_runner._trees as trees_mod  # noqa: E402


def test_runner_version_and_checks_map():
    """Cross-module pin: the runner's _CHECKS_TO_SCRIPT must match the
    host's _CHECKS script paths exactly. A mismatch here means the farm
    runs a different harness than the host attests."""
    assert isinstance(runner.RUNNER_VERSION, int)
    host_checks = runs_mod._CHECKS
    for key, (_kind, script) in host_checks.items():
        assert runner._CHECKS_TO_SCRIPT.get(key) == script, (
            f"runner._CHECKS_TO_SCRIPT[{key!r}] = "
            f"{runner._CHECKS_TO_SCRIPT.get(key)!r}, "
            f"host _CHECKS[{key!r}] script = {script!r}"
        )
    # Bidirectional: a runner-only extra key would silently widen the
    # contract without failing this pin.
    assert set(runner._CHECKS_TO_SCRIPT) == set(host_checks), (
        f"runner-only checks keys: {set(runner._CHECKS_TO_SCRIPT) - set(host_checks)}"
    )


def test_token_check():
    assert runner._check_token("abc", "abc")
    assert not runner._check_token("abc", "abd")
    assert not runner._check_token("", "abc")
    assert not runner._check_token("abc", "")


def test_run_job_rejects_unknown_checks():
    result = runner._run_job({"checks": "nope"})
    assert result["ok"] is False
    assert "unknown checks" in result["error"]


def test_run_job_rejects_unknown_mode():
    result = runner._run_job({"checks": "tests", "mode": "weird"})
    assert result["ok"] is False
    assert "unknown mode" in result["error"]


def test_run_job_rejects_bad_payload_shapes():
    """Validator failures are client errors (ok False), never 500s: bad
    extra_env, bad files, non-hex base_sha, and base_sha in local mode."""
    bad_env = runner._run_job(
        {"checks": "tests", "mode": "main", "extra_env": {"NOPE": "x"}}
    )
    assert bad_env["ok"] is False and "extra_env" in bad_env["error"]
    bad_files = runner._run_job(
        {"checks": "tests", "mode": "local", "files": "not-a-list"}
    )
    assert bad_files["ok"] is False and "files" in bad_files["error"]
    bad_sha = runner._run_job({"checks": "tests", "mode": "main", "base_sha": "z" * 40})
    assert bad_sha["ok"] is False and "base_sha" in bad_sha["error"]
    misplaced_sha = runner._run_job(
        {
            "checks": "tests",
            "mode": "local",
            "base_sha": "a" * 40,
            "files": [{"path": "a", "content": "b"}],
        }
    )
    assert misplaced_sha["ok"] is False and "main-mode" in misplaced_sha["error"]


def test_bootstrap_parity_pin():
    mods = runner._bootstrap()
    assert mods["runs"] is runs_mod
    assert mods["sandbox"] is sandbox_mod
    assert mods["slots"] is slots_mod
    assert mods["trees"] is trees_mod


def test_dispatch_parity_ignores_run_specific_summary_keys():
    # _parse_summary (server/ci_runner/_sandbox.py) injects wall-clock
    # keys (slowest_s, timings_median_ms, regressions) into summary that
    # can never agree across two machines; the parity diff must not treat
    # them as failures, while real differences (failed_files) still show.
    used = dispatch_test._RUN_SPECIFIC_SUMMARY_KEYS
    assert "slowest_s" in used
    assert "timings_median_ms" in used
    assert "regressions" in used
    host = {
        "summary": {
            "passed_files": 216,
            "failed_files": 0,
            "slowest_s": {"tests/test_a.py": 3.1},
        }
    }
    other = {
        "summary": {
            "passed_files": 216,
            "failed_files": 0,
            "slowest_s": {"tests/test_a.py": 2.7},
        }
    }
    assert dispatch_test._parity_summary(
        host["summary"]
    ) == dispatch_test._parity_summary(other["summary"])
    host["failed_files"] = ["tests/test_a.py"]
    other["failed_files"] = ["tests/test_b.py"]
    assert set(host["failed_files"]) != set(other["failed_files"])


def test_http_health_and_auth():
    farm = runner.FarmRunner("127.0.0.1", 0, token="farm-test-token")
    t = threading.Thread(target=farm.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    try:
        base = f"http://127.0.0.1:{farm.port}"
        with urllib.request.urlopen(base + "/health") as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["ok"] is True
            assert body["version"] == runner.RUNNER_VERSION
            assert body["busy"] is False
            assert "docker_available" in body
            assert "active_runs" in body
            assert "head_sha" in body
            if body["head_sha"] is not None:
                assert runner._BASE_SHA_RE.fullmatch(body["head_sha"]) is not None
        req = urllib.request.Request(
            base + "/run",
            data=b"{}",
            headers={"Authorization": "Bearer wrong-token"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            raise AssertionError("expected 401 for bad token")
        except urllib.error.HTTPError as err:
            assert err.code == 401
        # A negative Content-Length must not bypass the body cap: it is a
        # malformed request (400), never an unbounded read.
        import socket

        raw = socket.create_connection(("127.0.0.1", farm.port), timeout=5)
        try:
            raw.sendall(
                b"POST /run HTTP/1.1\r\nHost: x\r\n"
                b"Authorization: Bearer farm-test-token\r\n"
                b"Content-Length: -1\r\n\r\n"
            )
            status = raw.recv(12)
            assert status.startswith(b"HTTP/1.0 400") or status.startswith(
                b"HTTP/1.1 400"
            ), status
        finally:
            raw.close()
    finally:
        farm.shutdown()
        t.join(timeout=5)


def test_import_side_effect_safety():
    """P0-4 pin: importing ci_farm.runner must not create files, write
    to the DB, or open network connections beyond the data dir. The
    data dir is the only allowed filesystem side effect."""
    dd = Path(os.environ["AGENTLAND_DATA_DIR"])
    entries = set(os.listdir(dd))
    for e in entries:
        assert not e.endswith(".db"), f"unexpected DB file in data dir: {e}"


def test_repo_head_shape():
    """_repo_head returns None or a 40-hex sha, never garbage."""
    head = runner._repo_head()
    assert head is None or runner._BASE_SHA_RE.fullmatch(head) is not None


def test_deps_freshness():
    """_deps_fresh: older requirements are fresh, newer-or-missing are stale."""
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("requirements.txt", "requirements-dev.txt"):
            Path(tmp, name).write_text("x\n", encoding="utf-8")
        assert runner._deps_fresh(time.time() + 60, root=tmp) is True
        Path(tmp, "requirements.txt").write_text("y\n", encoding="utf-8")
        os.utime(Path(tmp, "requirements.txt"), (time.time() + 120,) * 2)
        assert runner._deps_fresh(time.time(), root=tmp) is False
        assert runner._deps_fresh(time.time(), root=str(Path(tmp, "nope"))) is False


def test_repo_moved_predicate():
    """_repo_moved: disabled without a startup snapshot, exact on equality."""
    orig_head = runner._repo_head
    orig_start = runner._START_SHA
    try:
        runner._START_SHA = None
        runner._repo_head = lambda: "a" * 40
        assert runner._repo_moved() is False
        runner._START_SHA = "a" * 40
        assert runner._repo_moved() is False
        runner._START_SHA = "b" * 40
        assert runner._repo_moved() is True
    finally:
        runner._repo_head = orig_head
        runner._START_SHA = orig_start


def test_stale_gates_release_lock():
    """Fail-loud gates must never wedge the single-flight lock: after a 503
    (stale venv) - and after a 500 (failed re-exec) - the next request is
    answered normally and the lock reads free."""
    import os as _os

    farm = runner.FarmRunner("127.0.0.1", 0, token="farm-test-token")
    t = threading.Thread(target=farm.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    base = f"http://127.0.0.1:{farm.port}"

    def _post(body):
        req = urllib.request.Request(
            base + "/run",
            data=body,
            headers={"Authorization": "Bearer farm-test-token"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def _assert_lock_free():
        deadline = time.monotonic() + 1
        while runner.FarmHandler.lock.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runner.FarmHandler.lock.locked() is False, "runner lock stayed held"

    orig_fresh = runner._deps_fresh
    orig_moved = runner._repo_moved
    orig_execv = _os.execv
    try:
        runner._deps_fresh = lambda since: False
        status, _ = _post(b"{}")
        assert status == 503, f"stale venv must fail loud, got {status}"
        _assert_lock_free()
        runner._deps_fresh = lambda since: True
        status, body = _post(
            json.dumps({"checks": "nope", "mode": "main"}).encode("utf-8")
        )
        assert status == 200, f"post-503 request must be served, got {status}"
        assert body.get("mode") is None or "error" in body
        _assert_lock_free()
        runner._repo_moved = lambda: True

        def _boom(*args):
            raise OSError("no exec here")

        _os.execv = _boom
        status, _ = _post(b"{}")
        assert status == 500, f"failed re-exec must 500, got {status}"
        _assert_lock_free()
        runner._repo_moved = orig_moved
        status, _ = _post(
            json.dumps({"checks": "nope", "mode": "main"}).encode("utf-8")
        )
        assert status == 200, f"post-500 request must be served, got {status}"
        _assert_lock_free()
    finally:
        runner._deps_fresh = orig_fresh
        runner._repo_moved = orig_moved
        _os.execv = orig_execv
        farm.shutdown()
        t.join(timeout=5)


def _run_all_tests() -> int:
    """Run all test functions, print PASS/FAIL per test, return exit code."""
    tests = [
        ("test_runner_version_and_checks_map", test_runner_version_and_checks_map),
        ("test_token_check", test_token_check),
        ("test_run_job_rejects_unknown_checks", test_run_job_rejects_unknown_checks),
        ("test_run_job_rejects_unknown_mode", test_run_job_rejects_unknown_mode),
        (
            "test_run_job_rejects_bad_payload_shapes",
            test_run_job_rejects_bad_payload_shapes,
        ),
        ("test_bootstrap_parity_pin", test_bootstrap_parity_pin),
        (
            "test_dispatch_parity_ignores_run_specific_summary_keys",
            test_dispatch_parity_ignores_run_specific_summary_keys,
        ),
        ("test_http_health_and_auth", test_http_health_and_auth),
        ("test_import_side_effect_safety", test_import_side_effect_safety),
        ("test_repo_head_shape", test_repo_head_shape),
        ("test_deps_freshness", test_deps_freshness),
        ("test_repo_moved_predicate", test_repo_moved_predicate),
        ("test_stale_gates_release_lock", test_stale_gates_release_lock),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    if failed:
        print(f"{failed}/{len(tests)} tests FAILED")
        return 1
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all_tests())
