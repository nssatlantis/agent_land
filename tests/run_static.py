"""Static-only CI harness (server-side CI runner, checks="static").

Runs exactly the static half of tests/run_ci.py - compileall, mypy,
ruff check, ruff format --check, bash -n - without the test suite, so a
format rewrap or import-sort slip costs seconds to discover instead of a
full suite run. It is what the server's repo_ci_run(checks="static")
executes. The TESTS marker below is load-bearing: the sandbox parser
turns it into summary.tests_run=False, and the workflow gate accepts a
static-only green for the `lint` tick while `test`/`not-gutted` still
demand tests actually ran. Never cite a static-only green as merge
evidence - the tests did NOT run.

Run directly with: python tests/run_static.py
"""

import glob
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


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


def run_static_checks() -> int:
    """The static half, shared verbatim with tests/run_ci.py (which
    imports this - one source, never two copies drifting apart)."""
    print("--- static checks ---")
    if not (_module_available("mypy") and _module_available("ruff")):
        print(
            "!!! STATIC CHECKS SKIPPED — running on the host interpreter without "
            "mypy/ruff. This is the native host fallback: ONLY the test suite ran; "
            "static checks were NOT executed and this run is NOT "
            "GitHub-CI-equivalent. Use repo_ci_run(pr_number=...) or "
            "repo_ci_run(files=...) to get the full test+static surface."
        )
        print(
            "STATIC SUMMARY: compileall=skip mypy=-1 ruff_check=-1 "
            "ruff_format=-1 bash_n=skip"
        )
        print("STATIC RESULT: SKIPPED (native host fallback - static NOT run)")
        return 0

    failures = 0

    # compileall -q . -- pyc goes to tmpfs because /repo is read-only in the
    # sandbox, and the compile itself must not touch the mounted checkout.
    env = dict(os.environ)
    pycache_dir = os.path.join(tempfile.gettempdir(), "agentland_pyc")
    os.makedirs(pycache_dir, exist_ok=True)
    env["PYTHONPYCACHEPREFIX"] = pycache_dir
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
        if not scripts:
            bash_n = "skip"
            print("bash -n: skip (no deploy/*.sh scripts found)")
        else:
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
    failures = run_static_checks()
    # Load-bearing marker (see module docstring): the sandbox parser turns
    # this into summary.tests_run=False. tests/run_ci.py must never print
    # it - its runs execute the suite.
    print("TESTS: SKIPPED (static-only harness - tests NOT run)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
