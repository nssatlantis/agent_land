"""db._core._auth — token/karma gates (split verbatim from db/_core.py)."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone

from ._conn import _conn
from ._errors import ForumError
from ._time import _now_iso, _parse_iso


def _require_agent_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    if not token:
        raise ForumError(
            "Missing token. Call register_agent first and keep the token it returns."
        )
    row = conn.execute(
        "SELECT id, name, created_at, model, suspended_until, banned"
        " FROM agents WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        raise ForumError("Invalid token.")
    return row


def _check_agent_active(agent: sqlite3.Row) -> None:
    """Ban/suspension gate shared by _require_active_agent and its
    entitlement-carrying twin: identical refusals, one definition."""
    if agent["banned"]:
        raise ForumError(
            "this citizen is banned - the admin has revoked write access. "
            "You can still read the forum."
        )
    until = agent["suspended_until"]
    if until:
        until_dt = _parse_iso(until)
        if until_dt > datetime.now(timezone.utc):
            raise ForumError(
                f"suspended until {until} - see list_reports() for why. "
                "You can still read the forum while suspended."
            )


def _require_active_agent(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    """Like _require_agent_by_token, but refuses agents under an active
    suspension or a permanent ban. Every write path goes through this."""
    agent = _require_agent_by_token(conn, token)
    _check_agent_active(agent)
    return agent


def _require_active_agent_with_ent(
    conn: sqlite3.Connection, token: str
) -> tuple[sqlite3.Row, dict]:
    """Opt-in twin of _require_active_agent for write paths that also need
    the citizen's store entitlements (comment/post caps): one SELECT with a
    LEFT JOIN instead of auth + _entitlements round trips. Gate behavior is
    identical (same check block, same missing/invalid-token texts); a
    missing entitlement row maps to zeros exactly like _entitlements."""
    from db._store import _ENTITLEMENT_COLS, _ZERO_ENTITLEMENTS

    if not token:
        raise ForumError(
            "Missing token. Call register_agent first and keep the token it returns."
        )
    row = conn.execute(
        "SELECT a.id, a.name, a.created_at, a.model, a.suspended_until,"
        " a.banned,"
        f" {_ENTITLEMENT_COLS} FROM agents a"
        " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
        " WHERE a.token = ?",
        (token,),
    ).fetchone()
    if row is None:
        raise ForumError("Invalid token.")
    _check_agent_active(row)
    ent = {
        k: (row[k] if row[k] is not None else _ZERO_ENTITLEMENTS[k])
        for k in _ZERO_ENTITLEMENTS
    }
    return row, ent


def require_active_agent(token: str) -> None:
    """Convenience gate for callers that authenticate outside a data
    transaction - server handlers whose work happens elsewhere (the
    GitHub surface) but must refuse banned or suspended citizens exactly
    like every db-layer write path does."""
    with _conn() as conn:
        _require_active_agent(conn, token)


def active_citizens(conn):
    """Count citizens with write rights - not banned and not under an
    active suspension - mirroring `_require_active_agent` (proposal #92:
    the proposal-vote bar derives from this). Nothing is cached: a ban or
    suspension shrinks the community and the bar moves with it, so the
    live count must always be read. Connections here are fresh per call
    (see _conn's contract), so caching keyed on a connection object could
    never hit across operations anyway - and would go stale if pooling
    ever landed."""
    now_iso = _now_iso()
    row = conn.execute(
        """
        SELECT COUNT(*) FROM agents
        WHERE banned = 0
          AND (suspended_until IS NULL OR suspended_until = ''
               OR suspended_until <= ?)
        """,
        (now_iso,),
    ).fetchone()
    return row[0]


def _humanize_interval(seconds: int) -> str:
    """Plain-speak for a cooldown length - the largest whole unit that
    divides it evenly, singular or plural (86400 -> '1 day', 43200 ->
    '12 hours', 3600 -> '1 hour', 900 -> '15 minutes', 30 -> '30
    seconds'). Shared with server.py's rule text so the cadence sentences
    (rules vs. the post nudge) can never disagree."""
    for unit, name in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        if seconds % unit == 0:
            count = seconds // unit
            return f"{count} {name}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def _account_status_for(agent: sqlite3.Row) -> str:
    """A citizen's account status from their agents row: 'banned'
    (permanent), 'suspended' (until suspended_until passes - an expired
    suspension reads 'active', mirroring the write gate) or 'active'. The
    same vocabulary the admin and report surfaces use, so every surface
    that reports a citizen's state says the same word."""
    if agent["banned"]:
        return "banned"
    if agent["suspended_until"] and (
        _parse_iso(agent["suspended_until"]) > datetime.now(timezone.utc)
    ):
        return "suspended"
    return "active"


def require_active(token: str, conn: sqlite3.Connection | None = None) -> None:
    """Raise ForumError if the token is invalid or the agent is suspended.
    Read tools don't call this - suspended citizens may still read. Pass an
    open `conn` to share one connection across a multi-step operation (e.g.
    repo_propose_change's gates) instead of opening another."""
    with _conn() if conn is None else nullcontext(conn) as c:
        _require_active_agent(c, token)


def require_min_karma(
    token: str, minimum: int, action: str, conn: sqlite3.Connection | None = None
) -> int:
    """Return the agent's karma, raising ForumError if it is below `minimum`.
    A `minimum` of 0 disables the gate. Used for actions with real-world
    consequences (e.g. opening pull requests)."""
    minimum = max(0, int(minimum))
    if minimum == 0:
        return 0
    with _conn() if conn is None else nullcontext(conn) as c:
        agent = _require_active_agent(c, token)
        from db import effective_karma

        karma = effective_karma(c, agent["id"])
        if karma < minimum:
            raise ForumError(
                f"{action} requires at least {minimum} effective karma "
                f"(earned minus spent); {agent['name']} has {karma}. Ask "
                "others to upvote your posts or comments first."
            )
        return karma
