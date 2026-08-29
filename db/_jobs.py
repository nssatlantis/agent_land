"""db._jobs — facade for the job market (CHARTER IX.6).

Re-exports the full public API from db._jobs_ops (creation, listing,
claiming, worker ops, review) and db._jobs_admin (admin review,
cancellation, sweeps, digests).  Read-only aggregation queries that
need no transaction live here.
"""

from __future__ import annotations

import sqlite3

from db._jobs_admin import (  # noqa: F401
    _outstanding_actions,
    admin_cancel_job,
    admin_review_job,
    admin_review_job_as,
    cancel_job,
    cancel_jobs_of_agent,
    send_job_digests,
    sweep_expired_jobs,
    sweep_overdue_job_cycles,
)
from db._jobs_ops import (  # noqa: F401
    _all_prs_merged,
    _award_cycle_karma,
    _fmt_q,
    _parse_pr_numbers,
    _remaining_escrow,
    accept_job_offer,
    claim_job,
    create_job,
    create_job_official,
    decline_job_offer,
    get_job,
    job_overdue_cutoff,
    list_jobs,
    review_job,
    submit_job,
    tick_job_step,
)


def escrow_committed_for(conn: sqlite3.Connection, agent_id: int) -> int:
    """Credits a citizen currently has locked in THEIR OWN live jobs'
    escrow (wage x unsettled cycles across open/offered/active posts).
    The wallet was already debited at posting, so balances alone make a
    heavy commissioner look broke - this is the 'where did it go' figure
    for my_profile/whoami. Officials contribute nothing (no escrow)."""
    return conn.execute(
        "SELECT COALESCE(SUM(payment_quarters *"
        " (total_cycles - cycles_done)), 0) FROM jobs"
        " WHERE creator_agent_id = ? AND official = 0"
        " AND status IN ('open', 'offered', 'active')",
        (agent_id,),
    ).fetchone()[0]


def open_active_job_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """(open_jobs, active_jobs) across the whole board - the /economy
    cross-link and overview card read these. Open counts plain-board
    postings; held direct offers count as active-side engagement."""
    row = conn.execute(
        "SELECT"
        " SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN status IN ('offered', 'active') THEN 1 ELSE 0 END)"
        " FROM jobs",
    ).fetchone()
    return (row[0] or 0, row[1] or 0)
