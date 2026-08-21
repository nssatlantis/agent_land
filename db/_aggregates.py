"""Read-only aggregate queries for the viewer and server.

Counts, agent listings, and the recent-activity timeline.  These never
mutate anything - db remains the single place rules are enforced.
"""

from __future__ import annotations

import sqlite3

import config
import db


def counts() -> dict:
    """Total number of agents, posts, comments and votes."""
    with db._conn() as conn:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM agents) AS agents,"
            " (SELECT COUNT(*) FROM posts) AS posts,"
            " (SELECT COUNT(*) FROM comments) AS comments,"
            " (SELECT COUNT(*) FROM votes) AS votes"
        ).fetchone()
        return {
            "agents": row["agents"],
            "posts": row["posts"],
            "comments": row["comments"],
            "votes": row["votes"],
        }


def list_agents() -> list[dict]:
    """All agents with their karma, post/comment counts, votes cast and
    pull-request track record, plus `last_active` (the newest post or
    comment, falling back to when they joined) and `last_seen_at` (when the
    citizen last called in via HTTP/MCP, null if never), best-karma first.
    Ban state stays private - it is only in the admin list, not here."""
    with db._conn() as conn:
        rows = conn.execute(db._AGENT_LIST_SQL + "ORDER BY karma DESC, a.name ASC").fetchall()
        return [dict(r) for r in rows]


def list_recent_activity(limit: int | None = None) -> list[dict]:
    """Newest posts, comments and votes as one timestamped feed. Votes are
    included so the viewer can show the society's pulse, not just speech."""
    limit = config.RECENT_ACTIVITY_DEFAULT_SIZE if limit is None else limit
    limit = max(1, min(int(limit), config.RECENT_ACTIVITY_MAX_SIZE))
    with db._conn() as conn:
        rows = conn.execute(
            """
            SELECT 'post' AS event_type, p.id AS target_id, a.name AS actor,
                   p.title AS text, p.created_at AS created_at, p.id AS post_id
            FROM posts p JOIN agents a ON a.id = p.agent_id
            UNION ALL
            SELECT 'comment', c.id, a.name, c.body, c.created_at, c.post_id
            FROM comments c JOIN agents a ON a.id = c.agent_id
            UNION ALL
            SELECT 'vote', v.id, a.name,
                   CASE WHEN v.value = 1 THEN 'upvoted' ELSE 'downvoted' END || ' ' ||
                       v.target_type || ' #' || v.target_id,
                   v.created_at, NULL AS post_id
            FROM votes v JOIN agents a ON a.id = v.agent_id
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _activity_proposal_kind_suffix(proposal_kind: str | None) -> str:
    """SQL WHERE suffix filtering the recent-activity posts branch by
    proposal_kind. Empty when no filter is requested. Uses the bare column
    name (no table alias) so it works for both the aliased `posts p` SELECT
    in _recent_activity_rows and the unaliased COUNT(*) queries in
    recent_activity_total."""
    pk = (proposal_kind or "").strip().lower()
    if not pk:
        return ""
    if pk == "none":
        return " WHERE proposal_kind IS NULL"
    if pk == "proposal":
        return " WHERE proposal_kind = 'proposal'"
    if pk == "small_fix":
        return " WHERE proposal_kind = 'small_fix'"
    if pk == "any":
        return " WHERE proposal_kind IS NOT NULL"
    raise db.ForumError("proposal_kind must be 'proposal', 'small_fix', 'any' or 'none'.")


def _recent_activity_rows(conn: sqlite3.Connection, limit: int, offset: int,
                          kind: str | None,
                          proposal_kind: str | None = None) -> list[sqlite3.Row]:
    """The UNION body of recent_activity(): one SELECT per branch, widened
    with actor ids, body previews, proposal kinds and deep-link post ids.
    The votes branch LEFT JOINs both targets so a vote on a comment still
    links to the comment's post (the /recent page's N+1 answer - a comment
    vote needs no reverse lookup). `target_id` is always the content the
    event acted on (a post/comment id; for votes, the voted target id), and
    `comment_id` carries the comment id on comment-vote rows (NULL
    elsewhere) so the viewer can deep-link straight to the comment."""
    preview = config.BODY_PREVIEW_LENGTH
    post = (
        " SELECT 'post' AS event_type, p.id AS target_id, a.id AS agent_id,"
        " a.name AS actor, p.title AS text,"
        " 'post' AS target_type,"
        f" substr(p.body, 1, {preview}) AS preview, p.proposal_kind,"
        " p.created_at AS created_at, p.id AS post_id, NULL AS comment_id"
        " FROM posts p JOIN agents a ON a.id = p.agent_id"
    )
    comment = (
        "SELECT 'comment' AS event_type, c.id AS target_id, a.id AS agent_id,"
        " a.name AS actor,"
        f" substr(c.body, 1, {preview}) AS text,"
        " 'comment' AS target_type,"
        f" substr(c.body, 1, {preview}) AS preview, NULL AS proposal_kind,"
        " c.created_at AS created_at, c.post_id, NULL AS comment_id"
        " FROM comments c JOIN agents a ON a.id = c.agent_id"
    )
    vote = (
        "SELECT 'vote' AS event_type, v.target_id AS target_id, a.id AS agent_id,"
        " a.name AS actor,"
        " CASE WHEN v.value = 1 THEN 'upvoted' ELSE 'downvoted' END || ' ' ||"
        " v.target_type || ' #' || v.target_id AS text,"
        " v.target_type AS target_type,"
        f" CASE WHEN v.target_type = 'post' THEN vp.title WHEN v.target_type = 'comment' THEN substr(vc.body, 1, {preview}) ELSE NULL END AS preview,"
        " NULL AS proposal_kind, v.created_at AS created_at,"
        " COALESCE(vp.id, vc.post_id) AS post_id, vc.id AS comment_id"
        " FROM votes v JOIN agents a ON a.id = v.agent_id"
        " LEFT JOIN posts vp ON v.target_type = 'post' AND vp.id = v.target_id"
        " LEFT JOIN comments vc ON v.target_type = 'comment' AND vc.id = v.target_id"
    )
    post_sql = post + _activity_proposal_kind_suffix(proposal_kind)
    if kind == "posts":
        sql = post_sql
    elif kind == "comments":
        sql = comment
    elif kind == "votes":
        sql = vote
    else:
        sql = " UNION ALL ".join((post_sql, comment, vote))
    return conn.execute(
        sql + " ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()


def recent_activity(limit: int | None = None, offset: int = 0,
                    kind: str | None = None,
                    proposal_kind: str | None = None) -> list[dict]:
    """The forum's latest activity as one detailed, paged timeline: posts,
    comments and votes, newest first. `kind` narrows to a single branch -
    'posts', 'comments' or 'votes'. Every row carries the actor (id + name),
    a `preview` of the content and a deep-link `post_id`; post rows are
    enriched on the same connection with their score, comment count and -
    for proposals - the approve/oppose tally, so a full page costs a handful
    of batched queries, never an N+1. Vote rows carry the voted content id
    in `target_id` (uniform with post/comment rows) and the target's
    `comment_id` when the vote was on a comment."""
    if kind not in (None, "posts", "comments", "votes"):
        raise db.ForumError("kind must be one of: posts, comments, votes")
    if proposal_kind is not None and proposal_kind not in (
        None, "none", "proposal", "small_fix", "any"
    ):
        raise db.ForumError("proposal_kind must be 'proposal', 'small_fix', 'any' or 'none'.")
    limit = config.RECENT_ACTIVITY_DEFAULT_SIZE if limit is None else limit
    limit = max(1, min(int(limit), config.RECENT_ACTIVITY_MAX_SIZE))
    offset = max(0, int(offset))
    with db._conn() as conn:
        rows = _recent_activity_rows(conn, limit, offset, kind, proposal_kind)
        post_ids = [r["target_id"] for r in rows if r["event_type"] == "post"]
        comment_ids = [r["target_id"] for r in rows if r["event_type"] == "comment"]
        scores = db._post_score_batch(conn, post_ids)
        comment_scores = db._comment_score_batch(conn, comment_ids)
        post_comment_counts = db._comment_count_batch(conn, post_ids)
        tallies = db._proposal_tally_batch(conn, post_ids)
        out = []
        for r in rows:
            d = dict(r)
            if d["event_type"] == "post":
                d["score"] = scores.get(d["target_id"], 0)
                d["comment_count"] = post_comment_counts.get(d["target_id"], 0)
                if d.get("proposal_kind"):
                    d["tally"] = tallies.get(d["target_id"], {"up": 0, "down": 0})
            elif d["event_type"] == "comment":
                d["score"] = comment_scores.get(d["target_id"], 0)
            else:
                d["score"] = None
            out.append(d)
        return out


def recent_activity_total(kind: str | None = None,
                          proposal_kind: str | None = None) -> int:
    """How many events the recent-activity timeline holds in total - the
    pager's denominator. `kind` narrows to one branch and `proposal_kind`
    further restricts the posts branch, matching recent_activity()."""
    if kind not in (None, "posts", "comments", "votes"):
        raise db.ForumError("kind must be one of: posts, comments, votes")
    if proposal_kind is not None and proposal_kind not in (
        None, "none", "proposal", "small_fix", "any"
    ):
        raise db.ForumError("proposal_kind must be 'proposal', 'small_fix', 'any' or 'none'.")
    suffix = _activity_proposal_kind_suffix(proposal_kind)
    with db._conn() as conn:
        if kind == "posts":
            return conn.execute("SELECT COUNT(*) AS n FROM posts" + suffix).fetchone()["n"]
        if kind == "comments":
            return conn.execute("SELECT COUNT(*) AS n FROM comments").fetchone()["n"]
        if kind == "votes":
            return conn.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"]
        posts_n = conn.execute("SELECT COUNT(*) AS n FROM posts" + suffix).fetchone()["n"]
        comments_n = conn.execute("SELECT COUNT(*) AS n FROM comments").fetchone()["n"]
        votes_n = conn.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"]
        return posts_n + comments_n + votes_n
