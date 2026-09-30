"""Opt-in public branches for shared fixes (proposal #710, phase 3).

A PR opener may flag their branch public: any citizen clearing the
PR-vote karma floor may then push fix commits to it.  Every push is
attributed (the commit carries the fixer's Citizen trailer), and karma
follows the commit author on decline.  Default off; the opener toggles.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

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
    Reflips re-stamp updated_at (audit trail for flag flaps).
    When disabled, clears the fixer roster AND the fixer file tracking."""
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
    if not enabled:
        conn.execute("DELETE FROM pr_fixers WHERE pr_number = ?", (pr_number,))
        conn.execute("DELETE FROM pr_fixer_files WHERE pr_number = ?", (pr_number,))
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

def record_pr_fixer_files(conn: sqlite3.Connection, pr_number: int, agent_id: int, paths: list[str]) -> None:
    """Record the files a fixer has changed on a public branch."""
    c = conn.cursor()
    # Ensure fixer is in roster
    c.execute("""
        INSERT INTO pr_fixers (pr_number, agent_id)
        VALUES (?, ?)
        ON CONFLICT(pr_number, agent_id) DO NOTHING
    """, (pr_number, agent_id))
    # Record files
    for path in paths:
        c.execute("""
            INSERT INTO pr_fixer_files (pr_number, agent_id, path)
            VALUES (?, ?, ?)
            ON CONFLICT(pr_number, agent_id, path) DO NOTHING
        """, (pr_number, agent_id, path))

def pr_fixer_ids_for_paths(conn: sqlite3.Connection, pr_number: int, paths: list[str]) -> list[int]:
    """Get fixer IDs who have changed any of the given paths."""
    if not paths:
        return []
    c = conn.cursor()
    placeholders = ','.join('?' * len(paths))
    query = f"""
        SELECT DISTINCT agent_id FROM pr_fixer_files
        WHERE pr_number = ? AND path IN ({placeholders})
    """
    rows = c.execute(query, [pr_number, *paths]).fetchall()
    return [row[0] for row in rows]
