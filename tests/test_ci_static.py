"""Tests for the static-only CI harness (proposal #503, checks="static").

tests/run_static.py runs exactly the static half (compileall, mypy, ruff
check, ruff format, bash -n) without the suite; the sandbox parser turns
its TESTS-skipped marker into summary.tests_run=False, and the workflow
gate accepts that for the `lint` tick while `test`/`not-gutted` still
demand tests actually ran. Pins: green+fast on a clean tree, red on a
planted violation, run_ci parity (no marker, same static source), parser
round-trip, the reported e2e lane, and the pure gate predicate matrix."""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests.run_static  # noqa: F401,E402
from db._workflow import _ci_event_covers  # noqa: E402
from server.ci_runner._sandbox import _parse_summary  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


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


def _tools_present():
    return tests.run_static._module_available(
        "mypy"
    ) and tests.run_static._module_available("ruff")


def test_static_green_fast_with_markers():
    r, dt = _run_static()
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    if "STATIC CHECKS SKIPPED" in r.stdout:
        # No mypy/ruff on this host (e.g. GitHub's test job - only its
        # static job carries the tools): the documented degraded path.
        assert "STATIC RESULT: SKIPPED" in r.stdout
        return
    assert "STATIC RESULT: PASS" in r.stdout
    assert "TESTS: SKIPPED (static-only" in r.stdout
    summary, _ = _parse_summary(r.stdout)
    assert summary is not None and summary["static"]["result"] == "pass"
    assert summary.get("tests_run") is False
    assert dt < 300, f"static-only run took {dt:.0f}s - must stay far below a suite run"


def test_static_toolless_degrade_pinned():
    # The tool-less tolerance arms above never execute where tools exist;
    # pin them by monkeypatch instead of by host state.
    import contextlib
    import io
    import tempfile

    real = tests.run_static._module_available
    tests.run_static._module_available = lambda module: False
    try:
        with tempfile.TemporaryDirectory(prefix="agentland_static_notools_") as tmp:
            Path(tmp, "_probe.py").write_text("x=1\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = tests.run_static.run_static_checks(tmp)
            out = buf.getvalue()
    finally:
        tests.run_static._module_available = real
    assert rc == 0
    assert "STATIC RESULT: SKIPPED" in out
    assert "TESTS: SKIPPED" not in out


def test_static_red_on_planted_violation():
    # In-process against a throwaway dir: the suite must never mutate the
    # source tree (the CI sandbox mounts it read-only - a tree-writing red
    # test greens locally and reds in CI, proven live by ev45871).
    import contextlib
    import io
    import tempfile

    with tempfile.TemporaryDirectory(prefix="agentland_static_probe_") as tmp:
        Path(tmp, "_probe.py").write_text("x=1\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tests.run_static.run_static_checks(tmp)
        out = buf.getvalue()
    if not _tools_present():
        # Same degraded path as above: without the tools there is nothing
        # to fail - the skip itself is the pinned behavior.
        assert rc == 0
        assert "STATIC RESULT: SKIPPED" in out
        return
    assert rc != 0
    assert "STATIC RESULT: FAIL" in out
    # The direct call skips main()'s TESTS marker by design (it belongs to
    # the harness entrypoint, never the shared function run_ci imports);
    # the green subprocess pin above covers marker + tests_run end to end.
    summary, _ = _parse_summary(out)
    assert summary is not None and summary["static"]["result"] == "fail"


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


def test_parser_e2e_run_reported_on_every_summary():
    # #B118: this harness never runs the four test_e2e_0*.py suites (run_all
    # _SKIPs them and no harness invokes run_e2e.py), so every summary it
    # produces describes a run that did not cover them. The key is
    # present-and-False on each summary - known, did not run - and absent
    # when there is no summary at all, which is unknown rather than "did
    # not run": two states, no third reading. Report-only by construction;
    # _ci_event_covers reads tests_run alone, which is what makes adding a
    # key to the summary safe.
    # Deliberately asserts nothing about tests_run. Each flag has exactly
    # one owning pin, so removing either one turns exactly one test red
    # rather than two - the direction that says which half broke.
    # Shape 1: a tests-only run with no static half at all.
    summary, _ = _parse_summary("test_misc.py: ok (1.00s)\nall 3 test files passed\n")
    assert summary is not None
    assert summary["e2e_run"] is False
    # Shape 2: a red tests run, so the key rides a failing summary too.
    summary, _ = _parse_summary("test_misc.py: FAIL\nFAILED: 1 of 3 test files\n")
    assert summary is not None
    assert summary["e2e_run"] is False
    # Shape 3: static-only, the one shape that already carries a flag.
    summary, _ = _parse_summary(
        "--- static checks ---\ncompileall: ok\n"
        "STATIC SUMMARY: compileall=ok mypy=0 ruff_check=0 ruff_format=0 bash_n=skip\n"
        "STATIC RESULT: PASS\n"
        "TESTS: SKIPPED (static-only harness - tests NOT run)\n"
    )
    assert summary is not None
    assert summary["e2e_run"] is False
    # Shape 4: unparsed output (and checks="format") - no summary, so no
    # claim either way.
    summary, _ = _parse_summary("nothing this parser recognises\n")
    assert summary is None


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


def test_bash_missing_is_incomplete_not_pass():
    # A host without bash skips the shell-check arm: the run must refuse
    # PASS (fail-closed INCOMPLETE), while skip-with-nothing-to-check
    # never takes the incomplete arm. No-bash simulated by stubbing
    # shutil.which, the same monkeypatch idiom as
    # test_static_toolless_degrade_pinned.
    import contextlib
    import io
    import shutil
    import tempfile

    real_which = shutil.which
    shutil.which = lambda cmd: None if cmd == "bash" else real_which(cmd)
    try:
        with tempfile.TemporaryDirectory(prefix="agentland_static_nobash_") as tmp:
            Path(tmp, "_probe.py").write_text("x = 1\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = tests.run_static.run_static_checks(tmp)
            out = buf.getvalue()
    finally:
        shutil.which = real_which
    if not _tools_present():
        # Same degraded path as the toolless pin: without mypy/ruff the
        # harness never reaches the bash arm.
        assert rc == 0
        assert "STATIC RESULT: SKIPPED" in out
        return
    assert rc != 0, out[-2000:]
    assert "STATIC RESULT: INCOMPLETE" in out
    assert "STATIC RESULT: PASS" not in out
    summary, _ = _parse_summary(out)
    assert summary is not None and summary["static"]["result"] == "incomplete"
    assert summary["static"]["bash_n"] == "skip"
    # Companion: bash present but no scripts never takes the incomplete
    # arm either. (No PASS assertion: mypy takes its file scope from
    # pyproject [tool.mypy], so a bare tmp dir is a mypy usage-error and
    # the overall verdict is FAIL for reasons unrelated to this change -
    # the property under test is the absence of INCOMPLETE plus the
    # skip marker, not the overall verdict.)
    with tempfile.TemporaryDirectory(prefix="agentland_static_noscripts_") as tmp:
        Path(tmp, "_probe.py").write_text("x = 1\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tests.run_static.run_static_checks(tmp)
        out = buf.getvalue()
    assert "STATIC RESULT: INCOMPLETE" not in out
    summary, _ = _parse_summary(out)
    assert summary is not None and summary["static"]["bash_n"] == "skip"


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} static-harness tests passed")
