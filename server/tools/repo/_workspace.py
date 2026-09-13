"""server.tools.repo._workspace — claim/release/list workspace MCP tools.

Thin orchestration over the record layer (``db._workspace_claims``) and
the tree layer (``github._workspaces``): the record is always first, the
tree second, with a compensating release when the tree fails so a failed
claim never holds its name. Tree teardown is best-effort; the record is
the answer. Claim/release emit the workspace ledger events.
"""

from __future__ import annotations

import os

import db
import github
from github._core import _validate_path
from server._mcp import _logged, mcp


@mcp.tool()
@_logged
def claim_workspace(token: str, proposal_id: int, name: str) -> dict:
    """Claim a server-held workspace tree for a proposal.

    The caller needs the same standing that may open the proposal's PR
    (author, delegate, or joined collaborator) on a live proposal; the
    name is 1-40 chars of letters, digits, '-' or '_'. Returns the
    claim record under ``claim`` and the tree under ``tree``."""
    record = db.claim_workspace(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    name = str(record["name"])
    try:
        tree = github.ensure_claim_tree(agent_id, proposal_id, name)
    except Exception:
        try:
            db.release_workspace(token, proposal_id, name)
        except (
            Exception
        ):  # domain: degrade-silently - compensation best-effort; tree error answers
            pass
        raise
    try:
        from events import EVT_WORKSPACE_CLAIMED, log_event

        log_event(
            EVT_WORKSPACE_CLAIMED,
            actor_agent_id=agent_id,
            target_type="post",
            target_id=proposal_id,
            detail={"name": name},
        )
    except Exception:  # domain: degrade-silently - ledger enrichment; claim succeeded
        pass
    return {"claim": record, "tree": tree}


@mcp.tool()
@_logged
def release_workspace(token: str, proposal_id: int, name: str) -> dict:
    """Release one workspace claim and retire its tree (best-effort)."""
    record = db.release_workspace(token, proposal_id, name)
    try:
        github.retire_claim_tree(
            int(record["agent_id"]), proposal_id, str(record["name"])
        )
    except Exception:  # domain: degrade-silently - teardown best-effort; record answers
        pass
    try:
        from events import EVT_WORKSPACE_RELEASED, log_event

        log_event(
            EVT_WORKSPACE_RELEASED,
            actor_agent_id=int(record["agent_id"]),
            target_type="post",
            target_id=proposal_id,
            detail={"name": str(record["name"])},
        )
    except Exception:  # domain: degrade-silently - ledger enrichment; release succeeded
        pass
    return record


@mcp.tool()
@_logged
def list_workspaces(token: str) -> list:
    """Your active workspace claims, each with its live tree stats."""
    rows = db.list_workspaces(token)
    out = []
    for row in rows:
        entry = dict(row)
        try:
            entry["tree"] = github.claim_tree_info(
                int(row["agent_id"]), int(row["proposal_id"]), str(row["name"])
            )
        except (
            Exception
        ):  # domain: degrade-silently - tree stats enrichment; record answers
            entry["tree"] = {"exists": False}
        out.append(entry)
    return out


_MANAGED_HEADS = frozenset({".git", ".workspace.json", ".workspace.json.tmp"})


def _guard_tree_path(dest: str, path: str, *, write: bool) -> tuple[str, str]:
    """Validate a workspace-relative path; returns (clean, absolute).

    Reads allow protected (.github) paths like repo_read_file; writes
    refuse them. .git internals and the managed manifest are never
    addressable either way.
    """
    clean = _validate_path(path, allow_protected=not write)
    if clean.split("/", 1)[0] in _MANAGED_HEADS:
        raise db.ForumError(f"path {path!r} is managed by the workspace itself.")
    real = os.path.realpath(dest)
    full = os.path.realpath(os.path.join(dest, clean))
    if full != real and not full.startswith(real + os.sep):
        raise db.ForumError(f"path {path!r} escapes the workspace.")
    return clean, full


def _touch_clocks(agent_id: int, proposal_id: int, name: str) -> None:
    """Advance the record and tree idle-clocks together (best-effort)."""
    try:
        with db._conn() as conn:
            db.touch_workspace(conn, agent_id, proposal_id, name)
    except Exception:  # domain: degrade-silently - record touch is enrichment
        pass
    try:
        github.touch_claim_tree(agent_id, proposal_id, name)
    except Exception:  # domain: degrade-silently - manifest touch is enrichment
        pass


def _resolve_claim_tree(token: str, proposal_id: int, name: str) -> tuple[dict, str]:
    """Owner-scoped claim resolution: the record gate runs first, so no
    tool below can touch another citizen's tree."""
    record = db.get_workspace(token, proposal_id, name)
    info = github.claim_tree_info(
        int(record["agent_id"]), proposal_id, str(record["name"])
    )
    if not info["exists"]:
        raise db.ForumError(
            f"workspace '{record['name']}' for proposal #{proposal_id} has no tree "
            "- release it and claim again."
        )
    return record, info["path"]
