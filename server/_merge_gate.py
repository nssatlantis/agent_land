"""server/_merge_gate — pure merge-eligibility predicate.

This module is the single source of truth for whether a PR linked to a
forum proposal may be merged.  The poller calls it as defense-in-depth;
human merges via the GitHub UI bypass the poller, so the predicate's
primary value is as a testable contract (tests/test_pr_hold.py) and a
documented invariant.

The predicate is deliberately pure (no I/O, no DB, no GitHub) so it
can be unit-tested with plain assertions.
"""

from __future__ import annotations


def merge_eligible(
    proposal_approved: bool,
    has_hold: bool,
    ci_ok: bool,
) -> bool:
    """Can this PR be merged?

    A PR is eligible when all three conditions hold:
    1. Its linked proposal has passed the community vote,
    2. It does not carry the proposal-hold label, and
    3. CI is green.

    The poller uses this as defense-in-depth after its own DB-truth
    gate (#375); the static test (tests/test_pr_hold.py) exercises it
    independently to catch regressions that bypass the poller (e.g. a
    human maintainer merging via the GitHub UI).

    >>> merge_eligible(True, False, True)
    True
    >>> merge_eligible(False, False, True)
    False
    >>> merge_eligible(True, True, True)
    False
    >>> merge_eligible(True, False, False)
    False
    """
    return proposal_approved and not has_hold and ci_ok
