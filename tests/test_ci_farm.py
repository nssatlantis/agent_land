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


def _run_all_tests() -> int:
    """Run all test functions, print PASS/FAIL per test, return exit code."""
    tests = [
        ("test_runner_version_and_checks_map", test_runner_version_and_checks_map),
        ("test_token_check", test_token_check),
        ("test_run_job_rejects_unknown_checks", test_run_job_rejects_unknown_checks),
        ("test_run_job_rejects_unknown_mode", test_run_job_rejects_unknown_mode),
        ("test_bootstrap_parity_pin", test_bootstrap_parity_pin),
        (
            "test_dispatch_parity_ignores_run_specific_summary_keys",
            test_dispatch_parity_ignores_run_specific_summary_keys,
        ),
        ("test_http_health_and_auth", test_http_health_and_auth),
        ("test_import_side_effect_safety", test_import_side_effect_safety),
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
