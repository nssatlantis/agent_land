"""Tests for the launcher-side hang watchdog (proposal #837, finding #32).

#PR1537 armed a faulthandler watchdog in `test_farm.py` alone. The exposure
is `tests/run_all.py`'s per-file 120s wall, so every test file in the repo is
spawned under the same kill and the other ~260 of them still lost their
evidence with no stack. `tests/_hang_guard.py` arms it for every child.

These pins cover the four things that would silently undo that, in
descending order of how quietly they would fail:

1. the guard really does run the target as `__main__`, rewrites sys.argv to
   the target, forwards extra args, and preserves the exit code (executed,
   not source-matched);
2. the guard really arms a timer that dumps and exits non-zero (executed at
   a 1s literal, so the mechanism is proven in ~2s rather than asserted);
3. the margin between the watchdog and the harness wall is a real gap, and
   the call still exists - a presence check would stay green on exactly the
   regression this exists to prevent;
4. the guard stays a sibling of `run_all.py`, because `sys.path[0]` is the
   directory of the script being run and moving it would change the import
   root for every test file in the repo.

Two of these were red on their first rehearsal and both reds were in the
PINS rather than in the guard, which is recorded here because the second one
is a trap worth naming: the watchdog's literal cannot be shortened by
patching the parent, because the guard runs in a child process. See
_HANG_BOOTSTRAP.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_GUARD = _HERE / "_hang_guard.py"
_RUN_ALL = _HERE / "run_all.py"

# Fixture that only succeeds when the guard ran it as __main__, left
# sys.argv[0] pointing at the target, and forwarded the extra argument.
# Exits 7 so the caller can prove the exit code survives the run_path
# indirection - a guard that swallowed it would turn a failing suite green.
_AS_MAIN_FIXTURE = """\
import os
import sys

if __name__ != "__main__":
    raise SystemExit("NOT_MAIN: guard did not run the target as __main__")
_expected = os.environ["HANG_GUARD_TARGET"]
if sys.argv[0] != _expected:
    raise SystemExit(f"ARGV0: set {sys.argv[0]!r}, expected {_expected!r}")
if sys.argv[1:] != ["SENTINEL_ARG"]:
    raise SystemExit(f"ARGV_REST: extra args not forwarded: {sys.argv[1:]!r}")
with open(os.environ["HANG_GUARD_SENTINEL"], "w", encoding="utf-8") as fh:
    fh.write("ran")
raise SystemExit(7)
"""

# Fixture that blocks long enough to be killed by an armed watchdog, named
# so the dumped stack proves WHICH frame was stuck rather than merely that a
# stack arrived.
_HANG_FIXTURE = """\
import time


def the_hanging_test():
    time.sleep(30)


the_hanging_test()
"""

# Runs the guard's REAL main() in a process where the shortened literal is
# actually visible to the call site. The obvious version of this pin -
# import _hang_guard in the test, set WATCHDOG_SECONDS = 1, then subprocess
# the guard as a child - is silently vacuous: the child re-imports the
# module at its real 110s, so the fixture sleeps its full 30s and exits 0,
# and the pin fails for the wrong reason (it cost exactly that one red).
# Here the patch and the call site share a process, so the arm genuinely
# fires at 1s while the code under test is unchanged.
_HANG_BOOTSTRAP = """\
import os
import sys

sys.path.insert(0, os.environ["HANG_GUARD_DIR"])
# main() slices sys.argv[1:], so argv[0] is the program name and argv[1]
# is the target. A one-element argv therefore reads as "no target given" and
# the guard prints its usage text - which is how this pin's own failure
# message carried the diagnosis on the run that caught it.
sys.argv = ["_hang_guard.py", os.environ["HANG_GUARD_TARGET"]]
import _hang_guard

_hang_guard.WATCHDOG_SECONDS = 1
_hang_guard.main()
"""


def _write_fixture(name: str, body: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="hang_guard_"))
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    return path


def test_hang_guard_runs_the_target_as_main() -> None:
    """Executed: run_name="__main__" is what fires the suite's sys.exit(main())
    convention, and it is the whole reason the guard uses run_path rather
    than importing the target. A guard that merely imported the file would
    pass a source-shape check and silently never run a single test.

    Also pins argv rewriting and pass-through, and the exit code: with no
    extra argument the guard sets sys.argv to exactly [target], which is
    what direct invocation gives, so the assertion compares the forwarded
    argument rather than indexing a second element that legitimately does
    not exist.
    """
    fixture = _write_fixture("as_main_probe.py", _AS_MAIN_FIXTURE)
    sentinel = fixture.parent / "sentinel.txt"
    env = dict(os.environ)
    env["HANG_GUARD_SENTINEL"] = str(sentinel)
    env["HANG_GUARD_TARGET"] = str(fixture)
    proc = subprocess.run(
        [sys.executable, str(_GUARD), str(fixture), "SENTINEL_ARG"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert sentinel.exists(), (
        "the target never ran its __main__ block through the guard; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert proc.returncode == 7, (
        f"exit code did not survive run_path: got {proc.returncode}, want 7 "
        f"(a failing test file must still fail its child). "
        f"stderr={proc.stderr!r}"
    )


def test_guard_arms_a_watchdog_that_dumps_and_exits_nonzero() -> None:
    """Executed mechanism proof, at a 1s literal.

    faulthandler exposes no query for a pending dump, so the ONLY way to
    know the arm exists is to make it fire. Asserts the header, the non-zero
    exit (so a hang is a failure with evidence, not a silent kill) and the
    stuck frame's name - the last is what makes this a diagnosis rather than
    a dump receipt.
    """
    fixture = _write_fixture("hang_probe.py", _HANG_FIXTURE)
    env = dict(os.environ)
    env["HANG_GUARD_DIR"] = str(_HERE)
    env["HANG_GUARD_TARGET"] = str(fixture)
    proc = subprocess.run(
        [sys.executable, "-c", _HANG_BOOTSTRAP],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    blob = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0, (
        f"a hang must be a non-zero FAILURE, got {proc.returncode}; output={blob!r}"
    )
    assert "Timeout (0:00:01)!" in blob, (
        f"the watchdog header is absent - the timer was never armed. output={blob!r}"
    )
    assert "the_hanging_test" in blob, (
        "the dump did not name the stuck frame, so the evidence is a stack "
        f"without a culprit. output={blob!r}"
    )


def test_watchdog_margin_under_harness_wall() -> None:
    """Pin the MARGIN, not the presence.

    At or above the wall the harness SIGKILLs the child mid-dump and the
    evidence dies with the process, so the gap is the design. Read the wall
    out of run_all.py's single subprocess.run call rather than trusting a
    literal, so moving either number is caught.
    """
    guard_src = _GUARD.read_text(encoding="utf-8")
    harness_src = _RUN_ALL.read_text(encoding="utf-8")
    m = re.search(r"WATCHDOG_SECONDS\s*=\s*(\d+)", guard_src)
    assert m, "WATCHDOG_SECONDS is gone from tests/_hang_guard.py"
    assert "dump_traceback_later(" in guard_src, (
        "the dump_traceback_later call is gone from the guard - the timer is "
        "unarmed and the module is a no-op wrapper"
    )
    assert "exit=True" in guard_src, (
        "exit=True was dropped from the guard. With it, the child ends ITSELF "
        "at the watchdog literal and the dump rides run_all.py's normal-return "
        "path; without it the harness kills at the wall and the evidence dies"
    )
    watchdog_s = int(m.group(1))
    w = re.search(r"subprocess\.run\(.*?timeout=(\d+)", harness_src, re.S)
    assert w, "run_all.py's per-file wall literal moved - update this pin"
    wall_s = int(w.group(1))
    margin = wall_s - watchdog_s
    assert margin >= 5, (
        f"watchdog fires at {watchdog_s}s, harness wall is {wall_s}s: margin "
        f"{margin}s < 5s. At or above the wall the SIGKILL truncates the dump "
        "mid-write and the evidence dies with the process."
    )


def test_hang_guard_is_a_sibling_of_run_all() -> None:
    """sys.path[0] is the directory of the script being run.

    Routing every test file through a guard in another directory would
    change sys.path[0] for all of them, silently, with no test failing -
    which is why this is a pin and not a comment.
    """
    assert _GUARD.exists(), f"tests/_hang_guard.py is missing (looked in {_GUARD})"
    assert _GUARD.parent == _RUN_ALL.parent, (
        f"the guard must stay a sibling of run_all.py: it is at {_GUARD.parent} "
        f"while run_all.py is at {_RUN_ALL.parent}. sys.path[0] is the "
        "directory of the script being run, so this change is invisible to "
        "every test file's imports"
    )


def test_run_all_spawns_every_child_through_the_guard() -> None:
    """AST census, not a text search, so a comment quoting the old argv can
    neither satisfy this nor break it. Asserts there is exactly ONE
    subprocess.run call in the launcher and that its argv names the guard -
    reverting the one-line change reds here rather than silently
    un-covering ~260 files."""
    tree = ast.parse(_RUN_ALL.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert len(calls) == 1, (
        f"expected exactly one subprocess.run in the launcher, found "
        f"{len(calls)}; this pin's timeout regex assumes the first one is it"
    )
    argv = calls[0].args[0]
    assert isinstance(argv, ast.List), (
        f"the launcher's argv is {type(argv).__name__}, not a list literal - "
        "this pin cannot see through it"
    )
    names = {n.id for n in ast.walk(argv) if isinstance(n, ast.Name)}
    attrs = {
        (n.value.id, n.attr)
        for n in ast.walk(argv)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    }
    assert "_HANG_GUARD" in names, (
        f"the launcher no longer spawns children through the hang guard: "
        f"argv names {sorted(names)} {sorted(attrs)}. This is the one-line "
        "revert that un-covers every test file in the repo"
    )
    assert ("sys", "executable") in attrs, (
        f"the launcher no longer invokes sys.executable: {sorted(attrs)}"
    )


def main() -> int:
    tests = [
        test_hang_guard_runs_the_target_as_main,
        test_guard_arms_a_watchdog_that_dumps_and_exits_nonzero,
        test_watchdog_margin_under_harness_wall,
        test_hang_guard_is_a_sibling_of_run_all,
        test_run_all_spawns_every_child_through_the_guard,
    ]
    failures: list[str] = []
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # collect rather than die on the first
            failures.append(f"{fn.__name__}: {exc}")
    for line in failures:
        print(f"FAIL {line}")
    total = len(tests)
    if failures:
        print(f"=== test_hang_guard: {len(failures)} of {total} FAILED ===")
        return 1
    print(f"=== test_hang_guard: all {total} passed ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
