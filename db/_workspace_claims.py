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
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps

import config
from db._core import ForumError, _conn, _id_chunks, _now_iso, _require_active_agent
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


_LIFECYCLE_LOCKS = threading.local()


def _claim_tree_lock(agent_id: int, proposal_id: int, name: str):
    from github._workspaces import _claim_dir, workspace_lock

    return workspace_lock(
        _claim_dir(int(agent_id), int(proposal_id), str(name)), allow_missing=True
    )


def _claim_key(row) -> tuple[int, int, str]:
    return (
        int(row["agent_id"]),
        int(row["proposal_id"]),
        str(row["name"]),
    )


@contextmanager
def _workspace_claim_locks(post_id: int | None):
    if post_id is None:
        yield
        return
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, agent_id, proposal_id, name FROM workspace_claims"
            " WHERE proposal_id = ? AND status = 'active' ORDER BY id",
            (int(post_id),),
        ).fetchall()
    locked = set()
    with ExitStack() as stack:
        for row in rows:
            stack.enter_context(
                _claim_tree_lock(row["agent_id"], row["proposal_id"], row["name"])
            )
            locked.add(_claim_key(row))
        previous: set[tuple[int, int, str]] = getattr(_LIFECYCLE_LOCKS, "keys", set())
        _LIFECYCLE_LOCKS.keys = locked
        try:
            yield
        finally:
            _LIFECYCLE_LOCKS.keys = previous


def _with_workspace_claim_locks(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        post_id = kwargs.get("post_id")
        if post_id is None and len(args) > 1:
            post_id = args[1]
        with _workspace_claim_locks(post_id):
            return func(*args, **kwargs)

    return wrapped


def _release_claim_row(conn: sqlite3.Connection, row) -> int:
    cur = conn.execute(
        "UPDATE workspace_claims SET status = 'released', updated_at = ?"
        " WHERE id = ? AND status = 'active'",
        (_now_iso(), row["id"]),
    )
    return cur.rowcount


def _sweep_idle_workspaces(conn: sqlite3.Connection) -> int:
    """Release active claims idle past WORKSPACE_CLAIM_TTL_HOURS. Returns
    the released count. Zero disables. Runs lazily on every claim and from
    the admin GC, so an abandoned claim never holds its name forever."""
    ttl_hours = float(config.WORKSPACE_CLAIM_TTL_HOURS)
    if ttl_hours <= 0:
        return 0
    cutoff = _now_iso(datetime.now(timezone.utc) - timedelta(hours=ttl_hours))
    rows = conn.execute(
        "SELECT id, agent_id, proposal_id, name FROM workspace_claims"
        " WHERE status = 'active' AND updated_at < ? ORDER BY id",
        (cutoff,),
    ).fetchall()
    released = 0
    for row in rows:
        try:
            with _claim_tree_lock(row["agent_id"], row["proposal_id"], row["name"]):
                cur = conn.execute(
                    "UPDATE workspace_claims SET status = 'released', updated_at = ?"
                    " WHERE id = ? AND status = 'active' AND updated_at < ?",
                    (_now_iso(), row["id"], cutoff),
                )
        except Exception:
            continue
        released += cur.rowcount
    return released


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
            cur = conn.execute(
                "INSERT INTO workspace_claims"
                " (proposal_id, agent_id, name, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'active', ?, ?)",
                (proposal_id, agent["id"], name, now, now),
            )
            claim_id = cur.lastrowid
            if claim_id is None:
                raise ForumError("workspace claim insert returned no id.")
            claim_id = int(claim_id)
        except sqlite3.IntegrityError as exc:  # domain: fail-loudly - double-claim race is user-visible, translate to the same ForumError as the pre-check
            raise ForumError(
                f"you already hold workspace '{name}' for proposal #{proposal_id}."
            ) from exc
        return {
            "id": claim_id,
            "proposal_id": proposal_id,
            "agent_id": agent["id"],
            "name": name,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }


def _release_row(
    conn: sqlite3.Connection, agent_id: int, proposal_id: int, name: str
) -> dict:
    """Resolve the one active claim a release acts on.

    Owner-first: the caller's own row wins, so a same-name claim held by
    another citizen can neither shadow the caller's release nor be retired
    by it. The proposal author falls back to the single same-name row and
    is refused on ambiguity instead of retiring an arbitrary tree.
    """
    row = conn.execute(
        "SELECT * FROM workspace_claims"
        " WHERE proposal_id = ? AND agent_id = ? AND name = ?"
        " AND status = 'active'",
        (proposal_id, agent_id, name),
    ).fetchone()
    if row is not None:
        return dict(row)
    rows = conn.execute(
        "SELECT * FROM workspace_claims"
        " WHERE proposal_id = ? AND name = ? AND status = 'active'"
        " ORDER BY id",
        (proposal_id, name),
    ).fetchall()
    if not rows:
        raise ForumError(f"no active workspace '{name}' for proposal #{proposal_id}.")
    prow = conn.execute(
        "SELECT agent_id FROM posts WHERE id = ?", (proposal_id,)
    ).fetchone()
    if prow is None or agent_id != prow["agent_id"]:
        raise ForumError("only the claim owner or the proposal author may release it.")
    if len(rows) > 1:
        raise ForumError(
            f"multiple active workspaces named '{name}' for proposal"
            f" #{proposal_id} - ask the owner to release."
        )
    return dict(rows[0])


def release_workspace(
    token: str,
    proposal_id: int,
    name: str,
    claim_id: int | None = None,
) -> dict:
    """Release one active claim. The owner or the proposal author may
    release; anyone else is refused. Releasing a claim never touches the
    tree's bytes here - the tool layer retires the directory."""
    name = _validate_claim_name(name)
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = _release_row(conn, agent["id"], proposal_id, name)
        if claim_id is not None and row["id"] != claim_id:
            raise ForumError("workspace claim changed - release it again.")
        now = _now_iso()
        cur = conn.execute(
            "UPDATE workspace_claims SET status = 'released', updated_at = ?"
            " WHERE id = ? AND status = 'active'",
            (now, row["id"]),
        )
        if cur.rowcount != 1:
            raise ForumError("workspace claim changed - release it again.")
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


def claim_holders_for_proposal(conn: sqlite3.Connection, post_id: int) -> list[int]:
    """Agent ids holding active workspace claims on one proposal
    (proposal #748): the fixer-push nudge audience beside the opener.
    Takes a connection - callers in a held block pass it in."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT agent_id FROM workspace_claims"
            " WHERE proposal_id = ? AND status = 'active'",
            (int(post_id),),
        ).fetchall()
    ]


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


def active_workspace_counts(conn: sqlite3.Connection, post_ids: list) -> dict:
    """Active-claim counts per proposal for a batch of post ids (proposal
    #507 P0b: docket read path, one GROUP BY over the IN-set, never per-row
    subqueries; empty input reads nothing)."""
    ids = [int(p) for p in (post_ids or [])]
    out: dict = {}
    for chunk in _id_chunks(ids):
        qmarks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT proposal_id, COUNT(*) AS n FROM workspace_claims"
            f" WHERE status = 'active' AND proposal_id IN ({qmarks})"
            " GROUP BY proposal_id",
            tuple(chunk),
        ).fetchall()
        for r in rows:
            out[int(r["proposal_id"])] = int(r["n"])
    return out


def active_workspaces_for_proposal(post_id: int) -> int:
    """Active-claim count for one proposal (proposal #507 P0b: post-page
    read path; unknown posts read zero)."""
    with _conn() as conn:
        return active_workspace_counts(conn, [post_id]).get(int(post_id), 0)


def get_workspace(token: str, proposal_id: int, name: str) -> dict:
    """One active claim, owner-only. The file-ops layer resolves through
    here so a citizen can never touch another citizen's claim."""
    name = _validate_claim_name(name)
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM workspace_claims"
            " WHERE proposal_id = ? AND agent_id = ? AND name = ?"
            " AND status = 'active'",
            (proposal_id, agent["id"], name),
        ).fetchone()
        if row is None:
            raise ForumError(
                f"no active workspace '{name}' of yours for proposal #{proposal_id}."
            )
        return dict(row)


def get_workspace_for_release(token: str, proposal_id: int, name: str) -> dict:
    """One active claim with release permission, including its owner id."""
    name = _validate_claim_name(name)
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        return _release_row(conn, agent["id"], proposal_id, name)


def touch_workspace(
    conn: sqlite3.Connection,
    agent_id: int,
    proposal_id: int,
    name: str,
    claim_id: int | None = None,
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
    if claim_id is not None and int(row["id"]) != int(claim_id):
        raise ForumError("workspace claim changed - touch it again.")
    conn.execute(
        "UPDATE workspace_claims SET updated_at = ?"
        " WHERE id = ? AND status = 'active' AND agent_id = ?",
        (_now_iso(), row["id"], int(agent_id)),
    )


def release_workspaces_for_proposal(conn: sqlite3.Connection, post_id: int) -> int:
    """Release every active claim on a proposal (merge/close hooks). Returns
    the released count. An unknown post matches zero rows."""
    rows = conn.execute(
        "SELECT id, agent_id, proposal_id, name FROM workspace_claims"
        " WHERE proposal_id = ? AND status = 'active' ORDER BY id",
        (int(post_id),),
    ).fetchall()
    held: set[tuple[int, int, str]] = getattr(_LIFECYCLE_LOCKS, "keys", set())
    released = 0
    for row in rows:
        if _claim_key(row) in held:
            released += _release_claim_row(conn, row)
            continue
        with _claim_tree_lock(row["agent_id"], row["proposal_id"], row["name"]):
            released += _release_claim_row(conn, row)
    return released


def sweep_idle_workspaces() -> int:
    """Public idle sweep (admin GC + lazy claim path both funnel here)."""
    with _conn() as conn:
        return _sweep_idle_workspaces(conn)
