"""Opt-in public branches for shared fixes (proposal #710, phase 3).

A PR opener may flag their branch public: any citizen clearing the
PR-vote karma floor may then push fix commits to it.  Every push is
attributed (the commit carries the fixer's Citizen trailer), and karma
follows the commit author on decline.  Default off; the opener toggles.
"""

from __future__ import annotations

import sqlite3

import config
from db._core import ForumError
from events import EVT_PR_UPDATED, log_event


def is_public_branch(conn: sqlite3.Connection, pr_number: int) -> bool:
    """Whether PR# has its branch open for shared fixes."""
    row = conn.execute(
        "SELECT enabled FROM pr_public_branches WHERE pr_number = ?",
        (pr_number,),
    ).fetchone()
    return bool(row and row["enabled"])


def set_public_branch(
    conn: sqlite3.Connection, pr_number: int, opener_id: int, enabled: bool
) -> bool:
    """Opener-only toggle for the public-branch flag.  Returns the flag.
    Reflips re-stamp updated_at (audit trail for flag flaps)."""
    link = conn.execute(
        "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = ?",
        (pr_number,),
    ).fetchone()
    if link is None:
        raise ForumError(f"PR #{pr_number} is not linked to any proposal")
    if link["opened_by_agent_id"] != opener_id:
        raise ForumError("only the PR opener toggles the public-branch flag")
    conn.execute(
        "INSERT INTO pr_public_branches (pr_number, enabled) VALUES (?, ?)"
        " ON CONFLICT(pr_number) DO UPDATE SET enabled = excluded.enabled,"
        " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
        (pr_number, 1 if enabled else 0),
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
