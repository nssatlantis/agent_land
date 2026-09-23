"""Smoke tests for the CI farm runner (proposal #667, PR 1).

Import safety, token check, checks map, the parity pin (the runner
delegates to the host's exact modules), and the HTTP surface (health
+ 401 auth). No docker, no network beyond 127.0.0.1.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_farm_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import ci_farm.runner as runner  # noqa: E402
import server.ci_runner._runs as runs_mod  # noqa: E402
import server.ci_runner._sandbox as sandbox_mod  # noqa: E402
import server.ci_runner._slots as slots_mod  # noqa: E402
import server.ci_runner._trees as trees_mod  # noqa: E402


def test_runner_version_and_checks_map():
    assert isinstance(runner.RUNNER_VERSION, int)
    assert runner._CHECKS_TO_SCRIPT["tests"] == "tests/run_all.py"
    assert runner._CHECKS_TO_SCRIPT["static"] == "tests/run_static.py"
    assert runner._CHECKS_TO_SCRIPT["format"] == "tests/run_format.py"
    assert runner._CHECKS_TO_SCRIPT["db_benchmark"] == "tests/test_benchmark.py"
    assert runner._CHECKS_TO_SCRIPT["db_bench"] == "tests/test_benchmark.py"


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


def test_http_health_and_auth():
    farm = runner.FarmRunner("127.0.0.1", 0, token="farm-test-token")
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
