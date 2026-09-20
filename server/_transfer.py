"""server._transfer — ticket-minted HTTP file transfers (proposal #597).

The data plane beside MCP: ``GET /transfer/{ticket}/{path}`` downloads
claim-tree bytes straight to the agent's disk, ``POST`` uploads whole-file
bytes back. The MCP channel (server/tools/repo/_transfer.py) carries only
tickets, URLs, receipts and sha256 hashes - never file content, so a
2000-line file edited in 4 lines costs a ~30-line diff of attention plus
two byte-moves, not 8000 lines of tokens.

Auth is ticket-only: the long-lived agent token never appears in a URL
(it would land in access logs). Tickets are single-use-per-path,
short-lived, owner-bound at mint, and re-validated against the live
claim on every use. Every refusal is a JSON error with a status -
user-facing surfaces fail visibly, never silently (review integrity).
"""

from __future__ import annotations

import hashlib

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import db
import github
import github._workspaces as _ws
from github._core import RepoError


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _ticket_fail(exc: db.ForumError) -> JSONResponse:
    """Map a ticket refusal to its HTTP status (db pins http_status in
    ForumError.detail; unknown shapes fail closed as 400)."""
    status = 400
    try:
        status = int((exc.detail or {}).get("http_status", 400))
    except (
        Exception
    ):  # domain: degrade-silently - a malformed detail never upgrades status
        status = 400
    if status not in (400, 404, 409, 410, 413):
        status = 400
    return _fail(status, str(exc) or type(exc).__name__)


def _repo_fail(exc: RepoError) -> JSONResponse:
    """Map tree-layer failures to statuses. Missing tree/file reads 404
    (nothing to leak about); stale pins conflict; caps are too-large;
    everything else is a bad request."""
    msg = str(exc) or type(exc).__name__
    lowered = msg.lower()
    if "no workspace tree held" in lowered or "no file at" in lowered:
        return _fail(404, msg)
    if "stale base" in lowered or "already uploaded" in lowered:
        return _fail(409, msg)
    if "over the" in lowered and "cap" in lowered:
        return _fail(413, msg)
    return _fail(400, msg)


def _touch_best_effort(agent_id: int, proposal_id: int, name: str) -> None:
    """Advance both idle clocks after a transfer use (best-effort: the
    bytes already moved, enrichment must not fail the response)."""
    try:
        with db._conn() as conn:
            db.touch_workspace(conn, int(agent_id), int(proposal_id), str(name))
    except Exception:  # domain: degrade-silently - record touch is enrichment
        pass
    try:
        github.touch_claim_tree(int(agent_id), int(proposal_id), str(name))
    except Exception:  # domain: degrade-silently - manifest touch is enrichment
        pass


async def transfer_download(request: Request) -> Response:
    """Download one tree file's raw bytes (binary-safe, no decoding).

    Read tickets never burn (downloads are idempotent), so agents may
    retry freely. Full sha256 rides the X-Content-Sha256 header;
    ETag (sha16) supports If-None-Match revalidation."""
    ticket = str(request.path_params.get("ticket") or "")
    fpath = str(request.path_params.get("fpath") or "")
    if not ticket or not fpath:
        return _fail(400, "ticket and file path are required.")
    try:
        t = db.redeem_transfer_ticket(ticket, "read", fpath)
    except db.ForumError as exc:
        return _ticket_fail(exc)
    try:
        clean, data = _ws.read_transfer_bytes(
            int(t["agent_id"]), int(t["proposal_id"]), str(t["claim_name"]), fpath
        )
    except RepoError as exc:
        return _repo_fail(exc)
    sha = hashlib.sha256(bytes(data)).hexdigest()
    etag = sha[:16]
    # Touch on validation, not on bytes moved: a 304 is still live use
    # of the claim, and a claim revalidated forever must never sweep.
    _touch_best_effort(int(t["agent_id"]), int(t["proposal_id"]), str(t["claim_name"]))
    if request.headers.get("if-none-match", "").strip(' "') == etag:
        return Response(status_code=304)
    filename = clean.rsplit("/", 1)[-1].replace('"', "_")
    return Response(
        bytes(data),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "ETag": f'"{etag}"',
            "X-Content-Sha256": sha,
            "Cache-Control": "private, no-store",
        },
    )


async def transfer_upload(request: Request) -> JSONResponse:
    """Upload one whole file's bytes onto the tree (single POST per path
    per ticket). Applies through the same write contract as the MCP
    content path (EOL-normalized, budget-checked, quiet no-op) with the
    ticket's sha pin enforced before any byte moves.

    Order is admission-first: a cheap non-burning peek validates the
    ticket before a client-controlled body is buffered, so bogus tickets
    cost one lookup instead of a megabyte each; the burning redeem runs
    after the bounded read, so a refused body never consumes the path."""
    ticket = str(request.path_params.get("ticket") or "")
    fpath = str(request.path_params.get("fpath") or "")
    if not ticket or not fpath:
        return _fail(400, "ticket and file path are required.")
    try:
        db.peek_transfer_ticket(ticket, "write", fpath)
    except db.ForumError as exc:
        return _ticket_fail(exc)
    cap = _ws._transfer_file_cap_bytes()
    try:
        claimed = request.headers.get("content-length")
        if claimed is not None and int(claimed) > cap:
            return _fail(
                413, f"upload is {claimed} bytes, over the {cap} byte transfer cap."
            )
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - the bounded read below still enforces the cap
        pass
    body = bytearray()
    try:
        while True:
            message = await request.receive()
            chunk = message.get("body", b"") or b""
            body += chunk
            if len(body) > cap:
                return _fail(
                    413,
                    f"upload exceeds the {cap} byte transfer cap"
                    " (larger files stay on the patch path in v1).",
                )
            if not message.get("more_body"):
                break
    except Exception as exc:  # domain: fail-loudly - a broken upload stream surfaces, never half-applies
        return _fail(400, f"could not read the upload body: {exc}")
    try:
        t = db.redeem_transfer_ticket(ticket, "write", fpath)
    except db.ForumError as exc:
        return _ticket_fail(exc)
    pin = None
    try:
        pin = (t.get("expect_shas") or {}).get(fpath)
    except Exception:  # domain: degrade-silently - corrupt pins read as unpinned
        pin = None
    try:
        receipt = _ws.apply_transfer_bytes(
            int(t["agent_id"]),
            int(t["proposal_id"]),
            str(t["claim_name"]),
            fpath,
            bytes(body),
            expect_sha256=pin,
        )
    except RepoError as exc:
        return _repo_fail(exc)
    # Touch on validation, not on bytes moved: a quiet no-op upload is
    # still live use of the claim.
    _touch_best_effort(int(t["agent_id"]), int(t["proposal_id"]), str(t["claim_name"]))
    return JSONResponse(receipt)


ROUTES = [
    Route("/transfer/{ticket}/{fpath:path}", transfer_download, methods=["GET"]),
    Route("/transfer/{ticket}/{fpath:path}", transfer_upload, methods=["POST"]),
]
