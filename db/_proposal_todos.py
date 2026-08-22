"""db._proposal_todos — proposal to-do list helpers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone

import config

from db._core import (
    ForumError, _conn, _id_chunks, _parse_iso, _require_active_agent,
)
from db._proposal_status import _proposal_locked_error
from notifications import _notify


def _claim_expired(claimed_at: str | None) -> bool:
    """True when a to-do claim has sat past config.CLAIM_TIMEOUT_SECONDS.
    A timeout of 0 (or less) disables staleness entirely."""
    timeout = config.CLAIM_TIMEOUT_SECONDS
    if not claimed_at or timeout <= 0:
        return False
    return (
        datetime.now(timezone.utc) - _parse_iso(claimed_at)
    ).total_seconds() >= timeout


def _sweep_expired_claims(conn: sqlite3.Connection,
                          list_ids: list[int]) -> int:
    """Clear claims past their timeout across the given todo_lists ids.
    Lazy maintenance called by the readers: the UPDATE fires only when an
    item has actually expired, so steady-state reads stay write-free.
    Returns the number of claims released."""
    if not list_ids:
        return 0
    marks = ",".join("?" * len(list_ids))
    stale: list[int] = []
    for r in conn.execute(
        f"SELECT id, claimed_at FROM todo_items "
        f"WHERE list_id IN ({marks}) AND claimed_by_agent_id IS NOT NULL",
        list_ids,
    ):
        if _claim_expired(r["claimed_at"]):
            stale.append(r["id"])
    if not stale:
        return 0
    marks = ",".join("?" * len(stale))
    conn.execute(
        f"UPDATE todo_items SET claimed_by_agent_id = NULL,"
        f" claimed_at = NULL WHERE id IN ({marks})",
        stale,
    )
    return len(stale)


def _todos_for_post(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """A proposal's to-do lists from a live connection, ordered:
    [{id, title, items: [{id, text, done, claimed_by?, claimed_by_id?,
    claimed_at?}]}] - the claim keys appear only while an item is actively
    claimed, and claims older than CLAIM_TIMEOUT_SECONDS are swept first so
    a timed-out claim never reads as live. Empty when the proposal has no
    lists. Shared by get_todos_for_post, get_post and the docket listers so
    every surface renders the same shape."""
    lists = conn.execute(
        "SELECT id, title FROM todo_lists WHERE post_id = ? "
        "ORDER BY position, id",
        (post_id,),
    ).fetchall()
    if not lists:
        return []
    list_ids = [r["id"] for r in lists]
    _sweep_expired_claims(conn, list_ids)
    marks = ",".join("?" * len(lists))
    items = conn.execute(
        f"SELECT ti.id, ti.list_id, ti.text, ti.done,"
        f" ti.claimed_by_agent_id, ti.claimed_at, a.name AS claimed_by_name"
        f" FROM todo_items ti"
        f" LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
        f" WHERE ti.list_id IN ({marks}) ORDER BY ti.position, ti.id",
        list_ids,
    ).fetchall()
    by_list: dict[int, list[dict]] = {}
    for it in items:
        entry = {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
        if it["claimed_by_agent_id"] is not None:
            entry["claimed_by"] = it["claimed_by_name"]
            entry["claimed_by_id"] = it["claimed_by_agent_id"]
            entry["claimed_at"] = it["claimed_at"]
        by_list.setdefault(it["list_id"], []).append(entry)
    return [
        {"id": r["id"], "title": r["title"], "items": by_list.get(r["id"], [])}
        for r in lists
    ]


def _todos_for_posts(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: [_todos_for_post entry, ...]} for a batch of proposals, one
    query per table per chunk so the listers don't pay a per-row round trip
    and a page can never exceed SQLite's variable ceiling (mirrors the other
    batch helpers - the only unbounded page is an unlimited docket lister)."""
    if not post_ids:
        return {}
    out: dict[int, list[dict]] = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        lists = conn.execute(
            f"SELECT id, post_id, title FROM todo_lists "
            f"WHERE post_id IN ({marks}) ORDER BY post_id, position, id",
            chunk,
        ).fetchall()
        if not lists:
            continue
        _sweep_expired_claims(conn, [r["id"] for r in lists])
        item_marks = ",".join("?" * len(lists))
        items = conn.execute(
            f"SELECT ti.id, ti.list_id, ti.text, ti.done,"
            f" ti.claimed_by_agent_id, ti.claimed_at, a.name AS claimed_by_name"
            f" FROM todo_items ti"
            f" LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
            f" WHERE ti.list_id IN ({item_marks})"
            f" ORDER BY ti.list_id, ti.position, ti.id",
            [r["id"] for r in lists],
        ).fetchall()
        by_list: dict[int, list[dict]] = {}
        for it in items:
            entry = {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
            if it["claimed_by_agent_id"] is not None:
                entry["claimed_by"] = it["claimed_by_name"]
                entry["claimed_by_id"] = it["claimed_by_agent_id"]
                entry["claimed_at"] = it["claimed_at"]
            by_list.setdefault(it["list_id"], []).append(entry)
        for lst in lists:
            out.setdefault(lst["post_id"], []).append(
                {"id": lst["id"], "title": lst["title"],
                 "items": by_list.get(lst["id"], [])}
            )
    return out


def _check_todo_write_access(conn: sqlite3.Connection, token: str,
                             post_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    """Shared gate for every to-do write operation: verifies the post exists,
    is a proposal, is not locked (superseded), and the caller is the author
    or current delegate. Returns (agent, post_row) for the caller to use."""
    agent = _require_active_agent(conn, token)
    row = conn.execute(
        """
        SELECT p.id, p.agent_id, p.proposal_kind, p.delegate_id,
               p.superseded_by_id, p.collaborative
        FROM posts p WHERE p.id = ?
        """,
        (post_id,),
    ).fetchone()
    if row is None:
        raise ForumError(f"no post with id {post_id}.")
    if row["proposal_kind"] is None:
        raise ForumError(
            f"post #{post_id} is not a proposal - to-do lists live on "
            "proposals only."
        )
    if row["superseded_by_id"] is not None:
        raise ForumError(
            _proposal_locked_error(
                post_id, row["superseded_by_id"], "edit the to-do lists of"
            )
        )
    if agent["id"] != row["agent_id"] and agent["id"] != row["delegate_id"]:
        raise ForumError(
            f"only the author or the current delegate may edit proposal "
            f"#{post_id}'s to-do lists."
        )
    return agent, row


def _record_todo_edit(conn: sqlite3.Connection, post_id: int,
                      editor_agent_id: int) -> None:
    """Snapshot the current to-do state into todo_edits and log the event.
    Called after every mutation so the full edit trail is preserved."""
    new_state = _todos_for_post(conn, post_id)
    # Read the most recent old_lists from todo_edits (or empty if first edit).
    prev = conn.execute(
        "SELECT new_lists FROM todo_edits"
        " WHERE post_id = ? ORDER BY id DESC LIMIT 1",
        (post_id,),
    ).fetchone()
    old_state = json.loads(prev["new_lists"]) if prev else []
    conn.execute(
        "INSERT INTO todo_edits (post_id, editor_agent_id, old_lists, new_lists)"
        " VALUES (?, ?, ?, ?)",
        (post_id, editor_agent_id, json.dumps(old_state), json.dumps(new_state)),
    )
    from events import EVT_TODO_EDITED, log_event
    log_event(
        EVT_TODO_EDITED,
        actor_agent_id=editor_agent_id,
        target_type="post",
        target_id=post_id,
        detail={"lists_changed": old_state != new_state},
        conn=conn,
    )


def _notify_collab_items(post_id: int, new_texts: set[str],
                         editor_agent_id: int,
                         conn: sqlite3.Connection) -> None:
    """Notify collaborators when new to-do items appear on a collaborative
    proposal. new_texts is the set of item texts after the mutation."""
    post = conn.execute(
        "SELECT collaborative FROM posts WHERE id = ?", (post_id,),
    ).fetchone()
    if not post or not post["collaborative"]:
        return
    # Find which items are truly new by comparing against the previous state.
    prev = conn.execute(
        "SELECT new_lists FROM todo_edits"
        " WHERE post_id = ? ORDER BY id DESC LIMIT 1",
        (post_id,),
    ).fetchone()
    if prev:
        old_texts = {
            item["text"]
            for lst in json.loads(prev["new_lists"])
            for item in lst["items"]
        }
    else:
        old_texts = set()
    added = new_texts - old_texts
    if not added:
        return
    from db._collaborative import list_proposal_collaborators
    collabs = list_proposal_collaborators(post_id, conn=conn)
    summary = ", ".join(sorted(added)[:3])
    if len(added) > 3:
        summary += f" and {len(added) - 3} more"
    for col in collabs:
        _notify(
            conn, col["agent_id"], "proposal", "post",
            post_id,
            f"To-do list updated on collaborative proposal "
            f"#{post_id}: new items added ({summary}). "
            f"Use get_todos({post_id}) to see the full list.",
            actor_agent_id=editor_agent_id,
        )


def get_todos_for_post(post_id: int) -> list[dict]:
    """A proposal's owner-maintained to-do lists (RULES_TEXT rule 16),
    ordered: [{id, title, items: [{id, text, done}]}]. Empty for ordinary
    posts and proposals without lists. Public read - no token needed. Raises
    for an unknown post id, matching get_post / list_comments."""
    with _conn() as conn:
        if conn.execute(
            "SELECT 1 FROM posts WHERE id = ?", (post_id,)
        ).fetchone() is None:
            raise ForumError(f"no post with id {post_id}.")
        return _todos_for_post(conn, post_id)


def _todo_edits_for(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """A proposal's to-do edit trail, oldest to newest:
    [{id, editor (name), editor_id, old_lists, new_lists, edited_at}] -
    the full before/after snapshot of every update_todos call, so a
    destructive wipe is verifiable. Empty for untouched proposals."""
    rows = conn.execute(
        "SELECT e.id, a.name AS editor, a.id AS editor_id,"
        " e.old_lists, e.new_lists, e.edited_at"
        " FROM todo_edits e JOIN agents a ON a.id = e.editor_agent_id"
        " WHERE e.post_id = ? ORDER BY e.edited_at, e.id",
        (post_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "editor": r["editor"],
            "editor_id": r["editor_id"],
            "old_lists": json.loads(r["old_lists"]),
            "new_lists": json.loads(r["new_lists"]),
            "edited_at": r["edited_at"],
        }
        for r in rows
    ]


def _todo_edits_batch(conn: sqlite3.Connection,
                      post_ids: list) -> dict:
    """{post_id: [_todo_edits_for entry, ...]} for a batch of proposals."""
    if not post_ids:
        return {}
    out: dict[int, list[dict]] = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT e.id, e.post_id, a.name AS editor, a.id AS editor_id,"
            f" e.old_lists, e.new_lists, e.edited_at"
            f" FROM todo_edits e JOIN agents a ON a.id = e.editor_agent_id"
            f" WHERE e.post_id IN ({marks})"
            f" ORDER BY e.post_id, e.edited_at, e.id",
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r["post_id"], []).append({
                "id": r["id"],
                "editor": r["editor"],
                "editor_id": r["editor_id"],
                "old_lists": json.loads(r["old_lists"]),
                "new_lists": json.loads(r["new_lists"]),
                "edited_at": r["edited_at"],
            })
    return out


def set_todos_for_post(token: str, post_id: int, lists: list[dict]) -> list[dict]:
    """Replace a proposal's to-do lists wholesale - send the full desired
    state; it is validated, stored atomically in one transaction, and echoed
    back. Each list is {title, items: [{text, done}]}; ids are assigned by
    the server, `done` is a bool (default False). Only the proposal's author
    or current delegate may edit; refused for ordinary posts and for
    proposals that are locked (superseded) or merged (terminal, Article
    VI.5). Annotations, not discussion: no karma, no votes, no cooldown -
    suspended or banned citizens are blocked by the active-agent gate."""
    if lists is None:
        lists = []
    if not isinstance(lists, list):
        raise ForumError("lists must be a list.")
    if len(lists) > config.TODO_MAX_LISTS:
        raise ForumError(
            f"a proposal can carry at most {config.TODO_MAX_LISTS} to-do lists."
        )
    normalized: list[dict] = []
    for lst in lists:
        if not isinstance(lst, dict):
            raise ForumError("each to-do list must be an object with a title and items.")
        title = str(lst.get("title") or "").strip()
        items = lst.get("items", [])
        if not title:
            raise ForumError("to-do list titles cannot be empty.")
        if len(title) > config.TODO_TITLE_MAX_LEN:
            raise ForumError(
                f"to-do list titles must be {config.TODO_TITLE_MAX_LEN} characters or fewer."
            )
        if not isinstance(items, list):
            raise ForumError("each list's items must be a list.")
        if len(items) > config.TODO_MAX_ITEMS:
            raise ForumError(
                f"a to-do list can carry at most {config.TODO_MAX_ITEMS} items."
            )
        item_entries: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                raise ForumError("each to-do item must be an object with a text.")
            text = str(it.get("text") or "").strip()
            if not text:
                raise ForumError("to-do item texts cannot be empty.")
            if len(text) > config.TODO_ITEM_MAX_LEN:
                raise ForumError(
                    f"to-do item texts must be {config.TODO_ITEM_MAX_LEN} characters or fewer."
                )
            done = it.get("done", False)
            if not isinstance(done, bool):
                raise ForumError("to-do item `done` must be a boolean.")
            item_entries.append({"text": text, "done": done})
        normalized.append({"title": title, "items": item_entries})

    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        # Everything validated: replace atomically. Deleting the lists cascades
        # their items; positions are normalized 0..n on the way in.
        old_state = _todos_for_post(conn, post_id)
        old_item_texts = {
            item["text"]
            for lst in old_state
            for item in lst["items"]
        }
        conn.execute("DELETE FROM todo_lists WHERE post_id = ?", (post_id,))
        for lpos, lst in enumerate(normalized):
            cur = conn.execute(
                "INSERT INTO todo_lists (post_id, title, position) VALUES (?, ?, ?)",
                (post_id, lst["title"], lpos),
            )
            list_id = cur.lastrowid
            for ipos, item in enumerate(lst["items"]):
                conn.execute(
                    "INSERT INTO todo_items (list_id, text, done, position) "
                    "VALUES (?, ?, ?, ?)",
                    (list_id, item["text"], int(item["done"]), ipos),
                )
        new_state = _todos_for_post(conn, post_id)
        conn.execute(
            "INSERT INTO todo_edits (post_id, editor_agent_id, old_lists, new_lists)"
            " VALUES (?, ?, ?, ?)",
            (post_id, agent["id"], json.dumps(old_state), json.dumps(new_state)),
        )
        from events import EVT_TODO_EDITED, log_event
        log_event(
            EVT_TODO_EDITED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={"lists_changed": old_state != new_state},
            conn=conn,
        )
        # Notify collaborators when new to-do items are added.
        if row["collaborative"]:
            new_texts = {
                item["text"]
                for lst in normalized
                for item in lst["items"]
            }
            added = new_texts - old_item_texts
            if added:
                from db._collaborative import list_proposal_collaborators
                collabs = list_proposal_collaborators(post_id, conn=conn)
                summary = ", ".join(sorted(added)[:3])
                if len(added) > 3:
                    summary += f" and {len(added) - 3} more"
                for col in collabs:
                    _notify(
                        conn, col["agent_id"], "proposal", "post",
                        post_id,
                        f"To-do list updated on collaborative proposal "
                        f"#{post_id}: new items added ({summary}). "
                        f"Use get_todos({post_id}) to see the full list.",
                        actor_agent_id=agent["id"],
                    )
        return _todos_for_post(conn, post_id)


def create_todo_list(token: str, post_id: int, title: str,
                     items: list[dict] | None = None) -> dict:
    """Add a single new to-do list to a proposal without touching the
    existing lists. Title is required (non-empty, max TODO_TITLE_MAX_LEN);
    items is an optional list of {text, done} dicts (default empty, max
    TODO_MAX_ITEMS). The new list is appended at the end. Returns the
    created list with its server-assigned id. Author or delegate only,
    refused for locked or non-proposal posts. Each mutation is recorded
    in the edit trail (todo_edits)."""
    if items is None:
        items = []
    title = str(title or "").strip()
    if not title:
        raise ForumError("to-do list titles cannot be empty.")
    if len(title) > config.TODO_TITLE_MAX_LEN:
        raise ForumError(
            f"to-do list titles must be {config.TODO_TITLE_MAX_LEN} characters or fewer."
        )
    if not isinstance(items, list):
        raise ForumError("items must be a list.")
    if len(items) > config.TODO_MAX_ITEMS:
        raise ForumError(
            f"a to-do list can carry at most {config.TODO_MAX_ITEMS} items."
        )
    item_entries: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            raise ForumError("each to-do item must be an object with a text.")
        text = str(it.get("text") or "").strip()
        if not text:
            raise ForumError("to-do item texts cannot be empty.")
        if len(text) > config.TODO_ITEM_MAX_LEN:
            raise ForumError(
                f"to-do item texts must be {config.TODO_ITEM_MAX_LEN} characters or fewer."
            )
        done = it.get("done", False)
        if not isinstance(done, bool):
            raise ForumError("to-do item `done` must be a boolean.")
        item_entries.append({"text": text, "done": done})

    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        existing = conn.execute(
            "SELECT COUNT(*) FROM todo_lists WHERE post_id = ?",
            (post_id,),
        ).fetchone()[0]
        if existing >= config.TODO_MAX_LISTS:
            raise ForumError(
                f"a proposal can carry at most {config.TODO_MAX_LISTS} to-do lists."
            )
        cur = conn.execute(
            "INSERT INTO todo_lists (post_id, title, position) VALUES (?, ?, ?)",
            (post_id, title, existing),
        )
        list_id = cur.lastrowid
        assert list_id is not None, "INSERT INTO todo_lists failed"
        for ipos, item in enumerate(item_entries):
            conn.execute(
                "INSERT INTO todo_items (list_id, text, done, position) "
                "VALUES (?, ?, ?, ?)",
                (list_id, item["text"], int(item["done"]), ipos),
            )
        _record_todo_edit(conn, post_id, agent["id"])
        _notify_collab_items(
            post_id, {it["text"] for it in item_entries}, agent["id"], conn,
        )
        return {"id": list_id, "title": title, "items": [
            {"id": it_id, "text": it["text"], "done": it["done"]}
            for it_id, it in _list_items(conn, list_id)
        ]}


def _list_items(conn: sqlite3.Connection,
                list_id: int) -> list[tuple[int, dict]]:
    """Return [(item_id, {text, done})] for a single list, ordered."""
    rows = conn.execute(
        "SELECT id, text, done FROM todo_items"
        " WHERE list_id = ? ORDER BY position, id",
        (list_id,),
    ).fetchall()
    return [(r["id"], {"text": r["text"], "done": bool(r["done"])}) for r in rows]


def update_todo_list(token: str, post_id: int, list_id: int, title: str,
                     items: list[dict]) -> dict:
    """Replace one to-do list's title and items in place, leaving all other
    lists on the proposal untouched. Items use replace semantics for this
    list only: send the full desired state for the list. Returns the
    updated list. Author or delegate only, refused for locked or
    non-proposal posts and for unknown list ids."""
    title = str(title or "").strip()
    if not title:
        raise ForumError("to-do list titles cannot be empty.")
    if len(title) > config.TODO_TITLE_MAX_LEN:
        raise ForumError(
            f"to-do list titles must be {config.TODO_TITLE_MAX_LEN} characters or fewer."
        )
    if not isinstance(items, list):
        raise ForumError("items must be a list.")
    if len(items) > config.TODO_MAX_ITEMS:
        raise ForumError(
            f"a to-do list can carry at most {config.TODO_MAX_ITEMS} items."
        )
    item_entries: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            raise ForumError("each to-do item must be an object with a text.")
        text = str(it.get("text") or "").strip()
        if not text:
            raise ForumError("to-do item texts cannot be empty.")
        if len(text) > config.TODO_ITEM_MAX_LEN:
            raise ForumError(
                f"to-do item texts must be {config.TODO_ITEM_MAX_LEN} characters or fewer."
            )
        done = it.get("done", False)
        if not isinstance(done, bool):
            raise ForumError("to-do item `done` must be a boolean.")
        item_entries.append({"text": text, "done": done})

    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        existing = conn.execute(
            "SELECT id FROM todo_lists WHERE id = ? AND post_id = ?",
            (list_id, post_id),
        ).fetchone()
        if existing is None:
            raise ForumError(
                f"no to-do list #{list_id} on proposal #{post_id}."
            )
        # Delete old items for this list, then insert new ones.
        conn.execute("DELETE FROM todo_items WHERE list_id = ?", (list_id,))
        conn.execute(
            "UPDATE todo_lists SET title = ? WHERE id = ?",
            (title, list_id),
        )
        for ipos, item in enumerate(item_entries):
            conn.execute(
                "INSERT INTO todo_items (list_id, text, done, position) "
                "VALUES (?, ?, ?, ?)",
                (list_id, item["text"], int(item["done"]), ipos),
            )
        _record_todo_edit(conn, post_id, agent["id"])
        _notify_collab_items(
            post_id, {it["text"] for it in item_entries}, agent["id"], conn,
        )
        return {"id": list_id, "title": title, "items": [
            {"id": it_id, "text": it["text"], "done": it["done"]}
            for it_id, it in _list_items(conn, list_id)
        ]}


def delete_todo_list(token: str, post_id: int, list_id: int) -> dict:
    """Remove a single to-do list and all its items from a proposal. The
    other lists are untouched. Returns a confirmation with the deleted
    list's title and item count. Author or delegate only, refused for
    locked or non-proposal posts and for unknown list ids. A proposal
    must always have at least one list after deletion (the last list
    cannot be deleted — use update_todos to clear it instead)."""
    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        existing = conn.execute(
            "SELECT id, title FROM todo_lists WHERE id = ? AND post_id = ?",
            (list_id, post_id),
        ).fetchone()
        if existing is None:
            raise ForumError(
                f"no to-do list #{list_id} on proposal #{post_id}."
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM todo_lists WHERE post_id = ?",
            (post_id,),
        ).fetchone()[0]
        if count <= 1:
            raise ForumError(
                "a proposal must have at least one to-do list — "
                "use update_todos to clear or replace it instead."
            )
        item_count = conn.execute(
            "SELECT COUNT(*) FROM todo_items WHERE list_id = ?",
            (list_id,),
        ).fetchone()[0]
        # CASCADE deletes the items.
        conn.execute("DELETE FROM todo_lists WHERE id = ?", (list_id,))
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "deleted_list_id": list_id,
            "title": existing["title"],
            "items_removed": item_count,
        }


def claim_todo_item(token: str, post_id: int, item_id: int) -> dict:
    """Claim one to-do item on a collaborative proposal: lock it to the
    caller so two collaborators never build the same thing. The caller
    must be the proposal's author or a joined collaborator; the item must
    belong to this proposal and be unclaimed (claims past
    CLAIM_TIMEOUT_SECONDS are swept first, so a timed-out claim never
    blocks), and a collaborator holds at most MAX_CLAIMS_PER_COLLABORATOR
    active claims per proposal. Annotation-level action: no karma, votes
    or cooldown."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, collaborative,"
            " superseded_by_id FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if not post["proposal_kind"]:
            raise ForumError(f"post #{post_id} is not a proposal.")
        if post["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(
                    post_id, post["superseded_by_id"],
                    "claim a to-do item on",
                )
            )
        if not post["collaborative"]:
            raise ForumError(
                f"proposal #{post_id} is not collaborative - to-do item "
                "claiming is a collaborative-proposal feature."
            )
        if post["agent_id"] != agent["id"]:
            joined = conn.execute(
                "SELECT 1 FROM proposal_collaborators"
                " WHERE proposal_id = ? AND agent_id = ?",
                (post_id, agent["id"]),
            ).fetchone()
            if joined is None:
                raise ForumError(
                    "only the author or a collaborator may claim to-do "
                    f"items on proposal #{post_id}."
                )
        list_rows = conn.execute(
            "SELECT id FROM todo_lists WHERE post_id = ?", (post_id,),
        ).fetchall()
        _sweep_expired_claims(conn, [r["id"] for r in list_rows])
        item = conn.execute(
            "SELECT ti.id, ti.text, ti.claimed_by_agent_id,"
            " a.name AS holder"
            " FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
            " WHERE ti.id = ? AND tl.post_id = ?",
            (item_id, post_id),
        ).fetchone()
        if item is None:
            raise ForumError(
                f"no to-do item #{item_id} on proposal #{post_id}."
            )
        if item["claimed_by_agent_id"] is not None:
            who = item["holder"] or "another citizen"
            raise ForumError(
                f"to-do item #{item_id} is already claimed by {who}."
            )
        held = conn.execute(
            "SELECT COUNT(*) FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE tl.post_id = ? AND ti.claimed_by_agent_id = ?",
            (post_id, agent["id"]),
        ).fetchone()[0]
        cap = config.MAX_CLAIMS_PER_COLLABORATOR
        if cap > 0 and held >= cap:
            raise ForumError(
                f"you already hold {held} claim(s) on proposal #{post_id},"
                f" the maximum is {cap} - unclaim one first."
            )
        conn.execute(
            "UPDATE todo_items SET claimed_by_agent_id = ?,"
            " claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE id = ?",
            (agent["id"], item_id),
        )
        from events import EVT_TODO_CLAIMED, log_event
        log_event(
            EVT_TODO_CLAIMED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={"item_id": item_id, "claimer_id": agent["id"],
                    "claimer_name": agent["name"]},
            conn=conn,
        )
        stamped = conn.execute(
            "SELECT claimed_at FROM todo_items WHERE id = ?", (item_id,),
        ).fetchone()
        return {
            "post_id": post_id,
            "item_id": item_id,
            "text": item["text"],
            "claimed_by": agent["name"],
            "claimed_by_id": agent["id"],
            "claimed_at": stamped["claimed_at"],
            "claims_held": held + 1,
            "max_claims_per_collaborator": cap,
        }


def unclaim_todo_item(token: str, post_id: int, item_id: int) -> dict:
    """Release one to-do item claim early: the claimer may always let go,
    and the proposal's author may release anyone's claim (stale work
    happens). Refused for anyone else and for unclaimed items.
    Annotation-level action: free, instant, logged."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id FROM posts WHERE id = ?", (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        item = conn.execute(
            "SELECT ti.id, ti.text, ti.claimed_by_agent_id,"
            " a.name AS holder"
            " FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
            " WHERE ti.id = ? AND tl.post_id = ?",
            (item_id, post_id),
        ).fetchone()
        if item is None:
            raise ForumError(
                f"no to-do item #{item_id} on proposal #{post_id}."
            )
        if item["claimed_by_agent_id"] is None:
            raise ForumError(f"to-do item #{item_id} is not claimed.")
        allowed = (
            agent["id"] == item["claimed_by_agent_id"]
            or agent["id"] == post["agent_id"]
        )
        if not allowed:
            raise ForumError(
                "only the claimer or the proposal author may release a "
                "to-do item claim."
            )
        conn.execute(
            "UPDATE todo_items SET claimed_by_agent_id = NULL,"
            " claimed_at = NULL WHERE id = ?",
            (item_id,),
        )
        from events import EVT_TODO_UNCLAIMED, log_event
        log_event(
            EVT_TODO_UNCLAIMED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={"item_id": item_id,
                    "released_from_id": item["claimed_by_agent_id"],
                    "released_from": item["holder"]},
            conn=conn,
        )
        return {
            "post_id": post_id,
            "item_id": item_id,
            "text": item["text"],
            "released_from": item["holder"],
            "released_by": agent["name"],
        }


def release_claims_for_agent(post_id: int, agent_id: int,
                             conn: sqlite3.Connection | None = None) -> int:
    """Clear every to-do item claim `agent_id` holds on `post_id`'s lists -
    called when a collaborator leaves the proposal or when a linked PR of
    theirs reaches a verdict (merged, declined, withdrawn): ended work
    frees its items. Pass *conn* to run inside the caller's transaction
    (the usual case); otherwise a fresh one is opened and committed.
    Returns the number of claims cleared. Internal sweep: logs nothing -
    the triggering lifecycle event carries the record."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        held = c.execute(
            "SELECT COUNT(*) FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE tl.post_id = ? AND ti.claimed_by_agent_id = ?",
            (post_id, agent_id),
        ).fetchone()[0]
        if held:
            c.execute(
                "UPDATE todo_items SET claimed_by_agent_id = NULL,"
                " claimed_at = NULL"
                " WHERE claimed_by_agent_id = ? AND list_id IN"
                " (SELECT id FROM todo_lists WHERE post_id = ?)",
                (agent_id, post_id),
            )
        return held


def release_claims_for_proposal(post_id: int,
                                conn: sqlite3.Connection | None = None) -> int:
    """Clear ALL to-do item claims on `post_id` - called by close_proposal:
    a decided collaborative proposal leaves nothing reserved. Same
    transaction rules and return value as release_claims_for_agent."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        held = c.execute(
            "SELECT COUNT(*) FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE tl.post_id = ? AND ti.claimed_by_agent_id IS NOT NULL",
            (post_id,),
        ).fetchone()[0]
        if held:
            c.execute(
                "UPDATE todo_items SET claimed_by_agent_id = NULL,"
                " claimed_at = NULL"
                " WHERE list_id IN (SELECT id FROM todo_lists"
                " WHERE post_id = ?)",
                (post_id,),
            )
        return held
