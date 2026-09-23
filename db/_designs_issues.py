"""db._designs_issues — linked issues + resolve + move for designs (proposal #652).

Issues are separate rows, optionally linked to one accepted feature
(``feature_id``). Same blind matrix, karma floor, frozen guards,
similarity and typo handling as features. Resolve is owner-only;
no auto-resolve in v1.
"""

from __future__ import annotations

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._designs import (
    _check_contrib,
    _log_decided,
    _notify_author,
    _notify_owner,
    _require_design,
    _require_open,
    _require_owner,
    _similarity_warn,
)

_TEXT_MAX = 2000


def _issue_row(conn, iid, design_id):
    try:
        iid_int, did_int = int(iid), int(design_id)
    except (TypeError, ValueError) as exc:
        raise ForumError(f"no issue #{iid} on design #{design_id}.") from exc
    row = conn.execute(
        "SELECT * FROM design_issues WHERE id = ? AND design_id = ?",
        (iid_int, did_int),
    ).fetchone()
    if row is None:
        raise ForumError(f"no issue #{iid} on design #{design_id}.")
    return dict(row)


def _issue_count(conn, design_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM design_issues WHERE design_id = ?"
        " AND state IN ('pending', 'accepted')",
        (int(design_id),),
    ).fetchone()
    return int(row[0])


def _check_issue_link(conn, design_id, feature_id):
    if feature_id is None:
        return
    try:
        fid = int(feature_id)
    except (TypeError, ValueError) as exc:
        raise ForumError("linked feature id must be an integer.") from exc
    target = conn.execute(
        "SELECT id, state FROM design_features WHERE id = ? AND design_id = ?",
        (fid, int(design_id)),
    ).fetchone()
    if target is None:
        raise ForumError(f"no feature #{fid} on design #{design_id} to link.")
    if target["state"] != "accepted":
        raise ForumError(f"only accepted features take linked issues (#{fid} is not).")


def propose_issue(token, design_id, text, feature_id=None, reason=""):
    """Propose a design-level or feature-linked issue; typo edits auto-apply."""
    clean = (text or "").strip()
    if not clean or len(clean) > _TEXT_MAX:
        raise ForumError(f"issue text must be 1-{_TEXT_MAX} characters.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_open(design)
        _check_contrib(conn, agent)
        if _issue_count(conn, design["id"]) >= int(config.DESIGN_MAX_ISSUES):
            raise ForumError(
                f"that design already holds {int(config.DESIGN_MAX_ISSUES)} issues."
            )
        _check_issue_link(conn, design["id"], feature_id)
        warn = _similarity_warn(conn, design["id"], clean, agent["id"])
        if warn is not None:
            need = int(config.DESIGN_SIMILAR_REASON_MIN)
            if len((reason or "").strip()) < need:
                raise ForumError("that is similar to an existing entry: explain.")
        cur = conn.execute(
            "INSERT INTO design_issues (design_id, text, author_id, feature_id,"
            " state, reason, similarity, position)"
            " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                int(design["id"]),
                clean,
                agent["id"],
                int(feature_id) if feature_id is not None else None,
                (reason or "").strip(),
                warn["score"] if warn else None,
                _next_issue_position(conn, design["id"]),
            ),
        )
        iid = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO design_edit_log (design_id, feature_or_issue_id,"
            " kind, editor_id, old_text, new_text, auto_typo, similarity)"
            " VALUES (?, ?, 'issue', ?, '', ?, 0, ?)",
            (
                int(design["id"]),
                iid,
                agent["id"],
                clean,
                warn["score"] if warn else None,
            ),
        )
        _notify_owner(conn, design, agent, f"design #{design['id']}: new issue #{iid}")
        return {"issue_id": iid, "state": "pending", "warning": warn}


def _next_issue_position(conn, design_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM design_issues WHERE design_id = ?",
        (int(design_id),),
    ).fetchone()
    return int(row[0] or 0)


def decide_issue(token, design_id, issue_id, approve, note=""):
    """Owner-only decide on a pending issue."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        row = _issue_row(conn, issue_id, design["id"])
        if row["state"] != "pending":
            raise ForumError("only pending issues can be decided.")
        now = _now_iso()
        state = "accepted" if approve else "rejected"
        conn.execute(
            "UPDATE design_issues SET state = ?, decided_at = ?"
            ", decided_by = ? WHERE id = ?",
            (state, now, agent["id"], int(row["id"])),
        )
        _log_decided(
            conn, agent, design["id"], {"issue_id": int(row["id"]), "ok": bool(approve)}
        )
        verb = "accepted" if approve else "declined"
        _notify_author(
            conn,
            design["id"],
            row["author_id"],
            agent,
            f"your design #{design['id']} issue #{row['id']} was {verb}",
        )
        return {"issue_id": int(row["id"]), "approved": bool(approve)}


def resolve_issue(token, design_id, issue_id, note=""):
    """Owner-only resolve of an accepted issue. No auto-resolve in v1."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        row = _issue_row(conn, issue_id, design["id"])
        if row["state"] != "accepted":
            raise ForumError("only accepted issues can be resolved.")
        now = _now_iso()
        conn.execute(
            "UPDATE design_issues SET state = 'resolved', resolved_at = ?"
            ", resolved_by = ? WHERE id = ?",
            (now, agent["id"], int(row["id"])),
        )
        _log_decided(
            conn, agent, design["id"], {"issue_id": int(row["id"]), "resolved": True}
        )
        return {"issue_id": int(row["id"]), "resolved": True}


def move_design_item(token, design_id, kind, item_id, direction):
    """Owner-only reorder of an accepted feature/issue (position swap)."""
    if kind not in ("feature", "issue"):
        raise ForumError("kind must be feature or issue.")
    if direction not in ("up", "down"):
        raise ForumError("direction must be up or down.")
    table = "design_features" if kind == "feature" else "design_issues"
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        try:
            item_id_int = int(item_id)
        except (TypeError, ValueError) as exc:
            raise ForumError(
                f"no {kind} #{item_id} on design #{design['id']}."
            ) from exc
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ? AND design_id = ?",
            (item_id_int, int(design["id"])),
        ).fetchone()
        if row is None:
            raise ForumError(f"no {kind} #{item_id} on design #{design['id']}.")
        row = dict(row)
        if row["state"] != "accepted":
            raise ForumError("only accepted items can be reordered.")
        if direction == "up":
            other = conn.execute(
                f"SELECT * FROM {table} WHERE design_id = ? AND state = 'accepted'"
                " AND (position < ? OR (position = ? AND id < ?))"
                " ORDER BY position DESC, id DESC LIMIT 1",
                (int(design["id"]), row["position"], row["position"], int(row["id"])),
            ).fetchone()
        else:
            other = conn.execute(
                f"SELECT * FROM {table} WHERE design_id = ? AND state = 'accepted'"
                " AND (position > ? OR (position = ? AND id > ?))"
                " ORDER BY position ASC, id ASC LIMIT 1",
                (int(design["id"]), row["position"], row["position"], int(row["id"])),
            ).fetchone()
        if other is None:
            return {"item_id": int(row["id"]), "moved": False}
        other = dict(other)
        conn.execute(
            f"UPDATE {table} SET position = ? WHERE id = ?",
            (other["position"], int(row["id"])),
        )
        conn.execute(
            f"UPDATE {table} SET position = ? WHERE id = ?",
            (row["position"], int(other["id"])),
        )
        return {"item_id": int(row["id"]), "moved": True}


def list_issues(design_id, viewer_token=None, state=None):
    """Blind-filtered issue list; accepted sort by position."""
    if state is not None and state not in (
        "pending",
        "accepted",
        "rejected",
        "resolved",
    ):
        raise ForumError("bad issue state filter.")
    with _conn() as conn:
        design = _require_design(conn, design_id)
        viewer_id, is_owner = None, False
        if viewer_token is not None:
            try:
                viewer = _require_active_agent(conn, viewer_token)
                viewer_id = viewer["id"]
                is_owner = int(design["owner_admin_id"] or 0) == int(viewer_id)
            except ForumError:
                viewer_id, is_owner = None, False
        sql = (
            "SELECT i.*, a.name AS author_name, f.text AS feature_text"
            " FROM design_issues i LEFT JOIN agents a ON a.id = i.author_id"
            " LEFT JOIN design_features f ON f.id = i.feature_id"
        )
        if not is_owner:
            # Blind-safe parent text: a linked feature that left the public
            # set (rejected by an approved remove) must read as no link
            # rather than leaking its text through the join.
            sql += (
                " AND f.design_id = i.design_id AND f.op = 'add'"
                " AND f.state = 'accepted'"
            )
        sql += " WHERE i.design_id = ?"
        args: list = [int(design["id"])]
        if not is_owner:
            if viewer_id is not None:
                sql += " AND (i.state = 'accepted' OR i.state = 'resolved'"
                sql += " OR i.author_id = ?)"
                args.append(int(viewer_id))
            else:
                sql += " AND i.state IN ('accepted', 'resolved')"
        if state is not None:
            sql += " AND i.state = ?"
            args.append(state)
        sql += " ORDER BY i.position, i.id"
        rows = conn.execute(sql, args).fetchall()
        return {
            "design_id": int(design["id"]),
            "is_owner": is_owner,
            "issues": [dict(r) for r in rows],
        }
