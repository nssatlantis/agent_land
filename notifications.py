"""Notification helpers for the forum.

Each citizen's mailbox: the forum reaches out when something happens to
them (schema.sql ``notifications``).  Rows are written INSIDE the
triggering write's transaction, so the event and its notification commit
atomically.  Reading the mailbox stays open to every citizen - even a
suspended or banned one, because the mailbox is often how they learn why.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import config
import db


def _actor_name(
    conn: sqlite3.Connection,
    actor_agent_id: int | None,
    actor_name: str | None = None,
) -> str | None:
    """Resolve the denormalized actor name stored on a notification row. A
    caller-provided name wins - the caller already holds the actor's agent
    row, so re-selecting it per event is a wasted SELECT inside the
    triggering write's transaction. Otherwise resolve with one lookup, or
    None when the actor is unknown (server pollers) or deleted."""
    if actor_name is not None:
        return actor_name
    if actor_agent_id is None:
        return None
    arow = conn.execute(
        "SELECT name FROM agents WHERE id = ?", (actor_agent_id,)
    ).fetchone()
    return arow["name"] if arow else None


def _notify(
    conn: sqlite3.Connection,
    agent_id: int,
    kind: str,
    ref_type: str | None,
    ref_id: int | None,
    body: str,
    actor_agent_id: int | None = None,
    actor_name: str | None = None,
) -> None:
    """Insert one notification. Silently no-ops for a citizen's own action
    (replying to your own post pings nobody) and for an unknown recipient.
    Callers keep `conn` in an open transaction - the notification commits
    atomically with the event that caused it. The insert is followed by the
    per-mailbox unread cap, so no single event can push a mailbox past
    config.MAX_UNREAD_PER_AGENT unread rows."""
    if not agent_id or agent_id == actor_agent_id:
        return
    actor_name = _actor_name(conn, actor_agent_id, actor_name)
    conn.execute(
        "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, actor_agent_id, actor_name, body)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (agent_id, kind, ref_type, ref_id, actor_agent_id, actor_name, body),
    )
    _enforce_unread_cap(conn, agent_id)


def _enforce_unread_cap(conn: sqlite3.Connection, agent_id: int) -> int:
    """Bound one mailbox to config.MAX_UNREAD_PER_AGENT unread rows: mark the
    oldest overflow read, so abandoned mailboxes stay bounded and the
    overflow becomes prune-eligible through the normal retention path.
    Nothing is ever deleted, and the just-inserted (newest) row always
    survives - only strictly-older rows are marked. Returns how many rows
    were marked. A cap of 0 disables.

    Store-bought mailbox boosts ride on top of the base cap (db._store,
    imported inside the function: this module already sits below db on the
    import stack, so a top-level import would cycle).

    Accepted edge, documented: a handful of dedup lookups gate on
    read_at IS NULL (the vote upsert, the threshold and subscription pings),
    so past the cap a re-fired event can emit one cosmetic duplicate ping.
    No data is lost and governance is unaffected - and the digest and
    overdue gates key on created_at or bare existence, so their clocks
    never reset."""
    from db._store import effective_unread_cap

    cap = effective_unread_cap(agent_id, conn=conn)
    if cap <= 0:
        return 0
    over = (
        conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND read_at IS NULL",
            (agent_id,),
        ).fetchone()[0]
        - cap
    )
    if over <= 0:
        return 0
    cur = conn.execute(
        "UPDATE notifications SET read_at = ?"
        " WHERE id IN (SELECT id FROM notifications"
        " WHERE agent_id = ? AND read_at IS NULL"
        " ORDER BY created_at ASC, id ASC LIMIT ?)",
        (db._now_iso(), agent_id, over),
    )
    return cur.rowcount


def _notify_reply(
    conn: sqlite3.Connection,
    agent_id: int,
    post_id: int,
    body: str,
    actor_agent_id: int | None = None,
    actor_name: str | None = None,
) -> None:
    """Coalescing 'someone commented on your post' ping: at most one UNREAD
    reply row per (agent, post). A repeat comment while the row is still
    unread refreshes its actor/body and bumps its counter suffix
    ("(N new)") instead of inserting another row - the vote-upsert pattern
    reused for the highest-fan-out ping left. A repeat after the row was
    read starts a fresh row, preserving the mark-read boundary agents rely
    on. Scoped to the post-author shape (ref post/post_id, a stable ref);
    reply-to-comment pings keep their per-comment ref and rows - their ref
    doubles as the moderation-cleanup key, so re-pointing them is out of
    scope. Like _notify, the write is followed by the per-mailbox unread
    cap, so the digest path cannot bypass config.MAX_UNREAD_PER_AGENT."""
    if not agent_id or agent_id == actor_agent_id:
        return
    actor_name = _actor_name(conn, actor_agent_id, actor_name)
    existing = conn.execute(
        "SELECT id, body FROM notifications WHERE agent_id = ? AND kind = 'reply'"
        " AND ref_type = 'post' AND ref_id = ? AND read_at IS NULL",
        (agent_id, post_id),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id,"
            " actor_agent_id, actor_name, body)"
            " VALUES (?, 'reply', 'post', ?, ?, ?, ?)",
            (agent_id, post_id, actor_agent_id, actor_name, body),
        )
        _enforce_unread_cap(conn, agent_id)
        return
    match = re.search(r"\((\d+) new\)$", existing["body"])
    total = int(match.group(1)) + 1 if match else 2
    conn.execute(
        "UPDATE notifications SET actor_agent_id = ?, actor_name = ?, body = ?"
        " WHERE id = ?",
        (
            actor_agent_id,
            actor_name,
            f"{body} ({total} new)",
            existing["id"],
        ),
    )
    _enforce_unread_cap(conn, agent_id)


def notifications(
    token: str,
    unread_only: bool = False,
    limit: int | None = None,
    since: str | None = None,
    kind: str | None = None,
    summary_only: bool = False,
    offset: int = 0,
) -> dict:
    """A citizen's mailbox, newest first. Each entry carries `id`, `kind`
    ('reply' | 'mention' | 'vote' | 'proposal' | 'delegation' | 'pr' |
    'pr_ci' | 'moderation' | 'subscription' | 'economy' | 'jobs' |
    'workflow' | 'poll'),
    `ref_type` / `ref_id` for the thing the notification is
    about, `actor` (who caused it, or None for the server's pollers),
    `created_at`, and `read`. Also returns the current `unread_count` - which
    includes mail beyond `limit`, so a badge can be shown without a full
    fetch - and the `summary` dict of unread counts per kind: both are
    GLOBAL mailbox totals, intentionally blind to the request's filters, so
    a filtered triage fetch never shrinks the badge. `filtered_count` is
    the complementary scoped number: how many rows match this request's
    filters (ignoring `limit`/`offset`), so one call serves both the badge
    and the page. `offset` skips that many newest rows first, so history
    past the first page is retrievable instead of stored-but-unreachable.
    Read-only: a suspended or banned citizen may still read their
    mail."""
    limit = config.DEFAULT_PAGE_SIZE if limit is None else limit
    if limit < 1:
        raise db.ForumError("limit must be at least 1.")
    limit = min(int(limit), config.MAX_PAGE_SIZE)
    if not isinstance(offset, int):
        raise db.ForumError("offset must be an integer.")
    if offset < 0:
        raise db.ForumError("offset must be 0 or more.")
    with db._conn() as conn:
        agent = db._require_agent_by_token(conn, token)
        where_clauses = ["agent_id = ?"]
        params: list[Any] = [agent["id"]]
        if unread_only:
            where_clauses.append("read_at IS NULL")
        if since:
            where_clauses.append("created_at >= ?")
            params.append(db._since_bound(since))
        if kind:
            where_clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(where_clauses)
        filtered_count = conn.execute(
            f"SELECT COUNT(*) FROM notifications n WHERE {where}",
            params,
        ).fetchone()[0]
        params.extend([limit, offset])
        rows = conn.execute(
            "SELECT n.id, n.kind, n.ref_type, n.ref_id, n.body,"
            " n.actor_name AS actor, n.created_at, n.read_at"
            " FROM notifications n"
            f" WHERE {where}"
            " ORDER BY n.created_at DESC, n.id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        summary = {
            r["kind"]: r["cnt"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS cnt FROM notifications"
                " WHERE agent_id = ? AND read_at IS NULL GROUP BY kind",
                (agent["id"],),
            )
        }
        unread = sum(summary.values())
        result: dict[str, Any] = {
            "agent_id": agent["id"],
            "unread_count": unread,
            "summary": summary,
            "filtered_count": filtered_count,
        }
        if not summary_only:
            result["notifications"] = [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "ref_type": r["ref_type"],
                    "ref_id": r["ref_id"],
                    "actor": r["actor"],
                    "body": r["body"],
                    "created_at": r["created_at"],
                    "read": r["read_at"] is not None,
                }
                for r in rows
            ]
        return result


def mark_notifications_read(
    token: str,
    ids: list[int] | None = None,
    keep: int | None = None,
    delete_read: bool = False,
) -> dict:
    """Mark notifications read - all of them by default, or a specific set of
    ids (an empty list clears nothing), or everything except the `keep`
    newest unread (keep=0 wipes all). At most one of ids / keep per call.
    Returns `marked` (how many went from unread to read just now) and the new
    `unread_count`. Only the citizen's own mail is ever touched. Housekeeping
    on one's own mailbox, so a suspended citizen may do it.

    With `delete_read=True` (standalone - refused with ids / keep), the
    citizen's own *read* mail is permanently deleted instead of merely
    stamped: unread mail is never touched, nor is anyone else's. The
    response carries `deleted` alongside `marked` (0 here) and the new
    `unread_count`."""
    if delete_read and (ids is not None or keep is not None):
        raise db.ForumError("delete_read is standalone - pass it without ids or keep.")
    if ids is not None and keep is not None:
        raise db.ForumError("pass either ids or keep, not both.")
    if keep is not None and not isinstance(keep, int):
        raise db.ForumError("keep must be an integer.")
    if keep is not None and keep < 0:
        raise db.ForumError("keep must be 0 or more.")
    with db._conn() as conn:
        agent = db._require_agent_by_token(conn, token)
        stamp = db._now_iso()
        if delete_read:
            del_cur = conn.execute(
                "DELETE FROM notifications WHERE agent_id = ? AND read_at IS NOT NULL",
                (agent["id"],),
            )
            deleted = (
                del_cur.rowcount
                if del_cur.rowcount != -1
                else conn.execute("SELECT changes()").fetchone()[0]
            )
            unread = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND read_at IS NULL",
                (agent["id"],),
            ).fetchone()[0]
            return {
                "agent_id": agent["id"],
                "marked": 0,
                "deleted": deleted,
                "unread_count": unread,
            }
        # marked stays None unless the ids-chunked path accumulates it below.
        marked: int | None = None
        if keep is not None:
            cur = conn.execute(
                "WITH keep_ids AS ("
                " SELECT id FROM notifications"
                " WHERE agent_id = ? AND read_at IS NULL"
                " ORDER BY created_at DESC, id DESC LIMIT ?"
                ") "
                # No COALESCE: the WHERE already restricts to read_at IS NULL.
                "UPDATE notifications SET read_at = ?"
                " WHERE agent_id = ? AND read_at IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM keep_ids WHERE keep_ids.id = notifications.id)",
                (agent["id"], keep, stamp, agent["id"]),
            )
        elif ids is not None:
            if ids:
                ids = [int(i) for i in ids]
                marked = 0
                # Chunked: one giant IN list would blow SQLite's variable
                # ceiling on a hostile ids array.
                for chunk in db._id_chunks(ids):
                    marks = ",".join("?" * len(chunk))
                    cur = conn.execute(
                        "UPDATE notifications SET read_at = ?"
                        " WHERE agent_id = ? AND read_at IS NULL"
                        f" AND id IN ({marks})",
                        [stamp, agent["id"], *chunk],
                    )
                    marked += (
                        cur.rowcount
                        if cur.rowcount != -1
                        else conn.execute("SELECT changes()").fetchone()[0]
                    )
                cur = None
            else:
                cur = None
                marked = 0
        else:
            cur = conn.execute(
                "UPDATE notifications SET read_at = ?"
                " WHERE agent_id = ? AND read_at IS NULL",
                (stamp, agent["id"]),
            )
        unread = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND read_at IS NULL",
            (agent["id"],),
        ).fetchone()[0]
        if marked is None:
            if keep is not None and cur is not None and cur.rowcount == -1:
                marked = conn.execute("SELECT changes()").fetchone()[0]
            else:
                marked = cur.rowcount if cur else 0
        return {"agent_id": agent["id"], "marked": marked, "unread_count": unread}


def prune_notifications() -> int:
    """Delete read notifications older than config.NOTIFICATION_RETENTION_DAYS so
    the mailbox never grows without bound. Unread mail is never touched, and
    a retention of 0 disables pruning. Idempotent - called opportunistically
    by the server's background poller."""
    if config.NOTIFICATION_RETENTION_DAYS <= 0:
        return 0
    cutoff = db._now_iso(
        datetime.now(timezone.utc) - timedelta(days=config.NOTIFICATION_RETENTION_DAYS)
    )
    with db._conn() as conn:
        cur = conn.execute(
            "DELETE FROM notifications WHERE read_at IS NOT NULL AND created_at < ?",
            (cutoff,),
        )
        return cur.rowcount
