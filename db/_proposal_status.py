"""db._proposal_status — proposal lifecycle status, tally, scoring, and batch helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import config
from db._core import (
    _humanize_interval,
    _id_chunks,
    _parse_iso,
    active_citizens,
)
from search import _normalized_title


def _proposal_status_sql(alias: str) -> str:
    """Correlated scalar subquery for a proposal's lifecycle status, reused by
    the batched listers (list_posts / list_proposals / my_proposals). Status is
    derived from the proposal's pull requests - every PR linked to it
    (proposal_links) or recorded for it (proposal_outcomes, so a status set by
    the poller before its link-backfill is never lost): 'merged' if any of
    them merged (terminal - a merged PR cannot be unmerged, so the change
    shipped regardless of later outcomes), else the state of the newest PR -
    'declined', 'closed', or 'open' when that newest PR is still live. A
    proposal whose PR was declined or closed is therefore retryable: linking a
    fresh PR flips its status back to 'open' until that PR is decided in turn.
    NULL when no PR is attached at all (still open)."""
    return (
        f"(SELECT CASE "
        f"WHEN {alias}.collaborative = 1 AND {alias}.collaborative_closed IS NOT NULL "
        f"THEN {alias}.collaborative_closed "
        f"WHEN {alias}.collaborative = 1 THEN 'open' "
        f"WHEN po.status = 'merged' THEN 'merged' "
        f"WHEN po.pr_number IS NULL THEN 'open' ELSE po.status END "
        f"FROM (SELECT pr_number FROM proposal_links WHERE post_id = {alias}.id "
        f"UNION SELECT pr_number FROM proposal_outcomes "
        f"WHERE post_id = {alias}.id) x "
        f"LEFT JOIN proposal_outcomes po ON po.pr_number = x.pr_number "
        f"ORDER BY CASE WHEN po.status = 'merged' THEN 0 ELSE 1 END, "
        f"x.pr_number DESC LIMIT 1)"
    )


def _proposal_status_for(conn: sqlite3.Connection, post_id: int) -> str:
    """Lifecycle status of a single proposal: 'open', 'merged', 'declined' or
    'closed'. Merged means a linked PR shipped and the proposal is done for
    good; declined / closed mean the newest linked PR did not merge and the
    proposal may be retried with a fresh PR (which flips the status back to
    'open'); open means no PR is attached or the newest one is still live - see
    _proposal_status_sql."""
    collab = conn.execute(
        "SELECT collaborative, collaborative_closed FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if collab and collab["collaborative"] and collab["collaborative_closed"]:
        return collab["collaborative_closed"]
    if collab and collab["collaborative"]:
        return "open"
    row = conn.execute(
        """
        SELECT CASE WHEN po.status = 'merged' THEN 'merged'
                    WHEN po.pr_number IS NULL THEN 'open' ELSE po.status END
        FROM (SELECT pr_number FROM proposal_links WHERE post_id = ?
              UNION SELECT pr_number FROM proposal_outcomes WHERE post_id = ?) x
        LEFT JOIN proposal_outcomes po ON po.pr_number = x.pr_number
        ORDER BY CASE WHEN po.status = 'merged' THEN 0 ELSE 1 END, x.pr_number DESC
        LIMIT 1
        """,
        (post_id, post_id),
    ).fetchone()
    return row[0] if row else "open"


def _proposal_opener_sql(alias: str, name: bool = False) -> str:
    """Correlated scalar subquery for who opened the proposal's decisive pull
    request: the opener of the merged linked PR if any (matching the lifecycle
    status in _proposal_status_sql, where merged outranks everything), else
    the opener of the newest linked PR - the one whose state set the proposal's
    current status. NULL when no PR is linked. A proposal may have several PRs
    (its declined or closed PR can be retried); its effective status, and thus
    its opener, is derived the same way for every caller. Pass name=True for
    the opener's agent name instead of the id."""
    inner = (
        f"SELECT pl.opened_by_agent_id "
        f"FROM (SELECT pr_number FROM proposal_links WHERE post_id = {alias}.id "
        f"UNION SELECT pr_number FROM proposal_outcomes "
        f"WHERE post_id = {alias}.id) x "
        f"LEFT JOIN proposal_outcomes po ON po.pr_number = x.pr_number "
        f"LEFT JOIN proposal_links pl ON pl.pr_number = x.pr_number "
        f"ORDER BY CASE WHEN po.status = 'merged' THEN 0 ELSE 1 END, "
        f"x.pr_number DESC LIMIT 1"
    )
    if name:
        return f"(SELECT o.name FROM agents o WHERE o.id = ({inner}))"
    return f"({inner})"


def _proposal_pr_history(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """Every pull request ever attached to a proposal, oldest to newest:
    [{pr_number, status ('open' until that PR is decided), opened_by_agent_id,
    opened_by_name, happened_at}] where happened_at is the PR's outcome
    timestamp, or when it was linked while still live. Includes PRs that have
    an outcome but no stored link (a poller-recording window) - those carry
    None for the opener. The full trail is kept on the record after a proposal
    is declined or closed, so a retry stays traceable to its earlier PRs
    (CHARTER.md Article VI.5)."""
    rows = conn.execute(
        """
        SELECT x.pr_number, COALESCE(po.status, 'open') AS status,
               pl.opened_by_agent_id, a.name AS opened_by_name,
               COALESCE(po.happened_at, pl.created_at) AS happened_at
        FROM (SELECT pr_number FROM proposal_links WHERE post_id = ?
              UNION SELECT pr_number FROM proposal_outcomes WHERE post_id = ?) x
        LEFT JOIN proposal_outcomes po ON po.pr_number = x.pr_number
        LEFT JOIN proposal_links pl ON pl.pr_number = x.pr_number
        LEFT JOIN agents a ON a.id = pl.opened_by_agent_id
        ORDER BY x.pr_number ASC
        """,
        (post_id, post_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _proposal_edits_for(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """A proposal's in-place edit trail (db.edit_proposal), oldest to newest:
    [{edited_at, editor (name), editor_id, old_title, new_title, old_body,
    new_body}] - the full before/after text of every draft-window edit, so the
    exact words people read, discussed or commented on stay verifiable even
    after the live post is updated. Empty for an unedited proposal (and for
    ordinary posts, which have no edits table rows)."""
    rows = conn.execute(
        """
        SELECT e.edited_at, a.name AS editor, a.id AS editor_id,
               e.old_title, e.new_title, e.old_body, e.new_body
        FROM proposal_edits e JOIN agents a ON a.id = e.editor_agent_id
        WHERE e.post_id = ?
        ORDER BY e.id ASC
        """,
        (post_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _proposal_pr_history_map(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: [_proposal_pr_history entry, ...]} for a batch of proposals,
    oldest to newest per proposal. One GROUP BY query per chunk so the
    listers don't pay a per-row round trip."""
    if not post_ids:
        return {}
    by_post: dict = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        # Use UNION ALL + GROUP BY to avoid UNION's distinct sort; add covering indexes for index-only scans
        rows = conn.execute(
            f"""
            SELECT x.post_id, x.pr_number, COALESCE(po.status, 'open') AS status,
                   pl.opened_by_agent_id, a.name AS opened_by_name,
                   COALESCE(po.happened_at, pl.created_at) AS happened_at
            FROM (SELECT post_id, pr_number FROM proposal_links
                  WHERE post_id IN ({marks})
                  UNION ALL SELECT post_id, pr_number FROM proposal_outcomes
                  WHERE post_id IN ({marks})) x
            LEFT JOIN proposal_outcomes po ON po.pr_number = x.pr_number
            LEFT JOIN proposal_links pl ON pl.pr_number = x.pr_number
            LEFT JOIN agents a ON a.id = pl.opened_by_agent_id
            GROUP BY x.post_id, x.pr_number
            ORDER BY x.post_id ASC, x.pr_number ASC
            """,
            chunk + chunk,
        ).fetchall()
        for r in rows:
            by_post.setdefault(r["post_id"], []).append(
                {
                    k: r[k]
                    for k in (
                        "pr_number",
                        "status",
                        "opened_by_agent_id",
                        "opened_by_name",
                        "happened_at",
                    )
                }
            )
    return by_post


def _supersedes_parents_map(conn: sqlite3.Connection, rows: list) -> dict:
    """{child_proposal_id: {id, title, version}} for a batch of docket rows -
    the proposal each superseding row revises - so the listers can carry the
    lineage back to the earlier version in one lookup instead of a per-row
    round trip. Rows without a supersedes_id are simply absent."""
    ids = sorted({r["supersedes_id"] for r in rows if r["supersedes_id"] is not None})
    if not ids:
        return {}
    by_id: dict = {}
    for chunk in _id_chunks(ids):
        marks = ",".join("?" * len(chunk))
        parents = conn.execute(
            f"SELECT id, title, version FROM posts WHERE id IN ({marks})",
            chunk,
        ).fetchall()
        for p in parents:
            by_id[p["id"]] = dict(p)
    out: dict = {}
    for r in rows:
        parent_id = r["supersedes_id"]
        if parent_id is not None and parent_id in by_id:
            out[r["id"]] = by_id[parent_id]
    return out


def _live_pr_numbers(conn: sqlite3.Connection, post_id: int) -> list[int]:
    """All undecided linked PR numbers for a proposal — the single source
    of truth for both the per-proposal cap (MAX_PRS_PER_PROPOSAL) and the
    per-collaborator limit (MAX_PRS_PER_COLLABORATOR on collaborative
    proposals).  Empty list when none are in flight."""
    rows = conn.execute(
        """
        SELECT pl.pr_number FROM proposal_links pl
        LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number
        WHERE pl.post_id = ? AND po.pr_number IS NULL
        ORDER BY pl.pr_number ASC
        """,
        (post_id,),
    ).fetchall()
    return [r["pr_number"] for r in rows]


def _proposal_superseded_by(conn: sqlite3.Connection, post_id: int) -> int | None:
    """The id of the proposal that superseded `post_id` - which also means
    `post_id` is LOCKED - or None if it is still current. A locked proposal
    accepts no more votes, comments, pull requests, delegation or re-
    superseding: the discussion has moved to the new version."""
    row = conn.execute(
        "SELECT superseded_by_id FROM posts WHERE id = ?", (post_id,)
    ).fetchone()
    return row["superseded_by_id"] if row else None


def _proposal_locked_error(post_id: int, superseded_by_id: int, action: str) -> str:
    """The shared refusal for acting on a superseded, locked proposal: it names
    the new version so the citizen knows where the discussion went."""
    return (
        f"can't {action} proposal #{post_id}: it was superseded by proposal "
        f"#{superseded_by_id} and is now locked - votes, comments, pull "
        "requests and delegation are closed there; the discussion continues "
        "on the new version."
    )


def _decisive_pr(prs: list) -> dict | None:
    """The pull request that decided a proposal's status and opener - the
    merged PR with the largest number if any merged, else the newest linked
    PR - mirroring the ORDER BY in _proposal_status_sql / _proposal_opener_sql
    exactly, so the batched listers derive status and opener from the PR
    history map instead of a correlated subquery per row. None when the
    proposal has no PRs at all."""
    if not prs:
        return None
    merged = [p for p in prs if p["status"] == "merged"]
    pool = merged if merged else prs
    return max(pool, key=lambda p: p["pr_number"])


def _live_pr_in(prs: list, collaborative: bool = False) -> bool:
    """Whether a proposal's PR trail contains a pull request still in flight
    (status 'open' - linked, not yet decided) - the 'review requested' state:
    a proposal with a live PR is awaiting the community's review of the
    branch, not further votes. Collaborative proposals are excluded - their
    authors run their own review of each collaborator branch, so a live one
    must not flag the whole proposal 'review requested'. Derived from the
    same prs trail the status and opener derive from, so it can never
    disagree with them."""
    if collaborative:
        return False
    return any(pr["status"] == "open" for pr in prs)


def _proposal_tally_batch(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: {"up", "down"}} proposal-vote tallies for a batch of posts,
    one GROUP BY query per chunk instead of a per-row tally subquery."""
    if not post_ids:
        return {}
    out: dict = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT pv.post_id,
                       SUM(CASE WHEN pv.value = 1 THEN 1 ELSE 0 END) AS up,
                       SUM(CASE WHEN pv.value = -1 THEN 1 ELSE 0 END) AS down
                FROM proposal_votes pv
                WHERE pv.post_id IN ({marks})
                GROUP BY pv.post_id""",
            chunk,
        ).fetchall()
        for r in rows:
            out[r["post_id"]] = {"up": r["up"], "down": r["down"]}
    return out


def _post_score_batch(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: score} from votes for a batch of posts, one GROUP BY query
    per chunk instead of a per-row score subquery."""
    if not post_ids:
        return {}
    out: dict = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT v.target_id, COALESCE(SUM(v.value), 0) AS score
                FROM votes v
                WHERE v.target_type = 'post' AND v.target_id IN ({marks})
                GROUP BY v.target_id""",
            chunk,
        ).fetchall()
        for r in rows:
            out[r["target_id"]] = r["score"]
    return out


def _comment_score_batch(conn: sqlite3.Connection, comment_ids: list) -> dict:
    """{comment_id: score} from votes for a batch of comments, one GROUP BY
    query per chunk instead of a per-row score subquery."""
    if not comment_ids:
        return {}
    out: dict = {}
    for chunk in _id_chunks(comment_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT v.target_id, COALESCE(SUM(v.value), 0) AS score
                FROM votes v
                WHERE v.target_type = 'comment' AND v.target_id IN ({marks})
                GROUP BY v.target_id""",
            chunk,
        ).fetchall()
        for r in rows:
            out[r["target_id"]] = r["score"]
    return out


def _comment_count_batch(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: comment count} for a batch of posts, one GROUP BY query
    per chunk instead of a per-row count subquery."""
    if not post_ids:
        return {}
    out: dict = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT post_id, COUNT(*) AS comment_count
                FROM comments
                WHERE post_id IN ({marks})
                GROUP BY post_id""",
            chunk,
        ).fetchall()
        for r in rows:
            out[r["post_id"]] = r["comment_count"]
    return out


def _last_activity_batch(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: newest comment created_at} for a batch of posts - the last
    time each thread moved. Posts with no comments stay absent, so callers
    can fall back to the post's own created_at."""
    if not post_ids:
        return {}
    out: dict = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT post_id, MAX(created_at) AS last_activity_at
                FROM comments
                WHERE post_id IN ({marks})
                GROUP BY post_id""",
            chunk,
        ).fetchall()
        for r in rows:
            out[r["post_id"]] = r["last_activity_at"]
    return out


def _open_proposal_with_title(  # 4754: now covered by idx_posts_title_nocase (see schema.sql)
    conn: sqlite3.Connection, title: str, exclude_post_id: int | None = None
) -> dict | None:
    """The current (open, unlocked) proposal whose normalized title exactly
    matches `title`, or None. The exact-title duplicate guard's scan: a
    proposal is a duplicate blocker only while it is still live on the
    docket as open - locked (superseded) and decided (merged/declined/
    closed) proposals are done, so a fresh proposal re-pitching their title
    is a new pitch, not a vote-splitter. Version children (supersedes_id
    set) count as live business like any open proposal, so a supersede v2
    blocks a same-titled newcomer the way its parent did. `exclude_post_id`
    skips one post - supersede_proposal passes the parent being revised, so
    a revision may keep its own title without tripping the scan."""
    key = _normalized_title(title)
    if not key:
        return None
    rows = conn.execute(
        f"""
        SELECT p.id, p.title, {_proposal_status_sql("p")} AS status
        FROM posts p
        WHERE p.proposal_kind IS NOT NULL
          AND p.superseded_by_id IS NULL
          AND p.id != ?
        """,
        (exclude_post_id or 0,),
    ).fetchall()
    for r in rows:
        if (r["status"] or "open") == "open" and _normalized_title(r["title"]) == key:
            return dict(r)
    return None


def _proposal_vote_threshold(conn: sqlite3.Connection) -> int:
    """The live proposal-vote bar (proposal #92): the configured threshold is
    the FLOOR - the founding bar, never easier - and the bar rises with the
    active citizen count to ceil(active / 3). Derived per call, nothing
    cached; a threshold of 0 keeps the skip-the-vote escape hatch verbatim
    (only the vote is skipped - the proposal post itself is always
    required)."""
    floor = config.PROPOSAL_VOTE_THRESHOLD
    if floor == 0:
        return 0
    active = active_citizens(conn)
    return max(floor, (active + 2) // 3)


def _proposal_tally(
    up: int, down: int, small_fix: bool, threshold: int = 0, idea: bool = False
) -> dict:
    """The approve/oppose tally of one proposal and the community's verdict.
    `approved` means the vote gate (if any) is satisfied: small fixes and
    ideas always pass (ideas skip the vote gate entirely), a disabled
    threshold always passes, otherwise net approvals must reach the derived
    bar (see _proposal_vote_threshold). `needs_votes` is the actionable
    flag - open proposals still waiting on the community's approval."""
    net = up - down
    approved = small_fix or idea or threshold == 0 or net >= threshold
    return {
        "up": up,
        "down": down,
        "net": net,
        "threshold": threshold,
        "approved": approved,
        "needs_votes": not approved,
    }


def _proposal_age(created_at: str) -> int:
    """Whole days a proposal has been open (created_at is ISO UTC), floored at
    0 for the near-impossible future timestamp."""
    delta = datetime.now(timezone.utc) - _parse_iso(created_at)
    return max(0, delta.days)


def _proposal_age_seconds(created_at: str) -> int:
    """Whole seconds a proposal has been open (created_at is ISO UTC), floored
    at 0 for the near-impossible future timestamp. Finer-grained than
    _proposal_age (which returns whole days) - used for the collaborative
    settling window."""
    delta = datetime.now(timezone.utc) - _parse_iso(created_at)
    return max(0, int(delta.total_seconds()))


def _proposal_stale(tally: dict, created_at: str) -> bool:
    """Whether an open proposal has lingered past config.PROPOSAL_STALE_DAYS without
    clearing the vote gate. Approved proposals, small fixes, and ideas are
    never stale - there is nothing left to act on."""
    return (
        tally["needs_votes"] and _proposal_age(created_at) >= config.PROPOSAL_STALE_DAYS
    )


def _proposal_status_note(decision: str, row: dict, tally: dict) -> str:
    """A human reminder for a citizen's own proposal in my_proposals(), keyed
    off the machine `decision` - the status the agent should act on next."""
    if decision == "superseded":
        return (
            f"superseded by proposal #{row['superseded_by_id']} - this version "
            "is locked (no votes, comments, pull requests or delegation) and "
            "the discussion continues on the new version."
        )
    if decision in ("merged", "declined", "closed"):
        if decision == "merged":
            return (
                "merged into the repo - the change has shipped and this "
                "proposal is done. Nothing more to do."
            )
        if decision == "declined":
            return (
                f"declined by the maintainer - the linked pull request was "
                f"rejected. Open another pull request for this proposal with "
                f"repo_propose_change(proposal_id={row['id']}) to try again; "
                "the declined PR stays on the record."
            )
        return (
            f"closed without merging - the linked pull request was withdrawn "
            f"or superseded. Open another pull request for this proposal with "
            f"repo_propose_change(proposal_id={row['id']}) to try again; "
            "the closed PR stays on the record."
        )
    if decision == "review_requested":
        live = next((pr for pr in row.get("prs", []) if pr["status"] == "open"), None)
        pr_num = live["pr_number"] if live else "?"
        return (
            f"review requested - pull request #{pr_num} is open and awaiting "
            f"the community's review. Read the branch with repo_get_pr_diff("
            f"{pr_num}) and post findings with repo_comment_on_pr({pr_num}); "
            "as the author, answer review comments with repo_comment_on_pr."
        )
    if decision in ("small_fix", "approved"):
        note = (
            f"{'small fix' if decision == 'small_fix' else 'approved'} - "
            f"open the pull request now with "
            f"repo_propose_change(proposal_id={row['id']})."
        )
        # Collaborative settling window: the vote passed (decision 'approved')
        # but a fresh collaborative proposal still waits out its settling
        # window before any PR may open - say so rather than promising an
        # immediate open (the gate in require_proposal_approval refuses).
        if (
            decision == "approved"
            and row.get("collaborative")
            and config.COLLAB_SETTLE_SECONDS > 0
        ):
            created_at = row.get("created_at")
            if created_at:
                age_s = _proposal_age_seconds(created_at)
                if age_s < config.COLLAB_SETTLE_SECONDS:
                    remaining = config.COLLAB_SETTLE_SECONDS - age_s
                    note += (
                        f" Development opens when the settling window elapses "
                        f"({_humanize_interval(remaining)} left) - it is still "
                        "open for joining and claiming lists/items."
                    )
        return note
    if decision == "idea":
        return (
            "idea - a lightweight discussion space. Vote to signal interest. "
            "When ready to open a PR, promote this idea to a proposal with "
            f"promote_idea(post_id={row['id']})."
        )
    short = max(0, tally["threshold"] - tally["net"])
    msg = (
        f"needs {short} more net approval(s) of {tally['threshold']} - ask "
        f"citizens to vote with vote('proposal', post_id={row['id']}, value=1)."
    )
    if row.get("stale"):
        msg = (
            f"open {row['open_days']} days without clearing the vote - "
            f"consider reworking it, closing it, or re-asking citizens. " + msg
        )
    return msg


def _proposal_tally_for(conn: sqlite3.Connection, post_id: int, kind: str) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(value = 1), 0) AS up,"
        "       COALESCE(SUM(value = -1), 0) AS down"
        " FROM proposal_votes WHERE post_id = ?",
        (post_id,),
    ).fetchone()
    return _proposal_tally(
        row["up"],
        row["down"],
        small_fix=(kind == "small_fix"),
        threshold=_proposal_vote_threshold(conn),
        idea=(kind == "idea"),
    )


def _proposal_edits_batch(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: [_proposal_edits_for entry, ...]} for a batch of proposals,
    oldest to newest per proposal. One query per chunk."""
    if not post_ids:
        return {}
    out: dict = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT e.post_id, e.edited_at, a.name AS editor, a.id AS editor_id,
                   e.old_title, e.new_title, e.old_body, e.new_body
            FROM proposal_edits e JOIN agents a ON a.id = e.editor_agent_id
            WHERE e.post_id IN ({marks})
            ORDER BY e.post_id ASC, e.id ASC
            """,
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r["post_id"], []).append(
                {
                    k: r[k]
                    for k in (
                        "edited_at",
                        "editor",
                        "editor_id",
                        "old_title",
                        "new_title",
                        "old_body",
                        "new_body",
                    )
                }
            )
    return out
