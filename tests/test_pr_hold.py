"""Test the proposal-hold merge-eligibility predicate.

Covers the 'human merges below-threshold PR via GitHub UI' failure
class that the poller alone cannot seal (#321, supersedes #233).
Each test exercises the pure merge_eligible() predicate from
server/_merge_gate.py — no I/O, no DB, no GitHub.
"""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server._merge_gate import merge_eligible


def test_rejects_hold():
    """PR carrying proposal-hold must not be mergeable."""
    assert merge_eligible(True, 5, 4, has_hold=True, ci_ok=True) is False


def test_rejects_unapproved():
    """PR whose proposal has not passed the community vote must not merge."""
    assert merge_eligible(False, 5, 4, has_hold=False, ci_ok=True) is False


def test_rejects_unapproved_and_hold():
    """Unapproved proposal AND hold label must reject."""
    assert merge_eligible(False, 5, 4, has_hold=True, ci_ok=True) is False
    assert merge_eligible(False, 5, 4, has_hold=True, ci_ok=False) is False


def test_rejects_ci_red():
    """Approved proposal and no hold, but red CI blocks merge."""
    assert merge_eligible(True, 5, 4, has_hold=False, ci_ok=False) is False


def test_rejects_below_threshold():
    """Net tally below threshold must block even when approved."""
    assert merge_eligible(True, 3, 4, has_hold=False, ci_ok=True) is False
    assert merge_eligible(True, 0, 4, has_hold=False, ci_ok=True) is False


def test_accepts_clean_path():
    """Approved proposal, net >= threshold, no hold, CI green — eligible."""
    assert merge_eligible(True, 5, 4, has_hold=False, ci_ok=True) is True
    assert merge_eligible(True, 4, 4, has_hold=False, ci_ok=True) is True
    assert merge_eligible(True, 10, 4, has_hold=False, ci_ok=True) is True


def test_threshold_boundary():
    """Exact threshold boundary: net == threshold is eligible."""
    assert merge_eligible(True, 4, 4, has_hold=False, ci_ok=True) is True
    assert merge_eligible(True, 3, 4, has_hold=False, ci_ok=True) is False


def test_tuple_vs_row_regression():
    """Regression pin for the Agent8 #7 / #313 norm: if a caller passes
a sqlite3.Row (which raises on integer comparison) instead of an int
for net, the predicate must propagate the error rather than silently
coerce via bool()."""
    # A raw sqlite3.Row raised on int operations — this is the failure
    # mode that #1040 (sophia's hotfix) caught in backfill_escrow_account.
    mock_row = MagicMock(spec=sqlite3.Row)
    mock_row.__ge__ = MagicMock(side_effect=TypeError("tuple indices must be integers"))
    try:
        merge_eligible(True, mock_row, 4, has_hold=False, ci_ok=True)  # type: ignore[arg-type]
    except TypeError:
        pass  # expected — raw Row access would blow up here
    else:
        assert False, "Expected TypeError from mock Row comparison"


def main():
    tests = [
        test_rejects_hold,
        test_rejects_unapproved,
        test_rejects_unapproved_and_hold,
        test_rejects_ci_red,
        test_rejects_below_threshold,
        test_accepts_clean_path,
        test_threshold_boundary,
        test_tuple_vs_row_regression,
    ]
    for fn in tests:
        fn()
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
