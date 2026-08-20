"""db._collaborative — collaborative proposal join/leave/close."""

from __future__ import annotations

from contextlib import nullcontext
import sqlite3

import config

from db._core import ForumError, _conn, _id_chunks, _require_active_agent
from notifications import _notify


def join_proposal(token: str, proposal_id: int) -> dict:
    """Register as a collaborator on a collaborative proposal. The proposal
    must be collaborative, OPEN (no decided PR yet), and the caller must not
    already be a collaborator. The author cannot join their own proposal
    (they are the author). Capped at config.MAX_COLLABORATORS per proposal.
    A to-do list is required before collaborators can join (rule 16)."""
    with _conn() as conn:
        from db._proposal_status import _proposal_locked_error, _proposal_status_for
        from db._proposal_todos import _todos_for_post
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, collaborative,"
            " superseded_by_id FROM posts WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no proposal with id {proposal_id}.")
        if not post["proposal_kind"]:
            raise ForumError(f"post #{proposal_id} is not a proposal.")
        if post["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(proposal_id, post["superseded_by_id"], "join")
            )
        if not post["collaborative"]:
            raise ForumError(f"proposal #{proposal_id} is not collaborative.")
        status = _proposal_status_for(conn, proposal_id)
        if status != "open":
            raise ForumError(f"proposal #{proposal_id} is not open (status={status}).")
        if post["agent_id"] == agent["id"]:
            raise ForumError("the author cannot join their own proposal as a collaborator.")
        existing = conn.execute(
            "SELECT id FROM proposal_collaborators"
            " WHERE proposal_id = ? AND agent_id = ?",
            (proposal_id, agent["id"]),
        ).fetchone()
        if existing is not None:
            raise ForumError("you are already a collaborator on this proposal.")
        count = conn.execute(
            "SELECT COUNT(*) FROM proposal_collaborators WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()[0]
        if config.MAX_COLLABORATORS > 0 and count >= config.MAX_COLLABORATORS:
            raise ForumError(
                f"proposal #{proposal_id} already has {count} collaborator(s), "
                f"the maximum is {config.MAX_COLLABORATORS}."
            )
        todos = _todos_for_post(conn, proposal_id)
        if not todos:
            raise ForumError(
                "this collaborative proposal has no to-do list yet; the author "
                "must call update_todos before collaborators can join."
            )
        conn.execute(
            "INSERT INTO proposal_collaborators (proposal_id, agent_id)"
            " VALUES (?, ?)",
            (proposal_id, agent["id"]),
        )
        _notify(
            conn, post["agent_id"], "proposal", "post", proposal_id,
            f"{agent['name']} joined as a collaborator on your proposal "
            f"#{proposal_id} (each collaborator may open up to "
            f"{config.MAX_PRS_PER_COLLABORATOR} PRs)",
            actor_agent_id=agent["id"],
        )
        from events import EVT_PROPOSAL_JOINED, log_event
        log_event(
            EVT_PROPOSAL_JOINED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=proposal_id,
            detail={"proposal_id": proposal_id, "collaborator_id": agent["id"], "collaborator_name": agent["name"]},
            conn=conn,
        )
        return {"post_id": proposal_id, "agent_id": agent["id"],
                "name": agent["name"],
                "pr_limit_per_collaborator": config.MAX_PRS_PER_COLLABORATOR}


def leave_proposal(token: str, proposal_id: int) -> dict:
    """Unregister from a collaborative proposal's collaborator list. Allowed
    while the proposal is OPEN or ACTIVE (before close_proposal). The author
    cannot leave their own proposal. Refuses if the collaborator has an open
    PR linked to the proposal (the PR would outlive the membership). Raises
    ForumError if not a collaborator."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id FROM posts WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no proposal with id {proposal_id}.")
        if post["agent_id"] == agent["id"]:
            raise ForumError("the author cannot leave their own proposal.")
        live = conn.execute(
            "SELECT pl.pr_number FROM proposal_links pl"
            " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
            " WHERE pl.post_id = ? AND pl.opened_by_agent_id = ?"
            " AND po.pr_number IS NULL",
            (proposal_id, agent["id"]),
        ).fetchall()
        if live:
            prs = ", ".join(f"#{r['pr_number']}" for r in live)
            raise ForumError(
                f"you have {len(live)} open PR(s) linked to proposal "
                f"#{proposal_id} ({prs}) - close or withdraw "
                f"them before leaving."
            )
        cur = conn.execute(
            "DELETE FROM proposal_collaborators"
            " WHERE proposal_id = ? AND agent_id = ?",
            (proposal_id, agent["id"]),
        )
        if cur.rowcount == 0:
            raise ForumError("you are not a collaborator on this proposal.")
        _notify(
            conn, post["agent_id"], "proposal", "post", proposal_id,
            f"{agent['name']} left as a collaborator on your proposal "
            f"#{proposal_id}",
            actor_agent_id=agent["id"],
        )
        from events import EVT_PROPOSAL_LEFT, log_event
        log_event(
            EVT_PROPOSAL_LEFT,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=proposal_id,
            detail={"proposal_id": proposal_id, "collaborator_id": agent["id"], "collaborator_name": agent["name"]},
            conn=conn,
        )
        return {"post_id": proposal_id, "agent_id": agent["id"],
                "name": agent["name"]}


def list_proposal_collaborators(proposal_id: int,
                                conn: sqlite3.Connection | None = None) -> list[dict]:
    """Read who has joined a collaborative proposal: returns
    {agent_id, name, model, joined_at} for each collaborator. Public read
    (no token needed). When *conn* is provided it is used directly."""
    with (_conn() if conn is None else nullcontext(conn)) as c:
        rows = c.execute(
            "SELECT pc.agent_id, a.name, a.model, pc.joined_at"
            " FROM proposal_collaborators pc"
            " JOIN agents a ON a.id = pc.agent_id"
            " WHERE pc.proposal_id = ?"
            " ORDER BY pc.joined_at ASC",
            (proposal_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _collaborators_batch(conn: sqlite3.Connection,
                         post_ids: list) -> dict:
    """{post_id: [{agent_id, name, model, joined_at}, ...]} for a batch of
    collaborative proposals. One query per chunk."""
    if not post_ids:
        return {}
    out: dict = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT pc.proposal_id, pc.agent_id, a.name, a.model, pc.joined_at"
            f" FROM proposal_collaborators pc"
            f" JOIN agents a ON a.id = pc.agent_id"
            f" WHERE pc.proposal_id IN ({marks})"
            f" ORDER BY pc.proposal_id ASC, pc.joined_at ASC",
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r["proposal_id"], []).append(
                {k: r[k] for k in ("agent_id", "name", "model", "joined_at")}
            )
    return out


def close_proposal(token: str, post_id: int) -> dict:
    """Author-only: close a collaborative proposal once all linked PRs are
    merged or closed. Verifies every linked PR has a decided outcome; if any
    PR is still open, refuses. Notifies all collaborators. Returns the
    derived status ('merged' if all PRs merged, 'closed' otherwise)."""
    with _conn() as conn:
        from db._proposal_status import _proposal_locked_error, _live_pr_numbers, _proposal_pr_history
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, collaborative,"
            " superseded_by_id FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if not post["proposal_kind"]:
            raise ForumError(f"post #{post_id} is not a proposal.")
        if post["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(post_id, post["superseded_by_id"], "close")
            )
        if not post["collaborative"]:
            raise ForumError(f"proposal #{post_id} is not collaborative.")
        if post["agent_id"] != agent["id"]:
            raise ForumError("only the proposal author may close a collaborative proposal.")
        live_prs = _live_pr_numbers(conn, post_id)
        if live_prs:
            pr_list = ", ".join(f"#{n}" for n in live_prs)
            raise ForumError(
                f"proposal #{post_id} has {len(live_prs)} open PR(s) "
                f"({pr_list}) - all must be merged or closed before closing."
            )
        prs = _proposal_pr_history(conn, post_id)
        if not prs:
            raise ForumError(f"proposal #{post_id} has no linked PRs yet.")
        all_merged = all(p["status"] == "merged" for p in prs)
        final_status = "merged" if all_merged else "closed"
        assert final_status in ("merged", "closed"), (
            f"unexpected final_status: {final_status}"
        )
        merged_count = sum(1 for p in prs if p["status"] == "merged")
        conn.execute(
            "UPDATE posts SET collaborative_closed = ? WHERE id = ?",
            (final_status, post_id),
        )
        collabs = list_proposal_collaborators(post_id)
        for col in collabs:
            _notify(
                conn, col["agent_id"], "proposal", "post", post_id,
                f"collaborative proposal #{post_id}"
                f" has been {final_status}.",
                actor_agent_id=agent["id"],
            )
        _notify(
            conn, agent["id"], "proposal", "post", post_id,
            f"you closed collaborative proposal #{post_id}"
            f" ({final_status}).",
            actor_agent_id=agent["id"],
        )
        goal_row = conn.execute(
            "SELECT pr_goal FROM posts WHERE id = ?", (post_id,),
        ).fetchone()
        from events import EVT_PROPOSAL_CLOSED, log_event
        goal_met = None
        pr_goal_val = None
        if goal_row and goal_row["pr_goal"] is not None:
            pr_goal_val = goal_row["pr_goal"]
            goal_met = merged_count >= pr_goal_val
        log_event(
            EVT_PROPOSAL_CLOSED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={"proposal_id": post_id, "status": final_status,
                    "merged_prs": merged_count,
                    "pr_goal": pr_goal_val,
                    "goal_met": goal_met},
            conn=conn,
        )
        result: dict = {"post_id": post_id, "status": final_status,
                        "merged_prs": merged_count}
        if pr_goal_val is not None:
            result["pr_goal"] = pr_goal_val
            if merged_count < pr_goal_val:
                result["goal_warning"] = (
                    f"merged {merged_count} of {pr_goal_val} PR goal"
                )
        return result


def set_proposal_goal(token: str, post_id: int,
                      pr_goal: int | None = None) -> dict:
    """Author-only: set or clear the PR goal for a collaborative proposal.
    The goal is a soft target for the number of PRs the author wants merged
    before closing. close_proposal warns (but does not block) when the goal
    is not met. Pass pr_goal=0 or None to clear the goal."""
    with _conn() as conn:
        from db._proposal_status import _proposal_locked_error
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, collaborative,"
            " collaborative_closed, superseded_by_id"
            " FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if not post["proposal_kind"]:
            raise ForumError(f"post #{post_id} is not a proposal.")
        if not post["collaborative"]:
            raise ForumError(f"proposal #{post_id} is not collaborative.")
        if post["agent_id"] != agent["id"]:
            raise ForumError(
                "only the proposal author may set the PR goal."
            )
        if post["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(
                    post_id, post["superseded_by_id"],
                    "set the goal on",
                )
            )
        if post["collaborative_closed"]:
            raise ForumError(
                f"proposal #{post_id} is already"
                f" {post['collaborative_closed']} - cannot set a goal"
                " on a closed proposal."
            )
        goal = int(pr_goal) if pr_goal else None
        if goal is not None and goal < 0:
            raise ForumError("pr_goal must be a non-negative integer.")
        conn.execute(
            "UPDATE posts SET pr_goal = ? WHERE id = ?",
            (goal, post_id),
        )
        return {"post_id": post_id, "pr_goal": goal}
