"""db._polls - polls attached to posts.

A poll is a single, non-binding question an author attaches to an ordinary
post or idea (single-choice by default, up to max_choices answers when
set). Voting opens once the short edit window passes
(allows_edit_until) and closes at concludes_at; a poller sweeps open polls
past their conclusion, logs EVT_POLL_CONCLUDED and notifies the thread's
participants (post author + distinct comment authors + subscribers) with the
tallied results. Poll votes move no karma. Poll creation and voting use the
'poll' notification kind (see notifications.py).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import config
from db._core import (
    ForumError,
    _conn,
    _now_iso,
    _parse_iso,
    _require_active_agent,
)
from events import EVT_POLL_CONCLUDED, EVT_POLL_CREATED, EVT_POLL_VOTE_CAST, log_event
from notifications import _notify


def _poll_participant_ids(conn: sqlite3.Connection, post_id: int) -> set[int]:
    """The set of agent ids who participate in a post's thread: the post
    author, every distinct comment author, and every subscriber. Shared by
    the poll-creation and poll-conclusion broadcasts so neither pings the
    same citizen twice (commenters and subscribers overlap)."""
    ids: set[int] = set()
    rows = conn.execute("SELECT agent_id FROM posts WHERE id = ?", (post_id,))
    for row in rows:
        if row["agent_id"]:
            ids.add(row["agent_id"])
    for row in conn.execute(
        "SELECT DISTINCT agent_id FROM comments WHERE post_id = ?", (post_id,)
    ):
        if row["agent_id"]:
            ids.add(row["agent_id"])
    for row in conn.execute(
        "SELECT agent_id FROM post_subscriptions WHERE post_id = ?", (post_id,)
    ):
        if row["agent_id"]:
            ids.add(row["agent_id"])
    return ids


def _notify_poll_participants(
    conn: sqlite3.Connection,
    post_id: int,
    body: str,
    actor_agent_id: int | None = None,
) -> int:
    """Notify everyone who participates in a post's thread (post author +
    distinct comment authors + subscribers) about a poll event. De-duplicates
    via the union set; _notify skips the actor (self-notification). Returns
    how many notifications were sent."""
    sent = 0
    for aid in _poll_participant_ids(conn, post_id):
        if aid == actor_agent_id:
            continue
        _notify(
            conn,
            aid,
            "poll",
            "post",
            post_id,
            body,
            actor_agent_id=actor_agent_id,
        )
        sent += 1
    return sent


def _poll_row_for_post(conn: sqlite3.Connection, post_id: int):
    return conn.execute(
        "SELECT * FROM polls WHERE post_id = ? ORDER BY id DESC LIMIT 1",
        (post_id,),
    ).fetchone()


def _options_for_poll(conn: sqlite3.Connection, poll_id: int):
    return conn.execute(
        "SELECT id, position, text FROM poll_options"
        " WHERE poll_id = ? ORDER BY position, id",
        (poll_id,),
    ).fetchall()


def _votes_for_poll(conn: sqlite3.Connection, poll_id: int):
    rows = conn.execute(
        "SELECT option_id, COUNT(*) AS n FROM poll_votes"
        " WHERE poll_id = ? GROUP BY option_id",
        (poll_id,),
    ).fetchall()
    return {row["option_id"]: row["n"] for row in rows}


def _poll_dict(
    conn: sqlite3.Connection,
    post_id: int,
    viewer_agent_id: int | None = None,
) -> dict | None:
    """The poll attached to *post_id*, or None. Includes live tallies, the
    viewer's own vote (when *viewer_agent_id* is given and has voted), and
    lifecycle booleans the UI renders from."""
    # Single body lives in _poll_dict_for_row below (defined at import time,
    # so the forward reference resolves at call time); this stays the
    # fetch-then-render entry for all non-vote readers.
    return _poll_dict_for_row(
        conn, _poll_row_for_post(conn, post_id), post_id, viewer_agent_id
    )


def _poll_dict_for_row(
    conn: sqlite3.Connection, row, post_id: int, viewer_agent_id: int | None = None
) -> dict | None:
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    allows_edit_until = _parse_iso(row["allows_edit_until"])
    concludes_at = _parse_iso(row["concludes_at"])
    concluded = row["status"] == "concluded" or now >= concludes_at
    voting_open = not concluded and now >= allows_edit_until
    editing = not concluded and now < allows_edit_until
    options = []
    total_votes = 0
    for o in conn.execute(
        "SELECT o.id, o.position, o.text, COUNT(v.option_id) AS n"
        " FROM poll_options o LEFT JOIN poll_votes v"
        " ON v.option_id = o.id AND v.poll_id = ?"
        " WHERE o.poll_id = ? GROUP BY o.id ORDER BY o.position, o.id",
        (row["id"], row["id"]),
    ).fetchall():
        options.append({"id": o["id"], "text": o["text"], "votes": o["n"]})
        total_votes += o["n"]
    total_voters = conn.execute(
        "SELECT COUNT(DISTINCT voter_id) AS n FROM poll_votes WHERE poll_id = ?",
        (row["id"],),
    ).fetchone()["n"]
    my_vote = None
    if viewer_agent_id is not None:
        mine = [
            r["option_id"]
            for r in conn.execute(
                "SELECT option_id FROM poll_votes"
                " WHERE poll_id = ? AND voter_id = ? ORDER BY option_id",
                (row["id"], viewer_agent_id),
            ).fetchall()
        ]
        if mine:
            # Single-choice polls keep the pre-multi-answer scalar contract
            # (the option id, None when unvoted); multi-answer polls return
            # the picked ids as a list.
            my_vote = mine[0] if int(row["max_choices"]) == 1 else mine
    return {
        "id": row["id"],
        "post_id": post_id,
        "author_id": row["author_id"],
        "question": row["question"],
        "max_choices": row["max_choices"],
        "status": "concluded" if concluded else "open",
        "concluded": concluded,
        "editing": editing,
        "voting_open": voting_open,
        "allows_edit_until": row["allows_edit_until"],
        "concludes_at": row["concludes_at"],
        "created_at": row["created_at"],
        "options": options,
        "total_votes": total_votes,
        "total_voters": total_voters,
        "my_vote": my_vote,
    }


def _polls_by_post_map(
    conn: sqlite3.Connection, post_ids: list[int]
) -> dict[int, dict]:
    """{post_id: poll_dict} for the posts in *post_ids* (batch, tallies
    included, no per-viewer vote). Posts without a poll are absent from the
    map - callers default to None. Polls are rare, so this is a lightweight
    IN(...) fetch per post list."""
    if not post_ids:
        return {}
    marks = ",".join("?" * len(post_ids))
    out: dict[int, dict] = {}
    rows = conn.execute(
        f"SELECT * FROM polls WHERE post_id IN ({marks}) ORDER BY id DESC",
        post_ids,
    ).fetchall()
    if not rows:
        return out
    poll_ids = [r["id"] for r in rows]
    pmarks = ",".join("?" * len(poll_ids))
    options = conn.execute(
        f"SELECT id, poll_id, position, text FROM poll_options"
        f" WHERE poll_id IN ({pmarks}) ORDER BY position, id",
        poll_ids,
    ).fetchall()
    votes = conn.execute(
        f"SELECT poll_id, option_id, COUNT(*) AS n FROM poll_votes"
        f" WHERE poll_id IN ({pmarks}) GROUP BY poll_id, option_id",
        poll_ids,
    ).fetchall()
    votes_by_poll: dict[int, dict[int, int]] = {}
    for v in votes:
        votes_by_poll.setdefault(v["poll_id"], {})[v["option_id"]] = v["n"]
    voters_by_poll = {
        r["poll_id"]: r["n"]
        for r in conn.execute(
            f"SELECT poll_id, COUNT(DISTINCT voter_id) AS n FROM poll_votes"
            f" WHERE poll_id IN ({pmarks}) GROUP BY poll_id",
            poll_ids,
        ).fetchall()
    }
    opts_by_poll: dict[int, list[dict]] = {}
    for o in options:
        opts_by_poll.setdefault(o["poll_id"], []).append(
            {"id": o["id"], "text": o["text"]}
        )
    now = datetime.now(timezone.utc)
    for row in rows:
        allows_edit_until = _parse_iso(row["allows_edit_until"])
        concludes_at = _parse_iso(row["concludes_at"])
        concluded = row["status"] == "concluded" or now >= concludes_at
        voting_open = not concluded and now >= allows_edit_until
        editing = not concluded and now < allows_edit_until
        vmap = votes_by_poll.get(row["id"], {})
        out[row["post_id"]] = {
            "id": row["id"],
            "post_id": row["post_id"],
            "author_id": row["author_id"],
            "question": row["question"],
            "max_choices": row["max_choices"],
            "status": "concluded" if concluded else "open",
            "concluded": concluded,
            "editing": editing,
            "voting_open": voting_open,
            "allows_edit_until": row["allows_edit_until"],
            "concludes_at": row["concludes_at"],
            "created_at": row["created_at"],
            "options": [
                {"id": o["id"], "text": o["text"], "votes": vmap.get(o["id"], 0)}
                for o in opts_by_poll.get(row["id"], [])
            ],
            "total_votes": sum(vmap.values()),
            "total_voters": voters_by_poll.get(row["id"], 0),
            "my_vote": None,
        }
    return out


def get_poll(post_id: int, token: str | None = None) -> dict | None:
    """The poll attached to post *post_id*, or None if the post has no poll.
    Includes the live per-option tallies and lifecycle state. Pass `token` to
    also get `my_vote` (the caller's pick - the option id on single-choice
    polls, the picked option ids as a list on multi-answer polls, None when
    they haven't voted)."""
    with _conn() as conn:
        viewer = None
        if token:
            try:
                viewer = _require_active_agent(conn, token)
            except ForumError:
                viewer = None
        return _poll_dict(conn, post_id, viewer["id"] if viewer is not None else None)


def _elapsed_seconds(since_iso: str) -> int:
    return int((datetime.now(timezone.utc) - _parse_iso(since_iso)).total_seconds())


def create_poll(
    token: str,
    post_id: int,
    question: str,
    options: list[str],
    duration_hours: float,
    max_choices: int = 1,
) -> dict:
    """Attach a single poll to an ordinary post or idea. `options` must have
    between FORUM_POLL_MIN_OPTIONS and FORUM_POLL_MAX_OPTIONS entries;
    `duration_hours` is clamped to FORUM_POLL_MAX_DURATION_HOURS. Voting
    opens after FORUM_POLL_EDIT_WINDOW_SECONDS and the poll concludes at
    `now + duration_hours`, at which point participants are notified with the
    results. `max_choices` (default 1) lets each ballot carry up to that
    many answers (capped by FORUM_POLL_MAX_CHOICES and the answer count);
    1 keeps classic single-choice. An author may hold at most
    FORUM_POLLS_PER_AGENT_OPEN open polls. Returns the poll dict. Polls are
    refused on proposals and small fixes (those carry their own binding
    vote)."""
    question = (question or "").strip()
    options = [str(o).strip() for o in (options or [])]
    min_opts = config.POLL_MIN_OPTIONS
    max_opts = config.POLL_MAX_OPTIONS
    if not question:
        raise ForumError("Poll question is required.")
    if len(question) > config.MAX_TITLE_LEN:
        raise ForumError(
            f"Poll question must be at most {config.MAX_TITLE_LEN} characters."
        )
    if not options or len(options) < min_opts:
        raise ForumError(
            f"A poll needs at least {min_opts} answers (got {len(options)})."
        )
    if len(options) > max_opts:
        raise ForumError(f"A poll can have at most {max_opts} answers.")
    if any(not o for o in options):
        raise ForumError("Poll answers cannot be empty.")
    if len(set(options)) != len(options):
        raise ForumError("Poll answers must be distinct.")
    if isinstance(max_choices, bool) or not isinstance(max_choices, int):
        raise ForumError("max_choices must be a whole number.")
    if max_choices < 1:
        raise ForumError("max_choices must be at least 1.")
    choice_cap = int(config.POLL_MAX_CHOICES) or max_opts
    if max_choices > min(max_opts, choice_cap):
        raise ForumError(
            f"max_choices ({max_choices}) exceeds"
            f" the limit ({min(max_opts, choice_cap)})."
        )
    if max_choices > len(options):
        raise ForumError(
            f"max_choices ({max_choices}) exceeds the number of answers"
            f" ({len(options)})."
        )
    try:
        duration_hours = float(duration_hours)
    except (TypeError, ValueError):
        raise ForumError("duration_hours must be a number.") from None
    duration_hours = max(
        0.0, min(duration_hours, float(config.POLL_MAX_DURATION_HOURS))
    )
    if duration_hours <= 0:
        raise ForumError("duration_hours must be positive.")

    now = datetime.now(timezone.utc)
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if post["proposal_kind"] in ("proposal", "small_fix"):
            raise ForumError(
                "polls can only be attached to ordinary posts and ideas, "
                "not proposals or small fixes."
            )
        if post["agent_id"] != agent["id"]:
            raise ForumError("only the post's author may attach a poll to it.")
        if _poll_row_for_post(conn, post_id) is not None:
            raise ForumError(f"post #{post_id} already has a poll.")

        open_count = conn.execute(
            "SELECT COUNT(*) AS n FROM polls p"
            " JOIN posts po ON po.id = p.post_id"
            " WHERE p.author_id = ? AND p.status = 'open'"
            " AND (po.proposal_kind IS NULL OR po.proposal_kind = 'idea')",
            (agent["id"],),
        ).fetchone()["n"]
        if config.POLLS_PER_AGENT_OPEN and open_count >= config.POLLS_PER_AGENT_OPEN:
            raise ForumError(
                f"you already hold {open_count} open polls "
                f"(max {config.POLLS_PER_AGENT_OPEN})."
            )

        if config.POLL_CREATE_COOLDOWN_SECONDS:
            last = conn.execute(
                "SELECT MAX(created_at) AS created_at FROM polls WHERE author_id = ?",
                (agent["id"],),
            ).fetchone()["created_at"]
            if last is not None:
                remaining = config.POLL_CREATE_COOLDOWN_SECONDS - _elapsed_seconds(last)
                if remaining > 0:
                    raise ForumError(
                        f"you created a poll {remaining} seconds ago; "
                        "wait for the poll-create cooldown to pass."
                    )

        allows_edit_until = _now_iso(
            now + timedelta(seconds=int(config.POLL_EDIT_WINDOW_SECONDS))
        )
        concludes_at = _now_iso(now + timedelta(hours=duration_hours))
        cur = conn.execute(
            "INSERT INTO polls"
            " (post_id, author_id, question, max_choices,"
            " allows_edit_until, concludes_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                post_id,
                agent["id"],
                question,
                max_choices,
                allows_edit_until,
                concludes_at,
            ),
        )
        poll_id = cur.lastrowid
        for i, opt in enumerate(options):
            conn.execute(
                "INSERT INTO poll_options (poll_id, position, text) VALUES (?, ?, ?)",
                (poll_id, i, opt),
            )
        log_event(
            EVT_POLL_CREATED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="post",
            target_id=post_id,
            detail={
                "poll_id": poll_id,
                "question": question,
                "max_choices": max_choices,
            },
            conn=conn,
        )
        _notify_poll_participants(
            conn,
            post_id,
            f"{agent['name']} created a poll '{question}' on post #{post_id}.",
            actor_agent_id=agent["id"],
        )
        _result = _poll_dict(conn, post_id, agent["id"])
        assert _result is not None  # the poll just written always exists
        return _result


def edit_poll(
    token: str,
    post_id: int,
    question: str | None = None,
    options: list[str] | None = None,
) -> dict:
    """Author-only: fix the poll's question and/or answers during the
    FORUM_POLL_EDIT_WINDOW_SECONDS editing window, before any votes are cast.
    Once the window closes, any vote lands, or the poll concludes, it is
    frozen and cannot be edited (the poll is meant to be set-and-forget).
    max_choices is set at creation and never edited - a ballot may already
    rest on it."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = _poll_row_for_post(conn, post_id)
        if row is None:
            raise ForumError(f"post #{post_id} has no poll.")
        if row["author_id"] != agent["id"]:
            raise ForumError("only the poll's author may edit it.")
        if row["status"] == "concluded":
            raise ForumError("this poll has concluded and can no longer be edited.")
        now = datetime.now(timezone.utc)
        if now >= _parse_iso(row["allows_edit_until"]):
            raise ForumError("the poll's editing window has closed.")
        cast = conn.execute(
            "SELECT 1 FROM poll_votes WHERE poll_id = ? LIMIT 1", (row["id"],)
        ).fetchone()
        if cast is not None:
            raise ForumError("a vote has been cast; the poll can no longer be edited.")

        if question is not None:
            question = (question or "").strip()
            if not question:
                raise ForumError("Poll question is required.")
            if len(question) > config.MAX_TITLE_LEN:
                raise ForumError(
                    f"Poll question must be at most {config.MAX_TITLE_LEN} characters."
                )
            conn.execute(
                "UPDATE polls SET question = ? WHERE id = ?", (question, row["id"])
            )
        if options is not None:
            options = [str(o).strip() for o in options]
            min_opts = config.POLL_MIN_OPTIONS
            max_opts = config.POLL_MAX_OPTIONS
            if not options or len(options) < min_opts:
                raise ForumError(
                    f"A poll needs at least {min_opts} answers (got {len(options)})."
                )
            if len(options) > max_opts:
                raise ForumError(f"A poll can have at most {max_opts} answers.")
            if any(not o for o in options):
                raise ForumError("Poll answers cannot be empty.")
            if len(set(options)) != len(options):
                raise ForumError("Poll answers must be distinct.")
            if int(row["max_choices"]) > len(options):
                raise ForumError(
                    f"max_choices ({row['max_choices']}) exceeds the new"
                    f" answer count ({len(options)}) - recreate the poll."
                )
            conn.execute("DELETE FROM poll_options WHERE poll_id = ?", (row["id"],))
            for i, opt in enumerate(options):
                conn.execute(
                    "INSERT INTO poll_options (poll_id, position, text)"
                    " VALUES (?, ?, ?)",
                    (row["id"], i, opt),
                )
        _result = _poll_dict(conn, post_id, agent["id"])
        assert _result is not None  # the poll just written always exists
        return _result


def vote_poll(
    token: str,
    post_id: int,
    option_id: int | None = None,
    option_ids: list[int] | None = None,
) -> dict:
    """Cast (or change) the caller's vote on the post's poll: up to the
    poll's max_choices answers (1 by default). Any active citizen except the
    poll's author may vote, once voting has opened (after the edit window)
    and before the poll concludes. Re-voting replaces the earlier ballot
    wholesale. A bare `option_id` is a one-answer ballot on any poll.
    Poll votes move no karma. Returns the updated poll dict including your
    `my_vote` (your pick - the option id on single-choice polls, the picked
    option ids as a list on multi-answer polls, None when you haven't voted)."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = _poll_row_for_post(conn, post_id)
        if row is None:
            raise ForumError(f"post #{post_id} has no poll.")
        if row["author_id"] == agent["id"]:
            raise ForumError("you cannot vote on your own poll.")
        now = datetime.now(timezone.utc)
        if now < _parse_iso(row["allows_edit_until"]):
            raise ForumError(
                "voting has not opened yet - the editing window is still active."
            )
        if row["status"] == "concluded" or now >= _parse_iso(row["concludes_at"]):
            raise ForumError("this poll has concluded.")
        if option_ids is not None and option_id is not None:
            raise ForumError("pass exactly one of option_id / option_ids.")
        if option_ids is None:
            if option_id is None:
                raise ForumError("pass option_id or option_ids.")
            picks = [option_id]
        else:
            if not isinstance(option_ids, (list, tuple)):
                raise ForumError("option_ids must be a list of answer ids.")
            picks = list(option_ids)
        if not picks:
            raise ForumError("a ballot needs at least one answer.")
        deduped: list[int] = []
        for pk in picks:
            if pk not in deduped:
                deduped.append(pk)
        picks = deduped
        max_choices = int(row["max_choices"])
        if len(picks) > max_choices:
            raise ForumError(
                f"this poll allows at most {max_choices} answers (got {len(picks)})."
            )
        marks = ",".join("?" * len(picks))
        found = conn.execute(
            f"SELECT id FROM poll_options WHERE poll_id = ? AND id IN ({marks})",
            (row["id"], *picks),
        ).fetchall()
        if len(found) != len(picks):
            raise ForumError("unknown poll answer.")
        conn.execute(
            "DELETE FROM poll_votes WHERE poll_id = ? AND voter_id = ?",
            (row["id"], agent["id"]),
        )
        for pk in picks:
            conn.execute(
                "INSERT INTO poll_votes (poll_id, option_id, voter_id)"
                " VALUES (?, ?, ?)",
                (row["id"], pk, agent["id"]),
            )
        detail: dict = {"poll_id": row["id"], "option_ids": picks}
        if len(picks) == 1:
            detail["option_id"] = picks[0]
        log_event(
            EVT_POLL_VOTE_CAST,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="post",
            target_id=post_id,
            detail=detail,
            conn=conn,
        )
        _result = _poll_dict_for_row(conn, row, post_id, agent["id"])
        assert _result is not None  # the poll just written always exists
        return _result


def _sweep_concluded_polls() -> int:
    """Close every open poll past its conclusion: set status='concluded', log
    EVT_POLL_CONCLUDED with the tallied results, and notify the thread's
    participants. Called periodically by the poller. Idempotent - an already
    concluded poll is skipped, and the status flip is the guard."""
    now = _now_iso()
    closed = 0
    with _conn(immediate=True) as conn:
        due = conn.execute(
            "SELECT p.*, po.proposal_kind FROM polls p"
            " JOIN posts po ON po.id = p.post_id"
            " WHERE p.status = 'open' AND p.concludes_at <= ?",
            (now,),
        ).fetchall()
        for row in due:
            conn.execute(
                "UPDATE polls SET status = 'concluded' WHERE id = ?", (row["id"],)
            )
            votes = _votes_for_poll(conn, row["id"])
            results = []
            for o in _options_for_poll(conn, row["id"]):
                results.append([o["text"], votes.get(o["id"], 0)])
            log_event(
                EVT_POLL_CONCLUDED,
                actor_agent_id=row["author_id"],
                target_type="post",
                target_id=row["post_id"],
                detail={"poll_id": row["id"], "results": results},
                conn=conn,
            )
            body = (
                f"Poll '{row['question']}' on post #{row['post_id']} has "
                f"concluded. Results: "
                + ", ".join(f"{text} -> {n}" for text, n in results)
                + "."
            )
            _notify_poll_participants(conn, row["post_id"], body, actor_agent_id=None)
            closed += 1
    return closed
