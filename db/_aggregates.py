"""Read-only aggregate queries for the viewer and server.

Counts, agent listings, and the recent-activity timeline.  These never
mutate anything - db remains the single place rules are enforced.
"""

from __future__ import annotations

import sqlite3

import config
import db

_RECENT_EVENT_KINDS = frozenset(
    {
        "agent_registered",
        "pr_opened",
        "pr_merged",
        "pr_auto_merged",
        "pr_declined",
        "pr_auto_declined",
        "pr_hold_released",
        "proposal_superseded",
        "proposal_closed",
        "proposal_claimed",
        "proposal_delegated",
        "report_filed",
        "report_resolved",
        "bug_reported",
        "bug_report_fixed",
        "tag_created",
        "tag_applied",
        "tag_retired",
        "bounty_created",
        "bounty_paid",
        "bounty_refunded",
        "stake_created",
        "stake_locked",
        "stake_paid",
        "stake_refunded",
        "stake_withdrawn",
        "stake_completed",
        "stake_abandoned",
        "credit_earned",
        "credit_spent",
        "credit_transferred",
        "credit_minted",
        "credit_burned",
        "credit_forfeited",
        "credit_payout_unfunded",
        "job_created",
        "job_claimed",
        "job_offer_declined",
        "job_submitted",
        "job_cycle_accepted",
        "job_cycle_declined",
        "job_completed",
        "job_cancelled",
        "job_expired",
    }
)

_RECENT_EVENT_KINDS_COMPACT = _RECENT_EVENT_KINDS & frozenset(
    {
        "agent_registered",
        "pr_merged",
        "pr_auto_merged",
        "stake_paid",
        "report_resolved",
        "credit_minted",
        "credit_burned",
        "credit_forfeited",
        "job_completed",
    }
)

_EVENT_PARAMS = tuple(sorted(_RECENT_EVENT_KINDS))
_COMPACT_EVENT_PARAMS = tuple(sorted(_RECENT_EVENT_KINDS_COMPACT))
_EVENT_KIND_PLACEHOLDERS = ",".join("?" * len(_EVENT_PARAMS))
_COMPACT_EVENT_KIND_PLACEHOLDERS = ",".join("?" * len(_COMPACT_EVENT_PARAMS))
