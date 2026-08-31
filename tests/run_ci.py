"""Combined "tests + static" CI harness (server-side CI runner).

Runs the repository's full CI surface the way GitHub's `test` + `static`
jobs do: the test suite (tests/run_all.py) followed by the static checks
(compileall, mypy, ruff check, ruff format --check, bash -n).  It is what
the server's repo_ci_run(checks="tests") executes, so a green run covers
the same ground GitHub CI does - no separate static rehearsal needed.

Exit code is non-zero if the tests OR any applicable static check fails.

The static half needs mypy/ruff: the sandbox image bakes them from
requirements-dev.txt, so branch/rehearsal runs always include it.  Native
(host-interpreter) runs skip it gracefully when the tools are absent and
still report the tests.  The parseable markers below (`STATIC SUMMARY:`,
`STATIC RESULT:`) are consumed by server/ci_runner._parse_static_summary.

Run directly with: python tests/run_ci.py
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _module_available(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


def _run(args, cwd, env=None, capture=False):
    """Run *args* inheriting stdout/stderr (so output reaches the caller's
    captured pipe) or capturing it, and return the CompletedProcess either
    way."""
    if capture:
        return subprocess.run(args, cwd=cwd, text=True, env=env, capture_output=True)
    return subprocess.run(args, cwd=cwd, env=env)


def _dump(result, limit=4000) -> None:
    """Print a possibly-trimmed trace of a failed check's raw output."""
    out = (result.stdout or "") + (result.stderr or "")
    if len(out) > limit:
        out = out[-limit:]
    print(out)


def _count_found(result) -> int:
    m = re.search(r"Found (\d+) error", result.stdout + result.stderr)
    return int(m.group(1)) if m else 0


def _count_formatted(result) -> int:
    m = re.search(r"(\d+) files? would be reformatted", result.stdout + result.stderr)
    return int(m.group(1)) if m else 0


def _run_static() -> int:
    print("--- static checks ---")
    if not (_module_available("mypy") and _module_available("ruff")):
        print("mypy/ruff not installed in this interpreter; skipping static checks")
        print(
            "STATIC SUMMARY: compileall=skip mypy=-1 ruff_check=-1 "
            "ruff_format=-1 bash_n=skip"
        )
        print("STATIC RESULT: SKIPPED")
        return 0

    failures = 0

    # compileall -q . -- pyc goes to tmpfs because /repo is read-only in the
    # sandbox, and the compile itself must not touch the mounted checkout.
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(tempfile.gettempdir(), "agentland_pyc")
    r = _run([sys.executable, "-m", "compileall", "-q", REPO], REPO, env=env)
    compileall = "ok" if r.returncode == 0 else "fail"
    print(f"compileall: {compileall}")
    if r.returncode != 0:
        failures = 1

    # mypy (bare: file scope comes from pyproject.toml [tool.mypy]).
    mypy_cache = os.path.join(tempfile.gettempdir(), "agentland_mypy", "cache")
    r = _run(
        [sys.executable, "-m", "mypy", "--cache-dir", mypy_cache],
        REPO,
        capture=True,
    )
    mypy_errors = r.stdout.count("error:") if r.returncode != 0 else 0
    print(f"mypy: {mypy_errors} errors")
    if r.returncode != 0:
        failures = 1
        _dump(r)

    # ruff check .
    r = _run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "."],
        REPO,
        capture=True,
    )
    ruff_check = _count_found(r)
    print(f"ruff check: {ruff_check} errors")
    if r.returncode != 0:
        failures = 1
        _dump(r)

    # ruff format --check .
    r = _run(
        [sys.executable, "-m", "ruff", "format", "--check", "--no-cache", "."],
        REPO,
        capture=True,
    )
    ruff_format = _count_formatted(r)
    print(f"ruff format: {ruff_format} files would be reformatted")
    if r.returncode != 0:
        failures = 1
        _dump(r)

    # bash -n deploy/*.sh
    if shutil.which("bash") is None:
        bash_n = "skip"
        print("bash -n: skip (no bash on this host)")
    else:
        scripts = sorted(glob.glob(os.path.join(REPO, "deploy", "*.sh")))
        r = _run(["bash", "-n"] + scripts, REPO)
        bash_n = "ok" if r.returncode == 0 else "fail"
        print(f"bash -n: {bash_n}")
        if r.returncode != 0:
            failures = 1

    print(
        f"STATIC SUMMARY: compileall={compileall} mypy={mypy_errors} "
        f"ruff_check={ruff_check} ruff_format={ruff_format} bash_n={bash_n}"
    )
    print("STATIC RESULT: FAIL" if failures else "STATIC RESULT: PASS")
    return failures


def main() -> int:
    tests = _run([sys.executable, os.path.join(REPO, "tests", "run_all.py")], REPO)
    tests_ok = tests.returncode == 0
    static_fail = _run_static()
    return 1 if (not tests_ok or static_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
