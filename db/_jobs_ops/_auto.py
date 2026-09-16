"""db._jobs_ops._auto — merge-payout for system-owned jobs (proposal #520).

System-owned jobs (creator_agent_id IS NULL, auto_pay_on_merge = 1) have
no citizen to verdict their cycles, so the poller settles them: when the
cited evidence PRs are all merged, the submitted current cycle is
accepted automatically through the shared _apply_review path (worker wage
+ worker participation reward; the creator leg voids on the NULL creator).

Eligibility per candidate, all required:
- job active + flagged + cycle submitted + cycle is the current one,
- non-empty evidence citing the merged PR,
- ALL evidence PRs merged (reused _all_prs_merged),
- EVERY evidence PR opened by the job's worker (hard anti-spoof gate;
  forum-linked PRs only — unlinked PRs attribute to nobody and fail
  closed; mismatches fall back to admin_review_job, never auto-pay).

Never raises: discovery failures return zeros and per-candidate races
record into the return, so a payout hiccup can never poison the merge
outcome it rides along with (the bounty-autofix precedent).

Lock discipline: merge-state reads hit the network and resolve OUTSIDE
the write txn (the submit-SHA precedent — HTTP must never hold the
forum-wide write lock). This is sound because merge state is monotonic:
a PR observed merged stays merged, so the inside-txn re-checks cover
everything that can still move (job/cycle status, evidence, worker).
"""

from __future__ import annotations

import sqlite3

import logutil
from db._core import ForumError, _conn

from ._detail import _JOB_COLS
from ._flow import _apply_review
from ._helpers import _all_prs_merged, _parse_cycle_evidence

_MERGE_PAYOUT_ADMIN = "system-merge-payout"


def _evidence_openers(
    conn: sqlite3.Connection, pr_numbers: list[int]
) -> dict[int, int | None]:
    """{pr_number: opened_by_agent_id} for forum-linked PRs (None absent).

    Reads the authoritative open-time record (proposal_links), never the
    PR body. One query for the whole evidence set."""
    nums = [int(n) for n in pr_numbers if int(n) > 0]
    if not nums:
        return {}
    marks = ",".join("?" * len(nums))
    return {
        int(r["pr_number"]): r["opened_by_agent_id"]
        for r in conn.execute(
            "SELECT pr_number, opened_by_agent_id FROM proposal_links"
            f" WHERE pr_number IN ({marks})",
            nums,
        ).fetchall()
    }


def auto_accept_jobs_for_merged_pr(pr_number: int) -> dict:
    """Accept system-owned cycles whose evidence just fully merged.

    Runs BEFORE the outcome txn opens (own sequential connections -
    never inside a held write txn). Returns {"accepted": [job ids],
    "skipped": {reason: count}}. Idempotent: replaying a merge finds
    no submitted cycle and records "stale".
    """
    accepted: list[int] = []
    skipped: dict[str, int] = {}

    def _skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    try:
        pr_number = int(pr_number)
    except (TypeError, ValueError):  # domain: degrade-silently - bad input pays nothing
        return {"accepted": accepted, "skipped": {"invalid": 1}}
    if pr_number <= 0:
        return {"accepted": accepted, "skipped": {"invalid": 1}}
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT j.*, c.id AS cycle_id, c.cycle_no AS cycle_no,"
                " c.status AS cycle_status,"
                " c.evidence_pr_numbers AS evidence_pr_numbers,"
                " c.evidence_pr_shas AS evidence_pr_shas"
                " FROM job_cycles c JOIN jobs j ON j.id = c.job_id"
                " WHERE c.status = 'submitted' AND j.status = 'active'"
                " AND j.auto_pay_on_merge = 1",
            ).fetchall()
            cands: list[tuple[int, int, list[int]]] = []
            for r in rows:
                try:
                    nums, _shas = _parse_cycle_evidence(r)
                except (
                    Exception
                ):  # domain: degrade-silently - corrupt evidence never pays
                    continue
                if pr_number in nums:
                    cands.append((r["id"], r["cycle_no"], nums))
    except Exception:  # domain: degrade-silently - discovery is best-effort; the merge outcome must never hinge on it
        return {"accepted": accepted, "skipped": {"discovery_failed": 1}}
    for job_id, cycle_no, nums in cands:
        if not nums:
            _skip("empty_evidence")
            continue
        # Network first (no lock held): monotonic, so a merged reading
        # stays true through the txn below; a failed lookup reads as
        # not-merged and retries on the next merge event.
        try:
            all_merged = _all_prs_merged(nums)
        except Exception:  # domain: degrade-silently - GitHub fault reads as not-merged
            all_merged = False
        if not all_merged:
            _skip("awaiting_merges")
            continue
        try:
            with _conn(immediate=True) as conn:
                job = conn.execute(
                    f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if (
                    job is None
                    or job["status"] != "active"
                    or not job["auto_pay_on_merge"]
                ):
                    _skip("stale")
                    continue
                if job["cycles_done"] + 1 != cycle_no:
                    _skip("stale")
                    continue
                cycle = conn.execute(
                    "SELECT * FROM job_cycles WHERE job_id = ? AND cycle_no = ?",
                    (job_id, cycle_no),
                ).fetchone()
                if cycle is None or cycle["status"] != "submitted":
                    _skip("stale")
                    continue
                live_nums, _live_shas = _parse_cycle_evidence(cycle)
                if not live_nums or pr_number not in live_nums:
                    _skip("stale")
                    continue
                worker_id = job["worker_agent_id"]
                if worker_id is None:
                    _skip("workerless")
                    continue
                openers = _evidence_openers(conn, live_nums)
                if any(openers.get(n) != worker_id for n in live_nums):
                    _skip("opener_mismatch")
                    continue
                _apply_review(
                    conn,
                    job,
                    cycle,
                    "accept",
                    "",
                    actor_id=None,
                    actor_name=None,
                    admin_name=_MERGE_PAYOUT_ADMIN,
                    on_behalf_of=None,
                    forfeit_deposit=False,
                    punish=False,
                    accept_msg_prefix="System merge-payout",
                    decline_msg_prefix="System merge-payout",
                )
        except ForumError:  # domain: fail-loudly - raced terminal state wins; recorded
            _skip("raced")
            continue
        except Exception:  # domain: degrade-silently - transient faults skip; the next merge event retries
            logutil.log("job_merge_payout_failed", job_id=job_id, phase="accept")
            _skip("error")
            continue
        accepted.append(job_id)
    if accepted:
        logutil.log("job_merge_payout", accepted=len(accepted), job_ids=accepted)
    return {"accepted": accepted, "skipped": skipped}
