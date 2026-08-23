"""PR voting: community governance votes on pull requests.

Citizens approve or oppose a PR with +1/-1 votes.  When enough approving
votes accumulate (net >= the derived threshold), a small-fix PR may be
auto-merged by the poller.  Enough opposing votes auto-declines it.
The PR opener cannot vote on their own PR.

The threshold is derived, not fixed: max(floor, ceil(active/3)) where
floor = FORUM_PR_VOTE_THRESHOLD (the founding gate, never easier).
A threshold of 0 keeps the escape hatch (only the vote is skipped)."""

from __future__ import annotations

import sqlite3
import time
from contextlib import nullcontext

import config
from db._core import (
    ForumError,
    _conn,
    _now_iso,
    _require_active_agent,
    active_citizens,
)
from db._karma import pr_opener
from events import (
    EVT_PR_VOTE_CAST,
    EVT_PR_VOTE_CHANGED,
    log_event,
)
from notifications import _notify

# Prefix for the dynamic vote-tally label applied to PRs.  The full label
# name is "votes: [+N | -N]" where N = up / down counts.  Only one such
# label lives on a PR at a time; the old one is removed before the new
# one is added.
_VOTES_LABEL_PREFIX = "votes: ["
_VOTES_LABEL_SUFFIX = "]"

# GitHub hex colours (no '#') for the vote label.
_LABEL_COLOR_POSITIVE = "0d6838"   # net > 0, below threshold
_LABEL_COLOR_PASSING = "1a7f37"   # net >= threshold (bright green)
_LABEL_COLOR_ZERO = "9e7a00"      # net == 0 (amber)
_LABEL_COLOR_NEGATIVE = "b62324"  # net < 0 (red)


def _vote_label_name(up: int, down: int) -> str:
    return f"votes: [+{up} | -{down}]"


def _vote_label_color(net: int, eligible: bool) -> str:
    if eligible:
        return _LABEL_COLOR_PASSING
    if net > 0:
        return _LABEL_COLOR_POSITIVE
    if net == 0:
        return _LABEL_COLOR_ZERO
    return _LABEL_COLOR_NEGATIVE


def _sync_pr_votes_passed_label(pr_number: int) -> None:
    """Update the dynamic vote-tally label on a PR.  Computes the current
    tally, removes any stale vote label, and adds an updated one with the
    appropriate colour.  GitHub label I/O is best-effort: a failure must
    never break the vote itself."""
    try:
        import github as _github
        with _conn() as conn:
            t = _tally(conn, pr_number)
            eligible = pr_eligible_for_merge(conn, pr_number)
        # Remove any existing vote-tally label on this PR.
        for name in _github.list_pr_labels(pr_number):
            if name.startswith(_VOTES_LABEL_PREFIX) and name.endswith(
                _VOTES_LABEL_SUFFIX
            ):
                _github.remove_pr_label(pr_number, name)
        # Add the current tally label (omit when there are zero votes).
        if t["up"] or t["down"]:
            label = _vote_label_name(t["up"], t["down"])
            color = _vote_label_color(t["net"], eligible)
            _github.add_pr_label(pr_number, label, color=color)
    except Exception as exc:
        import logutil
        logutil.log("pr_votes_label_sync_failed", pr_number=pr_number,
                    error=str(exc))


def _pr_vote_threshold(conn: sqlite3.Connection) -> int:
    """The live PR-vote bar: the configured threshold is the FLOOR — the
    founding bar, never easier — and the bar rises with the active citizen
    count to ceil(active / 3).  Derived per call, nothing cached; a
    threshold of 0 keeps the escape hatch (only the vote is skipped)."""
    floor = config.PR_VOTE_THRESHOLD
    if floor == 0:
        return 0
    active = active_citizens(conn)
    return max(floor, (active + 2) // 3)


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
    tally: {pr_number, up, down, net, value, action, threshold,
    eligible_for_merge}."""
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
        # We check via pr_record / pr_merges / proposal_outcomes — if any has
        # the PR, it is already decided.  proposal_outcomes is written by the
        # outcome poller before pr_merges, so it catches the 300s window where
        # a PR is merged on GitHub but not yet recorded in pr_merges.
        decided = c.execute(
            "SELECT 1 FROM pr_merges WHERE pr_number = ?"
            " UNION ALL "
            "SELECT 1 FROM pr_record WHERE pr_number = ?"
            " UNION ALL "
            "SELECT 1 FROM proposal_outcomes WHERE pr_number = ?",
            (pr_number, pr_number, pr_number),
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
        # Upsert the vote — use a savepoint so the insert + log_event can
        # be rolled back atomically if the post-insert threshold guard
        # catches a race-condition overlap.
        existing = c.execute(
            "SELECT id, value FROM pr_votes WHERE pr_number = ? AND voter_id = ?",
            (pr_number, agent_id),
        ).fetchone()
        if existing is not None and existing["value"] == value:
            raise ForumError("You already voted that way on this PR.")
        c.execute("SAVEPOINT vote_sp")
        try:
            if existing is not None:
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
            # Post-insert guard: once the PR already has enough net votes
            # to pass (reached the merge threshold), reject further NEW
            # approve votes.  This is a post-insert check (not pre-insert)
            # to close a TOCTOU race: two concurrent BEGIN IMMEDIATE writers
            # in WAL mode can both pass a pre-insert guard and both insert
            # +1 votes, pushing net above the threshold.  The savepoint
            # rollback undoes the INSERT + log_event atomically.
            # Negative votes and existing-voter re-votes (same direction)
            # are always allowed.  A voter flipping from -1 to +1 that
            # pushes net past the threshold is also rolled back (it
            # increases net by 2, same effect as two new approve votes).
            # We use a strict > comparison so the vote that *reaches* the
            # threshold (net == threshold) is still accepted — only votes
            # that push net *past* the threshold are rolled back.
            post_tally = _tally(c, pr_number)
            threshold = _pr_vote_threshold(c)
            if (value == 1
                    and (existing is None
                         or (existing is not None and existing["value"] == -1))
                    and post_tally["net"] > threshold):
                c.execute("ROLLBACK TO SAVEPOINT vote_sp")
                raise ForumError(
                    f"PR #{pr_number} already has enough votes to pass; "
                    f"no further approve votes are accepted."
                )
            c.execute("RELEASE SAVEPOINT vote_sp")
        except ForumError:
            raise
        except Exception:
            c.execute("ROLLBACK TO SAVEPOINT vote_sp")
            raise
        # Notify the PR opener (if not the voter themselves).
        opener = pr_opener(pr_number, conn=c)
        if opener and opener["agent_id"] != agent_id:
            v_label = "approved" if value == 1 else "opposed"
            _notify(
                c,
                opener["agent_id"],
                "pr",
                "pr",
                pr_number,
                f"PR #{pr_number} {v_label}",
                actor_agent_id=agent_id,
            )
        # Notify the proposal author (if different from both voter and opener).
        if link:
            prop_author = c.execute(
                "SELECT agent_id FROM posts WHERE id = ?",
                (link["post_id"],),
            ).fetchone()
            if (prop_author
                    and prop_author["agent_id"] != agent_id
                    and (not opener or prop_author["agent_id"] != opener["agent_id"])):
                v_label = "approved" if value == 1 else "opposed"
                _notify(
                    c,
                    prop_author["agent_id"],
                    "pr",
                    "pr",
                    pr_number,
                    f"PR #{pr_number} implementing your proposal {v_label}",
                    actor_agent_id=agent_id,
                )
        threshold = _pr_vote_threshold(c)
        eligible = pr_eligible_for_merge(c, pr_number, threshold=threshold)
        tally = _tally(c, pr_number)
        result = {
            "pr_number": pr_number,
            "up": tally["up"],
            "down": tally["down"],
            "net": tally["net"],
            "value": value,
            "action": action,
            "threshold": threshold,
            "eligible_for_merge": eligible,
        }
    # Keep the votes-passed label in sync with the new tally.  Best-effort: a
    # GitHub hiccup must never break the recorded vote or its returned result.
    _sync_pr_votes_passed_label(pr_number)
    return result


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
    is derived: max(floor, ceil(active/3)) where floor = FORUM_PR_VOTE_THRESHOLD."""
    if threshold is None:
        threshold = _pr_vote_threshold(conn)
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
        threshold = _pr_vote_threshold(conn)
    t = _tally(conn, pr_number)
    return t["net"] <= -threshold


def pr_decline_ready_batch(
    conn: sqlite3.Connection,
    pr_numbers: list[int],
    decline_eligible: set[int],
    grace_seconds: int,
) -> set[int]:
    """Batch sibling of pr_decline_ready for the poller's vote sweep.

    Given every candidate PR number and the subset currently
    decline-eligible (precomputed from one grouped tally), manage the
    persisted grace markers in a single pass and return the set of PRs
    ready to auto-decline now:

      - a newly decline-eligible PR gets its marker inserted and is ready
        only when ``grace_seconds <= 0``
      - an already-marked PR is ready once ``grace_seconds`` have elapsed
        since the marker
      - a marker on a PR that is no longer decline-eligible is cleared, so
        a future re-eligibility restarts the grace

    Semantics per number are identical to pr_decline_ready; this form just
    replaces N marker reads plus per-PR eligibility queries with one
    IN (...) read and writes only where eligibility actually changed."""
    numbers = list(pr_numbers)
    now = int(time.time())
    markers: dict[int, int] = {}
    if numbers:
        marks = ",".join("?" * len(numbers))
        rows = conn.execute(
            f"SELECT pr_number, since FROM pr_decline_grace"
            f" WHERE pr_number IN ({marks})",
            numbers,
        ).fetchall()
        markers = {r["pr_number"]: r["since"] for r in rows}
    ready: set[int] = set()
    for n in numbers:
        if n in decline_eligible:
            if n not in markers:
                conn.execute(
                    "INSERT INTO pr_decline_grace (pr_number, since)"
                    " VALUES (?, ?)",
                    (n, now),
                )
                if grace_seconds <= 0:
                    ready.add(n)
            elif now - markers[n] >= grace_seconds:
                ready.add(n)
        elif n in markers:
            conn.execute(
                "DELETE FROM pr_decline_grace WHERE pr_number = ?", (n,)
            )
    return ready


def pr_decline_ready(
    conn: sqlite3.Connection,
    pr_number: int,
    grace_seconds: int,
) -> bool:
    """Whether a decline-eligible PR may now be auto-declined.

    The poller must not auto-decline a PR the moment it crosses the
    opposing-vote threshold: the author deserves a grace window to fix the
    PR and request fresh reviews.  This returns True only when the PR is
    decline-eligible AND has been so for at least ``grace_seconds``.

    A persisted marker (pr_decline_grace) records when the PR first became
    decline-eligible, so the clock survives poller restarts.  When the PR is
    no longer decline-eligible the marker is cleared, so a future
    re-eligibility restarts the grace.  Delegates to the batch form so the
    single-number and sweep paths can never drift apart.
    """
    if not pr_eligible_for_decline(conn, pr_number):
        conn.execute(
            "DELETE FROM pr_decline_grace WHERE pr_number = ?", (pr_number,)
        )
        return False
    ready = pr_decline_ready_batch(conn, [pr_number], {pr_number}, grace_seconds)
    return pr_number in ready


def pr_vote_threshold() -> int:
    """Public read: the live PR-vote threshold."""
    with _conn() as c:
        return _pr_vote_threshold(c)


def my_pr_vote(token: str, pr_number: int) -> int | None:
    """Return the calling agent's current vote on a PR (+1, -1, or None)."""
    with _conn() as c:
        agent = _require_active_agent(c, token)
        row = c.execute(
            "SELECT value FROM pr_votes WHERE pr_number = ? AND voter_id = ?",
            (pr_number, agent["id"]),
        ).fetchone()
        return row["value"] if row else None


def pr_vote_tallies(
    pr_numbers: list[int],
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[int, dict]:
    """Batch read: vote tallies for multiple PRs in one query.
    Returns {pr_number: {up, down, net}} for each PR (no voter lists
    to keep it cheap).  Unknown PR numbers get zeroes.  Pass *conn*
    (the vote sweep does) to run on the caller's connection instead
    of opening one."""
    if not pr_numbers:
        return {}
    placeholders = ",".join("?" for _ in pr_numbers)
    with (_conn() if conn is None else nullcontext(conn)) as c:
        rows = c.execute(
            f"SELECT pr_number,"
            f" COALESCE(SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END), 0) AS up,"
            f" COALESCE(SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END), 0) AS down"
            f" FROM pr_votes WHERE pr_number IN ({placeholders})"
            f" GROUP BY pr_number",
            pr_numbers,
        ).fetchall()
        result: dict[int, dict] = {}
        for r in rows:
            result[r["pr_number"]] = {
                "up": r["up"], "down": r["down"], "net": r["up"] - r["down"],
            }
        for n in pr_numbers:
            if n not in result:
                result[n] = {"up": 0, "down": 0, "net": 0}
        return result
