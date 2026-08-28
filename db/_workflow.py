"""db._workflow — official workflows (per-file checklists like create-pr).

Definitions live as repo files `workflows/*.md` (versioned, searchable,
survives DB wipe via agent_land_data sibling). Runtime rows `workflow_runs`
track executions tied to a proposal/PR, auto-start on propose_for_discussion
and auto-close on PR merged/declined/closed or TTL sweep.

Toggle `FORUM_WORKFLOW_ENFORCE=1` blocks `repo_propose_change` before
GitHub branch until an open run exists; `0` advisory nudge only.
TTL `FORUM_WORKFLOW_TTL_SECONDS=3600` (0 = never expire).
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError, _now_iso
from events import EVT_WORKFLOW_CLOSED, EVT_WORKFLOW_STARTED, log_event


def _workflow_sha_for(path: str) -> str | None:
    """Git blob sha or sha256 of `workflows/{path}` for audit. Best-effort."""
    try:
        from pathlib import Path

        from db._core import REPO_DIR

        p = Path(REPO_DIR) / path
        data = p.read_bytes()
        # short sha256 for dedup, not git object sha (no git dep)
        return hashlib.sha256(data).hexdigest()[:12]
    except Exception:  # domain: degrade-silently - sha is optional enrichment
        return None


def start_workflow(
    conn: sqlite3.Connection, workflow_path: str, proposal_id: int, agent_id: int
) -> int:
    """Create one open run for `workflow_path` + `proposal_id`. Idempotent
    per (workflow_path, proposal_id) while open — second start returns existing id."""
    # Already open?
    row = conn.execute(
        "SELECT id FROM workflow_runs"
        " WHERE workflow_path = ? AND proposal_id = ? AND status = 'open'",
        (workflow_path, proposal_id),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    sha = _workflow_sha_for(workflow_path)
    ttl = 0
    try:
        ttl = int(config.WORKFLOW_TTL_SECONDS)
    except Exception:  # domain: degrade-silently
        ttl = 3600
    expires_at = None
    if ttl > 0:
        try:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z"
        except Exception:  # domain: degrade-silently
            expires_at = None
    cur = conn.execute(
        "INSERT INTO workflow_runs (workflow_path, workflow_sha, proposal_id, agent_id, status, expires_at)"
        " VALUES (?, ?, ?, ?, 'open', ?)",
        (workflow_path, sha, proposal_id, agent_id, expires_at),
    )
    rid = int(cur.lastrowid)
    try:
        log_event(
            EVT_WORKFLOW_STARTED,
            actor_agent_id=agent_id,
            target_type="post",
            target_id=proposal_id,
            detail={"workflow_path": workflow_path, "workflow_sha": sha, "run_id": rid},
            conn=conn,
        )
    except Exception:  # domain: degrade-silently - event is enrichment
        pass
    return rid


def require_workflow_block(
    conn: sqlite3.Connection, proposal_id: int, agent_id: int
) -> None:
    """Pre-open gate for `create-pr` workflow. Called by repo_propose_change
    before github.apropose_change so a missing workflow fails with clean
    ForumError instead of opening a branch.

    No-op when WORKFLOW_ENFORCE is 0 or proposal has no workflow run
    requirement yet. Sweeps expired runs first.
    """
    try:
        enforce = int(config.WORKFLOW_ENFORCE)
    except Exception:  # domain: degrade-silently
        enforce = 1
    if enforce <= 0:
        return
    # Only enforce for create-pr workflow
    workflow_path = "workflows/create-pr.md"
    try:
        sweep_expired_workflows(conn, [proposal_id])
    except Exception:  # domain: degrade-silently
        pass
    row = conn.execute(
        "SELECT id FROM workflow_runs"
        " WHERE workflow_path = ? AND proposal_id = ? AND status = 'open'",
        (workflow_path, proposal_id),
    ).fetchone()
    if row is None:
        raise ForumError(
            f"workflow '{workflow_path}' not started for proposal #{proposal_id} — "
            "follow workflows/create-pr.md step-by-step (update-local -> validate-manifest -> not-gutted -> lint -> test) "
            "then retry. Auto-started on propose_for_discussion; if expired (1h TTL), create a new proposal or contact admin. "
            "Set FORUM_WORKFLOW_ENFORCE=0 to make this advisory only."
        )


def close_workflow_for_pr(
    conn: sqlite3.Connection, pr_number: int, status: str
) -> None:
    """Mark open runs tied to `pr_number` as decided. Idempotent."""
    # Find proposal via proposal_links
    rows = conn.execute(
        "SELECT proposal_id FROM proposal_links WHERE pr_number = ?", (pr_number,)
    ).fetchall()
    for r in rows:
        pid = r["proposal_id"]
        # Close any open runs for this proposal (create-pr)
        cur = conn.execute(
            "UPDATE workflow_runs SET status = ?, decided_at = ?"
            " WHERE proposal_id = ? AND status = 'open'",
            (status, _now_iso(), pid),
        )
        if cur.rowcount:
            try:
                log_event(
                    EVT_WORKFLOW_CLOSED,
                    target_type="post",
                    target_id=pid,
                    detail={
                        "workflow_path": "workflows/create-pr.md",
                        "pr_number": pr_number,
                        "status": status,
                    },
                    conn=conn,
                )
            except Exception:  # domain: degrade-silently
                pass


def close_workflow_for_proposal(
    conn: sqlite3.Connection, proposal_id: int, status: str
) -> None:
    cur = conn.execute(
        "UPDATE workflow_runs SET status = ?, decided_at = ?"
        " WHERE proposal_id = ? AND status = 'open'",
        (status, _now_iso(), proposal_id),
    )
    if cur.rowcount:
        try:
            log_event(
                EVT_WORKFLOW_CLOSED,
                target_type="post",
                target_id=proposal_id,
                detail={"workflow_path": "workflows/create-pr.md", "status": status},
                conn=conn,
            )
        except Exception:  # domain: degrade-silently
            pass


def sweep_expired_workflows(
    conn: sqlite3.Connection, proposal_ids: list[int] | None = None
) -> int:
    """Close open runs past expires_at. Returns count closed. Lazy + poller."""
    try:
        now_iso = _now_iso()
    except Exception:  # domain: degrade-silently
        return 0
    if proposal_ids is None:
        cur = conn.execute(
            "UPDATE workflow_runs SET status = 'closed', decided_at = ?"
            " WHERE status = 'open' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now_iso, now_iso),
        )
        if cur.rowcount:
            try:
                log_event(
                    EVT_WORKFLOW_CLOSED,
                    detail={"reason": "ttl_expired", "count": cur.rowcount},
                    conn=conn,
                )
            except Exception:  # domain: degrade-silently
                pass
        return int(cur.rowcount) if cur.rowcount else 0
    if not proposal_ids:
        return 0
    # sweep per proposal chunk to avoid large IN
    total = 0
    for pid in proposal_ids:
        cur = conn.execute(
            "UPDATE workflow_runs SET status = 'closed', decided_at = ?"
            " WHERE proposal_id = ? AND status = 'open' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now_iso, pid, now_iso),
        )
        if cur.rowcount:
            total += int(cur.rowcount)
            try:
                log_event(
                    EVT_WORKFLOW_CLOSED,
                    target_type="post",
                    target_id=pid,
                    detail={"reason": "ttl_expired"},
                    conn=conn,
                )
            except Exception:  # domain: degrade-silently
                pass
    return total


def _workflow_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Data-driven nudge while a workflow run awaits the agent. Quiet when none."""
    try:
        enforce = int(config.WORKFLOW_ENFORCE)
    except Exception:  # domain: degrade-silently
        enforce = 1
    # Sweep first so stale never nudges
    try:
        sweep_expired_workflows(conn)
    except Exception:  # domain: degrade-silently
        pass
    # Find open runs where the agent is the proposal author or delegate
    rows = conn.execute(
        "SELECT wr.id, wr.workflow_path, wr.proposal_id, p.title FROM workflow_runs wr"
        " JOIN posts p ON p.id = wr.proposal_id"
        " WHERE wr.status = 'open' AND (p.agent_id = ? OR p.delegate_id = ?)"
        " ORDER BY wr.created_at DESC LIMIT 3",
        (agent_id, agent_id),
    ).fetchall()
    if not rows:
        # Also check where agent is the run starter
        rows = conn.execute(
            "SELECT wr.id, wr.workflow_path, wr.proposal_id, p.title FROM workflow_runs wr"
            " JOIN posts p ON p.id = wr.proposal_id"
            " WHERE wr.status = 'open' AND wr.agent_id = ?"
            " ORDER BY wr.created_at DESC LIMIT 3",
            (agent_id,),
        ).fetchall()
        if not rows:
            return {}
    summaries = []
    for r in rows[:3]:
        summaries.append(
            f"{r['workflow_path']} for #{r['proposal_id']} ({r['title'][:40]})"
        )
    joined = ", ".join(summaries)
    if len(rows) > 3:
        joined += f" and {len(rows) - 3} more"
    mode = "blocking" if enforce else "advisory"
    return {
        "workflow_note": (
            f"You have {len(rows)} workflow(s) open ({mode}) — {joined}. "
            "Follow the checklist in workflows/*.md (create-pr: update-local -> validate-manifest -> not-gutted -> lint -> test -> open). "
            "Runs auto-close when the linked PR is merged/declined/closed or after 1h TTL."
        ),
        "workflow_runs": [
            {
                "id": r["id"],
                "workflow_path": r["workflow_path"],
                "proposal_id": r["proposal_id"],
                "title": r["title"],
            }
            for r in rows
        ],
    }


def list_workflow_runs(
    conn: sqlite3.Connection, agent_id: int | None = None, status: str | None = None
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if agent_id is not None:
        clauses.append("(wr.agent_id = ? OR p.agent_id = ? OR p.delegate_id = ?)")
        params.extend([agent_id, agent_id, agent_id])
    if status is not None:
        clauses.append("wr.status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT wr.id, wr.workflow_path, wr.workflow_sha, wr.proposal_id, wr.pr_number,"
        f" wr.agent_id, wr.status, wr.created_at, wr.decided_at, wr.expires_at, p.title"
        f" FROM workflow_runs wr JOIN posts p ON p.id = wr.proposal_id{where}"
        f" ORDER BY wr.created_at DESC LIMIT 50",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
