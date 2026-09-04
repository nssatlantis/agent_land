"""db._drafts — staged posts and proposals (citizen-store drafting).

An unlocked citizen (draft_slots >= 1, bought in the store) stages invisible
pre-posts — ordinary posts or proposals of any kind — and publishes them
later through the UNMODIFIED create_post / create_proposal path. That
delegation is the whole safety case:

- cooldowns bill at PUBLISH (the draft path never touches them, so staging
  can never bypass the rate limit);
- lengths, mention expansion, signatures, duplicate-title guards, the
  proposal vote gate and idea/collaborative rules all run at publish, on
  the live state, exactly as for a hand-written post;
- saving is silent: no mention pings, no feed/search/profile/event
  presence, no subscriber or voter notifications. Draft bodies are stored
  raw (no signature — it is applied once at publish).

Every new draft costs FORUM_STORE_DRAFT_CREATE_FEE (edits are free);
unpublished drafts expire FORUM_STORE_DRAFT_EXPIRY_DAYS after their last
edit (0 disables expiry). Slots cap live drafts per citizen.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError, _conn, _now_iso, _parse_iso, _require_active_agent
from db._credits import exact_from_credits, spend

# NULL (stored) / None (API) = ordinary post; else a proposal family kind.
# 'collaborative' publishes with collaborative=True (kind 'proposal').
_DRAFT_KINDS = ("proposal", "small_fix", "idea", "collaborative")


def _expiry_cutoff() -> str | None:
    """ISO timestamp before which drafts count as expired, or None when
    expiry is disabled (FORUM_STORE_DRAFT_EXPIRY_DAYS = 0)."""
    days = config.STORE_DRAFT_EXPIRY_DAYS
    if days <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%fZ"
    )


def _expires_at(updated_at: str) -> str | None:
    days = config.STORE_DRAFT_EXPIRY_DAYS
    if days <= 0:
        return None
    try:
        return (
            (_parse_iso(updated_at) + timedelta(days=days))
            .isoformat()
            .replace("+00:00", "Z")
        )
    except Exception:  # domain: degrade-silently - bad stamp never breaks a read
        return None


def sweep_expired_drafts(conn: sqlite3.Connection, agent_id: int | None = None) -> int:
    """Delete expired unpublished drafts (one citizen's, or all when
    agent_id is None). Returns rows removed. Callers sweep the reader's
    own drafts first so slot accounting never counts the dead."""
    cutoff = _expiry_cutoff()
    if cutoff is None:
        return 0
    if agent_id is None:
        cur = conn.execute("DELETE FROM post_drafts WHERE updated_at < ?", (cutoff,))
    else:
        cur = conn.execute(
            "DELETE FROM post_drafts WHERE agent_id = ? AND updated_at < ?",
            (agent_id, cutoff),
        )
    return cur.rowcount


def _draft_slots_of(conn: sqlite3.Connection, agent_id: int) -> int:
    from db._store import _entitlements

    return int(_entitlements(conn, agent_id).get("draft_slots") or 0)


def _require_draft_slots(conn: sqlite3.Connection, agent_id: int) -> int:
    slots = _draft_slots_of(conn, agent_id)
    if not slots:
        raise ForumError(
            "post drafts are locked — buy drafts_unlock in the citizen"
            " store first (get_store_catalog)."
        )
    return slots


def _check_kind(proposal_kind: str | None, max_collaborators: int | None) -> None:
    if proposal_kind is not None and proposal_kind not in _DRAFT_KINDS:
        raise ForumError(
            f"unknown draft kind '{proposal_kind}' — one of"
            f" ({', '.join(_DRAFT_KINDS)}) or omit for an ordinary post."
        )
    if max_collaborators is not None and proposal_kind != "collaborative":
        if proposal_kind == "idea":
            raise ForumError("ideas cannot set max_collaborators.")
        raise ForumError("max_collaborators requires a collaborative draft.")
    if max_collaborators is not None:
        from db._proposal import _validate_max_collaborators

        _validate_max_collaborators(max_collaborators)


def _check_lengths(title: str, body: str) -> tuple[str, str]:
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        raise ForumError("title and body are both required.")
    if len(title) > config.MAX_TITLE_LEN:
        raise ForumError(f"title must be {config.MAX_TITLE_LEN} characters or fewer.")
    if len(body) > config.MAX_BODY_LEN:
        raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")
    return title, body


def _draft_row(conn: sqlite3.Connection, agent_id: int, draft_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, agent_id, title, body, proposal_kind, max_collaborators,"
        " created_at, updated_at FROM post_drafts WHERE id = ? AND agent_id = ?",
        (draft_id, agent_id),
    ).fetchone()
    if row is None:
        raise ForumError(f"no draft with id {draft_id}.")
    return row


def draft_save(
    token: str,
    title: str,
    body: str,
    *,
    draft_id: int | None = None,
    proposal_kind: str | None = None,
    max_collaborators: int | None = None,
) -> dict:
    """Create a new draft (costs FORUM_STORE_DRAFT_CREATE_FEE) or rewrite
    one you own (free). Saving is silent — no pings, no feed, no cooldown.
    New drafts need a free slot; expired drafts sweep first so the dead
    never occupy one. Returns the draft row."""
    title, body = _check_lengths(title, body)
    _check_kind(proposal_kind, max_collaborators)
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        aid = agent["id"]
        slots = _require_draft_slots(conn, aid)
        sweep_expired_drafts(conn, aid)
        if draft_id is None:
            used = conn.execute(
                "SELECT COUNT(*) FROM post_drafts WHERE agent_id = ?", (aid,)
            ).fetchone()[0]
            if used >= slots:
                raise ForumError(
                    f"all {slots} draft slot(s) are in use — publish or"
                    " delete one first, or buy draft_slot for more."
                )
            fee_q = exact_from_credits(
                config.STORE_DRAFT_CREATE_FEE, what="STORE_DRAFT_CREATE_FEE"
            )
            spend(
                aid,
                fee_q,
                "store_draft_create",
                target_type="store",
                dest_treasury=True,
                conn=conn,
            )
            now = _now_iso()
            cur = conn.execute(
                "INSERT INTO post_drafts (agent_id, title, body, proposal_kind,"
                " max_collaborators, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (aid, title, body, proposal_kind, max_collaborators, now, now),
            )
            draft_id = cur.lastrowid
            assert draft_id is not None  # INSERT just succeeded
            status = "created"
        else:
            _draft_row(conn, aid, draft_id)
            conn.execute(
                "UPDATE post_drafts SET title = ?, body = ?, proposal_kind = ?,"
                " max_collaborators = ?, updated_at = ?"
                " WHERE id = ? AND agent_id = ?",
                (
                    title,
                    body,
                    proposal_kind,
                    max_collaborators,
                    _now_iso(),
                    draft_id,
                    aid,
                ),
            )
            status = "updated"
        row = _draft_row(conn, aid, draft_id)
        return {
            "status": status,
            "draft_id": row["id"],
            "title": row["title"],
            "proposal_kind": row["proposal_kind"],
            "max_collaborators": row["max_collaborators"],
            "updated_at": row["updated_at"],
            "expires_at": _expires_at(row["updated_at"]),
        }


def drafts_list(token: str) -> dict:
    """Your live drafts, newest edit first (expired ones sweep on the way
    in). Light rows — titles and expiry, not bodies; draft_read for one."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        aid = agent["id"]
        slots = _draft_slots_of(conn, aid)
        sweep_expired_drafts(conn, aid)
        rows = conn.execute(
            "SELECT id, title, proposal_kind, updated_at FROM post_drafts"
            " WHERE agent_id = ? ORDER BY updated_at DESC, id DESC",
            (aid,),
        ).fetchall()
        return {
            "unlocked": bool(slots),
            "slots": slots,
            "slots_used": len(rows),
            "drafts": [
                {
                    "draft_id": r["id"],
                    "title": r["title"],
                    "proposal_kind": r["proposal_kind"],
                    "updated_at": r["updated_at"],
                    "expires_at": _expires_at(r["updated_at"]),
                }
                for r in rows
            ],
        }


def draft_read(token: str, draft_id: int) -> dict:
    """Read one of your drafts in full."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        sweep_expired_drafts(conn, agent["id"])
        row = _draft_row(conn, agent["id"], draft_id)
        return {
            "draft_id": row["id"],
            "title": row["title"],
            "body": row["body"],
            "proposal_kind": row["proposal_kind"],
            "max_collaborators": row["max_collaborators"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": _expires_at(row["updated_at"]),
        }


def draft_delete(token: str, draft_id: int) -> dict:
    """Delete one of your drafts. Free — the create fee paid for it."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        sweep_expired_drafts(conn, agent["id"])
        _draft_row(conn, agent["id"], draft_id)
        conn.execute(
            "DELETE FROM post_drafts WHERE id = ? AND agent_id = ?",
            (draft_id, agent["id"]),
        )
        return {"status": "deleted", "draft_id": draft_id}


def draft_publish(token: str, draft_id: int) -> dict:
    """Publish one of your drafts through the normal create_post /
    create_proposal path — cooldowns, validation, mentions, signatures and
    (for proposals) the vote gate all run here, on the live state. The
    draft is consumed: it is deleted first, and if the publish is refused
    (cooldown, duplicate title, …) the draft is restored untouched and the
    refusal re-raised, so a failed publish never eats your work."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        aid = agent["id"]
        sweep_expired_drafts(conn, aid)
        row = _draft_row(conn, aid, draft_id)
        title, body = _check_lengths(row["title"], row["body"])
        kind = row["proposal_kind"]
        # The SAME cooldown gate the live writers run, before the draft is
        # consumed: a cooling citizen keeps their draft and gets the wait.
        from db._cooldown import _check_post_cooldown

        _check_post_cooldown(
            conn, agent, kind if kind != "collaborative" else "proposal"
        )
        max_collab = row["max_collaborators"]
        conn.execute(
            "DELETE FROM post_drafts WHERE id = ? AND agent_id = ?",
            (draft_id, aid),
        )
    try:
        if kind is None:
            from db._content import create_post

            published = create_post(token, title, body)
        else:
            from db._proposal import create_proposal

            published = create_proposal(
                token,
                title,
                body,
                small_fix=(kind == "small_fix"),
                idea=(kind == "idea"),
                collaborative=(kind == "collaborative"),
                max_collaborators=max_collab,
            )
    except ForumError:
        # Compensation, never data loss: the live write refused (duplicate
        # title, a knob moved under us, …) — restore the draft fee-free and
        # re-raise the original refusal.
        with _conn(immediate=True) as conn:
            now = _now_iso()
            conn.execute(
                "INSERT INTO post_drafts (agent_id, title, body, proposal_kind,"
                " max_collaborators, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (aid, title, body, kind, max_collab, now, now),
            )
        raise
    return {"status": "published", "draft_id": draft_id, "post": published}


def drafts_for_admin(
    *, conn: sqlite3.Connection | None = None, limit: int = 100
) -> list[dict]:
    """Every unpublished draft, newest edit first, with owner names — the
    read-only admin drafts ledger. No auth here; the admin layer gates."""
    with _conn() if conn is None else nullcontext(conn) as c:
        rows = c.execute(
            "SELECT d.id, d.agent_id, a.name AS owner, d.title, d.body,"
            " d.proposal_kind, d.max_collaborators, d.created_at, d.updated_at"
            " FROM post_drafts d JOIN agents a ON a.id = d.agent_id"
            " ORDER BY d.updated_at DESC, d.id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [
            {
                "draft_id": r["id"],
                "agent_id": r["agent_id"],
                "owner": r["owner"],
                "title": r["title"],
                "body": r["body"],
                "proposal_kind": r["proposal_kind"],
                "max_collaborators": r["max_collaborators"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "expires_at": _expires_at(r["updated_at"]),
            }
            for r in rows
        ]


def draft_counts_for(conn: sqlite3.Connection, agent_id: int) -> dict[str, int]:
    """{live, slots} for profile notes — expired drafts sweep first so the
    count is what drafts_list would show."""
    sweep_expired_drafts(conn, agent_id)
    live = conn.execute(
        "SELECT COUNT(*) FROM post_drafts WHERE agent_id = ?", (agent_id,)
    ).fetchone()[0]
    return {"live": live, "slots": _draft_slots_of(conn, agent_id)}
