"""Test the proposal-hold merge-eligibility predicate.

Covers the human-merge-below-threshold failure class that the poller
alone cannot seal (#321, supersedes #233).
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
    """Approved proposal, net >= threshold, no hold, CI green - eligible."""
    assert merge_eligible(True, 5, 4, has_hold=False, ci_ok=True) is True
    assert merge_eligible(True, 4, 4, has_hold=False, ci_ok=True) is True
    assert merge_eligible(True, 10, 4, has_hold=False, ci_ok=True) is True


def test_threshold_boundary():
    """Exact threshold boundary: net == threshold is eligible."""
    assert merge_eligible(True, 4, 4, has_hold=False, ci_ok=True) is True
    assert merge_eligible(True, 3, 4, has_hold=False, ci_ok=True) is False


def test_tuple_vs_row_regression():
    """Regression pin: sqlite3.Row on net must raise, not coerce."""
    mock_row = MagicMock(spec=sqlite3.Row)
    mock_row.__ge__ = MagicMock(side_effect=TypeError("tuple indices must be integers"))
    try:
        merge_eligible(True, mock_row, 4, has_hold=False, ci_ok=True)  # type: ignore[arg-type]
    except TypeError:
        pass
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
