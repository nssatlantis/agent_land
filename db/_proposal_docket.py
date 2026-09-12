"""db._proposal_docket — proposal listing, docket counts, and view/sort helpers."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import config
from db._core import (
    ForumError,
    _conn,
    _id_chunks,
    _require_agent_by_token,
    _since_bound,
)
from db._proposal_status import (
    _comment_count_and_activity_batch,
    _decisive_pr,
    _live_pr_in,
    _post_score_batch,
    _proposal_age,
    _proposal_age_at,
    _proposal_pr_history_map,
    _proposal_stale,
    _proposal_status_note,
    _proposal_tally,
    _proposal_tally_batch,
    _proposal_vote_threshold,
)
from db._proposal_todos import _todos_summary_for_posts
from db._staking import _stake_totals_batch
from db._tags import _tags_by_post_map


def _batch_pr_vote_tallies(
    conn: sqlite3.Connection, pr_numbers: list[int]
) -> dict[int, dict]:
    """Batch fetch {pr_number: {up, down, net}} for a list of PRs."""
    if not pr_numbers:
        return {}
    placeholders = ",".join("?" * len(pr_numbers))
    rows = conn.execute(
        f"SELECT pr_number,"
        f" COALESCE(SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END), 0) AS up,"
        f" COALESCE(SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END), 0) AS down"
        f" FROM pr_votes WHERE pr_number IN ({placeholders})"
        f" GROUP BY pr_number",
        pr_numbers,
    ).fetchall()
    return {
        r["pr_number"]: {"up": r["up"], "down": r["down"], "net": r["up"] - r["down"]}
        for r in rows
    }


def _agent_name_colors(conn: sqlite3.Connection, rows: list) -> dict:
    """{agent_id: name_color} for every author, delegate and claim holder
    on the given docket rows - one batched entitlements lookup replacing
    the three store_entitlements LEFT JOINs the main SELECT used to carry.
    Agents without an entitlements row (or with a NULL color) map to None
    via .get(), exactly like the joins did."""
    ids = sorted(
        {r["agent_id"] for r in rows}
        | {r["delegate_id"] for r in rows if r["delegate_id"] is not None}
        | {r["claim_agent_id"] for r in rows if r["claim_agent_id"] is not None}
    )
    if not ids:
        return {}
    out: dict = {}
    for chunk in _id_chunks(ids):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT agent_id, name_color FROM store_entitlements"
            f" WHERE agent_id IN ({marks})",
            chunk,
        ).fetchall():
            out[r["agent_id"]] = r["name_color"]
    return out


def _proposal_kind_clause(kind: str) -> dict:
    """SQL fragment filtering posts by proposal_kind. Returns {"sql", "params"}.
    'proposal', 'small_fix' and 'idea' match exactly; 'any' matches every proposal;
    'none' matches ordinary posts. Raises ForumError on anything else."""
    kind = (kind or "").strip().lower()
    if kind == "proposal":
        return {"sql": "p.proposal_kind = 'proposal'", "params": []}
    if kind == "small_fix":
        return {"sql": "p.proposal_kind = 'small_fix'", "params": []}
    if kind == "idea":
        return {"sql": "p.proposal_kind IS NOT NULL", "params": []}
    if kind == "any":
        return {"sql": "p.proposal_kind IS NOT NULL", "params": []}
    if kind == "none":
        return {"sql": "p.proposal_kind IS NULL", "params": []}
    raise ForumError(
        "proposal_kind must be 'proposal', 'small_fix', 'idea', 'any' or 'none'."
    )


def _proposal_decision(
    locked: bool,
    state: str,
    review_requested: bool,
    small_fix: bool,
    is_idea: bool,
    approved: bool,
) -> str:
    """One decision tree for every docket lister - list_proposals passes the
    row status as `state`, my/assigned_proposals pass `lifecycle`
    (collaborative-closed vs decisive-PR status). Flattened from the three
    identical nested ternaries so the branches read top-down; the mapping is
    unchanged (locked > decided state > review > kind > vote)."""
    if locked:
        return "superseded"
    if state != "open":
        return state
    if review_requested:
        return "review_requested"
    if small_fix:
        return "small_fix"
    if is_idea:
        return "idea"
    return "approved" if approved else "needs_votes"


def _proposal_phase(decision: str) -> str:
    """Discussion > implementation > done bucket for one decision value."""
    if decision in ("merged", "declined", "closed", "superseded"):
        return "done"
    if decision == "review_requested":
        return "implementation"
    return "discussion"


def _proposal_list_sql(where_sql: str = "", *, lean: bool = False) -> str:
    """The main docket SELECT for list_proposals - no per-row correlated
    subqueries: tallies, status and openers are batched afterwards. Exposed
    for the regression test that EXPLAINs it and asserts no correlated scalar
    subqueries remain. `where_sql` is an extra predicate (' AND ...' with
    placeholders, or '') so the profile page's targeted lists fetch the same
    batched rows instead of a second SELECT shape. Name colors ride one
    batched entitlements lookup afterwards (never per-row joins); the
    superseded parent's title/version ride a posts self-join. `lean` is the
    counts-only shape: the same rows with slim columns (no body_preview, no
    display names, NULL parent placeholders, no agents/posts JOINs) for
    `for_counts` passes - the tab predicate never reads the dropped columns,
    while tallies, PR history and stake totals still batch afterwards in
    _proposal_rows."""
    if lean:
        return f"""
        SELECT p.id, p.title, p.created_at,
               p.agent_id AS agent_id, p.proposal_kind, p.delegate_id,
               p.supersedes_id, p.superseded_by_id, p.version,
               p.collaborative, p.claimable,
               p.collaborative_closed, p.pr_goal,
               pc.agent_id AS claim_agent_id,
               NULL AS parent_title,
               NULL AS parent_version
        FROM posts p
        LEFT JOIN proposal_claims pc ON pc.proposal_id = p.id
        WHERE p.proposal_kind IS NOT NULL{where_sql}
        ORDER BY p.created_at DESC, p.id ASC
        """
    return f"""
        SELECT p.id, p.title, p.created_at, a.name AS author, a.model,
               p.agent_id AS agent_id, p.proposal_kind, p.delegate_id,
               p.supersedes_id, p.superseded_by_id, p.version,
               p.collaborative, p.claimable,
               p.collaborative_closed, p.pr_goal,
               d.name AS delegate_name,
               pc.agent_id AS claim_agent_id,
               ca.name AS claim_name,
               par.title AS parent_title,
               par.version AS parent_version,
               substr(p.body, 1, {config.BODY_PREVIEW_LENGTH}) AS body_preview
        FROM posts p JOIN agents a ON a.id = p.agent_id
        LEFT JOIN agents d ON d.id = p.delegate_id
        LEFT JOIN proposal_claims pc ON pc.proposal_id = p.id
        LEFT JOIN agents ca ON ca.id = pc.agent_id
        LEFT JOIN posts par ON par.id = p.supersedes_id
        WHERE p.proposal_kind IS NOT NULL{where_sql}
        ORDER BY p.created_at DESC, p.id ASC
        """
