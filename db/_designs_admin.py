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
    _TEXT_MAX,
    _TITLE_MAX,
    REQUEST_TAGS,
    _agent_name,
    _feature_row,
    _is_admin_name,
    _log_decided,
    _next_position,
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


_UNSET = object()


def _direct_feature_target(conn, design_id, feature_id):
    row = _feature_row(conn, feature_id, design_id)
    if row["op"] != "add" or row["state"] != "accepted":
        raise ForumError("only accepted add features can be authored directly.")
    return row


def _direct_issue_target(conn, design_id, issue_id):
    from db._designs_issues import _issue_row

    row = _issue_row(conn, issue_id, design_id)
    if row["state"] != "accepted":
        raise ForumError("only accepted issues can be authored directly.")
    return row


def _direct_issue_link(conn, design_id, feature_id):
    if feature_id is None or str(feature_id).strip() == "":
        return None
    try:
        fid = int(feature_id)
    except (TypeError, ValueError) as exc:
        raise ForumError("linked feature id must be an integer.") from exc
    row = conn.execute(
        "SELECT id, op, state FROM design_features WHERE id = ? AND design_id = ?",
        (fid, int(design_id)),
    ).fetchone()
    if row is None or row["op"] != "add" or row["state"] != "accepted":
        raise ForumError("only accepted add features take linked issues.")
    return fid


def _direct_edit_log(conn, design_id, row_id, kind, editor_id, old_text, new_text):
    conn.execute(
        "INSERT INTO design_edit_log (design_id, feature_or_issue_id, kind,"
        " editor_id, old_text, new_text, auto_typo, similarity)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, NULL)",
        (int(design_id), int(row_id), kind, editor_id, old_text, new_text),
    )


def _issue_edit_snapshot(text, feature_id):
    link = "none" if feature_id is None else str(int(feature_id))
    return f"{text} [feature_id={link}]"


def admin_create_feature(admin, design_id, text):
    clean = (text or "").strip()
    if not clean or len(clean) > _TEXT_MAX:
        raise ForumError(f"feature text must be 1-{_TEXT_MAX} characters.")
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        count = conn.execute(
            "SELECT COUNT(*) FROM design_features WHERE design_id = ?"
            " AND state IN ('pending', 'accepted')",
            (int(design["id"]),),
        ).fetchone()[0]
        if int(count or 0) >= int(config.DESIGN_MAX_FEATURES):
            raise ForumError(
                f"that design already holds {int(config.DESIGN_MAX_FEATURES)} features."
            )
        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO design_features (design_id, text, author_id, state,"
            " op, position, created_at, decided_at, decided_by)"
            " VALUES (?, ?, ?, 'accepted', 'add', ?, ?, ?, ?)",
            (
                int(design["id"]),
                clean,
                agent["id"],
                _next_position(conn, design["id"]),
                now,
                now,
                agent["id"],
            ),
        )
        fid = int(cur.lastrowid or 0)
        _direct_edit_log(conn, design["id"], fid, "feature", agent["id"], "", clean)
        _log_decided(
            conn,
            agent,
            design["id"],
            {"fid": fid, "ok": True, "direct": True, "op": "add"},
        )
        return {"feature_id": fid, "state": "accepted", "direct": True}


def admin_edit_feature(admin, design_id, feature_id, text):
    clean = (text or "").strip()
    if not clean or len(clean) > _TEXT_MAX:
        raise ForumError(f"feature text must be 1-{_TEXT_MAX} characters.")
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        target = _direct_feature_target(conn, design["id"], feature_id)
        conn.execute(
            "UPDATE design_features SET text = ? WHERE id = ?",
            (clean, int(target["id"])),
        )
        _direct_edit_log(
            conn,
            design["id"],
            target["id"],
            "feature",
            agent["id"],
            target["text"],
            clean,
        )
        _log_decided(
            conn,
            agent,
            design["id"],
            {"fid": int(target["id"]), "ok": True, "direct": True, "op": "edit"},
        )
        return {"feature_id": int(target["id"]), "state": "accepted", "direct": True}


def admin_remove_feature(admin, design_id, feature_id):
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        target = _direct_feature_target(conn, design["id"], feature_id)
        linked = conn.execute(
            "SELECT id FROM design_issues WHERE design_id = ? AND feature_id = ?"
            " AND state IN ('pending', 'accepted') LIMIT 1",
            (int(design["id"]), int(target["id"])),
        ).fetchone()
        if linked is not None:
            raise ForumError("cannot remove a feature with linked issues.")
        now = _now_iso()
        conn.execute(
            "UPDATE design_features SET state = 'rejected', decided_at = ?,"
            " decided_by = ? WHERE id = ?",
            (now, agent["id"], int(target["id"])),
        )
        _direct_edit_log(
            conn,
            design["id"],
            target["id"],
            "feature",
            agent["id"],
            target["text"],
            "",
        )
        _log_decided(
            conn,
            agent,
            design["id"],
            {"fid": int(target["id"]), "ok": True, "direct": True, "op": "remove"},
        )
        return {"feature_id": int(target["id"]), "state": "rejected", "direct": True}


def admin_create_issue(admin, design_id, text, feature_id=None):
    clean = (text or "").strip()
    if not clean or len(clean) > _TEXT_MAX:
        raise ForumError(f"issue text must be 1-{_TEXT_MAX} characters.")
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        count = conn.execute(
            "SELECT COUNT(*) FROM design_issues WHERE design_id = ?"
            " AND state IN ('pending', 'accepted')",
            (int(design["id"]),),
        ).fetchone()[0]
        if int(count or 0) >= int(config.DESIGN_MAX_ISSUES):
            raise ForumError(
                f"that design already holds {int(config.DESIGN_MAX_ISSUES)} issues."
            )
        fid = _direct_issue_link(conn, design["id"], feature_id)
        now = _now_iso()
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM design_issues"
            " WHERE design_id = ?",
            (int(design["id"]),),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO design_issues (design_id, text, author_id, feature_id,"
            " state, position, created_at, decided_at, decided_by)"
            " VALUES (?, ?, ?, ?, 'accepted', ?, ?, ?, ?)",
            (
                int(design["id"]),
                clean,
                agent["id"],
                fid,
                int(pos or 0),
                now,
                now,
                agent["id"],
            ),
        )
        iid = int(cur.lastrowid or 0)
        _direct_edit_log(conn, design["id"], iid, "issue", agent["id"], "", clean)
        _log_decided(
            conn,
            agent,
            design["id"],
            {"issue_id": iid, "ok": True, "direct": True, "op": "add"},
        )
        return {"issue_id": iid, "state": "accepted", "direct": True}


def admin_edit_issue(admin, design_id, issue_id, text, feature_id=_UNSET):
    clean = (text or "").strip()
    if not clean or len(clean) > _TEXT_MAX:
        raise ForumError(f"issue text must be 1-{_TEXT_MAX} characters.")
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        target = _direct_issue_target(conn, design["id"], issue_id)
        fid = target["feature_id"]
        if feature_id is not _UNSET:
            fid = _direct_issue_link(conn, design["id"], feature_id)
        conn.execute(
            "UPDATE design_issues SET text = ?, feature_id = ? WHERE id = ?",
            (clean, fid, int(target["id"])),
        )
        old_snapshot = _issue_edit_snapshot(target["text"], target["feature_id"])
        new_snapshot = _issue_edit_snapshot(clean, fid)
        _direct_edit_log(
            conn,
            design["id"],
            target["id"],
            "issue",
            agent["id"],
            old_snapshot,
            new_snapshot,
        )
        _log_decided(
            conn,
            agent,
            design["id"],
            {
                "issue_id": int(target["id"]),
                "old_feature_id": target["feature_id"],
                "feature_id": fid,
                "ok": True,
                "direct": True,
                "op": "edit",
            },
        )
        return {"issue_id": int(target["id"]), "state": "accepted", "direct": True}


def admin_remove_issue(admin, design_id, issue_id):
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        target = _direct_issue_target(conn, design["id"], issue_id)
        now = _now_iso()
        conn.execute(
            "UPDATE design_issues SET state = 'rejected', decided_at = ?,"
            " decided_by = ? WHERE id = ?",
            (now, agent["id"], int(target["id"])),
        )
        _direct_edit_log(
            conn,
            design["id"],
            target["id"],
            "issue",
            agent["id"],
            target["text"],
            "",
        )
        _log_decided(
            conn,
            agent,
            design["id"],
            {"issue_id": int(target["id"]), "ok": True, "direct": True, "op": "remove"},
        )
        return {"issue_id": int(target["id"]), "state": "rejected", "direct": True}


def admin_design_history(admin, design_id):
    with _conn() as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        feats = conn.execute(
            "SELECT f.*, a.name AS author_name, target.text AS target_text"
            " FROM design_features f LEFT JOIN agents a ON a.id = f.author_id"
            " LEFT JOIN design_features target ON target.id = f.target_feature_id"
            " WHERE f.design_id = ? ORDER BY f.position, f.id",
            (int(design["id"]),),
        ).fetchall()
        issues = conn.execute(
            "SELECT i.*, a.name AS author_name, f.text AS feature_text"
            " FROM design_issues i LEFT JOIN agents a ON a.id = i.author_id"
            " LEFT JOIN design_features f ON f.id = i.feature_id"
            " WHERE i.design_id = ? ORDER BY i.position, i.id",
            (int(design["id"]),),
        ).fetchall()
        questions = conn.execute(
            "SELECT q.*, a.name AS asker_name FROM design_questions q"
            " LEFT JOIN agents a ON a.id = q.asker_id"
            " WHERE q.design_id = ? ORDER BY q.id",
            (int(design["id"]),),
        ).fetchall()
        comments = conn.execute(
            "SELECT c.*, a.name AS author_name FROM design_comments c"
            " LEFT JOIN agents a ON a.id = c.author_id"
            " WHERE c.design_id = ? ORDER BY c.created_at, c.id",
            (int(design["id"]),),
        ).fetchall()
        meta = conn.execute(
            "SELECT m.*, a.name AS editor_name FROM design_meta_edits m"
            " LEFT JOIN agents a ON a.id = m.editor_id"
            " WHERE m.design_id = ? ORDER BY m.id",
            (int(design["id"]),),
        ).fetchall()
        edits = conn.execute(
            "SELECT l.*, a.name AS editor_name FROM design_edit_log l"
            " LEFT JOIN agents a ON a.id = l.editor_id"
            " WHERE l.design_id = ? ORDER BY l.id",
            (int(design["id"]),),
        ).fetchall()
        decisions = conn.execute(
            "SELECT e.id, e.kind, e.category, e.actor_agent_id,"
            " COALESCE(e.actor_name, a.name) AS actor_name, e.target_type,"
            " e.target_id, e.detail, e.created_at FROM events e"
            " LEFT JOIN agents a ON a.id = e.actor_agent_id"
            " WHERE e.kind = 'design_decided' AND e.target_type = 'design'"
            " AND e.target_id = ? ORDER BY e.id",
            (int(design["id"]),),
        ).fetchall()
        d = dict(design)
        d["owner_name"] = _agent_name(conn, design["owner_admin_id"])
        d["is_owner"] = True
        return {
            "design": d,
            "features": [dict(r) for r in feats],
            "issues": [dict(r) for r in issues],
            "questions": [dict(r) for r in questions],
            "comments_enabled": bool(design["comments_enabled"]),
            "comments": [dict(r) for r in comments],
            "meta_edits": [dict(r) for r in meta],
            "edit_logs": [dict(r) for r in edits],
            "decisions": [dict(r) for r in decisions],
        }


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
            "SELECT f.*, a.name AS author_name, target.text AS target_text"
            " FROM design_features f LEFT JOIN agents a ON a.id = f.author_id"
            " LEFT JOIN design_features target ON target.id = f.target_feature_id"
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
    the resolved panel actor id, or NULL for an unregistered panel login,
    with the panel username in the detail for audit.
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
            actor_agent_id=agent["id"],
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
    text+tags split, `updated_at` bump); the trail's `editor_id` is the
    resolved panel actor id, or NULL for an unregistered panel login.
    Never resets the 24h promote clock.
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


def _preview_ids(value):
    if value is None:
        return None
    try:
        return tuple(
            int(part.strip()) for part in str(value).split(",") if part.strip()
        )
    except (TypeError, ValueError) as exc:
        raise ForumError("invalid archive preview.") from exc


def admin_close_design(
    admin,
    design_id,
    confirm=False,
    preview_feature_ids=None,
    preview_issue_ids=None,
    preview_question_ids=None,
):
    """Sole-admin archive of a system-owned design (panel authority).

    Mirrors close_design: same 2-step confirm (pending features/issues and
    open questions listed on the first call, dropped on confirm), same
    frozen terminal. Never deleted; the archived event carries the resolved
    panel actor id, or NULL for an unregistered panel login.
    """
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        pend, ipend, open_q = _open_counts(conn, design["id"])
        feature_ids = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM design_features WHERE design_id = ?"
                " AND state = 'pending' ORDER BY id",
                (int(design["id"]),),
            ).fetchall()
        ]
        issue_ids = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM design_issues WHERE design_id = ?"
                " AND state = 'pending' ORDER BY id",
                (int(design["id"]),),
            ).fetchall()
        ]
        question_ids = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM design_questions WHERE design_id = ?"
                " AND state = 'open' ORDER BY id",
                (int(design["id"]),),
            ).fetchall()
        ]
        if (pend or ipend or open_q) and not confirm:
            return {
                "need_confirm": True,
                "pending_features": pend,
                "pending_issues": ipend,
                "open_questions": open_q,
                "pending_feature_ids": feature_ids,
                "pending_issue_ids": issue_ids,
                "open_question_ids": question_ids,
                "hint": "re-run with confirm=True to drop them and archive",
            }
        if confirm and (pend or ipend or open_q):
            expected = (
                _preview_ids(preview_feature_ids),
                _preview_ids(preview_issue_ids),
                _preview_ids(preview_question_ids),
            )
            current = (tuple(feature_ids), tuple(issue_ids), tuple(question_ids))
            if expected != current:
                raise ForumError(
                    "archive preview changed; re-preview before confirming."
                )
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
            actor_agent_id=agent["id"],
            target_type="design",
            target_id=int(design["id"]),
            detail={"panel": agent["name"]},
            conn=conn,
        )
        return {"design_id": int(design["id"]), "status": "archived"}
