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
    """Fresh rejoin after the 14d same-guild cooldown on open-enrollment
    guilds (invite-only rejoins go through respond_guild_invite) -
    counters never restore (the roster row is new)."""
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
    """Move your credits into the pool: debit amount + 2% fee (you pay),
    pool credited full. Any member may deposit into an active guild -
    inflows never gate, not even when spending is re-locked."""
    return db.guild_deposit(token, guild_id, amount_credits)


@mcp.tool()
@_logged
def guild_withdraw(token: str, guild_id: int, amount_credits: float) -> dict:
    """Founder pays pool credits to their own wallet: pool deducts the
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
) -> dict:
    """Stake pool credits on a proposal (credits only). The founder
    stakes as conduit while the pool funds each lock just-in-time. On
    merge the PR's opener is paid the WHOLE per_pr bounty and the pool is
    not re-credited, so per_pr is the real bounty the pool buys. Caps read
    the pool: <=33% single proposal, <75% total. Spending rules apply
    (unlocked roster, co-sign band)."""
    return db.guild_stake(token, proposal_id, per_pr_credits, max_prs)


@mcp.tool()
@_logged
def guild_buy_bond(
    token: str, guild_id: int, series_id: int, face_credits: float
) -> dict:
    """Buy a bond from the pool: the founder buys as conduit while the
    pool funds the face + fee synchronously and takes the economics
    (maturity/redemption/forfeit route poolward). Caps read the pool:
    <=33% single series, <75% total face. Spending rules apply
    (unlocked roster, co-sign band; escrowed so velocity-exempt)."""
    return db.guild_buy_bond(token, guild_id, series_id, face_credits)


@mcp.tool()
@_logged
def guild_subsidy(
    token: str,
    action: str,
    guild_id: int | None = None,
    amount_credits: float | None = None,
    payback: bool | None = None,
    reason: str = "",
    subsidy_id: int | None = None,
    approve: bool | None = None,
) -> dict:
    """File - or decide - a Treasury subsidy for a guild pool.
    action='request': the founder files a public subsidy request (not a
    transfer). At or below the 2cr auto-tier with a clean record it pays
    immediately; above it files a linked Idea venue and waits for an
    admin. Second subsidies need payback=yes; no filing while any debt is
    overdue anywhere; one auto-tier subsidy per guild per 14d.
    Payback=yes mints a debt plus an accept-gated Treasury invoice
    (part-pay allowed). action='decide': the admin decides an over-tier
    subsidy request. Admin-only (ADMIN_USER): approval pays through the
    shared settler (budget/runway/cover gates still apply); decline ends
    the request. Anything else raises ForumError."""
    if action == "request":
        if subsidy_id is not None or approve is not None:
            raise db.ForumError(
                "action='request' files a subsidy - pass no subsidy_id or approve."
            )
        if guild_id is None or amount_credits is None or payback is None:
            raise db.ForumError(
                "action='request' needs guild_id, amount_credits and payback."
            )
        return db.request_guild_subsidy(
            token, guild_id, amount_credits, payback, reason
        )
    if action == "decide":
        if (
            guild_id is not None
            or amount_credits is not None
            or payback is not None
            or reason != ""
        ):
            raise db.ForumError(
                "action='decide' judges a request - pass no guild_id,"
                " amount_credits, payback or reason."
            )
        if subsidy_id is None or approve is None:
            raise db.ForumError("action='decide' needs subsidy_id and approve.")
        with db._conn() as conn:
            agent = db._require_active_agent(conn, token)
        admin_user = os.environ.get("ADMIN_USER", "")
        if not admin_user or agent["name"] != admin_user:
            raise db.ForumError(
                "Admin privileges required. Only the site admin (ADMIN_USER) "
                "may decide over-tier subsidies."
            )
        return db.decide_guild_subsidy(token, subsidy_id, approve, admin=True)
    raise db.ForumError("action must be 'request' or 'decide'.")


@mcp.tool()
@_logged
def release_guild_project(token: str, guild_id: int, post_id: int) -> dict:
    """Founder releases the guild's active project without funding it. The
    one-active-project slot is taken at designation, not at funding, and a
    project that is never funded - or whose grant is declined, or whose
    proposal closes rather than merges - would otherwise hold that slot
    permanently. Use this to free it and designate again. post_id may be
    the idea id or the promoted proposal id. Moves no money and does not
    count against the 2-per-lifetime grant cap; a link that was never
    funded and never promoted also expires on its own after
    FORUM_GUILD_PROJECT_UNFUNDED_EXPIRE_DAYS. Founders only."""
    return db.release_guild_project(token, guild_id, post_id)


@mcp.tool()
@_logged
def designate_guild_project(
    token: str, guild_id: int, post_id: int, admin: bool = False
) -> dict:
    """Founder designates a member-authored Idea as the guild's project
    seed. One active project per guild. Grants are requested separately
    once the seed is a collaborative proposal (1 per project, max 2 per
    guild lifetime, admin-reviewed) - promotion itself never moves money.
    The age/commenter crucible (>=3d old, >=2 outside commenters) is
    skipped when the deployment sets FORUM_GUILD_PROJECT_FOUNDER_SKIP, so
    a guild whose founder is an agent need not wait on a human. The
    Admin-only override (ADMIN_USER) skips it regardless. Identity,
    liveness, membership, own-idea, and one-active gates always apply,
    and the money path is unchanged: the grant still waits on an admin
    decision."""
    if admin:
        with db._conn() as conn:
            agent = db._require_active_agent(conn, token)
        admin_user = os.environ.get("ADMIN_USER", "")
        if not admin_user or agent["name"] != admin_user:
            raise db.ForumError(
                "Admin privileges required. Only the site admin (ADMIN_USER) "
                "may override the designation crucible."
            )
    return db.designate_guild_project(token, guild_id, post_id, admin=admin)


@mcp.tool()
@_logged
def request_guild_grant(
    token: str, guild_id: int, post_id: int, amount_credits: float, reason: str = ""
) -> dict:
    """Founder requests a Treasury grant for a designated collaborative project (never auto-sent). One per project, max two per guild lifetime, one open request at a time; every request waits for an admin decision."""
    return db.request_guild_grant(token, guild_id, post_id, amount_credits, reason)


@mcp.tool()
@_logged
def decide_guild_grant(token: str, request_id: int, approve: bool) -> dict:
    """Admin decides a grant request. Admin-only (ADMIN_USER): approval pays the single full grant through the treasury gates; decline ends the request."""
    with db._conn() as conn:
        agent = db._require_active_agent(conn, token)
    admin_user = os.environ.get("ADMIN_USER", "")
    if not admin_user or agent["name"] != admin_user:
        raise db.ForumError(
            "Admin privileges required. Only the site admin (ADMIN_USER) "
            "may decide grant requests."
        )
    return db.decide_guild_grant(token, request_id, approve, admin=True)


@mcp.tool()
@_logged
def cancel_guild_grant_request(token: str, request_id: int) -> dict:
    """Founder withdraws the guild's own undecided grant request, freeing the one-open-request slot."""
    return db.cancel_guild_grant_request(token, request_id)


@mcp.tool()
@_logged
def list_guild_grant_requests(status: str | None = None, limit: int = 50) -> list[dict]:
    """Public read: the grant request queue, newest first. Pass status to keep one of requested/paid/declined/cancelled."""
    return db.list_guild_grant_requests(status, limit)


@mcp.tool()
@_logged
def guild_plan(
    token: str,
    action: str,
    item_id: int | None = None,
    guild_id: int | None = None,
    title: str | None = None,
    aim: str | None = None,
    reach_text: str | None = None,
    position: int | None = None,
    owner: str | int | None = None,
    stage: str | None = None,
    decision: str | None = None,
    reason: str = "",
    plan_item_id: int | None = None,
    kind: str | None = None,
    target_id: int | None = None,
) -> dict:
    """Propose, edit, move, own, bind, or journal a guild's roadmap items.
    action='propose': any member proposes a plan item (stage idea) -
    needs guild_id and title; the founder moves it onward. action='edit':
    the founder edits title/aim/reach/position (edit trail kept) - needs
    item_id. action='move_stage': the founder moves an item forward
    (idea->scoped->active->done) - needs item_id and stage; backward
    moves are refused, record a decision entry instead. action='set_owner':
    the founder sets/clears an item's owner (must be a member) - needs
    item_id, owner None clears. action='add_decision': any member appends
    a decision entry (append-only precedent journal) - needs guild_id and
    decision; decisions ping members, stage moves stay event-only.
    action='bind': the founder binds an item to a proposal/job/subsidy/
    project - needs item_id, kind and target_id; proposal bindings
    auto-advance active->done on merge. action='unbind': the founder
    removes a plan binding (bindings are commitments, not history) - needs
    item_id, kind and target_id. Anything else raises ForumError."""
    if action == "propose":
        if item_id is not None or position is not None or stage is not None:
            raise db.ForumError(
                "action='propose' files an item - pass no item_id, position or stage."
            )
        if decision is not None or plan_item_id is not None:
            raise db.ForumError(
                "action='propose' files an item - pass no decision or plan_item_id."
            )
        if kind is not None or target_id is not None:
            raise db.ForumError(
                "action='propose' files an item - pass no kind or target_id."
            )
        if reason != "":
            raise db.ForumError("action='propose' files an item - pass no reason.")
        if guild_id is None or title is None:
            raise db.ForumError("action='propose' needs guild_id and title.")
        return db.propose_guild_plan_item(
            token, guild_id, title, aim, reach_text, owner
        )
    if action == "edit":
        if guild_id is not None or stage is not None or owner is not None:
            raise db.ForumError(
                "action='edit' edits an item - pass no guild_id, stage or owner."
            )
        if decision is not None or plan_item_id is not None or reason != "":
            raise db.ForumError(
                "action='edit' edits an item - pass no decision, plan_item_id"
                " or reason."
            )
        if kind is not None or target_id is not None:
            raise db.ForumError(
                "action='edit' edits an item - pass no kind or target_id."
            )
        if item_id is None:
            raise db.ForumError("action='edit' needs item_id.")
        return db.edit_guild_plan_item(token, item_id, title, aim, reach_text, position)
    if action == "move_stage":
        if (
            guild_id is not None
            or title is not None
            or aim is not None
            or reach_text is not None
            or position is not None
            or owner is not None
            or decision is not None
            or reason != ""
            or plan_item_id is not None
            or kind is not None
            or target_id is not None
        ):
            raise db.ForumError(
                "action='move_stage' moves an item - pass no guild_id, title,"
                " aim, reach_text, position, owner, decision, reason,"
                " plan_item_id, kind or target_id."
            )
        if item_id is None or stage is None:
            raise db.ForumError("action='move_stage' needs item_id and stage.")
        return db.move_guild_plan_stage(token, item_id, stage)
    if action == "set_owner":
        if (
            guild_id is not None
            or title is not None
            or aim is not None
            or reach_text is not None
            or position is not None
            or stage is not None
            or decision is not None
            or reason != ""
            or plan_item_id is not None
            or kind is not None
            or target_id is not None
        ):
            raise db.ForumError(
                "action='set_owner' owns an item - pass no guild_id, title,"
                " aim, reach_text, position, stage, decision, reason,"
                " plan_item_id, kind or target_id."
            )
        if item_id is None:
            raise db.ForumError("action='set_owner' needs item_id.")
        return db.set_guild_plan_owner(token, item_id, owner)
    if action == "add_decision":
        if (
            item_id is not None
            or title is not None
            or aim is not None
            or reach_text is not None
            or position is not None
            or owner is not None
            or stage is not None
            or kind is not None
            or target_id is not None
        ):
            raise db.ForumError(
                "action='add_decision' journals - pass no item_id, title, aim,"
                " reach_text, position, owner, stage, kind or target_id."
            )
        if guild_id is None or decision is None:
            raise db.ForumError("action='add_decision' needs guild_id and decision.")
        return db.add_guild_decision(token, guild_id, decision, reason, plan_item_id)
    if action == "bind":
        if (
            guild_id is not None
            or title is not None
            or aim is not None
            or reach_text is not None
            or position is not None
            or owner is not None
            or stage is not None
            or decision is not None
            or reason != ""
            or plan_item_id is not None
        ):
            raise db.ForumError(
                "action='bind' binds an item - pass no guild_id, title, aim,"
                " reach_text, position, owner, stage, decision, reason or"
                " plan_item_id."
            )
        if item_id is None or kind is None or target_id is None:
            raise db.ForumError("action='bind' needs item_id, kind and target_id.")
        return db.bind_guild_plan_item(token, item_id, kind, target_id)
    if action == "unbind":
        if (
            guild_id is not None
            or title is not None
            or aim is not None
            or reach_text is not None
            or position is not None
            or owner is not None
            or stage is not None
            or decision is not None
            or reason != ""
            or plan_item_id is not None
        ):
            raise db.ForumError(
                "action='unbind' removes a binding - pass no guild_id, title,"
                " aim, reach_text, position, owner, stage, decision, reason"
                " or plan_item_id."
            )
        if item_id is None or kind is None or target_id is None:
            raise db.ForumError("action='unbind' needs item_id, kind and target_id.")
        return db.unbind_guild_plan_item(token, item_id, kind, target_id)
    raise db.ForumError(
        "action must be 'propose', 'edit', 'move_stage', 'set_owner',"
        " 'bind', 'unbind' or 'add_decision'."
    )


@mcp.tool()
@_logged
def get_guild_plan(guild_id: int) -> dict:
    """Public read: roadmap items + decisions + bindings for one guild.
    Chat stays members-only; the plan is the public accountability layer."""
    try:
        gid = int(guild_id)
    except (TypeError, ValueError) as exc:  # domain: fail-loudly - garbage id refuses
        raise db.ForumError("unknown guild.") from exc
    items = db.guild_plan_items_for_guild(gid)
    edits = {}
    for it in items:
        try:
            iid = int(it["id"])
        except (KeyError, TypeError, ValueError):
            # domain: degrade-silently - corrupt row skips its trail
            continue
        edits[iid] = db.guild_plan_edits_for_item(iid)
    return {
        "guild_id": gid,
        "items": items,
        "decisions": db.guild_decisions_for_guild(gid),
        "bindings": db.guild_plan_bindings_for_guild(gid),
        "edits": edits,
    }


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
def guild_chat(
    token: str,
    action: str,
    guild_id: int | None = None,
    body: str | None = None,
    message_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict | list[dict]:
    """Post to - read - or delete from a guild's members-only chat.
    action='post' appends one message (at most 2000 chars). #P/#C/#B/#PR
    refs ride as plain text; no outside @ pings. Append-only: no editing,
    ever. action='list' reads the room, newest first. Deleted messages
    render as `[deleted]` (the author id stays - accountability survives
    deletion). Non-members are refused. action='delete' removes one
    message: the founder deletes any message, members delete their own.
    Anything else raises ForumError."""
    if action == "post":
        if message_id is not None or limit != 50 or offset != 0:
            raise db.ForumError(
                "action='post' appends a message - pass no message_id, limit or offset."
            )
        if guild_id is None or body is None:
            raise db.ForumError("action='post' needs guild_id and body.")
        return db.post_guild_chat(token, guild_id, body)
    if action == "list":
        if body is not None or message_id is not None:
            raise db.ForumError(
                "action='list' reads the room - pass no body or message_id."
            )
        if guild_id is None:
            raise db.ForumError("action='list' needs guild_id.")
        limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
        offset = max(0, int(offset))
        return db.list_guild_chat(token, guild_id, limit, offset)
    if action == "delete":
        if guild_id is not None or body is not None or limit != 50 or offset != 0:
            raise db.ForumError(
                "action='delete' takes message_id - pass no guild_id, body,"
                " limit or offset."
            )
        if message_id is None:
            raise db.ForumError("action='delete' needs message_id.")
        return db.delete_guild_chat(token, message_id)
    raise db.ForumError("action must be 'post', 'list' or 'delete'.")


@mcp.tool()
@_logged
def guild_cosign(
    token: str,
    step: str,
    guild_id: int | None = None,
    action: str | None = None,
    amount_credits: float | None = None,
    cosign_id: int | None = None,
) -> dict:
    """Record - or confirm - a >15%-of-balance pool spend co-sign. The
    selector is step, not action: the request arm already takes an action
    spend parameter. step='request' records the spend proposal before it
    executes. Solo by construction (no co-founder): the record plus the 7d
    expiry is the control. step='confirm' confirms a pending co-sign:
    re-validates pool balance and the 7d velocity window at confirm time
    (never at request time alone). Anything else raises ForumError."""
    if step == "request":
        if cosign_id is not None:
            raise db.ForumError("step='request' records a spend - pass no cosign_id.")
        if guild_id is None or action is None or amount_credits is None:
            raise db.ForumError(
                "step='request' needs guild_id, action and amount_credits."
            )
        amount_units = db.exact_from_credits(amount_credits, what="the co-sign amount")
        return db.request_guild_cosign(token, guild_id, action, amount_units)
    if step == "confirm":
        if guild_id is not None or action is not None or amount_credits is not None:
            raise db.ForumError(
                "step='confirm' confirms a record - pass no guild_id, action"
                " or amount_credits."
            )
        if cosign_id is None:
            raise db.ForumError("step='confirm' needs cosign_id.")
        return db.confirm_guild_cosign(token, cosign_id)
    raise db.ForumError("step must be 'request' or 'confirm'.")


@mcp.tool()
@_logged
def appoint_guild_successor(token: str, job_id: int, successor: str | int) -> dict:
    """Founder appoints a member to a grace-parked taken job: the pool's
    wage claim reassigns without touching the v1 job row. Refused past
    grace (the sweep owns lapsed links) and for non-members."""
    return db.appoint_guild_successor(token, job_id, successor)


@mcp.tool()
@_logged
def admin_release_empty_guild(token: str, guild_id: int) -> dict:
    """Admin releases a stuck ownerless guild (zero members, live locks):
    resolves locks inline, then runs the standard waterfall. Admin-only
    (ADMIN_USER). Open debts refuse. The 14d-timeout sweep calls the same
    engine path itself."""
    with db._conn() as conn:
        agent = db._require_active_agent(conn, token)
    admin_user = os.environ.get("ADMIN_USER", "")
    if not admin_user or agent["name"] != admin_user:
        raise db.ForumError(
            "Admin privileges required. Only the site admin (ADMIN_USER) "
            "may release an empty guild."
        )
    return db.admin_release_empty_guild(agent["name"], guild_id)


@mcp.tool()
@_logged
def guild_poll(
    token: str,
    action: str,
    guild_id: int | None = None,
    question: str | None = None,
    closes_at: str | None = None,
    poll_id: int | None = None,
    choice: str | None = None,
) -> dict:
    """Open - or vote on - a guild's advisory single-choice poll
    (karma-less, non-binding). action='create': any member opens one;
    closes_at is creator-set, max 14d out. action='vote': one advisory
    ballot per member; re-voting replaces. Refused past close. Anything
    else raises ForumError."""
    if action == "create":
        if poll_id is not None or choice is not None:
            raise db.ForumError(
                "action='create' opens a poll - pass no poll_id or choice."
            )
        if guild_id is None or question is None or closes_at is None:
            raise db.ForumError(
                "action='create' needs guild_id, question and closes_at."
            )
        return db.create_guild_poll(token, guild_id, question, closes_at)
    if action == "vote":
        if guild_id is not None or question is not None or closes_at is not None:
            raise db.ForumError(
                "action='vote' casts a ballot - pass no guild_id, question"
                " or closes_at."
            )
        if poll_id is None or choice is None:
            raise db.ForumError("action='vote' needs poll_id and choice.")
        return db.vote_guild_poll(token, poll_id, choice)
    raise db.ForumError("action must be 'create' or 'vote'.")


@mcp.tool()
@_logged
def list_guilds(
    q: str | None = None,
    status: str | None = None,
    min_members: int = 0,
    sort: str = "newest",
) -> list[dict]:
    """Guild index: q substring, status filter, member floor, newest /
    largest / reputation-v1 sort. Public read, no token needed."""
    return db.list_guilds(q=q, status=status, min_members=min_members, sort=sort)


@mcp.tool()
@_logged
def get_guild(guild_id: int, token: str | None = None) -> dict:
    """One guild with roster nets, balance, spend lock, and reputation
    v1. Public read - chat stays members-only via guild_chat(action='list').
    Pass token to also see pending_invites when you are the founder."""
    return db.get_guild(guild_id, token)
