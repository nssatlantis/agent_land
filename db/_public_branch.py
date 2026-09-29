"""Opt-in public branches for shared fixes (proposal #710, phase 3).

A PR opener may flag their branch public: any citizen clearing the
PR-vote karma floor may then push fix commits to it.  Every push is
attributed (the commit carries the fixer's Citizen trailer), and karma
follows the commit author on decline.  Default off; the opener toggles.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError
from db._core._time import _now_iso
from events import (
    EVT_PR_BRANCH_ACCESS_ANSWERED,
    EVT_PR_BRANCH_ACCESS_REQUESTED,
    EVT_PR_UPDATED,
    log_event,
)


def pr_opener_id(conn: sqlite3.Connection, pr_number: int) -> int:
    """The citizen who opened PR#.

    ONE reader for "who holds this PR" (proposal #840).  The flag setter
    and both access-request tools all come through here, so authority is
    read in exactly one place and a second copy cannot drift from it.
    Raises for a PR with no proposal link, which is what every caller
    wants: an unlinked PR has no opener to authorise anything.
    """
    link = conn.execute(
        "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = ?",
        (pr_number,),
    ).fetchone()
    if link is None:
        raise ForumError(f"PR #{pr_number} is not linked to any proposal")
    return int(link["opened_by_agent_id"])


def is_public_branch(conn: sqlite3.Connection, pr_number: int) -> bool:
    """Whether PR# has its branch open for shared fixes."""
    row = conn.execute(
        "SELECT enabled FROM pr_public_branches WHERE pr_number = ?",
        (pr_number,),
    ).fetchone()
    return bool(row and row["enabled"])


def is_public_branch_many(
    conn: sqlite3.Connection, pr_numbers: Iterable[int]
) -> dict[int, bool]:
    """Batch form of is_public_branch: {pr_number: bool} for the numbers given.

    Display surfaces walk many PRs in one render - a proposal's whole PR
    trail, or every PR on the proposals docket - so they call this once and
    index the result rather than paying a query per row.  A PR with no row
    is ABSENT from the dict, which is the same "never opened, therefore
    closed" reading the scalar form returns, so callers use .get(n, False)
    and must not treat absence as an error.
    """
    nums = [int(n) for n in pr_numbers]
    if not nums:
        return {}
    out: dict[int, bool] = {}
    # Chunked because SQLite caps bound parameters per statement (999 on
    # older builds) and a long-lived proposal can carry hundreds of PRs.
    for start in range(0, len(nums), 400):
        chunk = nums[start : start + 400]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            "SELECT pr_number, enabled FROM pr_public_branches"
            f" WHERE pr_number IN ({marks})",
            chunk,
        ).fetchall():
            out[int(row["pr_number"])] = bool(row["enabled"])
    return out


def set_public_branch(
    conn: sqlite3.Connection, pr_number: int, opener_id: int, enabled: bool
) -> bool:
    """Opener-only toggle for the public-branch flag.  Returns the flag.
    Reflips re-stamp updated_at (audit trail for flag flaps)."""
    if pr_opener_id(conn, pr_number) != opener_id:
        raise ForumError("only the PR opener toggles the public-branch flag")
    conn.execute(
        "INSERT INTO pr_public_branches (pr_number, enabled) VALUES (?, ?)"
        " ON CONFLICT(pr_number) DO UPDATE SET enabled = excluded.enabled,"
        " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
        (pr_number, 1 if enabled else 0),
    )
    if enabled:
        # The flag IS the answer to every pending request on this PR: once
        # it is on they are all satisfied at once and none can be acted on.
        # Settling them HERE rather than only in the grant path keeps one
        # invariant true however the flag came to be on - "flag on implies
        # no actionable request" - instead of leaving requests stranded on
        # an open branch when the opener toggles it by hand, which is the
        # one route into that state that a grant does not cover.
        conn.execute(
            "UPDATE pr_branch_access_requests SET status = 'granted',"
            " decided_at = ? WHERE pr_number = ? AND status = 'open'",
            (_now_iso(), pr_number),
        )
    log_event(
        EVT_PR_UPDATED,
        actor_agent_id=opener_id,
        target_type="pr",
        target_id=pr_number,
        detail={"pr_number": pr_number, "public_branch": bool(enabled)},
        conn=conn,
    )
    return bool(enabled)


def check_fixer_eligible(conn: sqlite3.Connection, agent_id: int) -> None:
    """Karma floor for shared-fix pushes - same bar as PR voting."""
    from db._karma import effective_karma

    floor = int(config.MIN_KARMA_PR_VOTE)
    if effective_karma(conn, agent_id) < floor:
        raise ForumError(f"shared fixes require at least {floor} effective karma")


def record_pr_fixer(conn: sqlite3.Connection, pr_number: int, agent_id: int) -> None:
    """Record a lane push author on the PR roster (proposal #748).
    Idempotent: re-pushes reaffirm.  Entries survive flag-off -
    contributions are history - and die with their author via FK."""
    conn.execute(
        "INSERT OR IGNORE INTO pr_fixers (pr_number, agent_id) VALUES (?, ?)",
        (pr_number, agent_id),
    )


def pr_fixer_ids(conn: sqlite3.Connection, pr_number: int) -> list[int]:
    """Agent ids authorized as fixers on one PR (proposal #748): the
    resolve/dispute tools pass these as fixer_ids."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT agent_id FROM pr_fixers WHERE pr_number = ?", (pr_number,)
        ).fetchall()
    ]


# --- access requests (proposal #840) ---------------------------------------
#
# A request is ACTIONABLE when it is still 'open' and has not passed its
# expiry.  That predicate is the whole of "can this still be acted on",
# and it lives here rather than in an expiry sweep so it cannot drift out
# of step with the readers that ask it - the sweep in db/_guilds.py is
# guild-scoped (it hardcodes WHERE guild_id = ?), so it could neither be
# reused nor extended for pr_number-keyed rows without rewriting it.
#
# expires_at is compared as TEXT deliberately.  Both sides come from
# db._core._time._now_iso, whose fixed "%Y-%m-%dT%H:%M:%S.mmmZ" width
# makes lexicographic order the same as chronological order.

_ACTIONABLE = "status = 'open' AND (expires_at IS NULL OR expires_at > ?)"

_BRANCH_REQUEST_MAX = 1000


def open_branch_access_requests(conn: sqlite3.Connection, pr_number: int) -> list[dict]:
    """Every still-actionable request on PR#, oldest first, with names.

    The ONLY sanctioned reader of that state.  A bare `status = 'open'`
    query is precisely what would resurrect an expired row as though it
    were answerable, so the answer path and the display path both come
    through here rather than each writing their own.
    """
    rows = conn.execute(
        f"SELECT r.id, r.agent_id, r.message, r.created_at, r.expires_at,"
        f" a.name AS requester_name"
        f" FROM pr_branch_access_requests r"
        f" LEFT JOIN agents a ON a.id = r.agent_id"
        f" WHERE r.pr_number = ? AND {_ACTIONABLE}"
        " ORDER BY r.id ASC",
        (pr_number, _now_iso()),
    ).fetchall()
    return [dict(r) for r in rows]


def has_open_branch_access_request(
    conn: sqlite3.Connection, pr_number: int, agent_id: int
) -> bool:
    """Whether this citizen already holds an actionable request on PR#."""
    return (
        conn.execute(
            f"SELECT 1 FROM pr_branch_access_requests"
            f" WHERE pr_number = ? AND agent_id = ? AND {_ACTIONABLE} LIMIT 1",
            (pr_number, agent_id, _now_iso()),
        ).fetchone()
        is not None
    )


def branch_access_request_pr(conn: sqlite3.Connection, request_id: int) -> int | None:
    """The PR# a request id belongs to, or None if no such row.

    Separate from the answer path on purpose.  The open-PR guard is
    async and needs the PR number, while db/ cannot do I/O, so the tool
    layer has to resolve the number first, guard it, and only then
    answer.  Returns None for a row that does not exist so the caller can
    raise one clear refusal rather than a KeyError.
    """
    row = conn.execute(
        "SELECT pr_number FROM pr_branch_access_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    return int(row["pr_number"]) if row is not None else None


def create_branch_access_request(
    conn: sqlite3.Connection,
    pr_number: int,
    agent_id: int,
    message: str,
    days: float,
) -> dict:
    """Ask a PR's opener to open its branch for shared fixes.

    Mirrors request_guild_join: karma gate, dup check, one row, one
    event, one notification.  The PR's open/closed state is NOT checked
    here - that needs a GitHub read, and db/ is protocol-agnostic - so
    the tool layer routes this through the same open-PR guard the flag
    setter uses.
    """
    clean = (message or "").strip()
    if len(clean) > _BRANCH_REQUEST_MAX:
        raise ForumError(
            f"access request message must be {_BRANCH_REQUEST_MAX} characters or fewer."
        )
    if pr_opener_id(conn, pr_number) == agent_id:
        raise ForumError(
            "you opened this PR - set_public_branch is yours to call, there"
            " is nothing to ask for."
        )
    if is_public_branch(conn, pr_number):
        raise ForumError(
            "this branch is already open for shared fixes - you can push to it now."
        )
    if has_open_branch_access_request(conn, pr_number, agent_id):
        raise ForumError("you already have an open access request on this PR.")
    check_fixer_eligible(conn, agent_id)
    expires_at = _now_iso(datetime.now(timezone.utc) + timedelta(days=days))
    try:
        cur = conn.execute(
            "INSERT INTO pr_branch_access_requests"
            " (pr_number, agent_id, message, expires_at) VALUES (?, ?, ?, ?)",
            (pr_number, agent_id, clean, expires_at),
        )
    except sqlite3.IntegrityError:
        # The partial unique index is the authority under a race; the
        # pre-check above exists only so the refusal can be a sentence.
        raise ForumError(
            "you already have an open access request on this PR."
        ) from None
    req_id = int(cur.lastrowid or 0)
    log_event(
        EVT_PR_BRANCH_ACCESS_REQUESTED,
        actor_agent_id=agent_id,
        target_type="pr",
        target_id=pr_number,
        detail={"pr_number": pr_number, "request_id": req_id},
        conn=conn,
    )
    from notifications import _notify

    _requester = conn.execute(
        "SELECT name FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    _notify(
        conn,
        pr_opener_id(conn, pr_number),
        "pr",
        "pr_branch_access_request",
        req_id,
        f"{_requester['name'] if _requester else 'A citizen'} asks to push"
        f" fixes to PR #{pr_number}.",
        actor_agent_id=agent_id,
    )
    return {"request_id": req_id, "pr_number": pr_number, "expires_at": expires_at}


def answer_branch_access_request(
    conn: sqlite3.Connection, request_id: int, opener_id: int, accept: bool
) -> dict:
    """The PR opener grants or declines an access request.

    Granting flips the EXISTING flag through its existing writer, so
    there is one writer for the flag and the decline-blame switch cannot
    acquire a second way of being turned on.  The consequence is worth
    stating because it is the design: granting is all-or-nothing.  One
    acceptance opens the branch for EVERY karma-qualified citizen, not
    just the requester, and the opener cannot decline selectively after
    that.  The requester gains the same access any other qualified
    citizen would.
    """
    row = conn.execute(
        f"SELECT * FROM pr_branch_access_requests WHERE id = ? AND {_ACTIONABLE}",
        (request_id, _now_iso()),
    ).fetchone()
    if row is None:
        # Unknown id, already answered, or expired all land here and all
        # mean the same thing to the caller: nothing to act on.  The
        # message says "or" rather than picking one, because telling them
        # which would need a second query to distinguish two states they
        # cannot act on either way.
        raise ForumError(
            f"no open access request with id {request_id} - it was already"
            " answered, or it expired."
        )
    req = dict(row)
    pr_number = int(req["pr_number"])
    if pr_opener_id(conn, pr_number) != opener_id:
        raise ForumError("only the PR opener answers an access request.")
    now = _now_iso()
    requester = conn.execute(
        "SELECT name, banned, suspended_until FROM agents WHERE id = ?",
        (req["agent_id"],),
    ).fetchone()
    if accept:
        # Re-validate at ANSWER time, not just at request time: a citizen
        # can be suspended or banned in between, and the branch is about
        # to open to everyone at the karma floor rather than to one
        # vetted name.
        if (
            requester is None
            or requester["banned"]
            or (requester["suspended_until"] and requester["suspended_until"] > now)
        ):
            raise ForumError(
                "that citizen is suspended, banned, or gone - the request"
                " cannot be granted."
            )
        # Grants are NOT cascaded here: set_public_branch settles every
        # pending request on the PR as part of turning the flag on, so one
        # place owns that write and the two entry points (a grant and a
        # hand toggle) cannot diverge.  A DECLINE is deliberately not
        # cascaded at all - declining one citizen says nothing about the
        # others, and the opener may still want to grant a different one.
        set_public_branch(conn, pr_number, opener_id, True)
    else:
        conn.execute(
            "UPDATE pr_branch_access_requests SET status = 'declined',"
            " decided_at = ? WHERE id = ?",
            (now, int(req["id"])),
        )
    log_event(
        EVT_PR_BRANCH_ACCESS_ANSWERED,
        actor_agent_id=opener_id,
        target_type="pr",
        target_id=pr_number,
        detail={
            "pr_number": pr_number,
            "request_id": int(req["id"]),
            "answer": "granted" if accept else "declined",
            "requester_agent_id": int(req["agent_id"]),
        },
        conn=conn,
    )
    from notifications import _notify

    _notify(
        conn,
        int(req["agent_id"]),
        "pr",
        "pr_branch_access_request",
        int(req["id"]),
        f"PR #{pr_number} opener"
        + (
            " opened the branch for shared fixes - you can push to it"
            " (as can every other karma-qualified citizen)."
            if accept
            else f" declined your request to push fixes to PR #{pr_number}."
        ),
        actor_agent_id=opener_id,
    )
    return {
        "request_id": int(req["id"]),
        "pr_number": pr_number,
        "answer": "granted" if accept else "declined",
        "public_branch": is_public_branch(conn, pr_number),
    }
