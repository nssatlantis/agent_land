"""db._proposal_todos._edits — todo_edits compact engine: codec, chain replay, store and readers."""

from __future__ import annotations

import copy
import json
import sqlite3

import config
from db._core import _id_chunks

# Compact todo_edits format: each row stores only the AFTER side, as either a
# full snapshot (bare JSON list, separators (",", ":")) or a delta (a dict
# {"v":2,"type":"delta","ops":[...]}). The before side of edit N is the after
# side of edit N-1 - the readers replay the chain instead of storing it again,
# and old_lists on compact rows carries _OLD_DERIVED (NULL sentinel, column is
# nullable for compact rows; legacy rows keep their own snapshot).
_OLD_DERIVED = None
_DELTA_TYPE = "delta"


def _compact_json(state: list[dict]) -> str:
    """Encode a full snapshot. pr_number:null is omitted (Option 4 trim); the
    readers re-add it so the public trail shape never changes."""
    return json.dumps(_trim_snapshot(state), separators=(",", ":"))


def _compact_delta(ops: list[dict]) -> str:
    return json.dumps({"v": 2, "type": _DELTA_TYPE, "ops": ops}, separators=(",", ":"))


def _decode_new_lists(raw: str) -> tuple[str, list[dict]]:
    """Split a stored new_lists value into its kind and payload: a bare list is
    a full snapshot ('snapshot', state); a {"type":"delta"} dict is a delta
    ('delta', ops). Anything else is treated as a full snapshot state."""
    val = json.loads(raw)
    if isinstance(val, dict) and val.get("type") == _DELTA_TYPE:
        return "delta", val["ops"]
    return "snapshot", val


def _trim_snapshot(state: list[dict]) -> list[dict]:
    """Strip absent pr_number (None) from items so stored snapshots don't pay
    for nulls; the readers normalize it back on read."""
    out: list[dict] = []
    for lst in state:
        items = []
        for it in lst["items"]:
            i = dict(it)
            if i.get("pr_number") is None:
                i.pop("pr_number", None)
            items.append(i)
        l = dict(lst)
        l["items"] = items
        out.append(l)
    return out


def _normalize_state(state: list[dict]) -> list[dict]:
    """Re-add pr_number (None) to any item missing it, so the public trail
    shape is byte-identical whether the row was stored as a snapshot (which
    omits pr_number:null) or a delta."""
    for lst in state:
        for it in lst["items"]:
            if "pr_number" not in it:
                it["pr_number"] = None
    return state


def _apply_ops(state: list[dict], ops: list[dict]) -> list[dict]:
    """Apply delta ops to a deep copy of state, returning the new state. Each
    op mutates by item/list id; list/items retain their stored ordering."""
    state = copy.deepcopy(state)

    def find_list(lst_id: int) -> dict | None:
        for l in state:
            if l["id"] == lst_id:
                return l
        return None

    def find_item(item_id: int) -> dict | None:
        for l in state:
            for it in l["items"]:
                if it["id"] == item_id:
                    return it
        return None

    def find_item_list(item_id: int) -> dict | None:
        for l in state:
            for it in l["items"]:
                if it["id"] == item_id:
                    return l
        return None

    for op in ops:
        kind = op["t"]
        if kind == "tick":
            it = find_item(op["id"])
            if it is not None:
                it["done"] = op["done"]
        elif kind == "ren":
            it = find_item(op["id"])
            if it is not None:
                it["text"] = op["text"]
        elif kind == "pr":
            it = find_item(op["id"])
            if it is not None:
                if op["v"] is None:
                    it.pop("pr_number", None)
                else:
                    it["pr_number"] = op["v"]
        elif kind == "add":
            lst = find_list(op["list"])
            if lst is not None:
                item: dict = {"id": op["id"], "text": op["text"], "done": op["done"]}
                if op.get("pr") is not None:
                    item["pr_number"] = op["pr"]
                pos = op.get("pos")
                if pos is not None and 0 <= pos < len(lst["items"]):
                    lst["items"].insert(pos, item)
                else:
                    lst["items"].append(item)
        elif kind == "del":
            lst = find_item_list(op["id"])
            if lst is not None:
                lst["items"] = [it for it in lst["items"] if it["id"] != op["id"]]
        elif kind == "mv":
            src = find_item_list(op["id"])
            if src is None:
                continue
            item = next(it for it in src["items"] if it["id"] == op["id"])
            src["items"] = [it for it in src["items"] if it["id"] != op["id"]]
            dst = find_list(op["list"])
            if dst is None:
                dst = src
            pos = op.get("pos")
            if pos is not None and 0 <= pos < len(dst["items"]):
                dst["items"].insert(pos, item)
            else:
                dst["items"].append(item)
        elif kind == "ladd":
            state.append(
                {
                    "id": op["id"],
                    "title": op["title"],
                    "claim_mode": op.get("claim_mode", "item"),
                    "items": [dict(i) for i in op.get("items", [])],
                }
            )
        elif kind == "ldel":
            state[:] = [l for l in state if l["id"] != op["id"]]
        elif kind == "lren":
            lst = find_list(op["id"])
            if lst is not None:
                lst["title"] = op["title"]
    return state


def _diff_states(old: list[dict], new: list[dict]) -> list[dict]:
    """Diff two full states into delta ops. Handles the common, lossless cases:
    item done/tick, text rename, pr_number change, item add/delete, list add/
    delete/rename. Anything else (claims, reorders, cross-list moves, list
    reorder) is simply not emitted here - the caller's round-trip verify sees
    the mismatch and stores an exact snapshot instead, so the diff never needs
    to be complete. Ops are lossless for the cases emitted."""

    def item_map(lst: dict) -> dict[int, dict]:
        return {i["id"]: i for i in lst["items"]}

    ops: list[dict] = []
    old_lists: dict[int, dict] = {l["id"]: l for l in old}
    new_lists: dict[int, dict] = {l["id"]: l for l in new}

    for l in new:
        lid = l["id"]
        if lid not in old_lists:
            ops.append(
                {
                    "t": "ladd",
                    "id": lid,
                    "title": l["title"],
                    "claim_mode": l.get("claim_mode", "item"),
                    "items": l["items"],
                }
            )
            continue
        o = old_lists[lid]
        if l["title"] != o["title"]:
            ops.append({"t": "lren", "id": lid, "title": l["title"]})
        om = item_map(o)
        nm = item_map(l)
        out_pos = 0
        for i in l["items"]:
            iid = i["id"]
            if iid not in om:
                ops.append(
                    {
                        "t": "add",
                        "id": iid,
                        "list": lid,
                        "text": i["text"],
                        "done": i["done"],
                        "pr": i.get("pr_number"),
                        "pos": out_pos,
                    }
                )
            else:
                oi = om[iid]
                if i["done"] != oi.get("done"):
                    ops.append({"t": "tick", "id": iid, "done": i["done"]})
                if i["text"] != oi.get("text"):
                    ops.append({"t": "ren", "id": iid, "text": i["text"]})
                if i.get("pr_number") != oi.get("pr_number"):
                    ops.append({"t": "pr", "id": iid, "v": i.get("pr_number")})
            out_pos += 1
        for iid in om:
            if iid not in nm:
                ops.append({"t": "del", "id": iid})

    for l in old:
        lid = l["id"]
        if lid not in new_lists:
            ops.append({"t": "ldel", "id": lid})
    return ops


def _resolved_state_for_post(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """Replay a proposal's todo_edits chain to its current full state (the
    before-side of the next edit). Used by the writer to diff against and by
    _notify_collab_items for the previous item texts."""
    rows = conn.execute(
        "SELECT old_lists, new_lists FROM todo_edits WHERE post_id = ?"
        " ORDER BY edited_at, id",
        (post_id,),
    ).fetchall()
    current: list[dict] = []
    for r in rows:
        kind, payload = _decode_new_lists(r["new_lists"])
        if kind == "delta":
            current = _apply_ops(current, payload)
        else:
            current = payload
    return _normalize_state(current)


def _has_ids(state: list[dict]) -> bool:
    """True when every list and item in a resolved state carries an id - the
    identity anchored by deltas. Legacy pre-#684 shapes (no ids) can't be
    diffed losslessly, so the writer falls back to a full snapshot for them."""
    for l in state:
        if "id" not in l:
            return False
        for it in l.get("items", []):
            if "id" not in it:
                return False
    return True


_CHURN_ONLY_OPS = frozenset({"tick", "pr"})


def _store_todo_edit(
    conn: sqlite3.Connection,
    post_id: int,
    editor_agent_id: int,
    new_state: list[dict],
    *,
    force: bool = False,
) -> tuple[list[dict], bool]:
    """Insert one edit-trail row for a post-mutation state, unless the change
    is pure state-flip churn. Diffs the previous resolved state against the new
    one; stores a compact delta when the diff round-trips losslessly and is
    smaller than a full snapshot, else an exact full snapshot. A diff made only
    of ``tick``/``pr`` ops - a manual done-flip, a PR-binding change, or a no-op
    re-tick (``ops == []``) - carries no structural information and records no
    row (the live ``todo_items`` row already holds ``pr_number`` and ``done``),
    unless ``force=True`` so important events (e.g. a PR-merge completion)
    always land. Returns ``(prev_state, recorded)`` - the caller's before-side
    plus whether a row was written."""
    prev_state = _resolved_state_for_post(conn, post_id)
    had_ids = _has_ids(prev_state)
    ops = _diff_states(prev_state, new_state) if had_ids else []
    # Pure tick/pr churn records nothing, but only when the ops actually
    # reproduce the new state. A change the diff cannot represent (e.g. a
    # same-list reorder, which emits no ops) fails the round-trip and must
    # still fall back to a snapshot. pr ops are normalized on replay (they pop
    # pr_number, which the live serializer always sets to None), so compare
    # the normalized shapes.
    churn = (
        had_ids
        and not force
        and all(op["t"] in _CHURN_ONLY_OPS for op in ops)
        and _normalize_state(_apply_ops([dict(l) for l in prev_state], ops))
        == _normalize_state(copy.deepcopy(new_state))
    )
    if churn:
        return _normalize_state([dict(l) for l in prev_state]), False
    max_ops = getattr(config, "FORUM_TODO_DELTA_MAX_SNAPSHOT_OPS", 16)
    use_delta = len(ops) > 0
    if max_ops:
        use_delta = use_delta and len(ops) <= max_ops
    delta_json = _compact_delta(ops) if use_delta else ""
    if use_delta and _apply_ops([dict(l) for l in prev_state], ops) != new_state:
        # Round-trip mismatch - the diff missed something; store the exact state.
        use_delta = False
    snap_json = _compact_json(new_state)
    if use_delta and len(delta_json) >= len(snap_json):
        use_delta = False
    encoded = delta_json if use_delta else snap_json
    conn.execute(
        "INSERT INTO todo_edits (post_id, editor_agent_id, old_lists, new_lists)"
        " VALUES (?, ?, ?, ?)",
        (post_id, editor_agent_id, _OLD_DERIVED, encoded),
    )
    return _normalize_state([dict(l) for l in prev_state]), True


def _derive_edits(rows: list) -> list[dict]:
    """Build the edit-trail dict list from raw todo_edits rows (rows must be
    ordered oldest-to-newest for one proposal).

    Compact rows store only the after side (old_lists is the _OLD_DERIVED
    sentinel), as either a full snapshot or a delta applied to the previous
    state; their before side is the derived previous state - the exact state
    the previous edit stored, so no information is lost. Historical rows keep
    their own before snapshot and pass through unchanged. Either way the
    reader returns the same full before/after trail."""
    out: list[dict] = []
    current: list[dict] = []
    for r in rows:
        if r["old_lists"]:
            old = json.loads(r["old_lists"])
        else:
            old = _normalize_state([dict(lst) for lst in current])
        kind, payload = _decode_new_lists(r["new_lists"])
        if kind == "delta":
            new = _apply_ops(current, payload)
        else:
            new = payload
        current = new
        out.append(
            {
                "id": r["id"],
                "editor": r["editor"],
                "editor_id": r["editor_id"],
                "old_lists": _normalize_state(copy.deepcopy(old)),
                "new_lists": _normalize_state(copy.deepcopy(new)),
                "edited_at": r["edited_at"],
            }
        )
    return out


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
    return _derive_edits(rows)


def _todo_edits_batch(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: [_todo_edits_for entry, ...]} for a batch of proposals."""
    if not post_ids:
        return {}
    groups: dict[int, list] = {}
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
            groups.setdefault(r["post_id"], []).append(r)
    return {pid: _derive_edits(groups[pid]) for pid in post_ids if pid in groups}
