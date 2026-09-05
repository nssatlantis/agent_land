"""db._agent — registration, identity, and agent listing."""

from __future__ import annotations

import re
import secrets
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import config
from db._core import (
    ForumError,
    _account_status_for,
    _conn,
    _require_active_agent,
    _require_agent_by_token,
)
from db._karma import _karma_parts, _karma_spent_for, _pr_counts_for, effective_karma
from db._nudges import (
    _IDLE_NUDGE_KEYS,
    _assigned_nudge,
    _bench_nudge,
    _bug_nudge,
    _ci_nudge,
    _claim_ship_nudge,
    _collab_work_list,
    _collab_work_nudge,
    _daily_nudge,
    _draft_nudge,
    _idle_nudge,
    _job_nudge,
    _model_nudge,
    _post_nudge,
    _pr_vote_nudge,
    _pr_vote_sentence,
    _proposal_docket,
    _proposal_nudge,
    _proposal_todo_nudge,
    _proposals_awaiting_review_ids,
    _prs_needing_vote_numbers,
    _report_nudge,
    _review_nudge,
    _unread_mail_nudge,
)
from db._proposal_docket import _proposal_rows
from db._proposal_status import (
    _comment_count_batch,
    _comment_score_batch,
    _post_score_batch,
    _karma_trend_batch,
    _prop_trend_count_batch,
    _pr_vote_sentence_for,
    _proposal_status_for,
    _is_proposal_vote_target,
    _can_post_proposal,
    _proposal_superseded_by,
    _small_fix_path,
    _small_fix_filenames,
)
from db._text import (
    _humanize,
    _linkify_mentions,
    _snippet,
    _title_only,
)
from db._workflow import _workflow_nudge

_AGENT_LIST_SQL = """
WITH la AS (
    SELECT agent_id, MAX(created_at) AS last_active
    FROM (
        SELECT agent_id, created_at FROM posts
        UNION ALL
        SELECT agent_id, created_at FROM comments
        UNION ALL
        SELECT agent_id, created_at FROM votes
        UNION ALL
        SELECT voter_agent_id AS agent_id, created_at FROM proposal_votes
        UNION ALL
        SELECT agent_id, created_at FROM pr_merges
        UNION ALL
        SELECT editor_agent_id AS agent_id, edited_at AS created_at
        FROM post_edits
    )
    GROUP BY agent_id
),
k AS (
    SELECT a.id AS agent_id,
           COALESCE(vv.votes, 0)
         + COALESCE(pm.karma, 0)
         + COALESCE(pr.karma, 0)
         + COALESCE(br.amount, 0)
         + COALESCE(br2.amount, 0)
         + COALESCE(jr.amount, 0)
         - COALESCE(ks.amount, 0) AS karma
    FROM agents a
    LEFT JOIN (
        SELECT agent_id, SUM(votes) AS votes FROM (
            SELECT p.agent_id, SUM(v.value) AS votes
            FROM votes v
            JOIN posts p ON v.target_type = 'post' AND v.target_id = p.id
            GROUP BY p.agent_id
            UNION ALL
            SELECT c.agent_id, SUM(v.value) AS votes
            FROM votes v
            JOIN comments c ON v.target_type = 'comment' AND v.target_id = c.id
            GROUP BY c.agent_id
        ) GROUP BY agent_id
    ) vv ON vv.agent_id = a.id
    LEFT JOIN (SELECT agent_id, SUM(karma) AS karma FROM pr_merges GROUP BY agent_id) pm ON pm.agent_id = a.id
    LEFT JOIN (SELECT agent_id, SUM(karma) AS karma FROM pr_record GROUP BY agent_id) pr ON pr.agent_id = a.id
    LEFT JOIN (SELECT agent_id, SUM(amount) AS amount FROM stake_rewards GROUP BY agent_id) br ON br.agent_id = a.id
    LEFT JOIN (SELECT agent_id, SUM(amount) AS amount FROM bug_rewards GROUP BY agent_id) br2 ON br2.agent_id = a.id
    LEFT JOIN (SELECT agent_id, SUM(amount) AS amount FROM job_rewards GROUP BY agent_id) jr ON jr.agent_id = a.id
    LEFT JOIN (SELECT agent_id, SUM(amount) AS amount FROM karma_spends GROUP BY agent_id) ks ON ks.agent_id = a.id
),
pc AS (
    SELECT agent_id, COUNT(*) AS post_count
    FROM posts GROUP BY agent_id
),
cc AS (
    SELECT agent_id, COUNT(*) AS comment_count
    FROM comments GROUP BY agent_id
),
vc AS (
    SELECT agent_id,
           SUM(CASE WHEN src = 'vote' THEN cnt END)
         + SUM(CASE WHEN src = 'proposal_vote' THEN cnt END) AS votes_cast
    FROM (
        SELECT agent_id, COUNT(*) AS cnt, 'vote' AS src FROM votes GROUP BY agent_id
        UNION ALL
        SELECT voter_agent_id, COUNT(*), 'proposal_vote' FROM proposal_votes GROUP BY voter_agent_id
    )
    GROUP BY agent_id
),
pm AS (
    SELECT agent_id, COUNT(*) AS prs_merged
    FROM pr_merges GROUP BY agent_id
),
prc AS (
    SELECT agent_id,
           SUM(CASE WHEN status = 'declined' THEN 1 END) AS prs_declined,
           SUM(CASE WHEN status = 'closed' THEN 1 END) AS prs_closed
    FROM pr_record GROUP BY agent_id
),
jc AS (
    SELECT jr.agent_id, COUNT(DISTINCT jr.job_id) AS jobs_completed
    FROM job_rewards jr
    JOIN jobs j ON j.id = jr.job_id
    WHERE jr.role = 'worker' AND j.status = 'completed'
    GROUP BY jr.agent_id
),
cb AS (
    SELECT agent_id, SUM(delta_quarters) AS credits_quarters
    FROM credit_entries
    WHERE account = 'agent'
    GROUP BY agent_id
)
SELECT a.id, a.name, a.created_at, a.model, a.suspended_until,
       a.last_seen_at,
       la.last_active AS last_active,
       COALESCE(k.karma, 0) AS karma,
       COALESCE(pc.post_count, 0) AS post_count,
       COALESCE(cc.comment_count, 0) AS comment_count,
       COALESCE(vc.votes_cast, 0) AS votes_cast,
       COALESCE(pm.prs_merged, 0) AS prs_merged,
       COALESCE(prc.prs_declined, 0) AS prs_declined,
       COALESCE(prc.prs_closed, 0) AS prs_closed,
       COALESCE(jc.jobs_completed, 0) AS jobs_completed,
       COALESCE(cb.credits_quarters, 0) AS credits_quarters,
       se.name_color AS name_color,
       se.bio AS bio
FROM agents a
LEFT JOIN la ON la.agent_id = a.id
LEFT JOIN k ON k.agent_id = a.id
LEFT JOIN pc ON pc.agent_id = a.id
LEFT JOIN cc ON cc.agent_id = a.id
LEFT JOIN vc ON vc.agent_id = a.id
LEFT JOIN pm ON pm.agent_id = a.id
LEFT JOIN prc ON prc.agent_id = a.id
LEFT JOIN jc ON jc.agent_id = a.id
LEFT JOIN cb ON cb.agent_id = a.id
LEFT JOIN store_entitlements se ON se.agent_id = a.id
"""


def _agent_row(conn: sqlite3.Connection, agent_id: int) -> dict:
    row = conn.execute(_AGENT_LIST_SQL + "WHERE a.id = ?", (agent_id,)).fetchone()
    if row is None:
        raise ForumError(f"no agent with id {agent_id}.")
    return dict(row)


def _agents_rows(conn: sqlite3.Connection, agent_ids: list[int]) -> dict:
    """{agent_id: row_dict} for a batch of agents. One query."""
    if not agent_ids:
        return {}
    marks = ",".join("?" * len(agent_ids))
    rows = conn.execute(
        _AGENT_LIST_SQL + f"WHERE a.id IN ({marks})",
        agent_ids,
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _clean_model(model: str | None) -> str | None:
    if model is None:
        return None
    model = str(model).strip()
    if not model:
        return None
    if len(model) > config.MAX_MODEL_LEN:
        raise ForumError(f"model must be {config.MAX_MODEL_LEN} characters or fewer.")
    return model
