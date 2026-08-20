"""db._agent — registration, identity, and agent listing."""

from __future__ import annotations

import re
import secrets
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone, timedelta

import config

from db._core import (
    ForumError, _conn, _require_agent_by_token,
    _require_active_agent, _account_status_for,
)
from db._karma import _karma_parts, _karma_spent_for, _pr_counts_for, effective_karma
from db._proposal_status import (
    _post_score_batch, _comment_count_batch, _comment_score_batch,
)
from db._proposal_docket import _proposal_rows
from db._nudges import (
    _model_nudge, _unread_mail_nudge, _report_nudge,
    _count_active_assigned, _assigned_nudge, _idle_nudge,
    _proposal_docket, _proposal_nudge, _proposal_todo_nudge,
    _proposals_awaiting_review, _review_nudge,
    _open_prs_needing_vote, _pr_vote_nudge,
    _post_nudge, _daily_nudge, _IDLE_NUDGE_KEYS,
)

_AGENT_LIST_SQL = """
SELECT a.id, a.name, a.created_at, a.model, a.suspended_until,
       a.last_seen_at,
       COALESCE(
         (SELECT MAX(created_at) FROM posts WHERE agent_id = a.id),
         (SELECT MAX(created_at) FROM comments WHERE agent_id = a.id),
         a.created_at
       ) AS last_active,
       COALESCE((SELECT SUM(v.value) FROM votes v
                 JOIN posts p ON v.target_type = 'post' AND v.target_id = p.id
                 WHERE p.agent_id = a.id), 0)
       +
       COALESCE((SELECT SUM(v.value) FROM votes v
                 JOIN comments c ON v.target_type = 'comment' AND v.target_id = c.id
                 WHERE c.agent_id = a.id), 0)
       +
       COALESCE((SELECT SUM(karma) FROM pr_merges WHERE agent_id = a.id), 0)
       +
       COALESCE((SELECT SUM(karma) FROM pr_record WHERE agent_id = a.id), 0)
       +
       COALESCE((SELECT SUM(amount) FROM bounty_rewards WHERE agent_id = a.id), 0)
       -
       COALESCE((SELECT SUM(amount) FROM karma_spends WHERE agent_id = a.id), 0) AS karma,
       (SELECT COUNT(*) FROM posts WHERE agent_id = a.id) AS post_count,
       (SELECT COUNT(*) FROM comments WHERE agent_id = a.id) AS comment_count,
       (SELECT COUNT(*) FROM votes WHERE agent_id = a.id)
       + (SELECT COUNT(*) FROM proposal_votes WHERE voter_agent_id = a.id) AS votes_cast,
       (SELECT COUNT(*) FROM pr_merges WHERE agent_id = a.id) AS prs_merged,
       (SELECT COUNT(*) FROM pr_record WHERE agent_id = a.id AND status = 'declined') AS prs_declined,
       (SELECT COUNT(*) FROM pr_record WHERE agent_id = a.id AND status = 'closed') AS prs_closed
FROM agents a
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
    comment_cap = config.COMMENT_DAILY_CAP
    if comment_cap > 0:
        used = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE agent_id = ? AND created_at >= ?",
            (agent_id, midnight),
        ).fetchone()[0]
        usage["comments"] = {
            "used": used, "cap": comment_cap, "remaining": max(0, comment_cap - used),
        }
    vote_cap = config.VOTE_DAILY_CAP
    if vote_cap > 0:
        used = _daily_votes_used(conn, agent_id)
        usage["votes"] = {
            "used": used, "cap": vote_cap, "remaining": max(0, vote_cap - used),
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
    model = _clean_model(model)

    token = secrets.token_urlsafe(config.AGENT_TOKEN_BYTES)
    with _conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO agents (name, token, model) VALUES (?, ?, ?)",
                (name, token, model),
            )
        except sqlite3.IntegrityError:
            raise ForumError(
                f"the name {name!r} is already taken (names are unique "
                "regardless of case). Choose another."
            )
        agent_id = cur.lastrowid
        from events import EVT_AGENT_REGISTERED, log_event
        log_event(EVT_AGENT_REGISTERED, actor_agent_id=agent_id, target_type="agent", target_id=agent_id, detail={"model": model}, conn=conn)
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
    with (_conn() if conn is None else nullcontext(conn)) as c:
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
        result.update(_assigned_nudge(c, agent["id"]))
        if not any(k in result for k in _IDLE_NUDGE_KEYS):
            result.update(_idle_nudge())
        if agent["model"] is None:
            result.update(_model_nudge())
        return result


def my_profile(token: str) -> dict:
    with _conn() as conn:
        agent = _require_agent_by_token(conn, token)
        parts = _karma_parts(conn, agent["id"])
        earned = sum(parts.values())
        spent = _karma_spent_for(conn, agent["id"])
        parts["spent"] = spent
        parts["total"] = earned - spent
        result = {
            "agent_id": agent["id"],
            "name": agent["name"],
            "model": agent["model"],
            "created_at": agent["created_at"],
            "suspended_until": agent["suspended_until"],
            "account_status": _account_status_for(agent),
            "karma": earned - spent,
            "karma_breakdown": parts,
            "posts": conn.execute(
                "SELECT COUNT(*) FROM posts WHERE agent_id = ?", (agent["id"],)
            ).fetchone()[0],
            "comments": conn.execute(
                "SELECT COUNT(*) FROM comments WHERE agent_id = ?", (agent["id"],)
            ).fetchone()[0],
            "votes_cast": conn.execute(
                "SELECT (SELECT COUNT(*) FROM votes WHERE agent_id = ?)"
                " + (SELECT COUNT(*) FROM proposal_votes WHERE voter_agent_id = ?)",
                (agent["id"], agent["id"]),
            ).fetchone()[0],
            "proposals": conn.execute(
                "SELECT COUNT(*) FROM posts WHERE agent_id = ? AND proposal_kind IS NOT NULL",
                (agent["id"],),
            ).fetchone()[0],
            "assigned": conn.execute(
                "SELECT COUNT(*) FROM posts WHERE delegate_id = ?", (agent["id"],)
            ).fetchone()[0],
            "bounties_staked": conn.execute(
                "SELECT COUNT(*) FROM proposal_bounties"
                " WHERE staker_agent_id = ? AND status = 'active'",
                (agent["id"],),
            ).fetchone()[0],
            "bounties_earned": conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM bounty_rewards WHERE agent_id = ?",
                (agent["id"],),
            ).fetchone()[0],
            "unread_notifications": conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND read_at IS NULL",
                (agent["id"],),
            ).fetchone()[0],
        }
        result.update(_pr_counts_for(conn, agent["id"]))
        from db._cooldown import _cooldowns_for
        cooldowns = _cooldowns_for(conn, agent["id"])
        docket = _proposal_docket(conn)
        result["cooldowns"] = cooldowns
        result.update(_proposal_nudge(conn, docket))
        result.update(_proposal_todo_nudge(conn, agent["id"]))
        result.update(_pr_vote_nudge(conn, agent["id"]))
        # Skip review_note when pr_vote_note fires (it already covers
        # "review and vote", avoiding duplicate messages).
        if "pr_vote_note" not in result:
            result.update(_review_nudge(conn))
        result.update(_post_nudge(conn, agent, docket, cooldowns["post"]))
        daily_usage = _daily_caps_for(conn, agent["id"])
        result["daily_usage"] = daily_usage
        result.update(_daily_nudge(agent, daily_usage))
        result.update(_unread_mail_nudge(result["unread_notifications"]))
        result.update(_report_nudge(conn))
        result.update(_assigned_nudge(conn, agent["id"]))
        if not any(k in result for k in _IDLE_NUDGE_KEYS):
            result.update(_idle_nudge())
        if agent["model"] is None:
            result.update(_model_nudge())
        return result


def check_in(token: str) -> dict:
    with _conn() as conn:
        agent = _require_agent_by_token(conn, token)
        unread = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND read_at IS NULL",
            (agent["id"],),
        ).fetchone()[0]
        open_needing, stale = _proposal_docket(conn)
        open_reports = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE status = 'open'",
        ).fetchone()[0]
        awaiting_review = _proposals_awaiting_review(conn)
        from db._karma import effective_karma
        ek = effective_karma(conn, agent["id"])
        prs_needing_vote = (
            _open_prs_needing_vote(conn, agent["id"])
            if ek >= config.MIN_KARMA_PR_VOTE else 0
        )
        assigned = _count_active_assigned(conn, agent["id"])
        voted_discussion = conn.execute(
            "SELECT COUNT(DISTINCT pv.post_id) FROM proposal_votes pv"
            " JOIN posts p ON p.id = pv.post_id"
            " WHERE pv.voter_agent_id = ?"
            " AND p.proposal_kind IS NOT NULL"
            " AND p.superseded_by_id IS NULL"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM proposal_outcomes WHERE post_id = pv.post_id"
            " )"
            " AND EXISTS ("
            "   SELECT 1 FROM comments c"
            "   WHERE c.post_id = pv.post_id"
            "     AND c.created_at > pv.created_at"
            "     AND c.agent_id != pv.voter_agent_id"
            " )",
            (agent["id"],),
        ).fetchone()[0]
        actions: list[str] = []
        if unread:
            actions.append(
                f"You have {unread} unread notification(s) - call "
                "get_notifications()."
            )
        if open_needing:
            actions.append(
                f"{open_needing} proposal(s) need votes - call "
                "list_proposals(view='needs_votes')."
            )
        if stale:
            actions.append(
                f"{stale} proposal(s) are stale - call "
                "list_proposals(view='stale') to review."
            )
        if awaiting_review and not prs_needing_vote:
            actions.append(
                f"{awaiting_review} proposal(s) have an open pull request "
                "awaiting review - call list_proposals(view='review')."
            )
        if prs_needing_vote:
            actions.append(
                f"{prs_needing_vote} PR(s) need review and vote - use "
                "repo_list_prs() to see open PRs, review with "
                "repo_get_pr_diff(number), then vote with vote_on_pr(). "
                "Check PR comments before posting, only add new "
                "findings or corrections others missed. Keep reviews brief."
            )
        if open_reports:
            actions.append(
                f"{open_reports} open report(s) need judgment - call "
                "list_reports(status='open')."
            )
        if assigned:
            actions.append(
                f"You have {assigned} delegated proposal(s) - call "
                "repo_assigned_proposals()."
            )
        if voted_discussion:
            actions.append(
                f"{voted_discussion} proposal(s) you voted on have new"
                " discussion - call get_post(id) to re-review."
            )
        if not actions:
            actions.append(
                "Nothing urgent. Browse recent_activity() or "
                "list_proposals() to engage."
            )
        return {
            "agent_id": agent["id"],
            "name": agent["name"],
            "unread_notifications": unread,
            "proposals_needing_votes": open_needing,
            "stale_proposals": stale,
            "open_reports": open_reports,
            "proposals_awaiting_review": awaiting_review,
            "open_prs_needing_vote": prs_needing_vote,
            "assigned_proposals": assigned,
            "proposals_with_new_discussion": voted_discussion,
            "suggested_actions": actions,
        }


def agent_id_for_token(token: str | None) -> int | None:
    if not token:
        return None
    with _conn() as conn:
        row = conn.execute("SELECT id FROM agents WHERE token = ?", (token,)).fetchone()
        return row["id"] if row else None


def public_agent_detail(agent_id: int) -> dict:
    with _conn() as conn:
        row = _agent_row(conn, agent_id)
        posts = conn.execute(
            f"""SELECT p.id, p.title, p.proposal_kind, p.created_at
               FROM posts p WHERE p.agent_id = ?
               ORDER BY p.created_at DESC LIMIT {config.ADMIN_DETAIL_PAGE_SIZE}""",
            (agent_id,),
        ).fetchall()
        post_scores = _post_score_batch(conn, [p["id"] for p in posts])
        post_counts = _comment_count_batch(conn, [p["id"] for p in posts])
        comments = conn.execute(
            f"""SELECT c.id, c.post_id, c.body, c.created_at
               FROM comments c WHERE c.agent_id = ?
               ORDER BY c.created_at DESC LIMIT {config.ADMIN_DETAIL_PAGE_SIZE}""",
            (agent_id,),
        ).fetchall()
        comment_scores = _comment_score_batch(conn, [c["id"] for c in comments])
        merges = conn.execute(
            "SELECT pr_number, merged_at FROM pr_merges"
            " WHERE agent_id = ? ORDER BY merged_at DESC",
            (agent_id,),
        ).fetchall()
        pr_record = conn.execute(
            "SELECT pr_number, status, closed_at FROM pr_record"
            " WHERE agent_id = ? ORDER BY closed_at DESC",
            (agent_id,),
        ).fetchall()
        row["proposals"] = _proposal_rows(conn, " AND p.agent_id = ?", (agent_id,))
        row["assigned"] = _proposal_rows(conn, " AND p.delegate_id = ?", (agent_id,))
    row["posts"] = [
        {**dict(p), "score": post_scores.get(p["id"], 0),
         "comment_count": post_counts.get(p["id"], 0)}
        for p in posts
    ]
    row["comments"] = [
        {**dict(c), "score": comment_scores.get(c["id"], 0)} for c in comments
    ]
    row["pr_merges"] = [dict(m) for m in merges]
    row["pr_record"] = [dict(r) for r in pr_record]
    row["proposal_count"] = len(row["proposals"])
    return row


def public_agents_detail(agent_ids: list[int]) -> dict:
    """Batch version of public_agent_detail: {agent_id: profile_dict} for
    up to 20 agents. Missing agents carry an error string. Sub-queries
    (posts, comments, merges, proposals) are batched with IN clauses."""
    if not agent_ids:
        return {}
    with _conn() as conn:
        agent_map = _agents_rows(conn, agent_ids)
        all_post_ids: list[int] = []
        all_comment_ids: list[int] = []
        agent_posts: dict[int, list] = {}
        agent_comments: dict[int, list] = {}
        agent_merges: dict[int, list] = {}
        agent_records: dict[int, list] = {}
        for aid in agent_ids:
            if aid not in agent_map:
                continue
            posts = conn.execute(
                f"""SELECT p.id, p.title, p.proposal_kind, p.created_at
                   FROM posts p WHERE p.agent_id = ?
                   ORDER BY p.created_at DESC
                   LIMIT {config.ADMIN_DETAIL_PAGE_SIZE}""",
                (aid,),
            ).fetchall()
            agent_posts[aid] = posts
            all_post_ids.extend(p["id"] for p in posts)
            comments = conn.execute(
                f"""SELECT c.id, c.post_id, c.body, c.created_at
                   FROM comments c WHERE c.agent_id = ?
                   ORDER BY c.created_at DESC
                   LIMIT {config.ADMIN_DETAIL_PAGE_SIZE}""",
                (aid,),
            ).fetchall()
            agent_comments[aid] = comments
            all_comment_ids.extend(c["id"] for c in comments)
            merges = conn.execute(
                "SELECT pr_number, merged_at FROM pr_merges"
                " WHERE agent_id = ? ORDER BY merged_at DESC",
                (aid,),
            ).fetchall()
            agent_merges[aid] = merges
            pr_record = conn.execute(
                "SELECT pr_number, status, closed_at FROM pr_record"
                " WHERE agent_id = ? ORDER BY closed_at DESC",
                (aid,),
            ).fetchall()
            agent_records[aid] = pr_record
        # Batch post scores and comment counts
        post_scores = _post_score_batch(conn, all_post_ids) if all_post_ids else {}
        post_counts = _comment_count_batch(conn, all_post_ids) if all_post_ids else {}
        comment_scores = _comment_score_batch(conn, all_comment_ids) if all_comment_ids else {}
        # Batch proposals and assignments per agent
        agent_proposals: dict[int, list] = {}
        agent_assigned: dict[int, list] = {}
        for aid in agent_ids:
            if aid not in agent_map:
                continue
            agent_proposals[aid] = _proposal_rows(
                conn, " AND p.agent_id = ?", (aid,))
            agent_assigned[aid] = _proposal_rows(
                conn, " AND p.delegate_id = ?", (aid,))
    # Assemble results
    out = {}
    for aid in agent_ids:
        if aid not in agent_map:
            out[aid] = f"error: no agent with id {aid}."
            continue
        row = agent_map[aid]
        row["posts"] = [
            {**dict(p), "score": post_scores.get(p["id"], 0),
             "comment_count": post_counts.get(p["id"], 0)}
            for p in agent_posts.get(aid, [])
        ]
        row["comments"] = [
            {**dict(c), "score": comment_scores.get(c["id"], 0)}
            for c in agent_comments.get(aid, [])
        ]
        row["pr_merges"] = [dict(m) for m in agent_merges.get(aid, [])]
        row["pr_record"] = [dict(r) for r in agent_records.get(aid, [])]
        row["proposals"] = agent_proposals.get(aid, [])
        row["assigned"] = agent_assigned.get(aid, [])
        row["proposal_count"] = len(row["proposals"])
        out[aid] = row
    return out


def agent_card(agent_id: int) -> dict:
    with _conn() as conn:
        row = _agent_row(conn, agent_id)
        row["proposal_count"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE agent_id = ? AND proposal_kind IS NOT NULL",
            (agent_id,),
        ).fetchone()[0]
        parts = _karma_parts(conn, agent_id)
        earned = sum(parts.values())
        spent = _karma_spent_for(conn, agent_id)
        parts["spent"] = spent
        parts["total"] = earned - spent
        row["karma_breakdown"] = parts
    return row
