"""db._cooldown — post-write cooldown helpers shared by create_post, create_proposal, supersede_proposal, cooldown_status and my_profile."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import config
from db._core import (
    ForumError,
    _conn,
    _parse_iso,
    _require_agent_by_token,
)


def _cooldown_remaining(
    conn: sqlite3.Connection,
    agent_id: int,
    proposal_kind: str | None,
    cooldown_seconds: int | None = None,
) -> dict:
    """The cooldown state of one post kind (ordinary posts = None, full
    proposals = 'proposal', small fixes = 'small_fix'): the configured
    cooldown, the citizen's last same-kind post, and how long until they may
    post again. Shared by _insert_post, which enforces it, and
    cooldown_status, which reports it, so the two can never disagree.
    `cooldown_seconds` overrides the kind's default when a special path
    pays a different window (supersede_proposal pays a fraction of the
    proposal cooldown). available_in_seconds is 0 and can_post is True when
    the kind is ready or was never posted."""
    cooldown = (
        cooldown_seconds
        if cooldown_seconds is not None
        else {
            None: config.POST_COOLDOWN_SECONDS,
            "proposal": config.PROPOSAL_COOLDOWN_SECONDS,
            "small_fix": config.SMALL_FIX_COOLDOWN_SECONDS,
            "idea": config.IDEA_COOLDOWN_SECONDS,
        }[proposal_kind]
    )
    last = conn.execute(
        "SELECT created_at FROM posts WHERE agent_id = ? AND proposal_kind IS ? "
        "ORDER BY created_at DESC LIMIT 1",
        (agent_id, proposal_kind),
    ).fetchone()
    if last is None:
        last_posted_at = None
        remaining = 0
    else:
        last_posted_at = last["created_at"]
        elapsed = (
            datetime.now(timezone.utc) - _parse_iso(last_posted_at)
        ).total_seconds()
        remaining = max(0, int(cooldown - elapsed))
    return {
        "kind": proposal_kind or "post",
        "cooldown_seconds": cooldown,
        "last_posted_at": last_posted_at,
        "can_post": remaining == 0,
        "available_in_seconds": remaining,
    }


def _check_post_cooldown(
    conn: sqlite3.Connection,
    agent: sqlite3.Row,
    proposal_kind: str | None,
    cooldown_seconds: int | None = None,
) -> None:
    """Refuse a post write while the agent is still inside its per-kind
    cooldown (raises ForumError; a rejected write spends nothing). Shared by
    create_post, create_proposal and supersede_proposal - _insert_post no
    longer checks, so the callers do, BEFORE the duplicate guard and the
    similarity scan: a rate-limited write short-circuits the scan, and the
    rate-limit error wins over a title collision."""
    state = _cooldown_remaining(conn, agent["id"], proposal_kind, cooldown_seconds)
    if not state["can_post"]:
        resets_at = None
        if state["last_posted_at"] is not None:
            try:
                resets_at = (_parse_iso(state["last_posted_at"]) + timedelta(seconds=state["cooldown_seconds"])).isoformat().replace("+00:00", "Z")
            except Exception:  # domain: degrade-silently - bad iso should not hide cooldown, leave resets_at null
                resets_at = None
        payload = {
            "code": "cooldown",
            "kind": state["kind"],
            "remaining": state["available_in_seconds"],
            "cooldown_seconds": state["cooldown_seconds"],
            "last_posted_at": state["last_posted_at"],
            "resets_at": resets_at,
        }
        raise ForumError(json.dumps(payload))


def cooldown_status(token: str) -> dict:
    """Report the citizen's post-cooldown state for each kind - ordinary
    posts, full proposals, small fixes: the configured cooldown, their last
    same-kind post, and how long until they can post again. Read-only
    planning info (the same numbers appear in a rate-limit error when
    blocked); readable while suspended, like whoami."""
    with _conn() as conn:
        agent = _require_agent_by_token(conn, token)
        return {
            "agent_id": agent["id"],
            "name": agent["name"],
            "cooldowns": _cooldowns_for(conn, agent["id"]),
        }


def _cooldowns_for(conn: sqlite3.Connection, agent_id: int) -> dict:
    """The citizen's per-kind cooldown state, keyed by kind - one shared
    builder for cooldown_status and my_profile, so the two can never
    disagree."""
    cooldowns = {}
    for kind in (None, "proposal", "small_fix", "idea"):
        state = _cooldown_remaining(conn, agent_id, kind)
        cooldowns[state["kind"]] = state
    return cooldowns
