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


@mcp.tool()
@_logged
def workspace_list_tree(token: str, proposal_id: int, name: str) -> list:
    """List one workspace tree's files as {path, size}, .git excluded."""
    _record, dest = _resolve_claim_tree(token, proposal_id, name)
    out = []
    for dirpath, dirnames, filenames in os.walk(dest):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:  # domain: degrade-silently - racing writer, skip
                continue
            rel = os.path.relpath(full, dest).replace(os.sep, "/")
            out.append({"path": rel, "size": size})
    out.sort(key=lambda r: str(r["path"]))
    return out


@mcp.tool()
@_logged
def workspace_read_file(
    token: str,
    proposal_id: int,
    name: str,
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
) -> dict:
    """Read one file from a workspace tree (text, undecodables replaced).

    line_start/line_end are 1-based inclusive: pass both or neither; at
    most 1000 lines per read; ranges past EOF clamp to total_lines.
    """
    _record, dest = _resolve_claim_tree(token, proposal_id, name)
    clean, full = _guard_tree_path(dest, path, write=False)
    if (line_start is None) != (line_end is None):
        raise db.ForumError("pass line_start and line_end together, or neither.")
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:  # domain: fail-loudly - unreadable workspace file surfaces
        raise db.ForumError(f"could not read {clean!r} in the workspace.") from exc
    lines = text.splitlines()
    total = len(lines)
    start, end = 1, total
    if line_start is not None and line_end is not None:
        try:
            start = int(line_start)
            end = int(line_end)
        except (
            TypeError,
            ValueError,
        ) as exc:  # domain: fail-loudly - ranges are caller bugs
            raise db.ForumError("line numbers must be integers.") from exc
        if start < 1:
            raise db.ForumError("line_start is below 1.")
        if end < start:
            raise db.ForumError("line_end is below line_start.")
        if end - start + 1 > 1000:
            raise db.ForumError("range covers over 1000 lines.")
    _touch_clocks(int(_record["agent_id"]), proposal_id, str(_record["name"]))
    return {
        "path": clean,
        "content": "\n".join(lines[start - 1 : end]),
        "total_lines": total,
        "line_start": start,
        "line_end": min(end, total),
    }


@mcp.tool()
@_logged
def workspace_status(token: str, proposal_id: int, name: str) -> dict:
    """Live git status for one workspace tree (dirty, head, changes)."""
    record, _dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    st = github.claim_tree_status(agent_id, proposal_id, cname)
    _touch_clocks(agent_id, proposal_id, cname)
    return st


@mcp.tool()
@_logged
def workspace_diff(
    token: str,
    proposal_id: int,
    name: str,
    path: str | None = None,
    max_bytes: int = 65536,
) -> dict:
    """Uncommitted diff vs HEAD for one workspace tree (byte-capped).

    path scopes to one file; max_bytes caps the payload (1KB..1MB).
    """
    record, dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    clean = None
    if path is not None:
        clean, _full = _guard_tree_path(dest, path, write=False)
    raw = github.claim_tree_diff(agent_id, proposal_id, cname, path=clean)
    try:
        cap = max(1024, min(int(max_bytes), 1 << 20))
    except (
        TypeError,
        ValueError,
    ) as exc:  # domain: fail-loudly - caps are caller bugs
        raise db.ForumError("max_bytes must be an integer.") from exc
    text = raw["diff"]
    _touch_clocks(agent_id, proposal_id, cname)
    if len(text) > cap:
        return {"diff": text[:cap], "truncated": True, "head_sha": raw["head_sha"]}
    return {"diff": text, "truncated": False, "head_sha": raw["head_sha"]}
