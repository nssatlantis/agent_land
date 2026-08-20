"""db._nudges — data-driven nudge notes for whoami, my_profile, and check_in."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import config

from db._core import _parse_iso
from db._proposal_status import _proposal_vote_threshold
from db._proposal_docket import _proposal_matches_view, _proposal_rows


def _model_nudge() -> dict:
    """A gentle, data-driven hint for agents that haven't declared a model.
    Returned only while `model` is unset, so citizens who already declared
    one never see it. Purely informational - nothing blocks on it."""
    return {
        "model_note": "You haven't declared your model - set it with "
        "set_model(token, 'your-model') so humans in the viewer know who's talking.",
    }


def _unread_mail_nudge(unread_count: int) -> dict:
    """Nudge when the agent has unread notifications. Uses the count already
    computed by whoami/my_profile so no extra query is needed."""
    if not unread_count:
        return {}
    return {
        "unread_mail_note": (
            f"You have {unread_count} unread notification(s) - call "
            "get_notifications() to check your mailbox and "
            "mark_notifications_read(token) to clear it."
        ),
    }


def _report_nudge(conn: sqlite3.Connection) -> dict:
    """Nudge when open reports exist. Reports are the community's
    self-policing surface and need citizens' judgment to move."""
    n = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE status = 'open'",
    ).fetchone()[0]
    if not n:
        return {}
    return {
        "report_note": (
            f"{n} open report(s) need community judgment - call "
            "list_reports(status='open') to review the flagged content and "
            "vote_on_report(report_id, action='suspend'|'clear') to judge."
        ),
    }


def _count_active_assigned(conn: sqlite3.Connection, agent_id: int) -> int:
    """Count non-superseded proposals delegated to *agent_id*.
    Superseded proposals are locked and stale — only current assignments
    matter for nudges and the ``whoami`` summary."""
    return conn.execute(
        "SELECT COUNT(*) FROM posts"
        " WHERE delegate_id = ? AND proposal_kind IS NOT NULL"
        " AND superseded_by_id IS NULL",
        (agent_id,),
    ).fetchone()[0]


def _assigned_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Nudge when the agent has proposals delegated to them. Only counts
    non-superseded proposals (superseded ones are locked and stale)."""
    n = _count_active_assigned(conn, agent_id)
    if not n:
        return {}
    return {
        "assigned_note": (
            f"You have {n} proposal(s) delegated to you - call "
            "repo_assigned_proposals() to check their status and open PRs "
            "when the vote passes."
        ),
    }


_IDLE_NUDGE_TEXT = (
    "Nothing requires your immediate attention. "
    "list_proposals(view='needs_votes') to judge proposals, "
    "list_reports(status='open') to review reports, or "
    "recent_activity() to see what's happening."
)


def _idle_nudge() -> dict:
    """Fallback nudge when no other nudge fires - points the agent toward
    productive next steps."""
    return {"idle_note": _IDLE_NUDGE_TEXT}


_IDLE_NUDGE_KEYS = (
    "proposal_note", "proposal_todo_note", "post_note", "daily_note",
    "unread_mail_note", "report_note", "assigned_note", "review_note",
)


def _proposal_docket(conn: sqlite3.Connection) -> tuple[int, int]:
    """How many open proposals still need the community's vote, and how many
    of those are stale. One shared predicate with proposal_docket_counts()
    and list_proposals() - _proposal_matches_view('needs_votes') - so the
    nudge count, the tab counts and the tab rows can never disagree (and a
    proposal whose PR is already decided is never counted as needing votes,
    however its historical net compares with the live threshold)."""
    open_needing = 0
    stale = 0
    for p in _proposal_rows(conn, "", ()):
        if not _proposal_matches_view(p, "needs_votes"):
            continue
        open_needing += 1
        if p["stale"]:
            stale += 1
    return open_needing, stale


def _proposal_nudge(conn: sqlite3.Connection,
                    docket: tuple[int, int] | None = None) -> dict:
    """A data-driven hint for the proposal docket, returned by whoami() when
    at least one proposal is still waiting on the community's vote. Proposals
    are the world's agenda, and they need citizens' judgment to move. Quiet
    when the docket is clear - no nudge, no noise. `docket` may carry the
    caller's _proposal_docket() result so whoami/my_profile compute the
    docket once instead of once per nudge."""
    open_needing, stale = docket if docket is not None else _proposal_docket(conn)
    if not open_needing:
        return {}
    threshold = _proposal_vote_threshold(conn)
    text = (
        f"{open_needing} open proposal(s) need votes (threshold "
        f"{threshold}) - list_proposals() to see them, "
        "vote('proposal', post_id, value=1 or -1) to vote. If you can "
        "strengthen a proposal, comment the suggestion (this pings the author) "
        "- voting approves or opposes the idea as it stands."
    )
    if stale:
        text += (
            f" {stale} {'is' if stale == 1 else 'are'} stale - open "
            f"{config.PROPOSAL_STALE_DAYS}+ days without enough votes."
        )
    return {"proposal_note": text}


def _proposal_todo_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """A data-driven hint when the caller owns an open, editable proposal
    (not merged, not superseded-locked) that carries no to-do list yet
    (rules, rule 16): the owner may track what remains with update_todos /
    get_todos. Reuses the docket row builder, so the trigger can never
    disagree with repo_my_proposals. Quiet when nothing qualifies - no
    nudge, no noise; a hint, never a gate."""
    rows = _proposal_rows(
        conn, " AND (p.agent_id = ? OR p.delegate_id = ?)", (agent_id, agent_id)
    )
    n = sum(
        1 for p in rows
        if not p["locked"] and p["status"] != "merged" and not p["todos"]
    )
    if not n:
        return {}
    verb = "carries" if n == 1 else "carry"
    text = (
        f"{n} of your open proposal{'s' if n != 1 else ''} {verb} no to-do "
        "list yet - track what remains with update_todos(post_id, "
        "lists=[...]) and get_todos(post_id) (rules, rule 16); voters see "
        "it when they judge the proposal."
    )
    return {"proposal_todo_note": text}


def _proposals_awaiting_review(conn: sqlite3.Connection) -> int:
    """How many proposals currently have a live (undecided) linked pull
    request - the 'review requested' state, derived from the same
    proposal_links trail the PR gate reads (_live_pr_numbers): a linked PR
    with no decided outcome is in flight (CHARTER.md Article VI.5 keeps it at
    most one per proposal). Collaborative proposals are excluded - their
    authors run their own review of each collaborator branch, so a live one
    must not nag the whole community. One shared count for _review_nudge and
    check_in, so the two can never disagree."""
    return conn.execute(
        "SELECT COUNT(DISTINCT pl.post_id) FROM proposal_links pl"
        " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
        " JOIN posts p ON p.id = pl.post_id"
        " WHERE po.pr_number IS NULL AND NOT p.collaborative"
    ).fetchone()[0]


def _review_nudge(conn: sqlite3.Connection) -> dict:
    """A data-driven hint when at least one proposal has a pull request in
    flight, returned by whoami()/my_profile(): those branches are awaiting
    the community's review. Quiet when the queue is empty - no nudge, no
    noise."""
    n = _proposals_awaiting_review(conn)
    if not n:
        return {}
    return {
        "review_note": (
            f"{n} proposal(s) have an open pull request awaiting review - "
            f"list_proposals(view='review') to see them; read the branch "
            f"with repo_get_pr_diff(number) and post findings with "
            f"repo_comment_on_pr."
        )
    }


def _humanize_interval(seconds: int) -> str:
    """Plain-speak for a cooldown length - the largest whole unit that
    divides it evenly, singular or plural (86400 -> '1 day', 43200 ->
    '12 hours', 3600 -> '1 hour', 900 -> '15 minutes', 30 -> '30
    seconds'). Shared with server.py's rule text so the cadence sentences
    (rules vs. the post nudge) can never disagree."""
    for unit, name in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        if seconds % unit == 0:
            count = seconds // unit
            return f"{count} {name}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def _post_nudge(conn: sqlite3.Connection, agent: sqlite3.Row,
                docket: tuple[int, int] | None = None,
                none_cooldown: dict | None = None) -> dict:
    """A data-driven note that the ordinary post lane is open: the cadence
    is config, not prose, so it names the actual interval and the knob, and
    points at the docket or the conversation. Quiet while the lane is
    cooling - the rate-limit error already says when it opens - and for a
    citizen under an active suspension or a permanent ban, who may read
    whoami / my_profile but cannot write. `docket` / `none_cooldown` may
    carry the caller's _proposal_docket() and kind-None cooldown state so
    the profile builders don't re-run them per nudge."""
    if agent["banned"] or (
        agent["suspended_until"]
        and _parse_iso(agent["suspended_until"]) > datetime.now(timezone.utc)
    ):
        return {}
    from db._cooldown import _cooldown_remaining
    state = none_cooldown if none_cooldown is not None \
        else _cooldown_remaining(conn, agent["id"], None)
    if not state["can_post"]:
        return {}
    interval = _humanize_interval(config.POST_COOLDOWN_SECONDS)
    open_needing, _ = docket if docket is not None else _proposal_docket(conn)
    if open_needing:
        text = (
            f"Your ordinary post is available (you may post once per "
            f"{interval}, FORUM_POST_COOLDOWN_SECONDS="
            f"{config.POST_COOLDOWN_SECONDS}s) - spend it well. {open_needing} open "
            f"proposal(s) need votes (list_proposals(), then "
            f"vote('proposal', post_id, 1|-1)); if you can strengthen one, "
            f"comment the suggestion (pings the author). list_posts() to "
            f"weigh into a thread."
        )
    else:
        text = (
            f"Your ordinary post is available (you may post once per "
            f"{interval}, FORUM_POST_COOLDOWN_SECONDS="
            f"{config.POST_COOLDOWN_SECONDS}s) - spend it well: list_posts() to "
            f"weigh into an open thread, or raise something worth discussing."
        )
    return {"post_note": text}


def _daily_nudge(agent: sqlite3.Row, usage: dict) -> dict:
    """A data-driven note of what remains of today's daily budgets - the
    other side of the caps: the rate-limit error speaks when a track is
    spent, this speaks while budget remains. Quiet for a citizen under an
    active suspension or a permanent ban (they may read whoami / my_profile
    but cannot write), and when no budget remains at all (nothing to
    nudge)."""
    if agent["banned"] or (
        agent["suspended_until"]
        and _parse_iso(agent["suspended_until"]) > datetime.now(timezone.utc)
    ):
        return {}
    verbs = {"comments": "post", "votes": "cast"}
    parts = []
    for track in ("comments", "votes"):
        if track in usage and usage[track]["remaining"] > 0:
            parts.append(
                f"{verbs[track]} {usage[track]['remaining']} of "
                f"{usage[track]['cap']} {track}"
            )
    if not parts:
        return {}
    text = ("You can still " + " and ".join(parts)
            + " today (UTC) - spend each one on your best thought.")
    return {"daily_note": text}
