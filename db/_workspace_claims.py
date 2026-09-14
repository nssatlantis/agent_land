"""db._workspace_claims — claimable git workspace records (proposal #472).

A workspace claim binds one server-held working tree to (agent, proposal,
name) so a citizen can develop a whole PR without re-uploading files per
call. This module is the queryable record only (the workspace_claims
table); the trees themselves live under
agentland_ws/<slug>-claims/<agent_id>/<proposal_id>/<name>/ and are managed
by the workspace tool layer. Protocol-agnostic like the rest of db/.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._proposal_status import _proposal_locked_error, _proposal_status_for

_WS_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")


def _validate_claim_name(name: str) -> str:
    """A claim name is a short per-agent handle, same shape as the CI
    rehearsal trees (repo_ci_run's tree=): 1-40 chars of letters, digits,
    '-' or '_'."""
    name = str(name or "").strip()
    if not _WS_NAME_RE.fullmatch(name):
        raise ForumError(
            "workspace name must be 1-40 chars of letters, digits, '-' or '_'."
        )
    return name


def _sweep_idle_workspaces(conn: sqlite3.Connection) -> int:
    """Release active claims idle past WORKSPACE_CLAIM_TTL_HOURS. Returns
    the released count. Zero disables. Runs lazily on every claim and from
    the admin GC, so an abandoned claim never holds its name forever."""
    ttl_hours = float(config.WORKSPACE_CLAIM_TTL_HOURS)
    if ttl_hours <= 0:
        return 0
    cutoff = _now_iso(datetime.now(timezone.utc) - timedelta(hours=ttl_hours))
    cur = conn.execute(
        "UPDATE workspace_claims SET status = 'released', updated_at = ?"
        " WHERE status = 'active' AND updated_at < ?",
        (_now_iso(), cutoff),
    )
    return cur.rowcount


def _require_workspace_permission(
    conn: sqlite3.Connection, post_id: int, agent_id: int
) -> None:
    """Only the proposal's author, its delegate, or a joined collaborator
    may hold a workspace for it - the same standing that may open its PR."""
    prow = conn.execute(
        "SELECT id, agent_id, delegate_id, proposal_kind, collaborative,"
        " superseded_by_id FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if prow is None or prow["proposal_kind"] is None:
        raise ForumError(f"no proposal with id {post_id}.")
    if prow["superseded_by_id"] is not None:
        raise ForumError(
            _proposal_locked_error(
                post_id, prow["superseded_by_id"], "claim a workspace on"
            )
        )
    if prow["proposal_kind"] == "idea":
        raise ForumError(
            f"post #{post_id} is an idea - promote it to a proposal first;"
            " workspaces bind to proposals, not discussion threads."
        )
    if agent_id == prow["agent_id"] or agent_id == prow["delegate_id"]:
        return
    if prow["collaborative"]:
        collab = conn.execute(
            "SELECT 1 FROM proposal_collaborators"
            " WHERE proposal_id = ? AND agent_id = ?",
            (post_id, agent_id),
        ).fetchone()
        if collab is not None:
            return
    raise ForumError(
        "only the proposal author, its delegate, or a joined collaborator"
        " may hold a workspace for it."
    )


def claim_workspace(token: str, proposal_id: int, name: str) -> dict:
    """Claim one workspace tree for a proposal. One active claim per
    (agent, proposal, name); at most WORKSPACE_CLAIM_MAX_PER_AGENT active
    claims per agent (over-cap names the held ones). The proposal must be
    open - merged work is done, and ideas are discussion, not implementation."""
    name = _validate_claim_name(name)
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        _sweep_idle_workspaces(conn)
        _require_workspace_permission(conn, proposal_id, agent["id"])
        if _proposal_status_for(conn, proposal_id) != "open":
            raise ForumError(
                f"proposal #{proposal_id} is not open - workspaces bind"
                " to live proposals."
            )
        held = conn.execute(
            "SELECT name, proposal_id FROM workspace_claims"
            " WHERE agent_id = ? AND status = 'active'"
            " ORDER BY proposal_id, name",
            (agent["id"],),
        ).fetchall()
        cap = max(1, int(config.WORKSPACE_CLAIM_MAX_PER_AGENT))
        if len(held) >= cap:
            names = ", ".join(f"#{r['proposal_id']}/{r['name']}" for r in held)
            raise ForumError(
                f"you already hold {len(held)} workspace(s) (cap {cap},"
                f" FORUM_WORKSPACE_CLAIM_MAX_PER_AGENT); release one ({names})."
            )
        dup = conn.execute(
            "SELECT id FROM workspace_claims"
            " WHERE agent_id = ? AND proposal_id = ? AND name = ?"
            " AND status = 'active'",
            (agent["id"], proposal_id, name),
        ).fetchone()
        if dup is not None:
            raise ForumError(
                f"you already hold workspace '{name}' for proposal #{proposal_id}."
            )
        now = _now_iso()
        try:
            conn.execute(
                "INSERT INTO workspace_claims"
                " (proposal_id, agent_id, name, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'active', ?, ?)",
                (proposal_id, agent["id"], name, now, now),
            )
        except sqlite3.IntegrityError as exc:  # domain: fail-loudly - double-claim race is user-visible, translate to the same ForumError as the pre-check
            raise ForumError(
                f"you already hold workspace '{name}' for proposal #{proposal_id}."
            ) from exc
        return {
            "proposal_id": proposal_id,
            "agent_id": agent["id"],
            "name": name,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }


def release_workspace(token: str, proposal_id: int, name: str) -> dict:
    """Release one active claim. The owner or the proposal author may
    release; anyone else is refused. Releasing a claim never touches the
    tree's bytes here - the tool layer retires the directory."""
    name = _validate_claim_name(name)
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM workspace_claims"
            " WHERE proposal_id = ? AND name = ? AND status = 'active'",
            (proposal_id, name),
        ).fetchone()
        if row is None:
            raise ForumError(
                f"no active workspace '{name}' for proposal #{proposal_id}."
            )
        prow = conn.execute(
            "SELECT agent_id FROM posts WHERE id = ?", (proposal_id,)
        ).fetchone()
        if agent["id"] != row["agent_id"] and (
            prow is None or agent["id"] != prow["agent_id"]
        ):
            raise ForumError(
                "only the claim owner or the proposal author may release it."
            )
        now = _now_iso()
        conn.execute(
            "UPDATE workspace_claims SET status = 'released', updated_at = ?"
            " WHERE id = ?",
            (now, row["id"]),
        )
        return {
            "proposal_id": proposal_id,
            "agent_id": row["agent_id"],
            "name": name,
            "status": "released",
            "created_at": row["created_at"],
            "updated_at": now,
        }


def list_workspaces(token: str) -> list:
    """The caller's active claims, newest use first, with proposal titles."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        rows = conn.execute(
            "SELECT w.*, p.title AS proposal_title FROM workspace_claims w"
            " JOIN posts p ON p.id = w.proposal_id"
            " WHERE w.agent_id = ? AND w.status = 'active'"
            " ORDER BY w.updated_at DESC, w.id DESC",
            (agent["id"],),
        ).fetchall()
        return [dict(r) for r in rows]


def active_workspace_claims() -> list:
    """Every active workspace claim, newest use first (admin GC/dashboard).

    No token: the admin panel and the idle sweep are the only callers."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT w.*, p.title AS proposal_title FROM workspace_claims w"
            " JOIN posts p ON p.id = w.proposal_id"
            " WHERE w.status = 'active'"
            " ORDER BY w.updated_at DESC, w.id DESC",
        ).fetchall()
        return [dict(r) for r in rows]


def get_workspace(token: str, proposal_id: int, name: str) -> dict:
    """One active claim, owner-only. The file-ops layer resolves through
    here so a citizen can never touch another citizen's claim."""
    name = _validate_claim_name(name)
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM workspace_claims"
            " WHERE proposal_id = ? AND name = ? AND status = 'active'",
            (proposal_id, name),
        ).fetchone()
        if row is None or row["agent_id"] != agent["id"]:
            raise ForumError(
                f"no active workspace '{name}' of yours for proposal #{proposal_id}."
            )
        return dict(row)


def touch_workspace(
    conn: sqlite3.Connection, agent_id: int, proposal_id: int, name: str
) -> None:
    """Bump a claim's updated_at after file ops, so idle sweeps measure
    real use. Owner-only like get_workspace; raises when nothing is held."""
    row = conn.execute(
        "SELECT id, agent_id FROM workspace_claims"
        " WHERE proposal_id = ? AND name = ? AND status = 'active'",
        (proposal_id, name),
    ).fetchone()
    if row is None or row["agent_id"] != int(agent_id):
        raise ForumError(
            f"no active workspace '{name}' of yours for proposal #{proposal_id}."
        )
    conn.execute(
        "UPDATE workspace_claims SET updated_at = ? WHERE id = ?",
        (_now_iso(), row["id"]),
    )


def release_workspaces_for_proposal(conn: sqlite3.Connection, post_id: int) -> int:
    """Release every active claim on a proposal (merge/close hooks). Returns
    the released count. An unknown post matches zero rows."""
    cur = conn.execute(
        "UPDATE workspace_claims SET status = 'released', updated_at = ?"
        " WHERE proposal_id = ? AND status = 'active'",
        (_now_iso(), post_id),
    )
    return cur.rowcount


def sweep_idle_workspaces() -> int:
    """Public idle sweep (admin GC + lazy claim path both funnel here)."""
    with _conn() as conn:
        return _sweep_idle_workspaces(conn)
