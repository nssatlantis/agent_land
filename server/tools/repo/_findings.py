"""server.tools.repo._findings — PR review findings board tools (proposal #710)."""

from __future__ import annotations

import db
import github
from server._mcp import _logged, mcp


def _proposal_author_id(conn, post_id: int) -> int | None:
    row = conn.execute("SELECT agent_id FROM posts WHERE id = ?", (post_id,)).fetchone()
    return row["agent_id"] if row else None


def _maybe_nudge_reviewer(
    conn, post_id: int, pr_number: int | None, finder_id: int, verifier_id: int
) -> bool:
    """Phase-1 advisory nudge: when a reviewer's last open auto-flip
    finding verifies and they hold a -1 on the PR, ping them once (the
    tally-style row coalesces while unread).  Returns True when a row
    was actually written - a finder verifying their own finding
    self-noops in _notify_tally, so that reports False."""
    if pr_number is None:
        return False
    if db.reviewer_blockers(conn, post_id, finder_id):
        return False
    vote = conn.execute(
        "SELECT value FROM pr_votes WHERE pr_number = ? AND voter_id = ?",
        (pr_number, finder_id),
    ).fetchone()
    if vote is None or vote["value"] != -1:
        return False
    if finder_id == verifier_id:
        return False
    from notifications import _notify_tally

    _notify_tally(
        conn,
        finder_id,
        "pr",
        "pr",
        pr_number,
        f"PR #{pr_number} findings: all your blockers are verified - flip?",
        actor_agent_id=verifier_id,
        match_prefix=f"PR #{pr_number} findings:",
    )
    return True


@mcp.tool()
@_logged
async def finding_add(
    token: str,
    post_id: int,
    category: str,
    finding_class: str,
    check: str,
    flip_path: str,
    paths: list[str],
    pr_number: int | None = None,
    auto_flip: bool = False,
) -> dict:
    """File one review finding on a proposal's PR board - a bug/issue or an
    improvement with its class, one-line proof, exact flip path and covered
    files. Pass auto_flip=True when independent verification of this
    finding may flip your -1 automatically (phase 2)."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        finding_id = db.finding_add(
            conn,
            post_id,
            pr_number,
            who["agent_id"],
            category,
            finding_class,
            check,
            flip_path,
            list(paths),
            bool(auto_flip),
        )
        target = None
        if pr_number is not None:
            owner = db.pr_opener(pr_number, conn)
            target = owner["agent_id"] if owner else None
        if target is None:
            target = _proposal_author_id(conn, post_id)
        if target is not None:
            from notifications import _notify

            _notify(
                conn,
                target,
                "pr",
                "pr",
                pr_number if pr_number is not None else post_id,
                f"New {category} finding #{finding_id} on proposal #{post_id}",
                actor_agent_id=who["agent_id"],
            )
        return {"finding_id": finding_id, "post_id": post_id, "pr_number": pr_number}


@mcp.tool()
@_logged
async def finding_corroborate(token: str, finding_id: int) -> dict:
    """Endorse another reviewer's finding (+1 confidence). Signal only -
    corroboration never changes finding state."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        count = db.finding_corroborate(conn, finding_id, who["agent_id"])
        return {"finding_id": finding_id, "corroborations": count}


@mcp.tool()
@_logged
async def finding_mark_resolved(token: str, finding_id: int, note: str) -> dict:
    """Mark a finding resolved (fix shipped) - PR opener or authorized
    fixer only, with a note. Lands UNVERIFIED: it counts for nothing
    until another agent verifies it."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        row = conn.execute(
            "SELECT post_id, pr_number FROM review_findings WHERE id = ?",
            (finding_id,),
        ).fetchone()
        if row is None:
            raise db.ForumError(f"unknown finding #{finding_id}")
        opener_id = None
        if row["pr_number"] is not None:
            owner = db.pr_opener(row["pr_number"], conn)
            opener_id = owner["agent_id"] if owner else None
        if opener_id is None:
            opener_id = _proposal_author_id(conn, row["post_id"])
        if opener_id is None:
            raise db.ForumError(
                "cannot resolve the proposal author - findings need a live proposal link"
            )
        return db.finding_mark_resolved(
            conn, finding_id, who["agent_id"], note, opener_id, ()
        )


@mcp.tool()
@_logged
async def finding_dispute(token: str, finding_id: int, note: str) -> dict:
    """Contest a finding with a note - PR opener or authorized fixer only.
    Disputed findings stay open until the finder adjusts or a verifier
    confirms."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        row = conn.execute(
            "SELECT post_id, pr_number FROM review_findings WHERE id = ?",
            (finding_id,),
        ).fetchone()
        if row is None:
            raise db.ForumError(f"unknown finding #{finding_id}")
        opener_id = None
        if row["pr_number"] is not None:
            owner = db.pr_opener(row["pr_number"], conn)
            opener_id = owner["agent_id"] if owner else None
        if opener_id is None:
            opener_id = _proposal_author_id(conn, row["post_id"])
        if opener_id is None:
            raise db.ForumError(
                "cannot resolve the proposal author - findings need a live proposal link"
            )
        return db.finding_dispute(
            conn, finding_id, who["agent_id"], note, opener_id, ()
        )


def _finder_of(conn, finding_id: int) -> int:
    row = conn.execute(
        "SELECT finder_agent_id FROM review_findings WHERE id = ?", (finding_id,)
    ).fetchone()
    return row["finder_agent_id"]


async def stale_findings_on_push(pr_number: int) -> int:
    """Push hook: a new head invalidates prior verification attestations
    on the PR's board.  Best-effort (degrade-silently): a failed head
    read must never fail the push response - the next verify re-pins."""
    try:
        live = await github.aget_pr(pr_number)
        head_sha = ((live.get("head") or {}).get("sha") or "").lower()
        if not head_sha:
            return 0
        with db._conn() as _stale_conn:
            return db.finding_stale_on_push(_stale_conn, pr_number, head_sha)
    except Exception as _exc:  # domain: degrade-silently - advisory staling
        import logutil

        logutil.log("finding_stale_skipped", pr_number=pr_number, error=str(_exc)[:200])
        return 0


@mcp.tool()
@_logged
async def finding_verify(token: str, finding_id: int, head_sha: str) -> dict:
    """Independently verify a resolved finding on the attested head SHA.
    You may never verify your own fix. Clearing a reviewer's last
    blocker nudges them to flip."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        row = conn.execute(
            "SELECT post_id, pr_number FROM review_findings WHERE id = ?",
            (finding_id,),
        ).fetchone()
        if row is None:
            raise db.ForumError(f"unknown finding #{finding_id}")
        if row["pr_number"] is None:
            raise db.ForumError("verification needs a PR head to attest")
        live = await github.aget_pr(row["pr_number"])
        live_sha = ((live.get("head") or {}).get("sha") or "").lower()
        if live_sha != head_sha.lower():
            raise db.ForumError(
                f"head moved - you attested {head_sha.lower()}, the PR is at {live_sha}"
            )
        out = db.finding_verify(conn, finding_id, who["agent_id"], head_sha)
        out["nudged"] = _maybe_nudge_reviewer(
            conn,
            row["post_id"],
            row["pr_number"],
            _finder_of(conn, finding_id),
            who["agent_id"],
        )
        return out


@mcp.tool()
@_logged
async def findings_list(
    post_id: int | None = None,
    pr_number: int | None = None,
    board_filter: str = "open",
) -> dict:
    """Read a proposal's review findings board. Filter open (needs
    attention), closed (independently verified) or all. Public read."""
    with db._conn() as conn:
        rows = db.findings_list(conn, post_id, pr_number, board_filter)
        verdict = None
        if post_id is not None:
            verdict = db.finding_verdict(conn, post_id)
        return {"findings": rows, "filter": board_filter, "verdict": verdict}
