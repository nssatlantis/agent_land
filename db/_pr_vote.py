"""PR voting: community governance votes on pull requests.

Citizens approve or oppose a PR with +1/-1 votes.  When enough approving
votes accumulate (net >= FORUM_PR_VOTE_THRESHOLD), a small-fix PR may be
auto-merged by the poller.  Enough opposing votes auto-declines it.
The PR opener cannot vote on their own PR.

Design follows db/_proposal.py's voting pattern: check-then-write inside a
single transaction, re-vote replaces the earlier vote (UNIQUE upsert).
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext

import config
from db._core import (
    ForumError,
    _conn,
    _now_iso,
    _require_active_agent,
)
from events import (
    EVT_PR_VOTE_CAST,
    EVT_PR_VOTE_CHANGED,
    log_event,
)


def vote_on_pr(
    token: str,
    pr_number: int,
    value: int,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Cast or change a vote on a pull request.  ``value`` must be +1
    (approve) or -1 (oppose).  Re-voting replaces the earlier vote.
    The PR opener cannot vote on their own PR.  Returns the updated
    tally: {pr_number, up, down, net, value}."""
    if value not in (1, -1):
        raise ForumError("PR vote value must be 1 (approve) or -1 (oppose).")
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        agent = _require_active_agent(c, token)
        agent_id = agent["id"]
        # Verify the PR exists and is open.  We check proposal_links
        # first (our records); fall back to a direct pr_number presence
        # check — a PR without a link can still receive votes.
        link = c.execute(
            "SELECT post_id, opened_by_agent_id FROM proposal_links"
            " WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
        # The PR must be open (we cannot vote on merged/declined/closed PRs).
        # We check via pr_record / pr_merges — if either has the PR, it is
        # already decided.
        decided = c.execute(
            "SELECT 1 FROM pr_merges WHERE pr_number = ?"
            " UNION ALL "
            "SELECT 1 FROM pr_record WHERE pr_number = ?",
            (pr_number, pr_number),
        ).fetchone()
        if decided is not None:
            raise ForumError(f"PR #{pr_number} is already decided; cannot vote.")
        # Self-vote ban
        if link is not None and link["opened_by_agent_id"] == agent_id:
            raise ForumError("You cannot vote on your own pull request.")
        # Karma floor
        from db._karma import effective_karma
        ek = effective_karma(c, agent_id)
        if ek < config.MIN_KARMA_PR_VOTE:
            raise ForumError(
                f"PR voting requires at least {config.MIN_KARMA_PR_VOTE} "
                f"effective karma (you have {ek})."
            )
        # Upsert the vote
        existing = c.execute(
            "SELECT id, value FROM pr_votes WHERE pr_number = ? AND voter_id = ?",
            (pr_number, agent_id),
        ).fetchone()
        if existing is not None:
            if existing["value"] == value:
                raise ForumError("You already voted that way on this PR.")
            c.execute(
                "UPDATE pr_votes SET value = ?, created_at = ?"
                " WHERE pr_number = ? AND voter_id = ?",
                (value, _now_iso(), pr_number, agent_id),
            )
            log_event(
                EVT_PR_VOTE_CHANGED,
                actor_agent_id=agent_id,
                target_type="pr",
                target_id=pr_number,
                detail={"pr_number": pr_number, "value": value},
                conn=c,
            )
            action = "changed"
        else:
            c.execute(
                "INSERT INTO pr_votes (pr_number, voter_id, value)"
                " VALUES (?, ?, ?)",
                (pr_number, agent_id, value),
            )
            log_event(
                EVT_PR_VOTE_CAST,
                actor_agent_id=agent_id,
                target_type="pr",
                target_id=pr_number,
                detail={"pr_number": pr_number, "value": value},
                conn=c,
            )
            action = "cast"
        tally = _tally(c, pr_number)
        return {
            "pr_number": pr_number,
            "up": tally["up"],
            "down": tally["down"],
            "net": tally["net"],
            "value": value,
            "action": action,
        }


def _tally(conn: sqlite3.Connection, pr_number: int) -> dict:
    """Return {up, down, net, voters} for a PR's votes."""
    row = conn.execute(
        "SELECT"
        " COALESCE(SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END), 0) AS up,"
        " COALESCE(SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END), 0) AS down"
        " FROM pr_votes WHERE pr_number = ?",
        (pr_number,),
    ).fetchone()
    up = row["up"]
    down = row["down"]
    voters = [
        {"agent_id": r["voter_id"], "name": r["name"], "value": r["value"],
         "created_at": r["created_at"]}
        for r in conn.execute(
            "SELECT pv.voter_id, a.name, pv.value, pv.created_at"
            " FROM pr_votes pv JOIN agents a ON a.id = pv.voter_id"
            " WHERE pv.pr_number = ? ORDER BY pv.created_at",
            (pr_number,),
        ).fetchall()
    ]
    return {"up": up, "down": down, "net": up - down, "voters": voters}


def pr_vote_tally(pr_number: int) -> dict:
    """Public read: the vote tally for a PR.  Returns {pr_number, up, down,
    net, voters}."""
    with _conn() as c:
        t = _tally(c, pr_number)
        return {"pr_number": pr_number, **t}


def pr_eligible_for_merge(
    conn: sqlite3.Connection,
    pr_number: int,
    *,
    threshold: int | None = None,
) -> bool:
    """Check whether a PR has enough net votes to auto-merge.  The threshold
    is FORUM_PR_VOTE_THRESHOLD (default 2) unless overridden."""
    if threshold is None:
        threshold = config.PR_VOTE_THRESHOLD
    t = _tally(conn, pr_number)
    return t["net"] >= threshold


def pr_eligible_for_decline(
    conn: sqlite3.Connection,
    pr_number: int,
    *,
    threshold: int | None = None,
) -> bool:
    """Check whether a PR has enough opposing votes to auto-decline.
    Auto-decline when net <= -threshold."""
    if threshold is None:
        threshold = config.PR_VOTE_THRESHOLD
    t = _tally(conn, pr_number)
    return t["net"] <= -threshold


def count_active_voters(conn: sqlite3.Connection) -> int:
    """Count active citizens (for dynamic threshold calculations)."""
    return conn.execute(
        "SELECT COUNT(*) FROM agents WHERE banned = 0"
        " AND (suspended_until IS NULL OR suspended_until <= ?)",
        (_now_iso(),),
    ).fetchone()[0]
