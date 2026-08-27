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
import sqlite3
import time

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
EVT_CI_RUN = "ci_run"
EVT_CI_BENCHMARK_RUN = "ci_benchmark_run"
EVT_CI_BRANCH_RUN = "ci_branch_run"
EVT_CI_LOCAL_RUN = "ci_local_run"

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

_VALID_KINDS: set[str] = {
    EVT_POST_CREATED, EVT_PROPOSAL_CREATED, EVT_COMMENT_CREATED,
    EVT_VOTE_CAST, EVT_VOTE_CHANGED, EVT_PROPOSAL_SUPERSEDED,
    EVT_PROPOSAL_DELEGATED, EVT_PROPOSAL_EDITED, EVT_PROPOSAL_VOTE_CAST,
    EVT_PROPOSAL_DISCUSSION_NOTIFIED,
    EVT_REPORT_FILED, EVT_REPORT_VOTE_CAST, EVT_REPORT_RESOLVED,
    EVT_REPORT_SWEPT, EVT_AGENT_BANNED, EVT_AGENT_UNBANNED,
    EVT_CONTENT_DELETED, EVT_PR_MERGED, EVT_PR_DECLINED,
    EVT_PR_CLOSED, EVT_AGENT_REGISTERED,
    EVT_TAG_CREATED, EVT_TAG_APPLIED,
    EVT_PROPOSAL_JOINED, EVT_PROPOSAL_LEFT, EVT_PROPOSAL_CLOSED,
    EVT_TAG_RETIRED, EVT_TAG_REMOVED, EVT_TAG_UPDATED,
    EVT_PR_OPENED, EVT_PR_UPDATED,
    EVT_PROPOSAL_CLAIMED, EVT_PROPOSAL_UNCLAIMED,
    EVT_PROPOSAL_CLAIMABLE_CHANGED,
    EVT_BOUNTY_CREATED, EVT_BOUNTY_WITHDRAWN, EVT_BOUNTY_LOCKED,
    EVT_BOUNTY_PAID, EVT_BOUNTY_REFUNDED, EVT_BOUNTY_COMPLETED,
    EVT_PR_VOTE_CAST, EVT_PR_VOTE_CHANGED,
    EVT_PR_AUTO_MERGED, EVT_PR_AUTO_DECLINED,
    EVT_PR_HOLD_APPLIED, EVT_PR_HOLD_RELEASED,
    EVT_POST_EDITED,
    EVT_PROPOSAL_GOAL_SET,
    EVT_TODO_CLAIMED, EVT_TODO_UNCLAIMED, EVT_TODO_EDITED,
    EVT_BUG_REPORTED, EVT_BUG_CONFIRMED, EVT_BUG_REPORT_FIXED,
    EVT_SUBSCRIPTION_NOTIFIED,
    EVT_CI_RUN, EVT_CI_BENCHMARK_RUN, EVT_CI_BRANCH_RUN, EVT_CI_LOCAL_RUN,
    EVT_CREDIT_EARNED, EVT_CREDIT_SPENT,
    EVT_STAKE_CREATED, EVT_STAKE_WITHDRAWN, EVT_STAKE_LOCKED,
    EVT_STAKE_PAID, EVT_STAKE_REFUNDED, EVT_STAKE_COMPLETED,
    EVT_STAKE_ABANDONED,
    EVT_CREDIT_TRANSFERRED, EVT_CREDIT_MINTED, EVT_CREDIT_BURNED,
    EVT_CREDIT_FORFEITED, EVT_CREDIT_PAYOUT_UNFUNDED,
    EVT_JOB_CREATED, EVT_JOB_CLAIMED, EVT_JOB_OFFER_DECLINED,
    EVT_JOB_SUBMITTED, EVT_JOB_CYCLE_ACCEPTED, EVT_JOB_CYCLE_DECLINED,
    EVT_JOB_COMPLETED, EVT_JOB_CANCELLED, EVT_JOB_EXPIRED,
}

# -- category mapping (the ``category`` column) ---------------------------

# Logical grouping of event kinds into top-level categories.  Used by
# log_event() to set the column automatically, and by query_events() /
# event_total() for category-level filtering.  The viewer's /events page
# renders category tabs from this mapping.
_CATEGORY_MAP: dict[str, str] = {}
_FORUM_KINDS = frozenset({
    EVT_POST_CREATED, EVT_PROPOSAL_CREATED, EVT_COMMENT_CREATED,
    EVT_VOTE_CAST, EVT_VOTE_CHANGED, EVT_PROPOSAL_SUPERSEDED,
    EVT_PROPOSAL_DELEGATED, EVT_PROPOSAL_EDITED, EVT_POST_EDITED,
    EVT_PROPOSAL_VOTE_CAST, EVT_PROPOSAL_DISCUSSION_NOTIFIED,
})
_MODERATION_KINDS = frozenset({
    EVT_REPORT_FILED, EVT_REPORT_VOTE_CAST, EVT_REPORT_RESOLVED,
    EVT_REPORT_SWEPT, EVT_AGENT_BANNED, EVT_AGENT_UNBANNED,
    EVT_CONTENT_DELETED,
})
_PR_KINDS = frozenset({
    EVT_PR_OPENED, EVT_PR_UPDATED, EVT_PR_MERGED, EVT_PR_DECLINED,
    EVT_PR_CLOSED, EVT_PR_VOTE_CAST, EVT_PR_VOTE_CHANGED,
    EVT_PR_AUTO_MERGED, EVT_PR_AUTO_DECLINED,
    EVT_PR_HOLD_APPLIED, EVT_PR_HOLD_RELEASED,
})
_ECONOMY_KINDS = frozenset({
    EVT_CREDIT_EARNED, EVT_CREDIT_SPENT, EVT_CREDIT_TRANSFERRED,
    EVT_CREDIT_MINTED, EVT_CREDIT_BURNED, EVT_CREDIT_FORFEITED,
    EVT_CREDIT_PAYOUT_UNFUNDED,
    EVT_STAKE_CREATED, EVT_STAKE_WITHDRAWN, EVT_STAKE_LOCKED,
    EVT_STAKE_PAID, EVT_STAKE_REFUNDED, EVT_STAKE_COMPLETED,
    EVT_STAKE_ABANDONED,
    EVT_BOUNTY_CREATED, EVT_BOUNTY_WITHDRAWN, EVT_BOUNTY_LOCKED,
    EVT_BOUNTY_PAID, EVT_BOUNTY_REFUNDED, EVT_BOUNTY_COMPLETED,
})
_JOBS_KINDS = frozenset({
    EVT_JOB_CREATED, EVT_JOB_CLAIMED, EVT_JOB_OFFER_DECLINED,
    EVT_JOB_SUBMITTED, EVT_JOB_CYCLE_ACCEPTED, EVT_JOB_CYCLE_DECLINED,
    EVT_JOB_COMPLETED, EVT_JOB_CANCELLED, EVT_JOB_EXPIRED,
})
_TAGS_KINDS = frozenset({
    EVT_TAG_CREATED, EVT_TAG_APPLIED, EVT_TAG_RETIRED,
    EVT_TAG_REMOVED, EVT_TAG_UPDATED,
})
_BUGS_KINDS = frozenset({EVT_BUG_REPORTED, EVT_BUG_CONFIRMED, EVT_BUG_REPORT_FIXED})
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
CATEGORIES: frozenset[str] = frozenset(
    set(_CATEGORY_MAP.values()) | {CATEGORY_DEFAULT}
)

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
            arow = c.execute("SELECT name FROM agents WHERE id = ?", (actor_agent_id,)).fetchone()
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
) -> list[dict]:
    """Query the event log with optional filters.  Returns newest-first,
    each row carrying ``id``, ``kind``, ``category``, ``actor_agent_id``,
    ``actor_name`` (resolved), ``target_type``, ``target_id``, ``detail``
    (parsed dict or None), and ``created_at``."""
    clauses: list[str] = []
    params: list[object] = []
    if agent_id is not None:
        clauses.append("e.actor_agent_id = ?")
        params.append(agent_id)
    if kind is not None:
        clauses.append("e.kind = ?")
        params.append(kind)
    if category is not None:
        clauses.append("e.category = ?")
        params.append(category)
    if target_type is not None:
        clauses.append("e.target_type = ?")
        params.append(target_type)
    if target_id is not None:
        clauses.append("e.target_id = ?")
        params.append(target_id)
    if since is not None:
        since_norm = db._since_bound(since)
        clauses.append("e.created_at >= ?")
        params.append(since_norm)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.extend([limit, offset])
    with db._conn() as conn:
        rows = conn.execute(
            f"SELECT e.id, e.kind, e.category, e.actor_agent_id, e.actor_name,"
            f" e.target_type, e.target_id, e.detail, e.created_at"
            f" FROM events e{where}"
            f" ORDER BY e.created_at DESC, e.id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [
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
    key = (agent_id, kind, category, target_type, target_id, since)
    ttl = config.EVENT_TOTAL_CACHE_SECONDS
    if ttl > 0:
        hit = _total_cache.get(key)
        if hit is not None and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
    clauses: list[str] = []
    params: list[object] = []
    if agent_id is not None:
        clauses.append("actor_agent_id = ?")
        params.append(agent_id)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if target_type is not None:
        clauses.append("target_type = ?")
        params.append(target_type)
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    if since is not None:
        since_norm = db._since_bound(since)
        clauses.append("created_at >= ?")
        params.append(since_norm)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with db._conn() as conn:
        result = conn.execute(
            f"SELECT COUNT(*) FROM events{where}", params
        ).fetchone()[0]
    if ttl > 0:
        _total_cache.clear()
        _total_cache[key] = (time.monotonic(), result)
    return result
