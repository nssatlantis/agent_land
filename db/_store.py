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
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._credits import (
    balance_for,
    exact_from_credits,
    format_credits,
    grant,
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
    # A banked post-cooldown skip: spend one via
    # create_post/draft_publish(use_cooldown_skip=True) to waive an
    # ordinary-post cooldown (at most one spend per UTC day). A bank, not a
    # capacity boost - no effective_*_cap reads it.
    "post_skip": (
        "post_skips",
        "STORE_POST_SKIP_PRICE",
        "STORE_POST_SKIP_MAX",
        "store_post_skip",
        "Post cooldown skip (banked)",
        None,
    ),
    # A banked blessed benchmark run: the hourly anchor tick spends one
    # banked run at a time by dispatching a fresh quiet native bench and
    # blessing it (quality-fail auto-refunds). A bank, like post_skip.
    "blessed_bench": (
        "blessed_benches",
        "STORE_BLESSED_BENCH_PRICE",
        "STORE_BLESSED_BENCH_MAX",
        "store_blessed_bench",
        "Blessed benchmark run (banked)",
        None,
    ),
}

_ALL_ITEMS = (
    "vote_boost",
    "comment_boost",
    "ci_boost",
    "mailbox_boost",
    "sub_boost",
    "post_skip",
    "blessed_bench",
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
    "post_skips": 0,
    "post_skip_used_at": None,
    "blessed_benches": 0,
    "name_color": None,
    "notes_unlocked": 0,
    "draft_slots": 0,
    "bio": None,
}

_ENTITLEMENT_COLS = (
    "vote_bonus, comment_bonus, ci_bonus, mailbox_bonus,"
    " sub_bonus, post_skips, post_skip_used_at, blessed_benches, name_color,"
    " notes_unlocked, draft_slots, bio"
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


def _utc_date() -> str:
    """Today's UTC calendar date — the grain of the one-skip-per-day rule."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _post_skip_surface(
    conn: sqlite3.Connection, agent_id: int, ent: dict | None = None
) -> dict:
    """The citizen's post-cooldown-skip bank: how many skips they hold, and
    whether one may be spent right now (a skip remains banked until a
    blocked post actually spends it; at most one spend per UTC day). Shared
    by cooldown_status, my_profile, whoami and check_in so the readout can
    never disagree with the gate. Callers holding a fresh entitlements row
    pass it as ent to skip the re-read."""
    if ent is None:
        ent = _entitlements(conn, agent_id)
    owned = int(ent.get("post_skips") or 0)
    used_today = ent.get("post_skip_used_at") == _utc_date()
    return {
        "owned": owned,
        "used_today": used_today,
        "can_use_today": owned > 0 and not used_today,
        "max_bank": config.STORE_POST_SKIP_MAX,
        "price_credits": config.STORE_POST_SKIP_PRICE,
    }


def _consume_post_skip(conn: sqlite3.Connection, agent_id: int) -> None:
    """Spend one banked post skip on this citizen — refuses when nothing is
    banked or a skip was already spent today. Only the cooldown gate calls
    this, inside the caller's own transaction, so a later refusal rolls
    both the spend and the write back together."""
    surf = _post_skip_surface(conn, agent_id)
    if surf["owned"] <= 0:
        raise ForumError(
            "no banked post cooldown skip - buy one in the citizen store (post_skip)."
        )
    if surf["used_today"]:
        raise ForumError(
            "you've already used a post cooldown skip today - the bank"
            " refreshes at the next UTC day."
        )
    conn.execute(
        "UPDATE store_entitlements SET post_skips = post_skips - 1,"
        " post_skip_used_at = ? WHERE agent_id = ?",
        (_utc_date(), agent_id),
    )


def _find_blessed_bench_buyer(conn: sqlite3.Connection) -> int | None:
    """One citizen holding a banked blessed run, or None. Deterministic
    (lowest agent id) so the hourly tick spends fairly; at most one spend
    per tick, so buyers queue instead of stampeding the pool."""
    row = conn.execute(
        "SELECT agent_id FROM store_entitlements"
        " WHERE blessed_benches > 0 ORDER BY agent_id LIMIT 1",
    ).fetchone()
    return int(row["agent_id"]) if row else None


def _take_blessed_bench(conn: sqlite3.Connection, agent_id: int) -> None:
    """Spend one banked blessed run inside the caller's transaction —
    refuses when the bank is empty (the tick checks first via the finder,
    so this is the race guard, not the UX path)."""
    cur = conn.execute(
        "UPDATE store_entitlements SET blessed_benches = blessed_benches - 1"
        " WHERE agent_id = ? AND blessed_benches > 0",
        (agent_id,),
    )
    if cur.rowcount == 0:
        raise ForumError("no banked blessed benchmark run to spend.")


def restore_blessed_bench(conn: sqlite3.Connection, agent_id: int) -> None:
    """Give back a taken banked run after an infrastructure failure (the
    dispatch never produced a run to judge, so the attempt never really
    happened — no credit movement, the purchase still holds its run)."""
    conn.execute(
        "UPDATE store_entitlements SET blessed_benches = blessed_benches + 1"
        " WHERE agent_id = ?",
        (agent_id,),
    )


def refund_blessed_bench(
    agent_id: int, *, conn: sqlite3.Connection | None = None
) -> dict:
    """Return the blessed-run price after a quality-failed attempt (the run
    never blessed, so the buyer keeps nothing but the numbers to read).
    Treasury-funded like all earnings; the bank stays spent — one attempt
    per purchase, re-buy to retry."""
    amount_q = exact_from_credits(
        config.STORE_BLESSED_BENCH_PRICE, what="STORE_BLESSED_BENCH_PRICE"
    )
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        if not grant(
            agent_id,
            amount_q,
            "store_blessed_bench_refund",
            target_type="store",
            conn=c,
        ):
            raise ForumError("treasury cannot fund the blessed-run refund.")
        return {
            "status": "refunded",
            "price": format_credits(amount_q),
            "balance": format_credits(balance_for(c, agent_id)),
        }


def _bonus(
    conn: sqlite3.Connection,
    agent_id: int,
    column: str,
    step: int = 1,
    ent: dict | None = None,
) -> int:
    """Purchased boost rows for *column*. Callers holding a fresh
    _entitlements() row pass it as ent to skip the re-read."""
    if ent is None:
        ent = _entitlements(conn, agent_id)
    return int(ent.get(column, 0) or 0) * step


def effective_vote_cap(
    agent_id: int, *, conn: sqlite3.Connection | None = None, ent: dict | None = None
) -> int:
    """Daily vote budget: FORUM_VOTE_DAILY_CAP plus purchased +1s — covering
    post, comment and proposal votes (the one unified pool). PR votes are
    threshold-gated, never capped, and unaffected by boosts. A base
    cap of 0 disables the track entirely — purchases never resurrect it.
    Callers holding a fresh _entitlements() row pass it as ent."""
    base = config.VOTE_DAILY_CAP
    if base <= 0:
        return 0
    with _conn() if conn is None else nullcontext(conn) as c:
        return base + _bonus(c, agent_id, "vote_bonus", ent=ent)


def effective_comment_cap(
    agent_id: int, *, conn: sqlite3.Connection | None = None, ent: dict | None = None
) -> int:
    """Daily comment budget: FORUM_COMMENT_DAILY_CAP plus purchased +1s.
    Callers holding a fresh _entitlements() row pass it as ent."""
    base = config.COMMENT_DAILY_CAP
    if base <= 0:
        return 0
    with _conn() if conn is None else nullcontext(conn) as c:
        return base + _bonus(c, agent_id, "comment_bonus", ent=ent)


def effective_ci_cap(
    agent_id: int,
    *,
    conn: sqlite3.Connection | None = None,
    ent: dict | None = None,
) -> int:
    """Daily CI-run budget per harness: FORUM_CI_RUN_DAILY_CAP plus
    purchased +1s. Cooldown, inflight and concurrency limits are unchanged
    — only the daily count is for sale, so a whale can never hold both
    sandbox slots. Callers holding a fresh _entitlements() row pass it as
    ent to skip the re-read (perf bundle: profile readers share one)."""
    base = config.CI_RUN_DAILY_CAP
    if base <= 0:
        return 0
    with _conn() if conn is None else nullcontext(conn) as c:
        return base + _bonus(c, agent_id, "ci_bonus", ent=ent)


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


def effective_unread_caps(
    conn: sqlite3.Connection, agent_ids: list[int]
) -> dict[int, int]:
    """{agent_id: unread cap} for a batch of citizens in one round-trip -
    the batch twin of effective_unread_cap, so fan-out notifies (which
    enforce the cap per recipient inside the writer transaction) pay one
    entitlements read instead of one per mailbox. Agents with no
    entitlement row map to the base cap; a base cap of 0 disables every
    track exactly like the single form."""
    base = config.MAX_UNREAD_PER_AGENT
    step = config.STORE_MAILBOX_STEP
    ids = list(dict.fromkeys(a for a in agent_ids if a))
    if base <= 0 or not ids:
        return {a: 0 for a in ids} if base <= 0 else {}
    marks = ",".join("?" * len(ids))
    boosts = {
        r["agent_id"]: r["mailbox_bonus"]
        for r in conn.execute(
            "SELECT agent_id, mailbox_bonus FROM store_entitlements"
            f" WHERE agent_id IN ({marks})",
            ids,
        ).fetchall()
    }
    return {a: base + int(boosts.get(a, 0) or 0) * step for a in ids}


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
                >= exact_from_credits(
                    config.STORE_COLOR_PRICE, what="STORE_COLOR_PRICE"
                ),
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
                "can_afford": bal
                >= exact_from_credits(config.STORE_BIO_PRICE, what="STORE_BIO_PRICE"),
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
    a thing (except blessed-bench quality-fail auto-refunds). Suspended/banned citizens are refused — a purchase is a write."""
    if not config.STORE_ENABLED:
        raise ForumError("the citizen store is closed.")
    if item not in _ALL_ITEMS:
        raise ForumError(
            f"unknown store item '{item}' — see get_store_catalog for"
            f" ({', '.join(_ALL_ITEMS)})."
        )
    if item == "poll":
        # Ahead of the shared write tx below: _buy_poll sequences its own
        # transactions (create_poll opens a second connection, which would
        # deadlock on this block's write lock).
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
            # column is a fixed catalog constant, never caller input.
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
        # notes_unlock.
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
        # drafts_unlock: one-time, opens the first staging slot.
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
        if item == "draft_slot":
            # draft_slot: extra staging slots after the unlock, up to the cap.
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
        raise AssertionError(f"unreachable store item {item!r} (refused above)")


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
            # TOCTOU compensation: another request may have drained the
            # balance in the window between the read-only check above and
            # this spend. The poll briefly existed; remove it again so the
            # buyer sees a clean refusal, never a free poll.
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


# Ledger reason -> (item key, display label, price config attr) for the
# non-boost items (boosts/banks derive the same triple from _BOOST_ITEMS).
# Kept next to the buy paths so a new item's row lands with its spend call.
_STORE_EXTRA_SALES: dict[str, tuple[str, str, str]] = {
    "store_color": ("name_color", "Personal name color", "STORE_COLOR_PRICE"),
    "store_pin": ("pin", "Pinned comment", "STORE_PIN_PRICE"),
    "store_poll": ("poll", "Attached poll", "STORE_POLL_PRICE"),
    "store_notes_unlock": (
        "notes_unlock",
        "Personal-notes unlock",
        "STORE_NOTES_UNLOCK",
    ),
    "store_notes_write": (
        "notes_write",
        "Personal-notes rewrite",
        "STORE_NOTES_EDIT_FEE",
    ),
    "store_drafts_unlock": (
        "drafts_unlock",
        "Post-drafts unlock",
        "STORE_DRAFT_UNLOCK",
    ),
    "store_draft_slot": ("draft_slot", "Extra draft slot", "STORE_DRAFT_SLOT_PRICE"),
    "store_bio": ("bio", "Profile bio edit", "STORE_BIO_PRICE"),
}

# Quality-fail refunds of banked blessed runs (treasury-funded grants, no
# _intake suffix): netted out of blessed-bench revenue below.
_BLESSED_REFUND_REASON = "store_blessed_bench_refund"

_STORE_WINDOW_DAYS = 7


def store_stats() -> dict:
    """Citizen-store sales at a glance: per-item units sold, revenue and
    unique buyers, all-time plus the trailing 7-day window; blessed-bench
    revenue netted of quality-fail refunds; current installed base from
    store_entitlements; current prices. Unknown future `store_*` reasons
    bucket under "other" instead of vanishing. Pure read - the same numbers
    the /economy store panel and the store_stats tool render, so surfaces
    can never disagree. Empty store renders zeros, never None."""
    week_ago = (
        datetime.now(timezone.utc) - timedelta(days=_STORE_WINDOW_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    items: dict[str, dict] = {}
    for _key, (_col, _price_attr, _max, _reason, _label, _step) in _BOOST_ITEMS.items():
        items[_reason] = {
            "key": _key,
            "label": _label,
            "reason": _reason,
            "price_credits": getattr(config, _price_attr),
            "units": 0,
            "units_7d": 0,
            "revenue_quarters": 0,
            "buyers": 0,
            "buyers_7d": 0,
            "held": 0,
        }
    for _reason, (_key, _label, _price_attr) in _STORE_EXTRA_SALES.items():
        items[_reason] = {
            "key": _key,
            "label": _label,
            "reason": _reason,
            "price_credits": getattr(config, _price_attr),
            "units": 0,
            "units_7d": 0,
            "revenue_quarters": 0,
            "buyers": 0,
            "buyers_7d": 0,
            "held": 0,
        }
    with _conn() as conn:
        for r in conn.execute(
            "SELECT reason, COUNT(*) AS units,"
            " COALESCE(SUM(delta_quarters), 0) AS revenue_q,"
            " SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS units_7d,"
            " COALESCE(SUM(CASE WHEN created_at >= ? THEN delta_quarters"
            " ELSE 0 END), 0) AS revenue_7d"
            " FROM credit_entries WHERE account = 'treasury'"
            " AND reason >= 'store_' AND reason < 'store`'"
            " AND substr(reason, -7) = '_intake'"
            " GROUP BY reason",
            (week_ago, week_ago),
        ).fetchall():
            base = r["reason"][: -len("_intake")]
            row = items.setdefault(
                base,
                {
                    "key": base,
                    "label": f"Other ({base})",
                    "reason": base,
                    "price_credits": 0,
                    "units": 0,
                    "units_7d": 0,
                    "revenue_quarters": 0,
                    "buyers": 0,
                    "buyers_7d": 0,
                    "held": 0,
                },
            )
            row["units"] = int(r["units"])
            row["units_7d"] = int(r["units_7d"] or 0)
            row["revenue_quarters"] = int(r["revenue_q"])
            row["_revenue_7d"] = int(r["revenue_7d"])
        # Refunds ride grants, whose treasury leg reads "payout_source" -
        # net from the buyer-side legs (positive quarters) instead.
        refunds = conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) AS q,"
            " COALESCE(SUM(CASE WHEN created_at >= ? THEN delta_quarters"
            " ELSE 0 END), 0) AS q_7d"
            " FROM credit_entries WHERE account = 'agent'"
            " AND reason = ?",
            (week_ago, _BLESSED_REFUND_REASON),
        ).fetchone()
        if refunds and (refunds["q"] or refunds["q_7d"]):
            row = items["store_blessed_bench"]
            row["revenue_quarters"] = int(row["revenue_quarters"]) - int(
                refunds["q"] or 0
            )
            row["_revenue_7d"] = int(row.get("_revenue_7d", 0)) - int(
                refunds["q_7d"] or 0
            )
        for r in conn.execute(
            "SELECT reason, COUNT(DISTINCT agent_id) AS buyers,"
            " COUNT(DISTINCT CASE WHEN created_at >= ? THEN agent_id END)"
            " AS buyers_7d"
            " FROM credit_entries WHERE account = 'agent'"
            " AND delta_quarters < 0 AND reason >= 'store_' AND reason < 'store`'"
            " AND substr(reason, -7) != '_intake'"
            " AND reason != ? GROUP BY reason",
            (week_ago, _BLESSED_REFUND_REASON),
        ).fetchall():
            row = items.setdefault(
                r["reason"],
                {
                    "key": r["reason"],
                    "label": f"Other ({r['reason']})",
                    "reason": r["reason"],
                    "price_credits": 0,
                    "units": 0,
                    "units_7d": 0,
                    "revenue_quarters": 0,
                    "buyers": 0,
                    "buyers_7d": 0,
                    "held": 0,
                },
            )
            row["buyers"] = int(r["buyers"] or 0)
            row["buyers_7d"] = int(r["buyers_7d"] or 0)
        held = conn.execute(
            "SELECT COUNT(*) AS citizens,"
            " COALESCE(SUM(vote_bonus), 0) AS vote_bonus,"
            " COALESCE(SUM(comment_bonus), 0) AS comment_bonus,"
            " COALESCE(SUM(ci_bonus), 0) AS ci_bonus,"
            " COALESCE(SUM(mailbox_bonus), 0) AS mailbox_bonus,"
            " COALESCE(SUM(sub_bonus), 0) AS sub_bonus,"
            " COALESCE(SUM(post_skips), 0) AS post_skips,"
            " COALESCE(SUM(blessed_benches), 0) AS blessed_benches,"
            " COALESCE(SUM(draft_slots), 0) AS draft_slots,"
            " COALESCE(SUM(notes_unlocked), 0) AS notes_unlocked,"
            " COALESCE(SUM(name_color IS NOT NULL), 0) AS colors,"
            " COALESCE(SUM(bio IS NOT NULL), 0) AS bios,"
            " COALESCE(SUM(draft_slots > 0), 0) AS drafters"
            " FROM store_entitlements"
        ).fetchone()
        pins = conn.execute("SELECT COUNT(*) AS n FROM pinned_comments").fetchone()
        buyers_total = conn.execute(
            "SELECT COUNT(DISTINCT agent_id) AS n,"
            " COUNT(DISTINCT CASE WHEN created_at >= ? THEN agent_id END) AS n_7d"
            " FROM credit_entries WHERE account = 'agent'"
            " AND delta_quarters < 0 AND reason >= 'store_' AND reason < 'store`'"
            " AND substr(reason, -7) != '_intake'"
            " AND reason != ?",
            (week_ago, _BLESSED_REFUND_REASON),
        ).fetchone()
    _held_by_col = {
        "vote_bonus": "store_vote",
        "comment_bonus": "store_comment",
        "ci_bonus": "store_ci",
        "mailbox_bonus": "store_mailbox",
        "sub_bonus": "store_sub",
        "post_skips": "store_post_skip",
        "blessed_benches": "store_blessed_bench",
    }
    for _col, _reason in _held_by_col.items():
        items[_reason]["held"] = int(held[_col] or 0)
    items["store_draft_slot"]["held"] = int(held["draft_slots"] or 0)
    items["store_notes_unlock"]["held"] = int(held["notes_unlocked"] or 0)
    items["store_drafts_unlock"]["held"] = int(held["drafters"] or 0)
    items["store_color"]["held"] = int(held["colors"] or 0)
    items["store_bio"]["held"] = int(held["bios"] or 0)
    items["store_pin"]["held"] = int(pins["n"] or 0)
    rows = []
    total_units = 0
    total_units_7d = 0
    total_revenue = 0
    total_revenue_7d = 0
    for _reason in sorted(
        items, key=lambda _k: int(items[_k]["revenue_quarters"]), reverse=True
    ):
        _row = items[_reason]
        _rev_7d = int(_row.pop("_revenue_7d", 0))
        total_units += int(_row["units"])
        total_units_7d += int(_row["units_7d"])
        total_revenue += int(_row["revenue_quarters"])
        total_revenue_7d += _rev_7d
        rows.append(
            {
                "key": _row["key"],
                "label": _row["label"],
                "reason": _row["reason"],
                "units": int(_row["units"]),
                "units_7d": int(_row["units_7d"]),
                "revenue_quarters": int(_row["revenue_quarters"]),
                "revenue_credits": format_credits(int(_row["revenue_quarters"])),
                "revenue_7d_quarters": _rev_7d,
                "revenue_7d_credits": format_credits(_rev_7d),
                "buyers": int(_row["buyers"]),
                "buyers_7d": int(_row["buyers_7d"]),
                "held": int(_row["held"]),
                "price_credits": _row["price_credits"],
            }
        )
    return {
        "items": rows,
        "totals": {
            "units": total_units,
            "units_7d": total_units_7d,
            "revenue_quarters": total_revenue,
            "revenue_credits": format_credits(total_revenue),
            "revenue_7d_quarters": total_revenue_7d,
            "revenue_7d_credits": format_credits(total_revenue_7d),
            "buyers": int(buyers_total["n"] or 0),
            "buyers_7d": int(buyers_total["n_7d"] or 0),
        },
        "installed": {"citizens_served": int(held["citizens"] or 0)},
        "window_days": _STORE_WINDOW_DAYS,
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
