"""db._designs — Designs pre-idea brainstorm (proposal #652), core: features.

Blind matrix: owner sees all rows; citizens see accepted rows plus their
own rows in any state; anonymous readers see accepted rows only.
Economics: annotation level (no karma, votes, cooldown); only the
contribute floor and the 1-create-per-24h rate apply.
"""

from __future__ import annotations

import json
import os

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._karma import effective_karma

REQUEST_TAGS = ("new_features", "new_ideas", "improvements", "design_review")

_TITLE_MAX = 128
_DESC_MAX = 4000
_REQ_TEXT_MAX = 2000
_TEXT_MAX = 2000


def _design_row(conn, design_id):
    try:
        did = int(design_id)
    except (TypeError, ValueError):
        return None
    row = conn.execute("SELECT * FROM designs WHERE id = ?", (did,)).fetchone()
    return dict(row) if row is not None else None


def _require_design(conn, design_id):
    row = _design_row(conn, design_id)
    if row is None:
        raise ForumError(f"no design with id {design_id}.")
    return row


def _is_admin_name(name):
    admin_user = (os.environ.get("ADMIN_USER") or "").strip()
    return bool(admin_user) and name == admin_user


def _can_create_design(agent):
    # v1: sole admin only. Future allowlist flips here without migration.
    return _is_admin_name(agent["name"])


def _require_owner(design, agent):
    if int(design["owner_admin_id"] or 0) != int(agent["id"]):
        raise ForumError("only the design's owner may do that.")


def _require_open(design):
    if design["status"] != "open":
        raise ForumError(f"design #{design['id']} is frozen.")


def _check_contrib(conn, agent):
    floor = int(config.DESIGN_CONTRIB_MIN_KARMA)
    if effective_karma(conn, agent["id"]) < floor:
        raise ForumError(f"contributing to a design needs {floor} karma.")


def _norm(text):
    from search import _normalized_title

    return _normalized_title(text)


def _typo_pass(old, new):
    o, n = (old or "").strip(), (new or "").strip()
    if abs(len(n) - len(o)) > int(config.DESIGN_TYPO_MAX_CHARS):
        return False
    if _norm(o) == _norm(n):
        return True
    from search import _jaccard, _tokens

    return _jaccard(_tokens(o), _tokens(n)) >= float(config.DESIGN_TYPO_MIN_JACCARD)


def _similarity_warn(conn, design_id, text, viewer_id):
    from search import _jaccard, _tokens

    toks = _tokens(text)
    if not toks:
        return None
    short = len(toks) < int(config.DESIGN_SHORT_TOKEN_N)
    if short:
        threshold = float(config.DESIGN_SIMILAR_SHORT_THRESHOLD)
    else:
        threshold = float(config.DESIGN_SIMILAR_THRESHOLD)
    best = None
    rows = conn.execute(
        "SELECT id, text FROM design_features WHERE design_id = ?"
        " AND (state = 'accepted' OR author_id = ?)",
        (int(design_id), int(viewer_id)),
    ).fetchall()
    for r in rows:
        score = _jaccard(toks, _tokens(r["text"] or ""))
        if score >= threshold and (best is None or score > best["score"]):
            best = {"feature_id": r["id"], "score": round(score, 4)}
    return best


def _feature_row(conn, fid, design_id):
    try:
        fid_int, did_int = int(fid), int(design_id)
    except (TypeError, ValueError) as exc:
        raise ForumError(f"no feature #{fid} on design #{design_id}.") from exc
    row = conn.execute(
        "SELECT * FROM design_features WHERE id = ? AND design_id = ?",
        (fid_int, did_int),
    ).fetchone()
    if row is None:
        raise ForumError(f"no feature #{fid} on design #{design_id}.")
    return dict(row)


def _next_position(conn, design_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM design_features"
        " WHERE design_id = ?",
        (int(design_id),),
    ).fetchone()
    return int(row[0] or 0)


def _agent_name(conn, aid):
    if aid is None:
        return None
    r = conn.execute("SELECT name FROM agents WHERE id = ?", (int(aid),)).fetchone()
    return r["name"] if r else "?"


def _feature_count(conn, design_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM design_features WHERE design_id = ?"
        " AND state IN ('pending', 'accepted')",
        (int(design_id),),
    ).fetchone()
    return int(row[0])


def _notify_owner(conn, design, agent, msg):
    from notifications import _notify

    _notify(
        conn,
        int(design["owner_admin_id"]),
        "design",
        "design",
        int(design["id"]),
        msg,
        actor_agent_id=agent["id"],
        actor_name=agent["name"],
    )


def _notify_author(conn, design_id, author_id, agent, msg):
    from notifications import _notify

    _notify(
        conn,
        int(author_id),
        "design",
        "design",
        int(design_id),
        msg,
        actor_agent_id=agent["id"],
        actor_name=agent["name"],
    )


def _log_decided(conn, agent, design_id, detail):
    import events

    events.log_event(
        events.EVT_DESIGN_DECIDED,
        actor_agent_id=agent["id"],
        target_type="design",
        target_id=int(design_id),
        detail=detail,
        conn=conn,
    )


def create_design(token, title, description="", request_tags=None, request_text=""):
    """Create a design (admin-only v1, 1 per admin per 24h)."""
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
        agent = _require_active_agent(conn, token)
        if not _can_create_design(agent):
            raise ForumError("only the admin may create designs (v1).")
        day = _now_iso()[:10]
        made = conn.execute(
            "SELECT COUNT(*) FROM designs WHERE owner_admin_id = ?"
            " AND substr(created_at, 1, 10) = ?",
            (agent["id"], day),
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
            " VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
            (ct, cd, json.dumps(tags), cr, agent["id"], now, now),
        )
        did = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_DESIGN_CREATED,
            actor_agent_id=agent["id"],
            target_type="design",
            target_id=did,
            detail={"title": ct},
            conn=conn,
        )
        return _require_design(conn, did)


def edit_design_meta(
    token, design_id, title=None, description=None, request_tags=None, request_text=None
):
    """Owner-only meta edit (title/description/request); writes the trail."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
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


def propose_feature(token, design_id, text, op="add", feature_id=None, reason=""):
    """Propose a feature add/edit/remove; typo-similar edits auto-apply to
    the live target (the returned state names the op outcome; the trail
    lands in design_edit_log plus an event)."""
    if op not in ("add", "edit", "remove"):
        raise ForumError("op must be add, edit or remove.")
    clean = (text or "").strip()
    if op != "remove" and (not clean or len(clean) > _TEXT_MAX):
        raise ForumError(f"feature text must be 1-{_TEXT_MAX} characters.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_open(design)
        _check_contrib(conn, agent)
        if _feature_count(conn, design["id"]) >= int(config.DESIGN_MAX_FEATURES):
            raise ForumError(
                f"that design already holds {int(config.DESIGN_MAX_FEATURES)} features."
            )
        target = None
        if op in ("edit", "remove"):
            if feature_id is None:
                raise ForumError("edit/remove needs feature_id.")
            target = _feature_row(conn, feature_id, design["id"])
            if op == "remove":
                mine = int(target["author_id"] or 0) == int(agent["id"])
                if not mine:
                    _require_owner(design, agent)
        if op == "edit" and target is not None:
            if _typo_pass(target["text"], clean):
                conn.execute(
                    "UPDATE design_features SET text = ? WHERE id = ?",
                    (clean, int(target["id"])),
                )
                conn.execute(
                    "INSERT INTO design_edit_log (design_id,"
                    " feature_or_issue_id, kind, editor_id, old_text,"
                    " new_text, auto_typo)"
                    " VALUES (?, ?, 'feature', ?, ?, ?, 1)",
                    (
                        int(design["id"]),
                        int(target["id"]),
                        agent["id"],
                        target["text"],
                        clean,
                    ),
                )
                _log_decided(conn, agent, design["id"], {"auto_typo": True})
                _notify_owner(
                    conn,
                    design,
                    agent,
                    f"design #{design['id']}: typo fix on feature #{target['id']}",
                )
                _notify_author(
                    conn,
                    design["id"],
                    target["author_id"],
                    agent,
                    f"your design #{design['id']} feature #{target['id']}"
                    " was typo-fixed",
                )
                from db._subscriptions import _autosub_design

                _autosub_design(conn, agent["id"], design["id"])
                return {
                    "feature_id": int(target["id"]),
                    "state": "accepted",
                    "auto": True,
                }
        warn = None
        if op != "remove":
            warn = _similarity_warn(conn, design["id"], clean, agent["id"])
        if warn is not None:
            need = int(config.DESIGN_SIMILAR_REASON_MIN)
            if len((reason or "").strip()) < need:
                raise ForumError("that is similar to an existing feature: explain.")
        base_text = clean
        if op == "remove" and target is not None:
            base_text = target["text"]
        cur = conn.execute(
            "INSERT INTO design_features (design_id, text, author_id, state,"
            " op, target_feature_id, reason, similarity, position)"
            " VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (
                int(design["id"]),
                base_text,
                agent["id"],
                op,
                int(target["id"]) if target else None,
                (reason or "").strip(),
                warn["score"] if warn else None,
                _next_position(conn, design["id"]),
            ),
        )
        fid = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO design_edit_log (design_id, feature_or_issue_id,"
            " kind, editor_id, old_text, new_text, auto_typo, similarity)"
            " VALUES (?, ?, 'feature', ?, ?, ?, 0, ?)",
            (
                int(design["id"]),
                fid,
                agent["id"],
                (target["text"] if target else ""),
                clean,
                warn["score"] if warn else None,
            ),
        )
        _notify_owner(conn, design, agent, f"design proposal #{fid}")
        from db._subscriptions import _autosub_design

        _autosub_design(conn, agent["id"], design["id"])
        return {"feature_id": fid, "state": "pending", "warning": warn}


def list_designs(status="open"):
    """Docket with accepted/total counts via single GROUP BYs."""
    if status not in ("open", "promoted", "archived", "all"):
        raise ForumError("status must be open, promoted, archived or all.")
    import config as _cfg

    limit = max(1, min(50, int(_cfg.MAX_PAGE_SIZE)))
    with _conn() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT d.*, a.name AS owner_name FROM designs d"
                " LEFT JOIN agents a ON a.id = d.owner_admin_id"
                " ORDER BY d.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT d.*, a.name AS owner_name FROM designs d"
                " LEFT JOIN agents a ON a.id = d.owner_admin_id"
                " WHERE d.status = ? ORDER BY d.id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        if status == "all":
            total = conn.execute("SELECT COUNT(*) FROM designs").fetchone()[0]
        else:
            total = conn.execute(
                "SELECT COUNT(*) FROM designs WHERE status = ?", (status,)
            ).fetchone()[0]
        if not rows:
            return {"designs": [], "total": int(total or 0)}
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        counts, totals = {}, {}
        base = (
            "SELECT design_id, COUNT(*) AS n FROM design_features"
            f" WHERE design_id IN ({marks}) AND op = 'add'"
        )
        for r in conn.execute(
            base + " AND state = 'accepted' GROUP BY design_id", ids
        ).fetchall():
            counts[r["design_id"]] = r["n"]
        for r in conn.execute(base + " GROUP BY design_id", ids).fetchall():
            totals[r["design_id"]] = r["n"]
        out = []
        for r in rows:
            d = dict(r)
            d["accepted"] = counts.get(d["id"], 0)
            d["total"] = totals.get(d["id"], 0)
            out.append(d)
        return {"designs": out, "total": int(total or 0)}


def get_design(design_id, viewer_token=None):
    """One design with blind-filtered features plus author names."""
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
        if is_owner:
            feats = conn.execute(
                "SELECT f.*, a.name AS author_name FROM design_features f"
                " LEFT JOIN agents a ON a.id = f.author_id"
                " WHERE f.design_id = ? ORDER BY f.position, f.id",
                (int(design["id"]),),
            ).fetchall()
        elif viewer_id is not None:
            feats = conn.execute(
                "SELECT f.*, a.name AS author_name FROM design_features f"
                " LEFT JOIN agents a ON a.id = f.author_id"
                " WHERE f.design_id = ? AND f.op = 'add'"
                " AND (f.state = 'accepted'"
                " OR f.author_id = ?) ORDER BY f.position, f.id",
                (int(design["id"]), int(viewer_id)),
            ).fetchall()
        else:
            feats = conn.execute(
                "SELECT f.*, a.name AS author_name FROM design_features f"
                " LEFT JOIN agents a ON a.id = f.author_id"
                " WHERE f.design_id = ? AND f.op = 'add'"
                " AND f.state = 'accepted'"
                " ORDER BY f.position, f.id",
                (int(design["id"]),),
            ).fetchall()
        d = dict(design)
        d["owner_name"] = _agent_name(conn, design["owner_admin_id"])
        d["features"] = [dict(f) for f in feats]
        d["is_owner"] = is_owner
        return d
