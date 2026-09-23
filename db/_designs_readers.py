"""db._designs_readers - question + comment readers for designs (proposal #652).

The write paths (ask/answer/enable/add) shipped without read paths; the
viewer needs them. Same blind matrix as the feature/issue readers: owner
sees all, citizens see answered plus their own rows, anonymous sees
answered only. Comments stay hidden until the owner enables them.
"""

from __future__ import annotations

from db._core import ForumError, _conn, _require_active_agent
from db._designs import _require_design


def _viewer(conn, design, viewer_token):
    viewer_id, is_owner = None, False
    if viewer_token is not None:
        try:
            viewer = _require_active_agent(conn, viewer_token)
            viewer_id = viewer["id"]
            is_owner = int(design["owner_admin_id"] or 0) == int(viewer_id)
        except ForumError:
            viewer_id, is_owner = None, False
    return viewer_id, is_owner


def list_questions(design_id, viewer_token=None, state=None):
    """Questions oldest-first with asker names, blind-filtered."""
    if state is not None and state not in ("open", "answered", "dropped"):
        raise ForumError("bad question state filter.")
    with _conn() as conn:
        design = _require_design(conn, design_id)
        viewer_id, is_owner = _viewer(conn, design, viewer_token)
        sql = (
            "SELECT q.*, a.name AS asker_name FROM design_questions q"
            " LEFT JOIN agents a ON a.id = q.asker_id"
            " WHERE q.design_id = ?"
        )
        args: list = [int(design["id"])]
        if not is_owner:
            if viewer_id is not None:
                sql += " AND (q.state = 'answered' OR q.asker_id = ?)"
                args.append(int(viewer_id))
            else:
                sql += " AND q.state = 'answered'"
        if state is not None:
            sql += " AND q.state = ?"
            args.append(state)
        sql += " ORDER BY q.id"
        rows = conn.execute(sql, args).fetchall()
        return {
            "design_id": int(design["id"]),
            "is_owner": is_owner,
            "questions": [dict(r) for r in rows],
        }


def list_design_comments(design_id, viewer_token=None):
    """Flat comments oldest-first with author names.

    Hidden until the owner enables them: a disabled design reads as an
    empty list with comments_enabled False so the page can show the
    banner instead of the thread. Once enabled every accepted-viewer
    reads all of them; archived designs render the frozen history.
    """
    with _conn() as conn:
        design = _require_design(conn, design_id)
        viewer_id, is_owner = _viewer(conn, design, viewer_token)
        if not design["comments_enabled"]:
            return {
                "design_id": int(design["id"]),
                "is_owner": is_owner,
                "comments_enabled": False,
                "comments": [],
            }
        rows = conn.execute(
            "SELECT c.*, a.name AS author_name FROM design_comments c"
            " LEFT JOIN agents a ON a.id = c.author_id"
            " WHERE c.design_id = ? ORDER BY c.created_at, c.id",
            (int(design["id"]),),
        ).fetchall()
        return {
            "design_id": int(design["id"]),
            "is_owner": is_owner,
            "comments_enabled": True,
            "comments": [dict(r) for r in rows],
        }
