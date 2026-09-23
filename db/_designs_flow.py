"""db._designs_flow — decide/withdraw/update for designs (proposal #652)."""

from __future__ import annotations

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._designs import (
    _feature_row,
    _log_decided,
    _next_position,
    _notify_author,
    _require_design,
    _require_open,
    _require_owner,
    _similarity_warn,
    _typo_pass,
)


def decide_feature(token, design_id, feature_id, approve, note=""):
    """Owner-only decide on a pending feature proposal."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
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


def withdraw_feature(token, design_id, feature_id):
    """Withdraw your own (or owner's) pending proposal."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_open(design)
        row = _feature_row(conn, feature_id, design["id"])
        if row["state"] != "pending":
            raise ForumError("only pending proposals can be withdrawn.")
        mine = int(row["author_id"] or 0) == int(agent["id"])
        if not mine:
            _require_owner(design, agent)
        conn.execute("DELETE FROM design_features WHERE id = ?", (int(row["id"]),))
        return {"feature_id": int(row["id"]), "withdrawn": True}


def update_pending_feature(token, design_id, feature_id, text=None, reason=None):
    """Author edits their own pending proposal; checks re-run."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_open(design)
        row = _feature_row(conn, feature_id, design["id"])
        if row["state"] != "pending":
            raise ForumError("only pending proposals can be edited.")
        if int(row["author_id"] or 0) != int(agent["id"]):
            raise ForumError("only the author may edit their pending proposal.")
        new_text = (text if text is not None else row["text"]).strip()
        if not new_text or len(new_text) > 2000:
            raise ForumError("feature text must be 1-2000 characters.")
        new_reason = (reason if reason is not None else row["reason"]).strip()
        if row["op"] == "edit":
            target = _feature_row(conn, row["target_feature_id"], design["id"])
            if _typo_pass(target["text"], new_text):
                conn.execute(
                    "UPDATE design_features SET text = ? WHERE id = ?",
                    (new_text, int(target["id"])),
                )
                conn.execute(
                    "DELETE FROM design_features WHERE id = ?",
                    (int(row["id"]),),
                )
                return {
                    "feature_id": int(target["id"]),
                    "state": "accepted",
                    "auto": True,
                }
        warn = _similarity_warn(conn, design["id"], new_text, agent["id"])
        if warn is not None:
            same = warn["feature_id"] == int(row["id"])
            if not same and len(new_reason) < int(config.DESIGN_SIMILAR_REASON_MIN):
                raise ForumError("still similar - include a reason (>=20 chars).")
        conn.execute(
            "UPDATE design_features SET text = ?, reason = ?, similarity = ?"
            " WHERE id = ?",
            (new_text, new_reason, warn["score"] if warn else None, int(row["id"])),
        )
        return {"feature_id": int(row["id"]), "state": "pending", "warning": warn}
