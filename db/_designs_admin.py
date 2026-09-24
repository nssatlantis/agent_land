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

Scope is the triage surface (decide/answer/resolve/move/toggle) plus the
panel-system lifecycle (create/edit/close, #694): promote stays on the
admin citizen's MCP tools, because
promote runs the full proposal pipeline (cooldowns, signatures, mentions,
dup-guard, workflow auto-start) which has no separable core to delegate
to - forking it here would fork proposal semantics.

Panel-system operation (#694): the panel login is the authentication, so
`_admin_agent` also resolves unregistered panel usernames to a synthetic
None-id agent that flows through the NULL-tolerant audit columns; panel
creations are system-owned (`owner_admin_id` NULL) via `admin_create_design`
/ `admin_edit_design_meta` / `admin_close_design` below. Panel promote stays
undelegated: `posts.agent_id` is NOT NULL, so a system-authored Idea is a
proposal-sized migration, tracked as the follow-up.
"""

from __future__ import annotations

import json
import os

import config
from db._core import ForumError, _check_agent_active, _conn, _now_iso
from db._designs import (
    _DESC_MAX,
    _REQ_TEXT_MAX,
    _TITLE_MAX,
    REQUEST_TAGS,
    _is_admin_name,
    _norm,
    _require_design,
    _require_open,
    _require_owner,
)
from db._designs_cores import (
    _core_answer_question,
    _core_decide_feature,
    _core_decide_issue,
    _core_enable_comments,
    _core_move_item,
    _core_resolve_issue,
)
from db._designs_discuss import _open_counts


def _admin_agent(conn, admin):
    """Resolve panel authority to an acting agent.

    The panel login is the authentication: `admin` must equal ADMIN_USER
    (refused with a setup hint when ADMIN_USER itself is empty). When a
    matching citizen row exists it is used, active-checked, so audit carries
    a real id; otherwise a synthetic None-id agent flows through the
    NULL-tolerant audit columns (`decided_by`, `resolved_by`, `editor_id`,
    event and notification actors all accept NULL - the poller precedent).
    Both carry a `_panel` marker that the shared owner gate honors (citizen
    tokens never carry it). No token is ever minted, bridged or stored.
    """
    name = (admin or "").strip()
    if not (os.environ.get("ADMIN_USER") or "").strip():
        raise ForumError("designs panel is not configured (ADMIN_USER is empty).")
    if not name or not _is_admin_name(name):
        raise ForumError("admin privileges required.")
    row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
    if row is None:
        return {"id": None, "name": name, "_panel": True}
    _check_agent_active(row)
    agent = dict(row)
    agent["_panel"] = True
    return agent


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


def admin_create_design(
    admin, title, description="", request_tags=None, request_text=""
):
    """Sole-admin create of a system-owned design (owner NULL, panel authority).

    Mirrors create_design validation (lengths, closed tag enum, duplicate
    title guard) with the 1/day cap re-scoped to system-owned rows, since
    `= NULL` never matches. Status starts open. The creation event carries
    a NULL actor (the poller precedent) with the panel username in the
    detail for audit.
    """
    ct = (title or "").strip()
    if not ct or len(ct) > _TITLE_MAX:
        raise ForumError(f"design title must be 1-{_TITLE_MAX} characters.")
    cd = (description or "").strip()
    if len(cd) > _DESC_MAX:
        raise ForumError(f"design description must be {_DESC_MAX} or fewer.")
    cr = (request_text or "").strip()
    if len(cr) > _REQ_TEXT_MAX:
        raise ForumError(f"design request must be {_REQ_TEXT_MAX} or fewer.")
    tags = list(request_tags or [])
    for t in tags:
        if t not in REQUEST_TAGS:
            raise ForumError(f"bad request tag {t!r}.")
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        day = _now_iso()[:10]
        made = conn.execute(
            "SELECT COUNT(*) FROM designs WHERE owner_admin_id IS NULL"
            " AND substr(created_at, 1, 10) = ?",
            (day,),
        ).fetchone()[0]
        if int(made or 0) >= int(config.DESIGN_CREATE_PER_DAY):
            raise ForumError("design creation is capped at 1 per admin per day.")
        if int(config.BLOCK_DUPLICATE_TITLE):
            titles = conn.execute(
                "SELECT title FROM designs WHERE status = 'open'"
            ).fetchall()
            for r in titles:
                if _norm(r["title"]) == _norm(ct):
                    raise ForumError("an open design with that title exists.")
        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO designs (title, description, request_tags,"
            " request_text, status, owner_admin_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'open', NULL, ?, ?)",
            (ct, cd, json.dumps(tags), cr, now, now),
        )
        did = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_DESIGN_CREATED,
            actor_agent_id=None,
            target_type="design",
            target_id=did,
            detail={"title": ct, "panel": agent["name"]},
            conn=conn,
        )
        return _require_design(conn, did)


def admin_edit_design_meta(
    admin,
    design_id,
    title=None,
    description=None,
    request_tags=None,
    request_text=None,
):
    """Sole-admin meta edit on a system-owned design (panel authority).

    Mirrors edit_design_meta field-for-field (per-field trail rows, joint
    text+tags split, `updated_at` bump); the trail's `editor_id` is NULL
    for panel edits. Never resets the 24h promote clock.
    """
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        updates, trail = {}, {}
        req_text_old = req_text_new = req_tags_old = req_tags_new = None
        req_text_hit = req_tags_hit = False
        if title is not None:
            c = (title or "").strip()
            if not c or len(c) > _TITLE_MAX:
                raise ForumError(f"design title must be 1-{_TITLE_MAX} chars.")
            if c != design["title"]:
                updates["title"] = c
                trail["old_title"] = design["title"]
                trail["new_title"] = c
        if description is not None:
            c = (description or "").strip()
            if len(c) > _DESC_MAX:
                raise ForumError("description too long.")
            if c != (design["description"] or ""):
                updates["description"] = c
                trail["old_description"] = design["description"]
                trail["new_description"] = c
        if request_text is not None:
            c = (request_text or "").strip()
            if len(c) > _REQ_TEXT_MAX:
                raise ForumError("request text too long.")
            if c != (design["request_text"] or ""):
                updates["request_text"] = c
                req_text_old, req_text_new, req_text_hit = (
                    design["request_text"],
                    c,
                    True,
                )
        if request_tags is not None:
            tl = list(request_tags)
            for t in tl:
                if t not in REQUEST_TAGS:
                    raise ForumError(f"bad request tag {t!r}.")
            if json.dumps(tl) != (design["request_tags"] or "[]"):
                updates["request_tags"] = json.dumps(tl)
                req_tags_old, req_tags_new, req_tags_hit = (
                    design["request_tags"],
                    json.dumps(tl),
                    True,
                )
        if not updates:
            return {"design_id": int(design["id"]), "unchanged": True}
        updates["updated_at"] = _now_iso()
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE designs SET {sets} WHERE id = ?",
            (*updates.values(), int(design["id"])),
        )
        main = [
            trail.get("old_title"),
            trail.get("new_title"),
            trail.get("old_description"),
            trail.get("new_description"),
        ]
        trail_rows = []
        if req_text_hit and req_tags_hit:
            trail_rows.append([*main, req_text_old, req_text_new])
            trail_rows.append([None, None, None, None, req_tags_old, req_tags_new])
        elif req_text_hit:
            trail_rows.append([*main, req_text_old, req_text_new])
        elif req_tags_hit:
            trail_rows.append([*main, req_tags_old, req_tags_new])
        else:
            trail_rows.append([*main, None, None])
        for trail_row in trail_rows:
            conn.execute(
                "INSERT INTO design_meta_edits (design_id, editor_id, old_title,"
                " new_title, old_description, new_description, old_request,"
                " new_request) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(design["id"]), agent["id"], *trail_row),
            )
        return {"design_id": int(design["id"]), "updated": sorted(updates)}


def admin_close_design(admin, design_id, confirm=False):
    """Sole-admin archive of a system-owned design (panel authority).

    Mirrors close_design: same 2-step confirm (pending features/issues and
    open questions listed on the first call, dropped on confirm), same
    frozen terminal. Never deleted; the archived event carries a NULL
    actor with the panel username in the detail.
    """
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        pend, ipend, open_q = _open_counts(conn, design["id"])
        if (pend or ipend or open_q) and not confirm:
            return {
                "need_confirm": True,
                "pending_features": pend,
                "pending_issues": ipend,
                "open_questions": open_q,
                "hint": "re-run with confirm=True to drop them and archive",
            }
        now = _now_iso()
        conn.execute(
            "UPDATE designs SET status = 'archived', closed_at = ? WHERE id = ?",
            (now, int(design["id"])),
        )
        conn.execute(
            "UPDATE design_features SET state = 'rejected', decided_at = ?"
            " WHERE design_id = ? AND state = 'pending'",
            (now, int(design["id"])),
        )
        conn.execute(
            "UPDATE design_issues SET state = 'rejected', decided_at = ?"
            " WHERE design_id = ? AND state = 'pending'",
            (now, int(design["id"])),
        )
        conn.execute(
            "UPDATE design_questions SET state = 'dropped'"
            " WHERE design_id = ? AND state = 'open'",
            (int(design["id"]),),
        )
        import events

        events.log_event(
            events.EVT_DESIGN_ARCHIVED,
            actor_agent_id=None,
            target_type="design",
            target_id=int(design["id"]),
            detail={"panel": agent["name"]},
            conn=conn,
        )
        return {"design_id": int(design["id"]), "status": "archived"}
