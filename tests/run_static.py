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


def _scratch_dir(name: str, target: str) -> str:
    """Writable scratch dir for static-check caches.

    `tempfile.gettempdir()` raises when no candidate is usable (e.g. the
    sandbox /tmp tmpfs filled mid-suite) - fall back through /tmp, HOME,
    then a dot-dir under the checked-out target. Every candidate is
    probed with makedirs + mkstemp so an unwritable pick never escapes.
    Raises OSError when nothing is writable; the caller reports that as
    a parseable STATIC RESULT: FAIL instead of a bare traceback."""
    candidates: list[str] = []
    try:
        candidates.append(tempfile.gettempdir())
    except Exception:
        pass  # domain: degrade-silently - try the next candidate
    candidates.append("/tmp")
    _home = os.environ.get("HOME")
    if _home:
        candidates.append(_home)
    candidates.append(os.path.join(target, ".agentland_tmp"))
    _seen: set[str] = set()
    last_exc: Exception | None = None
    for _base in candidates:
        if _base in _seen:
            continue
        _seen.add(_base)
        try:
            _d = os.path.join(_base, name)
            os.makedirs(_d, exist_ok=True)
            _fd, _probe = tempfile.mkstemp(prefix=".w_", dir=_d)
            os.close(_fd)
            os.unlink(_probe)
            return _d
        except Exception as _exc:
            last_exc = _exc
            continue  # domain: degrade-silently - try next candidate
    raise OSError(f"no writable scratch dir for {name!r}: {last_exc}")


def run_static_checks(target: str = REPO) -> int:
    """The static half, shared verbatim with tests/run_ci.py (which
    imports this - one source, never two copies drifting apart). `target`
    is the tree to check (default: this repo); tests pass a throwaway dir
    so the red path never mutates the source tree (the CI sandbox mounts
    it read-only)."""
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
    # _scratch_dir falls back past a filled tmpfs so exhaustion reports a
    # parseable FAIL instead of an unhandled gettempdir traceback.
    env = dict(os.environ)
    try:
        pycache_dir = _scratch_dir("agentland_pyc", target)
    except OSError as _exc:
        print(f"compileall: fail ({_exc})")
        print(
            "STATIC SUMMARY: compileall=fail mypy=-1 "
            "ruff_check=-1 ruff_format=-1 bash_n=skip"
        )
        print(f"STATIC RESULT: FAIL ({_exc})")
        return 1
    env["PYTHONPYCACHEPREFIX"] = pycache_dir
    r = _run([sys.executable, "-m", "compileall", "-q", target], target, env=env)
    compileall = "ok" if r.returncode == 0 else "fail"
    print(f"compileall: {compileall}")
    if r.returncode != 0:
        failures = 1

    # mypy (bare: against this repo, file scope comes from
    # pyproject.toml [tool.mypy]). The sandbox mounts a persistent
    # per-slot cache at AGENTLAND_MYPY_CACHE_DIR when configured;
    # anything unwritable falls back to the per-run tmpfs cache.
    try:
        _mypy_scratch = _scratch_dir("agentland_mypy", target)
    except OSError:
        _mypy_scratch = os.path.dirname(pycache_dir)  # proven above
    _default_mypy_cache = os.path.join(_mypy_scratch, "cache")
    mypy_cache = os.environ.get("AGENTLAND_MYPY_CACHE_DIR") or _default_mypy_cache
    try:
        os.makedirs(mypy_cache, exist_ok=True)
        if not os.access(mypy_cache, os.W_OK):
            raise OSError("mypy cache dir not writable")
    except Exception:
        mypy_cache = _default_mypy_cache  # domain: degrade-silently
    r = _run(
        [sys.executable, "-m", "mypy", "--cache-dir", mypy_cache],
        target,
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
        target,
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
        target,
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
        scripts = sorted(glob.glob(os.path.join(target, "deploy", "*.sh")))
        if not scripts:
            bash_n = "skip"
            print("bash -n: skip (no deploy/*.sh scripts found)")
        else:
            r = _run(["bash", "-n"] + scripts, target)
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
