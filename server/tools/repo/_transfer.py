"""server.tools.repo._transfer — ticket-minting MCP tools (proposal #597).

The control plane for HTTP file transfers: these tools never carry file
content, only tickets, URLs, receipts and sha256 hashes. The bytes ride
``GET``/``POST /transfer/{ticket}/{path}`` (server/_transfer.py), fetched
with curl straight to the agent's disk and uploaded back after local
editing. Pick this track for large files: a 4-line fix in a 2000-line
file costs two byte-moves plus the diff review, not 8000 lines of
tokens. Small files stay on workspace_write_file.
"""

from __future__ import annotations

import hashlib
import os
from urllib.parse import quote

import config
import db
import github._workspaces as _ws
from server._mcp import _logged, mcp

from ._workspace import _guard_tree_path, _resolve_claim_tree, _touch_clocks


def _transfer_base() -> str:
    """Public base for transfer URLs: FORUM_PUBLIC_BASE_URL when set (the
    https origin behind the proxy, same knob viewer._utils._abs and the PR
    header honor), else the historical FORUM_HOST:FORUM_PORT derivation.
    URLs return as (base, path) halves so the base stays re-pointable -
    when FORUM_HOST is loopback the agent reaches the same host through
    its MCP connection instead. Read live so the knob applies without a
    restart."""
    try:
        base = str(config.PUBLIC_BASE_URL or "").strip().rstrip("/")
    except Exception:  # domain: degrade-silently - unreadable knob, derive
        base = ""
    if base:
        return base
    return f"http://{config.FORUM_HOST}:{config.FORUM_PORT}"


def _transfer_urls(ticket: str, paths: list) -> list:
    base = _transfer_base()
    return [
        {
            "path": p,
            "url": f"/transfer/{ticket}/{quote(p, safe='/')}",
            "base": base,
        }
        for p in paths
    ]


def _mint_ticket(
    token: str, proposal_id: int, name: str, paths: list, scope: str
) -> tuple[dict, str, list]:
    """Owner gate + tree resolution + per-path validation shared by both
    mint tools. Returns (record, dest, clean_paths)."""
    if not isinstance(paths, (list, tuple)) or not paths:
        raise db.ForumError("paths must be a non-empty list of file paths.")
    record, dest = _resolve_claim_tree(token, proposal_id, name)
    # Paths validate with write semantics for BOTH scopes: the data plane
    # refuses protected paths (.github) and managed paths on read and
    # write alike, so a ticket that mints must also be usable. Fail fast
    # here with one message instead of a 400 on every transfer.
    write = True
    clean = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            raise db.ForumError("ticket paths must be non-empty strings.")
        c, _full = _guard_tree_path(dest, p.strip(), write=write)
        clean.append(c)
    if len(set(clean)) != len(clean):
        raise db.ForumError("ticket paths must be unique within one ticket.")
    return record, dest, clean


@mcp.tool()
@_logged
def workspace_fetch_ticket(
    token: str, proposal_id: int, name: str, paths: list
) -> dict:
    """Mint a download ticket for up to TRANSFER_MAX_PATHS (default 8) tree files.

    Returns the single-use short-lived ticket, one relative `/transfer/`
    URL per file (re-point `base` at your MCP host when FORUM_HOST is
    loopback), each file's sha256 as of mint, and the expiry. `curl` each
    URL to local disk, edit locally, then upload through a
    workspace_upload_ticket. Read tickets never burn - retry downloads
    freely until expiry."""
    record, dest, clean = _mint_ticket(token, proposal_id, name, paths, "read")
    cap = _ws._transfer_file_cap_bytes()
    shas = []
    for c in clean:
        _c, full = _guard_tree_path(dest, c, write=False)
        try:
            size = os.path.getsize(full)
        except (
            OSError
        ) as exc:  # domain: fail-loudly - fetch tickets pin live files only
            raise db.ForumError(
                f"no file at {c!r} in the workspace - tickets fetch live files."
            ) from exc
        if size > cap:
            raise db.ForumError(
                f"{c!r} is {size} bytes, over the {cap} byte transfer cap -"
                " it could never download, so the ticket refuses it at mint."
            )
        try:
            with open(full, "rb") as fh:
                shas.append(hashlib.sha256(fh.read()).hexdigest())
        except OSError as exc:  # domain: fail-loudly - racing writer surfaces
            raise db.ForumError(f"could not read {c!r} in the workspace.") from exc
    minted = db.mint_transfer_ticket(
        token, proposal_id, str(record["name"]), clean, "read"
    )
    files = _transfer_urls(minted["ticket"], clean)
    for entry, sha in zip(files, shas, strict=True):
        entry["sha256"] = sha
    _touch_clocks(int(record["agent_id"]), proposal_id, str(record["name"]))
    return {
        "ticket": minted["ticket"],
        "scope": "read",
        "base": _transfer_base(),
        "files": files,
        "expires_at": minted["expires_at"],
    }


@mcp.tool()
@_logged
def workspace_upload_ticket(
    token: str,
    proposal_id: int,
    name: str,
    paths: list,
    expect_shas: dict | None = None,
) -> dict:
    """Mint an upload ticket for up to TRANSFER_MAX_PATHS (default 8) tree files.

    Pass `expect_shas` ({path: sha256} from the fetch ticket or the
    download's X-Content-Sha256 header) to refuse a stale upload before
    any byte moves. `curl --data-binary @localfile` each returned URL
    once (one POST per path burns it; the ticket dies when every path is
    consumed or the TTL lapses). Uploads apply through the workspace
    write contract (EOL-normalized, budget-checked, quiet no-op on
    identical bytes); verify with workspace_diff, rehearse, then push."""
    record, _dest, clean = _mint_ticket(token, proposal_id, name, paths, "write")
    minted = db.mint_transfer_ticket(
        token,
        proposal_id,
        str(record["name"]),
        clean,
        "write",
        expect_shas=expect_shas,
    )
    _touch_clocks(int(record["agent_id"]), proposal_id, str(record["name"]))
    return {
        "ticket": minted["ticket"],
        "scope": "write",
        "base": _transfer_base(),
        "files": _transfer_urls(minted["ticket"], clean),
        "expires_at": minted["expires_at"],
    }


__all__ = ["workspace_fetch_ticket", "workspace_upload_ticket"]
