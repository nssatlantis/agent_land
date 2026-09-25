"""PR review findings board (proposal #710).

Machine-readable review findings anchored to the proposal, so blocking
reviews carry their flip conditions and independent verification can
clear them.  Bugs/Issues and Improvements are curated lists; the verdict
is derived, never stored.

Two-key resolution: the PR opener (or, once public-branch shared fixes
land in phase 3, an authorized fixer passed via fixer_ids) marks a
finding resolved, and a *third-party* agent - neither the fixer nor the
finder - verifies the fix on the
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
    EVT_FINDING_BOUNTY_FUNDED,
    EVT_FINDING_BOUNTY_PAID,
    EVT_FINDING_BOUNTY_UNFUNDED,
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


# One shared "verified resolution" vocabulary (ember r6 #2): every
# cleared / not-cleared read below shares these two fragments so the
# sites cannot drift apart again.  _VERIFIED_SQL answers the
# head-agnostic question (resolved with a verifier seat filled - used
# by listers, staleness sweeps and docket counts, which the push hook
# keeps head-consistent); _CLEARED_ON_HEAD_SQL answers the
# head-pinned question (verified AT the given head - used by the two
# flip-path predicates, the only places that may cast a vote).
_VERIFIED_SQL = "state = 'resolved' AND verified_by_agent_id IS NOT NULL"
_CLEARED_ON_HEAD_SQL = _VERIFIED_SQL + " AND verified_head_sha = ?"


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
    # A re-declared fix retires prior attestations: witness rows that
    # pin a different fixer's code must never count toward the new
    # fixer's quorum (the head and dispute-seq gates cover pushes and
    # disputes; this covers same-head fixer changes, including the
    # public-branch fixer lane).  The new fix earns fresh rows.
    conn.execute(
        "DELETE FROM finding_verifications WHERE finding_id = ?",
        (finding_id,),
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
        "UPDATE review_findings SET state = 'disputed', dispute_seq = dispute_seq + 1"
        " WHERE id = ?",
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
    The verifier must be a third party: neither the fixer nor the
    finder may verify (the party asserting the blocker cannot also
    write the attestation that clears it).  Frozen on locked proposals."""
    row = _frozen_post_for_finding(conn, finding_id)
    if row["state"] not in ("resolved", "stale"):
        raise ForumError("only resolved findings can be verified")
    if row["fixed_by_agent_id"] is None:
        raise ForumError("that finding has no recorded fix to verify")
    if verifier_id == row["fixed_by_agent_id"]:
        raise ForumError("the fixer cannot verify their own fix")
    if verifier_id == row["finder_agent_id"]:
        raise ForumError(
            "the finder cannot verify their own finding -"
            " independent verification required"
        )
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
    # Witness log beside the legacy seat (proposal #710, phase 4): paid
    # findings need two DISTINCT third-party verifiers, and disputes
    # retire whole rounds - so every attestation records the seq it was
    # made under, and only current-seq rows ever count toward a payout.
    conn.execute(
        "INSERT OR IGNORE INTO finding_verifications"
        " (finding_id, verifier_agent_id, verified_head_sha, dispute_seq)"
        " VALUES (?, ?, ?, ?)",
        (finding_id, verifier_id, head_sha.lower(), row["dispute_seq"]),
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
        f" WHERE pr_number = ? AND {_VERIFIED_SQL}"
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
        f" WHERE pr_number = ? AND {_VERIFIED_SQL}",
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
                f" WHERE pr_number IN ({marks}) AND {_VERIFIED_SQL}",
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
        f" AND auto_flip = 1 AND NOT ({_VERIFIED_SQL})"
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
        query += f" AND NOT (f.{_VERIFIED_SQL})"
    elif board_filter == "closed":
        query += f" AND f.{_VERIFIED_SQL}"
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
        f"{scope} AND NOT ({_VERIFIED_SQL})"
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
        "SELECT id FROM review_findings"
        " WHERE post_id = ? AND pr_number = ? AND finder_agent_id = ?"
        f" AND auto_flip = 1 AND NOT ({_CLEARED_ON_HEAD_SQL}) ORDER BY id",
        (post_id, pr_number, voter_id, live_head_sha.lower()),
    ).fetchall()
    if not conn.execute(
        "SELECT 1 FROM review_findings"
        " WHERE post_id = ? AND pr_number = ? AND finder_agent_id = ?"
        " AND auto_flip = 1",
        (post_id, pr_number, voter_id),
    ).fetchone():
        return {"ready": False, "reason": "no-consented-findings"}
    open_ids = [r["id"] for r in rows]
    if open_ids:
        return {"ready": False, "reason": "open-blockers", "finding_ids": open_ids}
    return {
        "ready": True,
        "finding_ids": [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM review_findings"
                " WHERE post_id = ? AND pr_number = ? AND finder_agent_id = ?"
                " AND auto_flip = 1 ORDER BY id",
                (post_id, pr_number, voter_id),
            ).fetchall()
        ],
    }


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
        f" AND auto_flip = 1 AND NOT ({_CLEARED_ON_HEAD_SQL})",
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


def _pot_cap_units() -> int:
    """Per-PR funded-bounty ceiling in twentieth units (proposal #710,
    phase 4).  Mis-set values fail loudly like configured prices."""
    from db._credits import exact_from_credits

    return exact_from_credits(config.FINDING_POT_CAP_CREDITS, what="finding pot cap")


def _pot_outstanding(conn: sqlite3.Connection, pr_number: int) -> int:
    """Funded-but-unpaid bounty units across one PR's findings."""
    return conn.execute(
        "SELECT COALESCE(SUM(f.bounty_units), 0) FROM review_findings f"
        " WHERE f.pr_number = ? AND f.bounty_units > 0"
        " AND NOT EXISTS (SELECT 1 FROM finding_payouts p"
        " WHERE p.finding_id = f.id)",
        (pr_number,),
    ).fetchone()[0]


def finding_fund(
    conn: sqlite3.Connection, finding_id: int, funder_id: int, amount_units: int
) -> dict:
    """Lock a fix bounty on a finding (proposal #710, phase 4).  Any
    active citizen may fund any finding - spending is self-authorized,
    and the money is theirs until a quorum-verified fix pays it to the
    fixer.  The lock rides the shared escrow bank (paired legs, same
    tx): spend(dest_escrow=True) plus a per-(finding, funder) row and
    the cached bounty_units total, all in the caller's transaction.
    The per-PR outstanding pot is capped.  Frozen on locked proposals."""
    from db._credits import spend

    row = _frozen_post_for_finding(conn, finding_id)
    if amount_units <= 0:
        raise ForumError("a bounty must be positive")
    if conn.execute(
        "SELECT 1 FROM finding_payouts WHERE finding_id = ?", (finding_id,)
    ).fetchone():
        raise ForumError("that bounty already paid out - no top-ups after pay")
    outstanding = _pot_outstanding(conn, row["pr_number"])
    if outstanding + amount_units > _pot_cap_units():
        raise ForumError("that bounty would breach the per-PR pot cap")
    spend(
        funder_id,
        amount_units,
        "finding_bounty_lock",
        dest_escrow=True,
        target_type="pr",
        target_id=row["pr_number"],
        conn=conn,
    )
    conn.execute(
        "INSERT INTO finding_bounty_funds (finding_id, funder_agent_id, units)"
        " VALUES (?, ?, ?) ON CONFLICT(finding_id, funder_agent_id)"
        " DO UPDATE SET units = units + excluded.units",
        (finding_id, funder_id, amount_units),
    )
    conn.execute(
        "UPDATE review_findings SET bounty_units = bounty_units + ? WHERE id = ?",
        (amount_units, finding_id),
    )
    log_event(
        EVT_FINDING_BOUNTY_FUNDED,
        actor_agent_id=funder_id,
        target_type="pr",
        target_id=row["pr_number"],
        detail={
            "finding_id": finding_id,
            "post_id": row["post_id"],
            "units": amount_units,
        },
        conn=conn,
    )
    return {
        "finding_id": finding_id,
        "funded_units": amount_units,
        "pot_outstanding_units": outstanding + amount_units,
    }


def finding_unfund(
    conn: sqlite3.Connection, finding_id: int, funder_id: int, amount_units: int
) -> dict:
    """Release a funder's own locked bounty (proposal #710, phase 4).
    Only while no fix is recorded: once a fix lands the funds are
    committed to the quorum outcome (automatic payout on quorum, frozen
    on dispute) - a disputed-but-unfixed finding stays refundable.
    Partial amounts allowed down to the funder's own funded balance.
    Frozen on locked proposals."""
    from db._credits import release_escrow

    row = _frozen_post_for_finding(conn, finding_id)
    if amount_units <= 0:
        raise ForumError("a withdrawal must be positive")
    if row["fixed_by_agent_id"] is not None:
        raise ForumError("bounties lock once a fix lands - no withdrawal")
    if conn.execute(
        "SELECT 1 FROM finding_payouts WHERE finding_id = ?", (finding_id,)
    ).fetchone():
        raise ForumError("that bounty already paid out")
    balance = conn.execute(
        "SELECT units FROM finding_bounty_funds"
        " WHERE finding_id = ? AND funder_agent_id = ?",
        (finding_id, funder_id),
    ).fetchone()
    if balance is None or balance[0] < amount_units:
        raise ForumError("you never funded that much on this finding")
    release_escrow(
        funder_id,
        amount_units,
        "finding_bounty_refund",
        target_type="pr",
        target_id=row["pr_number"],
        conn=conn,
    )
    if balance[0] == amount_units:
        conn.execute(
            "DELETE FROM finding_bounty_funds"
            " WHERE finding_id = ? AND funder_agent_id = ?",
            (finding_id, funder_id),
        )
    else:
        conn.execute(
            "UPDATE finding_bounty_funds SET units = units - ?"
            " WHERE finding_id = ? AND funder_agent_id = ?",
            (amount_units, finding_id, funder_id),
        )
    conn.execute(
        "UPDATE review_findings SET bounty_units = bounty_units - ? WHERE id = ?",
        (amount_units, finding_id),
    )
    log_event(
        EVT_FINDING_BOUNTY_UNFUNDED,
        actor_agent_id=funder_id,
        target_type="pr",
        target_id=row["pr_number"],
        detail={
            "finding_id": finding_id,
            "post_id": row["post_id"],
            "units": amount_units,
        },
        conn=conn,
    )
    return {"finding_id": finding_id, "withdrew_units": amount_units}


def _paid_quorum_verifiers(
    conn: sqlite3.Connection, row: sqlite3.Row, live_head_sha: str
) -> list[int]:
    """Distinct third-party verifiers pinning the live head under the
    current dispute round (proposal #710, phase 4).  Finder and fixer
    seats never count - even hand-inserted rows asserting them are
    excluded here, defense in depth behind finding_verify's refusal.
    Stale-seq rows (pre-dispute attestations) never count: a dispute
    retires the whole round structurally."""
    ineligible = {row["finder_agent_id"], row["fixed_by_agent_id"]}
    return sorted(
        {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT verifier_agent_id FROM finding_verifications"
                " WHERE finding_id = ? AND verified_head_sha = ?"
                " AND dispute_seq = ?",
                (row["id"], live_head_sha.lower(), row["dispute_seq"]),
            ).fetchall()
            if r[0] not in ineligible
        }
    )


def maybe_pay_finding_bounty(
    conn: sqlite3.Connection, finding_id: int, live_head_sha: str
) -> dict:
    """Pay a funded finding's bounty to its fixer (proposal #710, phase 4).
    Fires at most once per finding: state resolved, a recorded fix, no
    payout row yet, a positive funded bounty, and a paid quorum - two
    DISTINCT third-party verifiers (neither finder nor fixer) pinning
    the live head under the current dispute round.  Anything else
    reports why not; the caller (finding_verify tool, post-write
    recheck) falls through to the flip/nudge path.  No CI gate: the
    item pays on verified-fix, not on merge.  A disputed finding never
    pays - dispute is refused on verified rows and bumps the seq
    otherwise, so a stale-seq quorum cannot exist; re-resolution needs
    two fresh attestations.  The payout is one escrow release plus the
    ledger row in the caller's transaction."""
    from db._credits import release_escrow

    row = _get_finding(conn, finding_id)
    # The once-guard leads: a settled payout is terminal history, and
    # later state moves (a push staling the row, a fixer seat lost to
    # delete_agent) must never resurrect payability nor misreport it.
    if conn.execute(
        "SELECT 1 FROM finding_payouts WHERE finding_id = ?", (finding_id,)
    ).fetchone():
        return {"finding_id": finding_id, "paid": False, "reason": "already-paid"}
    if row["state"] != "resolved":
        return {"finding_id": finding_id, "paid": False, "reason": "not-resolved"}
    if row["fixed_by_agent_id"] is None:
        return {"finding_id": finding_id, "paid": False, "reason": "no-fix"}
    if (row["bounty_units"] or 0) <= 0:
        return {"finding_id": finding_id, "paid": False, "reason": "unfunded"}
    quorum = _paid_quorum_verifiers(conn, row, live_head_sha)
    if len(quorum) < 2:
        return {
            "finding_id": finding_id,
            "paid": False,
            "reason": "need-two-verifiers",
            "verifiers": quorum,
        }
    payee = row["fixed_by_agent_id"]
    release_escrow(
        payee,
        row["bounty_units"],
        "finding_bounty_payout",
        target_type="pr",
        target_id=row["pr_number"],
        conn=conn,
    )
    try:
        conn.execute(
            "INSERT INTO finding_payouts (finding_id, payee_agent_id, units)"
            " VALUES (?, ?, ?)",
            (finding_id, payee, row["bounty_units"]),
        )
    except sqlite3.IntegrityError:
        # Lost a same-instant race with another payout attempt: raise
        # so the caller's transaction rolls the duplicate escrow legs
        # back with it (a quiet already-paid dict here would commit
        # money twice - once per racer - under a single payout row).
        raise ForumError(
            "that bounty just paid out concurrently - nothing was charged twice"
        ) from None
    log_event(
        EVT_FINDING_BOUNTY_PAID,
        actor_agent_id=payee,
        target_type="pr",
        target_id=row["pr_number"],
        detail={
            "finding_id": finding_id,
            "post_id": row["post_id"],
            "units": row["bounty_units"],
            "verifiers": quorum,
        },
        conn=conn,
    )
    return {
        "finding_id": finding_id,
        "paid": True,
        "payee_agent_id": payee,
        "units": row["bounty_units"],
        "verifiers": quorum,
    }


def refund_dying_finding_bounties(
    conn: sqlite3.Connection, finding_ids: list[int], dead: tuple[int, ...] = ()
) -> dict[int, int]:
    """Refund every funded-but-unpaid bounty on findings about to be
    deleted (proposal #710, phase 4).  Fund rows vanish with the
    finding (CASCADE) while escrow legs are immutable - without this
    the money strands and the conservation audit trips forever.
    Live funders are refunded; shares whose funder is already gone
    (dead, or passed in `dead`) and any orphaned remainder sweep to
    the treasury, so escrow always balances the recompute.  Returns
    {finding_id: refunded_units}."""
    from db._credits import escrow_to_treasury, release_escrow

    out: dict[int, int] = {}
    dead_set = set(dead)
    for chunk in _id_chunks(list(dict.fromkeys(finding_ids))):
        marks = ",".join("?" * len(chunk))
        doomed = [
            r
            for r in conn.execute(
                "SELECT f.id, f.pr_number, f.bounty_units FROM review_findings f"
                f" WHERE f.id IN ({marks}) AND f.bounty_units > 0"
                " AND NOT EXISTS (SELECT 1 FROM finding_payouts p"
                " WHERE p.finding_id = f.id)",
                chunk,
            ).fetchall()
        ]
        for f in doomed:
            refunded = 0
            for fr in conn.execute(
                "SELECT funder_agent_id, units FROM finding_bounty_funds"
                " WHERE finding_id = ?",
                (f["id"],),
            ).fetchall():
                if (
                    fr["funder_agent_id"] in dead_set
                    or conn.execute(
                        "SELECT id FROM agents WHERE id = ?", (fr["funder_agent_id"],)
                    ).fetchone()
                    is None
                ):
                    escrow_to_treasury(
                        fr["units"],
                        "finding_bounty_stranded",
                        target_type="pr",
                        target_id=f["pr_number"],
                        conn=conn,
                    )
                else:
                    release_escrow(
                        fr["funder_agent_id"],
                        fr["units"],
                        "finding_bounty_refund",
                        target_type="pr",
                        target_id=f["pr_number"],
                        conn=conn,
                    )
                refunded += fr["units"]
            if f["bounty_units"] > refunded:
                escrow_to_treasury(
                    f["bounty_units"] - refunded,
                    "finding_bounty_stranded",
                    target_type="pr",
                    target_id=f["pr_number"],
                    conn=conn,
                )
                refunded = f["bounty_units"]
            conn.execute(
                "UPDATE review_findings SET bounty_units = 0 WHERE id = ?",
                (f["id"],),
            )
            conn.execute(
                "DELETE FROM finding_bounty_funds WHERE finding_id = ?",
                (f["id"],),
            )
            log_event(
                EVT_FINDING_BOUNTY_UNFUNDED,
                actor_agent_id=None,
                target_type="pr",
                target_id=f["pr_number"],
                detail={"finding_id": f["id"], "units": refunded, "reason": "dying"},
                conn=conn,
            )
            out[f["id"]] = refunded
    return out


def findings_dying_for_agent(
    conn: sqlite3.Connection, agent_id: int, post_ids: list[int]
) -> list[int]:
    """Funded-unpaid finding ids dying with a citizen: their authored
    findings anywhere plus any findings on their deleted posts."""
    ids: list[int] = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM review_findings WHERE finder_agent_id = ?"
            " AND bounty_units > 0 AND NOT EXISTS"
            " (SELECT 1 FROM finding_payouts p"
            " WHERE p.finding_id = review_findings.id)",
            (agent_id,),
        ).fetchall()
    ]
    if post_ids:
        marks = ",".join("?" * len(post_ids))
        ids += [
            r[0]
            for r in conn.execute(
                "SELECT id FROM review_findings WHERE post_id IN"
                f" ({marks}) AND bounty_units > 0 AND NOT EXISTS"
                " (SELECT 1 FROM finding_payouts p"
                " WHERE p.finding_id = review_findings.id)",
                post_ids,
            ).fetchall()
        ]
    return ids


def finding_bounty_map(conn: sqlite3.Connection, pr_number: int) -> dict[int, dict]:
    """{finding_id: {bounty_units, paid_units, paid}} for one PR - the
    read-only money twin of finding_verdict, so the viewer panel and
    tools render bounty state without touching the ledger."""
    out: dict[int, dict] = {}
    for r in conn.execute(
        "SELECT f.id, f.bounty_units,"
        " COALESCE(p.units, 0) AS paid_units,"
        " CASE WHEN p.finding_id IS NULL THEN 0 ELSE 1 END AS paid"
        " FROM review_findings f LEFT JOIN finding_payouts p"
        " ON p.finding_id = f.id WHERE f.pr_number = ?",
        (pr_number,),
    ).fetchall():
        out[r["id"]] = {
            "bounty_units": r["bounty_units"],
            "paid_units": r["paid_units"],
            "paid": bool(r["paid"]),
        }
    return out


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
            f" COALESCE(SUM(CASE WHEN NOT ({_VERIFIED_SQL}) THEN 1 ELSE 0 END), 0)"
            " AS open_findings,"
            f" COALESCE(SUM(CASE WHEN {_VERIFIED_SQL} THEN 1 ELSE 0 END), 0)"
            " AS verified_findings,"
            " COALESCE(SUM(CASE WHEN auto_flip = 1"
            f" AND NOT ({_VERIFIED_SQL}) THEN 1 ELSE 0 END), 0)"
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
