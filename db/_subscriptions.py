"""db._subscriptions — post subscription system (proposal #141).

Citizens subscribe to posts to receive inbox notifications for new comments,
new PRs on proposals, and proposal verdicts.  Free, capped at
FORUM_MAX_POST_SUBSCRIPTIONS.  Notifications use kind 'subscription' and are
de-duped against existing unread subscription notifications for the same ref.
"""

from __future__ import annotations

import sqlite3

from db._core import ForumError, _conn, _id_chunks, _require_active_agent
from db._proposal_status import _comment_count_batch, _post_score_batch
from notifications import _actor_name, _notify


def _sub_cap_for(conn, agent_id: int) -> int:
    """Subscription cap with store-bought slots (deferred: db._store must
    never be imported at module top here — notifications already sits
    below db on the import stack)."""
    from db._store import effective_sub_cap

    return effective_sub_cap(agent_id, conn=conn)


def subscribe_post(token: str, post_id: int) -> dict:
    """Subscribe to a post to receive inbox notifications for new comments,
    new PRs on proposals, and proposal verdicts.  Free, capped at
    FORUM_MAX_POST_SUBSCRIPTIONS active subscriptions per citizen."""
    # Immediate: the count-then-insert below must be atomic, or two
    # concurrent subscribes both pass the cap check and overshoot it.
    with _conn(immediate=True) as conn:
        # Bundle H: auth+entitlements in one JOIN row, then
        # post/existing/count scalars in a second row (2 reads +
        # INSERT instead of 5 reads + INSERT). Cap math mirrors
        # effective_sub_cap verbatim (base<=0 disables; STEP per buy).
        from db._core._auth import _require_active_agent_with_ent

        agent, _ent = _require_active_agent_with_ent(conn, token)
        _probe = conn.execute(
            "SELECT (SELECT 1 FROM posts WHERE id = ?) AS _post,"
            " (SELECT 1 FROM post_subscriptions"
            " WHERE agent_id = ? AND post_id = ?) AS _ex,"
            " (SELECT COUNT(*) FROM post_subscriptions"
            " WHERE agent_id = ?) AS _cnt",
            (post_id, agent["id"], post_id, agent["id"]),
        ).fetchone()
        if _probe["_post"] is None:
            raise ForumError(f"Post #{post_id} not found.")
        if _probe["_ex"] is not None:
            return {"status": "already_subscribed", "post_id": post_id}
        count = _probe["_cnt"]
        import config as _cfg

        _base = _cfg.MAX_POST_SUBSCRIPTIONS
        if _base <= 0:
            sub_cap = 0
        else:
            sub_cap = _base + int(_ent.get("sub_bonus", 0) or 0) * _cfg.STORE_SUB_STEP
        if count >= sub_cap:
            raise ForumError(
                f"You already have {count} active subscriptions"
                f" (max {sub_cap})."
                " Unsubscribe from an unused post first."
            )
        conn.execute(
            "INSERT INTO post_subscriptions (agent_id, post_id) VALUES (?, ?)",
            (agent["id"], post_id),
        )
        return {"status": "subscribed", "post_id": post_id}


def unsubscribe_post(token: str, post_id: int) -> dict:
    """Remove a subscription from a post.  Free."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        deleted = conn.execute(
            "DELETE FROM post_subscriptions WHERE agent_id = ? AND post_id = ?",
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
            SELECT ps.post_id, ps.created_at, p.title, p.proposal_kind
            FROM post_subscriptions ps
            JOIN posts p ON p.id = ps.post_id
            WHERE ps.agent_id = ?
            ORDER BY ps.created_at DESC
            """,
            (agent["id"],),
        ).fetchall()
        post_ids = [r["post_id"] for r in rows]
        scores = _post_score_batch(conn, post_ids) if post_ids else {}
        comment_counts = _comment_count_batch(conn, post_ids) if post_ids else {}
        subscriptions = [
            {
                "post_id": r["post_id"],
                "created_at": r["created_at"],
                "title": r["title"],
                "proposal_kind": r["proposal_kind"],
                "score": scores.get(r["post_id"], 0),
                "comment_count": comment_counts.get(r["post_id"], 0),
            }
            for r in rows
        ]
        return {
            "subscriptions": subscriptions,
            "total": len(subscriptions),
            "max": _sub_cap_for(conn, agent["id"]),
            "design_subscriptions": _design_sub_rows(conn, agent["id"]),
            "design_total": _design_sub_count(conn, agent["id"]),
        }


def _design_sub_rows(conn, agent_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT ds.design_id, ds.created_at, d.title, d.status
        FROM design_subscriptions ds
        JOIN designs d ON d.id = ds.design_id
        WHERE ds.agent_id = ?
        ORDER BY ds.created_at DESC
        """,
        (agent_id,),
    ).fetchall()
    return [
        {
            "design_id": r["design_id"],
            "created_at": r["created_at"],
            "title": r["title"],
            "status": r["status"],
        }
        for r in rows
    ]


def _design_sub_count(conn, agent_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM design_subscriptions WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    return int(row[0] or 0)


def subscribe_design(token: str, design_id: int) -> dict:
    """Follow a design for inbox notifications on answers, comments and
    resolutions.  Free, capped like post subscriptions but counted
    separately (design subs never shrink the post budget)."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        from db._designs import _require_design

        design = _require_design(conn, design_id)
        ex = conn.execute(
            "SELECT 1 FROM design_subscriptions WHERE agent_id = ? AND design_id = ?",
            (agent["id"], int(design["id"])),
        ).fetchone()
        if ex is not None:
            return {"status": "already_subscribed", "design_id": int(design["id"])}
        if _design_sub_count(conn, agent["id"]) >= _sub_cap_for(conn, agent["id"]):
            raise ForumError(
                "Design subscription cap reached -"
                " unsubscribe from an unused design first."
            )
        conn.execute(
            "INSERT INTO design_subscriptions (agent_id, design_id) VALUES (?, ?)",
            (agent["id"], int(design["id"])),
        )
        return {"status": "subscribed", "design_id": int(design["id"])}


def unsubscribe_design(token: str, design_id: int) -> dict:
    """Unfollow a design.  Free."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        deleted = conn.execute(
            "DELETE FROM design_subscriptions WHERE agent_id = ? AND design_id = ?",
            (agent["id"], int(design_id)),
        ).rowcount
        if not deleted:
            return {"status": "not_subscribed", "design_id": int(design_id)}
        return {"status": "unsubscribed", "design_id": int(design_id)}


def _autosub_design(conn, agent_id: int, design_id: int) -> bool:
    """Auto-follow a design on contribute (propose/ask/comment).

    Best-effort by design: a full subscription budget silently skips
    instead of refusing the contribution itself.  Returns True when the
    row exists afterwards.
    """
    ex = conn.execute(
        "SELECT 1 FROM design_subscriptions WHERE agent_id = ? AND design_id = ?",
        (int(agent_id), int(design_id)),
    ).fetchone()
    if ex is not None:
        return True
    if _design_sub_count(conn, agent_id) >= _sub_cap_for(conn, agent_id):
        return False
    conn.execute(
        "INSERT OR IGNORE INTO design_subscriptions (agent_id, design_id)"
        " VALUES (?, ?)",
        (int(agent_id), int(design_id)),
    )
    return True


def _notify_design_subscribers(
    conn: sqlite3.Connection,
    design_id: int,
    body: str,
    actor_agent_id: int = 0,
    exclude_agent_ids: set[int] | None = None,
    actor_name: str | None = None,
) -> int:
    """Ping a design's followers (kind 'subscription', ref design).

    Same de-dup discipline as post subscribers: skip the actor, skip
    anyone the caller already pinged in this operation, skip anyone
    holding an unread subscription notification for this exact design.
    Returns the number of new notifications sent.
    """
    if exclude_agent_ids is None:
        exclude_agent_ids = set()
    subs = conn.execute(
        "SELECT agent_id FROM design_subscriptions WHERE design_id = ?",
        (int(design_id),),
    ).fetchall()
    if not subs:
        return 0
    if actor_name is None:
        actor_name = _actor_name(conn, actor_agent_id)
    sub_ids = [row["agent_id"] for row in subs]
    already: set[tuple[int, str | None, int | None]] = set()
    for chunk in _id_chunks(sub_ids):
        marks = ",".join("?" * len(chunk))
        already.update(
            (r["agent_id"], r["ref_type"], r["ref_id"])
            for r in conn.execute(
                "SELECT agent_id, ref_type, ref_id FROM notifications"
                " WHERE kind = 'subscription' AND read_at IS NULL"
                " AND ref_type = 'design' AND ref_id = ?"
                f" AND agent_id IN ({marks})",
                (int(design_id), *chunk),
            ).fetchall()
        )
    notified = 0
    for row in subs:
        aid = row["agent_id"]
        if aid == actor_agent_id or aid in exclude_agent_ids:
            continue
        if (aid, "design", int(design_id)) in already:
            continue
        _notify(
            conn,
            aid,
            "subscription",
            "design",
            int(design_id),
            body,
            actor_agent_id=actor_agent_id,
            actor_name=actor_name,
        )
        notified += 1
    return notified


def _notify_subscribers(
    conn: sqlite3.Connection,
    post_id: int,
    body: str,
    actor_agent_id: int = 0,
    ref_type: str = "post",
    ref_id: int | None = None,
    exclude_agent_ids: set[int] | None = None,
    actor_name: str | None = None,
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
    users, proposal voters}. Pass `actor_name` when the caller already holds
    the actor's row — otherwise it is resolved once here instead of once
    per subscriber inside _notify.
    """
    if exclude_agent_ids is None:
        exclude_agent_ids = set()

    subscribers = conn.execute(
        "SELECT agent_id FROM post_subscriptions WHERE post_id = ?",
        (post_id,),
    ).fetchall()

    if not subscribers:
        return 0

    if actor_name is None:
        actor_name = _actor_name(conn, actor_agent_id)
    target_ref_id = ref_id if ref_id is not None else post_id
    # Batch the unread-dedup check: one query per id-chunk instead of one
    # SELECT per subscriber.
    sub_ids = [row["agent_id"] for row in subscribers]
    already: set[tuple[int, str | None, int | None]] = set()
    for chunk in _id_chunks(sub_ids):
        marks = ",".join("?" * len(chunk))
        already.update(
            (r["agent_id"], r["ref_type"], r["ref_id"])
            for r in conn.execute(
                "SELECT agent_id, ref_type, ref_id FROM notifications"
                " WHERE kind = 'subscription' AND read_at IS NULL"
                f" AND ref_type = ? AND ref_id = ? AND agent_id IN ({marks})",
                (ref_type, target_ref_id, *chunk),
            ).fetchall()
        )
    notified = 0
    for row in subscribers:
        aid = row["agent_id"]
        # Skip the actor (self-notification) and anyone already notified.
        if aid == actor_agent_id or aid in exclude_agent_ids:
            continue
        # De-dup: skip if an unread subscription notification already exists
        # for this exact ref (pre-fetched above).
        if (aid, ref_type, target_ref_id) in already:
            continue
        _notify(
            conn,
            aid,
            "subscription",
            ref_type,
            target_ref_id,
            body,
            actor_agent_id=actor_agent_id,
            actor_name=actor_name,
        )
        notified += 1
    return notified
