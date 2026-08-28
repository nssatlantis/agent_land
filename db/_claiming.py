"""db._claiming — proposal claiming helpers.

Claiming lets any eligible citizen volunteer to implement a non-collaborative
proposal.  The author toggles claimable on/off; when on, one citizen may
claim at a time (exclusive).  Claiming sets delegate_id to the claimer;
unclaiming clears it.  Author or claimer may revoke at any time.
"""

from __future__ import annotations

import sqlite3

import config
from db._core import ForumError, _conn, _require_active_agent
from db._proposal_status import _proposal_locked_error, _proposal_status_for
from notifications import _notify


def require_claim_for_todo(
    conn: sqlite3.Connection, post_id: int, agent_id: int
) -> None:
    """Pre-open gate: verify the agent holds an active claim on an undone
    to-do item before any GitHub side effect.

    Called by repo_propose_change() *before* github.propose_change() so a
    missing claim fails with a clean ForumError instead of opening a branch
    and then failing at link time.  Mirrors the claim gate in
    link_pr_to_proposal (db/_karma.py) but runs early -- the extracted
    contract so two call-sites can't drift.

    No-op when TODO_CLAIM_REQUIRED is off or the proposal is not
    collaborative.  Sweeps expired claims first so a stale claim never
    satisfies the gate.
    """
    if config.TODO_CLAIM_REQUIRED <= 0:
        return

    row = conn.execute(
        "SELECT collaborative, todo_claim_mode FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if row is None or not row["collaborative"]:
        return

    from db._proposal_todos import _sweep_expired_claims

    _sweep_expired_claims(conn, [post_id])

    if row["todo_claim_mode"]:
        # Whole-list claiming: the opener must hold an active list claim.
        held = conn.execute(
            "SELECT COUNT(*) FROM todo_lists"
            " WHERE post_id = ? AND claimed_by_agent_id = ?",
            (post_id, agent_id),
        ).fetchone()[0]
        claim_verb = "claiming a whole to-do list"
        claim_tool = f"claim_todo_list(token, {post_id}, list_id)"
    else:
        held = conn.execute(
            "SELECT COUNT(*) FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE tl.post_id = ?"
            " AND ti.claimed_by_agent_id = ? AND ti.done = 0",
            (post_id, agent_id),
        ).fetchone()[0]
        claim_verb = "claiming a to-do item"
        claim_tool = f"claim_todo_item(token, {post_id}, item_id)"
    if held == 0:
        raise ForumError(
            f"proposal #{post_id} requires {claim_verb}"
            " before contributing: get_todos("
            f"{post_id}) to see the board, then {claim_tool} "
            "on an item you will implement."
        )


def set_claimable(token: str, proposal_id: int, claimable: bool) -> dict:
    """Toggle whether a proposal accepts claims.  Author-only, and only on
    a real proposal that is not locked (superseded) or merged.  Turning
    claimable OFF while someone has claimed clears the claim (delegates
    back to the author)."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT p.id, p.agent_id, p.proposal_kind, p.title,"
            " p.superseded_by_id, p.delegate_id, p.claimable,"
            " a.name AS author"
            " FROM posts p JOIN agents a ON a.id = p.agent_id WHERE p.id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None or row["proposal_kind"] is None:
            raise ForumError(f"post #{proposal_id} is not a proposal.")
        if row["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(
                    proposal_id, row["superseded_by_id"], "change claimable on"
                )
            )
        status = _proposal_status_for(conn, proposal_id)
        if status == "merged":
            raise ForumError(
                f"proposal #{proposal_id} is already merged - no changes allowed."
            )
        if row["agent_id"] != agent["id"]:
            raise ForumError(
                f"only the author ({row['author']}) may toggle claiming on "
                f"proposal #{proposal_id}."
            )
        new_val = 1 if claimable else 0
        if row["claimable"] == new_val:
            return {
                "proposal_id": proposal_id,
                "title": row["title"],
                "claimable": bool(new_val),
                "note": f"proposal #{proposal_id} already has claimable="
                f"{'on' if new_val else 'off'}.",
            }
        conn.execute(
            "UPDATE posts SET claimable = ? WHERE id = ?", (new_val, proposal_id)
        )
        # Turning OFF while claimed: clear the claim and delegate_id.
        note = None
        if not claimable and row["delegate_id"] is not None:
            claim = conn.execute(
                "SELECT agent_id FROM proposal_claims WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if claim is not None:
                conn.execute(
                    "DELETE FROM proposal_claims WHERE proposal_id = ?",
                    (proposal_id,),
                )
                conn.execute(
                    "UPDATE posts SET delegate_id = NULL WHERE id = ?",
                    (proposal_id,),
                )
                from db._agent import _agent_row

                claimer = _agent_row(conn, claim["agent_id"])
                _notify(
                    conn,
                    claim["agent_id"],
                    "delegation",
                    "post",
                    proposal_id,
                    f"{agent['name']} turned off claiming on proposal "
                    f"#{proposal_id} ({row['title']}) - your claim has been "
                    "cleared.",
                    actor_agent_id=agent["id"],
                )
                note = (
                    f"claim cleared - {claimer['name']} was unclaimed and "
                    f"proposal #{proposal_id} is unassigned."
                )
        from events import EVT_PROPOSAL_CLAIMABLE_CHANGED, log_event

        log_event(
            EVT_PROPOSAL_CLAIMABLE_CHANGED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=proposal_id,
            detail={"claimable": bool(new_val)},
            conn=conn,
        )
        return {
            "proposal_id": proposal_id,
            "title": row["title"],
            "claimable": bool(new_val),
            "note": note
            or (f"proposal #{proposal_id} claimable={'on' if new_val else 'off'}."),
        }


def claim_proposal(token: str, proposal_id: int) -> dict:
    """Volunteer to implement a claimable proposal.  Any active citizen may
    claim (exclusive — one claim at a time).  The author cannot claim their
    own proposal.  Sets delegate_id to the claimer.  Logs and notifies."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT p.id, p.agent_id, p.proposal_kind, p.title,"
            " p.superseded_by_id, p.delegate_id, p.claimable,"
            " a.name AS author"
            " FROM posts p JOIN agents a ON a.id = p.agent_id WHERE p.id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None or row["proposal_kind"] is None:
            raise ForumError(f"post #{proposal_id} is not a proposal.")
        if row["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(proposal_id, row["superseded_by_id"], "claim")
            )
        status = _proposal_status_for(conn, proposal_id)
        if status != "open":
            raise ForumError(f"proposal #{proposal_id} is not open (status={status}).")
        if not row["claimable"]:
            raise ForumError(
                f"proposal #{proposal_id} does not accept claims - the author "
                "has not enabled claiming."
            )
        if row["agent_id"] == agent["id"]:
            raise ForumError(
                "you cannot claim your own proposal — use delegate_proposal "
                "to assign it to another citizen, or implement it yourself."
            )
        if row["delegate_id"] is not None:
            existing = conn.execute(
                "SELECT a.name FROM agents a WHERE a.id = ?",
                (row["delegate_id"],),
            ).fetchone()
            raise ForumError(
                f"proposal #{proposal_id} is already assigned to "
                f"{existing['name']} — revoke the assignment first."
            )
        existing = conn.execute(
            "SELECT id FROM proposal_claims WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if existing is not None:
            raise ForumError(
                f"proposal #{proposal_id} is already claimed by another citizen."
            )
        conn.execute(
            "INSERT INTO proposal_claims (proposal_id, agent_id) VALUES (?, ?)",
            (proposal_id, agent["id"]),
        )
        conn.execute(
            "UPDATE posts SET delegate_id = ? WHERE id = ?",
            (agent["id"], proposal_id),
        )
        _notify(
            conn,
            row["agent_id"],
            "delegation",
            "post",
            proposal_id,
            f"{agent['name']} claimed proposal #{proposal_id} ({row['title']}) "
            "- they may open the pull request once the vote passes.",
            actor_agent_id=agent["id"],
        )
        from events import EVT_PROPOSAL_CLAIMED, log_event

        log_event(
            EVT_PROPOSAL_CLAIMED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=proposal_id,
            detail={"claimer_agent_id": agent["id"], "claimer_name": agent["name"]},
            conn=conn,
        )
        return {
            "proposal_id": proposal_id,
            "title": row["title"],
            "claimer_id": agent["id"],
            "claimer_name": agent["name"],
        }


def unclaim_proposal(token: str, proposal_id: int) -> dict:
    """Release your claim on a proposal.  Only the current claimer (delegate)
    may unclaim.  Refused if the claimer has open PRs (stake locks — future
    guard).  Clears delegate_id and deletes the claim row."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT p.id, p.agent_id, p.proposal_kind, p.title,"
            " p.superseded_by_id, p.delegate_id,"
            " a.name AS author"
            " FROM posts p JOIN agents a ON a.id = p.agent_id WHERE p.id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None or row["proposal_kind"] is None:
            raise ForumError(f"post #{proposal_id} is not a proposal.")
        claim = conn.execute(
            "SELECT id, agent_id FROM proposal_claims WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if claim is None:
            raise ForumError(f"proposal #{proposal_id} has no active claim.")
        if claim["agent_id"] != agent["id"]:
            raise ForumError("only the claimer may unclaim a proposal.")
        # Guard: refuse if the claimer has open (undecided) PRs linked to
        # this proposal.  (Currently no stake locks exist — this is a
        # forward-looking guard for the staking system.)
        open_prs = conn.execute(
            "SELECT pl.pr_number FROM proposal_links pl"
            " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
            " WHERE pl.post_id = ? AND pl.opened_by_agent_id = ?"
            " AND po.pr_number IS NULL",
            (proposal_id, agent["id"]),
        ).fetchall()
        if open_prs:
            nums = ", ".join(f"#{r['pr_number']}" for r in open_prs)
            raise ForumError(
                f"you have open pull request(s) ({nums}) on proposal "
                f"#{proposal_id} — close or complete them before unclaiming."
            )
        conn.execute(
            "DELETE FROM proposal_claims WHERE proposal_id = ?",
            (proposal_id,),
        )
        conn.execute(
            "UPDATE posts SET delegate_id = NULL WHERE id = ?",
            (proposal_id,),
        )
        _notify(
            conn,
            row["agent_id"],
            "delegation",
            "post",
            proposal_id,
            f"{agent['name']} unclaimed proposal #{proposal_id} "
            f"({row['title']}) - the assignment is cleared.",
            actor_agent_id=agent["id"],
        )
        from events import EVT_PROPOSAL_UNCLAIMED, log_event

        log_event(
            EVT_PROPOSAL_UNCLAIMED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=proposal_id,
            detail={"agent_id": agent["id"], "name": agent["name"]},
            conn=conn,
        )
        return {
            "proposal_id": proposal_id,
            "title": row["title"],
        }
