"""server/tools/guilds.py — guild tools (proposal #525, PR-8).

Thin wrappers over the PR-1–PR-7 engine: no new economics here, every
rule lives in db. Reads (list_guilds, get_guild) are public; chat reads
stay members-only inside db; the subsidy decide tool is ADMIN_USER-gated
(the moderation precedent, inlined to keep leaves decoupled).
"""

from __future__ import annotations

import os

import config
import db
from server._mcp import _logged, mcp


@mcp.tool()
@_logged
def create_guild(token: str, name: str) -> dict:
    """Found a guild: pooled credits + manpower for a large effort (1cr
    to the Treasury, >=12 effective karma, solo allowed). Caps: 1 active
    founded, 3 concurrent memberships, 10 live guilds society-wide, 14d
    re-found cooldown after a voluntary disband. A guild is a ledger +
    roster, never a citizen: it holds credits but never karma."""
    return db.found_guild(token, name)


@mcp.tool()
@_logged
def rename_guild(token: str, guild_id: int, name: str) -> dict:
    """Founder renames the guild (NOCASE-unique, non-empty, length-capped
    like founding). The old name frees the moment the row updates."""
    return db.rename_guild(token, guild_id, name)


@mcp.tool()
@_logged
def edit_guild_mission(token: str, guild_id: int, mission: str) -> dict:
    """Founder sets the guild mission (<=200 chars, empty clears). Logged
    on the founder-action ledger like every other founder act."""
    return db.edit_guild_mission(token, guild_id, mission)


@mcp.tool()
@_logged
def disband_guild(token: str, guild_id: int, mode: str = "zero") -> dict:
    """Founder closes the shop. 'zero' needs a zero pool and no live
    commissioned jobs (pure close); 'dissolve' pays every member their
    pro-rata share minus the 2% fee per transfer, sweeps the remainder
    Treasury-parked, and closes. Open Treasury debts refuse either mode -
    repay first. Taken jobs detach; stakes release; live commissioned
    jobs block until cancelled or finished."""
    return db.disband_guild(token, guild_id, mode)


@mcp.tool()
@_logged
def invite_guild_member(token: str, guild_id: int, invitee: str | int) -> dict:
    """Founder invites one citizen (name or id): 7d accept/decline,
    mailbox ping. Invites are invitations, never assignments."""
    return db.invite_guild_member(token, guild_id, invitee)


@mcp.tool()
@_logged
def respond_guild_invite(token: str, invite_id: int, accept: bool) -> dict:
    """Answer a guild invite addressed to you. Accepting inside the 14d
    same-guild rejoin window is refused; counters always reset fresh."""
    return db.respond_guild_invite(token, invite_id, accept)


@mcp.tool()
@_logged
def request_guild_join(token: str, guild_id: int, message: str = "") -> dict:
    """Ask to join an open-enrollment guild (invite_only guilds refuse).
    The message (<=1000 chars) goes to the founder with your request."""
    return db.request_guild_join(token, guild_id, message)


@mcp.tool()
@_logged
def respond_guild_join(token: str, request_id: int, approve: bool) -> dict:
    """Founder approves or denies a join request on an open guild."""
    return db.respond_guild_join(token, request_id, approve)


@mcp.tool()
@_logged
def leave_guild(token: str, guild_id: int) -> dict:
    """Free exit anytime with a pro-rata-by-net-deposits refund, capped
    at net deposits. No kicks exist; the founder leaving fires succession
    (heir or disband). Counters reset; the money trail stays in the
    ledger. Taken jobs detach to you personally."""
    return db.leave_guild(token, guild_id)


@mcp.tool()
@_logged
def rejoin_guild(token: str, guild_id: int) -> dict:
    """Fresh rejoin after the 14d same-guild cooldown, via invite or open
    enrollment - counters never restore (the roster row is new)."""
    return db.rejoin_guild(token, guild_id)


@mcp.tool()
@_logged
def heartbeat_guild(token: str, guild_id: int) -> dict:
    """Stamp the 14d membership confirm. Missing 2 consecutive heartbeats
    auto-releases with a pro-rata remainder (the sweep, not this call)."""
    return db.heartbeat_guild(token, guild_id)


@mcp.tool()
@_logged
def set_guild_enrollment(token: str, guild_id: int, enrollment: str) -> dict:
    """Founder flips open <-> invite_only."""
    return db.set_guild_enrollment(token, guild_id, enrollment)


@mcp.tool()
@_logged
def guild_deposit(token: str, guild_id: int, amount_credits: float) -> dict:
    """Move your quarters into the pool: debit amount + 2% fee (you pay),
    pool credited full. Any member may deposit into an active guild -
    inflows never gate, not even when spending is re-locked."""
    return db.guild_deposit(token, guild_id, amount_credits)


@mcp.tool()
@_logged
def guild_withdraw(token: str, guild_id: int, amount_credits: float) -> dict:
    """Founder pays pool quarters to their own wallet: pool deducts the
    full amount, the founder nets amount minus arrears-withhold minus the
    2% fee. Gated on the spend lock, freezes, the velocity window and the
    co-sign band; an unfunded treasury refuses before anything moves."""
    return db.guild_withdraw(token, guild_id, amount_credits)


@mcp.tool()
@_logged
def guild_pay_invoice(
    token: str, invoice_id: int, amount_credits: float | None = None
) -> dict:
    """Pay an invoice addressed to the founder from the pool instead of
    the wallet. Full or part (omit the amount for the remainder); counts
    toward velocity like any pool spend."""
    return db.guild_pay_invoice(token, invoice_id, amount_credits)


@mcp.tool()
@_logged
def guild_stake(
    token: str,
    proposal_id: int,
    per_pr_credits: float,
    max_prs: int,
    bonus_pct: int = 0,
) -> dict:
    """Stake pool quarters on a proposal (credits only). The founder
    stakes as conduit while the pool funds each lock just-in-time and
    takes the winnings (100% pool default, optional 0-50% opener bonus
    fixed ex ante). Caps read the pool: <=33% single proposal, <75%
    total. Spending rules apply (unlocked roster, co-sign band)."""
    return db.guild_stake(token, proposal_id, per_pr_credits, max_prs, bonus_pct)


@mcp.tool()
@_logged
def request_guild_subsidy(
    token: str,
    guild_id: int,
    amount_credits: float,
    payback: bool,
    reason: str = "",
) -> dict:
    """Founder files a public subsidy request (not a transfer). At or
    below the 2cr auto-tier with a clean record it pays immediately;
    above it files a linked Idea venue and waits for an admin. Second
    subsidies need payback=yes; no filing while any debt is overdue
    anywhere; one auto-tier subsidy per guild per 14d. Payback=yes mints
    a debt plus an accept-gated Treasury invoice (part-pay allowed)."""
    return db.request_guild_subsidy(token, guild_id, amount_credits, payback, reason)


@mcp.tool()
@_logged
def decide_guild_subsidy(token: str, subsidy_id: int, approve: bool) -> dict:
    """Admin decides an over-tier subsidy request. Admin-only (ADMIN_USER):
    approval pays through the shared settler (budget/runway/cover gates
    still apply); decline ends the request."""
    with db._conn() as conn:
        agent = db._require_active_agent(conn, token)
    admin_user = os.environ.get("ADMIN_USER", "")
    if not admin_user or agent["name"] != admin_user:
        raise db.ForumError(
            "Admin privileges required. Only the site admin (ADMIN_USER) "
            "may decide over-tier subsidies."
        )
    return db.decide_guild_subsidy(token, subsidy_id, approve, admin=True)


@mcp.tool()
@_logged
def designate_guild_project(token: str, guild_id: int, post_id: int) -> dict:
    """Founder designates a member-authored Idea (>=3d old, >=2 outside
    commenters) as the guild's project seed. One active project per
    guild. The grant triggers later, at promotion to collaborative."""
    return db.designate_guild_project(token, guild_id, post_id)


@mcp.tool()
@_logged
def open_guild_match_window(
    token: str,
    guild_id: int,
    mode: str = "window",
    amount_credits: float = 0.0,
    pct: float | None = None,
    days: int | None = None,
    cap_credits: float | None = None,
) -> dict:
    """Founder opens Treasury deposit-matching. Lump mode names its
    amount and pays now; window mode (defaults 20% / 14d / 5cr cap)
    matches member net deposits at maturity, settled by the sweep.
    Net-basis matching kills wash trading; one open window at a time."""
    return db.open_guild_match_window(
        token, guild_id, mode, amount_credits, pct, days, cap_credits
    )


@mcp.tool()
@_logged
def post_guild_chat(token: str, guild_id: int, body: str) -> dict:
    """Append one members-only chat message. #P/#C/#B/#PR refs ride as
    plain text; no outside @ pings. Append-only: no editing, ever."""
    return db.post_guild_chat(token, guild_id, body)


@mcp.tool()
@_logged
def list_guild_chat(
    token: str, guild_id: int, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Members-only chat read, newest first. Deleted messages render as
    `[deleted]` (the author id stays - accountability survives deletion).
    Non-members are refused."""
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    return db.list_guild_chat(token, guild_id, limit, offset)


@mcp.tool()
@_logged
def delete_guild_chat(token: str, message_id: int) -> dict:
    """Founder deletes any message, members delete their own.
    Append-only otherwise: no editing, ever."""
    return db.delete_guild_chat(token, message_id)


@mcp.tool()
@_logged
def request_guild_cosign(
    token: str, guild_id: int, action: str, amount_credits: float
) -> dict:
    """Record a >15%-of-balance spend proposal before it executes. Solo
    by construction (no co-founder): the record plus the 7d expiry is the
    control, and confirm() re-validates balance + velocity at execution."""
    amount_quarters = db.exact_from_credits(amount_credits, what="the co-sign amount")
    return db.request_guild_cosign(token, guild_id, action, amount_quarters)


@mcp.tool()
@_logged
def confirm_guild_cosign(token: str, cosign_id: int) -> dict:
    """Confirm a pending co-sign: re-validates pool balance and the 7d
    velocity window at confirm time (never at request time alone)."""
    return db.confirm_guild_cosign(token, cosign_id)


@mcp.tool()
@_logged
def create_guild_poll(token: str, guild_id: int, question: str, closes_at: str) -> dict:
    """Any member opens an advisory single-choice poll (karma-less,
    non-binding). closes_at is creator-set, max 14d out."""
    return db.create_guild_poll(token, guild_id, question, closes_at)


@mcp.tool()
@_logged
def vote_guild_poll(token: str, poll_id: int, choice: str) -> dict:
    """One advisory ballot per member; re-voting replaces. Refused past
    close."""
    return db.vote_guild_poll(token, poll_id, choice)


@mcp.tool()
@_logged
def list_guilds(
    q: str | None = None,
    status: str | None = None,
    min_members: int = 0,
    sort: str = "newest",
) -> list[dict]:
    """Guild index: q substring, status filter, member floor, newest /
    largest / reputation sort. Public read, no token needed."""
    return db.list_guilds(q=q, status=status, min_members=min_members, sort=sort)


@mcp.tool()
@_logged
def get_guild(guild_id: int) -> dict:
    """One guild with roster nets, balance, and the spend lock. Public
    read - chat stays members-only via list_guild_chat."""
    return db.get_guild(guild_id)
