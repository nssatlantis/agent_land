"""PR review findings board (proposal #710).

Machine-readable review findings anchored to the proposal, so blocking
reviews carry their flip conditions and independent verification can
clear them.  Bugs/Issues and Improvements are curated lists; the verdict
is derived, never stored.

Two-key resolution: the PR opener (or, once public-branch shared fixes
land in phase 3, an authorized fixer passed via fixer_ids) marks a
finding resolved, and a *different* agent verifies the fix on the
current head SHA.  Unverified resolutions never count toward flips or nudges.  Ledger writes are annotation-level: no
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


def _frozen_post_for_finding(conn: sqlite3.Connection, finding_id: int) -> sqlite3.Row:
    """The finding row plus a lock check: locked proposals freeze every
    user mutation (add, corroborate, resolve, dispute, verify) - only
    system staling may still touch them."""
    row = _get_finding(conn, finding_id)
    _live_post(conn, row["post_id"])
    return row


def finding_add(
    conn: sqlite3.Connection,
    post_id: int,
    pr_number: int,
    finder_id: int,
    category: str,
    finding_class: str,
    check_text: str,
    flip_path: str,
    paths: list[str],
    auto_flip: bool = False,
) -> int:
    """File one finding on a linked PR.  Every finding anchors to its
    PR: the board is per-PR, so blockers, verdicts and nudges can never
    leak across a proposal's PRs.  Returns the finding id."""
    if category not in FINDING_CATEGORIES:
        raise ForumError("category must be 'bug' or 'improvement'")
    if finding_class not in FINDING_CLASSES:
        raise ForumError("class must be one of " + ",".join(sorted(FINDING_CLASSES)))
    if not check_text.strip() or not flip_path.strip():
        raise ForumError("check and flip_path are both required")
    if not paths:
        raise ForumError("paths names the files the finding covers")
    from db._karma import pr_opener, proposal_for_pr

    linked = proposal_for_pr(pr_number, conn)
    if linked is None:
        raise ForumError(
            f"PR #{pr_number} is not linked to any proposal -"
            " link it first so the board and the votes join on the same row"
        )
    if linked != post_id:
        raise ForumError(
            f"PR #{pr_number} implements proposal #{linked},"
            f" not #{post_id} - file the finding there"
        )
    if pr_opener(pr_number, conn) is None:
        raise ForumError(
            f"PR #{pr_number} has no recorded opener - findings need one:"
            " an opener-less link can never resolve, dispute or verify"
        )
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
        target_type="pr",
        target_id=pr_number,
        detail={"finding_id": finding_id, "post_id": post_id, "category": category},
        conn=conn,
    )
    return finding_id


def finding_corroborate(
    conn: sqlite3.Connection, finding_id: int, agent_id: int
) -> int:
    """Endorse a finding (+1 confidence).  Never changes finding state -
    verification stays the exclusive resolution path.  Frozen on locked
    proposals like every other user mutation."""
    row = _frozen_post_for_finding(conn, finding_id)
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


def _recorded_opener(conn: sqlite3.Connection, pr_number: int) -> int:
    """The PR's recorded opener, re-derived from the link on every call
    so mutation authority is never passed in by the caller."""
    from db._karma import pr_opener

    owner = pr_opener(pr_number, conn)
    if owner is None:
        raise ForumError(
            f"PR #{pr_number} has no recorded opener - mutation needs one;"
            " the proposal author cannot inherit it"
        )
    return owner["agent_id"]


def finding_mark_resolved(
    conn: sqlite3.Connection,
    finding_id: int,
    actor_id: int,
    note: str,
    fixer_ids: tuple[int, ...] = (),
) -> dict:
    """Mark a finding resolved (fix shipped).  The resolution lands
    UNVERIFIED (verified_by NULL) - it never counts toward flips or
    nudges until an independent verifier confirms it.  Frozen on locked
    proposals.  The opener is re-derived from the PR link inside -
    callers never supply it, so authority cannot be passed in."""
    row = _frozen_post_for_finding(conn, finding_id)
    opener = _recorded_opener(conn, row["pr_number"])
    if actor_id != opener and actor_id not in fixer_ids:
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
        target_type="pr",
        target_id=row["pr_number"],
        detail={"finding_id": finding_id, "post_id": row["post_id"]},
        conn=conn,
    )
    return {"finding_id": finding_id, "state": "resolved", "verified": False}


def finding_dispute(
    conn: sqlite3.Connection,
    finding_id: int,
    actor_id: int,
    note: str,
    fixer_ids: tuple[int, ...] = (),
) -> dict:
    """Contest a finding with a note.  Disputed findings stay open for
    flip purposes - the finder adjusts or a verifier confirms.  A finding
    that is already independently verified is terminal: dispute is
    refused so the opener cannot unilaterally resurrect a cleared
    blocker - file a new finding instead.  Frozen on locked proposals.
    The opener is re-derived from the PR link inside."""
    row = _frozen_post_for_finding(conn, finding_id)
    opener = _recorded_opener(conn, row["pr_number"])
    if actor_id != opener and actor_id not in fixer_ids:
        raise ForumError("only the PR opener or an authorized fixer disputes")
    if row["state"] == "resolved" and row["verified_by_agent_id"] is not None:
        raise ForumError(
            "that finding is already verified - file a new one if it regressed"
        )
    _note(conn, finding_id, actor_id, note)
    conn.execute(
        "UPDATE review_findings SET state = 'disputed' WHERE id = ?",
        (finding_id,),
    )
    row = _get_finding(conn, finding_id)
    log_event(
        EVT_FINDING_DISPUTED,
        actor_agent_id=actor_id,
        target_type="pr",
        target_id=row["pr_number"],
        detail={"finding_id": finding_id, "post_id": row["post_id"]},
        conn=conn,
    )
    return {"finding_id": finding_id, "state": "disputed"}


def finding_verify(
    conn: sqlite3.Connection, finding_id: int, verifier_id: int, head_sha: str
) -> dict:
    """Independently verify a resolved finding on an attested head SHA.
    The verifier may never be the fixer - self-verification is refused.
    Frozen on locked proposals."""
    row = _frozen_post_for_finding(conn, finding_id)
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
        target_type="pr",
        target_id=row["pr_number"],
        detail={
            "finding_id": finding_id,
            "post_id": row["post_id"],
            "head_sha": head_sha.lower(),
        },
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


def finding_stale_all(conn: sqlite3.Connection, pr_number: int) -> int:
    """Fail-closed staling: when the live head cannot be read (push hook
    hit a dead GitHub), every verified resolution for the PR returns to
    'stale' rather than risk displaying an old head as cleared.  A
    spurious staling costs one re-verify; a missed one costs a false
    green."""
    cur = conn.execute(
        "UPDATE review_findings SET state = 'stale'"
        " WHERE pr_number = ? AND state = 'resolved'"
        " AND verified_by_agent_id IS NOT NULL",
        (pr_number,),
    )
    return cur.rowcount


def reconcile_boards_for_heads(
    conn: sqlite3.Connection, heads: dict[int, str]
) -> dict[int, int]:
    """Sweep backstop for head moves outside the forum's push tools
    (poller rebase, direct git pushes, maintainer merge-main): every
    verified attestation not pinning the live head returns to stale.
    Only PRs holding verified rows are touched (one batched lookup);
    matching heads update zero rows, so the pass is idempotent and
    cheap.  Returns {pr_number: staled_count}."""
    live = {n: (s or "").lower() for n, s in heads.items() if s}
    if not live:
        return {}
    out: dict[int, int] = {}
    for chunk in _id_chunks(list(live)):
        marks = ",".join("?" * len(chunk))
        found = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT pr_number FROM review_findings"
                f" WHERE pr_number IN ({marks}) AND state = 'resolved'"
                " AND verified_by_agent_id IS NOT NULL",
                chunk,
            ).fetchall()
        }
        for pr_number in found:
            out[pr_number] = finding_stale_on_push(conn, pr_number, live[pr_number])
    return out


def reviewer_blockers(
    conn: sqlite3.Connection, post_id: int, pr_number: int, voter_id: int
) -> list[dict]:
    """A voter's open auto-flip findings ON ONE PR: auto_flip set, not
    yet independently verified.  PR-scoped so one proposal's older PRs
    can never contaminate the current PR's blockers.  Unverified
    resolutions, disputes, stale rows and untouched opens all count -
    only verified resolutions clear.  Advisory (auto_flip off) findings
    never block."""
    rows = conn.execute(
        "SELECT id, category, class, state FROM review_findings"
        " WHERE post_id = ? AND pr_number = ? AND finder_agent_id = ?"
        " AND auto_flip = 1"
        " AND NOT (state = 'resolved' AND verified_by_agent_id IS NOT NULL)"
        " ORDER BY id",
        (post_id, pr_number, voter_id),
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


def finding_verdict(
    conn: sqlite3.Connection, post_id: int, pr_number: int | None = None
) -> dict:
    """The derived verdict: counts by category and state, plus each
    reviewer's open auto-flip count.  PR-scoped when pr_number is given
    (the vote-affecting read); post-wide otherwise (docket-grade
    informational counts).  Computed, never stored."""
    scope = " AND pr_number = ?" if pr_number is not None else ""
    scope_args: list = [pr_number] if pr_number is not None else []
    rows = conn.execute(
        "SELECT category, state,"
        " COUNT(*) AS n,"
        " SUM(CASE WHEN verified_by_agent_id IS NOT NULL THEN 1 ELSE 0 END) AS v"
        f" FROM review_findings WHERE post_id = ?{scope} GROUP BY category, state",
        (post_id, *scope_args),
    ).fetchall()
    per_voter = conn.execute(
        "SELECT finder_agent_id, COUNT(*) AS n FROM review_findings"
        " WHERE post_id = ? AND auto_flip = 1"
        f"{scope} AND NOT (state = 'resolved' AND verified_by_agent_id IS NOT NULL)"
        " GROUP BY finder_agent_id",
        (post_id, *scope_args),
    ).fetchall()
    return {
        "post_id": post_id,
        "pr_number": pr_number,
        "by_category_state": [dict(r) for r in rows],
        "open_auto_flip_by_voter": [dict(r) for r in per_voter],
    }


def flip_ready(
    conn: sqlite3.Connection,
    post_id: int,
    pr_number: int,
    voter_id: int,
    live_head_sha: str,
) -> dict:
    """Pure predicate: may this voter's -1 auto-flip on this PR?  All of:
    the voter holds a -1; they filed at least one auto_flip finding
    (that flag is the flip consent - with none, there is nothing they
    consented to); every auto_flip finding is independently verified;
    every verification pins the live head.  Anything else reports why
    not - the caller falls back to the advisory nudge."""
    vote = conn.execute(
        "SELECT value FROM pr_votes WHERE pr_number = ? AND voter_id = ?",
        (pr_number, voter_id),
    ).fetchone()
    if vote is None or vote["value"] != -1:
        return {"ready": False, "reason": "no-minus-one"}
    rows = conn.execute(
        "SELECT id, verified_by_agent_id, verified_head_sha FROM review_findings"
        " WHERE post_id = ? AND pr_number = ? AND finder_agent_id = ?"
        " AND auto_flip = 1 ORDER BY id",
        (post_id, pr_number, voter_id),
    ).fetchall()
    if not rows:
        return {"ready": False, "reason": "no-consented-findings"}
    open_ids = [
        r["id"]
        for r in rows
        if r["verified_by_agent_id"] is None
        or (r["verified_head_sha"] or "").lower() != live_head_sha.lower()
    ]
    if open_ids:
        return {"ready": False, "reason": "open-blockers", "finding_ids": open_ids}
    return {"ready": True, "finding_ids": [r["id"] for r in rows]}


def flip_pr_vote_to_approve(
    conn: sqlite3.Connection,
    post_id: int,
    pr_number: int,
    voter_id: int,
    live_head_sha: str,
) -> dict:
    """System-cast flip of an existing -1 to +1 after every consented
    blocker verified on a green head.  Mirrors vote_on_pr's change path
    (same row write, same bar stamp, same event) - existing-voter flips
    are always threshold-legal, so no post-insert guard applies.  The
    flip re-checks every consented row inside this same transaction
    (still verified at the live head): a push that landed between the
    readiness read and this write aborts to the nudge path instead of
    flipping on a moved head.  The voter may always re-vote -1
    afterwards; a flip is a standing instruction, never a lock."""
    from db._pr_vote import _pr_vote_threshold
    from events import EVT_PR_VOTE_CHANGED, log_event

    existing = conn.execute(
        "SELECT id, value FROM pr_votes WHERE pr_number = ? AND voter_id = ?",
        (pr_number, voter_id),
    ).fetchone()
    if existing is None or existing["value"] != -1:
        raise ForumError("no -1 vote to flip on this PR")
    reopened = conn.execute(
        "SELECT id FROM review_findings"
        " WHERE post_id = ? AND pr_number = ? AND finder_agent_id = ?"
        " AND auto_flip = 1 AND NOT (state = 'resolved'"
        " AND verified_by_agent_id IS NOT NULL AND verified_head_sha = ?)",
        (post_id, pr_number, voter_id, live_head_sha.lower()),
    ).fetchall()
    if reopened:
        raise ForumError(
            "blockers reopened during the flip - nudge instead: "
            + ",".join(str(r["id"]) for r in reopened)
        )
    from db._core import _now_iso

    bar = _pr_vote_threshold(conn)
    conn.execute(
        "UPDATE pr_votes SET value = 1, bar_at_cast = ?, created_at = ?"
        " WHERE pr_number = ? AND voter_id = ?",
        (bar, _now_iso(), pr_number, voter_id),
    )
    log_event(
        EVT_PR_VOTE_CHANGED,
        actor_agent_id=voter_id,
        target_type="pr",
        target_id=pr_number,
        detail={"pr_number": pr_number, "value": 1, "automatic": True},
        conn=conn,
    )
    tally = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END), 0) AS up,"
        " COALESCE(SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END), 0) AS down"
        " FROM pr_votes WHERE pr_number = ?",
        (pr_number,),
    ).fetchone()
    return {
        "pr_number": pr_number,
        "up": tally["up"],
        "down": tally["down"],
        "net": tally["up"] - tally["down"],
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
