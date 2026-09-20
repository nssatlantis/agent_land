"""db._transfer_tickets — single-use HTTP transfer tickets for workspace claims.

A ticket binds (agent, proposal, claim, paths, scope) to one random secret
so file BYTES can ride plain HTTPS (curl to local disk) while the MCP
channel carries only URLs, receipts and sha256 hashes. Mint and redeem are
the control plane; the data plane lives in server/_transfer.py.

Scopes: 'read' tickets allow repeated GETs until expiry (downloads are
idempotent); 'write' tickets burn one POST per path and die when every
path is consumed or the TTL lapses. The long-lived agent token never
appears in a URL - a leaked ticket is single-use and short-lived.

Shape mirrors guild_invites (expires_at + status machine + lazy sweep):
unused -> used | expired. Protocol-agnostic like the rest of db/ - HTTP
status mapping rides ForumError.detail (http_status), read by the route
layer, never by db callers.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent

_VALID_SCOPES = ("read", "write")


def _ticket_ttl_seconds() -> int:
    """Ticket lifetime floor: tickets must expire, so a non-positive knob
    falls back to the one-hour default instead of disabling expiry."""
    try:
        ttl = int(config.TRANSFER_TICKET_TTL_SECONDS)
    except Exception:  # domain: degrade-silently - a bad knob falls back to default
        return 3600
    return ttl if ttl >= 60 else 3600


def _ticket_max_paths() -> int:
    try:
        cap = int(config.TRANSFER_MAX_PATHS)
    except Exception:  # domain: degrade-silently - a bad knob falls back to default
        return 8
    return cap if cap >= 1 else 8


def _fail(status: int, message: str) -> ForumError:
    """A ticket refusal carrying its HTTP mapping for the route layer."""
    exc = ForumError(message)
    exc.detail = {"http_status": status}
    return exc


def _validate_ticket_paths(paths: object) -> list:
    """Shape-check the path list (syntax is enforced by the tool/route
    layers against the live tree; db only pins shape and the cap)."""
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ForumError("ticket paths must be a non-empty list of file paths.")
    clean = []
    for p in paths:
        if not isinstance(p, str) or not p or not p.strip() or len(p) > 500:
            raise ForumError("ticket paths must be non-empty strings under 500 chars.")
        clean.append(p.strip())
    if len(set(clean)) != len(clean):
        raise ForumError("ticket paths must be unique within one ticket.")
    cap = _ticket_max_paths()
    if len(clean) > cap:
        raise ForumError(
            f"ticket covers {len(clean)} paths (cap {cap},"
            " TRANSFER_MAX_PATHS) - mint a second ticket for the rest."
        )
    return clean


def _ticket_retention_days() -> float:
    """How long terminal tickets (used/expired) are kept for audit. Zero
    disables pruning (rows accumulate); otherwise the sweep deletes them
    past the floor."""
    try:
        days = float(config.TRANSFER_TICKET_RETENTION_DAYS)
    except Exception:  # domain: degrade-silently - a bad knob falls back to default
        return 30.0
    return days if days > 0 else 0.0


def _sweep_expired_tickets(conn: sqlite3.Connection) -> int:
    """Mark unused tickets past their expiry and prune terminal tickets
    past the retention floor. Runs lazily on every mint and redeem, plus
    the admin GC, so an abandoned ticket never reads as redeemable and
    the table never grows without bound."""
    now = datetime.now(timezone.utc)
    cur = conn.execute(
        "UPDATE transfer_tickets SET status = 'expired'"
        " WHERE status = 'unused' AND expires_at <= ?",
        (_now_iso(now),),
    )
    marked = cur.rowcount
    retention = _ticket_retention_days()
    if retention > 0:
        floor = _now_iso(now - timedelta(days=retention))
        conn.execute(
            "DELETE FROM transfer_tickets"
            " WHERE status IN ('used', 'expired')"
            " AND COALESCE(used_at, created_at) <= ?",
            (floor,),
        )
    return marked


def _ticket_row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    try:
        out["paths"] = json.loads(row["paths_json"] or "[]")
    except Exception:  # domain: degrade-silently - corrupt JSON reads as no paths
        out["paths"] = []
    try:
        out["expect_shas"] = (
            json.loads(row["expect_shas_json"]) if row["expect_shas_json"] else None
        )
    except Exception:  # domain: degrade-silently - corrupt JSON reads as no pins
        out["expect_shas"] = None
    try:
        out["used_paths"] = json.loads(row["used_paths_json"] or "[]")
    except Exception:  # domain: degrade-silently - corrupt JSON reads as unused
        out["used_paths"] = []
    return out


def mint_transfer_ticket(
    token: str,
    proposal_id: int,
    name: str,
    paths: list,
    scope: str,
    expect_shas: dict | None = None,
) -> dict:
    """Mint one ticket over an owned active claim. Returns the raw secret
    ONCE (it is stored hashed) plus the expiry and the pinned paths.

    Only the claim owner may mint: the ticket inherits the exact standing
    of workspace file ops (db.get_workspace, owner-only)."""
    if scope not in _VALID_SCOPES:
        raise ForumError("ticket scope must be 'read' or 'write'.")
    clean_paths = _validate_ticket_paths(paths)
    pins = None
    if expect_shas is not None:
        if not isinstance(expect_shas, dict):
            raise ForumError("expect_shas must be a {path: sha256} mapping.")
        unknown = set(expect_shas) - set(clean_paths)
        if unknown:
            raise ForumError(
                f"expect_shas names paths outside this ticket: {sorted(unknown)!r}."
            )
        pins = {str(k): str(v) for k, v in expect_shas.items()}
    from db._workspace_claims import _validate_claim_name

    name = _validate_claim_name(name)
    raw = "xfer_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
    now = datetime.now(timezone.utc)
    # BEGIN IMMEDIATE: the sweep + claim-check + insert is a read-then-write
    # family, so concurrent mints serialize instead of interleaving.
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _sweep_expired_tickets(conn)
        claim = conn.execute(
            "SELECT id, agent_id FROM workspace_claims"
            " WHERE proposal_id = ? AND name = ? AND status = 'active'",
            (proposal_id, name),
        ).fetchone()
        if claim is None or claim["agent_id"] != agent["id"]:
            raise ForumError(
                f"no active workspace '{name}' of yours for proposal"
                f" #{proposal_id} - tickets mint on live owned claims only."
            )
        expires = _now_iso(now + timedelta(seconds=_ticket_ttl_seconds()))
        try:
            conn.execute(
                "INSERT INTO transfer_tickets"
                " (agent_id, proposal_id, claim_name, scope, paths_json,"
                " expect_shas_json, ticket_hash, status, used_paths_json,"
                " created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'unused', '[]', ?, ?)",
                (
                    agent["id"],
                    proposal_id,
                    name,
                    scope,
                    json.dumps(clean_paths),
                    json.dumps(pins) if pins is not None else None,
                    digest,
                    _now_iso(now),
                    expires,
                ),
            )
        except sqlite3.IntegrityError as exc:  # domain: fail-loudly - a hash collision mints visibly, never silently reuses
            raise ForumError("ticket mint collided - retry the mint.") from exc
        return {
            "ticket": raw,
            "agent_id": agent["id"],
            "proposal_id": proposal_id,
            "name": name,
            "scope": scope,
            "paths": clean_paths,
            "expires_at": expires,
        }


def redeem_transfer_ticket(ticket: str, scope: str, path: str) -> dict:
    """Validate one ticket use and consume it. Read scope never consumes
    (downloads are idempotent); write scope burns the path, and the last
    path burns the ticket. Every refusal carries an http_status detail
    for the route layer; unknown tickets read as 404 so existence never
    leaks."""
    raw = str(ticket or "")
    if not raw.startswith("xfer_"):
        raise _fail(404, "unknown transfer ticket.")
    digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
    # BEGIN IMMEDIATE: check-then-burn must be atomic, or two concurrent
    # POSTs of the same write path both see it unused and both apply.
    with _conn(immediate=True) as conn:
        _sweep_expired_tickets(conn)
        row = conn.execute(
            "SELECT * FROM transfer_tickets WHERE ticket_hash = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise _fail(404, "unknown transfer ticket.")
        if row["status"] == "expired" or row["expires_at"] <= _now_iso():
            if row["status"] != "expired":
                conn.execute(
                    "UPDATE transfer_tickets SET status = 'expired' WHERE id = ?",
                    (row["id"],),
                )
            raise _fail(410, "transfer ticket expired - mint a fresh one.")
        if row["status"] == "used":
            raise _fail(
                409, "transfer ticket already used - mint a fresh one to retry."
            )
        if row["scope"] != scope:
            raise _fail(
                400,
                f"ticket is {row['scope']}-only - mint a {scope} ticket"
                " for this direction.",
            )
        t = _ticket_row_to_dict(row)
        if path not in t["paths"]:
            raise _fail(400, f"path {path!r} is not covered by this ticket.")
        claim = conn.execute(
            "SELECT id FROM workspace_claims"
            " WHERE agent_id = ? AND proposal_id = ? AND name = ?"
            " AND status = 'active'",
            (row["agent_id"], row["proposal_id"], row["claim_name"]),
        ).fetchone()
        if claim is None:
            raise _fail(
                404,
                "workspace for this ticket is gone - release it and"
                " claim again, then mint a fresh ticket.",
            )
        if scope == "write":
            if path in t["used_paths"]:
                raise _fail(
                    409,
                    f"path {path!r} was already uploaded on this ticket -"
                    " mint a fresh ticket to retry it.",
                )
            used = list(t["used_paths"]) + [path]
            now = _now_iso()
            if set(used) >= set(t["paths"]):
                conn.execute(
                    "UPDATE transfer_tickets"
                    " SET used_paths_json = ?, status = 'used', used_at = ?"
                    " WHERE id = ?",
                    (json.dumps(used), now, row["id"]),
                )
            else:
                conn.execute(
                    "UPDATE transfer_tickets SET used_paths_json = ? WHERE id = ?",
                    (json.dumps(used), row["id"]),
                )
            t["used_paths"] = used
        return t


def sweep_expired_transfer_tickets() -> int:
    """Public expiry sweep (mint/redeem paths funnel here lazily; the
    admin GC calls it directly)."""
    with _conn() as conn:
        return _sweep_expired_tickets(conn)
