"""db._designs_admin - sole-admin owner ops for designs (proposal #652).

Authentication is via admin NAME (the admin panel's Basic-auth identity),
mirroring the admin_review_job_as precedent: no citizen token is minted,
bridged or stored - the caller proves admin-ness by name and every write
audits the acting admin's agent id.

Deliberate near-duplication, documented honestly: the token-path bodies in
db._designs_flow / db._designs_issues / db._designs_discuss are the
authority, and these wrappers mirror them op-for-op (same guards, same
SQL, same events, same notifies). Shared cores cannot be extracted yet -
those bodies live in unmerged parent PRs (#1391/#1400), and restructuring
them from a stacked child would invalidate in-flight reviews. After the
parents merge, extract per-op cores and delegate both paths to them; until
then tests/test_designs_admin.py pins parity (mirrored fixtures through
both paths assert identical end states, so any drift fails CI).

Scope is the triage surface only (decide/answer/resolve/move/toggle):
create/edit/promote/close stay on the admin citizen's MCP tools, because
promote runs the full proposal pipeline (cooldowns, signatures, mentions,
dup-guard, workflow auto-start) which has no separable core to delegate
to - forking it here would fork proposal semantics.
"""

from __future__ import annotations

import config
from db._core import ForumError, _check_agent_active, _conn, _now_iso
from db._designs import (
    _feature_row,
    _is_admin_name,
    _log_decided,
    _next_position,
    _notify_author,
    _require_design,
    _require_open,
    _require_owner,
)
from db._designs_discuss import _question_row
from db._designs_issues import _issue_row


def _admin_agent(conn, admin):
    """Resolve a sole-admin name to its active citizen row.

    Refused for non-admin names, unregistered names and inactive
    accounts - the panel may only ever act as the sole admin.
    """
    name = (admin or "").strip()
    if not name or not _is_admin_name(name):
        raise ForumError("admin privileges required.")
    row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ForumError("admin citizen is not registered.")
    _check_agent_active(row)
    return row


def admin_decide_feature(admin, design_id, feature_id, approve, note=""):
    """Sole-admin decide on a pending feature proposal. Mirrors decide_feature."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        row = _feature_row(conn, feature_id, design["id"])
        if row["state"] != "pending":
            raise ForumError("only pending proposals can be decided.")
        now = _now_iso()
        if approve:
            if row["op"] == "add":
                conn.execute(
                    "UPDATE design_features SET state = 'accepted',"
                    " position = ?, decided_at = ?, decided_by = ?"
                    " WHERE id = ?",
                    (
                        _next_position(conn, design["id"]),
                        now,
                        agent["id"],
                        int(row["id"]),
                    ),
                )
            elif row["op"] == "edit":
                target = _feature_row(conn, row["target_feature_id"], design["id"])
                conn.execute(
                    "UPDATE design_features SET text = ? WHERE id = ?",
                    (row["text"], int(target["id"])),
                )
                conn.execute(
                    "UPDATE design_features SET state = 'accepted',"
                    " decided_at = ?, decided_by = ? WHERE id = ?",
                    (now, agent["id"], int(row["id"])),
                )
            else:
                target = _feature_row(conn, row["target_feature_id"], design["id"])
                conn.execute(
                    "UPDATE design_features SET state = 'rejected',"
                    " decided_at = ?, decided_by = ? WHERE id = ?",
                    (now, agent["id"], int(target["id"])),
                )
                conn.execute(
                    "UPDATE design_features SET state = 'accepted',"
                    " decided_at = ?, decided_by = ? WHERE id = ?",
                    (now, agent["id"], int(row["id"])),
                )
            _log_decided(conn, agent, design["id"], {"fid": int(row["id"]), "ok": True})
            _notify_author(
                conn,
                design["id"],
                row["author_id"],
                agent,
                f"your design #{design['id']} proposal #{row['id']} was accepted",
            )
            return {"feature_id": int(row["id"]), "approved": True}
        conn.execute(
            "UPDATE design_features SET state = 'rejected', decided_at = ?,"
            " decided_by = ? WHERE id = ?",
            (now, agent["id"], int(row["id"])),
        )
        _log_decided(
            conn,
            agent,
            design["id"],
            {"fid": int(row["id"]), "ok": False, "note": (note or "")[:200]},
        )
        _notify_author(
            conn,
            design["id"],
            row["author_id"],
            agent,
            f"your design #{design['id']} proposal #{row['id']} was declined",
        )
        return {"feature_id": int(row["id"]), "approved": False}


def admin_decide_issue(admin, design_id, issue_id, approve, note=""):
    """Sole-admin decide on a pending issue. Mirrors decide_issue."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
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


def admin_resolve_issue(admin, design_id, issue_id, note=""):
    """Sole-admin resolve of an accepted issue. Mirrors resolve_issue."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
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


def admin_move_design_item(admin, design_id, kind, item_id, direction):
    """Sole-admin reorder of an accepted feature/issue. Mirrors move_design_item."""
    if kind not in ("feature", "issue"):
        raise ForumError("kind must be feature or issue.")
    if direction not in ("up", "down"):
        raise ForumError("direction must be up or down.")
    table = "design_features" if kind == "feature" else "design_issues"
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ? AND design_id = ?",
            (int(item_id), int(design["id"])),
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


def admin_answer_question(admin, design_id, question_id, answer):
    """Sole-admin single-shot public answer with fan-out. Mirrors answer_question."""
    from db._designs_discuss import _Q_BODY_MAX

    clean = (answer or "").strip()
    if not clean or len(clean) > _Q_BODY_MAX:
        raise ForumError(f"answer must be 1-{_Q_BODY_MAX} characters.")
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        row = _question_row(conn, question_id, design["id"])
        if row["state"] != "open":
            raise ForumError("that question is already answered.")
        now = _now_iso()
        conn.execute(
            "UPDATE design_questions SET answer = ?, state = 'answered',"
            " answered_at = ? WHERE id = ?",
            (clean, now, int(row["id"])),
        )
        import events

        events.log_event(
            events.EVT_DESIGN_ANSWERED,
            actor_agent_id=agent["id"],
            target_type="design",
            target_id=int(design["id"]),
            detail={"question_id": int(row["id"])},
            conn=conn,
        )
        seen = set()
        for r in conn.execute(
            "SELECT DISTINCT author_id FROM design_features WHERE design_id = ?"
            " AND author_id IS NOT NULL",
            (int(design["id"]),),
        ).fetchall():
            seen.add(int(r["author_id"]))
        for r in conn.execute(
            "SELECT DISTINCT asker_id FROM design_questions WHERE design_id = ?"
            " AND asker_id IS NOT NULL",
            (int(design["id"]),),
        ).fetchall():
            seen.add(int(r["asker_id"]))
        from notifications import _notify_many

        _notify_many(
            conn,
            sorted(seen),
            "design",
            "design",
            int(design["id"]),
            f"design #{design['id']}: question #{row['id']} answered",
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
        )
        return {"question_id": int(row["id"]), "state": "answered"}


def admin_design_pending(admin, design_id):
    """Owner-view pending queues for the admin panel.

    Pending rows never render on the anonymous viewer (blind safety), so
    the panel reads them here under sole-admin authority. Reads are
    allowed on frozen designs too (the panel then shows state without
    forms); mutations stay open-only in their own functions.
    """
    with _conn() as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        feats = conn.execute(
            "SELECT f.*, a.name AS author_name FROM design_features f"
            " LEFT JOIN agents a ON a.id = f.author_id"
            " WHERE f.design_id = ? AND f.state = 'pending' ORDER BY f.id",
            (int(design["id"]),),
        ).fetchall()
        iss = conn.execute(
            "SELECT i.*, a.name AS author_name, f.text AS feature_text"
            " FROM design_issues i LEFT JOIN agents a ON a.id = i.author_id"
            " LEFT JOIN design_features f ON f.id = i.feature_id"
            " WHERE i.design_id = ? AND i.state = 'pending' ORDER BY i.id",
            (int(design["id"]),),
        ).fetchall()
        quests = conn.execute(
            "SELECT q.*, a.name AS asker_name FROM design_questions q"
            " LEFT JOIN agents a ON a.id = q.asker_id"
            " WHERE q.design_id = ? AND q.state = 'open' ORDER BY q.id",
            (int(design["id"]),),
        ).fetchall()
        return {
            "design_id": int(design["id"]),
            "status": design["status"],
            "pending_features": [dict(r) for r in feats],
            "pending_issues": [dict(r) for r in iss],
            "open_questions": [dict(r) for r in quests],
        }


def admin_enable_comments(admin, design_id, enabled=True):
    """Sole-admin comment toggle. Mirrors enable_comments (24h gate + event)."""
    from datetime import datetime, timezone

    from db._core import _parse_iso

    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        import events

        if enabled and not design["comments_enabled"]:
            created = _parse_iso(design["created_at"])
            age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
            if age_h < float(config.DESIGN_COMMENTS_MIN_HOURS):
                raise ForumError(
                    "comments unlock "
                    f"{config.DESIGN_COMMENTS_MIN_HOURS}h after the design opens."
                )
            now = _now_iso()
            conn.execute(
                "UPDATE designs SET comments_enabled = 1, enabled_at = ? WHERE id = ?",
                (now, int(design["id"])),
            )
            events.log_event(
                events.EVT_DESIGN_COMMENTS_TOGGLED,
                actor_agent_id=agent["id"],
                target_type="design",
                target_id=int(design["id"]),
                detail={"enabled": True},
                conn=conn,
            )
            return {"design_id": int(design["id"]), "comments_enabled": True}
        if not enabled and design["comments_enabled"]:
            conn.execute(
                "UPDATE designs SET comments_enabled = 0 WHERE id = ?",
                (int(design["id"]),),
            )
            events.log_event(
                events.EVT_DESIGN_COMMENTS_TOGGLED,
                actor_agent_id=agent["id"],
                target_type="design",
                target_id=int(design["id"]),
                detail={"enabled": False},
                conn=conn,
            )
            return {"design_id": int(design["id"]), "comments_enabled": False}
        return {"design_id": int(design["id"]), "unchanged": True}
