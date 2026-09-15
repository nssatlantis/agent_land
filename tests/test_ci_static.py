"""Tests for the static-only CI harness (proposal #503, checks="static").

tests/run_static.py runs exactly the static half (compileall, mypy, ruff
check, ruff format, bash -n) without the suite; the sandbox parser turns
its TESTS-skipped marker into summary.tests_run=False, and the workflow
gate accepts that for the `lint` tick while `test`/`not-gutted` still
demand tests actually ran. Pins: green+fast on a clean tree, red on a
planted violation, run_ci parity (no marker, same static source), parser
round-trip, and the pure gate predicate matrix."""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests.run_static  # noqa: F401,E402
from db._workflow import _ci_event_covers  # noqa: E402
from server.ci_runner._sandbox import _parse_summary  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

_PLANTED = REPO / "tests" / "_static_probe_tmp.py"
_PLANTED_TEXT = "x=1\n"


def _run_static():
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, "tests/run_static.py"],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=600,
    )
    return r, time.time() - t0


def test_static_green_fast_with_markers():
    r, dt = _run_static()
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert "STATIC RESULT: PASS" in r.stdout
    assert "TESTS: SKIPPED (static-only" in r.stdout
    assert dt < 300, f"static-only run took {dt:.0f}s - must stay far below a suite run"


def test_static_red_on_planted_violation():
    _PLANTED.write_text(_PLANTED_TEXT)
    try:
        r, _ = _run_static()
    finally:
        _PLANTED.unlink(missing_ok=True)
    assert r.returncode != 0
    assert "STATIC RESULT: FAIL" in r.stdout
    summary, _ = _parse_summary(r.stdout)
    assert summary is not None and summary["static"]["result"] == "fail"
    assert summary.get("tests_run") is False


def test_run_ci_delegates_without_marker():
    # One static source: run_ci imports run_static's function and carries
    # none of the static bodies itself (no compileall/ruff/mypy literals),
    # and the combined runner never prints the static-only marker (its
    # runs execute the suite - the marker would lie about them). Identity
    # comparison is deliberately avoided: script-style (`python
    # tests/run_ci.py`) and package-style (`import tests.run_ci`) imports
    # instantiate the module twice, so `is` would be vacuous either way.
    src = (REPO / "tests" / "run_ci.py").read_text()
    assert "from run_static import run_static_checks" in src
    for literal in (
        '"compileall", "-q"',
        "_count_found",
        "mypy_errors =",
        "ruff_format =",
        "bash_n =",
    ):
        assert literal not in src, f"duplicated static body in run_ci.py: {literal}"
    assert "TESTS: SKIPPED" not in src


def test_parser_tests_run_flag():
    static_only = (
        "--- static checks ---\ncompileall: ok\n"
        "STATIC SUMMARY: compileall=ok mypy=0 ruff_check=0 ruff_format=0 bash_n=skip\n"
        "STATIC RESULT: PASS\n"
        "TESTS: SKIPPED (static-only harness - tests NOT run)\n"
    )
    summary, _ = _parse_summary(static_only)
    assert summary is not None
    assert summary["static"]["result"] == "pass"
    assert summary.get("tests_run") is False
    combined = (
        "test_misc.py: ok (1.00s)\nFAILED: 0 of 3 test files\n"
        "STATIC SUMMARY: compileall=ok mypy=0 ruff_check=0 ruff_format=0 bash_n=skip\n"
        "STATIC RESULT: PASS\n"
    )
    summary, _ = _parse_summary(combined)
    # Full runs leave the flag absent (every pre-change summary shape stays
    # byte-identical); the gate treats absent as tests-ran.
    assert summary is not None and "tests_run" not in summary
    bare = "STATIC RESULT: PASS\n"
    summary, _ = _parse_summary(bare)
    assert summary is None or "tests_run" not in summary


def test_gate_predicate_matrix():
    def detail(static="pass", **kw):
        d = {
            "ok": True,
            "timed_out": False,
            "exit_code": 0,
            "summary": {"static": {"result": static}},
        }
        d.update(kw)
        return d

    full = detail()
    full["summary"]["tests_run"] = True
    legacy_absent = detail()
    static_only = detail()
    static_only["summary"]["tests_run"] = False
    # Full and legacy (marker-less, flag absent) greens cover every step.
    for step in ("lint", "test", "not-gutted"):
        assert _ci_event_covers(full, step) is True
        assert _ci_event_covers(legacy_absent, step) is True
    # Static-only green covers lint alone.
    assert _ci_event_covers(static_only, "lint") is True
    assert _ci_event_covers(static_only, "test") is False
    assert _ci_event_covers(static_only, "not-gutted") is False
    # Anything else fails every step.
    for bad in (
        detail(static="skipped"),
        {**detail(), "ok": False},
        {**detail(), "timed_out": True},
        {**detail(), "exit_code": 1},
        {**detail(), "host_fallback_static_skipped": True},
        {},
        None,
    ):
        for step in ("lint", "test", "not-gutted"):
            assert _ci_event_covers(bad, step) is False, (bad, step)
    # Legacy parity, pinned so it never drifts silently: the shipped gate
    # never inspected result==pass (the harness exit code enforces it - a
    # real static FAIL exits nonzero), so a contradictory hand-made detail
    # still covers lint exactly like the old inline predicate did.
    odd = detail(static="fail")
    assert _ci_event_covers(odd, "lint") is True
    assert _ci_event_covers(odd, "test") is True


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} static-harness tests passed")
