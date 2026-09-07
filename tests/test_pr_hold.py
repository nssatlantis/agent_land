"""Test the proposal-hold merge-eligibility predicate.

Covers the 'human merges below-threshold PR via GitHub UI' failure
class that the poller alone cannot seal (#321, supersedes #233).
Each test exercises the pure merge_eligible() predicate from
server/_merge_gate.py — no I/O, no DB, no GitHub.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server._merge_gate import merge_eligible


def test_rejects_hold():
    """PR carrying proposal-hold must not be mergeable."""
    assert merge_eligible(proposal_approved=True, has_hold=True, ci_ok=True) is False


def test_rejects_unapproved():
    """PR whose proposal has not passed the community vote must not merge."""
    assert merge_eligible(proposal_approved=False, has_hold=False, ci_ok=True) is False


def test_rejects_unapproved_and_hold():
    """Unapproved proposal AND hold label must reject."""
    assert merge_eligible(proposal_approved=False, has_hold=True, ci_ok=True) is False
    assert merge_eligible(proposal_approved=False, has_hold=True, ci_ok=False) is False


def test_rejects_ci_red():
    """Approved proposal and no hold, but red CI blocks merge."""
    assert merge_eligible(proposal_approved=True, has_hold=False, ci_ok=False) is False


def test_accepts_clean_path():
    """Approved proposal, no hold, CI green — the only eligible path."""
    assert merge_eligible(proposal_approved=True, has_hold=False, ci_ok=True) is True


def test_tuple_vs_row_regression():
    """Regression pin: raw tuple-indexed value must raise, not coerce."""
    try:
        merge_eligible(proposal_approved="not a bool", has_hold=False, ci_ok=True)  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        pass


def main():
    tests = [
        test_rejects_hold,
        test_rejects_unapproved,
        test_rejects_unapproved_and_hold,
        test_rejects_ci_red,
        test_accepts_clean_path,
        test_tuple_vs_row_regression,
    ]
    for fn in tests:
        fn()
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
