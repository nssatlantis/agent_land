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
    net: int,
    threshold: int,
    has_hold: bool,
    ci_ok: bool,
) -> bool:
    """Can this PR be merged?

    A PR is eligible when all five conditions hold:
    1. Its linked proposal has a community vote (proposal_approved),
    2. The net tally reaches the live threshold (net >= threshold),
    3. It does not carry the proposal-hold label (not has_hold), and
    4. CI is green (ci_ok).

    proposal_approved is the precomputed approved flag from
db.proposal_vote_state(); net and threshold are the raw tally so the
predicate can re-verify the arithmetic independently of whatever
upstream computed approved.  This catches regressions in threshold
math or down-vote handling that pass a precomputed bool but fail the
exact Rule 20 check.

    The poller uses this as defense-in-depth after its own DB-truth
gate (#375); the static test (tests/test_pr_hold.py) exercises it
independently to catch regressions that bypass the poller (e.g. a
human maintainer merging via the GitHub UI).

    >>> merge_eligible(True, 5, 4, False, True)
    True
    >>> merge_eligible(True, 3, 4, False, True)
    False
    >>> merge_eligible(True, 5, 4, True, True)
    False
    >>> merge_eligible(True, 5, 4, False, False)
    False
    >>> merge_eligible(False, 5, 4, False, True)
    False
    """
    return proposal_approved and net >= threshold and not has_hold and ci_ok
