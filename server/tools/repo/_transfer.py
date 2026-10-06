"""server.tools.repo._transfer — claim-scoped transfer tickets (proposal #919).

The control plane for HTTP file transfers: nothing here ever carries file
content, only tickets, URLs and sha256 hashes. The bytes ride
``GET``/``POST /transfer/{ticket}/{path}`` (server/_transfer.py), fetched
with curl straight to the agent's disk and uploaded back after local
editing. Pick this track for large files: a 4-line fix in a 2000-line
file costs two byte-moves plus the diff review, not 8000 lines of
tokens. Small files stay on workspace_write_file.

Tickets are minted by ``workspace_claim`` itself - claim auto-mints them,
renew re-mints them - so there is no separate mint tool. They are
CLAIM-SCOPED: no path is pinned, because the path is already in the URL
and the engine guard re-validates it on every read and apply.

Read and write stay SEPARATE tickets. The redeem layer refuses a scope
mismatch, so one ticket cannot both disclose the tree and overwrite it;
collapsing them would hand a leaked read ticket the power to write.
"""

from __future__ import annotations

import hashlib
import os
from urllib.parse import quote

import config
import db
import github._workspaces as _ws

from ._workspace import _guard_tree_path, _touch_clocks


def _transfer_base() -> str:
    """Public base for transfer URLs: FORUM_PUBLIC_BASE_URL when set (the
    https origin behind the proxy, same knob viewer._utils._abs and the PR
    header honor), else the historical FORUM_HOST:FORUM_PORT derivation.
    Read live so the knob applies without a restart."""
    try:
        base = str(config.PUBLIC_BASE_URL or "").strip().rstrip("/")
    except Exception:  # domain: degrade-silently - unreadable knob, derive
        base = ""
    if base:
        return base
    return f"http://{config.FORUM_HOST}:{config.FORUM_PORT}"


def transfer_url(ticket: str, path: str) -> str:
    """One data-plane URL for `path` on a claim-scoped ticket, relative to
    the base the claim returned. Percent-encodes the path per segment so a
    name with a space, '#' or '%' still addresses the file it names."""
    return f"/transfer/{ticket}/{quote(path, safe='/')}"


def _check_pins(dest: str, expect_shas: dict | None) -> dict | None:
    """Make every pin SATISFIABLE or refuse the mint.

    A claim-scoped ticket pins no paths, so db cannot tell whether a pin
    key names a file this ticket could ever carry - an unknown key would
    simply never match a request and the guard would be dead code dressed
    as protection. So each key is checked against the LIVE tree here: it
    must guard clean, exist, and already hash to the pinned value. That
    makes a pin either real or loudly refused, and it catches the typo at
    mint instead of at upload.
    """
    if not expect_shas:
        return None
    # Key every pin by the VALIDATED path, never by the key as given. The
    # redeem layer looks a request up by the URL segment it was handed
    # (server/_transfer.py), so a key differing from its cleaned path by so
    # much as one leading space would be guard-checked and hash-verified
    # against the right file and then stored where no request can ever
    # match it - a dead pin, the exact failure this function exists to
    # prevent. Two raw keys cleaning to one path collapse safely: both
    # verified against the same bytes, so their pins agree.
    cleaned: dict[str, str] = {}
    for raw, want in sorted(expect_shas.items()):
        clean, full = _guard_tree_path(dest, str(raw), write=True)
        if not os.path.isfile(full):
            raise db.ForumError(
                f"expect_shas names {clean!r}, which is not a file in this"
                " workspace - a pin guards an overwrite, so it must exist."
            )
        cap = _ws._transfer_file_cap_bytes()
        try:
            size = os.path.getsize(full)
            if size > cap:
                raise db.ForumError(
                    f"expect_shas names {clean!r} at {size} bytes, over the"
                    f" {cap} byte transfer cap - it could never upload."
                )
            with open(full, "rb") as fh:
                got = hashlib.sha256(fh.read()).hexdigest()
        except OSError as exc:  # domain: fail-loudly - a racing writer surfaces
            raise db.ForumError(f"could not read {clean!r} in the workspace.") from exc
        if got != str(want).lower():
            raise db.ForumError(
                f"expect_shas for {clean!r} is {want!r} but the workspace"
                f" file hashes to {got} - the tree moved since you read it."
            )
        cleaned[clean] = str(want).lower()
    return cleaned


def mint_claim_tickets(
    token: str,
    record: dict,
    dest: str,
    expect_shas: dict | None = None,
) -> dict:
    """Mint the claim's read AND write tickets, claim-scoped.

    One call so the agent never asks for a capability its claim already
    confers. Returns {base, read, write, expires_at} where each of
    read/write is {ticket, scope, expires_at}; the caller composes URLs
    with transfer_url(ticket, path). `expect_shas` pins the files an upload
    will overwrite and is verified against the live tree BEFORE either
    ticket is minted, so a refused pin mints nothing at all.
    """
    agent_id = int(record["agent_id"])
    proposal_id = int(record["proposal_id"])
    name = str(record["name"])
    pins = _check_pins(dest, expect_shas)
    out: dict = {"base": _transfer_base()}
    expires = ""
    for scope in ("read", "write"):
        minted = db.mint_transfer_ticket(
            token,
            proposal_id,
            name,
            [],
            scope,
            expect_shas=pins if scope == "write" else None,
            claim_scoped=True,
        )
        out[scope] = {
            "ticket": minted["ticket"],
            "scope": scope,
            "expires_at": minted["expires_at"],
        }
        expires = minted["expires_at"]
    out["expires_at"] = expires
    _touch_clocks(agent_id, proposal_id, name, int(record["id"]))
    return out
