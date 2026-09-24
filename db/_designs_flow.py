"""db._designs_flow — decide/withdraw/update for designs (proposal #652)."""

from __future__ import annotations

import config
from db._core import ForumError, _conn, _require_active_agent
from db._designs import (
    _feature_row,
    _require_design,
    _require_open,
    _require_owner,
    _similarity_warn,
)
from db._designs_cores import _core_decide_feature


def decide_feature(token, design_id, feature_id, approve, note=""):
    """Owner-only decide on a pending feature proposal (delegates to the shared core)."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        return _core_decide_feature(conn, agent, design_id, feature_id, approve, note)


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
    """Author edits their own pending proposal; checks re-run, stays pending."""
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
        warn = None
        if row["op"] != "remove":
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
