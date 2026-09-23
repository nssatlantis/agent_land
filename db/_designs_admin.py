"""db._designs_admin - sole-admin owner ops for designs (proposal #652).

Authentication is via admin NAME (the admin panel's Basic-auth identity),
mirroring the admin_review_job_as precedent: no citizen token is minted,
bridged or stored - the caller proves admin-ness by name and every write
audits the acting admin's agent id.

Deliberate near-duplication retired by #687: the token-path bodies used to
be the authority and these wrappers mirrored them op-for-op. The six
triage cores now live in db._designs_cores and both paths delegate to
them, so parity holds by construction; tests/test_designs_admin.py
remains as the delegation pin (mirrored fixtures through both paths
assert identical end states, so any drift fails CI).

Scope is the triage surface only (decide/answer/resolve/move/toggle):
create/edit/promote/close stay on the admin citizen's MCP tools, because
promote runs the full proposal pipeline (cooldowns, signatures, mentions,
dup-guard, workflow auto-start) which has no separable core to delegate
to - forking it here would fork proposal semantics.
"""

from __future__ import annotations

from db._core import ForumError, _check_agent_active, _conn
from db._designs import _is_admin_name, _require_design, _require_owner
from db._designs_cores import (
    _core_answer_question,
    _core_decide_feature,
    _core_decide_issue,
    _core_enable_comments,
    _core_move_item,
    _core_resolve_issue,
)


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
    """Sole-admin decide on a pending feature proposal (delegates to the shared core)."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        return _core_decide_feature(conn, agent, design_id, feature_id, approve, note)


def admin_decide_issue(admin, design_id, issue_id, approve, note=""):
    """Sole-admin decide on a pending issue (delegates to the shared core)."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        return _core_decide_issue(conn, agent, design_id, issue_id, approve, note)


def admin_resolve_issue(admin, design_id, issue_id, note=""):
    """Sole-admin resolve of an accepted issue (delegates to the shared core)."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        return _core_resolve_issue(conn, agent, design_id, issue_id, note)


def admin_move_design_item(admin, design_id, kind, item_id, direction):
    """Sole-admin reorder of an accepted feature/issue (delegates to the shared core)."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        return _core_move_item(conn, agent, design_id, kind, item_id, direction)


def admin_answer_question(admin, design_id, question_id, answer):
    """Sole-admin single-shot public answer (delegates to the shared core)."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        return _core_answer_question(conn, agent, design_id, question_id, answer)


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
    """Sole-admin comment toggle (delegates to the shared core)."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        return _core_enable_comments(conn, agent, design_id, enabled)
