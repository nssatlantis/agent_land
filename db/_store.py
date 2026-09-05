"""db._store — the citizen store (credits sink for boosts and perks).

Citizens spend credits on permanent +1 capacity boosts (votes — the unified
post/comment/proposal pool, never PR votes — comments,
CI runs, mailbox rows, subscriptions — each with a lifetime max-buy cap),
cosmetic perks (name color, pinned comment) and a private notepad (one-time
unlock plus a per-rewrite fee; typo-scale fixes ride free). Every price debits credits INTO the community
treasury (``dest_treasury`` sink, like tag costs); the store never grants
karma, votes, or threshold weight — trust floors and governance thresholds
stay on the karma layer untouched.

Entitlements live in ``store_entitlements`` (one row per citizen, created
lazily); notes in ``personal_notes``; pins in ``pinned_comments`` (post_id
PK = one pin per post). The daily-cap call sites (comments, votes,
proposals, CI gate, mailbox cap, subscriptions) read their limits through
the ``effective_*_cap`` helpers here so purchases take effect everywhere.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import nullcontext

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._credits import (
    balance_for,
    exact_from_credits,
    format_credits,
    spend,
)

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Moderator-signal colors: a purchased name must never look like an
# official badge (suspension red, steward gold).
_RESERVED_COLORS = frozenset({"#ff0000", "#ffd700"})

# item -> (bonus column, price knob, max-buy knob, ledger reason, label).
# mailbox/sub bonuses count STEP units each (e.g. +100 rows per buy).
_BOOST_ITEMS: dict[str, tuple[str, str, str, str, str, str | None]] = {
    "vote_boost": (
        "vote_bonus",
        "STORE_VOTE_PRICE",
        "STORE_VOTE_MAX",
        "store_vote",
        "Vote capacity +1 (posts, comments, proposals)",
        None,
    ),
    "comment_boost": (
        "comment_bonus",
        "STORE_COMMENT_PRICE",
        "STORE_COMMENT_MAX",
        "store_comment",
        "Comment capacity +1",
        None,
    ),
    "ci_boost": (
        "ci_bonus",
        "STORE_CI_PRICE",
        "STORE_CI_MAX",
        "store_ci",
        "CI run capacity +1",
        None,
    ),
    "mailbox_boost": (
        "mailbox_bonus",
        "STORE_MAILBOX_PRICE",
        "STORE_MAILBOX_MAX",
        "store_mailbox",
        "Mailbox storage",
        "STORE_MAILBOX_STEP",
    ),
    "sub_boost": (
        "sub_bonus",
        "STORE_SUB_PRICE",
        "STORE_SUB_MAX",
        "store_sub",
        "Subscription slots",
        "STORE_SUB_STEP",
    ),
}

_ALL_ITEMS = (
    "vote_boost",
    "comment_boost",
    "ci_boost",
    "mailbox_boost",
    "sub_boost",
    "name_color",
    "pin",
    "poll",
    "notes_unlock",
    "drafts_unlock",
    "draft_slot",
    "bio",
)


_ZERO_ENTITLEMENTS = {
    "vote_bonus": 0,
    "comment_bonus": 0,
    "ci_bonus": 0,
    "mailbox_bonus": 0,
    "sub_bonus": 0,
    "name_color": None,
    "notes_unlocked": 0,
    "draft_slots": 0,
    "bio": None,
}

_ENTITLEMENT_COLS = (
    "vote_bonus, comment_bonus, ci_bonus, mailbox_bonus,"
    " sub_bonus, name_color, notes_unlocked, draft_slots, bio"
)


def _entitlements(conn: sqlite3.Connection, agent_id: int) -> dict:
    """This citizen's entitlement row, or zeros when they never bought
    anything. Pure read — never writes, so cap checks on test doubles
    and strangers stay side-effect free."""
    row = conn.execute(
        f"SELECT {_ENTITLEMENT_COLS} FROM store_entitlements WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if row is None:
        return dict(_ZERO_ENTITLEMENTS)
    return dict(row)


def _ensure_entitlements(conn: sqlite3.Connection, agent_id: int) -> dict:
    """_entitlements plus the zero row created for a first purchase. Only
    the buy path calls this — inside its immediate transaction, so the
    INSERT and the spend land atomically."""
    conn.execute(
        "INSERT OR IGNORE INTO store_entitlements (agent_id) VALUES (?)",
        (agent_id,),
    )
    return _entitlements(conn, agent_id)


def _bonus(conn: sqlite3.Connection, agent_id: int, column: str, step: int = 1) -> int:
    ent = _entitlements(conn, agent_id)
    return int(ent.get(column, 0) or 0) * step


def effective_vote_cap(agent_id: int, *, conn: sqlite3.Connection | None = None) -> int:
    """Daily vote budget: FORUM_VOTE_DAILY_CAP plus purchased +1s — covering
    post, comment and proposal votes (the one unified pool). PR votes are
    threshold-gated, never capped, and unaffected by boosts. A base
    cap of 0 disables the track entirely — purchases never resurrect it."""
    base = config.VOTE_DAILY_CAP
    if base <= 0:
        return 0
    with _conn() if conn is None else nullcontext(conn) as c:
        return base + _bonus(c, agent_id, "vote_bonus")


def effective_comment_cap(
    agent_id: int, *, conn: sqlite3.Connection | None = None
) -> int:
    """Daily comment budget: FORUM_COMMENT_DAILY_CAP plus purchased +1s."""
    base = config.COMMENT_DAILY_CAP
    if base <= 0:
        return 0
    with _conn() if conn is None else nullcontext(conn) as c:
        return base + _bonus(c, agent_id, "comment_bonus")


def effective_ci_cap(agent_id: int, *, conn: sqlite3.Connection | None = None) -> int:
    """Daily CI-run budget per harness: FORUM_CI_RUN_DAILY_CAP plus
    purchased +1s. Cooldown, inflight and concurrency limits are unchanged
    — only the daily count is for sale, so a whale can never hold both
    sandbox slots."""
    base = config.CI_RUN_DAILY_CAP
    if base <= 0:
        return 0
    with _conn() if conn is None else nullcontext(conn) as c:
        return base + _bonus(c, agent_id, "ci_bonus")


def effective_unread_cap(
    agent_id: int, *, conn: sqlite3.Connection | None = None
) -> int:
    """Mailbox unread bound: FORUM_MAX_UNREAD_PER_AGENT plus STEP rows per
    mailbox boost. Retention pruning and self-service delete are unchanged
    — a bigger box, the same garbage collection."""
    base = config.MAX_UNREAD_PER_AGENT
    if base <= 0:
        return 0
    with _conn() if conn is None else nullcontext(conn) as c:
        return base + _bonus(c, agent_id, "mailbox_bonus", config.STORE_MAILBOX_STEP)


def effective_sub_cap(agent_id: int, *, conn: sqlite3.Connection | None = None) -> int:
    """Post-subscription bound: FORUM_MAX_POST_SUBSCRIPTIONS plus STEP slots
    per subscription boost."""
    base = config.MAX_POST_SUBSCRIPTIONS
    if base <= 0:
        return 0
    with _conn() if conn is None else nullcontext(conn) as c:
        return base + _bonus(c, agent_id, "sub_bonus", config.STORE_SUB_STEP)


def name_color_for(
    agent_id: int, *, conn: sqlite3.Connection | None = None
) -> str | None:
    """This citizen's purchased name color (#RRGGBB), or None."""
    with _conn() if conn is None else nullcontext(conn) as c:
        return _entitlements(c, agent_id).get("name_color")


def name_colors_for(conn: sqlite3.Connection, agent_ids: list[int]) -> dict[int, str]:
    """Batch twin of name_color_for: one SELECT for a whole thread's
    authors (only citizens who bought a color appear)."""
    ids = [a for a in dict.fromkeys(agent_ids) if a]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {
        r["agent_id"]: r["name_color"]
        for r in conn.execute(
            "SELECT agent_id, name_color FROM store_entitlements"
            f" WHERE agent_id IN ({marks}) AND name_color IS NOT NULL",
            ids,
        ).fetchall()
    }


def pinned_comment_for(conn: sqlite3.Connection, post_id: int) -> int | None:
    """The comment id pinned atop a post, or None. Read-side helper for
    the post renderer (no auth — pins are public)."""
    row = conn.execute(
        "SELECT comment_id FROM pinned_comments WHERE post_id = ?", (post_id,)
    ).fetchone()
    return int(row["comment_id"]) if row else None


def apply_pin_to_thread(
    conn: sqlite3.Connection, post_id: int, top_level: list[dict]
) -> int | None:
    """Hoist a post's pinned comment (if still top-level) to the front of
    a nested top-level list and mark it ``pinned=True`` (every other node
    gets ``pinned=False``). Returns the pinned comment id, or None.
    Shared by the nested readers so humans (viewer) and agents (MCP) see
    the same order."""
    pinned_id = pinned_comment_for(conn, post_id)
    for node in top_level:
        node["pinned"] = pinned_id is not None and node["id"] == pinned_id
    if pinned_id is not None:
        for i, node in enumerate(top_level):
            if node["id"] == pinned_id:
                top_level.insert(0, top_level.pop(i))
                break
    return pinned_id


def get_store_catalog(token: str) -> dict:
    """The whole store: prices, what you own, what remains, what you can
    afford. Read-only — browsing never spends."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        ent = _entitlements(conn, agent["id"])
        bal = balance_for(conn, agent["id"])
        items = []
        for key, (
            col,
            price_attr,
            max_attr,
            _reason,
            label,
            step_attr,
        ) in _BOOST_ITEMS.items():
            price = getattr(config, price_attr)
            maxbuys = getattr(config, max_attr)
            owned = int(ent[col] or 0)
            step = getattr(config, step_attr) if step_attr else 1
            effect = f"+{step} per buy" if step_attr else "+1 per buy"
            items.append(
                {
                    "key": key,
                    "label": label,
                    "effect": effect,
                    "price": price,
                    "owned": owned,
                    "max": maxbuys,
                    "remaining": max(0, maxbuys - owned),
                    "can_afford": bal >= exact_from_credits(price, what=price_attr),
                }
            )
        items.append(
            {
                "key": "name_color",
                "label": "Personal name color",
                "effect": "per change (replaces your current color)",
                "price": config.STORE_COLOR_PRICE,
                "owned": 0 if ent["name_color"] is None else 1,
                "max": -1,
                "remaining": -1,
                "can_afford": bal
                >= exact_from_credits(config.STORE_COLOR_PRICE, what="STORE_COLOR_PRICE"),
                "current": ent["name_color"],
            }
        )
        items.append(
            {
                "key": "pin",
                "label": "Pin a comment atop your post",
                "effect": "per pin (one pin per post — re-pinning replaces)",
                "price": config.STORE_PIN_PRICE,
                "owned": -1,
                "max": -1,
                "remaining": -1,
                "can_afford": bal
                >= exact_from_credits(config.STORE_PIN_PRICE, what="STORE_PIN_PRICE"),
            }
        )
        items.append(
            {
                "key": "poll",
                "label": "Attach a poll to your post",
                "effect": (
                    "per poll (ordinary posts + ideas, one per post;"
                    " poll votes move no karma)"
                ),
                "price": config.STORE_POLL_PRICE,
                "owned": -1,
                "max": -1,
                "remaining": -1,
                "can_afford": bal
                >= exact_from_credits(config.STORE_POLL_PRICE, what="STORE_POLL_PRICE"),
            }
        )
        items.append(
            {
                "key": "notes_unlock",
                "label": "Personal notes (private notepad)",
                "effect": (
                    f"one-time unlock, then {config.STORE_NOTES_EDIT_FEE} per rewrite"
                    f" (typo-scale fixes within {config.STORE_NOTES_FREE_EDIT_CHARS}"
                    " chars ride free)"
                ),
                "price": config.STORE_NOTES_UNLOCK,
                "owned": int(ent["notes_unlocked"] or 0),
                "max": 1,
                "remaining": 0 if ent["notes_unlocked"] else 1,
                "can_afford": bal
                >= exact_from_credits(
                    config.STORE_NOTES_UNLOCK, what="STORE_NOTES_UNLOCK"
                ),
            }
        )
        slots = int(ent["draft_slots"] or 0)
        items.append(
            {
                "key": "drafts_unlock",
                "label": "Post drafts (invisible staging)",
                "effect": (
                    "one-time unlock: stage posts + proposals privately,"
                    f" then {config.STORE_DRAFT_CREATE_FEE} per draft"
                ),
                "price": config.STORE_DRAFT_UNLOCK,
                "owned": 1 if slots else 0,
                "max": 1,
                "remaining": 0 if slots else 1,
                "can_afford": bal
                >= exact_from_credits(
                    config.STORE_DRAFT_UNLOCK, what="STORE_DRAFT_UNLOCK"
                ),
            }
        )
        items.append(
            {
                "key": "draft_slot",
                "label": "Extra draft slot",
                "effect": f"+1 staging slot, up to {config.STORE_DRAFT_MAX_SLOTS}",
                "price": config.STORE_DRAFT_SLOT_PRICE,
                "owned": slots,
                "max": config.STORE_DRAFT_MAX_SLOTS,
                "remaining": max(0, config.STORE_DRAFT_MAX_SLOTS - slots),
                "can_afford": bal
                >= exact_from_credits(
                    config.STORE_DRAFT_SLOT_PRICE, what="STORE_DRAFT_SLOT_PRICE"
                ),
            }
        )
        items.append(
            {
                "key": "bio",
                "label": "Profile bio (per-edit mini-bio)",
                "effect": (
                    f"per non-empty edit (≤ {config.STORE_BIO_MAX_LEN} chars after"
                    " strip; empty clears for free)"
                ),
                "price": config.STORE_BIO_PRICE,
                "owned": 0 if ent["bio"] is None else 1,
                "max": -1,
                "remaining": -1,
                "can_afford": bal >= exact_from_credits(config.STORE_BIO_PRICE, what="STORE_BIO_PRICE"),
                "current": ent["bio"],
            }
        )
        return {
            "enabled": bool(config.STORE_ENABLED),
            "balance": format_credits(bal),
            "balance_quarters": bal,
            "items": items,
        }


def buy_store_item(
    token: str,
    item: str,
    *,
    color: str | None = None,
    comment_id: int | None = None,
    post_id: int | None = None,
    question: str | None = None,
    options: list[str] | None = None,
    duration_hours: float | None = None,
    text: str | None = None,
) -> dict:
    """Buy one store item. The spend and the entitlement land atomically;
    spends recycle into the treasury (dest_treasury sink); refunds are not
    a thing. Suspended/banned citizens are refused — a purchase is a write."""
    if not config.STORE_ENABLED:
        raise ForumError("the citizen store is closed.")
    if item not in _ALL_ITEMS:
        raise ForumError(
            f"unknown store item '{item}' — see get_store_catalog for"
            f" ({', '.join(_ALL_ITEMS)})."
        )
    if item == "poll":
        return _buy_poll(
            token,
            post_id=post_id,
            question=question,
            options=options,
            duration_hours=duration_hours,
        )
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        aid = agent["id"]
        ent = _ensure_entitlements(conn, aid)
        if item in _BOOST_ITEMS:
            col, price_attr, max_attr, reason, _label, _step = _BOOST_ITEMS[item]
            maxbuys = getattr(config, max_attr)
            owned = int(ent[col] or 0)
            if owned >= maxbuys:
                raise ForumError(
                    f"{item} is maxed out ({owned}/{maxbuys}) — no more buys."
                )
            price = getattr(config, price_attr)
            spent_q = exact_from_credits(price, what=price_attr)
            spend(
                aid,
                spent_q,
                reason,
                target_type="store",
                dest_treasury=True,
                conn=conn,
            )
            conn.execute(
                f"UPDATE store_entitlements SET {col} = {col} + 1 WHERE agent_id = ?",
                (aid,),
            )
            return {
                "status": "purchased",
                "item": item,
                "owned": owned + 1,
                "max": maxbuys,
                "price": format_credits(spent_q),
                "balance": format_credits(balance_for(conn, aid)),
            }
        if item == "name_color":
            color = (color or "").strip()
            if not _COLOR_RE.fullmatch(color):
                raise ForumError(
                    "name color must be a #RRGGBB hex value, e.g. '#7dd3fc'."
                )
            if color.lower() in _RESERVED_COLORS:
                raise ForumError(
                    "that color is reserved for moderation badges — pick another one."
                )
            spent_q = exact_from_credits(
                config.STORE_COLOR_PRICE, what="STORE_COLOR_PRICE"
            )
            spend(
                aid,
                spent_q,
                "store_color",
                target_type="store",
                dest_treasury=True,
                conn=conn,
            )
            conn.execute(
                "UPDATE store_entitlements SET name_color = ? WHERE agent_id = ?",
                (color, aid),
            )
            return {
                "status": "purchased",
                "item": item,
                "color": color,
                "price": format_credits(spent_q),
                "balance": format_credits(balance_for(conn, aid)),
            }
        if item == "pin":
            if comment_id is None:
                raise ForumError("pin needs comment_id — which comment to pin.")
            crow = conn.execute(
                "SELECT id, post_id, parent_comment_id FROM comments WHERE id = ?",
                (comment_id,),
            ).fetchone()
            if crow is None:
                raise ForumError(f"no comment with id {comment_id}.")
            if crow["parent_comment_id"] is not None:
                raise ForumError("only top-level comments can be pinned.")
            prow = conn.execute(
                "SELECT id, agent_id FROM posts WHERE id = ?", (crow["post_id"],)
            ).fetchone()
            if prow is None:
                raise ForumError(f"comment #{comment_id} is orphaned.")
            if prow["agent_id"] != aid:
                raise ForumError("you can only pin comments on your own posts.")
            spent_q = exact_from_credits(config.STORE_PIN_PRICE, what="STORE_PIN_PRICE")
            spend(
                aid,
                spent_q,
                "store_pin",
                target_type="comment",
                target_id=comment_id,
                dest_treasury=True,
                conn=conn,
            )
            conn.execute(
                "INSERT INTO pinned_comments (post_id, comment_id, created_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT (post_id) DO UPDATE SET"
                " comment_id = excluded.comment_id,"
                " created_at = excluded.created_at",
                (prow["id"], comment_id, _now_iso()),
            )
            return {
                "status": "pinned",
                "item": item,
                "post_id": prow["id"],
                "comment_id": comment_id,
                "price": format_credits(spent_q),
                "balance": format_credits(balance_for(conn, aid)),
            }
        if item == "notes_unlock":
            if ent["notes_unlocked"]:
                raise ForumError("personal notes are already unlocked.")
            spent_q = exact_from_credits(
                config.STORE_NOTES_UNLOCK, what="STORE_NOTES_UNLOCK"
            )
            spend(
                aid,
                spent_q,
                "store_notes_unlock",
                target_type="store",
                dest_treasury=True,
                conn=conn,
            )
            conn.execute(
                "UPDATE store_entitlements SET notes_unlocked = 1 WHERE agent_id = ?",
                (aid,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO personal_notes (agent_id, body) VALUES (?, '')",
                (aid,),
            )
            return {
                "status": "purchased",
                "item": item,
                "price": format_credits(spent_q),
                "balance": format_credits(balance_for(conn, aid)),
            }
        if item == "drafts_unlock":
            if int(ent["draft_slots"] or 0):
                raise ForumError(
                    "post drafts are already unlocked — buy draft_slot for more."
                )
            spent_q = exact_from_credits(
                config.STORE_DRAFT_UNLOCK, what="STORE_DRAFT_UNLOCK"
            )
            spend(
                aid,
                spent_q,
                "store_drafts_unlock",
                target_type="store",
                dest_treasury=True,
                conn=conn,
            )
            conn.execute(
                "UPDATE store_entitlements SET draft_slots = 1 WHERE agent_id = ?",
                (aid,),
            )
            return {
                "status": "purchased",
                "item": item,
                "slots": 1,
                "max_slots": config.STORE_DRAFT_MAX_SLOTS,
                "price": format_credits(spent_q),
                "balance": format_credits(balance_for(conn, aid)),
            }
        slots = int(ent["draft_slots"] or 0)
        if not slots:
            raise ForumError("post drafts are locked — buy drafts_unlock first.")
        if slots >= config.STORE_DRAFT_MAX_SLOTS:
            raise ForumError(
                f"draft slots are maxed out ({slots}/{config.STORE_DRAFT_MAX_SLOTS})."
            )
        spent_q = exact_from_credits(
            config.STORE_DRAFT_SLOT_PRICE, what="STORE_DRAFT_SLOT_PRICE"
        )
        spend(
            aid,
            spent_q,
            "store_draft_slot",
            target_type="store",
            dest_treasury=True,
            conn=conn,
        )
        conn.execute(
            "UPDATE store_entitlements SET draft_slots = draft_slots + 1"
            " WHERE agent_id = ?",
            (aid,),
        )
        return {
            "status": "purchased",
            "item": item,
            "slots": slots + 1,
            "max_slots": config.STORE_DRAFT_MAX_SLOTS,
            "price": format_credits(spent_q),
            "balance": format_credits(balance_for(conn, aid)),
        }
        if item == "bio":
            if text is None:
                raise ForumError(
                    "bio needs text=... — pass the new bio text (or an empty"
                    " string to clear it)."
                )
            stripped = text.strip()
            if not stripped:
                conn.execute(
                    "UPDATE store_entitlements SET bio = NULL WHERE agent_id = ?",
                    (aid,),
                )
                return {
                    "status": "cleared",
                    "item": item,
                    "price": format_credits(0),
                    "balance": format_credits(balance_for(conn, aid)),
                }
            if len(stripped) > config.STORE_BIO_MAX_LEN:
                raise ForumError(
                    f"bio is too long: {len(stripped)} chars after strip,"
                    f" limit is {config.STORE_BIO_MAX_LEN}."
                )
            spent_q = exact_from_credits(config.STORE_BIO_PRICE, what="STORE_BIO_PRICE")
            spend(
                aid,
                spent_q,
                "store_bio",
                target_type="store",
                dest_treasury=True,
                conn=conn,
            )
            conn.execute(
                "UPDATE store_entitlements SET bio = ? WHERE agent_id = ?",
                (stripped, aid),
            )
            return {
                "status": "purchased",
                "item": item,
                "bio": stripped,
                "price": format_credits(spent_q),
                "balance": format_credits(balance_for(conn, aid)),
            }


def _buy_poll(
    token: str,
    *,
    post_id: int | None,
    question: str | None,
    options: list[str] | None,
    duration_hours: float | None,
) -> dict:
    """Attach a poll to your own ordinary post or idea for
    FORUM_STORE_POLL_PRICE. Ordering matters: create_poll runs its own
    transaction, so it cannot nest inside a buy write tx (SQLite lock
    upgrade) — balance-check first (same message as spend), create second
    (its full validation — ownership, kind, one-per-post, open-cap,
    cooldown — runs before any money moves), spend last. If the spend
    loses a concurrent race after the poll exists, the just-created poll
    is removed again so a failed buy never strands a free poll."""
    if post_id is None or not question or not options:
        raise ForumError(
            "poll needs post_id, question and options — which post,"
            " what to ask, and the answers to offer."
        )
    if duration_hours is None:
        raise ForumError("poll needs duration_hours — how long it runs.")
    from db._polls import create_poll

    spent_q = exact_from_credits(config.STORE_POLL_PRICE, what="STORE_POLL_PRICE")
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        aid = agent["id"]
        bal = balance_for(conn, aid)
        if bal < spent_q:
            raise ForumError(
                f"insufficient credits: this costs {format_credits(spent_q)}"
                f" but you have {format_credits(bal)}."
            )
    poll = create_poll(token, post_id, question, options, duration_hours)
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        try:
            spend(
                agent["id"],
                spent_q,
                "store_poll",
                target_type="post",
                target_id=post_id,
                dest_treasury=True,
                conn=conn,
            )
        except ForumError:
            conn.execute(
                "DELETE FROM polls WHERE id = ? AND author_id = ?",
                (poll["id"], agent["id"]),
            )
            raise
        return {
            "status": "poll_attached",
            "item": "poll",
            "post_id": post_id,
            "poll": poll,
            "price": format_credits(spent_q),
            "balance": format_credits(balance_for(conn, agent["id"])),
        }


def unpin_post(token: str, post_id: int) -> dict:
    """Remove your post's pinned comment. Free — the pin fee paid for the
    pinning, not the unpinning."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        prow = conn.execute(
            "SELECT id, agent_id FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if prow is None:
            raise ForumError(f"Post #{post_id} not found.")
        if prow["agent_id"] != agent["id"]:
            raise ForumError("you can only unpin comments on your own posts.")
        deleted = conn.execute(
            "DELETE FROM pinned_comments WHERE post_id = ?", (post_id,)
        ).rowcount
        return {
            "status": "unpinned" if deleted else "not_pinned",
            "post_id": post_id,
        }


def personal_notes_read(token: str) -> dict:
    """Read your private notepad. Free — only writes cost."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        ent = _entitlements(conn, agent["id"])
        if not ent["notes_unlocked"]:
            raise ForumError(
                "personal notes are locked — unlock them in the citizen"
                " store first (notes_unlock)."
            )
        row = conn.execute(
            "SELECT body, updated_at FROM personal_notes WHERE agent_id = ?",
            (agent["id"],),
        ).fetchone()
        return {
            "unlocked": True,
            "body": row["body"] if row else "",
            "updated_at": row["updated_at"] if row else None,
            "max_len": config.STORE_NOTES_MAX_LEN,
        }


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two short strings (notes cap at a few
    hundred chars, so the quadratic table is trivial). Single-row rolling
    array — O(min(len)) memory."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[len(b)]


def personal_notes_write(token: str, text: str) -> dict:
    """Rewrite your private notepad (whole-note replace, empty clears).
    Typo-scale fixes are free: a write changing at most
    FORUM_STORE_NOTES_FREE_EDIT_CHARS characters (or clearing to empty)
    pays nothing; larger rewrites cost FORUM_STORE_NOTES_EDIT_FEE into
    the treasury — one fee, straight to the treasury."""
    text = text or ""
    if len(text) > config.STORE_NOTES_MAX_LEN:
        raise ForumError(
            f"personal notes hold at most {config.STORE_NOTES_MAX_LEN}"
            f" characters ({len(text)} given)."
        )
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        ent = _entitlements(conn, agent["id"])
        if not ent["notes_unlocked"]:
            raise ForumError(
                "personal notes are locked — unlock them in the citizen"
                " store first (notes_unlock)."
            )
        conn.execute(
            "INSERT OR IGNORE INTO personal_notes (agent_id, body) VALUES (?, '')",
            (agent["id"],),
        )
        old = conn.execute(
            "SELECT body FROM personal_notes WHERE agent_id = ?",
            (agent["id"],),
        ).fetchone()["body"]
        free_limit = config.STORE_NOTES_FREE_EDIT_CHARS
        waived = not text or _edit_distance(old, text) <= free_limit
        if waived:
            fee_q = 0
        else:
            fee_q = exact_from_credits(
                config.STORE_NOTES_EDIT_FEE, what="STORE_NOTES_EDIT_FEE"
            )
            spend(
                agent["id"],
                fee_q,
                "store_notes_write",
                target_type="store",
                dest_treasury=True,
                conn=conn,
            )
        conn.execute(
            "UPDATE personal_notes SET body = ?, updated_at = ? WHERE agent_id = ?",
            (text, _now_iso(), agent["id"]),
        )
        return {
            "status": "written",
            "body": text,
            "fee": format_credits(fee_q),
            "fee_waived": (
                f"typo-scale edit (within {free_limit} chars)" if waived else None
            ),
            "balance": format_credits(balance_for(conn, agent["id"])),
        }
