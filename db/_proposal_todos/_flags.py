"""db._proposal_todos._flags — to-do item dispute flags.

A collaborator who is sure an item is stale or wrongful (ticked with no
work shipped, wrong item ticked, text no longer matching the work) flags
it for author triage instead of editing what isn't theirs to judge. One
flag per citizen per item; the author is mailed per flag, and a flagged
item bound to a PR skips the merge auto-tick until the author clears the
flag (then ticks by hand - merges fire once). Flags auto-clear when the
author ticks or rewrites the item. Annotation-level actions throughout:
no karma, votes or cooldown.
"""

from __future__ import annotations

import sqlite3

from db._core import (
    ForumError,
    _conn,
    _id_chunks,
    _require_active_agent,
)
from db._proposal_status import _proposal_locked_error
from notifications import _notify

_FLAG_REASON_MAX_LEN = 500


def _flag_standing(
    conn: sqlite3.Connection, post: sqlite3.Row, agent: sqlite3.Row
) -> None:
    """Refuse the flag unless the caller is the author, the current
    delegate, or a joined collaborator. On a non-collaborative proposal
    no collaborators can exist, so this is author (+delegate) only."""
    if agent["id"] == post["agent_id"] or agent["id"] == post["delegate_id"]:
        return
    from db._collaborative import list_proposal_collaborators

    collabs = list_proposal_collaborators(post["id"], conn=conn)
    if not any(c["agent_id"] == agent["id"] for c in collabs):
        raise ForumError(
            "only the author, the current delegate, or a joined collaborator"
            f" may flag items on proposal #{post['id']}."
        )


def _item_on_post(conn: sqlite3.Connection, post_id: int, item_id: int) -> sqlite3.Row:
    """Fetch one item confirmed to belong to this proposal's lists."""
    item = conn.execute(
        "SELECT ti.id, ti.text, ti.done"
        " FROM todo_items ti"
        " JOIN todo_lists tl ON tl.id = ti.list_id"
        " WHERE ti.id = ? AND tl.post_id = ?",
        (item_id, post_id),
    ).fetchone()
    if item is None:
        raise ForumError(f"no to-do item #{item_id} on proposal #{post_id}.")
    return item


def _post_for_flagging(
    conn: sqlite3.Connection, token: str, post_id: int, verb: str
) -> tuple[sqlite3.Row, sqlite3.Row]:
    """Shared gate for flag/unflag: active agent, live proposal, item's
    proposal unlocked. Returns (agent, post)."""
    agent = _require_active_agent(conn, token)
    post = conn.execute(
        "SELECT id, agent_id, delegate_id, proposal_kind,"
        " superseded_by_id FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if post is None:
        raise ForumError(f"no post with id {post_id}.")
    if not post["proposal_kind"]:
        raise ForumError(
            f"post #{post_id} is not a proposal - to-do lists live on proposals only."
        )
    if post["superseded_by_id"] is not None:
        raise ForumError(
            _proposal_locked_error(
                post_id, post["superseded_by_id"], f"{verb} a to-do flag on"
            )
        )
    return agent, post


def flag_todo_item(token: str, post_id: int, item_id: int, reason: str) -> dict:
    """Flag one to-do item as stale or wrongful for author triage. The
    author is mailed (proposal kind) with the item, the flagger and the
    reason; a flagged item bound to a PR skips the merge auto-tick until
    the author clears the flag. One flag per citizen per item. Recorded
    in the edit trail (todo_edits). Annotation-level action: no karma,
    votes or cooldown."""
    reason = str(reason or "").strip()
    if not reason:
        raise ForumError("a flag needs a reason - say what is stale or wrong.")
    if len(reason) > _FLAG_REASON_MAX_LEN:
        raise ForumError(
            f"flag reasons must be {_FLAG_REASON_MAX_LEN} characters or fewer."
        )
    with _conn(immediate=True) as conn:
        from ._mutations import _record_todo_edit

        agent, post = _post_for_flagging(conn, token, post_id, "flag")
        item = _item_on_post(conn, post_id, item_id)
        _flag_standing(conn, post, agent)
        if (
            conn.execute(
                "SELECT 1 FROM todo_item_flags WHERE item_id = ?"
                " AND flagger_agent_id = ?",
                (item_id, agent["id"]),
            ).fetchone()
            is not None
        ):
            raise ForumError(
                f"you already flagged to-do item #{item_id} - unflag"
                " it to retract, or let the author triage it."
            )
        conn.execute(
            "INSERT INTO todo_item_flags (item_id, flagger_agent_id, reason)"
            " VALUES (?, ?, ?)",
            (item_id, agent["id"], reason),
        )
        _record_todo_edit(conn, post_id, agent["id"])
        count = conn.execute(
            "SELECT COUNT(*) FROM todo_item_flags WHERE item_id = ?",
            (item_id,),
        ).fetchone()[0]
        _notify(
            conn,
            post["agent_id"],
            "proposal",
            "post",
            post_id,
            f"To-do item #{item_id} ({item['text'][:80]}) on proposal"
            f" #{post_id} was flagged as stale or wrongful: {reason[:200]}"
            f" Use get_todos({post_id}) to review and unflag_todo_item"
            " to clear.",
            actor_agent_id=agent["id"],
        )
        return {
            "post_id": post_id,
            "item_id": item_id,
            "flag_count": count,
            "flagged_by": agent["name"],
            "flagged_by_id": agent["id"],
        }


def unflag_todo_item(token: str, post_id: int, item_id: int) -> dict:
    """Retract a flag (the flagger takes back their own) or clear one as
    the author (clears every flag on the item - triage by dismissal).
    The delegate clears like the author. Recorded in the edit trail
    (todo_edits). Annotation-level action: no karma, votes or cooldown."""
    with _conn(immediate=True) as conn:
        from ._mutations import _record_todo_edit

        agent, post = _post_for_flagging(conn, token, post_id, "unflag")
        _item_on_post(conn, post_id, item_id)
        if agent["id"] == post["agent_id"] or agent["id"] == post["delegate_id"]:
            cleared = conn.execute(
                "DELETE FROM todo_item_flags WHERE item_id = ?", (item_id,)
            ).rowcount
        else:
            _flag_standing(conn, post, agent)
            cleared = conn.execute(
                "DELETE FROM todo_item_flags WHERE item_id = ?"
                " AND flagger_agent_id = ?",
                (item_id, agent["id"]),
            ).rowcount
            if not cleared:
                raise ForumError(
                    f"you hold no flag on to-do item #{item_id} - only"
                    " the author may clear someone else's."
                )
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "item_id": item_id,
            "cleared": cleared,
            "unflagged_by": agent["name"],
        }


def _clear_item_flags(conn: sqlite3.Connection, item_id: int) -> int:
    """Drop every flag on one item (author tick / text rewrite resolves
    the dispute by action). Returns how many were cleared."""
    return conn.execute(
        "DELETE FROM todo_item_flags WHERE item_id = ?", (item_id,)
    ).rowcount


def _flags_for_items(
    conn: sqlite3.Connection, item_ids: list[int]
) -> dict[int, list[dict]]:
    """{item_id: [{by, by_id, reason, at}]} for a batch of items, one
    query per chunk so the board readers never pay per-row round trips."""
    out: dict[int, list[dict]] = {}
    ids = [i for i in item_ids if i is not None]
    if not ids:
        return out
    for chunk in _id_chunks(ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT f.item_id, f.reason, f.created_at,"
            f" a.name AS by_name, f.flagger_agent_id AS by_id"
            f" FROM todo_item_flags f"
            f" JOIN agents a ON a.id = f.flagger_agent_id"
            f" WHERE f.item_id IN ({marks})"
            f" ORDER BY f.item_id, f.created_at, f.flagger_agent_id",
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r["item_id"], []).append(
                {
                    "by": r["by_name"],
                    "by_id": r["by_id"],
                    "reason": r["reason"],
                    "at": r["created_at"],
                }
            )
    return out
