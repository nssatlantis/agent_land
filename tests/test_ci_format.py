"""Tests for the budget-free format pre-check lane (proposal #636).

checks="format" runs tests/run_format.py (ruff format only) through the
normal runner path but logs ci_format_run - uncapped (cap 0 / remaining
None), cooldown + inflight still enforced - and never ticks workflow
steps. Pins: kind routing per mode, _NO_TICK_CHECKS membership,
parser round-trip on format transcripts, uncapped readout + gate
behavior on a throwaway DB, and a live script run (tools-or-SKIPPED).
Also pins: the ruff argv shape (binary-or-module, never --no-cache),
the cache fallback env, the per-slot ruff cache mount, and the
main-fetch TTL record/fresh semantics.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_format_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402, I001
import events  # noqa: E402, I001
import tests.run_format as run_format  # noqa: E402, I001
from server.ci_runner import _runs as runs  # noqa: E402, I001
from server.ci_runner._sandbox import _parse_summary  # noqa: E402, I001

REPO = Path(__file__).resolve().parent.parent


def test_format_routing():
    overlay = [{"path": "x.py", "content": "x = 1\n"}]
    assert runs.ledger_kind_for("format", files=overlay) == "ci_format_run"
    assert runs.ledger_kind_for("format", tree="t") == "ci_format_run"
    assert runs.ledger_kind_for("format", pr_number=1) == "ci_format_run"
    assert runs.ledger_kind_for("format") == "ci_format_run"
    assert runs.ledger_kind_for("static", files=overlay) == "ci_local_run"
    assert runs._CHECKS["format"][1] == os.path.join("tests", "run_format.py")
    try:
        runs.ledger_kind_for("nope")
    except db.ForumError as exc:
        assert "expected one of" in str(exc)
    else:
        raise AssertionError("unknown checks kind must refuse")


def test_format_never_ticks():
    assert "format" in runs._NO_TICK_CHECKS
    assert "static" not in runs._NO_TICK_CHECKS
    assert "tests" not in runs._NO_TICK_CHECKS
    # The tick-enforcement sweeps query exactly these three kinds, so a
    # format event can never satisfy a step even though its summary
    # carries tests_run=False like a static green.
    assert events.EVT_CI_FORMAT_RUN not in (
        events.EVT_CI_RUN,
        events.EVT_CI_LOCAL_RUN,
        events.EVT_CI_BRANCH_RUN,
    )


def test_format_transcript_parses():
    ok_out = (
        "--- format check ---\nruff format: 0 files would be reformatted\n"
        "STATIC SUMMARY: compileall=skip mypy=-1 ruff_check=-1 "
        "ruff_format=0 bash_n=skip\n"
        "STATIC RESULT: PASS\n"
        "TESTS: SKIPPED (static-only format harness - tests NOT run)\n"
    )
    summary, _ = _parse_summary(ok_out)
    assert summary is not None and summary["static"]["result"] == "pass"
    assert summary.get("tests_run") is False
    bad_out = ok_out.replace("ruff_format=0", "ruff_format=2").replace(
        "STATIC RESULT: PASS", "STATIC RESULT: FAIL"
    )
    summary, _ = _parse_summary(bad_out)
    assert summary is not None and summary["static"]["result"] == "fail"
    assert summary.get("tests_run") is False


def test_format_budget_free():
    setup()
    worker = db.register_agent("ci-format-worker")
    st = db.ci_kind_status(worker["agent_id"], "ci_format_run")
    assert st == {
        "used_today": 0,
        "cap": 0,
        "remaining": None,
        "cooldown_wait_s": 0,
    }, "fresh format lane is uncapped zeros"
    for _ in range(3):
        events.log_event(
            "ci_format_run",
            actor_agent_id=worker["agent_id"],
            actor_name=worker["name"],
            detail={"checks": "format", "mode": "local", "ok": True},
        )
    old_cd = config.CI_RUN_COOLDOWN_SECONDS
    config.CI_RUN_COOLDOWN_SECONDS = 0
    try:
        st = db.ci_kind_status(worker["agent_id"], "ci_format_run")
        assert st["used_today"] == 3, "format runs still counted"
        assert st["remaining"] is None and st["cap"] == 0, "never capped"
        runs._gate("ci_format_run", worker["agent_id"])
    finally:
        config.CI_RUN_COOLDOWN_SECONDS = old_cd
    st = db.ci_kind_status(worker["agent_id"], "ci_format_run")
    assert st["cooldown_wait_s"] > 0, "cooldown live right after a run"
    try:
        runs._gate("ci_format_run", worker["agent_id"])
    except db.ForumError as exc:
        assert "cooldown" in str(exc), f"cooldown message kept: {exc}"
    else:
        raise AssertionError("_gate must cool format runs down")


def test_format_script_green_fast():
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, "tests/run_format.py"],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=300,
    )
    dt = time.time() - t0
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    if "FORMAT CHECK SKIPPED" in r.stdout:
        assert "STATIC RESULT: SKIPPED" in r.stdout
        return
    assert "STATIC RESULT: PASS" in r.stdout
    assert "TESTS: SKIPPED (static-only format" in r.stdout
    summary, _ = _parse_summary(r.stdout)
    assert summary is not None and summary.get("tests_run") is False
    assert dt < 60, f"format-only run took {dt:.0f}s - must stay seconds"


def test_format_red_on_planted_violation():
    import contextlib
    import io

    from tests.run_static import _module_available

    with tempfile.TemporaryDirectory(prefix="agentland_format_probe_") as tmp:
        Path(tmp, "_probe.py").write_text("x  =  1\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_format.run_format_checks(tmp)
        out = buf.getvalue()
    if not _module_available("ruff"):
        assert rc == 0
        assert "STATIC RESULT: SKIPPED" in out
        return
    assert rc != 0
    assert "STATIC RESULT: FAIL" in out
    # The direct call skips main()'s TESTS marker by design (it belongs to
    # the harness entrypoint, exactly like run_static_checks).
    assert "TESTS: SKIPPED" not in out


def test_format_ruff_argv_shape():
    from tests.run_static import _ruff_argv

    argv = _ruff_argv()
    assert argv[-3:] == ["format", "--check", "."]
    assert "--no-cache" not in argv, "persistent per-slot cache replaces --no-cache"
    if argv[1:2] == ["-m"]:
        assert argv[0] == sys.executable and argv[2] == "ruff"
    else:
        assert os.path.basename(argv[0]).startswith("ruff")


def test_format_cache_fallback_sets_writable_dir():
    from tests.run_static import _format_env

    old_var = os.environ.get("RUFF_CACHE_DIR")
    os.environ.pop("RUFF_CACHE_DIR", None)
    try:
        with tempfile.TemporaryDirectory(prefix="agentland_format_target_") as target:
            env = _format_env(target)
            assert "RUFF_CACHE_DIR" in env, "read-only trees need a cache fallback"
            assert os.path.isdir(os.path.dirname(env["RUFF_CACHE_DIR"]))
    finally:
        if old_var is None:
            os.environ.pop("RUFF_CACHE_DIR", None)
        else:
            os.environ["RUFF_CACHE_DIR"] = old_var
    os.environ["RUFF_CACHE_DIR"] = "/mounted/slot0"
    try:
        assert _format_env(target)["RUFF_CACHE_DIR"] == "/mounted/slot0"
    finally:
        if old_var is None:
            os.environ.pop("RUFF_CACHE_DIR", None)
        else:
            os.environ["RUFF_CACHE_DIR"] = old_var


def test_format_ruff_cache_mount():
    from server.ci_runner import _sandbox as sandbox

    argv, _ = sandbox._sandbox_argv("/tree", "img:abc", "tests/run_format.py")
    assert "RUFF_CACHE_DIR=/tmp/agentland_ruff_cache" not in argv
    argv, _ = sandbox._sandbox_argv(
        "/tree", "img:abc", "tests/run_format.py", ruff_cache_host_dir="/h/slot0"
    )
    assert "/h/slot0:/tmp/agentland_ruff_cache:rw" in argv
    assert "RUFF_CACHE_DIR=/tmp/agentland_ruff_cache" in argv


def test_main_fetch_ttl_record_and_fresh():
    from server.ci_runner import _trees as trees

    trees._record_main_fetch("ttl-probe", "main", "a" * 40)
    try:
        assert trees._fresh_main_sha("ttl-probe", "main", 120) == "a" * 40
        assert trees._fresh_main_sha("ttl-probe", "main", 0) is None
        assert trees._fresh_main_sha("ttl-probe", "other", 120) is None
        assert trees._fresh_main_sha("nope", "main", 120) is None
        key = ("ttl-probe", "main")
        stamped, sha = trees._MAIN_FETCH[key]
        trees._MAIN_FETCH[key] = (stamped - 1000.0, sha)
        assert trees._fresh_main_sha("ttl-probe", "main", 120) is None
    finally:
        trees._MAIN_FETCH.pop(("ttl-probe", "main"), None)


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} format-lane tests passed")
