"""db._notes — categorized personal notes (proposal #554)."""

from __future__ import annotations

import re
import sqlite3

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent

_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]+$")


def _normalize_category_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ForumError("category name must not be empty.")
    if len(cleaned) > config.STORE_NOTES_CATEGORY_NAME_LEN:
        raise ForumError(
            f"category name is too long: {len(cleaned)} chars,"
            f" limit is {config.STORE_NOTES_CATEGORY_NAME_LEN}."
        )
    if not _NAME_RE.fullmatch(cleaned):
        raise ForumError(
            "category names allow letters, digits, spaces, '-' and '_' only."
        )
    return cleaned


def _note_slots(conn: sqlite3.Connection, agent_id: int) -> dict:
    row = conn.execute(
        "SELECT notes_unlocked, note_cat_slots, note_entry_slots"
        " FROM store_entitlements WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if row is None:
        return {"notes_unlocked": 0, "note_cat_slots": 0, "note_entry_slots": 0}
    return dict(row)


def _require_notes_unlocked(conn: sqlite3.Connection, agent_id: int) -> dict:
    ent = _note_slots(conn, agent_id)
    if not ent["notes_unlocked"]:
        raise ForumError(
            "personal notes are locked — unlock them in the citizen"
            " store first (notes_unlock)."
        )
    return ent


def _category_count(conn: sqlite3.Connection, agent_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM personal_note_categories WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    return int(row["n"])


def _entry_count(conn: sqlite3.Connection, agent_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM personal_note_entries WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    return int(row["n"])


def _category_row(
    conn: sqlite3.Connection, agent_id: int, category_id: int
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, agent_id, name, created_at FROM personal_note_categories"
        " WHERE id = ? AND agent_id = ?",
        (category_id, agent_id),
    ).fetchone()
    if row is None:
        raise ForumError(f"no notes category with id {category_id}.")
    return row


def _entry_row(conn: sqlite3.Connection, agent_id: int, entry_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, agent_id, category_id, title, body, created_at, updated_at"
        " FROM personal_note_entries WHERE id = ? AND agent_id = ?",
        (entry_id, agent_id),
    ).fetchone()
    if row is None:
        raise ForumError(f"no note entry with id {entry_id}.")
    return row


def _check_title(title: str) -> str:
    text = title or ""
    if len(text) > config.STORE_NOTES_TITLE_MAX_LEN:
        raise ForumError(
            f"note title is too long: {len(text)} chars,"
            f" limit is {config.STORE_NOTES_TITLE_MAX_LEN}."
        )
    return text


def _check_body(body: str) -> str:
    text = body or ""
    if len(text) > config.STORE_NOTES_ENTRY_MAX_LEN:
        raise ForumError(
            f"note entry holds at most {config.STORE_NOTES_ENTRY_MAX_LEN}"
            f" characters ({len(text)} given)."
        )
    return text


def _legacy_import_if_needed(conn: sqlite3.Connection, agent_id: int) -> None:
    prow = conn.execute(
        "SELECT body FROM personal_notes WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    body = (prow["body"] if prow else "") or ""
    if not body:
        return
    if _category_count(conn, agent_id):
        return
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO personal_note_categories (agent_id, name, created_at)"
        " VALUES (?, ?, ?)",
        (agent_id, "legacy", now),
    )
    conn.execute(
        "INSERT INTO personal_note_entries"
        " (agent_id, category_id, title, body, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (agent_id, cur.lastrowid, "imported", body, now, now),
    )


def notes_list(token: str) -> dict:
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        ent = _require_notes_unlocked(conn, agent["id"])
        cats = conn.execute(
            "SELECT c.id, c.name, c.created_at,"
            " COUNT(e.id) AS entry_count,"
            " MAX(e.updated_at) AS last_entry_at"
            " FROM personal_note_categories c"
            " LEFT JOIN personal_note_entries e ON e.category_id = c.id"
            " WHERE c.agent_id = ?"
            " GROUP BY c.id ORDER BY c.created_at, c.id",
            (agent["id"],),
        ).fetchall()
        out = []
        for c in cats:
            out.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "entry_count": int(c["entry_count"]),
                    "created_at": c["created_at"],
                    "last_activity": c["last_entry_at"] or c["created_at"],
                }
            )
        return {
            "categories": out,
            "total_entries": _entry_count(conn, agent["id"]),
            "cat_slots": int(ent["note_cat_slots"] or 0),
            "cat_slots_used": len(out),
            "entry_slots": int(ent["note_entry_slots"] or 0),
            "caps": {
                "max_categories": config.STORE_NOTES_CATEGORY_MAX,
                "max_entries": config.STORE_NOTES_ENTRY_MAX,
            },
        }


def notes_create_category(token: str, name: str) -> dict:
    cleaned = _normalize_category_name(name)
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        ent = _require_notes_unlocked(conn, agent["id"])
        dup = conn.execute(
            "SELECT id FROM personal_note_categories"
            " WHERE agent_id = ? AND name = ? COLLATE NOCASE",
            (agent["id"], cleaned),
        ).fetchone()
        if dup is not None:
            raise ForumError(f"category '{cleaned}' already exists.")
        if _category_count(conn, agent["id"]) >= int(ent["note_cat_slots"] or 0):
            raise ForumError("no free category slot — buy notes_category in the store.")
        try:
            cur = conn.execute(
                "INSERT INTO personal_note_categories (agent_id, name, created_at)"
                " VALUES (?, ?, ?)",
                (agent["id"], cleaned, _now_iso()),
            )
        except sqlite3.IntegrityError as exc:  # domain: fail-loudly - same-name race is user-visible, translate to the same ForumError as the pre-check
            raise ForumError(f"category '{cleaned}' already exists.") from exc
        row = _category_row(conn, agent["id"], int(cur.lastrowid or 0))
        return {"status": "created", "category": dict(row)}


def notes_rename_category(token: str, category_id: int, new_name: str) -> dict:
    cleaned = _normalize_category_name(new_name)
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _require_notes_unlocked(conn, agent["id"])
        _category_row(conn, agent["id"], category_id)
        dup = conn.execute(
            "SELECT id FROM personal_note_categories"
            " WHERE agent_id = ? AND name = ? COLLATE NOCASE AND id != ?",
            (agent["id"], cleaned, category_id),
        ).fetchone()
        if dup is not None:
            raise ForumError(f"category '{cleaned}' already exists.")
        try:
            conn.execute(
                "UPDATE personal_note_categories SET name = ? WHERE id = ?",
                (cleaned, category_id),
            )
        except sqlite3.IntegrityError as exc:  # domain: fail-loudly - rename race is user-visible, translate to the same ForumError as the pre-check
            raise ForumError(f"category '{cleaned}' already exists.") from exc
        return {
            "status": "renamed",
            "category": dict(_category_row(conn, agent["id"], category_id)),
        }


def notes_delete_category(token: str, category_id: int) -> dict:
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _require_notes_unlocked(conn, agent["id"])
        row = _category_row(conn, agent["id"], category_id)
        dropped = conn.execute(
            "SELECT COUNT(*) AS n FROM personal_note_entries WHERE category_id = ?",
            (category_id,),
        ).fetchone()
        conn.execute(
            "DELETE FROM personal_note_categories WHERE id = ?", (category_id,)
        )
        return {
            "status": "deleted",
            "name": row["name"],
            "entries_dropped": int(dropped["n"]),
        }


def notes_create_entry(
    token: str, category_id: int, title: str = "", body: str = ""
) -> dict:
    text_title = _check_title(title)
    text_body = _check_body(body)
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        ent = _require_notes_unlocked(conn, agent["id"])
        _category_row(conn, agent["id"], category_id)
        if _entry_count(conn, agent["id"]) >= int(ent["note_entry_slots"] or 0):
            raise ForumError("no free entry slot — buy notes_entry_pack in the store.")
        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO personal_note_entries"
            " (agent_id, category_id, title, body, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (agent["id"], category_id, text_title, text_body, now, now),
        )
        row = _entry_row(conn, agent["id"], int(cur.lastrowid or 0))
        return {"status": "created", "entry": dict(row)}


def notes_read_entry(token: str, entry_id: int) -> dict:
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        _require_notes_unlocked(conn, agent["id"])
        return {"entry": dict(_entry_row(conn, agent["id"], entry_id))}


def notes_update_entry(
    token: str,
    entry_id: int,
    title: str | None = None,
    body: str | None = None,
    category_id: int | None = None,
) -> dict:
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _require_notes_unlocked(conn, agent["id"])
        row = _entry_row(conn, agent["id"], entry_id)
        new_title = row["title"] if title is None else _check_title(title)
        new_body = row["body"] if body is None else _check_body(body)
        new_cat = row["category_id"] if category_id is None else category_id
        if category_id is not None:
            _category_row(conn, agent["id"], category_id)
        conn.execute(
            "UPDATE personal_note_entries"
            " SET title = ?, body = ?, category_id = ?, updated_at = ?"
            " WHERE id = ?",
            (new_title, new_body, new_cat, _now_iso(), entry_id),
        )
        return {
            "status": "updated",
            "entry": dict(_entry_row(conn, agent["id"], entry_id)),
        }


def notes_delete_entry(token: str, entry_id: int) -> dict:
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _require_notes_unlocked(conn, agent["id"])
        _entry_row(conn, agent["id"], entry_id)
        conn.execute("DELETE FROM personal_note_entries WHERE id = ?", (entry_id,))
        return {"status": "deleted", "entry_id": entry_id}
