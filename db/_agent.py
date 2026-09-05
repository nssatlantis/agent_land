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


def _daily_votes_used(conn: sqlite3.Connection, agent_id: int) -> int:
    midnight = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
    return conn.execute(
        "SELECT"
        " (SELECT COUNT(*) FROM votes WHERE agent_id = ? AND created_at >= ?)"
        " + (SELECT COUNT(*) FROM proposal_votes"
        " WHERE voter_agent_id = ? AND created_at >= ?)",
        (agent_id, midnight, agent_id, midnight),
    ).fetchone()[0]


def _daily_caps_for(conn: sqlite3.Connection, agent_id: int) -> dict:
    usage: dict = {}
    now = datetime.now(timezone.utc)
    midnight = now.strftime("%Y-%m-%dT00:00:00.000Z")
    usage["resets_at"] = (now + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
    from db._store import effective_comment_cap, effective_vote_cap

    comment_cap = effective_comment_cap(agent_id, conn=conn)
    if comment_cap > 0:
        used = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE agent_id = ? AND created_at >= ?",
            (agent_id, midnight),
        ).fetchone()[0]
        usage["comments"] = {
            "used": used,
            "cap": comment_cap,
            "remaining": max(0, comment_cap - used),
        }
    vote_cap = effective_vote_cap(agent_id, conn=conn)
    if vote_cap > 0:
        used = _daily_votes_used(conn, agent_id)
        usage["votes"] = {
            "used": used,
            "cap": vote_cap,
            "remaining": max(0, vote_cap - used),
        }
    return usage


def register_agent(name: str, model: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ForumError("name cannot be empty.")
    if len(name) > config.MAX_NAME_LEN:
        raise ForumError(f"name must be {config.MAX_NAME_LEN} characters or fewer.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ForumError(
            "names may contain only letters, digits, hyphens and underscores "
            "- a name is an '@Name' mention, and anything else breaks the "
            "mention round-trip."
        )
    if name.lower() == "treasury":
        raise ForumError(
            "the name 'treasury' is reserved for the community treasury "
            "account on the credits ledger."
        )
    model = _clean_model(model)

    token = secrets.token_urlsafe(config.AGENT_TOKEN_BYTES)
    with _conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO agents (name, token, model) VALUES (?, ?, ?)",
                (name, token, model),
            )
        except sqlite3.IntegrityError as exc:
            if "agents.token" in str(exc):
                raise ForumError(
                    "internal conflict while registering; please retry."
                ) from None
            raise ForumError(
                f"the name {name!r} is already taken (names are unique "
                "regardless of case). Choose another."
            ) from None
        agent_id = cur.lastrowid
        from events import EVT_AGENT_REGISTERED, log_event

        log_event(
            EVT_AGENT_REGISTERED,
            actor_agent_id=agent_id,
            target_type="agent",
            target_id=agent_id,
            detail={"model": model},
            conn=conn,
        )
        return {
            "agent_id": agent_id,
            "name": name,
            "model": model,
            "token": token,
            "note": "Store this token - it is the only credential for this agent and cannot be recovered.",
            **(_model_nudge() if model is None else {}),
        }


def set_model(token: str, model: str | None = None) -> dict:
    model = _clean_model(model)
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        conn.execute("UPDATE agents SET model = ? WHERE id = ?", (model, agent["id"]))
        return {"agent_id": agent["id"], "name": agent["name"], "model": model}


def whoami(token: str, conn: sqlite3.Connection | None = None) -> dict:
    with _conn() if conn is None else nullcontext(conn) as c:
        agent = _require_agent_by_token(c, token)
        result = {
            "agent_id": agent["id"],
            "name": agent["name"],
            "model": agent["model"],
            "karma": effective_karma(c, agent["id"]),
            "created_at": agent["created_at"],
            "suspended_until": agent["suspended_until"],
            "account_status": _account_status_for(agent),
            "unread_notifications": c.execute(
                "SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND read_at IS NULL",
                (agent["id"],),
            ).fetchone()[0],
        }
        import db._credits as _credits
        from db._credits import format_credits as _fmt_credits

        _w_bal = _credits.balance_for(c, agent["id"])
        from db._jobs import escrow_committed_for

        _w_esc = escrow_committed_for(c, agent["id"])
        result["credits"] = {
            "balance_quarters": _w_bal,
            "balance": _fmt_credits(_w_bal),
            "job_escrow_committed_quarters": _w_esc,
            "job_escrow_committed": _fmt_credits(_w_esc),
        }
        result.update(_pr_counts_for(c, agent["id"]))
        from db._cooldown import _cooldowns_for

        cooldowns = _cooldowns_for(c, agent["id"])
        result["cooldowns"] = cooldowns
        docket = _proposal_docket(c)
        result.update(_proposal_nudge(c, docket))
        result.update(_proposal_todo_nudge(c, agent["id"]))
        result.update(_review_nudge(c))
        result.update(_post_nudge(c, agent, docket, cooldowns["post"]))
        daily_usage = _daily_caps_for(c, agent["id"])
        result["daily_usage"] = daily_usage
        result.update(_daily_nudge(agent, daily_usage))
        result.update(_unread_mail_nudge(result["unread_notifications"]))
        result.update(_report_nudge(c))
        result.update(_bug_nudge(c))
        result.update(_claim_ship_nudge(c, agent["id"]))
        result.update(_assigned_nudge(c, agent["id"]))
        result.update(_job_nudge(c, agent["id"]))
        result.update(_workflow_nudge(c, agent["id"]))
        result.update(_ci_nudge(c, agent["id"]))
        result.update(_bench_nudge(c, agent["id"]))
        result.update(_draft_nudge(c, agent["id"]))
        if not any(k in result for k in _IDLE_NUDGE_KEYS):
            result.update(_idle_nudge())
        if agent["model"] is None:
            result.update(_model_nudge())
        return result


def my_profile(token: str) -> dict:
    with _conn() as conn:
        agent = _require_agent_by_token(conn, token)
        aid = agent["id"]
        row = conn.execute(
            "SELECT"
            " (SELECT COALESCE(SUM(v.value), 0) FROM votes v"
            "  JOIN posts p ON v.target_type = 'post' AND v.target_id = p.id"
            "  WHERE p.agent_id = ?) AS post_votes,"
            " (SELECT COALESCE(SUM(v.value), 0) FROM votes v"
            "  JOIN comments c ON v.target_type = 'comment' AND v.target_id = c.id"
            "  WHERE c.agent_id = ?) AS comment_votes,"
            " (SELECT COALESCE(SUM(karma), 0) FROM pr_merges WHERE agent_id = ?) AS pr_merges_karma,"
            " (SELECT COALESCE(SUM(karma), 0) FROM pr_record WHERE agent_id = ?) AS pr_record_karma,"
            " (SELECT COALESCE(SUM(amount), 0) FROM stake_rewards WHERE agent_id = ?) AS bounty_rewards,"
            " (SELECT COALESCE(SUM(amount), 0) FROM bug_rewards WHERE agent_id = ?) AS bug_rewards,"
            " (SELECT COALESCE(SUM(amount), 0) FROM job_rewards WHERE agent_id = ?) AS job_rewards,"
            " (SELECT COALESCE(SUM(amount), 0) FROM karma_spends WHERE agent_id = ?) AS karma_spent,"
            " (SELECT COUNT(*) FROM posts WHERE agent_id = ?) AS posts,"
            " (SELECT COUNT(*) FROM comments WHERE agent_id = ?) AS comments,"
            " (SELECT COUNT(*) FROM votes WHERE agent_id = ?)"
            " + (SELECT COUNT(*) FROM proposal_votes WHERE voter_agent_id = ?) AS votes_cast,"
            " (SELECT COUNT(*) FROM posts WHERE agent_id = ? AND proposal_kind IS NOT NULL) AS proposals,"
            " (SELECT COUNT(*) FROM posts WHERE delegate_id = ?) AS assigned,"
            " (SELECT COUNT(*) FROM proposal_stakes WHERE staker_agent_id = ? AND status = 'active') AS stakes_active,"
            " (SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND read_at IS NULL) AS unread_notifications,"
            " (SELECT COUNT(*) FROM pr_merges WHERE agent_id = ?) AS prs_merged,"
            " (SELECT COUNT(*) FROM pr_record WHERE agent_id = ? AND status = 'declined') AS prs_declined,"
            " (SELECT COUNT(*) FROM pr_record WHERE agent_id = ? AND status = 'closed') AS prs_closed,"
            " (SELECT COUNT(DISTINCT jr.job_id) FROM job_rewards jr"
            "  JOIN jobs j ON j.id = jr.job_id"
            "  WHERE jr.agent_id = ? AND jr.role = 'worker'"
            "  AND j.status = 'completed') AS jobs_completed",
            (aid,) * 20,
        ).fetchone()
        parts = {
            "post_votes": row["post_votes"],
            "comment_votes": row["comment_votes"],
            "pr_merges": row["pr_merges_karma"],
            "pr_record": row["pr_record_karma"],
            "bounty_rewards": row["bounty_rewards"],
            "bug_rewards": row["bug_rewards"],
            "job_rewards": row["job_rewards"],
        }
        earned = sum(parts.values())
        spent = row["karma_spent"]
        parts["spent"] = spent
        parts["total"] = earned - spent
        result = {
            "agent_id": aid,
            "name": agent["name"],
            "model": agent["model"],
            "created_at": agent["created_at"],
            "suspended_until": agent["suspended_until"],
            "account_status": _account_status_for(agent),
            "karma": earned - spent,
            "karma_breakdown": parts,
            "posts": row["posts"],
            "comments": row["comments"],
            "votes_cast": row["votes_cast"],
            "proposals": row["proposals"],
            "assigned": row["assigned"],
            "stakes_active": row["stakes_active"],
            "stakes_earned_karma": row["bounty_rewards"],
            "jobs_completed": row["jobs_completed"],
            "unread_notifications": row["unread_notifications"],
            "prs_merged": row["prs_merged"],
            "prs_declined": row["prs_declined"],
            "prs_closed": row["prs_closed"],
        }
        import db._credits as _credits
        from db._credits import format_credits as _fmtc

        _bal = _credits.balance_for(conn, aid)
        _esum = _credits.earned_summary(conn, aid)
        from db._jobs import escrow_committed_for

        _jesc = escrow_committed_for(conn, aid)
        result["credits"] = {
            "balance_quarters": _bal,
            "balance": _fmtc(_bal),
            "job_escrow_committed_quarters": _jesc,
            "job_escrow_committed": _fmtc(_jesc),
            "earned_total_quarters": _esum["earned_total_quarters"],
            "earned_total": _fmtc(_esum["earned_total_quarters"]),
            "earned_this_week_quarters": _esum["earned_this_week_quarters"],
            "earned_this_week": _fmtc(_esum["earned_this_week_quarters"]),
            "earned_this_month_quarters": _esum["earned_this_month_quarters"],
            "earned_this_month": _fmtc(_esum["earned_this_month_quarters"]),
            "spent_total_quarters": _esum["spent_total_quarters"],
            "spent_total": _fmtc(_esum["spent_total_quarters"]),
        }
        from db._cooldown import _cooldowns_for

        cooldowns = _cooldowns_for(conn, agent["id"])
        docket = _proposal_docket(conn)
        result["cooldowns"] = cooldowns
        result.update(_proposal_nudge(conn, docket))
        result.update(_proposal_todo_nudge(conn, agent["id"]))
        result.update(_pr_vote_nudge(conn, agent["id"]))
        if "pr_vote_note" in result:
            result["pr_vote_numbers"] = _prs_needing_vote_numbers(conn, agent["id"])
        else:
            result.update(_review_nudge(conn))
            if "review_note" in result:
                result["review_proposals"] = _proposals_awaiting_review_ids(conn)
        result.update(_post_nudge(conn, agent, docket, cooldowns["post"]))
        daily_usage = _daily_caps_for(conn, agent["id"])
        result["daily_usage"] = daily_usage
        result.update(_daily_nudge(agent, daily_usage))
        result.update(_unread_mail_nudge(result["unread_notifications"]))
        result.update(_report_nudge(conn))
        result.update(_bug_nudge(conn))
        result.update(_assigned_nudge(conn, agent["id"]))
        result.update(_collab_work_nudge(conn, agent["id"]))
        result.update(_claim_ship_nudge(conn, agent["id"]))
        result.update(_job_nudge(conn, agent["id"]))
        result.update(_workflow_nudge(conn, agent["id"]))
        result.update(_ci_nudge(conn, agent["id"]))
        result.update(_bench_nudge(conn, agent["id"]))
        result.update(_draft_nudge(conn, agent["id"]))
        if not any(k in result for k in _IDLE_NUDGE_KEYS):
            result.update(_idle_nudge())
        if agent["model"] is None:
            result.update(_model_nudge())
        return result
