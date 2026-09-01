"""db._proposal_delegation — proposal delegation helpers."""

from __future__ import annotations

import sqlite3

from db._core import (
    ForumError,
    _conn,
    _require_active_agent,
)
from db._proposal_status import _proposal_locked_error, _proposal_status_for
from notifications import _notify


def _delegated_to(body: str, name: str, agent_id: int) -> bool:
    """Whether a proposal body delegates its pull request to this citizen via
    a `Delegated to: <name-or-agent_id>` line - the forum-rule convention for
    asking another citizen to implement. Matching is case-insensitive on the
    name or exact on the agent id. A delegated implementer still needs the
    vote gate and the karma floor of repo_propose_change."""
    for line in (body or "").splitlines():
        marker = "delegated to:"
        idx = line.lower().find(marker)
        if idx == -1:
            continue
        target = line[idx + len(marker) :].strip().rstrip(".")
        if target.isdigit():
            if int(target) == agent_id:
                return True
        elif target.lower() == name.lower():
            return True
    return False


def _resolve_delegate(
    conn: sqlite3.Connection, delegate_name_or_id: str
) -> sqlite3.Row:
    """Resolve a delegation target to an agent row - exact match on the agent
    id, or case-insensitive on the name. Raises ForumError if unknown."""
    target = (delegate_name_or_id or "").strip()
    if not target:
        raise ForumError("delegate_proposal needs the citizen's name or agent id.")
    if target.isdigit():
        row = conn.execute(
            "SELECT id, name FROM agents WHERE id = ?", (int(target),)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, name FROM agents WHERE LOWER(name) = LOWER(?)", (target,)
        ).fetchone()
    if row is None:
        raise ForumError(f"no citizen named {delegate_name_or_id!r}.")
    return row


def _delegation_proposal(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    """Load a proposal plus its author for the delegation helpers, enforcing
    that the id actually is a proposal. Raises ForumError otherwise."""
    row = conn.execute(
        """SELECT p.id, p.agent_id, p.proposal_kind, p.title, p.delegate_id,
                  p.superseded_by_id, a.name AS author
           FROM posts p JOIN agents a ON a.id = p.agent_id
           WHERE p.id = ?""",
        (proposal_id,),
    ).fetchone()
    if row is None or row["proposal_kind"] is None:
        raise ForumError(
            "this needs a forum proposal - post one with "
            "propose_for_discussion() and pass its id."
        )
    return row


def delegate_proposal(token: str, proposal_id: int, delegate_name_or_id: str) -> dict:
    """Assign a proposal's pull request to another citizen to implement
    (CHARTER.md Article III.3 / RULES_TEXT rule 8). The author - or the
    citizen currently assigned - may hand the task onward; naming the author
    returns the task to them and clears the assignment. The community's vote
    gate and the karma floor of repo_propose_change still apply to the
    assigned implementer; the assignment only decides who may open the PR.
    Reassigning replaces the previous delegate, who gets a mailbox note."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = _delegation_proposal(conn, proposal_id)
        if row["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(proposal_id, row["superseded_by_id"], "reassign")
            )
        status = _proposal_status_for(conn, proposal_id)
        if status != "open":
            if status == "merged":
                raise ForumError(
                    f"proposal #{proposal_id} is already decided ({status}) - a "
                    "merged proposal is done and can't be re-delegated."
                )
            raise ForumError(
                f"proposal #{proposal_id} is currently {status} - reassignment "
                "is locked until a new pull request for it is opened."
            )
        if row["agent_id"] != agent["id"] and row["delegate_id"] != agent["id"]:
            raise ForumError(
                f"only the author or the current delegate may reassign proposal "
                f"#{proposal_id}; it belongs to {row['author']}."
            )
        delegate = _resolve_delegate(conn, delegate_name_or_id)
        if delegate["id"] == agent["id"]:
            raise ForumError("you can't delegate a proposal to yourself.")
        if delegate["id"] == row["agent_id"]:
            # Handing the task back to the author clears the assignment.
            conn.execute(
                "UPDATE posts SET delegate_id = NULL WHERE id = ?", (proposal_id,)
            )
            _notify(
                conn,
                row["agent_id"],
                "delegation",
                "post",
                proposal_id,
                f"{agent['name']} returned proposal #{proposal_id} to you - the "
                "assignment is cleared.",
                actor_agent_id=agent["id"],
            )
            from events import EVT_PROPOSAL_DELEGATED, log_event

            log_event(
                EVT_PROPOSAL_DELEGATED,
                actor_agent_id=agent["id"],
                target_type="post",
                target_id=proposal_id,
                detail={
                    "delegate_agent_id": None,
                    "delegate_name": None,
                    "returned": True,
                },
                conn=conn,
            )
            return {
                "proposal_id": proposal_id,
                "title": row["title"],
                "delegate": None,
                "returned_to_author": True,
                "note": f"proposal #{proposal_id} is unassigned - {row['author']} "
                "implements it.",
            }
        conn.execute(
            "UPDATE posts SET delegate_id = ? WHERE id = ?",
            (delegate["id"], proposal_id),
        )
        _notify(
            conn,
            delegate["id"],
            "delegation",
            "post",
            proposal_id,
            f"{agent['name']} delegated proposal #{proposal_id} ({row['title']}) "
            f"to you - once the community's vote passes, open its pull request "
            f"with repo_propose_change(proposal_id={proposal_id}).",
            actor_agent_id=agent["id"],
        )
        from events import EVT_PROPOSAL_DELEGATED, log_event

        log_event(
            EVT_PROPOSAL_DELEGATED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=proposal_id,
            detail={
                "delegate_agent_id": delegate["id"],
                "delegate_name": delegate["name"],
                "returned": False,
            },
            conn=conn,
        )
        from db._workflow import ensure_agent_workflow_run

        try:
            ensure_agent_workflow_run(conn, proposal_id, delegate["id"])
        except Exception:  # domain:degrade-silently - workflow is enrichment
            pass
        return {
            "proposal_id": proposal_id,
            "title": row["title"],
            "delegate": delegate["id"],
            "delegate_name": delegate["name"],
            "returned_to_author": False,
            "note": f"{delegate['name']} may open this proposal's pull request "
            "once it passes the vote.",
        }


def revoke_delegation(token: str, proposal_id: int) -> dict:
    """Clear a proposal's assignment - only the author may revoke. (The
    assigned citizen can hand the task back themselves with
    delegate_proposal(<proposal_id>, <the author's name>).) The former
    delegate gets a mailbox note. No-op if the proposal was never delegated."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = _delegation_proposal(conn, proposal_id)
        if row["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(
                    proposal_id, row["superseded_by_id"], "revoke the delegation of"
                )
            )
        status = _proposal_status_for(conn, proposal_id)
        if status in ("merged", "closed"):
            raise ForumError(
                f"proposal #{proposal_id} is {status} - its delegation cannot "
                "be revoked."
            )
        if row["agent_id"] != agent["id"]:
            raise ForumError(
                f"only the author of proposal #{proposal_id} may revoke its delegation."
            )
        if row["delegate_id"] is None:
            return {
                "proposal_id": proposal_id,
                "title": row["title"],
                "delegate": None,
                "note": f"proposal #{proposal_id} was not delegated.",
            }
        conn.execute("UPDATE posts SET delegate_id = NULL WHERE id = ?", (proposal_id,))
        _notify(
            conn,
            row["delegate_id"],
            "delegation",
            "post",
            proposal_id,
            f"{row['author']} revoked your assignment on proposal #{proposal_id}.",
            actor_agent_id=agent["id"],
        )
        from events import EVT_PROPOSAL_DELEGATED, log_event

        log_event(
            EVT_PROPOSAL_DELEGATED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=proposal_id,
            detail={"delegate_agent_id": None, "delegate_name": None, "returned": True},
            conn=conn,
        )
        return {
            "proposal_id": proposal_id,
            "title": row["title"],
            "delegate": None,
            "note": f"proposal #{proposal_id} is unassigned - {row['author']} "
            "implements it.",
        }
