"""Combined "tests + static" CI harness (server-side CI runner).

Runs the test suite (tests/run_all.py) followed by the static checks
(compileall, mypy, ruff check, ruff format --check, bash -n).  It is what
the server's repo_ci_run(checks="tests") executes, so a green run covers
run_all + static in one go - no separate static rehearsal needed.  It is
NOT GitHub test-job parity: run_all.py skips the four test_e2e_0*.py
suites that .github/workflows/ci.yml runs in its `test` job (run those
via tests/run_e2e.py; the GitHub verdict itself is repo_pr_checks).

The static half lives in tests/run_static.py (imported here - one source,
never two copies): the static-only harness repo_ci_run(checks="static")
runs it without the suite. This combined runner must never print that
harness's TESTS-skipped marker - its runs execute the suite.

Exit code is non-zero if the tests OR any applicable static check fails.

The static half needs mypy/ruff: the sandbox image bakes them from
requirements-dev.txt, so branch/rehearsal runs always include it.  Native
(host-interpreter) runs skip it gracefully when the tools are absent and
still report the tests.  The parseable markers below (`STATIC SUMMARY:`,
`STATIC RESULT:`) are consumed by server/ci_runner._parse_static_summary.

Run directly with: python tests/run_ci.py [--no-session] [--workers=N] [selector ...]

Bare selectors forward to run_all.py (substring on basenames).
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import subprocess  # noqa: E402

from run_static import run_static_checks as _run_static  # noqa: E402


def _run(args, cwd, env=None):
    """Minimal runner for the tests half (the static half's richer helper
    stays in run_static.py - each half owns its own)."""
    return subprocess.run(args, cwd=cwd, env=env)


def main() -> int:
    # Forward the suite-scheduling flags (the session default lives in
    # run_all.py; these only override it on a manual run_ci call - the
    # server always invokes the bare default) plus any bare selectors, so
    # `run_ci.py guilds_engine` targets like `run_all.py` does instead of
    # silently running the full suite.
    passthrough = [
        a
        for a in sys.argv[1:]
        if (
            a in ("--no-session", "--durations")
            or a.startswith("--workers=")
            or not a.startswith("-")
        )
    ]
    tests = _run(
        [sys.executable, os.path.join(REPO, "tests", "run_all.py"), *passthrough],
        REPO,
    )
    tests_ok = tests.returncode == 0
    static_fail = _run_static()
    return 1 if (not tests_ok or static_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
