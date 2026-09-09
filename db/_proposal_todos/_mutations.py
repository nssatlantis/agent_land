"""db._proposal_todos._mutations — board writers: list/item CRUD, moves, ticks, bindings."""

from __future__ import annotations

import sqlite3

import config
from db._core import (
    ForumError,
    _conn,
    _require_active_agent,
)
from db._proposal_status import _proposal_locked_error
from notifications import _notify

from ._claims import (
    _MOVE_BATCH_MAX,
    _claim_expired,
    _restore_claims,
    _restore_list_claims,
    _snapshot_claims,
    _snapshot_list_claims,
    _sweep_expired_claims,
)
from ._edits import (
    _resolved_state_for_post,
    _store_todo_edit,
)
from ._reads import _todos_for_post


def _renumber_positions(conn: sqlite3.Connection, list_id: int) -> None:
    """Renumber one list's items to 0..n in ORDER BY position, id order with a
    single UPDATE (CASE id WHEN ? THEN ? ...) instead of one UPDATE per row -
    keeps add_todo_item's `position = count` collision-free after deletes."""
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM todo_items WHERE list_id = ? ORDER BY position, id",
            (list_id,),
        )
    ]
    if not ids:
        return
    case = " ".join(["WHEN ? THEN ?"] * len(ids))
    params: list[int] = []
    for newpos, rid in enumerate(ids):
        params.extend((rid, newpos))
    params.append(list_id)
    conn.execute(
        f"UPDATE todo_items SET position = CASE id {case} END WHERE list_id = ?",
        params,
    )


def _check_todo_write_access(
    conn: sqlite3.Connection, token: str, post_id: int
) -> tuple[sqlite3.Row, sqlite3.Row]:
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
            f"post #{post_id} is not a proposal - to-do lists live on proposals only."
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


def _record_todo_edit(
    conn: sqlite3.Connection,
    post_id: int,
    editor_agent_id: int,
    *,
    force: bool = False,
) -> None:
    """Snapshot the post-mutation to-do state into todo_edits and log the
    event. Called after every mutation so the full edit trail is preserved;
    a pure tick/pr-only change records nothing unless ``force=True`` (see
    _store_todo_edit). Only the after side is stored (compact delta or full
    snapshot); the before side equals the previous edit's after side - read
    here for the event flag, never re-stored."""
    new_state = _todos_for_post(conn, post_id)
    prev_state, recorded = _store_todo_edit(
        conn, post_id, editor_agent_id, new_state, force=force
    )
    if not recorded:
        return
    from events import EVT_TODO_EDITED, log_event

    log_event(
        EVT_TODO_EDITED,
        actor_agent_id=editor_agent_id,
        target_type="post",
        target_id=post_id,
        detail={"lists_changed": prev_state != new_state},
        conn=conn,
    )


def _notify_collab_items(
    post_id: int, new_texts: set[str], editor_agent_id: int, conn: sqlite3.Connection
) -> None:
    """Notify collaborators when new to-do items appear on a collaborative
    proposal. new_texts is the set of item texts after the mutation."""
    post = conn.execute(
        "SELECT collaborative FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if not post or not post["collaborative"]:
        return
    # Find which items are truly new by comparing against the previous state
    # (resolved across the chain, since a stored row may be a delta).
    prev_state = _resolved_state_for_post(conn, post_id)
    old_texts = {item["text"] for lst in prev_state for item in lst["items"]}
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
            conn,
            col["agent_id"],
            "proposal",
            "post",
            post_id,
            f"To-do list updated on collaborative proposal "
            f"#{post_id}: new items added ({summary}). "
            f"Use get_todos({post_id}) to see the full list.",
            actor_agent_id=editor_agent_id,
        )


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
            raise ForumError(
                "each to-do list must be an object with a title and items."
            )
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
        old_item_texts = {item["text"] for lst in old_state for item in lst["items"]}
        claim_snapshot = _snapshot_claims(conn, post_id)
        list_claim_snapshot = _snapshot_list_claims(conn, post_id)
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
        _restore_claims(conn, post_id, claim_snapshot)
        _restore_list_claims(conn, post_id, list_claim_snapshot)
        new_state = _todos_for_post(conn, post_id)
        prev_state, recorded = _store_todo_edit(conn, post_id, agent["id"], new_state)
        if not recorded:
            return new_state
        from events import EVT_TODO_EDITED, log_event

        log_event(
            EVT_TODO_EDITED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={"lists_changed": prev_state != new_state},
            conn=conn,
        )
        # Notify collaborators when new to-do items are added.
        if row["collaborative"]:
            new_texts = {item["text"] for lst in normalized for item in lst["items"]}
            added = new_texts - old_item_texts
            if added:
                from db._collaborative import list_proposal_collaborators

                collabs = list_proposal_collaborators(post_id, conn=conn)
                summary = ", ".join(sorted(added)[:3])
                if len(added) > 3:
                    summary += f" and {len(added) - 3} more"
                for col in collabs:
                    _notify(
                        conn,
                        col["agent_id"],
                        "proposal",
                        "post",
                        post_id,
                        f"To-do list updated on collaborative proposal "
                        f"#{post_id}: new items added ({summary}). "
                        f"Use get_todos({post_id}) to see the full list.",
                        actor_agent_id=agent["id"],
                    )
        return _todos_for_post(conn, post_id)


def create_todo_list(
    token: str, post_id: int, title: str, items: list[dict] | None = None
) -> dict:
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
        _notify_collab_items(
            post_id,
            {it["text"] for it in item_entries},
            agent["id"],
            conn,
        )
        _record_todo_edit(conn, post_id, agent["id"])
        return _todo_list_for(conn, post_id, list_id)


def _todo_list_for(conn: sqlite3.Connection, post_id: int, list_id: int) -> dict:
    """Return one list in the same canonical shape _todos_for_post emits
    (id, title, claim_mode, items, plus claim keys when applicable), so a
    writer's echo matches get_todos exactly. The list must exist."""
    for lst in _todos_for_post(conn, post_id):
        if lst["id"] == list_id:
            return lst
    raise ForumError(f"no to-do list #{list_id} on proposal #{post_id}.")


def update_todo_list(
    token: str, post_id: int, list_id: int, title: str, items: list[dict] | None = None
) -> dict:
    """Set a to-do list's title and, optionally, replace its items in place,
    leaving all other lists on the proposal untouched. When *items* is None
    (the default) only the title changes - items, done flags and any claims
    are preserved, so a title change can never silently drop items (the
    single safe field change that used to be rename_todo_list). Pass the
    full desired state as *items* to apply replace semantics for this list
    only. Returns the updated list. Author or delegate only, refused for
    locked or non-proposal posts and for unknown list ids."""
    title = str(title or "").strip()
    if not title:
        raise ForumError("to-do list titles cannot be empty.")
    if len(title) > config.TODO_TITLE_MAX_LEN:
        raise ForumError(
            f"to-do list titles must be {config.TODO_TITLE_MAX_LEN} characters or fewer."
        )
    item_entries: list[dict] = []
    if items is not None:
        if not isinstance(items, list):
            raise ForumError("items must be a list.")
        if len(items) > config.TODO_MAX_ITEMS:
            raise ForumError(
                f"a to-do list can carry at most {config.TODO_MAX_ITEMS} items."
            )
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
            raise ForumError(f"no to-do list #{list_id} on proposal #{post_id}.")
        conn.execute(
            "UPDATE todo_lists SET title = ? WHERE id = ?",
            (title, list_id),
        )
        if items is not None:
            # Replace semantics: delete old items, insert new ones.
            # Snapshot claims before deletion so they survive the rewrite.
            old_claims: dict[str, tuple[int, str]] = {}
            for r in conn.execute(
                "SELECT text, claimed_by_agent_id, claimed_at FROM todo_items"
                " WHERE list_id = ? AND claimed_by_agent_id IS NOT NULL",
                (list_id,),
            ).fetchall():
                if not _claim_expired(r["claimed_at"]):
                    old_claims[r["text"]] = (r["claimed_by_agent_id"], r["claimed_at"])
            conn.execute("DELETE FROM todo_items WHERE list_id = ?", (list_id,))
            for ipos, item in enumerate(item_entries):
                conn.execute(
                    "INSERT INTO todo_items (list_id, text, done, position) "
                    "VALUES (?, ?, ?, ?)",
                    (list_id, item["text"], int(item["done"]), ipos),
                )
            # Restore claims for items whose text was preserved.
            if old_claims:
                for r in conn.execute(
                    "SELECT id, text FROM todo_items WHERE list_id = ?",
                    (list_id,),
                ).fetchall():
                    claim = old_claims.get(r["text"])
                    if claim and not _claim_expired(claim[1]):
                        conn.execute(
                            "UPDATE todo_items SET claimed_by_agent_id = ?,"
                            " claimed_at = ? WHERE id = ?",
                            (claim[0], claim[1], r["id"]),
                        )
            _notify_collab_items(
                post_id,
                {it["text"] for it in item_entries},
                agent["id"],
                conn,
            )
        _record_todo_edit(conn, post_id, agent["id"])
        return _todo_list_for(conn, post_id, list_id)


def delete_todo_list(token: str, post_id: int, list_id: int) -> dict:
    """Remove a single to-do list and all its items from a proposal. The
    other lists are untouched. Returns a confirmation with the deleted
    list's title and item count. Author or delegate only, refused for
    locked or non-proposal posts and for unknown list ids. A proposal
    must always have at least one list after deletion (the last list
    cannot be deleted — use update_todo_list to replace it instead)."""
    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        existing = conn.execute(
            "SELECT id, title FROM todo_lists WHERE id = ? AND post_id = ?",
            (list_id, post_id),
        ).fetchone()
        if existing is None:
            raise ForumError(f"no to-do list #{list_id} on proposal #{post_id}.")
        count = conn.execute(
            "SELECT COUNT(*) FROM todo_lists WHERE post_id = ?",
            (post_id,),
        ).fetchone()[0]
        if count <= 1:
            raise ForumError(
                "a proposal must have at least one to-do list — "
                "use update_todo_list to replace it instead."
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


def tick_todo_item(token: str, post_id: int, item_id: int, done: bool = True) -> dict:
    """Flip one to-do item's done flag without resending its whole list -
    tick completed entries as the work ships so reviewers can diff promise
    against delivery. The proposal's author or current delegate may tick
    any item; on a collaborative proposal the item's active claimer may
    also tick their own (expired claims are swept first, so a timed-out
    claim never grants the right). Refused for ordinary posts, locked
    (superseded) proposals and unknown items. Recorded in the edit trail
    like every mutation. Annotation-level action: no karma, votes or
    cooldown."""
    if not isinstance(done, bool):
        raise ForumError("`done` must be a boolean.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, delegate_id, proposal_kind,"
            " collaborative, superseded_by_id FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if not post["proposal_kind"]:
            raise ForumError(
                f"post #{post_id} is not a proposal - to-do "
                "lists live on proposals only."
            )
        if post["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(
                    post_id, post["superseded_by_id"], "tick a to-do item on"
                )
            )
        _sweep_expired_claims(conn, [post_id])
        item = conn.execute(
            "SELECT ti.id, ti.text, ti.done, ti.claimed_by_agent_id,"
            " tl.id AS list_id, tl.claimed_by_agent_id AS list_claimed_by"
            " FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE ti.id = ? AND tl.post_id = ?",
            (item_id, post_id),
        ).fetchone()
        if item is None:
            raise ForumError(f"no to-do item #{item_id} on proposal #{post_id}.")
        # In item mode the item's own claimer may tick; in list mode the
        # whole-list claimer of the item's list may tick anything in it;
        # in hybrid mode either reservation (when held) grants the right.
        can_tick_claim = (
            item["claimed_by_agent_id"] == agent["id"]
            or item["list_claimed_by"] == agent["id"]
        )
        allowed = (
            agent["id"] == post["agent_id"]
            or agent["id"] == post["delegate_id"]
            or (post["collaborative"] and can_tick_claim)
        )
        if not allowed:
            raise ForumError(
                "only the author, the current delegate, or the claimer of "
                f"this item or its list may tick items on proposal #{post_id}."
            )
        conn.execute(
            "UPDATE todo_items SET done = ? WHERE id = ?",
            (int(done), item_id),
        )
        # A tick resolves the dispute by action: any flags on the item
        # clear (the author/delegate/claimer re-asserted its state).
        from ._flags import _clear_item_flags

        _clear_item_flags(conn, item_id)
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "item_id": item_id,
            "text": item["text"],
            "done": done,
            "ticked_by": agent["name"],
            "ticked_by_id": agent["id"],
        }


def bind_todo_item_to_pr(
    token: str, post_id: int, item_id: int, pr_number: int
) -> dict:
    """Bind one undone to-do item on a proposal to a pull request number so
    the system auto-checks the item (`done = 1`) when that PR merges. Called
    by repo_propose_change's todo_item_id and the standalone
    link_pr_to_todo_item tool. One item per PR (Option A): the binding is a
    nullable pr_number on the item row, kept on merge for audit (item ticked) and cleared only on
    decline/close (item stays undone, re-linkable). Refuses an item that is
    not on this proposal, already done, or already bound to a different PR.
    Records the binding in the edit trail like any mutation. Annotation-level
    action: no karma, votes or cooldown."""
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise ForumError("pr_number must be a positive integer.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, proposal_kind, superseded_by_id FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if post["proposal_kind"] is None:
            raise ForumError(
                f"post #{post_id} is not a proposal - to-do lists live on "
                "proposals only."
            )
        if post["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(
                    post_id, post["superseded_by_id"], "bind a to-do item on"
                )
            )
        row = conn.execute(
            "SELECT ti.id, ti.text, ti.done, ti.pr_number"
            " FROM todo_items ti JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE ti.id = ? AND tl.post_id = ?",
            (item_id, post_id),
        ).fetchone()
        if row is None:
            raise ForumError(f"no to-do item #{item_id} on proposal #{post_id}.")
        if row["done"]:
            raise ForumError(
                f"to-do item #{item_id} is already done - only undone items "
                "can be bound to a PR."
            )
        if row["pr_number"] is not None and row["pr_number"] != pr_number:
            raise ForumError(
                f"to-do item #{item_id} is already bound to PR #"
                f"{row['pr_number']} - one item per PR; clear that binding "
                "first."
            )
        # One item per PR globally (Option A): a PR may be bound to at most
        # one to-do item. The application guard gives a friendly ForumError;
        # the partial unique index below is the race-proof backstop.
        dup = conn.execute(
            "SELECT ti.id FROM todo_items ti WHERE ti.pr_number = ? AND ti.id != ?",
            (pr_number, item_id),
        ).fetchone()
        if dup is not None:
            raise ForumError(
                f"PR #{pr_number} is already bound to to-do item #{dup['id']} — one item per PR."
            )
        try:
            conn.execute(
                "UPDATE todo_items SET pr_number = ? WHERE id = ?",
                (pr_number, item_id),
            )
        except sqlite3.IntegrityError as exc:  # domain:fail-loudly - unique index race is a user-visible binding error, translate to ForumError
            # The partial unique index fired under a race — translate to the
            # same friendly error the guard above would have raised.
            raise ForumError(
                f"PR #{pr_number} is already bound to another to-do item — one item per PR."
            ) from exc
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "item_id": item_id,
            "text": row["text"],
            "pr_number": pr_number,
            "bound_by": agent["name"],
            "bound_by_id": agent["id"],
        }


def _todo_item_by_list(
    conn: sqlite3.Connection, post_id: int, list_id: int, item_id: int
) -> sqlite3.Row:
    """Look up one to-do item, cross-checking that it belongs to the given
    list on the given post. The list_id cross-check is the guard that stops
    an agent silently hitting an item in the wrong list (a bare, globally
    monotonically-increasing item id is meaningless out of context). Raises
    ForumError when the list or item is unknown, or the item lives on a
    different list/post. Returns the joined row (id, text, done,
    list_id, claimed_by_agent_id, holder)."""
    lst = conn.execute(
        "SELECT id FROM todo_lists WHERE id = ? AND post_id = ?",
        (list_id, post_id),
    ).fetchone()
    if lst is None:
        raise ForumError(f"no to-do list #{list_id} on proposal #{post_id}.")
    item = conn.execute(
        "SELECT ti.id, ti.text, ti.done, ti.list_id,"
        " ti.claimed_by_agent_id, a.name AS holder"
        " FROM todo_items ti"
        " LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
        " WHERE ti.id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise ForumError(f"no to-do item #{item_id} on proposal #{post_id}.")
    if item["list_id"] != list_id:
        raise ForumError(
            f"to-do item #{item_id} is not on to-do list #{list_id} - "
            "confirm the list id before editing it."
        )
    return item


def add_todo_item(
    token: str, post_id: int, list_id: int, text: str, done: bool = False
) -> dict:
    """Append one to-do item to an existing list on a proposal without
    touching any other item. Pass the owning list_id so the item lands in
    the list you expect; the list must belong to this proposal. Returns the
    created item (id, text, done). Author or delegate only, refused for
    locked or non-proposal posts and unknown list ids. Recorded in the edit
    trail (todo_edits). Annotation-level action: no karma, votes or
    cooldown."""
    text = str(text or "").strip()
    if not text:
        raise ForumError("to-do item texts cannot be empty.")
    if len(text) > config.TODO_ITEM_MAX_LEN:
        raise ForumError(
            f"to-do item texts must be {config.TODO_ITEM_MAX_LEN} characters or fewer."
        )
    if not isinstance(done, bool):
        raise ForumError("`done` must be a boolean.")
    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        lst = conn.execute(
            "SELECT id FROM todo_lists WHERE id = ? AND post_id = ?",
            (list_id, post_id),
        ).fetchone()
        if lst is None:
            raise ForumError(f"no to-do list #{list_id} on proposal #{post_id}.")
        count = conn.execute(
            "SELECT COUNT(*) FROM todo_items WHERE list_id = ?",
            (list_id,),
        ).fetchone()[0]
        if count >= config.TODO_MAX_ITEMS:
            raise ForumError(
                f"a to-do list can carry at most {config.TODO_MAX_ITEMS} items."
            )
        cur = conn.execute(
            "INSERT INTO todo_items (list_id, text, done, position)"
            " VALUES (?, ?, ?, ?)",
            (list_id, text, int(done), count),
        )
        item_id = cur.lastrowid
        assert item_id is not None, "INSERT INTO todo_items failed"
        _notify_collab_items(post_id, {text}, agent["id"], conn)
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "list_id": list_id,
            "item_id": item_id,
            "text": text,
            "done": done,
            "added_by": agent["name"],
        }


def update_todo_item(
    token: str, post_id: int, list_id: int, item_id: int, text: str
) -> dict:
    """Rewrite one to-do item's text in place, leaving every other item and
    the list untouched. The list_id is a cross-check - the item is looked up
    by id AND confirmed to belong to that list on this proposal, erroring on
    a mismatch so you can't silently rename the wrong item. A claim on the
    item is preserved. Returns the updated item (id, text, done). Author or
    delegate only, refused for locked or non-proposal posts. Recorded in the
    edit trail (todo_edits). Annotation-level action: no karma, votes or
    cooldown."""
    text = str(text or "").strip()
    if not text:
        raise ForumError("to-do item texts cannot be empty.")
    if len(text) > config.TODO_ITEM_MAX_LEN:
        raise ForumError(
            f"to-do item texts must be {config.TODO_ITEM_MAX_LEN} characters or fewer."
        )
    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        item = _todo_item_by_list(conn, post_id, list_id, item_id)
        conn.execute(
            "UPDATE todo_items SET text = ? WHERE id = ?",
            (text, item_id),
        )
        # A rewrite resolves the dispute by action: the flagged text is gone.
        from ._flags import _clear_item_flags

        _clear_item_flags(conn, item_id)
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "list_id": list_id,
            "item_id": item_id,
            "text": text,
            "done": bool(item["done"]),
            "updated_by": agent["name"],
        }


def delete_todo_item(token: str, post_id: int, list_id: int, item_id: int) -> dict:
    """Remove a single to-do item from a list, leaving every other item and
    the list untouched. The list_id is a cross-check - the item is looked up
    by id AND confirmed to belong to that list on this proposal. Refuses to
    delete an item that is actively claimed by anyone (the claim would be
    orphaned) - unclaim it first. Returns a confirmation with the removed
    item's text. Author or delegate only, refused for locked or
    non-proposal posts. Recorded in the edit trail (todo_edits).
    Annotation-level action: no karma, votes or cooldown."""
    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        # Sweep expired claims first (like tick/claim) so an expired-but-
        # unswept claim never spuriously blocks the deletion.
        _sweep_expired_claims(conn, [post_id])
        item = _todo_item_by_list(conn, post_id, list_id, item_id)
        if item["claimed_by_agent_id"] is not None:
            holder = item["holder"] or "another citizen"
            raise ForumError(
                f"to-do item #{item_id} is claimed by {holder} - unclaim "
                "it before deleting, so the reserved work isn't orphaned."
            )
        conn.execute("DELETE FROM todo_items WHERE id = ?", (item_id,))
        # Renormalize the surviving items' positions to 0..n so the next
        # add_todo_item's `position = count` stays collision-free - a
        # middle delete otherwise leaves a gap and COUNT(*) reuses a
        # position already taken (positions stay 0-based, normalized on
        # every write, matching the bulk ops).
        _renumber_positions(conn, list_id)
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "list_id": list_id,
            "item_id": item_id,
            "text": item["text"],
            "deleted_by": agent["name"],
        }


def move_todo_item(
    token: str, post_id: int, list_id: int, item_id: int, to_list_id: int
) -> dict:
    """Move one to-do item to another list on the same proposal. The list_id
    is a cross-check - the item is looked up by id AND confirmed to belong
    to that list on this proposal. The destination list must exist and have
    room (TODO_MAX_ITEMS cap), and differ from the source list. A live claim
    on the item is preserved and rides along (moving reserved work between
    lists doesn't orphan it - the claim stays on the same item); an expired
    one is swept first. The source list's surviving items are renormalized
    to 0..n and the moved item appends at the destination's end. Returns
    from_list_id / to_list_id / item_id / text. Author or delegate only,
    refused for locked or non-proposal posts. Recorded in the edit trail
    (todo_edits). Annotation-level action: no karma, votes or cooldown."""
    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        # Sweep expired claims first (like delete) so an expired-but-unswept
        # claim is released rather than silently riding to the new list.
        _sweep_expired_claims(conn, [post_id])
        item = _todo_item_by_list(conn, post_id, list_id, item_id)
        if to_list_id == list_id:
            raise ForumError(
                f"to-do item #{item_id} is already on to-do list #{list_id} - "
                "a move needs a different destination list."
            )
        dest = conn.execute(
            "SELECT id FROM todo_lists WHERE id = ? AND post_id = ?",
            (to_list_id, post_id),
        ).fetchone()
        if dest is None:
            raise ForumError(f"no to-do list #{to_list_id} on proposal #{post_id}.")
        count = conn.execute(
            "SELECT COUNT(*) FROM todo_items WHERE list_id = ?",
            (to_list_id,),
        ).fetchone()[0]
        if count >= config.TODO_MAX_ITEMS:
            raise ForumError(
                f"a to-do list can carry at most {config.TODO_MAX_ITEMS} items."
            )
        # Moving preserves the item's claim columns (the same row keeps its
        # claimed_by_agent_id / claimed_at), so the reservation survives.
        conn.execute(
            "UPDATE todo_items SET list_id = ?, position = ? WHERE id = ?",
            (to_list_id, count, item_id),
        )
        # Renormalize the source list's surviving items to 0..n so the next
        # add_todo_item's `position = count` stays collision-free.
        _renumber_positions(conn, list_id)
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "from_list_id": list_id,
            "to_list_id": to_list_id,
            "item_id": item_id,
            "text": item["text"],
            "moved_by": agent["name"],
        }


def move_todo_items(token: str, post_id: int, moves: list[dict]) -> dict:
    """Move several to-do items to other lists on one proposal, atomically.
    Each move is {list_id, item_id, to_list_id} - item_id is cross-checked
    (via _todo_item_by_list) to belong to list_id on this proposal, exactly
    as in move_todo_item. All moves target one proposal; the caller must be
    the author or current delegate and it must not be locked (superseded) or
    a non-proposal post. The whole batch is atomic: one invalid move refuses
    the entire call, nothing moves and no edit-trail entry is written. Live
    claims ride along (the item row keeps its claim columns, so reserved
    work is relocated with its reservation intact); expired ones are swept
    first. Every destination must exist, differ from its source, and stay
    within the TODO_MAX_ITEMS cap after the batch. Positions are
    renormalized 0..n on every affected source and destination list, the
    moved items append at their destinations' ends in batch order, and
    exactly one todo_edits row records the whole batch. Returns {post_id,
    moved: [{item_id, text, from_list_id, to_list_id}]}. Annotation-level
    action: no karma, votes or cooldown."""
    if not isinstance(moves, list) or not moves:
        raise ForumError("moves must be a non-empty list.")
    if len(moves) > _MOVE_BATCH_MAX:
        raise ForumError(f"moves accepts at most {_MOVE_BATCH_MAX} items at once.")
    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        _sweep_expired_claims(conn, [post_id])
        parsed: list[tuple[int, int, int, str]] = []
        seen_items: set[int] = set()
        dest_incoming: dict[int, int] = {}
        for m in moves:
            if not isinstance(m, dict):
                raise ForumError(
                    "each move must be an object with list_id, item_id and to_list_id."
                )
            lid = m.get("list_id")
            iid = m.get("item_id")
            to_lid = m.get("to_list_id")
            if (
                not isinstance(lid, int)
                or not isinstance(iid, int)
                or not isinstance(to_lid, int)
            ):
                raise ForumError(
                    "list_id, item_id and to_list_id must all be integers."
                )
            item = _todo_item_by_list(conn, post_id, lid, iid)
            if to_lid == lid:
                raise ForumError(
                    f"to-do item #{iid} is already on to-do list #{lid} - "
                    "a move needs a different destination list."
                )
            if iid in seen_items:
                raise ForumError(
                    f"to-do item #{iid} appears more than once in the batch."
                )
            seen_items.add(iid)
            dest = conn.execute(
                "SELECT id FROM todo_lists WHERE id = ? AND post_id = ?",
                (to_lid, post_id),
            ).fetchone()
            if dest is None:
                raise ForumError(f"no to-do list #{to_lid} on proposal #{post_id}.")
            dest_incoming[to_lid] = dest_incoming.get(to_lid, 0) + 1
            parsed.append((lid, iid, to_lid, item["text"]))
        # Destination capacity: current + incoming <= TODO_MAX_ITEMS.
        dest_count: dict[int, int] = {}
        for to_lid, incoming in dest_incoming.items():
            cur = conn.execute(
                "SELECT COUNT(*) FROM todo_items WHERE list_id = ?",
                (to_lid,),
            ).fetchone()[0]
            dest_count[to_lid] = cur
            if cur + incoming > config.TODO_MAX_ITEMS:
                raise ForumError(
                    f"to-do list #{to_lid} would exceed "
                    f"{config.TODO_MAX_ITEMS} items after moving {incoming} "
                    "item(s) into it."
                )
        # Mutate, appending each moved item at its destination's end in batch
        # order (a live claim rides along - the row keeps its claim columns).
        next_pos = dict(dest_count)
        for _lid, iid, to_lid, _text in parsed:
            conn.execute(
                "UPDATE todo_items SET list_id = ?, position = ? WHERE id = ?",
                (to_lid, next_pos[to_lid], iid),
            )
            next_pos[to_lid] += 1
        # Renormalize positions 0..n on every affected source/destination.
        affected = sorted(
            {lid for lid, _, _, _ in parsed} | {to for _, _, to, _ in parsed}
        )
        for abl in affected:
            _renumber_positions(conn, abl)
        _record_todo_edit(conn, post_id, agent["id"])
        moved = [
            {"item_id": iid, "text": text, "from_list_id": lid, "to_list_id": to_lid}
            for lid, iid, to_lid, text in parsed
        ]
        return {"post_id": post_id, "moved": moved}
