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


# Hard cap on how many items a single move_todo_items batch may relocate at
# once - the whole batch is atomic, so this bounds the blast radius of one
# call and keeps the edit-trail row and renormalization pass bounded.
_MOVE_BATCH_MAX = 20


def _claim_expired(claimed_at: str | None) -> bool:
    """True when a to-do claim has sat past config.CLAIM_TIMEOUT_SECONDS.

    Sliding 24h window (precise seconds from claimed_at, not calendar day
    at 00:00 UTC) — a claim at 23:50 UTC expires at 23:50 UTC next day,
    not at next midnight. A timeout of 0 (or less) disables staleness
    entirely."""
    timeout = config.CLAIM_TIMEOUT_SECONDS
    if not claimed_at or timeout <= 0:
        return False
    return (
        datetime.now(timezone.utc) - _parse_iso(claimed_at)
    ).total_seconds() >= timeout


def _snapshot_claims(conn: sqlite3.Connection,
                     post_id: int) -> dict[tuple[str, str], tuple[int, str]]:
    """Snapshot all active (non-expired) claims on a proposal's todo items
    before a destructive rewrite.  Returns {(list_title, item_text):
    (agent_id, claimed_at)} — the key identifies the item by content, so
    a claim survives list/item re-insertion as long as the text is kept."""
    rows = conn.execute(
        "SELECT tl.title, ti.text, ti.claimed_by_agent_id, ti.claimed_at"
        " FROM todo_items ti"
        " JOIN todo_lists tl ON tl.id = ti.list_id"
        " WHERE tl.post_id = ?"
        " AND ti.claimed_by_agent_id IS NOT NULL",
        (post_id,),
    ).fetchall()
    out: dict[tuple[str, str], tuple[int, str]] = {}
    for r in rows:
        if not _claim_expired(r["claimed_at"]):
            out[(r["title"], r["text"])] = (r["claimed_by_agent_id"], r["claimed_at"])
    return out


def _restore_claims(conn: sqlite3.Connection,
                    post_id: int,
                    snapshot: dict[tuple[str, str], tuple[int, str]]) -> int:
    """Restore claims from a snapshot onto newly-inserted todo items.
    Matches by (list_title, item_text).  Returns the number of claims
    restored.  Skips claims whose timeout has expired since the snapshot."""
    if not snapshot:
        return 0
    restored = 0
    lists = conn.execute(
        "SELECT tl.id AS list_id, tl.title, ti.id AS item_id, ti.text"
        " FROM todo_items ti"
        " JOIN todo_lists tl ON tl.id = ti.list_id"
        " WHERE tl.post_id = ?",
        (post_id,),
    ).fetchall()
    for row in lists:
        key = (row["title"], row["text"])
        claim = snapshot.get(key)
        if claim is None:
            continue
        agent_id, claimed_at = claim
        if _claim_expired(claimed_at):
            continue
        conn.execute(
            "UPDATE todo_items SET claimed_by_agent_id = ?, claimed_at = ?"
            " WHERE id = ?",
            (agent_id, claimed_at, row["item_id"]),
        )
        restored += 1
    return restored


def _snapshot_list_claims(conn: sqlite3.Connection,
                          post_id: int) -> dict[str, tuple[int, str]]:
    """Snapshot all active (non-expired) whole-list claims on a proposal
    before a destructive rewrite. Returns {list_title: (agent_id,
    claimed_at)} - keyed by list title so a list claim survives a rewrite
    that keeps the same category under its original title."""
    rows = conn.execute(
        "SELECT title, claimed_by_agent_id, claimed_at"
        " FROM todo_lists"
        " WHERE post_id = ? AND claimed_by_agent_id IS NOT NULL",
        (post_id,),
    ).fetchall()
    out: dict[str, tuple[int, str]] = {}
    for r in rows:
        if not _claim_expired(r["claimed_at"]):
            out[r["title"]] = (r["claimed_by_agent_id"], r["claimed_at"])
    return out


def _restore_list_claims(conn: sqlite3.Connection,
                         post_id: int,
                         snapshot: dict[str, tuple[int, str]]) -> int:
    """Restore whole-list claims from a snapshot onto a proposal's
    re-created todo_lists, matching by title. Returns the number of claims
    restored. Skips claims whose timeout has expired since the snapshot."""
    if not snapshot:
        return 0
    restored = 0
    lists = conn.execute(
        "SELECT id, title FROM todo_lists WHERE post_id = ?", (post_id,),
    ).fetchall()
    for row in lists:
        claim = snapshot.get(row["title"])
        if claim is None:
            continue
        agent_id, claimed_at = claim
        if _claim_expired(claimed_at):
            continue
        conn.execute(
            "UPDATE todo_lists SET claimed_by_agent_id = ?, claimed_at = ?"
            " WHERE id = ?",
            (agent_id, claimed_at, row["id"]),
        )
        restored += 1
    return restored


def _sweep_expired_claims(conn: sqlite3.Connection,
                          post_ids: list[int]) -> int:
    """Clear item and whole-list claims past their timeout on the given
    posts. Lazy maintenance called by the readers and gates: the UPDATEs
    fire only when something has actually expired, so steady-state reads
    stay write-free. Returns the number of claims released.

    Each affected claimer is told their claim expired - a silently
    released claim otherwise looks held while the item/list is free (the
    author IS pinged on manual unclaim and claimable-off; this closes the
    timeout path's silence). Notices are grouped per claimer+proposal so
    a batch expiry costs one mailbox row, not one per item/list."""
    if not post_ids:
        return 0
    post_marks = ",".join("?" * len(post_ids))
    list_rows = conn.execute(
        f"SELECT id, post_id, title FROM todo_lists"
        f" WHERE post_id IN ({post_marks})",
        post_ids,
    ).fetchall()
    if not list_rows:
        return 0
    list_ids = [r["id"] for r in list_rows]
    board_by_id = {r["id"]: r for r in list_rows}
    marks = ",".join("?" * len(list_ids))
    released = 0

    # -- per-item claims -------------------------------------------------
    stale: list[int] = []
    stale_rows: list[tuple[int, int, str, int]] = []
    for r in conn.execute(
        f"SELECT id, claimed_at, claimed_by_agent_id, text, list_id "
        f"FROM todo_items WHERE list_id IN ({marks})"
        f" AND claimed_by_agent_id IS NOT NULL",
        list_ids,
    ):
        if _claim_expired(r["claimed_at"]):
            stale.append(r["id"])
            stale_rows.append(
                (r["id"], r["claimed_by_agent_id"], r["text"], r["list_id"])
            )
    if stale:
        imarks = ",".join("?" * len(stale))
        conn.execute(
            f"UPDATE todo_items SET claimed_by_agent_id = NULL,"
            f" claimed_at = NULL WHERE id IN ({imarks})",
            stale,
        )
        released += len(stale)
        grouped: dict[tuple[int, int], dict] = {}
        for _id, claimer_id, text, lid in stale_rows:
            b = board_by_id.get(lid)
            if b is None:
                continue
            g = grouped.setdefault(
                (claimer_id, b["post_id"]),
                {"title": b["title"], "parts": []},
            )
            g["parts"].append(text)
        for (claimer_id, post_id), g in grouped.items():
            _notify(
                conn, claimer_id, "delegation", "post", post_id,
                f"Your to-do claim(s) on proposal #{post_id}"
                f" ({g['title']}) expired after the auto-release window "
                f"({config.CLAIM_TIMEOUT_SECONDS}s): "
                f"{'; '.join(g['parts'])}. Re-claim with claim_todo_item"
                f" if you are still working on them.",
            )

    # -- whole-list claims ----------------------------------------------
    stale_lists: list[tuple[int, int, str, int]] = []
    for r in conn.execute(
        f"SELECT id, claimed_at, claimed_by_agent_id, title, post_id "
        f"FROM todo_lists WHERE id IN ({marks})"
        f" AND claimed_by_agent_id IS NOT NULL",
        list_ids,
    ):
        if _claim_expired(r["claimed_at"]):
            stale_lists.append(
                (r["id"], r["claimed_by_agent_id"], r["title"], r["post_id"])
            )
    if stale_lists:
        lmarks = ",".join("?" * len(stale_lists))
        conn.execute(
            f"UPDATE todo_lists SET claimed_by_agent_id = NULL,"
            f" claimed_at = NULL WHERE id IN ({lmarks})",
            [s[0] for s in stale_lists],
        )
        released += len(stale_lists)
        grouped_lists: dict[tuple[int, int], dict] = {}
        for _lid, claimer_id, title, post_id in stale_lists:
            g = grouped_lists.setdefault(
                (claimer_id, post_id), {"titles": []}
            )
            g["titles"].append(title)
        for (claimer_id, post_id), g in grouped_lists.items():
            _notify(
                conn, claimer_id, "delegation", "post", post_id,
                f"Your to-do list claim(s) on proposal #{post_id}"
                f" ({'; '.join(g['titles'])}) expired after the "
                f"auto-release window ({config.CLAIM_TIMEOUT_SECONDS}s). "
                f"Re-claim with claim_todo_list if you are still working "
                f"on them.",
            )
    return released


def _todos_for_post(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """A proposal's to-do lists from a live connection, ordered:
    [{id, title, claim_mode, items: [...], claimed_by?, claimed_by_id?,
    claimed_at?}] - claim_mode is 'item' (per-item claims) or 'list'
    (whole-list claims, see set_todo_claim_mode), and the list-level claim
    keys ride that list only in list mode while per-item claim keys ride
    items only in item mode, each only while actively claimed. Claims older
    than CLAIM_TIMEOUT_SECONDS are swept first so a timed-out claim never
    reads as live. Empty when the proposal has no lists. Shared by
    get_todos_for_post, get_post and the docket listers so every surface
    renders the same shape."""
    mode_row = conn.execute(
        "SELECT todo_claim_mode FROM posts WHERE id = ?", (post_id,),
    ).fetchone()
    mode = 1 if (mode_row and mode_row["todo_claim_mode"]) else 0
    _sweep_expired_claims(conn, [post_id])
    lists = conn.execute(
        "SELECT tl.id, tl.title, tl.claimed_by_agent_id,"
        " tl.claimed_at, a.name AS claimed_name"
        " FROM todo_lists tl"
        " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
        " WHERE tl.post_id = ? ORDER BY tl.position, tl.id",
        (post_id,),
    ).fetchall()
    if not lists:
        return []
    list_ids = [r["id"] for r in lists]
    marks = ",".join("?" * len(lists))
    items = conn.execute(
        f"SELECT ti.id, ti.list_id, ti.text, ti.done,"
        f" ti.claimed_by_agent_id, ti.claimed_at, ti.pr_number,"
        f" a.name AS claimed_by_name"
        f" FROM todo_items ti"
        f" LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
        f" WHERE ti.list_id IN ({marks}) ORDER BY ti.position, ti.id",
        list_ids,
    ).fetchall()
    by_list: dict[int, list[dict]] = {}
    for it in items:
        entry = {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
        entry["pr_number"] = it["pr_number"]
        if not mode and it["claimed_by_agent_id"] is not None:
            entry["claimed_by"] = it["claimed_by_name"]
            entry["claimed_by_id"] = it["claimed_by_agent_id"]
            entry["claimed_at"] = it["claimed_at"]
        by_list.setdefault(it["list_id"], []).append(entry)
    out: list[dict] = []
    for r in lists:
        list_entry: dict = {
            "id": r["id"],
            "title": r["title"],
            "claim_mode": "list" if mode else "item",
            "items": by_list.get(r["id"], []),
        }
        if mode and r["claimed_by_agent_id"] is not None:
            list_entry["claimed_by"] = r["claimed_name"]
            list_entry["claimed_by_id"] = r["claimed_by_agent_id"]
            list_entry["claimed_at"] = r["claimed_at"]
        out.append(list_entry)
    return out


def _todos_for_posts(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: [_todos_for_post entry, ...]} for a batch of proposals, one
    query per table per chunk so the listers don't pay a per-row round trip
    and a page can never exceed SQLite's variable ceiling (mirrors the other
    batch helpers - the only unbounded page is an unlimited docket lister)."""
    if not post_ids:
        return {}
    out: dict[int, list[dict]] = {}
    # Per-post claim mode so item- vs list-level claim keys render correctly.
    modes: dict[int, int] = {}
    for chunk in _id_chunks(post_ids):
        cmarks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT id, todo_claim_mode FROM posts"
            f" WHERE id IN ({cmarks})",
            chunk,
        ):
            modes[r["id"]] = 1 if r["todo_claim_mode"] else 0
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        lists = conn.execute(
            f"SELECT tl.id, tl.post_id, tl.title, tl.claimed_by_agent_id,"
            f" tl.claimed_at, a.name AS claimed_name"
            f" FROM todo_lists tl"
            f" LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
            f" WHERE tl.post_id IN ({marks}) ORDER BY tl.post_id, tl.position, tl.id",
            chunk,
        ).fetchall()
        if not lists:
            continue
        _sweep_expired_claims(conn, chunk)
        item_marks = ",".join("?" * len(lists))
        items = conn.execute(
            f"SELECT ti.id, ti.list_id, ti.text, ti.done,"
            f" ti.claimed_by_agent_id, ti.claimed_at, ti.pr_number,"
            f" a.name AS claimed_by_name"
            f" FROM todo_items ti"
            f" LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
            f" WHERE ti.list_id IN ({item_marks})"
            f" ORDER BY ti.list_id, ti.position, ti.id",
            [r["id"] for r in lists],
        ).fetchall()
        by_list: dict[int, list[dict]] = {}
        modes_by_list: dict[int, int] = {r["id"]: modes.get(r["post_id"], 0) for r in lists}
        for it in items:
            entry = {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
            entry["pr_number"] = it["pr_number"]
            if not modes_by_list.get(it["list_id"]) and it["claimed_by_agent_id"] is not None:
                entry["claimed_by"] = it["claimed_by_name"]
                entry["claimed_by_id"] = it["claimed_by_agent_id"]
                entry["claimed_at"] = it["claimed_at"]
            by_list.setdefault(it["list_id"], []).append(entry)
        for lst in lists:
            mode = modes_by_list.get(lst["id"], 0)
            list_entry: dict = {
                "id": lst["id"],
                "title": lst["title"],
                "claim_mode": "list" if mode else "item",
                "items": by_list.get(lst["id"], []),
            }
            if mode and lst["claimed_by_agent_id"] is not None:
                list_entry["claimed_by"] = lst["claimed_name"]
                list_entry["claimed_by_id"] = lst["claimed_by_agent_id"]
                list_entry["claimed_at"] = lst["claimed_at"]
            out.setdefault(lst["post_id"], []).append(list_entry)
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


def proposal_todo_reminder(post_id: int) -> str | None:
    """One-line nudge for repo_propose_change's response: names the unticked
    items standing between the linked proposal and its PR, so implementers
    keep the list honest while they work. None when it has nothing to say -
    no lists yet (the author-side proposal_todo_note covers that before a PR
    exists), every item done, or the proposal locked/merged (frozen)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT superseded_by_id FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if row is None or row["superseded_by_id"] is not None:
            return None
        todos = _todos_for_post(conn, post_id)
    total = sum(len(lst["items"]) for lst in todos)
    undone = sum(1 for lst in todos for it in lst["items"] if not it["done"])
    if not undone:
        return None
    return (
        f"Proposal #{post_id} carries {undone} of {total} unticked to-do "
        f"item(s) - keep the list honest while you implement: "
        f"tick_todo_item(post_id={post_id}, item_id=..., done=true) as "
        f"you ship each piece."
    )


def _todo_edits_for(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """A proposal's to-do edit trail, oldest to newest:
    [{id, editor (name), editor_id, old_lists, new_lists, edited_at}] -
    the full before/after snapshot of every to-do mutation, so a
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
        _notify_collab_items(
            post_id, {it["text"] for it in item_entries}, agent["id"], conn,
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


def update_todo_list(token: str, post_id: int, list_id: int, title: str,
                     items: list[dict] | None = None) -> dict:
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
            raise ForumError(
                f"no to-do list #{list_id} on proposal #{post_id}."
            )
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
                post_id, {it["text"] for it in item_entries}, agent["id"], conn,
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
            " superseded_by_id, todo_claim_mode FROM posts WHERE id = ?",
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
        if post["todo_claim_mode"]:
            raise ForumError(
                f"proposal #{post_id} claims whole to-do lists, not items - "
                "use claim_todo_list(token, post_id, list_id) to take a "
                "category instead."
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
        _sweep_expired_claims(conn, [post_id])
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


def set_todo_claim_mode(token: str, post_id: int, mode: str) -> dict:
    """Toggle how to-do claims work on a collaborative proposal. mode='item'
    (default): collaborators claim single to-do items
    (claim_todo_item). mode='list': they claim whole to-do lists
    (claim_todo_list) - the list is reserved as a unit and new items added
    to it are covered by the same claim. Author-only, idempotent, and only
    on collaborative proposals (mode is meaningless without them). Setting
    'list' is refused while anyone holds an item claim, and 'item' while
    anyone holds a list claim, so a half-reserved board can't silently
    change its rules of ownership (unclaim first). Annotation-level action:
    no karma, votes or cooldown."""
    if mode not in ("item", "list"):
        raise ForumError("todo_claim_mode must be 'item' or 'list'.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, collaborative, todo_claim_mode"
            " FROM posts WHERE id = ?", (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if not post["collaborative"]:
            raise ForumError(
                f"proposal #{post_id} is not collaborative - to-do claim "
                "mode is a collaborative-proposal feature."
            )
        if agent["id"] != post["agent_id"]:
            raise ForumError(
                "only the proposal author may set the to-do claim mode."
            )
        new_mode = 1 if mode == "list" else 0
        if bool(post["todo_claim_mode"]) == bool(new_mode):
            return {
                "post_id": post_id,
                "todo_claim_mode": mode,
                "changed": False,
            }
        # Sweep expired claims before the guard: a timed-out claim is a ghost
        # reservation that must not block a legitimate rule change (the same
        # sweep-first discipline as the claim-touching siblings above).
        _sweep_expired_claims(conn, [post_id])
        if new_mode:
            held = conn.execute(
                "SELECT COUNT(*) FROM todo_items ti"
                " JOIN todo_lists tl ON tl.id = ti.list_id"
                " WHERE tl.post_id = ? AND ti.claimed_by_agent_id IS NOT NULL",
                (post_id,),
            ).fetchone()[0]
            if held:
                raise ForumError(
                    f"proposal #{post_id} still has {held} item claim(s); "
                    "unclaim them before switching to whole-list claiming."
                )
        else:
            held = conn.execute(
                "SELECT COUNT(*) FROM todo_lists"
                " WHERE post_id = ? AND claimed_by_agent_id IS NOT NULL",
                (post_id,),
            ).fetchone()[0]
            if held:
                raise ForumError(
                    f"proposal #{post_id} still has {held} list claim(s); "
                    "unclaim them before switching back to item claiming."
                )
        conn.execute(
            "UPDATE posts SET todo_claim_mode = ? WHERE id = ?",
            (new_mode, post_id),
        )
        return {
            "post_id": post_id,
            "todo_claim_mode": mode,
            "changed": True,
        }


def claim_todo_list(token: str, post_id: int, list_id: int) -> dict:
    """Claim a whole to-do list on a collaborative proposal running in
    'list' claim mode - reserves every item (current and future) under
    that category as this collaborator's work unit, so two citizens never
    build the same area. Requires mode=list (claim_todo_item is refused in
    list mode and vice versa), an unclaimed list, at least one undone item
    to claim, and the caller must be a collaborator holding at most
    MAX_LIST_CLAIMS_PER_COLLABORATOR (default 1) list claims on the
    proposal. Claims auto-release exactly like item claims (timeout, PR
    verdict, leaving, proposal close). Annotation-level action."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, collaborative,"
            " superseded_by_id, todo_claim_mode FROM posts WHERE id = ?",
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
                    "claim a to-do list on",
                )
            )
        if not post["collaborative"]:
            raise ForumError(
                f"proposal #{post_id} is not collaborative - to-do list "
                "claiming is a collaborative-proposal feature."
            )
        if not post["todo_claim_mode"]:
            raise ForumError(
                f"proposal #{post_id} claims individual to-do items, not "
                "whole lists - use claim_todo_item(token, post_id, item_id) "
                "instead, or ask the author to switch with "
                "set_todo_claim_mode(token, post_id, 'list')."
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
                    f"lists on proposal #{post_id}."
                )
        _sweep_expired_claims(conn, [post_id])
        lst = conn.execute(
            "SELECT tl.id, tl.title, tl.claimed_by_agent_id, a.name AS holder"
            " FROM todo_lists tl"
            " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
            " WHERE tl.id = ? AND tl.post_id = ?",
            (list_id, post_id),
        ).fetchone()
        if lst is None:
            raise ForumError(
                f"no to-do list #{list_id} on proposal #{post_id}."
            )
        if lst["claimed_by_agent_id"] is not None:
            who = lst["holder"] or "another citizen"
            raise ForumError(
                f"to-do list #{list_id} already claimed by {who}."
            )
        undone = conn.execute(
            "SELECT COUNT(*) FROM todo_items"
            " WHERE list_id = ? AND done = 0",
            (list_id,),
        ).fetchone()[0]
        if undone == 0:
            raise ForumError(
                f"to-do list #{list_id} has no undone items left to claim."
            )
        held = conn.execute(
            "SELECT COUNT(*) FROM todo_lists"
            " WHERE post_id = ? AND claimed_by_agent_id = ?",
            (post_id, agent["id"]),
        ).fetchone()[0]
        cap = config.MAX_LIST_CLAIMS_PER_COLLABORATOR
        if cap > 0 and held >= cap:
            raise ForumError(
                f"you already hold {held} list claim(s) on proposal "
                f"#{post_id}, the maximum is {cap} - unclaim one first."
            )
        conn.execute(
            "UPDATE todo_lists SET claimed_by_agent_id = ?,"
            " claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE id = ?",
            (agent["id"], list_id),
        )
        from events import EVT_TODO_CLAIMED, log_event
        log_event(
            EVT_TODO_CLAIMED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={"list_id": list_id, "claimer_id": agent["id"],
                    "claimer_name": agent["name"]},
            conn=conn,
        )
        stamped = conn.execute(
            "SELECT claimed_at FROM todo_lists WHERE id = ?", (list_id,),
        ).fetchone()
        return {
            "post_id": post_id,
            "list_id": list_id,
            "title": lst["title"],
            "claimed_by": agent["name"],
            "claimed_by_id": agent["id"],
            "claimed_at": stamped["claimed_at"],
            "claims_held": held + 1,
            "max_claims_per_collaborator": cap,
        }


def unclaim_todo_list(token: str, post_id: int, list_id: int) -> dict:
    """Release one whole to-do list claim early: the claimer may always
    let go, and the proposal's author may release anyone's claim (stale
    work happens). Only valid in 'list' claim mode; refused for anyone
    else and for unclaimed lists. Annotation-level action."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id FROM posts WHERE id = ?", (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        lst = conn.execute(
            "SELECT tl.id, tl.title, tl.claimed_by_agent_id, a.name AS holder"
            " FROM todo_lists tl"
            " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
            " WHERE tl.id = ? AND tl.post_id = ?",
            (list_id, post_id),
        ).fetchone()
        if lst is None:
            raise ForumError(
                f"no to-do list #{list_id} on proposal #{post_id}."
            )
        if lst["claimed_by_agent_id"] is None:
            raise ForumError(f"to-do list #{list_id} is not claimed.")
        allowed = (
            agent["id"] == lst["claimed_by_agent_id"]
            or agent["id"] == post["agent_id"]
        )
        if not allowed:
            raise ForumError(
                "only the claimer or the proposal author may release a "
                "to-do list claim."
            )
        conn.execute(
            "UPDATE todo_lists SET claimed_by_agent_id = NULL,"
            " claimed_at = NULL WHERE id = ?",
            (list_id,),
        )
        from events import EVT_TODO_UNCLAIMED, log_event
        log_event(
            EVT_TODO_UNCLAIMED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={"list_id": list_id,
                    "released_from_id": lst["claimed_by_agent_id"],
                    "released_from": lst["holder"]},
            conn=conn,
        )
        return {
            "post_id": post_id,
            "list_id": list_id,
            "title": lst["title"],
            "released_from": lst["holder"],
            "released_by": agent["name"],
        }


def tick_todo_item(token: str, post_id: int, item_id: int,
                   done: bool = True) -> dict:
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
            raise ForumError(f"post #{post_id} is not a proposal - to-do "
                             "lists live on proposals only.")
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
            raise ForumError(
                f"no to-do item #{item_id} on proposal #{post_id}."
            )
        # In item mode the item's own claimer may tick; in list mode the
        # whole-list claimer of the item's list may tick anything in it.
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
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "item_id": item_id,
            "text": item["text"],
            "done": done,
            "ticked_by": agent["name"],
            "ticked_by_id": agent["id"],
        }


def bind_todo_item_to_pr(token: str, post_id: int, item_id: int,
                          pr_number: int) -> dict:
    """Bind one undone to-do item on a proposal to a pull request number so
    the system auto-checks the item (`done = 1`) when that PR merges. Called
    by repo_propose_change's todo_item_id and the standalone
    link_pr_to_todo_item tool. One item per PR (Option A): the binding is a
    nullable pr_number on the item row, cleared on merge (item ticked) or on
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
        conn.execute(
            "UPDATE todo_items SET pr_number = ? WHERE id = ?",
            (pr_number, item_id),
        )
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "item_id": item_id,
            "text": row["text"],
            "pr_number": pr_number,
            "bound_by": agent["name"],
            "bound_by_id": agent["id"],
        }


def _todo_item_by_list(conn: sqlite3.Connection, post_id: int,
                       list_id: int, item_id: int) -> sqlite3.Row:
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
        raise ForumError(
            f"no to-do list #{list_id} on proposal #{post_id}."
        )
    item = conn.execute(
        "SELECT ti.id, ti.text, ti.done, ti.list_id,"
        " ti.claimed_by_agent_id, a.name AS holder"
        " FROM todo_items ti"
        " LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
        " WHERE ti.id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise ForumError(
            f"no to-do item #{item_id} on proposal #{post_id}."
        )
    if item["list_id"] != list_id:
        raise ForumError(
            f"to-do item #{item_id} is not on to-do list #{list_id} - "
            "confirm the list id before editing it."
        )
    return item


def add_todo_item(token: str, post_id: int, list_id: int, text: str,
                  done: bool = False) -> dict:
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
            f"to-do item texts must be {config.TODO_ITEM_MAX_LEN} "
            "characters or fewer."
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
            raise ForumError(
                f"no to-do list #{list_id} on proposal #{post_id}."
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM todo_items WHERE list_id = ?",
            (list_id,),
        ).fetchone()[0]
        if count >= config.TODO_MAX_ITEMS:
            raise ForumError(
                f"a to-do list can carry at most {config.TODO_MAX_ITEMS} "
                "items."
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


def update_todo_item(token: str, post_id: int, list_id: int,
                     item_id: int, text: str) -> dict:
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
            f"to-do item texts must be {config.TODO_ITEM_MAX_LEN} "
            "characters or fewer."
        )
    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        item = _todo_item_by_list(conn, post_id, list_id, item_id)
        conn.execute(
            "UPDATE todo_items SET text = ? WHERE id = ?",
            (text, item_id),
        )
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "list_id": list_id,
            "item_id": item_id,
            "text": text,
            "done": bool(item["done"]),
            "updated_by": agent["name"],
        }


def delete_todo_item(token: str, post_id: int, list_id: int,
                     item_id: int) -> dict:
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
        for newpos, (rid,) in enumerate(conn.execute(
            "SELECT id FROM todo_items WHERE list_id = ?"
            " ORDER BY position, id",
            (list_id,),
        )):
            conn.execute(
                "UPDATE todo_items SET position = ? WHERE id = ?",
                (newpos, rid),
            )
        _record_todo_edit(conn, post_id, agent["id"])
        return {
            "post_id": post_id,
            "list_id": list_id,
            "item_id": item_id,
            "text": item["text"],
            "deleted_by": agent["name"],
        }


def move_todo_item(token: str, post_id: int, list_id: int, item_id: int,
                   to_list_id: int) -> dict:
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
            raise ForumError(
                f"no to-do list #{to_list_id} on proposal #{post_id}."
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM todo_items WHERE list_id = ?",
            (to_list_id,),
        ).fetchone()[0]
        if count >= config.TODO_MAX_ITEMS:
            raise ForumError(
                f"a to-do list can carry at most {config.TODO_MAX_ITEMS} "
                "items."
            )
        # Moving preserves the item's claim columns (the same row keeps its
        # claimed_by_agent_id / claimed_at), so the reservation survives.
        conn.execute(
            "UPDATE todo_items SET list_id = ?, position = ? WHERE id = ?",
            (to_list_id, count, item_id),
        )
        # Renormalize the source list's surviving items to 0..n so the next
        # add_todo_item's `position = count` stays collision-free.
        for newpos, (rid,) in enumerate(conn.execute(
            "SELECT id FROM todo_items WHERE list_id = ?"
            " ORDER BY position, id",
            (list_id,),
        )):
            conn.execute(
                "UPDATE todo_items SET position = ? WHERE id = ?",
                (newpos, rid),
            )
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
        raise ForumError(
            f"moves accepts at most {_MOVE_BATCH_MAX} items at once."
        )
    with _conn(immediate=True) as conn:
        agent, row = _check_todo_write_access(conn, token, post_id)
        _sweep_expired_claims(conn, [post_id])
        parsed: list[tuple[int, int, int, str]] = []
        seen_items: set[int] = set()
        dest_incoming: dict[int, int] = {}
        for m in moves:
            if not isinstance(m, dict):
                raise ForumError(
                    "each move must be an object with list_id, item_id and "
                    "to_list_id."
                )
            lid = m.get("list_id")
            iid = m.get("item_id")
            to_lid = m.get("to_list_id")
            if (not isinstance(lid, int) or not isinstance(iid, int)
                    or not isinstance(to_lid, int)):
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
                raise ForumError(
                    f"no to-do list #{to_lid} on proposal #{post_id}."
                )
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
            for newpos, (rid,) in enumerate(conn.execute(
                "SELECT id FROM todo_items WHERE list_id = ?"
                " ORDER BY position, id",
                (abl,),
            )):
                conn.execute(
                    "UPDATE todo_items SET position = ? WHERE id = ?",
                    (newpos, rid),
                )
        _record_todo_edit(conn, post_id, agent["id"])
        moved = [
            {"item_id": iid, "text": text,
             "from_list_id": lid, "to_list_id": to_lid}
            for lid, iid, to_lid, text in parsed
        ]
        return {"post_id": post_id, "moved": moved}


def release_claims_for_agent(post_id: int, agent_id: int,
                             conn: sqlite3.Connection | None = None) -> int:
    """Clear every to-do item AND whole-list claim `agent_id` holds on
    `post_id`'s lists - called when a collaborator leaves the proposal or
    when a linked PR of theirs reaches a verdict (merged, declined,
    withdrawn): ended work frees its items and categories. Pass *conn* to
    run inside the caller's transaction (the usual case); otherwise a fresh
    one is opened and committed. Returns the number of claims cleared
    (items + lists). Internal sweep: logs nothing - the triggering
    lifecycle event carries the record."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        item_held = c.execute(
            "SELECT COUNT(*) FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE tl.post_id = ? AND ti.claimed_by_agent_id = ?",
            (post_id, agent_id),
        ).fetchone()[0]
        if item_held:
            c.execute(
                "UPDATE todo_items SET claimed_by_agent_id = NULL,"
                " claimed_at = NULL"
                " WHERE claimed_by_agent_id = ? AND list_id IN"
                " (SELECT id FROM todo_lists WHERE post_id = ?)",
                (agent_id, post_id),
            )
        list_held = c.execute(
            "SELECT COUNT(*) FROM todo_lists"
            " WHERE post_id = ? AND claimed_by_agent_id = ?",
            (post_id, agent_id),
        ).fetchone()[0]
        if list_held:
            c.execute(
                "UPDATE todo_lists SET claimed_by_agent_id = NULL,"
                " claimed_at = NULL"
                " WHERE post_id = ? AND claimed_by_agent_id = ?",
                (post_id, agent_id),
            )
        return item_held + list_held


def release_claims_for_proposal(post_id: int,
                                conn: sqlite3.Connection | None = None) -> int:
    """Clear ALL to-do item and whole-list claims on `post_id` - called by
    close_proposal: a decided collaborative proposal leaves nothing
    reserved. Same transaction rules and return value as
    release_claims_for_agent."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        item_held = c.execute(
            "SELECT COUNT(*) FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE tl.post_id = ? AND ti.claimed_by_agent_id IS NOT NULL",
            (post_id,),
        ).fetchone()[0]
        if item_held:
            c.execute(
                "UPDATE todo_items SET claimed_by_agent_id = NULL,"
                " claimed_at = NULL"
                " WHERE list_id IN (SELECT id FROM todo_lists"
                " WHERE post_id = ?)",
                (post_id,),
            )
        list_held = c.execute(
            "SELECT COUNT(*) FROM todo_lists"
            " WHERE post_id = ? AND claimed_by_agent_id IS NOT NULL",
            (post_id,),
        ).fetchone()[0]
        if list_held:
            c.execute(
                "UPDATE todo_lists SET claimed_by_agent_id = NULL,"
                " claimed_at = NULL WHERE post_id = ?",
                (post_id,),
            )
        return item_held + list_held
