"""db._jobs_ops — job creation, listing, claiming, worker ops, review.

Package (split verbatim from db/_jobs_ops.py): _helpers holds evidence
parsing, overdue math and formatting; _detail the detail assembly;
_create the intake and creation; _board the listing; _flow claiming,
worker ops and review. Shared by citizen and official paths; the
admin-only review variants and cancellation/sweep logic live in
db._jobs_admin. The public facade is db._jobs which re-exports from
both. This facade re-exports every name the old module exposed so all
existing importers (db/_jobs, db/_jobs_admin, tests) keep working
unchanged.
"""

from __future__ import annotations

from ._board import (  # noqa: F401
    _JOB_VIEWS,
    _board_total_cached,
    get_job,
    get_jobs,
    job_creator_status_counts,
    list_jobs,
)
from ._create import (  # noqa: F401
    _handle_taker_deposit,
    _insert_job_with_steps,
    _resolve_citizen,
    _validate_steps,
    _validate_taker_deposit,
    _validated_job_intake,
    create_job,
    create_job_official,
)
from ._detail import (  # noqa: F401
    _JOB_COLS,
    _detail_or_raise,
    _job_detail,
    _job_detail_from_parts,
    _job_details_batch,
    _remaining_escrow,
)
from ._flow import (  # noqa: F401
    _apply_review,
    _award_cycle_karma,
    _check_deposit_return,
    _maybe_pay_bonus,
    _pay_worker,
    _resolve_offer,
    _seed_next_cycle,
    accept_job_offer,
    claim_job,
    decline_job_offer,
    review_job,
    submit_job,
    tick_job_step,
)
from ._helpers import (  # noqa: F401
    _JOB_ANCHOR_KINDS,
    _JOB_ANCHOR_KINDS_SQL,
    _PR_RE,
    _all_prs_merged,
    _cycle_is_overdue,
    _fmt_q,
    _job_anchors_for,
    _job_overdue_anchor_sql,
    _overdue_flag,
    _overdue_windows_elapsed,
    _parse_cycle_evidence,
    _parse_pr_numbers,
    _unhold_cycle_prs,
    job_overdue_cutoff,
)
