"""Format-only CI harness (server-side CI runner, checks="format").

Runs exactly one check - `ruff format --check .` - over the target tree,
so a rewrap slip costs seconds to discover instead of a full static or
suite run. It is what the server's repo_ci_run(checks="format")
executes. Budget-free (proposal #636): no ledger deduction in any mode;
cooldown + single-inflight + pool slot still enforced. Advisory-only:
the TESTS marker below is load-bearing (the sandbox parser turns it
into summary.tests_run=False), the workflow gate never ticks anything
off a format run, and a format green is never merge evidence - the
suite did NOT run, and neither did ruff check or mypy.

Run directly with: python tests/run_format.py
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from run_static import _module_available, format_checks
except ImportError:  # domain: import-style - package import (import tests.run_format) resolves the sibling as tests.run_static
    from tests.run_static import _module_available, format_checks


def run_format_checks(target: str = REPO) -> int:
    """The format check on *target* (default: this repo); tests pass a
    throwaway dir so the red path never mutates the source tree (the CI
    sandbox mounts it read-only). Returns 1 on violations, else 0."""
    if not _module_available("ruff"):
        print(
            "!!! FORMAT CHECK SKIPPED — running on the host interpreter "
            "without ruff. This run proves nothing about formatting; use "
            'repo_ci_run(checks="format", files=[...]) to get the '
            "sandboxed check with pinned tooling."
        )
        print(
            "STATIC SUMMARY: compileall=skip mypy=-1 ruff_check=-1 "
            "ruff_format=-1 bash_n=skip"
        )
        print("STATIC RESULT: SKIPPED (format harness - ruff NOT available)")
        return 0
    print("--- format check ---")
    n = format_checks(target)
    print(
        "STATIC SUMMARY: compileall=skip mypy=-1 ruff_check=-1 "
        f"ruff_format={n} bash_n=skip"
    )
    print("STATIC RESULT: FAIL" if n else "STATIC RESULT: PASS")
    return 1 if n else 0


def main() -> int:
    failures = run_format_checks()
    # Load-bearing marker (see module docstring): the sandbox parser turns
    # this into summary.tests_run=False. tests/run_ci.py must never print
    # it - its runs execute the suite.
    print("TESTS: SKIPPED (static-only format harness - tests NOT run)")
    return failures


if __name__ == "__main__":
    sys.exit(main())
