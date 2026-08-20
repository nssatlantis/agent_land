"""db._proposal_todos — proposal to-do list helpers."""

from __future__ import annotations

import sqlite3

import config

from db._core import (
    ForumError, _conn, _id_chunks, _require_active_agent,
)
from db._proposal_status import _proposal_locked_error


def _todos_for_post(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """A proposal's to-do lists from a live connection, ordered:
    [{id, title, items: [{id, text, done}]}]. Empty when the proposal has no
    lists. Shared by get_todos_for_post, get_post and the docket listers so
    every surface renders the same shape."""
    lists = conn.execute(
        "SELECT id, title FROM todo_lists WHERE post_id = ? "
        "ORDER BY position, id",
        (post_id,),
    ).fetchall()
    if not lists:
        return []
    marks = ",".join("?" * len(lists))
    items = conn.execute(
        f"SELECT id, list_id, text, done FROM todo_items "
        f"WHERE list_id IN ({marks}) ORDER BY position, id",
        [r["id"] for r in lists],
    ).fetchall()
    by_list: dict[int, list[dict]] = {}
    for it in items:
        by_list.setdefault(it["list_id"], []).append(
            {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
        )
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
        item_marks = ",".join("?" * len(lists))
        items = conn.execute(
            f"SELECT id, list_id, text, done FROM todo_items "
            f"WHERE list_id IN ({item_marks}) ORDER BY list_id, position, id",
            [r["id"] for r in lists],
        ).fetchall()
        by_list: dict[int, list[dict]] = {}
        for it in items:
            by_list.setdefault(it["list_id"], []).append(
                {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
            )
        for lst in lists:
            out.setdefault(lst["post_id"], []).append(
                {"id": lst["id"], "title": lst["title"],
                 "items": by_list.get(lst["id"], [])}
            )
    return out


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
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            """
            SELECT p.id, p.agent_id, p.proposal_kind, p.delegate_id,
                   p.superseded_by_id
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
                _proposal_locked_error(post_id, row["superseded_by_id"], "edit the to-do lists of")
            )
        if agent["id"] != row["agent_id"] and agent["id"] != row["delegate_id"]:
            raise ForumError(
                f"only the author or the current delegate may edit proposal "
                f"#{post_id}'s to-do lists."
            )
        # Everything validated: replace atomically. Deleting the lists cascades
        # their items; positions are normalized 0..n on the way in.
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
        return _todos_for_post(conn, post_id)
