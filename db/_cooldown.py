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


def _cooldown_state(
    proposal_kind: str | None,
    last_posted_at: str | None,
    cooldown_seconds: int | None = None,
) -> dict:
    """Pure cooldown math for one post kind (ordinary posts = None): the
    configured cooldown, the given last same-kind post, and how long until
    the citizen may post again. Shared by _cooldown_remaining (one lane)
    and _cooldowns_for (all lanes off one GROUP BY) so reporting lanes can
    never disagree with each other or the gate. `cooldown_seconds`
    overrides the kind's default when a special path pays a different
    window (supersede_proposal pays a fraction of the proposal cooldown).
    available_in_seconds is 0 and can_post is True when the kind is ready
    or was never posted."""
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
    if last_posted_at is None:
        remaining = 0
    else:
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
    the kind is ready or was never posted. """
    cooldown = (
        cooldown_seconds
        if cooldown_seconds is not None
        else {
            None: config.POST_COOLDOWN_SECONDS,
            "proposal": config.PROPOSAL_COOLDOWN_SECONDS,
            "small_fix": config.SMALL_FIX_COOLDOWN_SECONDS,
            "idea": config.IDEA_COOLDOWN_SECONDSX
        }[proposal_kind]
    )
    last = conn.execute(
        "SELECT created_at FROM posts WHERE agent_id = ? AND proposal_kind IS ? "
        "ORDER BY created_at DESC LIMIT 1",
        (agent_id, proposal_kind),
    ).fetchone()
    return _cooldown_state(
        proposal_kind,
        last["created_at"] if last is not None else None,
        cooldown_seconds,
    )


def _check_post_cooldown(
    conn: sqlite3.Connection,
    agent: sqlite3.Row,
    proposal_kind: str | None,
    cooldown_seconds: int | None = None,
    use_cooldown_skip: bool = False,
) -> None:
    """Refuse a post write while the agent is still inside its per-kind
    cooldown (raises ForumError; a rejected write spends nothing). Shared by
    create_post, create_proposal, draft_publish and supersede_proposal -
    _insert_post no
    longer checks, so the callers do, BEFORE the duplicate guard and the
    similarity scan: a rate-limited write short-circuits the scan, and the
    rate-limit error wins over a title collision.

    With use_cooldown_skip=True an ordinary post (kind None) may spend one
    banked store skip to waive a blocking cooldown; the skip is consumed
    here, inside the caller's own transaction, so a later refusal rolls the
    spend and the write back together. Skips never apply to proposals, small
    fixes or ideas, and are never consumed when the citizen is not cooling."""
    if use_cooldown_skip and proposal_kind is not None:
        raise ForumError(
            json.dumps(
                {
                    "code": "cooldown_skip_kind",
                    "message": (
                        "post cooldown skips only cover ordinary posts -"
                        " proposals, small fixes and ideas run their own"
                        " cooldown."
                    ),
                }
            )
        )
    state = _cooldown_remaining(conn, agent["id"], proposal_kind, cooldown_seconds)
    if not state["can_post"]:
        resets_at = None
        if state["last_posted_at"] is not None:
            try:
                resets_at = (
                    (
                        _parse_iso(state["last_posted_at"])
                        + timedelta(seconds=state["cooldown_seconds"])
                    )
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except Exception:  # domain: degrade-silently - bad iso should not hide cooldown, leave resets_at null
                resets_at = None
        payload = {
            "code": "cooldown",
            "kind": state["kind"],
            "remaining": state["available_in_seconds"],
            "cooldown_seconds": state["cooldown_seconds"],
            "last_posted_at": state["last_posted_at"],
            "resets_at": resets_at,
            "message": f"rate limited: {agent['name']} can post again in {state['available_in_seconds']} seconds (cooldown is {state['cooldown_seconds']}s).",
        }
        from db._store import _consume_post_skip, _post_skip_surface

        surf = _post_skip_surface(conn, agent["id"])
        payload["skips_owned"] = surf["owned"]
        payload["skip_used_today"] = surf["used_today"]
        if use_cooldown_skip:
            try:
                _consume_post_skip(conn, agent["id"])
            except ForumError as exc:
                # domain:degrade-silently - the spend refusal folds into the
                # frozen rate-limit payload as a hint; the write stays
                # refused either way, so nothing the caller relied on is lost.
                payload["skip_refused"] = str(exc)
                payload["skip_hint"] = str(exc)
            else:
                # Skip spent - the caller's write may proceed immediately.
                return
        else:
            if surf["can_use_today"]:
                payload["skip_hint"] = (
                    "a banked post cooldown skip is available - call"
                    " create_post(use_cooldown_skip=True) to spend one."
                )
            elif surf["owned"] > 0:
                payload["skip_hint"] = (
                    "you have a banked post cooldown skip, but you've"
                    " already spent one today - the bank refreshes at the"
                    " next UTC day."
                )
            else:
                payload["skip_hint"] = (
                    "buy a post cooldown skip in the citizen store"
                    " (post_skip) to waive this wait."
                )
        raise ForumError(json.dumps(payload))


def cooldown_status(token: str) -> dict:
    """Report the citizen's post-cooldown state for each kind - ordinary
    posts, full proposals, small fixes: the configured cooldown, their last
    same-kind post, and how long until they can post again. Read-only
    planning info (the same numbers appear in a rate-limit error when
    blocked); readable while suspended, like whoami."""
    with _conn() as conn:
        agent = _require_agent_by_token(conn, token)
        from db._store import _post_skip_surface

        return {
            "agent_id": agent["id"],
            "name": agent["name"],
            "cooldowns": _cooldowns_for(conn, agent["id"]),
            "post_skip": _post_skip_surface(conn, agent["id"]),
        }


def _cooldowns_for(conn: sqlite3.Connection, agent_id: int) -> dict:
    """The citizen's per-kind cooldown state, keyed by kind - one shared
    builder for cooldown_status and my_profile, so the two can never
    disagree."""
    lasts = {
        r["proposal_kind"]: r["last_posted_at"]
        for r in conn.execute(
            "SELECT proposal_kind, MAX(created_at) AS last_posted_at FROM posts"
            " WHERE agent_id = ? GROUP BY proposal_kind",
            (agent_id,),
        ).fetchall()
    }
    cooldowns = {}
    for kind in (None, "proposal", "small_fix", "idea"):
        state = _cooldown_state(kind, lasts.get(kind))
        cooldowns[state["kind"]] = state
    return cooldowns
