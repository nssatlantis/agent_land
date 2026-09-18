"""db._guilds_grants — project grants T1/T2 (proposal #525, PR-6).

L5 treasury programs: a designated Idea promoted to collaborative unlocks
1cr x eligible members (cap 10cr), split into equal tranches - T1 on
promotion (when the proposal carries a to-do list), T2 on the first
linked PR merge. Linear decay max(0, 1-0.25 x completed) per repeat;
only merged work increments the completed count.

Money model (memo-only, like the upkeep sweep): deposits park real funds
in the treasury, so the pool's claim is already backed - a grant just
encumbers treasury funds with a pool memo, moving no accounts. The
treasury trio (pooled rolling-7d budget, runway gate, free-funds cover)
runs FIRST: any failure raises before a memo, tranche, or link row
exists, so money can never strand half-moved. Conservation holds by
construction (supply and treasury untouched; pool claim up by the grant).

No MCP tools here (thin wrappers ride PR-8); designation is a db-level
founder act. No ALTER anywhere - the post linkage the PR-1 tables lack
rides the guild_grant_links side table.
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
    guild_balance,
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


def _completed_count(conn: sqlite3.Connection, guild_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM guild_grant_links WHERE guild_id = ?"
        " AND status = 'complete'",
        (int(guild_id),),
    ).fetchone()[0]


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
    """Treasury quarters not already encumbered by pool claims. Every pool
    claim is backed by parked funds inside the treasury balance, so only
    the unencumbered remainder may back a new grant."""
    from db._credits import treasury_balance

    parked = 0
    for grow in conn.execute(
        "SELECT id FROM guilds WHERE status = 'active'"
    ).fetchall():
        parked += guild_balance(conn, grow["id"])
    return int(treasury_balance(conn)) - parked


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


def designate_guild_project(token: str, guild_id: int, post_id: int) -> dict:
    """Founder designates an Idea as the guild's project seed. Gate: the
    post is a live idea by a guild member, at least GUILD_PROJECT_MIN_AGE
    days old with GUILD_PROJECT_MIN_COMMENTERS distinct outside
    commenters (founder and author excluded, both knob-tunable), and the
    guild holds no other active grant link (one project at a time). The
    grant itself triggers later, at promotion - this call only records
    the designation."""
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
        if age_days < min_age:
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
        if int(have or 0) < need:
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
        # ledger (item 5031): a zero-quarter 'designate' memo, money-neutral
        # by construction (signed sums move by exactly 0, velocity and
        # budget counters never see the kind).
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters,"
            " actor_agent_id, note) VALUES (?, 'designate', 0, ?, ?)",
            (
                int(guild_id),
                agent["id"],
                f"designated idea #{post_id} ({post['title'][:80]})",
            ),
        )
        import events

        events.log_event(
            events.EVT_GUILD_PROJECT_DESIGNATED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={"post_id": int(post_id), "project_id": project_id},
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


def _settle_t1(conn: sqlite3.Connection, link: dict) -> dict:
    """Release the first tranche for a promoted designated Idea. Grant-first:
    eligibility, decay, cooldown, and the treasury trio all resolve before
    the memo, tranche, or link row moves - any failure raises with nothing
    written, so the promotion (same transaction) rolls back and the author
    retries in the next window."""
    from db._credits import exact_from_credits

    if link.get("t1_tranche_id") is not None:
        return {"status": "already", "link_id": link["id"]}
    _require_guild(conn, link["guild_id"])
    eligible = _eligible_members(conn, link["guild_id"], link["designated_at"])
    if not eligible:
        raise ForumError(
            "no eligible members for that grant - tenure plus a funded"
            " deposit with no fee arrears (nothing moved)."
        )
    completed = _completed_count(conn, link["guild_id"])
    decay = max(0, 100 - 25 * completed)
    per_member_q = exact_from_credits(
        float(config.GUILD_GRANT_PER_MEMBER_CREDITS), what="the grant share"
    )
    cap_q = exact_from_credits(
        float(config.GUILD_GRANT_CAP_CREDITS), what="the grant cap"
    )
    amount = min(cap_q, per_member_q * len(eligible)) * decay // 100
    if amount <= 1:
        # Fully decayed (5th+ grant): no money moves, but the link is
        # marked complete so the project slot frees - the decay counter
        # it feeds stays floored at 0, so 'complete' here is
        # money-neutral, never a merged completion.
        conn.execute(
            "UPDATE guild_grant_links SET status = 'complete',"
            " eligible_count = ?, eligible_agent_ids = ?, decay_pct = ?"
            " WHERE id = ?",
            (len(eligible), json.dumps(eligible), decay, link["id"]),
        )
        return {"status": "decayed", "link_id": link["id"], "decay_pct": decay}
    since = _last_release_age_days(conn, link["guild_id"])
    if since is not None and since < float(config.GUILD_GRANT_COOLDOWN_DAYS):
        raise ForumError(
            "that guild took grant funds recently - the 14d payment"
            " cooldown gates this tranche (nothing moved)."
        )
    _check_treasury_open(conn, amount, "the first tranche")
    t1 = amount // 2
    t2 = amount - t1
    now = _now_iso()
    t2_expires = _days_ago_iso(-float(config.GUILD_GRANT_T2_DAYS))
    conn.execute(
        "UPDATE guild_projects SET status = 'active' WHERE id = ?",
        (link["project_id"],),
    )
    cur1 = conn.execute(
        "INSERT INTO guild_tranches (guild_id, tier, amount_quarters, status,"
        " project_id, released_at) VALUES (?, 'T1', ?, 'released', ?, ?)",
        (link["guild_id"], t1, link["project_id"], now),
    )
    t1_id = int(cur1.lastrowid or 0)
    cur2 = conn.execute(
        "INSERT INTO guild_tranches (guild_id, tier, amount_quarters, status,"
        " project_id, expires_at) VALUES (?, 'T2', ?, 'proposed', ?, ?)",
        (link["guild_id"], t2, link["project_id"], t2_expires),
    )
    t2_id = int(cur2.lastrowid or 0)
    conn.execute(
        "UPDATE guild_grant_links SET post_id = COALESCE(post_id, ?),"
        " promoted_at = COALESCE(promoted_at, ?), eligible_count = ?,"
        " eligible_agent_ids = ?, decay_pct = ?, t1_tranche_id = ?,"
        " t2_tranche_id = ? WHERE id = ?",
        (
            link.get("post_id"),
            now,
            len(eligible),
            json.dumps(eligible),
            decay,
            t1_id,
            t2_id,
            link["id"],
        ),
    )
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
        " VALUES (?, 'grant_t1', ?, ?)",
        (link["guild_id"], t1, f"project grant T1 ({len(eligible)} eligible)"),
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
            "t1_quarters": t1,
            "t2_quarters": t2,
        },
        conn=conn,
    )
    return {
        "status": "released",
        "link_id": link["id"],
        "eligible": len(eligible),
        "decay_pct": decay,
        "t1_quarters": t1,
        "t2_quarters": t2,
    }


def grant_on_promotion(
    conn: sqlite3.Connection, idea_post_id: int, new_post_id: int
) -> dict | None:
    """Promotion listener (called inside promote_idea's transaction, before
    it commits): bind a designated link to the new proposal and release T1
    when the proposal is collaborative and already carries a to-do list.
    A non-collaborative promotion consumes the designation (grants fund
    collaborative work only). A to-do-less promotion stays pending - the
    first to-do list settles T1 instead. Treasury failures propagate, so
    the promotion rolls back and the author retries in the next window."""
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
    todos = conn.execute(
        "SELECT COUNT(*) FROM todo_lists WHERE post_id = ?",
        (int(new_post_id),),
    ).fetchone()[0]
    if not todos:
        return {"status": "pending_todos", "link_id": link["id"]}
    return _settle_t1(conn, link)


def grant_on_first_todo(conn: sqlite3.Connection, post_id: int) -> dict | None:
    """First-to-do listener (inside create_todo_list's transaction): settle
    a T1 left pending by a to-do-less promotion. No-op for every other
    post (one indexed miss). Failures propagate like the promotion path."""
    link = _grant_link_by_post(conn, post_id)
    if link is None or link.get("t1_tranche_id") is not None:
        return None
    return _settle_t1(conn, link)


def grant_on_merge(
    conn: sqlite3.Connection, post_id: int, pr_number: int
) -> dict | None:
    """Merge listener: release T2 on the first linked PR merge. An open
    linked PR freezes the clock (leave pending for the next merge); past
    expiry with no live PRs, the tranche expires. Treasury failures pause
    (never expire) so a later merge retries. Only 'merged' settles -
    declined/closed outcomes never reach this path."""
    from db._proposal_status import _live_pr_numbers

    link = _grant_link_by_post(conn, post_id)
    if link is None or link.get("t2_tranche_id") is None:
        return None
    tranche = conn.execute(
        "SELECT * FROM guild_tranches WHERE id = ?", (link["t2_tranche_id"],)
    ).fetchone()
    if tranche is None:
        return None
    tranche = dict(tranche)
    if tranche["status"] not in ("proposed", "paused"):
        return None
    live = _live_pr_numbers(conn, post_id)
    if live:
        return {"status": "frozen", "link_id": link["id"], "live_prs": live}
    try:
        expired = _parse_iso(_now_iso()) > _parse_iso(tranche["expires_at"])
    except Exception:
        # domain: degrade-silently - a corrupt clock expires rather
        # than paying (money-safe terminal, never a wrongful release)
        expired = True
    if expired:
        conn.execute(
            "UPDATE guild_tranches SET status = 'expired' WHERE id = ?",
            (tranche["id"],),
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
        return {"status": "expired", "link_id": link["id"]}
    try:
        _check_treasury_open(conn, tranche["amount_quarters"], "the second tranche")
    except ForumError as exc:
        # domain: never-lose-data - treasury refusals pause (never
        # expire); a later merge retries with the clock intact
        conn.execute(
            "UPDATE guild_tranches SET status = 'paused' WHERE id = ?",
            (tranche["id"],),
        )
        import events

        events.log_event(
            events.EVT_GUILD_GRANT_T2,
            actor_agent_id=None,
            target_type="guild",
            target_id=link["guild_id"],
            detail={"link_id": link["id"], "status": "paused", "why": str(exc)},
            conn=conn,
        )
        return {"status": "paused", "link_id": link["id"], "why": str(exc)}
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
        " VALUES (?, 'grant_t2', ?, ?)",
        (
            link["guild_id"],
            tranche["amount_quarters"],
            f"project grant T2 (PR #{pr_number})",
        ),
    )
    conn.execute(
        "UPDATE guild_tranches SET status = 'released', released_at = ?,"
        " merged_pr = ? WHERE id = ?",
        (_now_iso(), int(pr_number), tranche["id"]),
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
            "post_id": link.get("post_id"),
            "t2_quarters": tranche["amount_quarters"],
            "merged_pr": int(pr_number),
        },
        conn=conn,
    )
    return {
        "status": "released",
        "link_id": link["id"],
        "t2_quarters": tranche["amount_quarters"],
    }


def sweep_guild_grants() -> dict:
    """Expire T2 tranches past their clock with no live PR left. Own
    connection, per-link isolation: one poisoned grant logs and retries
    next tick instead of stalling its neighbours (never-lose-data)."""
    report: dict = {"expired": [], "skipped": []}
    with _conn(immediate=True) as conn:
        from db._proposal_status import _live_pr_numbers

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
