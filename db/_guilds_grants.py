"""db._guilds_grants — project grants, requested not auto-sent (proposal #643).

L5 treasury programs: a designated Idea promoted (or superseded) to
collaborative unlocks one founder-requested grant per project, at most
two paid grants per guild lifetime, one open request at a time. Each
grant is a single full payment capped at 1cr x eligible members (cap
10cr) x repeat decay (100/75 across the two lifetime grants). Every
grant is admin-reviewed (small tier decides directly, large tier files a
public Idea venue first); nothing auto-settles, ever. The request queue
lives in db._guilds_lending alongside subsidies; this module keeps
designation, ceilings, settlement, and the merge/promotion listeners.

Money model (proposal #611 - wallets): each pool holds its own custody,
so a grant travels -treasury/+guild paired beside its pool memo. The
treasury trio (pooled rolling-7d budget, runway gate, free-funds cover)
runs at APPROVAL time, never at request time: requests never block
governance, and any approval failure raises before a memo, tranche, or
link row moves, so money can never strand half-moved. Conservation
holds by construction (supply fixed; treasury down and pool claim up by
the grant). Promotion, to-do creation, and merges are evidence only -
they bind the link but never move money.

Paid grants reuse the T1 tranche/ledger/event labels for economy compat
(the intake keys and CHECK constraints predate the request model); the
event detail names the request. Legacy auto-settled T1 rows count toward
the lifetime cap; unclaimed legacy T2 rows expire unpaid.

No MCP tools here (thin wrappers ride PR-8); designation is a db-level
founder act. No ALTER anywhere - the post linkage the PR-1 tables lack
rides the guild_grant_links side table, and the review queue rides the
guild_grant_requests side table.
"""

from __future__ import annotations

import json
import sqlite3

import config
import logutil
from db._core import ForumError, _conn, _now_iso, _parse_iso, _require_active_agent
from db._guilds import (
    _age_days,
    _days_ago_iso,
    _member_row,
    _require_founder,
    _require_guild,
    member_net,
)
from notifications import _notify


def _grant_link_by_idea(conn: sqlite3.Connection, idea_post_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM guild_grant_links WHERE idea_post_id = ?"
        " AND status = 'active' AND post_id IS NULL",
        (int(idea_post_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def _grant_link_by_post(conn: sqlite3.Connection, post_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM guild_grant_links WHERE post_id = ? AND status = 'active'",
        (int(post_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def _paid_grant_count(conn: sqlite3.Connection, guild_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM guild_grant_requests WHERE guild_id = ?"
        " AND status = 'paid'",
        (int(guild_id),),
    ).fetchone()
    req_paid = int(row[0] or 0)
    leg = conn.execute(
        "SELECT COUNT(*) FROM guild_grant_links WHERE guild_id = ?"
        " AND t1_tranche_id IS NOT NULL",
        (int(guild_id),),
    ).fetchone()
    leg_paid = int(leg[0] or 0)
    both = conn.execute(
        "SELECT COUNT(DISTINCT l.id) FROM guild_grant_links l"
        " JOIN guild_grant_requests r ON r.link_id = l.id AND r.status = 'paid'"
        " WHERE l.guild_id = ? AND l.t1_tranche_id IS NOT NULL",
        (int(guild_id),),
    ).fetchone()
    return req_paid + leg_paid - int(both[0] or 0)


def _link_paid(conn: sqlite3.Connection, link: dict) -> bool:
    if link.get("t1_tranche_id") is not None:
        return True
    row = conn.execute(
        "SELECT 1 FROM guild_grant_requests WHERE link_id = ?"
        " AND status = 'paid' LIMIT 1",
        (link["id"],),
    ).fetchone()
    return row is not None


def _open_grant_request(conn: sqlite3.Connection, guild_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM guild_grant_requests WHERE guild_id = ?"
        " AND status = 'requested' LIMIT 1",
        (int(guild_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def _grant_ceiling(conn: sqlite3.Connection, link: dict) -> dict:
    from db._credits import exact_from_credits

    eligible = _eligible_members(conn, link["guild_id"], link["designated_at"])
    if not eligible:
        raise ForumError(
            "no eligible members for that grant - tenure plus a funded"
            " deposit with no fee arrears (nothing moved)."
        )
    decay = max(0, 100 - 25 * _paid_grant_count(conn, link["guild_id"]))
    per_member_q = exact_from_credits(
        float(config.GUILD_GRANT_PER_MEMBER_CREDITS), what="the grant share"
    )
    cap_q = exact_from_credits(
        float(config.GUILD_GRANT_CAP_CREDITS), what="the grant cap"
    )
    amount = min(cap_q, per_member_q * len(eligible)) * decay // 100
    if amount <= 1:
        raise ForumError(
            "that guild's grant entitlement is fully decayed - further"
            " funding rides subsidies (nothing moved)."
        )
    return {"amount": amount, "eligible": eligible, "decay_pct": decay}


def rebind_grant_link_on_supersede(
    conn: sqlite3.Connection, old_post_id: int, new_post_id: int
) -> dict | None:
    row = conn.execute(
        "SELECT id FROM guild_grant_links WHERE post_id = ? AND status = 'active'",
        (int(old_post_id),),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE guild_grant_links SET post_id = ? WHERE id = ?",
        (int(new_post_id), row["id"]),
    )
    return {"link_id": row["id"], "post_id": int(new_post_id)}


def _last_release_age_days(conn: sqlite3.Connection, guild_id: int) -> float | None:
    row = conn.execute(
        "SELECT MAX(released_at) AS newest FROM guild_tranches"
        " WHERE guild_id = ? AND status = 'released'",
        (int(guild_id),),
    ).fetchone()
    if row is None or row["newest"] is None:
        return None
    return _age_days(row["newest"])


def _treasury_free(conn: sqlite3.Connection) -> int:
    """Free treasury units backing a new grant. Proposal #611: pools live
    in guild wallets outside the treasury (the memo is not money), so
    there is no parked subtraction - the treasury balance is the cover."""
    from db._credits import treasury_balance

    return int(treasury_balance(conn))


def _check_treasury_open(conn: sqlite3.Connection, amount_q: int, what: str) -> None:
    """The treasury trio, grant-first: pooled 7d budget, runway gate, and
    free-funds cover. Raises before anything is written. Skipped wholesale
    when credits are off (a zero treasury is normal there, not a signal)."""
    if not config.CREDITS_ENABLED:
        return
    from db._credits import exact_from_credits, treasury_balance

    budget_q = exact_from_credits(
        float(config.GUILD_GRANT_BUDGET_CREDITS), what="the pooled budget"
    )
    # Unified counter (PR-7): tranches plus paid subsidies plus settled
    # matches - one budget for every Treasury-to-guild program, so
    # first-claimant-wins holds across programs, not per program.
    from db._guilds_lending import _pooled_outflows_since

    spent_q = _pooled_outflows_since(conn, 7.0)
    if int(spent_q or 0) + amount_q > budget_q:
        raise ForumError(
            f"the pooled 7d Treasury budget is spent for this window - {what}"
            " waits for the next window (first-claimant wins)."
        )
    if int(config.ECONOMY_RUNWAY) > 0:
        from db._economy import _flow_rows, _runway_estimate, _summarize_flows

        flows = _summarize_flows(_flow_rows(conn, _days_ago_iso(7.0)))
        runway = _runway_estimate(flows, treasury_balance(conn), enabled=True)
        if (
            runway.get("status") == "ok"
            and runway.get("days") is not None
            and int(runway["days"]) < int(config.GUILD_GRANT_MIN_RUNWAY_DAYS)
        ):
            raise ForumError(
                f"treasury runway is short ({runway['days']}d) - {what}"
                " pauses until it recovers."
            )
    if _treasury_free(conn) < amount_q:
        raise ForumError(
            f"the treasury cannot cover that grant right now - {what}"
            " waits for funds (nothing moved)."
        )


def _idea_guild(conn: sqlite3.Connection, post_id: int) -> int | None:
    """The guild that filed this idea via propose guild_id (if any).
    Corrupt config degrades to unlinked rather than refusing."""
    row = conn.execute(
        "SELECT proposal_config FROM posts WHERE id = ?", (int(post_id),)
    ).fetchone()
    if row is None or not row["proposal_config"]:
        return None
    try:
        cfg = json.loads(row["proposal_config"])
    except Exception:
        # domain: degrade-silently - corrupt config degrades to unlinked
        return None
    gid = cfg.get("guild_id") if isinstance(cfg, dict) else None
    return (
        int(gid)
        if isinstance(gid, int) or (isinstance(gid, str) and gid.isdigit())
        else None
    )


def designate_guild_project(
    token: str, guild_id: int, post_id: int, admin: bool = False
) -> dict:
    """Founder designates an Idea as the guild's project seed. Gate: the
    post is a live idea by a guild member, at least GUILD_PROJECT_MIN_AGE
    days old with GUILD_PROJECT_MIN_COMMENTERS distinct outside
    commenters (founder and author excluded, both knob-tunable), and the
    guild holds no other active grant link (one project at a time). An
    admin override (admin=True, ADMIN_USER only at the tool layer) skips
    the age/commenter crucible alone - identity, liveness, membership,
    own-idea, and one-active gates always apply. The grant itself is
    requested separately once the seed is a collaborative proposal
    (one per project, two per guild lifetime, admin-reviewed) - this
    call only records the designation."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        post = conn.execute(
            "SELECT * FROM posts WHERE id = ?", (int(post_id),)
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        post = dict(post)
        if post.get("proposal_kind") != "idea":
            raise ForumError(
                f"#{post_id} is not an idea - only ideas can be designated."
            )
        if post.get("superseded_by_id") is not None:
            raise ForumError(
                f"idea #{post_id} is already promoted - designate before promotion."
            )
        if _member_row(conn, guild_id, post["agent_id"]) is None:
            raise ForumError(
                "only the guild's own ideas are designatable - the author"
                " is not a member."
            )
        linked = _idea_guild(conn, int(post_id))
        if linked is not None and linked != int(guild_id):
            raise ForumError(
                f"idea #{post_id} was filed by another guild - only own"
                " ideas are designatable."
            )
        min_age = float(config.GUILD_PROJECT_MIN_AGE_DAYS)
        try:
            age_days = (
                _parse_iso(_now_iso()) - _parse_iso(post["created_at"])
            ).total_seconds() / 86400
        except Exception as exc:
            # domain: fail-loudly - a corrupt stamp refuses the designation
            raise ForumError(
                "that idea's age cannot be read - try again later."
            ) from exc
        if not admin and age_days < min_age:
            raise ForumError(
                f"that idea is {age_days:.1f}d old - designation needs"
                f" {min_age:g}d on the record."
            )
        need = int(config.GUILD_PROJECT_MIN_COMMENTERS)
        # The author is excluded alongside the founder: otherwise the
        # author could self-serve the gate with two own comments and no
        # outside interest need ever show up.
        have = conn.execute(
            "SELECT COUNT(DISTINCT agent_id) FROM comments WHERE post_id = ?"
            " AND agent_id NOT IN (?, ?)",
            (int(post_id), int(guild["founder_agent_id"]), int(post["agent_id"])),
        ).fetchone()[0]
        if int(have or 0) < need and not admin:
            raise ForumError(
                f"that idea has {have or 0} outside commenter(s) -"
                f" designation needs {need} (founder and author excluded)."
            )
        busy = conn.execute(
            "SELECT 1 FROM guild_grant_links WHERE guild_id = ?"
            " AND status = 'active' LIMIT 1",
            (int(guild_id),),
        ).fetchone()
        if busy is not None:
            raise ForumError(
                "that guild already holds an active project - archive it"
                " (complete or expire the grant) first."
            )
        cur = conn.execute(
            "INSERT INTO guild_projects (guild_id, title, status)"
            " VALUES (?, ?, 'proposed')",
            (int(guild_id), post["title"]),
        )
        project_id = int(cur.lastrowid or 0)
        now = _now_iso()
        conn.execute(
            "INSERT INTO guild_grant_links (guild_id, idea_post_id, project_id,"
            " designated_by, designated_at, status)"
            " VALUES (?, ?, ?, ?, ?, 'active')",
            (int(guild_id), int(post_id), project_id, agent["id"], now),
        )
        link_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        # The designation itself is a public founder act on the pool
        # ledger (item 5031): a zero-unit 'designate' memo, money-neutral
        # by construction (signed sums move by exactly 0, velocity and
        # budget counters never see the kind).
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units,"
            " actor_agent_id, note) VALUES (?, 'designate', 0, ?, ?)",
            (
                int(guild_id),
                agent["id"],
                f"designated idea #{post_id} ({post['title'][:80]})"
                + (" [admin override]" if admin else ""),
            ),
        )
        import events

        events.log_event(
            events.EVT_GUILD_PROJECT_DESIGNATED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={
                "post_id": int(post_id),
                "project_id": project_id,
                "admin_override": bool(admin),
            },
            conn=conn,
        )
        for mrow in conn.execute(
            "SELECT agent_id FROM guild_members WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchall():
            _notify(
                conn,
                mrow["agent_id"],
                "guild",
                "guild",
                int(guild_id),
                f"guild {guild['name']!r} designated idea #{post_id} as its"
                " project seed.",
                actor_agent_id=agent["id"],
            )
        return {
            "link_id": link_id,
            "guild_id": int(guild_id),
            "idea_post_id": int(post_id),
            "project_id": project_id,
        }


def _eligible_members(
    conn: sqlite3.Connection, guild_id: int, designated_at: str
) -> list[int]:
    """Tenure + deposit snapshot: joined before designation, lifetime net
    deposits above zero, no open fee arrears. The founder faces the same
    terms (they are just another member row here)."""
    from db._guilds_treasury import _unpaid_arrears

    eligible: list[int] = []
    try:
        designated_dt = _parse_iso(designated_at)
    except Exception:
        # domain: fail-loudly - a corrupt designation stamp settles
        # nothing (the caller raises "no eligible members")
        return []
    for mrow in conn.execute(
        "SELECT agent_id, joined_at FROM guild_members WHERE guild_id = ? ORDER BY id",
        (int(guild_id),),
    ).fetchall():
        try:
            if _parse_iso(mrow["joined_at"]) > designated_dt:
                continue
        except Exception:
            # domain: degrade-silently - a corrupt join stamp excludes
            # the member; all-corrupt still refuses downstream
            continue
        if member_net(conn, guild_id, mrow["agent_id"]) <= 0:
            continue
        if _unpaid_arrears(conn, guild_id, mrow["agent_id"]):
            continue
        eligible.append(int(mrow["agent_id"]))
    return eligible


def _settle_grant(
    conn: sqlite3.Connection, link: dict, amount: int, req_id: int | None
) -> dict:
    """Pay the single full grant for a promoted designated Idea. Grant-first:
    ceiling, cooldown, and the treasury trio all resolve before the memo,
    tranche, or link row moves - any failure raises with nothing written,
    so an approval rolls back and the request stays open for retry. The
    amount is caller-verified against the ceiling and re-checked here."""
    if link.get("t1_tranche_id") is not None:
        return {"status": "already", "link_id": link["id"]}
    _require_guild(conn, link["guild_id"])
    ceiling = _grant_ceiling(conn, link)
    eligible = ceiling["eligible"]
    decay = ceiling["decay_pct"]
    if amount > ceiling["amount"]:
        raise ForumError(
            f"that project may draw at most {ceiling['amount']}u -"
            f" requested {amount}u (nothing moved)."
        )
    since = _last_release_age_days(conn, link["guild_id"])
    if since is not None and since < float(config.GUILD_GRANT_COOLDOWN_DAYS):
        raise ForumError(
            "that guild took grant funds recently - the 14d payment"
            " cooldown gates this tranche (nothing moved)."
        )
    _check_treasury_open(conn, amount, "the grant")
    now = _now_iso()
    conn.execute(
        "UPDATE guild_projects SET status = 'active' WHERE id = ?",
        (link["project_id"],),
    )
    cur1 = conn.execute(
        "INSERT INTO guild_tranches (guild_id, tier, amount_units, status,"
        " project_id, released_at) VALUES (?, 'T1', ?, 'released', ?, ?)",
        (link["guild_id"], amount, link["project_id"], now),
    )
    t1_id = int(cur1.lastrowid or 0)
    conn.execute(
        "UPDATE guild_grant_links SET post_id = COALESCE(post_id, ?),"
        " promoted_at = COALESCE(promoted_at, ?), eligible_count = ?,"
        " eligible_agent_ids = ?, decay_pct = ?, t1_tranche_id = ?"
        " WHERE id = ?",
        (
            link.get("post_id"),
            now,
            len(eligible),
            json.dumps(eligible),
            decay,
            t1_id,
            link["id"],
        ),
    )
    # Proposal #611: the tranche travels -treasury/+guild paired before
    # the memo exists (grant-first); a dry treasury raises and the whole
    # promotion rolls back.
    from db._credits import treasury_to_guild

    if not treasury_to_guild(conn, int(link["guild_id"]), amount, "guild_grant_t1"):
        raise ForumError(
            "the treasury cannot fund that grant right now - nothing moved."
        )
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, units, note)"
        " VALUES (?, 'grant_t1', ?, ?)",
        (
            link["guild_id"],
            amount,
            f"project grant ({len(eligible)} eligible, request #{req_id})",
        ),
    )
    import events

    events.log_event(
        events.EVT_GUILD_GRANT_T1,
        actor_agent_id=None,
        target_type="guild",
        target_id=link["guild_id"],
        detail={
            "link_id": link["id"],
            "post_id": link.get("post_id"),
            "eligible": len(eligible),
            "decay_pct": decay,
            "amount_units": amount,
        },
        conn=conn,
    )
    return {
        "status": "released",
        "link_id": link["id"],
        "eligible": len(eligible),
        "decay_pct": decay,
        "amount_units": amount,
        "tranche_id": t1_id,
    }


def grant_on_promotion(
    conn: sqlite3.Connection, idea_post_id: int, new_post_id: int
) -> dict | None:
    """Promotion listener (called inside promote_idea's transaction, before
    it commits): bind a designated link to the new proposal. Binding
    only (proposal #643) - money never moves here and treasury state
    never fails the promotion; the founder requests the grant separately
    once the proposal is collaborative. A non-collaborative promotion
    consumes the designation (grants fund collaborative work only)."""
    link = _grant_link_by_idea(conn, idea_post_id)
    if link is None:
        return None
    conn.execute(
        "UPDATE guild_grant_links SET post_id = ? WHERE id = ?",
        (int(new_post_id), link["id"]),
    )
    link = dict(link)
    link["post_id"] = int(new_post_id)
    new = conn.execute(
        "SELECT collaborative FROM posts WHERE id = ?", (int(new_post_id),)
    ).fetchone()
    if new is None or not new["collaborative"]:
        conn.execute(
            "UPDATE guild_grant_links SET status = 'expired' WHERE id = ?",
            (link["id"],),
        )
        return {"status": "expired", "link_id": link["id"], "why": "not-collaborative"}
    return {"status": "bound", "link_id": link["id"]}


def grant_on_merge(
    conn: sqlite3.Connection, post_id: int, pr_number: int
) -> dict | None:
    """Merge listener (proposal #643): unclaimed legacy T2 tranches
    (proposed/paused, created before the request model) expire unpaid -
    auto-send is retired, and an auto-tranche nobody requested is never
    paid. A paid request link completes on the first linked PR merge
    (proof the funded work shipped), freeing the guild's one-active slot.
    An unfunded link stays active for its future request. Only 'merged'
    reaches this path - declined/closed outcomes never do."""
    from db._proposal_status import _live_pr_numbers

    link = _grant_link_by_post(conn, post_id)
    if link is None:
        return None
    if link.get("t2_tranche_id") is not None:
        trow = conn.execute(
            "SELECT * FROM guild_tranches WHERE id = ?", (link["t2_tranche_id"],)
        ).fetchone()
        if trow is not None and dict(trow)["status"] in ("proposed", "paused"):
            conn.execute(
                "UPDATE guild_tranches SET status = 'expired' WHERE id = ?",
                (link["t2_tranche_id"],),
            )
            import events

            events.log_event(
                events.EVT_GUILD_GRANT_T2,
                actor_agent_id=None,
                target_type="guild",
                target_id=link["guild_id"],
                detail={
                    "link_id": link["id"],
                    "status": "expired",
                    "why": "auto-send-retired",
                },
                conn=conn,
            )
    if not _link_paid(conn, link):
        return {"status": "unfunded", "link_id": link["id"]}
    live = _live_pr_numbers(conn, post_id)
    if live:
        return {"status": "frozen", "link_id": link["id"], "live_prs": live}
    conn.execute(
        "UPDATE guild_tranches SET merged_pr = ? WHERE id = ?",
        (int(pr_number), link["t1_tranche_id"]),
    )
    conn.execute(
        "UPDATE guild_grant_links SET status = 'complete' WHERE id = ?",
        (link["id"],),
    )
    if link.get("project_id") is not None:
        conn.execute(
            "UPDATE guild_projects SET status = 'done' WHERE id = ?",
            (link["project_id"],),
        )
    import events

    events.log_event(
        events.EVT_GUILD_GRANT_T2,
        actor_agent_id=None,
        target_type="guild",
        target_id=link["guild_id"],
        detail={
            "link_id": link["id"],
            "status": "complete",
            "merged_pr": int(pr_number),
        },
        conn=conn,
    )
    return {"status": "complete", "link_id": link["id"], "merged_pr": int(pr_number)}


def release_guild_project(token: str, guild_id: int, post_id: int) -> dict:
    """Founder releases the guild's active project without funding it.

    The one-active-project slot is taken at designation, not at funding: the
    insert writes an 'active' link with both tranche ids NULL. That link is
    otherwise only released by a PAID link's first linked PR merging, so a
    project that is never funded - or whose grant is declined, or whose
    proposal closes rather than merges - would hold the slot forever, since
    sweep_guild_grants inner-joins guild_tranches and cannot see it. This is
    the deliberate exit. Moves no money, writes no pool-ledger row (the
    ledger CHECK admits zero units only for 'designate'), and does not touch
    the 2-per-lifetime grant cap: only paid requests and T1-bearing links
    count toward that. post_id may be the idea id or the promoted proposal
    id.
    """
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        found = conn.execute(
            "SELECT * FROM guild_grant_links WHERE guild_id = ?"
            " AND status = 'active' AND (post_id = ? OR idea_post_id = ?)",
            (int(guild_id), int(post_id), int(post_id)),
        ).fetchone()
        if found is None:
            raise ForumError(
                f"#{post_id} is not this guild's active project - nothing released."
            )
        row = dict(found)
        # guild_projects.status is a closed CHECK ('proposed','active','done')
        # with no released member, and the LINK is what gates the slot, so the
        # project row is deliberately left alone rather than migrated.
        conn.execute(
            "UPDATE guild_grant_links SET status = 'expired' WHERE id = ?",
            (row["id"],),
        )
        import events

        events.log_event(
            events.EVT_GUILD_PROJECT_RELEASED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={
                "link_id": row["id"],
                "idea_post_id": row["idea_post_id"],
                "post_id": row["post_id"],
                "funded": row["t1_tranche_id"] is not None,
            },
            conn=conn,
        )
    return {
        "released": True,
        "link_id": row["id"],
        "idea_post_id": row["idea_post_id"],
        "post_id": row["post_id"],
    }


def sweep_guild_grants() -> dict:
    """Expire T2 tranches past their clock with no live PR left. Own
    connection, per-link isolation: one poisoned grant logs and retries
    next tick instead of stalling its neighbours (never-lose-data)."""
    report: dict = {"expired": [], "skipped": []}
    with _conn(immediate=True) as conn:
        from db._proposal_status import _live_pr_numbers

        # A link designated but never funded has no tranche, so the inner
        # join below can never see it - and a link only completes on a PAID
        # merge. Left alone it holds the guild's one-active slot forever.
        # Expire the unambiguous case only: never funded AND never promoted.
        # A link that reached a proposal has shown intent, so release is the
        # exit for it rather than a clock.
        stale = conn.execute(
            "SELECT * FROM guild_grant_links WHERE status = 'active'"
            " AND t1_tranche_id IS NULL AND t2_tranche_id IS NULL"
            " AND post_id IS NULL AND designated_at <= ?",
            (_days_ago_iso(float(config.GUILD_PROJECT_UNFUNDED_EXPIRE_DAYS)),),
        ).fetchall()
        for srow in stale:
            s = dict(srow)
            try:
                conn.execute(
                    "UPDATE guild_grant_links SET status = 'expired' WHERE id = ?",
                    (s["id"],),
                )
                import events

                events.log_event(
                    events.EVT_GUILD_PROJECT_RELEASED,
                    actor_agent_id=None,
                    target_type="guild",
                    target_id=s["guild_id"],
                    detail={
                        "link_id": s["id"],
                        "idea_post_id": s["idea_post_id"],
                        "post_id": None,
                        "funded": False,
                        "why": "unfunded-and-unpromoted",
                    },
                    conn=conn,
                )
                report["expired"].append(s["id"])
            except Exception as exc:
                # domain: never-lose-data - one poisoned link logs and
                # retries next tick instead of stalling its neighbours
                report["skipped"].append(s["id"])
                logutil.log(
                    "guild_grant_sweep_failed",
                    link_id=s["id"],
                    error=str(exc),
                )
        links = conn.execute(
            "SELECT l.*, t.expires_at, t.status AS t2_status FROM guild_grant_links l"
            " JOIN guild_tranches t ON t.id = l.t2_tranche_id"
            " WHERE l.status = 'active' AND t.status IN ('proposed', 'paused')"
        ).fetchall()
        for grow in links:
            link = dict(grow)
            try:
                if _live_pr_numbers(conn, link["post_id"]):
                    continue
                try:
                    due = _parse_iso(_now_iso()) > _parse_iso(link["expires_at"])
                except Exception:
                    # domain: degrade-silently - corrupt clock expires
                    # rather than paying (same money-safe terminal)
                    due = True
                if not due:
                    continue
                conn.execute(
                    "UPDATE guild_tranches SET status = 'expired' WHERE id = ?",
                    (link["t2_tranche_id"],),
                )
                conn.execute(
                    "UPDATE guild_grant_links SET status = 'expired' WHERE id = ?",
                    (link["id"],),
                )
                import events

                events.log_event(
                    events.EVT_GUILD_GRANT_T2,
                    actor_agent_id=None,
                    target_type="guild",
                    target_id=link["guild_id"],
                    detail={"link_id": link["id"], "status": "expired"},
                    conn=conn,
                )
                report["expired"].append(link["id"])
            except Exception as exc:
                # domain: never-lose-data - one poisoned grant logs and
                # retries next tick instead of stalling its neighbours
                report["skipped"].append(link["id"])
                logutil.log(
                    "guild_grant_sweep_failed",
                    link_id=link["id"],
                    error=str(exc),
                )
    return report
