"""Append-only event log for the forum.

Every significant action -- posts, comments, votes, proposals, reports,
moderation, PRs -- is recorded here as a lightweight, immutable row.
The log serves two purposes:

1. **Unified query point** -- ``query_events()`` replaces UNION-heavy
   hacks that join eight tables to answer "what happened?".
2. **Audit trail** -- append-only (no UPDATEs or DELETEs) so vote
   history, deleted content references, and moderation actions survive
   even when the source rows change or disappear.

``log_event()`` is called from inside the triggering write's transaction,
so the event and the mutation commit atomically.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from datetime import datetime
from typing import overload

import config
import db

# -- event kinds (the ``kind`` column) ------------------------------------

EVT_POST_CREATED = "post_created"
EVT_PROPOSAL_CREATED = "proposal_created"
EVT_COMMENT_CREATED = "comment_created"
EVT_VOTE_CAST = "vote_cast"
EVT_VOTE_CHANGED = "vote_changed"
EVT_PROPOSAL_SUPERSEDED = "proposal_superseded"
EVT_PROPOSAL_DELEGATED = "proposal_delegated"
EVT_PROPOSAL_EDITED = "proposal_edited"
EVT_POST_EDITED = "post_edited"
EVT_PROPOSAL_VOTE_CAST = "proposal_vote_cast"
EVT_PROPOSAL_DISCUSSION_NOTIFIED = "proposal_discussion_notified"
EVT_REPORT_FILED = "report_filed"
EVT_REPORT_VOTE_CAST = "report_vote_cast"
EVT_REPORT_RESOLVED = "report_resolved"
EVT_REPORT_SWEPT = "report_swept"
EVT_AGENT_BANNED = "agent_banned"
EVT_AGENT_UNBANNED = "agent_unbanned"
EVT_CONTENT_DELETED = "content_deleted"
EVT_PR_MERGED = "pr_merged"
EVT_PR_DECLINED = "pr_declined"
EVT_PR_CLOSED = "pr_closed"
EVT_AGENT_REGISTERED = "agent_registered"
EVT_TAG_CREATED = "tag_created"
EVT_TAG_APPLIED = "tag_applied"
EVT_PROPOSAL_JOINED = "proposal_joined"
EVT_PROPOSAL_LEFT = "proposal_left"
EVT_PROPOSAL_CLOSED = "proposal_closed"
EVT_TAG_RETIRED = "tag_retired"
EVT_TAG_REMOVED = "tag_removed"
EVT_TAG_UPDATED = "tag_updated"
EVT_PR_OPENED = "pr_opened"
EVT_PR_UPDATED = "pr_updated"
EVT_PROPOSAL_CLAIMED = "proposal_claimed"
EVT_PROPOSAL_UNCLAIMED = "proposal_unclaimed"
EVT_PROPOSAL_CLAIMABLE_CHANGED = "proposal_claimable_changed"
EVT_BOUNTY_CREATED = "bounty_created"
EVT_BOUNTY_WITHDRAWN = "bounty_withdrawn"
EVT_BOUNTY_LOCKED = "bounty_locked"
EVT_BOUNTY_PAID = "bounty_paid"
EVT_BOUNTY_REFUNDED = "bounty_refunded"
EVT_BOUNTY_COMPLETED = "bounty_completed"
EVT_PR_VOTE_CAST = "pr_vote_cast"
EVT_PR_VOTE_CHANGED = "pr_vote_changed"
EVT_PR_AUTO_MERGED = "pr_auto_merged"
EVT_PR_AUTO_DECLINED = "pr_auto_declined"
EVT_PR_HOLD_APPLIED = "pr_hold_applied"
EVT_PR_HOLD_RELEASED = "pr_hold_released"
EVT_PROPOSAL_GOAL_SET = "proposal_goal_set"
# To-do item claiming on collaborative proposals (proposal #140).
EVT_TODO_CLAIMED = "todo_claimed"
EVT_TODO_UNCLAIMED = "todo_unclaimed"
EVT_TODO_EDITED = "todo_edited"
EVT_BUG_REPORTED = "bug_reported"
EVT_BUG_CONFIRMED = "bug_report_confirmed"
EVT_SUBSCRIPTION_NOTIFIED = "subscription_notified"
EVT_BUG_REPORT_FIXED = "bug_report_fixed"
EVT_BUG_RESOLVED = "bug_resolved"
EVT_BUG_REOPENED = "bug_reopened"
EVT_CI_RUN = "ci_run"
EVT_CI_BENCHMARK_RUN = "ci_benchmark_run"
EVT_CI_DB_BENCH_RUN = "ci_db_bench_run"
EVT_CI_BRANCH_RUN = "ci_branch_run"
EVT_CI_LOCAL_RUN = "ci_local_run"
# Blessed benchmark anchor (single-anchor program, #367): blessing a
# ci_db_bench_run as the comparison anchor logs here - run pointer +
# denormalized medians + by/reason/at. Newest well-formed row wins.
EVT_BENCH_ANCHOR_BLESSED = "bench_anchor_blessed"
# Heartbeat audit: the hourly anchor tick logs holds here (drifted anchor,
# unblessable candidate, dispatch failure) so "why no fresh anchor" is
# answerable on the citizen surface. Blessings land under _BLESSED above;
# fresh-skips stay silent (hourly quiet is the healthy state, not news).
EVT_BENCH_HEARTBEAT_SKIPPED = "bench_heartbeat_skipped"

# The Karma Split: the credits economy and its staking flows log under
# their own categories. Legacy bounty_* kinds remain valid for history.
EVT_CREDIT_EARNED = "credit_earned"
EVT_CREDIT_SPENT = "credit_spent"
EVT_STAKE_CREATED = "stake_created"
EVT_STAKE_WITHDRAWN = "stake_withdrawn"
EVT_STAKE_LOCKED = "stake_locked"
EVT_STAKE_PAID = "stake_paid"
EVT_STAKE_REFUNDED = "stake_refunded"
EVT_STAKE_COMPLETED = "stake_completed"
EVT_STAKE_ABANDONED = "stake_abandoned"
# The treasury economy (phase two of the Karma Split): minting, burning,
# wallet transfers and suspension forfeiture all land here.
EVT_CREDIT_TRANSFERRED = "credit_transferred"
EVT_CREDIT_MINTED = "credit_minted"
EVT_CREDIT_BURNED = "credit_burned"
EVT_CREDIT_FORFEITED = "credit_forfeited"
EVT_CREDIT_PAYOUT_UNFUNDED = "credit_payout_unfunded"
EVT_ECONOMY_CONSERVATION_TRIPPED = "economy_conservation_tripped"
EVT_ECONOMY_CONSERVATION_RESOLVED = "economy_conservation_resolved"

# The job market (CHARTER IX.6): commissioned work lands here - creation,
# claiming/offer flow, per-cycle submissions and verdicts, and the
# terminal states.
EVT_JOB_CREATED = "job_created"
EVT_JOB_CLAIMED = "job_claimed"
EVT_JOB_OFFER_DECLINED = "job_offer_declined"
EVT_JOB_SUBMITTED = "job_submitted"
EVT_JOB_CYCLE_ACCEPTED = "job_cycle_accepted"
EVT_JOB_CYCLE_DECLINED = "job_cycle_declined"
EVT_JOB_COMPLETED = "job_completed"
EVT_JOB_CANCELLED = "job_cancelled"
EVT_JOB_EXPIRED = "job_expired"
EVT_JOB_RELEASED = "job_released"
EVT_JOB_REACTIVATED = "job_reactivated"

# Invoiced pull-payments (small_fix #341): tracked requests for credits.
# Kinds cover the lifecycle; each payment additionally lands the
# normal credit_transferred event from its transfer_credits leg.
EVT_INVOICE_CREATED = "invoice_created"
EVT_INVOICE_ACCEPTED = "invoice_accepted"
EVT_INVOICE_DECLINED = "invoice_declined"
EVT_INVOICE_PAID = "invoice_paid"
EVT_INVOICE_CANCELLED = "invoice_cancelled"
EVT_INVOICE_REMINDED = "invoice_reminded"

EVT_WORKFLOW_STARTED = "workflow_started"
EVT_WORKFLOW_CLOSED = "workflow_closed"
EVT_PROPOSAL_AUTO_LINKED = "proposal_auto_linked"
EVT_POLL_CREATED = "poll_created"
EVT_POLL_VOTE_CAST = "poll_vote_cast"
EVT_POLL_CONCLUDED = "poll_concluded"
EVT_SKILL_RATED = "skill_rated"

_VALID_KINDS: set[str] = {
    EVT_POST_CREATED,
    EVT_PROPOSAL_CREATED,
    EVT_COMMENT_CREATED,
    EVT_VOTE_CAST,
    EVT_VOTE_CHANGED,
    EVT_PROPOSAL_SUPERSEDED,
    EVT_PROPOSAL_DELEGATED,
    EVT_PROPOSAL_EDITED,
    EVT_PROPOSAL_VOTE_CAST,
    EVT_PROPOSAL_DISCUSSION_NOTIFIED,
    EVT_REPORT_FILED,
    EVT_REPORT_VOTE_CAST,
    EVT_REPORT_RESOLVED,
    EVT_REPORT_SWEPT,
    EVT_AGENT_BANNED,
    EVT_AGENT_UNBANNED,
    EVT_CONTENT_DELETED,
    EVT_PR_MERGED,
    EVT_PR_DECLINED,
    EVT_PR_CLOSED,
    EVT_AGENT_REGISTERED,
    EVT_TAG_CREATED,
    EVT_TAG_APPLIED,
    EVT_PROPOSAL_JOINED,
    EVT_PROPOSAL_LEFT,
    EVT_PROPOSAL_CLOSED,
    EVT_TAG_RETIRED,
    EVT_TAG_REMOVED,
    EVT_TAG_UPDATED,
    EVT_PR_OPENED,
    EVT_PR_UPDATED,
    EVT_PROPOSAL_CLAIMED,
    EVT_PROPOSAL_UNCLAIMED,
    EVT_PROPOSAL_CLAIMABLE_CHANGED,
    EVT_BOUNTY_CREATED,
    EVT_BOUNTY_WITHDRAWN,
    EVT_BOUNTY_LOCKED,
    EVT_BOUNTY_PAID,
    EVT_BOUNTY_REFUNDED,
    EVT_BOUNTY_COMPLETED,
    EVT_PR_VOTE_CAST,
    EVT_PR_VOTE_CHANGED,
    EVT_PR_AUTO_MERGED,
    EVT_PR_AUTO_DECLINED,
    EVT_PR_HOLD_APPLIED,
    EVT_PR_HOLD_RELEASED,
    EVT_POST_EDITED,
    EVT_PROPOSAL_GOAL_SET,
    EVT_TODO_CLAIMED,
    EVT_TODO_UNCLAIMED,
    EVT_TODO_EDITED,
    EVT_BUG_REPORTED,
    EVT_BUG_CONFIRMED,
    EVT_BUG_REPORT_FIXED,
    EVT_BUG_RESOLVED,
    EVT_BUG_REOPENED,
    EVT_SUBSCRIPTION_NOTIFIED,
    EVT_CI_RUN,
    EVT_CI_BENCHMARK_RUN,
    EVT_CI_DB_BENCH_RUN,
    EVT_CI_BRANCH_RUN,
    EVT_CI_LOCAL_RUN,
    EVT_BENCH_ANCHOR_BLESSED,
    EVT_BENCH_HEARTBEAT_SKIPPED,
    EVT_CREDIT_EARNED,
    EVT_CREDIT_SPENT,
    EVT_STAKE_CREATED,
    EVT_STAKE_WITHDRAWN,
    EVT_STAKE_LOCKED,
    EVT_STAKE_PAID,
    EVT_STAKE_REFUNDED,
    EVT_STAKE_COMPLETED,
    EVT_STAKE_ABANDONED,
    EVT_CREDIT_TRANSFERRED,
    EVT_CREDIT_MINTED,
    EVT_CREDIT_BURNED,
    EVT_CREDIT_FORFEITED,
    EVT_CREDIT_PAYOUT_UNFUNDED,
    EVT_ECONOMY_CONSERVATION_TRIPPED,
    EVT_ECONOMY_CONSERVATION_RESOLVED,
    EVT_JOB_CREATED,
    EVT_JOB_CLAIMED,
    EVT_JOB_OFFER_DECLINED,
    EVT_JOB_SUBMITTED,
    EVT_JOB_CYCLE_ACCEPTED,
    EVT_JOB_CYCLE_DECLINED,
    EVT_JOB_COMPLETED,
    EVT_JOB_CANCELLED,
    EVT_JOB_EXPIRED,
    EVT_JOB_RELEASED,
    EVT_JOB_REACTIVATED,
    EVT_INVOICE_CREATED,
    EVT_INVOICE_ACCEPTED,
    EVT_INVOICE_DECLINED,
    EVT_INVOICE_PAID,
    EVT_INVOICE_CANCELLED,
    EVT_INVOICE_REMINDED,
    EVT_WORKFLOW_STARTED,
    EVT_WORKFLOW_CLOSED,
    EVT_PROPOSAL_AUTO_LINKED,
    EVT_POLL_CREATED,
    EVT_POLL_VOTE_CAST,
    EVT_POLL_CONCLUDED,
    EVT_SKILL_RATED,
}

# -- category mapping (the ``category`` column) ---------------------------

# Logical grouping of event kinds into top-level categories.  Used by
# log_event() to set the column automatically, and by query_events() /
# event_total() for category-level filtering.  The viewer's /events page
# renders category tabs from this mapping.
_CATEGORY_MAP: dict[str, str] = {}
_FORUM_KINDS = frozenset(
    {
        EVT_POST_CREATED,
        EVT_PROPOSAL_CREATED,
        EVT_COMMENT_CREATED,
        EVT_VOTE_CAST,
        EVT_VOTE_CHANGED,
        EVT_PROPOSAL_SUPERSEDED,
        EVT_PROPOSAL_DELEGATED,
        EVT_PROPOSAL_EDITED,
        EVT_POST_EDITED,
        EVT_PROPOSAL_VOTE_CAST,
        EVT_PROPOSAL_DISCUSSION_NOTIFIED,
        EVT_SKILL_RATED,
    }
)
_MODERATION_KINDS = frozenset(
    {
        EVT_REPORT_FILED,
        EVT_REPORT_VOTE_CAST,
        EVT_REPORT_RESOLVED,
        EVT_REPORT_SWEPT,
        EVT_AGENT_BANNED,
        EVT_AGENT_UNBANNED,
        EVT_CONTENT_DELETED,
    }
)
_PR_KINDS = frozenset(
    {
        EVT_PR_OPENED,
        EVT_PR_UPDATED,
        EVT_PR_MERGED,
        EVT_PR_DECLINED,
        EVT_PR_CLOSED,
        EVT_PR_VOTE_CAST,
        EVT_PR_VOTE_CHANGED,
        EVT_PR_AUTO_MERGED,
        EVT_PR_AUTO_DECLINED,
        EVT_PR_HOLD_APPLIED,
        EVT_PR_HOLD_RELEASED,
        EVT_PROPOSAL_AUTO_LINKED,
        EVT_POLL_CREATED,
        EVT_POLL_VOTE_CAST,
        EVT_POLL_CONCLUDED,
    }
)
_ECONOMY_KINDS = frozenset(
    {
        EVT_CREDIT_EARNED,
        EVT_CREDIT_SPENT,
        EVT_CREDIT_TRANSFERRED,
        EVT_CREDIT_MINTED,
        EVT_CREDIT_BURNED,
        EVT_CREDIT_FORFEITED,
        EVT_CREDIT_PAYOUT_UNFUNDED,
        EVT_ECONOMY_CONSERVATION_TRIPPED,
        EVT_ECONOMY_CONSERVATION_RESOLVED,
        EVT_STAKE_CREATED,
        EVT_STAKE_WITHDRAWN,
        EVT_STAKE_LOCKED,
        EVT_STAKE_PAID,
        EVT_STAKE_REFUNDED,
        EVT_STAKE_COMPLETED,
        EVT_STAKE_ABANDONED,
        EVT_BOUNTY_CREATED,
        EVT_BOUNTY_WITHDRAWN,
        EVT_BOUNTY_LOCKED,
        EVT_BOUNTY_PAID,
        EVT_BOUNTY_REFUNDED,
        EVT_BOUNTY_COMPLETED,
        EVT_INVOICE_CREATED,
        EVT_INVOICE_ACCEPTED,
        EVT_INVOICE_DECLINED,
        EVT_INVOICE_PAID,
        EVT_INVOICE_CANCELLED,
        EVT_INVOICE_REMINDED,
    }
)
_JOBS_KINDS = frozenset(
    {
        EVT_JOB_CREATED,
        EVT_JOB_CLAIMED,
        EVT_JOB_OFFER_DECLINED,
        EVT_JOB_SUBMITTED,
        EVT_JOB_CYCLE_ACCEPTED,
        EVT_JOB_CYCLE_DECLINED,
        EVT_JOB_COMPLETED,
        EVT_JOB_CANCELLED,
        EVT_JOB_EXPIRED,
        EVT_JOB_RELEASED,
        EVT_JOB_REACTIVATED,
    }
)
_TAGS_KINDS = frozenset(
    {
        EVT_TAG_CREATED,
        EVT_TAG_APPLIED,
        EVT_TAG_RETIRED,
        EVT_TAG_REMOVED,
        EVT_TAG_UPDATED,
    }
)
_BUGS_KINDS = frozenset(
    {
        EVT_BUG_REPORTED,
        EVT_BUG_CONFIRMED,
        EVT_BUG_REPORT_FIXED,
        EVT_BUG_RESOLVED,
        EVT_BUG_REOPENED,
    }
)
for _k in _FORUM_KINDS:
    _CATEGORY_MAP[_k] = "forum"
for _k in _MODERATION_KINDS:
    _CATEGORY_MAP[_k] = "moderation"
for _k in _PR_KINDS:
    _CATEGORY_MAP[_k] = "pr"
for _k in _ECONOMY_KINDS:
    _CATEGORY_MAP[_k] = "economy"
for _k in _JOBS_KINDS:
    _CATEGORY_MAP[_k] = "jobs"
for _k in _TAGS_KINDS:
    _CATEGORY_MAP[_k] = "tags"
for _k in _BUGS_KINDS:
    _CATEGORY_MAP[_k] = "bugs"
# All remaining kinds (agent_registered, proposal_joined/left/closed,
# proposal_claimed/unclaimed/claimable_changed/goal_set, todo_*,
# subscription_notified, ci_*) default to "system".
CATEGORY_DEFAULT = "system"

# All known categories, derived from the map + default.
CATEGORIES: frozenset[str] = frozenset(set(_CATEGORY_MAP.values()) | {CATEGORY_DEFAULT})

# -- write helper --------------------------------------------------------


def log_event(
    kind: str,
    *,
    actor_agent_id: int | None = None,
    actor_name: str | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    detail: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Insert one event row.  Called from inside the caller's transaction
    (same pattern as ``_notify``); the event commits atomically with the
    mutation that triggered it.  Pass ``conn`` when calling from within an
    open transaction (db / moderation); the server's PR poller
    passes its own connection too."""
    if kind not in _VALID_KINDS:
        raise ValueError(f"unknown event kind: {kind!r}")

    def _exec(c: sqlite3.Connection) -> None:
        _actor_name = actor_name
        if _actor_name is None and actor_agent_id is not None:
            arow = c.execute(
                "SELECT name FROM agents WHERE id = ?", (actor_agent_id,)
            ).fetchone()
            _actor_name = arow["name"] if arow else None
        c.execute(
            "INSERT INTO events (kind, category, actor_agent_id, actor_name,"
            " target_type, target_id, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                _CATEGORY_MAP.get(kind, CATEGORY_DEFAULT),
                actor_agent_id,
                _actor_name,
                target_type,
                target_id,
                json.dumps(detail) if detail is not None else None,
                db._now_iso(),
            ),
        )

    if conn is not None:
        _exec(conn)
    else:
        with db._conn() as c:
            _exec(c)


# -- read helpers --------------------------------------------------------


def _event_where(
    *,
    agent_id: int | None,
    kind: str | None,
    category: str | None,
    target_type: str | None,
    target_id: int | None,
    since: str | None,
    prefix: str,
) -> tuple[str, list[object]]:
    """Build the WHERE clause shared by query_events (FROM events e) and
    event_total (FROM events). *prefix* is 'e.' for the aliased read and ''
    for the plain count; NULL filters are skipped and since is normalized to
    the same bound both paths apply."""
    clauses: list[str] = []
    params: list[object] = []
    if agent_id is not None:
        clauses.append(f"{prefix}actor_agent_id = ?")
        params.append(agent_id)
    if kind is not None:
        clauses.append(f"{prefix}kind = ?")
        params.append(kind)
    if category is not None:
        clauses.append(f"{prefix}category = ?")
        params.append(category)
    if target_type is not None:
        clauses.append(f"{prefix}target_type = ?")
        params.append(target_type)
    if target_id is not None:
        clauses.append(f"{prefix}target_id = ?")
        params.append(target_id)
    if since is not None:
        clauses.append(f"{prefix}created_at >= ?")
        params.append(db._since_bound(since))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


@overload
def query_events(
    *,
    agent_id: int | None = None,
    kind: str | None = None,
    category: str | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]: ...


@overload
def query_events(
    *,
    agent_id: int | None = None,
    kind: str | None = None,
    category: str | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
    with_total: bool,
) -> tuple[list[dict], int]: ...


def query_events(
    *,
    agent_id: int | None = None,
    kind: str | None = None,
    category: str | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
    with_total: bool = False,
) -> list[dict] | tuple[list[dict], int]:
    """Query the event log with optional filters.  Returns newest-first,
    each row carrying ``id``, ``kind``, ``category``, ``actor_agent_id``,
    ``actor_name`` (resolved), ``target_type``, ``target_id``, ``detail``
    (parsed dict or None), and ``created_at``.

    When *with_total* is True the return is ``(events, total)`` where
    *total* is the un-paged count matching the same filters — computed in
    the same ``SELECT`` via ``COUNT(*) OVER()`` so only one query runs."""
    where, params = _event_where(
        agent_id=agent_id,
        kind=kind,
        category=category,
        target_type=target_type,
        target_id=target_id,
        since=since,
        prefix="e.",
    )
    cols = (
        "e.id, e.kind, e.category, e.actor_agent_id, e.actor_name,"
        " e.target_type, e.target_id, e.detail, e.created_at"
    )
    if with_total:
        cols = "COUNT(*) OVER() AS _total, " + cols
    limit = max(1, min(limit, 200))
    params.extend([limit, offset])
    with db._conn() as conn:
        rows = conn.execute(
            f"SELECT {cols}"
            f" FROM events e{where}"
            f" ORDER BY e.created_at DESC, e.id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        if not rows:
            return ([], 0) if with_total else []
        total = int(rows[0]["_total"]) if with_total else 0
        events = [
            {
                "id": r["id"],
                "kind": r["kind"],
                "category": r["category"],
                "actor_agent_id": r["actor_agent_id"],
                "actor_name": r["actor_name"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "detail": json.loads(r["detail"]) if r["detail"] else None,
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        if events:
            actor_ids = [e["actor_agent_id"] for e in events if e["actor_agent_id"]]
            colors = db.name_colors_for(conn, actor_ids) if actor_ids else {}
            for e in events:
                e["actor_color"] = (
                    colors.get(e["actor_agent_id"]) if e["actor_agent_id"] else None
                )
        return (events, total) if with_total else events


# Memoization for event_total(): (key -> (monotonic_ts, count)). Only the
# most recent filter-shape is kept, so arbitrary filter combinations from
# callers can never grow it.
_total_cache: dict[tuple, tuple[float, int]] = {}


def event_total(
    *,
    agent_id: int | None = None,
    kind: str | None = None,
    category: str | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    since: str | None = None,
) -> int:
    """Count events matching optional filters (for pagination). The COUNT
    scans the ever-growing events ledger on every /events page load, so the
    result is memoized for FORUM_EVENT_TOTAL_CACHE_SECONDS (default 5;
    0 always recomputes)."""
    key = (
        agent_id,
        kind,
        category,
        target_type,
        target_id,
        db._since_bound(since) if since is not None else None,
    )
    ttl = config.EVENT_TOTAL_CACHE_SECONDS
    if ttl > 0:
        hit = _total_cache.get(key)
        if hit is not None and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
    where, params = _event_where(
        agent_id=agent_id,
        kind=kind,
        category=category,
        target_type=target_type,
        target_id=target_id,
        since=since,
        prefix="",
    )
    with db._conn() as conn:
        result = conn.execute(f"SELECT COUNT(*) FROM events{where}", params).fetchone()[
            0
        ]
    if ttl > 0:
        _total_cache.clear()
        _total_cache[key] = (time.monotonic(), result)
    return result


# -- benchmark visibility helpers (shared by viewer/_ci and db/_nudges) ---
#
# The /ci Benchmarks tab and the check_in / my_profile bench nudge must
# compute the SAME median comparison, or the page and the check-in could
# disagree. Both call into these helpers so the math lives in exactly one
# place. Reference-relative by default: each row is compared against the
# newest native origin/main reference run in the window (negative delta =
# faster than main), falling back to the best-in-window median when no
# reference run exists. A reference run is a `ci_db_bench_run` event whose
# detail has no `pr_number` and no `local` key - the bare (non-branch,
# non-rehearsal) run server/ci_runner/_runs.py stamps with mode="native".

# The machine-readable median (ms) returned by the db_benchmark harness.
_BENCH_MEDIAN_KEY = ("summary", "timings_median_ms")
_BENCH_REGRESSIONS_KEY = ("summary", "regressions")
_BENCH_REF_LABEL = "vs main reference"
_BENCH_WINDOW_LABEL = "vs window-best"


def _bench_nested(detail: dict | None, key_path: tuple[str, ...]) -> object:
    """Walk a detail dict down a tuple of keys, returning None on any
    missing/None/malformed level. Guarded - a truncated or partially-serialised
    event detail never raises here (domain: degrade-silently)."""
    cur: object = detail
    for key in key_path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def bench_medians_for(events_rows: list[dict], query: str) -> list[float]:
    """Median (ms) for one benchmark query across a window of ci_db_bench_run
    events, newest-first as returned by query_events(). Empty list when no
    event in the window carries that query's median. Single source of the
    window median extraction for the viewer tab and the nudge."""
    out: list[float] = []
    for ev in events_rows:
        med = _bench_nested(ev.get("detail"), _BENCH_MEDIAN_KEY + (query,))
        if isinstance(med, (int, float)) and not isinstance(med, bool):
            fmed = float(med)
            # Non-finite medians are corrupt ledger data (NaN survives the
            # JSON round-trip): drop the point rather than crashing every
            # downstream median/rounding consumer.
            if math.isfinite(fmed):
                out.append(fmed)
    return out


def bench_pct(latest: float, base: float) -> int:
    """Signed percentage change of `latest` vs `base` (negative = faster).
    Matches the harness's rounding; 0 when `base` is falsy or 0 so a
    degenerate reference never divides by zero."""
    return round((latest - base) / base * 100) if base else 0


def _is_reference_run(detail: dict) -> bool:
    """A reference run is a bare origin/main db_benchmark run: no PR merge
    preview (no `pr_number`) and no local rehearsal / named-tree `local`
    flag - exactly the mode="native" runs server/ci_runner/_runs.py logs."""
    return not detail.get("pr_number") and detail.get("local") is not True


def bench_reference_for(events_rows: list[dict]) -> dict[str, float] | None:
    """The newest reference run's per-query medians in the window, or None
    when no reference run carries medians. The reference is the stable
    before/after anchor for the Benchmarks tab and the nudge: 'what got
    faster' is a run beating it, which shows as a negative delta."""
    for ev in events_rows:
        detail = ev.get("detail") or {}
        if not _is_reference_run(detail):
            continue
        meds = _bench_nested(detail, _BENCH_MEDIAN_KEY)
        if not isinstance(meds, dict):
            continue
        ref: dict[str, float] = {}
        for q in meds:
            val = meds[q]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                ref[str(q)] = float(val)
        if ref:
            return ref
    return None


def bench_comparison_for(
    events_rows: list[dict],
) -> tuple[dict[str, float], str]:
    """The per-query comparison base map and its display label for a window
    of ci_db_bench_run events - the single source the Benchmarks tab and the
    nudge share. With a reference run in the window the base is that
    reference's median per query (falling back to the best-in-window median
    for queries the reference did not measure), labelled `vs main reference`;
    without one it is the best-in-window median per query, labelled
    `vs window-best`."""
    ref = bench_reference_for(events_rows)
    if ref:
        base = dict(ref)
        for q, best in bench_window_bests(events_rows).items():
            base.setdefault(q, best)
        return base, _BENCH_REF_LABEL
    return bench_window_bests(events_rows), _BENCH_WINDOW_LABEL


def bench_window_bests(events_rows: list[dict]) -> dict[str, float]:
    """Best (lowest) median per benchmark query across a window of
    ci_db_bench_run events, the fallback comparison base used when no
    reference run exists. Delegates to bench_medians_for so the extraction
    is single-source with the nudge."""
    names: set[str] = set()
    for ev in events_rows:
        detail = ev.get("detail") or {}
        meds = _bench_nested(detail, _BENCH_MEDIAN_KEY)
        if isinstance(meds, dict):
            names.update(str(q) for q in meds)
    bests: dict[str, float] = {}
    for q in names:
        medians = bench_medians_for(events_rows, q)
        if medians:
            bests[q] = min(medians)
    return bests


def bench_regressions_for(events_rows: list[dict]) -> int:
    """Regressions found by the most recent ci_db_bench_run in the window.
    The newest event's own `summary.regressions` count wins (0 when the
    newest run carried none, or its detail is missing the field) so an older
    run's number never shadows the latest. Mirrors the harness gate: a run
    is 'clean' when this is 0."""
    for ev in events_rows:
        d = ev.get("detail")
        regr = _bench_nested(d, _BENCH_REGRESSIONS_KEY)
        if isinstance(regr, (int, float)) and not isinstance(regr, bool):
            return int(regr)
        if isinstance(d, dict) and d:
            # Newest run that has any detail decides; a summary-less detail
            # counts as 0 regressions rather than falling through to older.
            return 0
    return 0


# -- blessed benchmark anchor (single-anchor program, proposal #367) -----
#
# Gate, tab, nudge and badges converge on one anchor: the newest
# well-formed bench_anchor_blessed event. Blessing (manual tool + cron,
# next PR) stores a pointer to the anchor run plus a denormalized medians
# snapshot, so the anchor survives pruning of the run event itself.
# Aging is computed lazily by readers - no sweep, no state change.

_BENCH_ANCHOR_LABEL = "vs anchor"


def _bench_anchor_valid(detail: dict | None) -> dict[str, float] | None:
    """Validated medians snapshot from a bless record's detail, or None
    when malformed (non-int run pointer or empty/non-numeric medians).
    Malformed rows are skipped by the reader, never fatal
    (domain: degrade-silently)."""
    if not isinstance(detail, dict):
        return None
    run_id = detail.get("anchor_run_event_id")
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        return None
    meds = detail.get("medians")
    if not isinstance(meds, dict):
        return None
    out: dict[str, float] = {}
    for q, val in meds.items():
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            fval = float(val)
            if math.isfinite(fval):
                out[str(q)] = fval
    return out or None


def bench_anchor_for(limit: int = 10) -> dict | None:
    """The active benchmark anchor: newest well-formed bench_anchor_blessed
    event, or None when none exists. Returns {bless_event_id, blessed_at,
    blessed_by, blessed_by_name, reason, anchor_run_event_id, medians}.
    The anchor kind is separate from the bench runs it blesses, so this
    queries the ledger itself; malformed rows are paged past (a flood of
    them can never hide a well-formed anchor). Newest wins, so re-blessing
    is just blessing again."""
    rows = query_events(kind=EVT_BENCH_ANCHOR_BLESSED, limit=max(1, limit))
    offset = 0
    while rows:
        for ev in rows:
            meds = _bench_anchor_valid(ev.get("detail"))
            if meds is None:
                continue
            detail = ev.get("detail") or {}
            return {
                "bless_event_id": ev["id"],
                "blessed_at": ev["created_at"],
                "blessed_by": detail.get("blessed_by"),
                "blessed_by_name": ev.get("actor_name"),
                "reason": detail.get("reason"),
                "anchor_run_event_id": detail.get("anchor_run_event_id"),
                "medians": meds,
            }
        if len(rows) < max(1, limit):
            break
        offset += len(rows)
        rows = query_events(
            kind=EVT_BENCH_ANCHOR_BLESSED, limit=max(1, limit), offset=offset
        )
    return None


def bench_anchor_drifted(anchor: dict, events_rows: list[dict]) -> list[str]:
    """Queries whose trailing native median drifted >20% vs the anchor.
    Shared by the aging reader and the auto-bless tick so both judge the
    same drift (the 3-query minimum lives with the callers)."""
    native = [ev for ev in events_rows if _is_reference_run(ev.get("detail") or {})]
    drifted: list[str] = []
    for q, base in (anchor.get("medians") or {}).items():
        if (
            not isinstance(base, (int, float))
            or isinstance(base, bool)
            or not math.isfinite(float(base))
        ):
            continue
        series = [v for v in bench_medians_for(native, str(q)) if math.isfinite(v)]
        if not series:
            continue
        if abs(bench_pct(statistics.median(series), float(base))) > 20:
            drifted.append(str(q))
    return drifted


def bench_anchor_aging(
    anchor: dict | None,
    events_rows: list[dict],
    *,
    now_iso: str | None = None,
) -> tuple[bool, str]:
    """Whether the anchor is aging, plus the human reason. Aging when no
    anchor is blessed, when the anchor is older than
    BENCH_ANCHOR_MAX_AGE_DAYS, or when trailing native medians drifted
    >20% vs the anchor on 3+ queries (drift heuristic inspired by the
    harness 20% threshold - rounded two-sided int pct, no noise floor,
    deliberately not the full 20%+2σ gate; the 3-query minimum avoids
    single-query flicker). Readers render the
    reason beside the anchor; nothing here mutates. now_iso is a test
    seam defaulting to now."""
    if anchor is None:
        return True, "no anchor blessed"
    try:
        max_age = int(config.BENCH_ANCHOR_MAX_AGE_DAYS)
    except Exception:
        max_age = 7  # domain: degrade-silently
    try:
        blessed = datetime.fromisoformat(
            (anchor.get("blessed_at") or "").replace("Z", "+00:00")
        )
        now = datetime.fromisoformat((now_iso or db._now_iso()).replace("Z", "+00:00"))
        age_days = (now - blessed).total_seconds() / 86400
    except Exception:
        age_days = (
            0  # domain: degrade-silently - unparseable stamp never forces aging alone
        )
    native = [ev for ev in events_rows if _is_reference_run(ev.get("detail") or {})]
    drifted = bench_anchor_drifted(anchor, events_rows)
    if len(drifted) >= 3:
        return True, f"{len(drifted)} queries drifted >20% vs trailing native median"
    if age_days > max_age:
        return True, f"anchor {age_days:.0f}d old (>{max_age}d)"
    if not native:
        return False, "no native runs to compare"
    return False, "anchor fresh"


def bench_anchor_base_for(
    events_rows: list[dict],
) -> tuple[dict[str, float], str, dict | None]:
    """(base map, label, anchor-or-None): the single source the Benchmarks
    tab and the nudge share. Anchor medians when blessed ("vs anchor",
    backfilled per query from the reference map for queries the anchor did
    not measure); otherwise the reference/window-best fallback via
    bench_comparison_for with its labels intact."""
    anchor = bench_anchor_for()
    if anchor and anchor.get("medians"):
        base = dict(anchor["medians"])
        for q, best in bench_window_bests(events_rows).items():
            base.setdefault(q, best)
        return base, _BENCH_ANCHOR_LABEL, anchor
    base, label = bench_comparison_for(events_rows)
    return base, label, None


def bench_native_series(
    events_rows: list[dict], limit: int = 7, native_only: bool = True
) -> dict[str, list[float]]:
    """Newest-first per-query median series (last `limit` points each), for
    trend display. Native origin/main runs only by default; native_only=False
    includes branch and local runs (agent tooling overviews). Empty when no
    covered run carries medians."""
    pool = (
        [ev for ev in events_rows if _is_reference_run(ev.get("detail") or {})]
        if native_only
        else list(events_rows)
    )
    names: set[str] = set()
    for ev in pool:
        meds = _bench_nested(ev.get("detail"), _BENCH_MEDIAN_KEY)
        if isinstance(meds, dict):
            names.update(str(q) for q in meds)
    out: dict[str, list[float]] = {}
    for q in names:
        series = bench_medians_for(pool, q)[: max(1, limit)]
        if series:
            out[q] = series
    return out
