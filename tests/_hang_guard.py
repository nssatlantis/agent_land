"""Launcher-side hang watchdog for every run_all.py child (proposal #837).

Finding #32 on #PR1537: the watchdog was armed in ONE test file, but the
exposure is `tests/run_all.py`'s per-file 120s wall, so every test file in
the repo is spawned under the same kill and the other ~260 of them still
lost their evidence with no stack. Arming here covers all of them with no
cooperation from any test file.

The cheaper alternative is NOT available, and this is recorded so nobody
proposes it again: `PYTHONFAULTHANDLER=1` enables the fatal-signal handler
but does NOT arm the timer, and `subprocess.run(timeout=)` kills with
SIGKILL, which no handler can catch or dump on. The timer has to be armed
inside the child, which is exactly why the launcher is the right seam.

`runpy.run_path(..., run_name="__main__")` is load-bearing, not a
convenience: the suite's whole convention is `if __name__ == "__main__":
sys.exit(main())`, which only fires if the target genuinely runs as
`__main__`. Importing the target would not fire it at all, and a
`-c "import runpy; ..."` preamble gives no better guarantee than a real
module. A failing target's `sys.exit` propagates out of `main()` and out
of the interpreter, so the exit code is unchanged.

THIS MODULE MUST STAY IN `tests/`. `sys.path[0]` is the directory of the
script being run, so routing every test file through a guard in some other
directory would silently change `sys.path[0]` for all of them. Test files
mostly insert the repo root themselves from `__file__` (which run_path
still sets correctly), so that is the only thing at risk - but it is
enough, and `test_hang_guard.py` pins the sibling property.
"""

from __future__ import annotations

import faulthandler
import runpy
import sys

# The margin IS the design, and it is pinned rather than asserted in prose
# (test_hang_guard.test_watchdog_margin_under_harness_wall). At or above
# run_all.py's 120s wall the harness SIGKILLs this child MID-DUMP and the
# evidence dies with the process; ten seconds of slack is what lets
# communicate() collect the stack. #PR1537 pins the same literal in
# test_farm.py, and the way to keep two separately-read literals honest is
# a pin, not a shared constant.
WATCHDOG_SECONDS = 110


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: _hang_guard.py <test_file> [args...]")
    target, *rest = sys.argv[1:]
    # Keep exit=True, and do not "tidy" it into `raise`. With exit=True this
    # child terminates ITSELF at ~110s with a non-zero returncode, so
    # subprocess.run(timeout=120) returns a CompletedProcess NORMALLY and
    # the dump rides that path's `output = result.stdout + result.stderr`.
    # Switching to `raise` reopens #B133 through a new door: a child
    # SIGKILLed at the wall may still hold an open connection or a
    # half-finished transaction in its worker's pooled session DB, which is
    # why run_all.py's timeout arm retains the slot instead of handing it on.
    # A child that exits cleanly at 110s is not in that state and so skips
    # the surrender - the right outcome, but by accident rather than design.
    faulthandler.dump_traceback_later(WATCHDOG_SECONDS, exit=True)
    sys.argv = [target, *rest]
    runpy.run_path(target, run_name="__main__")


if __name__ == "__main__":
    main()
