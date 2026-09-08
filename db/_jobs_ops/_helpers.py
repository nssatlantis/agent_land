"""db._jobs_ops._helpers — evidence parsing, overdue math, formatting (split verbatim from db/_jobs_ops.py)."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import config
import github

_PR_RE = re.compile(
    r"(?:#PR\s*(\d+)|PR\s*#?\s*(\d+)|/prs/(\d+)|/pull/(\d+))",
    re.IGNORECASE,
)


def _parse_pr_numbers(evidence: str) -> list[int]:
    """Extract PR numbers from evidence text for advisory linking."""
    if not evidence:
        return []
    seen: set[int] = set()
    out: list[int] = []
    for m in _PR_RE.finditer(evidence):
        for g in m.groups():
            if g and g.isdigit():
                n = int(g)
                if n > 0 and n not in seen:
                    seen.add(n)
                    out.append(n)
                    if len(out) >= 10:
                        return out
                break
    return out


_JOB_ANCHOR_KINDS = (
    "job_claimed",
    "job_submitted",
    "job_cycle_accepted",
    "job_cycle_declined",
)
# Pre-joined IN-list for the anchor expression below: rebuilt per query
# otherwise, once per module load is enough (tuple is a literal).
_JOB_ANCHOR_KINDS_SQL = ",".join(f"'{k}'" for k in _JOB_ANCHOR_KINDS)


def _job_overdue_anchor_sql(job_alias: str) -> str:
    """SQL for the latest events-ledger anchor of a job's current cycle.

    Returns a COALESCE(latest job event in _JOB_ANCHOR_KINDS, created_at)
    expression referencing the given jobs alias.  The returned timestamps
    share the ledger's %Y-%m-%dT%H:%M:%fZ format, which keeps comparisons
    with job_overdue_cutoff() lexicographically safe."""
    kinds = _JOB_ANCHOR_KINDS_SQL
    return (
        f"COALESCE((SELECT MAX(e.created_at) FROM events e"
        f" WHERE e.target_type = 'job' AND e.target_id = {job_alias}.id"
        f" AND e.kind IN ({kinds})), {job_alias}.created_at)"
    )


def job_overdue_cutoff() -> str:
    """The ISO boundary for 'overdue', or '' when the feature is disabled.

    An active job whose CURRENT cycle is still awaiting/declined past this
    many hours (config.JOB_CYCLE_DUE_HOURS) since its last status move
    reads as overdue.  A cutoff of 0 (FORUM_JOB_CYCLE_DUE_HOURS=0) disables
    the feature."""
    hours = int(config.JOB_CYCLE_DUE_HOURS)
    if hours <= 0:
        return ""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _cycle_is_overdue(status: str | None, anchor_at: str | None, cutoff: str) -> bool:
    """True when a job's current cycle idles past the due window.

    awaiting/declined are the worker's turn (the creator has already made
    their move); 'submitted' means the ball is with the creator and never
    counts as overdue.  Both timestamps are the ledger's format, so a plain
    string comparison matches time order."""
    if not cutoff or status not in ("awaiting", "declined"):
        return False
    if not anchor_at:
        return False
    return anchor_at <= cutoff


def _overdue_windows_elapsed(anchor_at: str | None, cutoff: str) -> int:
    """How many whole FORUM_JOB_CYCLE_DUE_HOURS windows a cycle has idled
    past its deadline: 0 = not overdue, 1 = the first due window has fully
    elapsed, then +1 per window.  Deterministic from the events anchor
    alone (no schema column), so the release threshold
    (FORUM_JOB_OVERDUE_RELEASE_AFTER) resolves on the fly; a misread
    ledger or dead clock degrades to 0 and never releases."""
    hours = int(config.JOB_CYCLE_DUE_HOURS)
    if not cutoff or hours <= 0 or not anchor_at:
        return 0
    try:
        window_s = hours * 3600
        anchor = datetime.fromisoformat(anchor_at.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - anchor).total_seconds()
        if age < window_s:
            return 0
        return int(age) // window_s
    except Exception:
        return 0  # domain: degrade-silently - unparsable clock = no release


def _overdue_flag(
    status: str,
    cur_cycle_status: str | None,
    anchor_at: str | None,
    cutoff: str,
) -> bool:
    """Board-level overdue flag: the job must be ACTIVE and its current
    cycle must idle past the due window.  Completed/expired/cancelled jobs
    never read overdue, even where a leftover cycle row still sits in a
    transitional status."""
    if status != "active":
        return False
    return _cycle_is_overdue(cur_cycle_status, anchor_at, cutoff)


def _all_prs_merged(pr_numbers: list[int]) -> bool:
    """Strict gate: all PRs in evidence must be closed && merged_at non-null.
    Best-effort, advisory - if GitHub unavailable, treat as not merged."""
    if not pr_numbers:
        return True
    try:
        for n in pr_numbers:
            try:
                pr = github.get_pr(n)
                if pr.get("state") != "closed" or not pr.get("merged_at"):
                    if pr.get("outcome") != "merged" and not pr.get("merged_at"):
                        return False
            except Exception:
                # domain: degrade-silently - PR lookup failed, not merged
                return False
        return True
    except Exception:
        # domain: degrade-silently - github import failed, not merged
        return False


def _fmt_q(quarters: int) -> str:
    from db._credits import format_credits

    return format_credits(quarters)


def _parse_cycle_evidence(r: sqlite3.Row) -> tuple[list[int], list[str]]:
    """Parse a job_cycles row's stored PR references (advisory linking).
    Degrade-silently: malformed or wrong-shaped JSON becomes empty lists."""
    try:
        pr_numbers = (
            json.loads(r["evidence_pr_numbers"]) if r["evidence_pr_numbers"] else []
        )
        if not isinstance(pr_numbers, list):
            pr_numbers = []
    except Exception:  # domain: degrade-silently
        pr_numbers = []
    try:
        pr_shas = json.loads(r["evidence_pr_shas"]) if r["evidence_pr_shas"] else []
        if not isinstance(pr_shas, list):
            pr_shas = []
    except Exception:  # domain: degrade-silently
        pr_shas = []
    pr_numbers = [
        int(n)
        for n in pr_numbers
        if isinstance(n, int) or (isinstance(n, str) and str(n).isdigit())
    ]
    return pr_numbers, pr_shas


def _unhold_cycle_prs(cycle: sqlite3.Row) -> None:
    """Remove 'hold' label from PRs referenced in a cycle after accept."""
    try:
        _pr_nums = (
            json.loads(cycle["evidence_pr_numbers"])
            if cycle["evidence_pr_numbers"]
            else []
        )
        for _prn in _pr_nums:
            try:
                github.remove_pr_label(int(_prn), "hold")
            except Exception:
                # domain: degrade-silently
                pass
    except Exception:
        # domain: degrade-silently
        pass
