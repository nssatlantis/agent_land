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


# Job-overdue accounting.  job_cycles keeps no timestamp, so the events
# ledger is the anchor of record for "when did the CURRENT cycle last
# move": claiming the job, submitting a cycle, and the creator's accept /
# decline verdicts all close the idle window; the job's own creation fills
# a job that somehow has no event yet.
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
    with job_overdue_cutoff() lexicographically safe. Single-row use;
    multi-row callers batch with _job_anchors_for instead of paying one
    correlated probe per row."""
    kinds = _JOB_ANCHOR_KINDS_SQL
    return (
        f"COALESCE((SELECT MAX(e.created_at) FROM events e"
        f" WHERE e.target_type = 'job' AND e.target_id = {job_alias}.id"
        f" AND e.kind IN ({kinds})), {job_alias}.created_at)"
    )


def _job_anchors_for(
    conn: sqlite3.Connection, job_ids: list[int]
) -> dict[int, str | None]:
    """{job_id: latest anchor created_at or None} for a batch of jobs - the
    batch twin of _job_overdue_anchor_sql's correlated subquery, so listers
    pay one GROUP BY instead of one probe per row.  A job with no anchor
    event is simply absent: callers fall back to the job's created_at,
    exactly the COALESCE the scalar form applies."""
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    kinds = _JOB_ANCHOR_KINDS_SQL
    return {
        r["job_id"]: r["anchor"]
        for r in conn.execute(
            "SELECT e.target_id AS job_id, MAX(e.created_at) AS anchor"
            " FROM events e"
            " WHERE e.target_type = 'job'"
            f" AND e.target_id IN ({marks})"
            f" AND e.kind IN ({kinds})"
            " GROUP BY e.target_id",
            job_ids,
        ).fetchall()
    }


def job_overdue_cutoff(hours: int | None = None) -> str:
    """The ISO boundary for 'overdue', or '' when the feature is disabled.

    An active job whose CURRENT cycle is still awaiting/declined past this
    many hours (config.JOB_CYCLE_DUE_HOURS) since its last status move
    reads as overdue.  A cutoff of 0 (FORUM_JOB_CYCLE_DUE_HOURS=0) disables
    the feature.  Pass `hours` to apply a per-job effective window (a
    cadenced recurring job's cycle_every_days x FORUM_JOB_CYCLE_DUE_HOURS);
    the default uses the configured hours."""
    hours = int(hours if hours is not None else config.JOB_CYCLE_DUE_HOURS)
    if hours <= 0:
        return ""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def job_cycle_opens_at(days: int) -> str:
    """ISO time a cadenced cycle opens: N days from now, in the ledger's
    format - the forward twin of job_overdue_cutoff(), so the same string
    comparison decides 'not open yet'."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _cadence_hours(job: sqlite3.Row) -> int:
    """A cadenced job's effective due window in hours: its cycle gap in
    days times the base FORUM_JOB_CYCLE_DUE_HOURS."""
    return int(int(job["cycle_every_days"] or 1) * int(config.JOB_CYCLE_DUE_HOURS))


def _cycle_is_overdue(
    status: str | None,
    anchor_at: str | None,
    cutoff: str,
    *,
    opens_at: str | None = None,
) -> bool:
    """True when a job's current cycle idles past the due window.

    awaiting/declined are the worker's turn (the creator has already made
    their move); 'submitted' means the ball is with the creator and never
    counts as overdue.  Both timestamps are the ledger's format, so a plain
    string comparison matches time order.  A cadenced cycle that has not
    opened yet (opens_at in the future) is never overdue - the worker has
    nothing to submit until it opens."""
    if not cutoff or status not in ("awaiting", "declined"):
        return False
    if not anchor_at:
        return False
    if opens_at:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if opens_at > now_iso:
            return False
    return anchor_at <= cutoff


def _overdue_windows_elapsed(
    anchor_at: str | None,
    cutoff: str,
    *,
    hours: int | None = None,
) -> int:
    """How many whole due windows a cycle has idled past its deadline: 0 =
    not overdue, 1 = the first window has fully elapsed, then +1 per window.
    `hours` overrides the base FORUM_JOB_CYCLE_DUE_HOURS for cadenced jobs
    (their effective window is cycle_every_days x base).  Deterministic
    from the events anchor alone (no schema column), so the release
    threshold (FORUM_JOB_OVERDUE_RELEASE_AFTER) resolves on the fly; a
    misread ledger or dead clock degrades to 0 and never releases."""
    hours = int(hours if hours is not None else config.JOB_CYCLE_DUE_HOURS)
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
    *,
    opens_at: str | None = None,
) -> bool:
    """Board-level overdue flag: the job must be ACTIVE and its current
    cycle must idle past the due window.  Completed/expired/cancelled jobs
    never read overdue, even where a leftover cycle row still sits in a
    transitional status.  A future opens_at (cadenced cycle not yet open)
    is never overdue."""
    if status != "active":
        return False
    return _cycle_is_overdue(cur_cycle_status, anchor_at, cutoff, opens_at=opens_at)


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
