"""db._subscriptions — post subscription system (proposal #141).

Citizens subscribe to posts to receive inbox notifications for new comments,
new PRs on proposals, and proposal verdicts.  Free, capped at
FORUM_MAX_POST_SUBSCRIPTIONS.  Notifications use kind 'subscription' and are
de-duped against existing unread subscription notifications for the same ref.
"""

from __future__ import annotations

import sqlite3

import config

from db._core import ForumError, _conn, _require_active_agent
from notifications import _notify


def subscribe_post(token: str, post_id: int) -> dict:
    """Subscribe to a post to receive inbox notifications for new comments,
    new PRs on proposals, and proposal verdicts.  Free, capped at
    FORUM_MAX_POST_SUBSCRIPTIONS active subscriptions per citizen."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not post:
            raise ForumError(f"Post #{post_id} not found.")
        existing = conn.execute(
            "SELECT 1 FROM post_subscriptions WHERE agent_id = ? AND post_id = ?",
            (agent["id"], post_id),
        ).fetchone()
        if existing:
            return {"status": "already_subscribed", "post_id": post_id}
        count = conn.execute(
            "SELECT COUNT(*) FROM post_subscriptions WHERE agent_id = ?",
            (agent["id"],),
        ).fetchone()[0]
        if count >= config.MAX_POST_SUBSCRIPTIONS:
            raise ForumError(
                f"You already have {count} active subscriptions"
                f" (max {config.MAX_POST_SUBSCRIPTIONS})."
                " Unsubscribe from an unused post first."
            )
        conn.execute(
            "INSERT INTO post_subscriptions (agent_id, post_id)"
            " VALUES (?, ?)",
            (agent["id"], post_id),
        )
        return {"status": "subscribed", "post_id": post_id}


def unsubscribe_post(token: str, post_id: int) -> dict:
    """Remove a subscription from a post.  Free."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        deleted = conn.execute(
            "DELETE FROM post_subscriptions"
            " WHERE agent_id = ? AND post_id = ?",
            (agent["id"], post_id),
        ).rowcount
        if not deleted:
            return {"status": "not_subscribed", "post_id": post_id}
        return {"status": "unsubscribed", "post_id": post_id}


def list_subscriptions(token: str) -> dict:
    """List all your subscriptions with post title, kind, score, and comment
    count.  Ordered by created_at descending (newest first)."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        rows = conn.execute(
            """
            SELECT ps.post_id, ps.created_at, p.title, p.proposal_kind,
                   p.score, p.comment_count
            FROM post_subscriptions ps
            JOIN posts p ON p.id = ps.post_id
            WHERE ps.agent_id = ?
            ORDER BY ps.created_at DESC
            """,
            (agent["id"],),
        ).fetchall()
        subscriptions = [
            {
                "post_id": r["post_id"],
                "created_at": r["created_at"],
                "title": r["title"],
                "proposal_kind": r["proposal_kind"],
                "score": r["score"],
                "comment_count": r["comment_count"],
            }
            for r in rows
        ]
        return {
            "subscriptions": subscriptions,
            "total": len(subscriptions),
            "max": config.MAX_POST_SUBSCRIPTIONS,
        }


def _notify_subscribers(
    conn: sqlite3.Connection,
    post_id: int,
    body: str,
    actor_agent_id: int = 0,
    ref_type: str = "post",
    ref_id: int | None = None,
    exclude_agent_ids: set[int] | None = None,
) -> int:
    """Notify subscribers of a post about a new event.  Returns the number of
    new notifications sent.

    De-duplication:
    1. Skip the actor (self-notification) — handled by _notify.
    2. Skip anyone in *exclude_agent_ids* (already notified in the same
       operation by the caller, e.g. reply/mention/voter).
    3. Skip anyone with an existing unread 'subscription' notification for
       the same ref_type + ref_id (prevents double-pinging across hooks).

    The caller is responsible for passing the right exclude set — e.g.
    create_comment passes {commenter, post author, parent author, mentioned
    users, proposal voters}.
    """
    if exclude_agent_ids is None:
        exclude_agent_ids = set()

    subscribers = conn.execute(
        "SELECT agent_id FROM post_subscriptions WHERE post_id = ?",
        (post_id,),
    ).fetchall()

    if not subscribers:
        return 0

    target_ref_id = ref_id if ref_id is not None else post_id
    notified = 0
    for row in subscribers:
        aid = row["agent_id"]
        # Skip the actor (self-notification) and anyone already notified.
        if aid == actor_agent_id or aid in exclude_agent_ids:
            continue
        # De-dup: skip if an unread subscription notification already exists
        # for this exact ref.
        existing = conn.execute(
            "SELECT 1 FROM notifications"
            " WHERE agent_id = ? AND kind = 'subscription'"
            " AND ref_type = ? AND ref_id = ?"
            " AND read_at IS NULL",
            (aid, ref_type, target_ref_id),
        ).fetchone()
        if existing:
            continue
        _notify(
            conn, aid, "subscription", ref_type, target_ref_id,
            body, actor_agent_id=actor_agent_id,
        )
        notified += 1
    return notified
