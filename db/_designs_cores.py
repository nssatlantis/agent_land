"""db._designs_cores — shared per-op cores for designs (proposal #652, #687).

The token path (db._designs_flow / db._designs_issues / db._designs_discuss)
and the sole-admin path (db._designs_admin) both delegate here, so the six
triage ops run one body under either authority. Callers resolve the acting
agent (token via _require_active_agent, panel via _admin_agent), open the
immediate transaction, load nothing else, and hand (conn, agent) in — the
core loads the design, enforces owner + open, then runs the exact
token-authority SQL, events and notifies.

Row helpers for issues/questions stay in their defining modules and are
imported lazily inside each core to avoid a top-level import cycle (cores
<- flow/issues/discuss <- cores). Events, notifications and subscription
fan-out are likewise function-level imports, matching house style.
"""

from __future__ import annotations

import config
from db._core import ForumError, _now_iso
from db._designs import (
    _feature_row,
    _log_decided,
    _next_position,
    _notify_author,
    _require_design,
    _require_open,
    _require_owner,
)


def _core_decide_feature(conn, agent, design_id, feature_id, approve, note=""):
    """Decide a pending feature proposal (add/edit/remove)."""
    design = _require_design(conn, design_id)
    _require_owner(design, agent)
    _require_open(design)
    row = _feature_row(conn, feature_id, design["id"])
    if row["state"] != "pending":
        raise ForumError("only pending proposals can be decided.")
    if approve and row["op"] == "remove":
        target = _feature_row(conn, row["target_feature_id"], design["id"])
        linked = conn.execute(
            "SELECT id FROM design_issues WHERE design_id = ? AND feature_id = ?"
            " AND state IN ('pending', 'accepted') LIMIT 1",
            (int(design["id"]), int(target["id"])),
        ).fetchone()
        if linked is not None:
            raise ForumError("cannot remove a feature with linked issues.")
    now = _now_iso()
    if approve and row["op"] in ("edit", "remove"):
        target = _feature_row(conn, row["target_feature_id"], design["id"])
        if target["op"] != "add" or target["state"] != "accepted":
            raise ForumError("only accepted add features can be edit/remove targets.")
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
        _log_decided(
            conn,
            agent,
            design["id"],
            {"fid": int(row["id"]), "ok": True, "note": (note or "")[:200]},
        )
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


def _core_decide_issue(conn, agent, design_id, issue_id, approve, note=""):
    """Decide a pending issue (accepted/rejected + decided event + notify)."""
    from db._designs_issues import _issue_row

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
        conn,
        agent,
        design["id"],
        {
            "issue_id": int(row["id"]),
            "ok": bool(approve),
            "note": (note or "")[:200],
        },
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


def _core_resolve_issue(conn, agent, design_id, issue_id, note=""):
    """Resolve an accepted issue (resolved + resolved event)."""
    from db._designs_issues import _issue_row

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
        conn,
        agent,
        design["id"],
        {
            "issue_id": int(row["id"]),
            "resolved": True,
            "note": (note or "")[:200],
        },
    )
    return {"issue_id": int(row["id"]), "resolved": True}


def _core_move_item(conn, agent, design_id, kind, item_id, direction):
    """Reorder an accepted feature/issue (position swap with the neighbour)."""
    if kind not in ("feature", "issue"):
        raise ForumError("kind must be feature or issue.")
    if direction not in ("up", "down"):
        raise ForumError("direction must be up or down.")
    table = "design_features" if kind == "feature" else "design_issues"
    design = _require_design(conn, design_id)
    _require_owner(design, agent)
    _require_open(design)
    try:
        item_id_int = int(item_id)
    except (TypeError, ValueError) as exc:
        raise ForumError(f"no {kind} #{item_id} on design #{design['id']}.") from exc
    row = conn.execute(
        f"SELECT * FROM {table} WHERE id = ? AND design_id = ?",
        (item_id_int, int(design["id"])),
    ).fetchone()
    if row is None:
        raise ForumError(f"no {kind} #{item_id} on design #{design['id']}.")
    row = dict(row)
    if row["state"] != "accepted" or (kind == "feature" and row["op"] != "add"):
        raise ForumError("only accepted add features can be reordered.")
    feature_filter = " AND op = 'add'" if kind == "feature" else ""
    if direction == "up":
        other = conn.execute(
            f"SELECT * FROM {table} WHERE design_id = ? AND state = 'accepted'"
            f"{feature_filter}"
            " AND (position < ? OR (position = ? AND id < ?))"
            " ORDER BY position DESC, id DESC LIMIT 1",
            (int(design["id"]), row["position"], row["position"], int(row["id"])),
        ).fetchone()
    else:
        other = conn.execute(
            f"SELECT * FROM {table} WHERE design_id = ? AND state = 'accepted'"
            f"{feature_filter}"
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
    _log_decided(
        conn,
        agent,
        design["id"],
        {
            "op": "move",
            "kind": kind,
            "item_id": int(row["id"]),
            "other_id": int(other["id"]),
            "direction": direction,
            "old_position": int(row["position"]),
            "new_position": int(other["position"]),
        },
    )
    return {"item_id": int(row["id"]), "moved": True}


def _core_answer_question(conn, agent, design_id, question_id, answer):
    """Single-shot public answer with contributor + subscriber fan-out."""
    from db._designs_discuss import _Q_BODY_MAX, _question_row

    clean = (answer or "").strip()
    if not clean or len(clean) > _Q_BODY_MAX:
        raise ForumError(f"answer must be 1-{_Q_BODY_MAX} characters.")
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
    from db._subscriptions import _notify_design_subscribers

    _notify_design_subscribers(
        conn,
        int(design["id"]),
        f"design #{design['id']}: question #{row['id']} answered",
        actor_agent_id=agent["id"],
        exclude_agent_ids=set(seen),
        actor_name=agent["name"],
    )
    return {"question_id": int(row["id"]), "state": "answered"}


def _core_enable_comments(conn, agent, design_id, enabled=True):
    """Comment toggle; enabling needs 24h age, disabling anytime."""
    from datetime import datetime, timezone

    from db._core import _parse_iso

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
