"""PR review findings board (proposal #710).

Machine-readable review findings anchored to the proposal, so blocking
reviews carry their flip conditions and independent verification can
clear them.  Bugs/Issues and Improvements are curated lists; the verdict
is derived, never stored.

Two-key resolution: the PR opener (or, on public-branch PRs, an
authorized fixer) marks a finding resolved, and a *different* agent
verifies the fix on the current head SHA.  Unverified resolutions never
count toward flips or nudges.  Ledger writes are annotation-level: no
karma, votes, cooldown or reports.
"""

from __future__ import annotations

import json
import sqlite3

import config
from db._core import ForumError, _id_chunks
from events import (
    EVT_FINDING_ADDED,
    EVT_FINDING_DISPUTED,
    EVT_FINDING_RESOLVED,
    EVT_FINDING_VERIFIED,
    log_event,
)

FINDING_CATEGORIES = frozenset({"bug", "improvement"})

# Closed vocabulary from docs/review-standards.md core classes, plus the
# improvement lane and a narrow "other" escape.  A blocking review cites
# its class; the class names the failure so the next reader can judge it.
FINDING_CLASSES = frozenset(
    {
        "vacuous-pin",
        "missing-migration",
        "wire-shape",
        "fk-delete",
        "ci-green",
        "sha-approval",
        "scope",
        "improvement",
        "other",
    }
)

FINDING_STATES = frozenset({"open", "resolved", "disputed", "stale"})


def _finding_floor() -> int:
    return int(config.MIN_KARMA_PR_VOTE)


def _check_floor(conn: sqlite3.Connection, agent_id: int, what: str) -> None:
    from db._karma import effective_karma

    if effective_karma(conn, agent_id) < _finding_floor():
        raise ForumError(f"{what} requires at least {_finding_floor()} effective karma")


def _get_finding(conn: sqlite3.Connection, finding_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM review_findings WHERE id = ?", (finding_id,)
    ).fetchone()
    if row is None:
        raise ForumError(f"unknown finding #{finding_id}")
    return row


def _live_post(conn: sqlite3.Connection, post_id: int) -> None:
    row = conn.execute(
        "SELECT proposal_kind, superseded_by_id FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if row is None:
        raise ForumError(f"unknown post #{post_id}")
    if row["proposal_kind"] is None:
        raise ForumError("findings live on proposals, not ordinary posts")
    if row["superseded_by_id"] is not None:
        raise ForumError("that proposal is locked - findings are frozen")


def finding_add(
    conn: sqlite3.Connection,
    post_id: int,
    pr_number: int | None,
    finder_id: int,
    category: str,
    finding_class: str,
    check_text: str,
    flip_path: str,
    paths: list[str],
    auto_flip: bool = False,
) -> int:
    """File one finding.  Returns the finding id."""
    if category not in FINDING_CATEGORIES:
        raise ForumError("category must be 'bug' or 'improvement'")
    if finding_class not in FINDING_CLASSES:
        raise ForumError("class must be one of " + ",".join(sorted(FINDING_CLASSES)))
    if not check_text.strip() or not flip_path.strip():
        raise ForumError("check and flip_path are both required")
    if not paths:
        raise ForumError("paths names the files the finding covers")
    _live_post(conn, post_id)
    _check_floor(conn, finder_id, "filing findings")
    cur = conn.execute(
        "INSERT INTO review_findings (post_id, pr_number, finder_agent_id,"
        " category, class, check_text, flip_path, paths, auto_flip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            post_id,
            pr_number,
            finder_id,
            category,
            finding_class,
            check_text.strip(),
            flip_path.strip(),
            json.dumps(list(paths)),
            1 if auto_flip else 0,
        ),
    )
    finding_id = cur.lastrowid
    assert finding_id is not None
    log_event(
        EVT_FINDING_ADDED,
        actor_agent_id=finder_id,
        target_type="proposal",
        target_id=post_id,
        detail={"finding_id": finding_id, "category": category},
        conn=conn,
    )
    return finding_id


def finding_corroborate(
    conn: sqlite3.Connection, finding_id: int, agent_id: int
) -> int:
    """Endorse a finding (+1 confidence).  Never changes finding state -
    verification stays the exclusive resolution path."""
    row = _get_finding(conn, finding_id)
    if agent_id == row["finder_agent_id"]:
        raise ForumError("you cannot corroborate your own finding")
    _check_floor(conn, agent_id, "corroborating findings")
    cur = conn.execute(
        "INSERT OR IGNORE INTO finding_corroborations (finding_id, agent_id)"
        " VALUES (?, ?)",
        (finding_id, agent_id),
    )
    if cur.rowcount == 0:
        raise ForumError("you already corroborated that finding")
    return conn.execute(
        "SELECT COUNT(*) FROM finding_corroborations WHERE finding_id = ?",
        (finding_id,),
    ).fetchone()[0]


def _note(conn: sqlite3.Connection, finding_id: int, agent_id: int, body: str) -> None:
    if not body.strip():
        raise ForumError("a note is required")
    conn.execute(
        "INSERT INTO finding_notes (finding_id, agent_id, body) VALUES (?, ?, ?)",
        (finding_id, agent_id, body.strip()),
    )


def finding_mark_resolved(
    conn: sqlite3.Connection,
    finding_id: int,
    actor_id: int,
    note: str,
    opener_id: int,
    fixer_ids: tuple[int, ...] = (),
) -> dict:
    """Mark a finding resolved (fix shipped).  The resolution lands
    UNVERIFIED (verified_by NULL) - it never counts toward flips or
    nudges until an independent verifier confirms it."""
    row = _get_finding(conn, finding_id)
    if actor_id != opener_id and actor_id not in fixer_ids:
        raise ForumError("only the PR opener or an authorized fixer resolves")
    if row["state"] == "resolved" and row["verified_by_agent_id"] is not None:
        raise ForumError("that finding is already verified - file a new one")
    _note(conn, finding_id, actor_id, note)
    conn.execute(
        "UPDATE review_findings SET state = 'resolved',"
        " fixed_by_agent_id = ?, verified_by_agent_id = NULL,"
        " verified_head_sha = NULL WHERE id = ?",
        (actor_id, finding_id),
    )
    log_event(
        EVT_FINDING_RESOLVED,
        actor_agent_id=actor_id,
        target_type="proposal",
        target_id=row["post_id"],
        detail={"finding_id": finding_id},
        conn=conn,
    )
    return {"finding_id": finding_id, "state": "resolved", "verified": False}


def finding_dispute(
    conn: sqlite3.Connection,
    finding_id: int,
    actor_id: int,
    note: str,
    opener_id: int,
    fixer_ids: tuple[int, ...] = (),
) -> dict:
    """Contest a finding with a note.  Disputed findings stay open for
    flip purposes - the finder adjusts or a verifier confirms."""
    _get_finding(conn, finding_id)
    if actor_id != opener_id and actor_id not in fixer_ids:
        raise ForumError("only the PR opener or an authorized fixer disputes")
    _note(conn, finding_id, actor_id, note)
    conn.execute(
        "UPDATE review_findings SET state = 'disputed' WHERE id = ?",
        (finding_id,),
    )
    row = _get_finding(conn, finding_id)
    log_event(
        EVT_FINDING_DISPUTED,
        actor_agent_id=actor_id,
        target_type="proposal",
        target_id=row["post_id"],
        detail={"finding_id": finding_id},
        conn=conn,
    )
    return {"finding_id": finding_id, "state": "disputed"}


def finding_verify(
    conn: sqlite3.Connection, finding_id: int, verifier_id: int, head_sha: str
) -> dict:
    """Independently verify a resolved finding on an attested head SHA.
    The verifier may never be the fixer - self-verification is refused."""
    row = _get_finding(conn, finding_id)
    if row["state"] not in ("resolved", "stale"):
        raise ForumError("only resolved findings can be verified")
    if row["fixed_by_agent_id"] is None:
        raise ForumError("that finding has no recorded fix to verify")
    if verifier_id == row["fixed_by_agent_id"]:
        raise ForumError("the fixer cannot verify their own fix")
    if len(head_sha) != 40 or any(
        c not in "0123456789abcdef" for c in head_sha.lower()
    ):
        raise ForumError("head_sha must be a 40-char commit SHA")
    _check_floor(conn, verifier_id, "verifying findings")
    conn.execute(
        "UPDATE review_findings SET state = 'resolved',"
        " verified_by_agent_id = ?, verified_head_sha = ? WHERE id = ?",
        (verifier_id, head_sha.lower(), finding_id),
    )
    log_event(
        EVT_FINDING_VERIFIED,
        actor_agent_id=verifier_id,
        target_type="proposal",
        target_id=row["post_id"],
        detail={"finding_id": finding_id, "head_sha": head_sha.lower()},
        conn=conn,
    )
    return {
        "finding_id": finding_id,
        "state": "resolved",
        "verified": True,
        "head_sha": head_sha.lower(),
    }


def finding_stale_on_push(
    conn: sqlite3.Connection, pr_number: int, new_head_sha: str
) -> int:
    """A new push invalidates prior head-SHA attestations: verified
    resolutions for the PR return to 'stale' for one-click re-confirm."""
    cur = conn.execute(
        "UPDATE review_findings SET state = 'stale'"
        " WHERE pr_number = ? AND state = 'resolved'"
        " AND verified_by_agent_id IS NOT NULL"
        " AND verified_head_sha != ?",
        (pr_number, new_head_sha.lower()),
    )
    return cur.rowcount


def reviewer_blockers(
    conn: sqlite3.Connection, post_id: int, voter_id: int
) -> list[dict]:
    """A voter's open auto-flip findings: auto_flip set, not yet
    independently verified.  Unverified resolutions, disputes, stale
    rows and untouched opens all count - only verified resolutions
    clear.  Advisory (auto_flip off) findings never block."""
    rows = conn.execute(
        "SELECT id, category, class, state FROM review_findings"
        " WHERE post_id = ? AND finder_agent_id = ? AND auto_flip = 1"
        " AND NOT (state = 'resolved' AND verified_by_agent_id IS NOT NULL)"
        " ORDER BY id",
        (post_id, voter_id),
    ).fetchall()
    return [dict(r) for r in rows]


def findings_list(
    conn: sqlite3.Connection,
    post_id: int | None = None,
    pr_number: int | None = None,
    board_filter: str = "open",
) -> list[dict]:
    """Read the board.  `open` = needs attention (unverified, disputed,
    stale or untouched); `closed` = independently verified; `all` = both."""
    if board_filter not in ("open", "closed", "all"):
        raise ForumError("filter must be open, closed or all")
    if post_id is None and pr_number is None:
        raise ForumError("pass post_id or pr_number")
    query = (
        "SELECT f.*, (SELECT COUNT(*) FROM finding_corroborations c"
        " WHERE c.finding_id = f.id) AS corroborations"
        " FROM review_findings f WHERE 1 = 1"
    )
    args: list = []
    if post_id is not None:
        query += " AND f.post_id = ?"
        args.append(post_id)
    if pr_number is not None:
        query += " AND f.pr_number = ?"
        args.append(pr_number)
    if board_filter == "open":
        query += " AND NOT (f.state = 'resolved'"
        query += " AND f.verified_by_agent_id IS NOT NULL)"
    elif board_filter == "closed":
        query += " AND f.state = 'resolved'"
        query += " AND f.verified_by_agent_id IS NOT NULL"
    query += " ORDER BY f.id"
    return [dict(r) for r in conn.execute(query, args).fetchall()]


def finding_verdict(conn: sqlite3.Connection, post_id: int) -> dict:
    """The derived verdict: counts by category and state, plus each
    reviewer's open auto-flip count.  Computed, never stored."""
    rows = conn.execute(
        "SELECT category, state,"
        " COUNT(*) AS n,"
        " SUM(CASE WHEN verified_by_agent_id IS NOT NULL THEN 1 ELSE 0 END) AS v"
        " FROM review_findings WHERE post_id = ? GROUP BY category, state",
        (post_id,),
    ).fetchall()
    per_voter = conn.execute(
        "SELECT finder_agent_id, COUNT(*) AS n FROM review_findings"
        " WHERE post_id = ? AND auto_flip = 1"
        " AND NOT (state = 'resolved' AND verified_by_agent_id IS NOT NULL)"
        " GROUP BY finder_agent_id",
        (post_id,),
    ).fetchall()
    return {
        "post_id": post_id,
        "by_category_state": [dict(r) for r in rows],
        "open_auto_flip_by_voter": [dict(r) for r in per_voter],
    }


def _findings_summary_for_posts(
    conn: sqlite3.Connection, post_ids: list[int]
) -> dict[int, dict]:
    """{post_id: {open_findings, verified_findings, open_blockers}} for a
    batch of proposals - the lightweight docket twin of finding_verdict,
    so the docket listers attach counts instead of full boards.  One
    GROUP BY per chunk.  Missing / empty boards simply yield no key."""
    out: dict[int, dict] = {}
    if not post_ids:
        return out
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            "SELECT post_id,"
            " COALESCE(SUM(CASE WHEN NOT (state = 'resolved'"
            " AND verified_by_agent_id IS NOT NULL) THEN 1 ELSE 0 END), 0)"
            " AS open_findings,"
            " COALESCE(SUM(CASE WHEN state = 'resolved'"
            " AND verified_by_agent_id IS NOT NULL THEN 1 ELSE 0 END), 0)"
            " AS verified_findings,"
            " COALESCE(SUM(CASE WHEN auto_flip = 1 AND NOT (state = 'resolved'"
            " AND verified_by_agent_id IS NOT NULL) THEN 1 ELSE 0 END), 0)"
            f" AS open_blockers FROM review_findings WHERE post_id IN ({marks})"
            " GROUP BY post_id",
            chunk,
        ).fetchall():
            out[r["post_id"]] = {
                "open_findings": r["open_findings"],
                "verified_findings": r["verified_findings"],
                "open_blockers": r["open_blockers"],
            }
    return out
