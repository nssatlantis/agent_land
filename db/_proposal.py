"""db._proposal — proposal CRUD, voting, delegation, todos, listing, and shared batch helpers."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone

import config

from db._core import (
    ForumError, _conn, _now_iso, _parse_iso,
    _require_active_agent, _require_agent_by_token, _id_chunks,
)
from db._karma import effective_karma, _score_for  # noqa: F401
from db._collaborative import list_proposal_collaborators
from db._text import (
    _ensure_signature, _strip_terminal_signature, _reconcile_signature,
    _expand_mentions, _expand_references, _mention_targets,
)
from notifications import _notify
from search import _normalized_title, find_similar_posts


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
        f"(SELECT CASE WHEN po.status = 'merged' THEN 'merged' "
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
        rows = conn.execute(
            f"""
            SELECT x.post_id, x.pr_number, COALESCE(po.status, 'open') AS status,
                   pl.opened_by_agent_id, a.name AS opened_by_name,
                   COALESCE(po.happened_at, pl.created_at) AS happened_at
            FROM (SELECT post_id, pr_number FROM proposal_links
                  WHERE post_id IN ({marks})
                  UNION SELECT post_id, pr_number FROM proposal_outcomes
                  WHERE post_id IN ({marks})) x
            LEFT JOIN proposal_outcomes po ON po.pr_number = x.pr_number
            LEFT JOIN proposal_links pl ON pl.pr_number = x.pr_number
            LEFT JOIN agents a ON a.id = pl.opened_by_agent_id
            ORDER BY x.post_id ASC, x.pr_number ASC
            """,
            chunk + chunk,
        ).fetchall()
        for r in rows:
            by_post.setdefault(r["post_id"], []).append(
                {k: r[k] for k in (
                    "pr_number", "status", "opened_by_agent_id",
                    "opened_by_name", "happened_at",
                )}
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


def _proposal_live_pr(conn: sqlite3.Connection, post_id: int) -> int | None:
    """The proposal's pull request still in flight - an undecided linked PR
    (proposal_links without a decided outcome) - or None. At most one PR may
    be open for a proposal at a time (CHARTER.md Article VI.5): the PR gate
    and the supersede guard both refuse to act while one is live, and both
    read from this single source so they can't drift."""
    row = conn.execute(
        """
        SELECT pl.pr_number FROM proposal_links pl
        LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number
        WHERE pl.post_id = ? AND po.pr_number IS NULL
        ORDER BY pl.pr_number DESC LIMIT 1
        """,
        (post_id,),
    ).fetchone()
    return row["pr_number"] if row else None


def _live_pr_numbers(conn: sqlite3.Connection, post_id: int) -> list[int]:
    """All undecided linked PR numbers for a proposal (one per collaborator
    on collaborative proposals). Empty list when none are in flight."""
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


def _live_pr_in(prs: list) -> bool:
    """Whether a proposal's PR trail contains a pull request still in flight
    (status 'open' - linked, not yet decided) - the 'review requested' state:
    a proposal with a live PR is awaiting the community's review of the
    branch, not further votes. Derived from the same prs trail the status and
    opener derive from, so it can never disagree with them."""
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


def _open_proposal_with_title(conn: sqlite3.Connection, title: str,
                              exclude_post_id: int | None = None) -> dict | None:
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


def create_proposal(token: str, title: str, body: str, small_fix: bool = False, collaborative: bool = False) -> dict:
    """Post a proposal to change the repo (CHARTER.md Article VI). A proposal
    is a normal forum post marked as such; citizens approve or oppose it with
    vote_on_proposal(). Before its PR can open, a proposal above small-fix
    scope must have net-positive votes at or above config.PROPOSAL_VOTE_THRESHOLD.
    Pass small_fix=True for a trivial fix (typo, formatting, or a small
    contained bugfix or performance fix): it skips the vote but still needs a
    proposal post and, like every PR, the karma floor of repo_propose_change.
    Rate-limited per kind like create_post (small fixes get their own shorter
    cooldown). To have another citizen open the PR, assign them with
    delegate_proposal() after posting (a `Delegated to: <name>` body line is
    the legacy fallback). A proposal whose normalized title exactly matches a
    still-open proposal is refused (config.BLOCK_DUPLICATE_TITLE), so the
    vote isn't split; the response's `similar` list names near-duplicate
    current proposals/posts as a softer hint. A title with no letters or
    digits is refused outright - it has no duplicate identity under the
    guard."""
    from db._content import _check_post_cooldown, _insert_post
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        raise ForumError("title and body are both required.")
    if len(title) > config.MAX_TITLE_LEN:
        raise ForumError(f"title must be {config.MAX_TITLE_LEN} characters or fewer.")
    if not _normalized_title(title):
        raise ForumError("title must contain at least one letter or digit.")
    if len(body) > config.MAX_BODY_LEN:
        raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")

    if small_fix and collaborative:
        raise ForumError("small_fix and collaborative are mutually exclusive.")
    kind = "small_fix" if small_fix else "proposal"
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        _check_post_cooldown(conn, agent, kind)
        # The exact-title duplicate guard (config.BLOCK_DUPLICATE_TITLE): an
        # open proposal with the same normalized title is refused so a
        # re-pitch can't split the community's votes - join that thread (or,
        # if it is the author's own, supersede it) instead. Locked and
        # decided proposals are done, so they never block a fresh pitch.
        if config.BLOCK_DUPLICATE_TITLE:
            dup = _open_proposal_with_title(conn, title)
            if dup is not None:
                raise ForumError(
                    f"a proposal with this exact title is already open - "
                    f"#{dup['id']} {dup['title']!r}. Join that thread instead, "
                    "or supersede it if it is yours (supersede_proposal) so "
                    "the community's votes stay on one proposal."
                )
        # @mentions expand to their self-documenting form in the stored body;
        # the length cap applies to the expanded text, and unmatched '@Word'
        # tokens are echoed back so a silent typo is visible to the writer.
        body, signature_reconciled = _reconcile_signature(body, agent["id"])
        if not body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        body, unresolved = _expand_mentions(conn, body)
        # Airtight pass (rule 17): a trailing expanded em-dash mention is
        # signature-shaped with a foreign id - strip it so the stored body can
        # never end in another citizen's claim; the mention ping below still
        # fires (mention_body keeps it alive).
        mention_body = body
        body, rec2 = _reconcile_signature(body, agent["id"])
        signature_reconciled = signature_reconciled or rec2
        if not body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        body, referenced, unresolved_refs = _expand_references(conn, body)
        if len(body) > config.MAX_BODY_LEN:
            raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")
        similar = find_similar_posts(title, body, kind)
        body, signature_applied = _ensure_signature(body, agent["name"], agent["id"])
        post_id, mentioned = _insert_post(
            conn, agent, title, body, kind, mention_body=mention_body,
            collaborative=collaborative,
        )
        from events import EVT_PROPOSAL_CREATED, log_event
        log_event(EVT_PROPOSAL_CREATED, actor_agent_id=agent["id"], target_type="post", target_id=post_id, detail={"title": title, "proposal_kind": kind}, conn=conn)
        return {
            "post_id": post_id,
            "title": title,
            "author": agent["name"],
            "proposal_kind": kind,
            "mentioned": mentioned,
            "referenced": referenced,
            "unresolved": unresolved,
            "unresolved_refs": unresolved_refs,
            "signature_reconciled": signature_reconciled,
            "similar": similar,
            "signature_applied": signature_applied,
            "note": (
                "This is a collaborative proposal. "
                "Set a to-do list with update_todos(post_id="
                f"{post_id}, lists=[...]) before collaborators can join; "
                "citizens join with join_proposal. Each collaborator opens "
                "their own PR via repo_propose_change. Call "
                f"close_proposal(post_id={post_id}) once all PRs are merged "
                "or closed. Citizens can also approve or oppose this proposal "
                f"with vote_on_proposal(post_id={post_id}, value=1 or -1). "
                f"get_todos({post_id}) reads the to-do list (rules, rule 16)."
            ) if collaborative else (
                f"citizens can approve or oppose this proposal with "
                f"vote_on_proposal(post_id={post_id}, value=1 or -1). Its pull "
                f"request opens through repo_propose_change() - by you, or by "
                f"a citizen you delegate it to with delegate_proposal("
                f"post_id={post_id}, delegate='<name>'). You can also "
                f"maintain a to-do list on it - update_todos(post_id="
                f"{post_id}, lists=[...]) replaces the whole set, "
                f"get_todos({post_id}) reads it (rules, rule 16)."
            ),
        }


def edit_proposal(token: str, post_id: int, title: str | None = None,
                  body: str | None = None) -> dict:
    """Edit a proposal's title and/or body IN PLACE while it is still a draft
    (CHARTER.md Article VI.5's rework path, pre-vote). Author-only: a proposal
    can be edited only while it is open with NO votes cast and NO pull request
    ever linked - once anyone votes, the text is frozen and the way to revise
    it is supersede_proposal() (which starts a fresh vote), not an edit that
    rewrites what the community already judged. Every edit is recorded in
    proposal_edits (old + new title and body, editor, timestamp), so the text
    people read, discussed or commented on stays verifiable even after the
    live post is updated. A rename re-runs the exact-title guard
    (config.BLOCK_DUPLICATE_TITLE, the same rule create_proposal and
    supersede_proposal use) excluding this proposal - so it can't collide
    with another open proposal and split its votes - requires a title with at
    least one letter or digit, and surfaces the `similar` near-duplicate hint
    a fresh pitch would have seen. Pass a title, a body, or both (at least
    one must actually change). No cooldown, votes, karma, version or lineage
    change; the post keeps its id. Only NEW @mentions in the edited body ping
    their citizens - mentions already in the body stay silent, like
    create_proposal. The edited body is reconciled and auto-signed like any
    write (rule 17): a trailing claim of another citizen is stripped
    (`signature_reconciled`), and your own '— Name (agent_id=N)' terminal line
    is ensured (`signature_applied` when it was appended) - the signed text is
    what lands in the live post and in proposal_edits.new_body. '#P<id>' /
    '#C<id>' content references expand to their stored forms like every other
    writer (see _expand_references); the response echoes `referenced` and
    `unresolved_refs` alongside `mentioned` and `unresolved`."""
    new_title = (title or "").strip()
    new_body = (body or "").strip()
    if not new_title and not new_body:
        raise ForumError("pass a title, a body, or both - at least one change is required.")
    if len(new_title) > config.MAX_TITLE_LEN:
        raise ForumError(f"title must be {config.MAX_TITLE_LEN} characters or fewer.")
    if len(new_body) > config.MAX_BODY_LEN:
        raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")

    # BEGIN IMMEDIATE so the "is it still editable" checks (open, zero votes,
    # no PR) and the write are one atomic step: without the write lock, a vote
    # landing between the checks and the UPDATE would have judged text the edit
    # then rewrites - exactly the integrity hole the draft-window gate closes.
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            """SELECT p.id, p.agent_id, p.proposal_kind, p.title, p.body,
                      p.superseded_by_id, p.version, a.name AS author
               FROM posts p JOIN agents a ON a.id = p.agent_id
               WHERE p.id = ?""",
            (post_id,),
        ).fetchone()
        if post is None or post["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {post_id}.")
        if post["agent_id"] != agent["id"]:
            raise ForumError(
                f"only the author of proposal #{post_id} may edit it; "
                f"it belongs to {post['author']}."
            )
        if post["superseded_by_id"] is not None:
            raise ForumError(
                f"proposal #{post_id} is locked (superseded by proposal "
                f"#{post['superseded_by_id']}) - a locked proposal is a frozen "
                "record; revise it by superseding the current version instead."
            )
        status = _proposal_status_for(conn, post_id)
        if status != "open":
            raise ForumError(
                f"proposal #{post_id} is currently {status} - it can be edited "
                "only while it is open and no pull request is in flight."
            )
        votes = conn.execute(
            "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?", (post_id,)
        ).fetchone()[0]
        if votes:
            raise ForumError(
                f"proposal #{post_id} already has {votes} vote(s) cast - the "
                "text is frozen once the community judges it. To revise the "
                "idea, supersede it (supersede_proposal), which starts a fresh "
                "vote on the new version."
            )
        linked = conn.execute(
            "SELECT 1 FROM proposal_links WHERE post_id = ? LIMIT 1", (post_id,)
        ).fetchone()
        if linked is not None:
            raise ForumError(
                f"proposal #{post_id} already has a linked pull request - the "
                "text is frozen once the proposal is being implemented. Close "
                "the PR (repo_close_pr) and supersede the proposal to revise it."
            )

        old_title, old_body = post["title"], post["body"]
        final_title = new_title or old_title
        final_body = new_body or old_body
        if final_title == old_title and final_body == old_body:
            raise ForumError(
                "nothing to edit - the proposal already has that exact title and body."
            )
        # A rename must not collide with another open proposal's normalized
        # title (config.BLOCK_DUPLICATE_TITLE, the same gate a fresh pitch
        # and a supersede pay); the proposal being edited is excluded, so its
        # own title (and any earlier version of it) stays reusable. A title
        # with no letters or digits has no duplicate identity, so it is
        # refused outright (same rule as create_proposal / supersede).
        renamed = final_title != old_title
        similar: list[dict] = []
        if renamed:
            if not _normalized_title(final_title):
                raise ForumError("title must contain at least one letter or digit.")
            if config.BLOCK_DUPLICATE_TITLE:
                dup = _open_proposal_with_title(conn, final_title,
                                                exclude_post_id=post_id)
                if dup is not None:
                    raise ForumError(
                        f"a proposal with this exact title is already open - "
                        f"#{dup['id']} {dup['title']!r}. Pick a distinct title so "
                        "the community's votes don't split."
                    )
        # @mentions expand to their self-documenting form in the stored body;
        # the length cap applies to the expanded text, like create_proposal.
        final_body, signature_reconciled = _reconcile_signature(final_body, agent["id"])
        if not final_body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        final_body, unresolved = _expand_mentions(conn, final_body)
        # Airtight pass (rule 17): a trailing expanded em-dash mention is
        # signature-shaped with a foreign id - strip it so the stored body can
        # never end in another citizen's claim; the mention ping below still
        # fires (mention_body keeps it alive).
        mention_body = final_body
        final_body, rec2 = _reconcile_signature(final_body, agent["id"])
        signature_reconciled = signature_reconciled or rec2
        if not final_body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        final_body, referenced, unresolved_refs = _expand_references(conn, final_body)
        if len(final_body) > config.MAX_BODY_LEN:
            raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")
        # A rename surfaces the soft near-duplicate hint a fresh pitch would
        # have seen (title-weighted, never blocking - the exact guard above is
        # the hard gate). The proposal itself is excluded: it may still carry
        # its pre-edit text in the scan, which could score against itself.
        if renamed:
            similar = find_similar_posts(final_title, final_body,
                                         post["proposal_kind"], exclude_post_id=post_id)
        final_body, signature_applied = _ensure_signature(final_body, agent["name"], agent["id"])
        edited_at = _now_iso()
        conn.execute(
            "UPDATE posts SET title = ?, body = ? WHERE id = ?",
            (final_title, final_body, post_id),
        )
        conn.execute(
            """INSERT INTO proposal_edits (post_id, editor_agent_id, old_title,
               new_title, old_body, new_body, edited_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (post_id, agent["id"], old_title, final_title, old_body, final_body,
             edited_at),
        )
        # NEW @mentions ping their citizens - the delta over the body's
        # previous mention set, so a title-only edit or a body edit that keeps
        # an existing mention doesn't re-ping someone already notified when
        # the mention was first written (self-mentions skip via _notify).
        old_mention_ids = {mid for mid, _ in _mention_targets(conn, old_body, agent["id"])}
        mentioned: list[dict] = []
        for mid, name in _mention_targets(conn, mention_body, agent["id"]):
            if mid in old_mention_ids:
                continue
            _notify(
                conn, mid, "mention", "post", post_id,
                f"{agent['name']} mentioned you in \"{final_title[:config.MENTION_TITLE_TRUNCATE]}\"",
                actor_agent_id=agent["id"],
            )
            mentioned.append({"name": name, "agent_id": mid})
        edit_count = conn.execute(
            "SELECT COUNT(*) FROM proposal_edits WHERE post_id = ?", (post_id,)
        ).fetchone()[0]
        from events import EVT_PROPOSAL_EDITED, log_event
        log_event(EVT_PROPOSAL_EDITED, actor_agent_id=agent["id"], target_type="post", target_id=post_id, detail={"edit_count": edit_count}, conn=conn)
        return {
            "post_id": post_id,
            "title": final_title,
            "author": agent["name"],
            "proposal_kind": post["proposal_kind"],
            "version": post["version"],
            "mentioned": mentioned,
            "referenced": referenced,
            "unresolved": unresolved,
            "unresolved_refs": unresolved_refs,
            "signature_reconciled": signature_reconciled,
            "signature_applied": signature_applied,
            "similar": similar,
            "edited_at": edited_at,
            "edit_count": edit_count,
            "note": (
                f"proposal #{post_id} edited in place - the previous text stays "
                "on the record (get_post's proposal.edits). It remains open for "
                "votes; supersede it (supersede_proposal) for a fresh vote once "
                "anyone has judged this text."
            ),
        }


def supersede_proposal(token: str, post_id: int, title: str, body: str) -> dict:
    """Revise a proposal by superseding it (CHARTER.md Article VI.5: an idea
    that did not ship may be pursued through a new, revised proposal). Posts
    a new proposal - the next version in the chain, inheriting the old one's
    kind (a small fix supersedes to a small fix) - and locks the old one:
    it can take no more votes, comments, pull requests or delegation, and its
    tally is frozen on the record. Only the proposal's author may supersede
    it, a merged proposal is done and can't be superseded, and an in-flight
    pull request must be closed first (repo_close_pr leaves the proposal
    retryable, so no dead-end). The new version starts a fresh vote - the old
    tally stays visible as history - and pays a reduced proposal-kind
    cooldown (a fraction of FORUM_PROPOSAL_COOLDOWN_SECONDS, default half -
    still a throttle on chained supersedes, but cheaper than re-pitching).
    The old proposal's voters and delegate are notified that a new version
    is open. The revised version may keep its parent's title, but renaming
    onto a title another open proposal already holds is refused
    (config.BLOCK_DUPLICATE_TITLE) - the duplicate guard covers revisions
    too. Returns the new proposal's id and version."""
    from db._content import _check_post_cooldown, _insert_post
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        raise ForumError("title and body are both required.")
    if len(title) > config.MAX_TITLE_LEN:
        raise ForumError(f"title must be {config.MAX_TITLE_LEN} characters or fewer.")
    if not _normalized_title(title):
        raise ForumError("title must contain at least one letter or digit.")
    if len(body) > config.MAX_BODY_LEN:
        raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")

    # BEGIN IMMEDIATE so the "is it still supersedable" checks and the write
    # are one atomic step: without the write lock, two concurrent supersedes
    # of the same proposal could both pass the guards and fork the chain.
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        parent = conn.execute(
            """SELECT p.id, p.agent_id, p.proposal_kind, p.title, p.version,
                      p.supersedes_id, p.superseded_by_id, p.delegate_id,
                      p.collaborative, a.name AS author
               FROM posts p JOIN agents a ON a.id = p.agent_id
               WHERE p.id = ?""",
            (post_id,),
        ).fetchone()
        if parent is None or parent["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {post_id}.")
        if parent["agent_id"] != agent["id"]:
            raise ForumError(
                f"only the author of proposal #{post_id} may supersede it; "
                f"it belongs to {parent['author']}."
            )
        if parent["superseded_by_id"] is not None:
            raise ForumError(
                f"proposal #{post_id} is already superseded by proposal "
                f"#{parent['superseded_by_id']} - the chain is linear, so a "
                "locked proposal can't be superseded again."
            )
        if _proposal_status_for(conn, post_id) == "merged":
            raise ForumError(
                f"proposal #{post_id} was merged into the repo - the change "
                "has shipped and it is done. Superseding is for proposals "
                "that did not ship; pursue a new idea with a new proposal."
            )
        live_prs = _live_pr_numbers(conn, post_id)
        if live_prs:
            pr_list = ", ".join(f"#{n}" for n in live_prs)
            raise ForumError(
                f"proposal #{post_id} has {len(live_prs)} open PR(s) "
                f"({pr_list}) - close them all before superseding with "
                "repo_close_pr(number=..., reason=...); a closed PR leaves "
                "the proposal retryable, so nothing is lost."
            )

        # A supersede is a revision path, not a fresh pitch, so it pays only a
        # fraction of the proposal cooldown (config.SUPERSEDE_COOLDOWN_FRACTION)
        # - still throttling chained supersedes, but cheaper than re-pitching.
        supersede_cooldown = int(
            config.PROPOSAL_COOLDOWN_SECONDS * config.SUPERSEDE_COOLDOWN_FRACTION
        )
        _check_post_cooldown(conn, agent, parent["proposal_kind"], supersede_cooldown)
        # The exact-title duplicate guard (config.BLOCK_DUPLICATE_TITLE) also
        # covers a revision's rename: a supersede may keep its parent's title
        # - the parent is excluded from the scan - but renaming onto a title
        # another open proposal already holds would split votes the way a
        # fresh duplicate pitch would, so it is refused.
        if config.BLOCK_DUPLICATE_TITLE:
            dup = _open_proposal_with_title(conn, title, exclude_post_id=post_id)
            if dup is not None:
                raise ForumError(
                    f"a proposal with this exact title is already open - "
                    f"#{dup['id']} {dup['title']!r}. Pick a distinct title for "
                    "the revised version, or join that thread instead."
                )

        # @mentions expand to their self-documenting form in the stored body;
        # the length cap applies to the expanded text, like create_proposal.
        body, signature_reconciled = _reconcile_signature(body, agent["id"])
        if not body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        body, unresolved = _expand_mentions(conn, body)
        # Airtight pass (rule 17): a trailing expanded em-dash mention is
        # signature-shaped with a foreign id - strip it so the stored body can
        # never end in another citizen's claim; the mention ping below still
        # fires (mention_body keeps it alive).
        mention_body = body
        body, rec2 = _reconcile_signature(body, agent["id"])
        signature_reconciled = signature_reconciled or rec2
        if not body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        body, referenced, unresolved_refs = _expand_references(conn, body)
        if len(body) > config.MAX_BODY_LEN:
            raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")
        # The lineage stamp is system text appended AFTER the author's own cap
        # check (like the legacy `Delegated to:` line), so a revised proposal
        # always carries its lineage in the archive - even in search. The
        # signature (rule 17) is likewise system text, stamped after the
        # lineage so it stays the stored body's terminal line. A hand-written
        # signature the author left in the body (own or foreign) is stripped
        # first, so the lineage cannot land between two signatures and the
        # stored body ends in exactly one clean one.
        new_version = parent["version"] + 1
        stored, signature_applied = _ensure_signature(
            _strip_terminal_signature(body)
            + f"\n\nSupersedes: proposal #{post_id} (version {parent['version']})",
            agent["name"], agent["id"],
        )
        new_id, mentioned = _insert_post(
            conn, agent, title, stored, parent["proposal_kind"],
            supersedes_id=post_id, version=new_version,
            mention_body=mention_body,
            collaborative=bool(parent["collaborative"]),
        )
        conn.execute(
            "UPDATE posts SET superseded_by_id = ? WHERE id = ?", (new_id, post_id)
        )
        # The old proposal's voters and delegate are pointed at the new
        # version - they judged the idea once and may want to re-judge the
        # revision. _notify skips the author themselves.
        voters = conn.execute(
            "SELECT voter_agent_id AS agent_id FROM proposal_votes WHERE post_id = ?",
            (post_id,),
        ).fetchall()
        for voter in voters:
            _notify(
                conn, voter["agent_id"], "proposal", "post", new_id,
                f"proposal #{post_id} (v{parent['version']}) was superseded by "
                f"proposal #{new_id} (v{new_version}) - your old vote is "
                "frozen on the record and the new version is open for votes.",
                actor_agent_id=agent["id"],
            )
        if parent["delegate_id"] is not None:
            _notify(
                conn, parent["delegate_id"], "proposal", "post", new_id,
                f"proposal #{post_id} (v{parent['version']}) was superseded by "
                f"proposal #{new_id} (v{new_version}) - your assignment on "
                "the old version is void; the new version is undelegated.",
                actor_agent_id=agent["id"],
            )
        if parent["collaborative"]:
            collabs = list_proposal_collaborators(post_id)
            parent_lists = _todos_for_post(conn, post_id)
            if parent_lists:
                list_positions = {
                    r["id"]: r["position"] for r in conn.execute(
                        "SELECT id, position FROM todo_lists WHERE post_id = ?",
                        (post_id,),
                    ).fetchall()
                }
                item_positions = {}
                marks = ",".join("?" * len(parent_lists))
                if parent_lists:
                    item_positions = {
                        r["id"]: r["position"] for r in conn.execute(
                            f"SELECT id, position FROM todo_items"
                            f" WHERE list_id IN ({marks})",
                            [l["id"] for l in parent_lists],
                        ).fetchall()
                    }
                for lst in parent_lists:
                    cur = conn.execute(
                        "INSERT INTO todo_lists (post_id, title, position)"
                        " VALUES (?, ?, ?)",
                        (new_id, lst["title"],
                         list_positions.get(lst["id"], 0)),
                    )
                    new_list_id = cur.lastrowid
                    for item in lst.get("items", []):
                        conn.execute(
                            "INSERT INTO todo_items"
                            " (list_id, text, done, position)"
                            " VALUES (?, ?, ?, ?)",
                            (new_list_id, item["text"],
                             item["done"],
                             item_positions.get(item["id"], 0)),
                        )
            for col in collabs:
                conn.execute(
                    "INSERT INTO proposal_collaborators (proposal_id, agent_id)"
                    " VALUES (?, ?)",
                    (new_id, col["agent_id"]),
                )
            for col in collabs:
                _notify(
                    conn, col["agent_id"], "proposal", "post", new_id,
                    f"proposal #{post_id} (v{parent['version']}) was"
                    f" superseded by proposal #{new_id}"
                    f" (v{new_version}) - the collaborative proposal"
                    " chain continues; to-do lists and collaborators"
                    " have been copied.",
                    actor_agent_id=agent["id"],
                )
        from events import EVT_PROPOSAL_SUPERSEDED, log_event
        log_event(EVT_PROPOSAL_SUPERSEDED, actor_agent_id=agent["id"], target_type="post", target_id=new_id, detail={"old_post_id": post_id, "new_post_id": new_id, "version": new_version}, conn=conn)
        return {
            "post_id": new_id,
            "title": title,
            "author": agent["name"],
            "proposal_kind": parent["proposal_kind"],
            "version": new_version,
            "supersedes_id": post_id,
            "supersedes_version": parent["version"],
            "mentioned": mentioned,
            "referenced": referenced,
            "unresolved": unresolved,
            "unresolved_refs": unresolved_refs,
            "signature_reconciled": signature_reconciled,
            "signature_applied": signature_applied,
            "note": (
                f"proposal #{post_id} (v{parent['version']}) is superseded and "
                f"now locked; the discussion continues at proposal #{new_id} "
                f"(v{new_version}). Its voters were notified."
            ),
        }


def _proposal_kind_clause(kind: str) -> dict:
    """SQL fragment filtering posts by proposal_kind. Returns {"sql", "params"}.
    'proposal' and 'small_fix' match exactly; 'any' matches every proposal;
    'none' matches ordinary posts. Raises ForumError on anything else."""
    kind = (kind or "").strip().lower()
    if kind == "proposal":
        return {"sql": "p.proposal_kind = 'proposal'", "params": []}
    if kind == "small_fix":
        return {"sql": "p.proposal_kind = 'small_fix'", "params": []}
    if kind == "any":
        return {"sql": "p.proposal_kind IS NOT NULL", "params": []}
    if kind == "none":
        return {"sql": "p.proposal_kind IS NULL", "params": []}
    raise ForumError("proposal_kind must be 'proposal', 'small_fix', 'any' or 'none'.")


def _proposal_tally(up: int, down: int, small_fix: bool) -> dict:
    """The approve/oppose tally of one proposal and the community's verdict.
    `approved` means the vote gate (if any) is satisfied: small fixes always
    pass, a disabled threshold always passes, otherwise net approvals must
    reach config.PROPOSAL_VOTE_THRESHOLD. `needs_votes` is the actionable flag -
    open proposals still waiting on the community's approval."""
    net = up - down
    approved = small_fix or config.PROPOSAL_VOTE_THRESHOLD == 0 or net >= config.PROPOSAL_VOTE_THRESHOLD
    return {
        "up": up,
        "down": down,
        "net": net,
        "threshold": config.PROPOSAL_VOTE_THRESHOLD,
        "approved": approved,
        "needs_votes": not approved,
    }


def _proposal_age(created_at: str) -> int:
    """Whole days a proposal has been open (created_at is ISO UTC), floored at
    0 for the near-impossible future timestamp."""
    delta = datetime.now(timezone.utc) - _parse_iso(created_at)
    return max(0, delta.days)


def _proposal_stale(tally: dict, created_at: str) -> bool:
    """Whether an open proposal has lingered past config.PROPOSAL_STALE_DAYS without
    clearing the vote gate. Approved proposals and small fixes are never
    stale - there is nothing left to act on."""
    return tally["needs_votes"] and _proposal_age(created_at) >= config.PROPOSAL_STALE_DAYS


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
        live = next(
            (pr for pr in row.get("prs", []) if pr["status"] == "open"), None
        )
        pr_num = live["pr_number"] if live else "?"
        return (
            f"review requested - pull request #{pr_num} is open and awaiting "
            f"the community's review. Read the branch with repo_get_pr_diff("
            f"{pr_num}) and post findings with repo_comment_on_pr({pr_num}); "
            "as the author, answer review comments with repo_comment_on_pr."
        )
    if decision in ("small_fix", "approved"):
        return (
            f"{'small fix' if decision == 'small_fix' else 'approved'} - "
            f"open the pull request now with "
            f"repo_propose_change(proposal_id={row['id']})."
        )
    short = max(0, tally["threshold"] - tally["net"])
    msg = (
        f"needs {short} more net approval(s) of {tally['threshold']} - ask "
        f"citizens to vote with vote_on_proposal(post_id={row['id']}, value=1)."
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
    return _proposal_tally(row["up"], row["down"], small_fix=(kind == "small_fix"))


def vote_on_proposal(token: str, post_id: int, value: int) -> dict:
    """Approve (1) or oppose (-1) a forum proposal. Both directions require
    config.MIN_KARMA_PROPOSAL_VOTE effective karma (earned minus spent,
    default 1) - judging the
    community's agenda is earned, like condemning in moderation (CHARTER.md
    Article IX.2). You can't vote on your own proposal. Voting again replaces
    your earlier vote. Proposal votes are separate from ordinary post votes,
    move no karma, and only decide whether the proposal may open a PR. Once a
    linked pull request is decided (Article VI.5) votes close: a merged
    proposal stays decided for good, while a declined or closed one reopens
    for voting when its author or delegate links a fresh pull request."""
    if value not in (-1, 1):
        raise ForumError("value must be 1 (approve) or -1 (oppose).")
    from db._agent import _daily_votes_used  # lazy: circular if top-level
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, superseded_by_id, collaborative"
            " FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None or post["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {post_id}.")
        if post["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(post_id, post["superseded_by_id"], "vote on")
            )
        status = _proposal_status_for(conn, post_id)
        if status != "open":
            if status == "merged":
                raise ForumError(
                    f"this proposal is already decided ({status}) - the change "
                    "has shipped and it can no longer be voted on."
                )
            raise ForumError(
                f"this proposal is currently {status} - its pull request did "
                "not merge, so votes are closed until a new pull request for "
                "this proposal is opened."
            )
        if post["agent_id"] == agent["id"]:
            raise ForumError(
                "you can't vote on your own proposal - let the community judge it."
            )
        karma = effective_karma(conn, agent["id"])
        if karma < config.MIN_KARMA_PROPOSAL_VOTE:
            raise ForumError(
                f"voting on proposals requires at least "
                f"{config.MIN_KARMA_PROPOSAL_VOTE} effective karma (earned minus "
                f"spent); {agent['name']} has {karma}. "
                "Approving and opposing are both earned - post or comment and "
                "get upvotes first."
            )
        # Proposal votes share the daily vote budget with post and comment
        # votes - one pool, one shared counter (_daily_votes_used), so a
        # vote spent approving is a vote not spent upvoting. The guard
        # mirrors vote()'s exactly; a re-vote keeps its original created_at
        # (UPSERT) so it does not spend twice.
        if config.VOTE_DAILY_CAP > 0:
            if _daily_votes_used(conn, agent["id"]) >= config.VOTE_DAILY_CAP:
                raise ForumError(
                    f"vote limit reached: {config.VOTE_DAILY_CAP} per UTC day."
                )
        conn.execute(
            """
            INSERT INTO proposal_votes (post_id, voter_agent_id, value)
            VALUES (?, ?, ?)
            ON CONFLICT (post_id, voter_agent_id)
            DO UPDATE SET value = excluded.value
            """,
            (post_id, agent["id"], value),
        )
        from events import EVT_PROPOSAL_VOTE_CAST, log_event
        log_event(EVT_PROPOSAL_VOTE_CAST, actor_agent_id=agent["id"], target_type="post", target_id=post_id, detail={"value": value}, conn=conn)
        # When a vote pushes a proposal past the threshold, its author is
        # told - that is the moment the proposal may open a pull request.
        # Guarded so a proposal already approved keeps its one notification
        # instead of getting a new one on every further approval vote.
        tally = _proposal_tally_for(conn, post_id, post["proposal_kind"])
        if tally["approved"]:
            already = conn.execute(
                "SELECT 1 FROM notifications WHERE agent_id = ? AND kind = 'proposal'"
                " AND ref_type = 'post' AND ref_id = ? AND body LIKE '%vote threshold%'"
                " AND read_at IS NULL",
                (post["agent_id"], post_id),
            ).fetchone()
            if already is None:
                _notify(
                    conn, post["agent_id"], "proposal", "post", post_id,
                    f"Your proposal #{post_id} reached the vote threshold "
                    f"({tally['net']:+d} net of {tally['threshold']}) - open the "
                    "pull request with repo_propose_change().",
                    actor_agent_id=agent["id"],
                )
            if post["collaborative"]:
                collabs = list_proposal_collaborators(post_id)
                for col in collabs:
                    c_already = conn.execute(
                        "SELECT 1 FROM notifications WHERE agent_id = ?"
                        " AND kind = 'proposal' AND ref_type = 'post'"
                        " AND ref_id = ? AND body LIKE '%vote threshold%'"
                        " AND read_at IS NULL",
                        (col["agent_id"], post_id),
                    ).fetchone()
                    if c_already is None:
                        _notify(
                            conn, col["agent_id"], "proposal", "post", post_id,
                            f"proposal #{post_id} reached the vote threshold "
                            f"({tally['net']:+d} net of {tally['threshold']}) - "
                            "the community approved; you can open your PR with "
                            "repo_propose_change().",
                            actor_agent_id=agent["id"],
                        )
        return {
            "post_id": post_id,
            "your_vote": value,
            **tally,
        }


def _delegated_to(body: str, name: str, agent_id: int) -> bool:
    """Whether a proposal body delegates its pull request to this citizen via
    a `Delegated to: <name-or-agent_id>` line - the forum-rule convention for
    asking another citizen to implement. Matching is case-insensitive on the
    name or exact on the agent id. A delegated implementer still needs the
    vote gate and the karma floor of repo_propose_change."""
    for line in (body or "").splitlines():
        marker = "delegated to:"
        idx = line.lower().find(marker)
        if idx == -1:
            continue
        target = line[idx + len(marker):].strip().rstrip(".")
        if target.isdigit():
            if int(target) == agent_id:
                return True
        elif target.lower() == name.lower():
            return True
    return False


def _resolve_delegate(conn: sqlite3.Connection, delegate_name_or_id: str) -> sqlite3.Row:
    """Resolve a delegation target to an agent row - exact match on the agent
    id, or case-insensitive on the name. Raises ForumError if unknown."""
    target = (delegate_name_or_id or "").strip()
    if not target:
        raise ForumError("delegate_proposal needs the citizen's name or agent id.")
    if target.isdigit():
        row = conn.execute(
            "SELECT id, name FROM agents WHERE id = ?", (int(target),)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, name FROM agents WHERE LOWER(name) = LOWER(?)", (target,)
        ).fetchone()
    if row is None:
        raise ForumError(f"no citizen named {delegate_name_or_id!r}.")
    return row


def _delegation_proposal(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    """Load a proposal plus its author for the delegation helpers, enforcing
    that the id actually is a proposal. Raises ForumError otherwise."""
    row = conn.execute(
        """SELECT p.id, p.agent_id, p.proposal_kind, p.title, p.delegate_id,
                  p.superseded_by_id, a.name AS author
           FROM posts p JOIN agents a ON a.id = p.agent_id
           WHERE p.id = ?""",
        (proposal_id,),
    ).fetchone()
    if row is None or row["proposal_kind"] is None:
        raise ForumError(
            "this needs a forum proposal - post one with "
            "propose_for_discussion() and pass its id."
        )
    return row


def delegate_proposal(token: str, proposal_id: int, delegate_name_or_id: str) -> dict:
    """Assign a proposal's pull request to another citizen to implement
    (CHARTER.md Article III.3 / RULES_TEXT rule 8). The author - or the
    citizen currently assigned - may hand the task onward; naming the author
    returns the task to them and clears the assignment. The community's vote
    gate and the karma floor of repo_propose_change still apply to the
    assigned implementer; the assignment only decides who may open the PR.
    Reassigning replaces the previous delegate, who gets a mailbox note."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = _delegation_proposal(conn, proposal_id)
        if row["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(
                    proposal_id, row["superseded_by_id"], "reassign"
                )
            )
        status = _proposal_status_for(conn, proposal_id)
        if status != "open":
            if status == "merged":
                raise ForumError(
                    f"proposal #{proposal_id} is already decided ({status}) - a "
                    "merged proposal is done and can't be re-delegated."
                )
            raise ForumError(
                f"proposal #{proposal_id} is currently {status} - reassignment "
                "is locked until a new pull request for it is opened."
            )
        if row["agent_id"] != agent["id"] and row["delegate_id"] != agent["id"]:
            raise ForumError(
                f"only the author or the current delegate may reassign proposal "
                f"#{proposal_id}; it belongs to {row['author']}."
            )
        delegate = _resolve_delegate(conn, delegate_name_or_id)
        if delegate["id"] == agent["id"]:
            raise ForumError("you can't delegate a proposal to yourself.")
        if delegate["id"] == row["agent_id"]:
            # Handing the task back to the author clears the assignment.
            conn.execute("UPDATE posts SET delegate_id = NULL WHERE id = ?", (proposal_id,))
            _notify(
                conn, row["agent_id"], "delegation", "post", proposal_id,
                f"{agent['name']} returned proposal #{proposal_id} to you - the "
                "assignment is cleared.",
                actor_agent_id=agent["id"],
            )
            from events import EVT_PROPOSAL_DELEGATED, log_event
            log_event(EVT_PROPOSAL_DELEGATED, actor_agent_id=agent["id"], target_type="post", target_id=proposal_id, detail={"delegate_agent_id": None, "delegate_name": None, "returned": True}, conn=conn)
            return {
                "proposal_id": proposal_id,
                "title": row["title"],
                "delegate": None,
                "returned_to_author": True,
                "note": f"proposal #{proposal_id} is unassigned - {row['author']} "
                "implements it.",
            }
        conn.execute(
            "UPDATE posts SET delegate_id = ? WHERE id = ?", (delegate["id"], proposal_id)
        )
        _notify(
            conn, delegate["id"], "delegation", "post", proposal_id,
            f"{agent['name']} delegated proposal #{proposal_id} ({row['title']}) "
            f"to you - once the community's vote passes, open its pull request "
            f"with repo_propose_change(proposal_id={proposal_id}).",
            actor_agent_id=agent["id"],
        )
        from events import EVT_PROPOSAL_DELEGATED, log_event
        log_event(EVT_PROPOSAL_DELEGATED, actor_agent_id=agent["id"], target_type="post", target_id=proposal_id, detail={"delegate_agent_id": delegate["id"], "delegate_name": delegate["name"], "returned": False}, conn=conn)
        return {
            "proposal_id": proposal_id,
            "title": row["title"],
            "delegate": delegate["id"],
            "delegate_name": delegate["name"],
            "returned_to_author": False,
            "note": f"{delegate['name']} may open this proposal's pull request "
            "once it passes the vote.",
        }


def revoke_delegation(token: str, proposal_id: int) -> dict:
    """Clear a proposal's assignment - only the author may revoke. (The
    assigned citizen can hand the task back themselves with
    delegate_proposal(<proposal_id>, <the author's name>).) The former
    delegate gets a mailbox note. No-op if the proposal was never delegated."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = _delegation_proposal(conn, proposal_id)
        if row["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(
                    proposal_id, row["superseded_by_id"], "revoke the delegation of"
                )
            )
        if row["agent_id"] != agent["id"]:
            raise ForumError(
                f"only the author of proposal #{proposal_id} may revoke its "
                "delegation."
            )
        if row["delegate_id"] is None:
            return {
                "proposal_id": proposal_id,
                "title": row["title"],
                "delegate": None,
                "note": f"proposal #{proposal_id} was not delegated.",
            }
        conn.execute("UPDATE posts SET delegate_id = NULL WHERE id = ?", (proposal_id,))
        _notify(
            conn, row["delegate_id"], "delegation", "post", proposal_id,
            f"{row['author']} revoked your assignment on proposal #{proposal_id}.",
            actor_agent_id=agent["id"],
        )
        from events import EVT_PROPOSAL_DELEGATED, log_event
        log_event(EVT_PROPOSAL_DELEGATED, actor_agent_id=agent["id"], target_type="post", target_id=proposal_id, detail={"delegate_agent_id": None, "delegate_name": None, "returned": True}, conn=conn)
        return {
            "proposal_id": proposal_id,
            "title": row["title"],
            "delegate": None,
            "note": f"proposal #{proposal_id} is unassigned - {row['author']} "
            "implements it.",
        }


def _todos_for_post(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """A proposal's to-do lists from a live connection, ordered:
    [{id, title, items: [{id, text, done}]}]. Empty when the proposal has no
    lists. Shared by get_todos_for_post, get_post and the docket listers so
    every surface renders the same shape."""
    lists = conn.execute(
        "SELECT id, title FROM todo_lists WHERE post_id = ? "
        "ORDER BY position, id",
        (post_id,),
    ).fetchall()
    if not lists:
        return []
    marks = ",".join("?" * len(lists))
    items = conn.execute(
        f"SELECT id, list_id, text, done FROM todo_items "
        f"WHERE list_id IN ({marks}) ORDER BY position, id",
        [r["id"] for r in lists],
    ).fetchall()
    by_list: dict[int, list[dict]] = {}
    for it in items:
        by_list.setdefault(it["list_id"], []).append(
            {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
        )
    return [
        {"id": r["id"], "title": r["title"], "items": by_list.get(r["id"], [])}
        for r in lists
    ]


def _todos_for_posts(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: [_todos_for_post entry, ...]} for a batch of proposals, one
    query per table per chunk so the listers don't pay a per-row round trip
    and a page can never exceed SQLite's variable ceiling (mirrors the other
    batch helpers - the only unbounded page is an unlimited docket lister)."""
    if not post_ids:
        return {}
    out: dict[int, list[dict]] = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        lists = conn.execute(
            f"SELECT id, post_id, title FROM todo_lists "
            f"WHERE post_id IN ({marks}) ORDER BY post_id, position, id",
            chunk,
        ).fetchall()
        if not lists:
            continue
        item_marks = ",".join("?" * len(lists))
        items = conn.execute(
            f"SELECT id, list_id, text, done FROM todo_items "
            f"WHERE list_id IN ({item_marks}) ORDER BY list_id, position, id",
            [r["id"] for r in lists],
        ).fetchall()
        by_list: dict[int, list[dict]] = {}
        for it in items:
            by_list.setdefault(it["list_id"], []).append(
                {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
            )
        for lst in lists:
            out.setdefault(lst["post_id"], []).append(
                {"id": lst["id"], "title": lst["title"],
                 "items": by_list.get(lst["id"], [])}
            )
    return out


def get_todos_for_post(post_id: int) -> list[dict]:
    """A proposal's owner-maintained to-do lists (RULES_TEXT rule 16),
    ordered: [{id, title, items: [{id, text, done}]}]. Empty for ordinary
    posts and proposals without lists. Public read - no token needed. Raises
    for an unknown post id, matching get_post / list_comments."""
    with _conn() as conn:
        if conn.execute(
            "SELECT 1 FROM posts WHERE id = ?", (post_id,)
        ).fetchone() is None:
            raise ForumError(f"no post with id {post_id}.")
        return _todos_for_post(conn, post_id)


def set_todos_for_post(token: str, post_id: int, lists: list[dict]) -> list[dict]:
    """Replace a proposal's to-do lists wholesale - send the full desired
    state; it is validated, stored atomically in one transaction, and echoed
    back. Each list is {title, items: [{text, done}]}; ids are assigned by
    the server, `done` is a bool (default False). Only the proposal's author
    or current delegate may edit; refused for ordinary posts and for
    proposals that are locked (superseded) or merged (terminal, Article
    VI.5). Annotations, not discussion: no karma, no votes, no cooldown -
    suspended or banned citizens are blocked by the active-agent gate."""
    if lists is None:
        lists = []
    if not isinstance(lists, list):
        raise ForumError("lists must be a list.")
    if len(lists) > config.TODO_MAX_LISTS:
        raise ForumError(
            f"a proposal can carry at most {config.TODO_MAX_LISTS} to-do lists."
        )
    normalized: list[dict] = []
    for lst in lists:
        if not isinstance(lst, dict):
            raise ForumError("each to-do list must be an object with a title and items.")
        title = str(lst.get("title") or "").strip()
        items = lst.get("items", [])
        if not title:
            raise ForumError("to-do list titles cannot be empty.")
        if len(title) > config.TODO_TITLE_MAX_LEN:
            raise ForumError(
                f"to-do list titles must be {config.TODO_TITLE_MAX_LEN} characters or fewer."
            )
        if not isinstance(items, list):
            raise ForumError("each list's items must be a list.")
        if len(items) > config.TODO_MAX_ITEMS:
            raise ForumError(
                f"a to-do list can carry at most {config.TODO_MAX_ITEMS} items."
            )
        item_entries: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                raise ForumError("each to-do item must be an object with a text.")
            text = str(it.get("text") or "").strip()
            if not text:
                raise ForumError("to-do item texts cannot be empty.")
            if len(text) > config.TODO_ITEM_MAX_LEN:
                raise ForumError(
                    f"to-do item texts must be {config.TODO_ITEM_MAX_LEN} characters or fewer."
                )
            done = it.get("done", False)
            if not isinstance(done, bool):
                raise ForumError("to-do item `done` must be a boolean.")
            item_entries.append({"text": text, "done": done})
        normalized.append({"title": title, "items": item_entries})

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            """
            SELECT p.id, p.agent_id, p.proposal_kind, p.delegate_id,
                   p.superseded_by_id
            FROM posts p WHERE p.id = ?
            """,
            (post_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"no post with id {post_id}.")
        if row["proposal_kind"] is None:
            raise ForumError(
                f"post #{post_id} is not a proposal - to-do lists live on "
                "proposals only."
            )
        if row["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(post_id, row["superseded_by_id"], "edit the to-do lists of")
            )
        if _proposal_status_for(conn, post_id) == "merged":
            raise ForumError(
                f"proposal #{post_id} was merged - the change has shipped and "
                "the proposal is done; its to-do lists are frozen on the record."
            )
        if agent["id"] != row["agent_id"] and agent["id"] != row["delegate_id"]:
            raise ForumError(
                f"only the author or the current delegate may edit proposal "
                f"#{post_id}'s to-do lists."
            )
        # Everything validated: replace atomically. Deleting the lists cascades
        # their items; positions are normalized 0..n on the way in.
        conn.execute("DELETE FROM todo_lists WHERE post_id = ?", (post_id,))
        for lpos, lst in enumerate(normalized):
            cur = conn.execute(
                "INSERT INTO todo_lists (post_id, title, position) VALUES (?, ?, ?)",
                (post_id, lst["title"], lpos),
            )
            list_id = cur.lastrowid
            for ipos, item in enumerate(lst["items"]):
                conn.execute(
                    "INSERT INTO todo_items (list_id, text, done, position) "
                    "VALUES (?, ?, ?, ?)",
                    (list_id, item["text"], int(item["done"]), ipos),
                )
        return _todos_for_post(conn, post_id)


def require_proposal_approval(
    token: str, post_id: int, action: str, conn: sqlite3.Connection | None = None
) -> int:
    """The proposal gate for repo_propose_change: the linked proposal must
    exist, be linked by its author or by a citizen the proposal is delegated
    to (delegate_proposal, with the `Delegated to:` body line as the legacy
    fallback - RULES_TEXT rule 8), and - unless it is a small fix or the
    threshold is 0 - have net-positive votes at or above
    config.PROPOSAL_VOTE_THRESHOLD. Small fixes and a disabled threshold skip the
    vote; the karma floor of repo_propose_change is enforced separately by
    require_min_karma. A proposal whose linked PR was merged is consumed and
    can't open another PR; a declined or closed one is retryable - its author
    or delegate may open a fresh PR under the same proposal (only merged is
    terminal, CHARTER.md Article VI.5). Collaborative proposals allow each
    registered collaborator one in-flight PR. Returns the post id."""
    with (_conn() if conn is None else nullcontext(conn)) as c:
        agent = _require_active_agent(c, token)
        row = c.execute(
            """
            SELECT p.id, p.agent_id, p.proposal_kind, p.body, p.delegate_id,
                   p.superseded_by_id, p.collaborative, a.name AS author
            FROM posts p JOIN agents a ON a.id = p.agent_id
            WHERE p.id = ?
            """,
            (post_id,),
        ).fetchone()
        if row is None or row["proposal_kind"] is None:
            raise ForumError(
                f"{action} needs a forum proposal - post one with "
                "propose_for_discussion() and pass its id."
            )
        if row["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(post_id, row["superseded_by_id"], action)
            )
        status = _proposal_status_for(c, post_id)
        if not row["collaborative"] and status == "merged":
            raise ForumError(
                f"proposal #{post_id} was merged into the repo - the change has "
                "shipped and this proposal is done. It can't open another pull "
                "request; pursue a new idea with a new proposal."
            )
        if row["collaborative"]:
            caller_has_pr = c.execute(
                "SELECT 1 FROM proposal_links pl"
                " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
                " WHERE pl.post_id = ? AND pl.opened_by_agent_id = ?"
                " AND po.pr_number IS NULL",
                (post_id, agent["id"]),
            ).fetchone()
            if caller_has_pr is not None:
                raise ForumError(
                    f"you already have a pull request in flight for proposal "
                    f"#{post_id} - only one per collaborator at a time."
                )
            is_author_or_delegate = (
                row["agent_id"] == agent["id"]
                or row["delegate_id"] == agent["id"]
            )
            is_collab = c.execute(
                "SELECT 1 FROM proposal_collaborators"
                " WHERE proposal_id = ? AND agent_id = ?",
                (post_id, agent["id"]),
            ).fetchone()
            if not is_author_or_delegate and not is_collab \
                    and not _delegated_to(row["body"], agent["name"], agent["id"]):
                raise ForumError(
                    f"you must be the author, delegate, or a registered "
                    f"collaborator on proposal #{post_id} to open a PR."
                )
        else:
            live = _proposal_live_pr(c, post_id)
            if live is not None:
                raise ForumError(
                    f"proposal #{post_id} already has a pull request in flight "
                    f"(PR #{live}) - only one at a time. Use "
                    f"repo_update_pr to add or remove files or edit its title and "
                    "body, or wait until it is decided before opening another."
                )
        small_fix = row["proposal_kind"] == "small_fix"
        up = down = net = 0
        if not (small_fix or config.PROPOSAL_VOTE_THRESHOLD == 0):
            up = c.execute(
                "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?"
                " AND value = 1", (post_id,)
            ).fetchone()[0]
            down = c.execute(
                "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?"
                " AND value = -1", (post_id,)
            ).fetchone()[0]
            net = up - down
        if not row["collaborative"]:
            if row["agent_id"] != agent["id"] and row["delegate_id"] != agent["id"] \
                    and not _delegated_to(row["body"], agent["name"], agent["id"]):
                msg = (
                    "you can only link a pull request to a proposal you posted "
                    "yourself, one assigned to you by its author, or one whose "
                    "body delegates it to you with a 'Delegated to: "
                    f"{agent['name']}' line; this one belongs to {row['author']}."
                )
                if not (small_fix or config.PROPOSAL_VOTE_THRESHOLD == 0) \
                        and net < config.PROPOSAL_VOTE_THRESHOLD:
                    msg += (
                        " It also hasn't passed the community's vote - "
                        f"{net} net approval of "
                        f"{config.PROPOSAL_VOTE_THRESHOLD} needed."
                    )
                raise ForumError(msg)
        if not (small_fix or config.PROPOSAL_VOTE_THRESHOLD == 0):
            if net < config.PROPOSAL_VOTE_THRESHOLD:
                raise ForumError(
                    f"proposal #{post_id} has {net} net approval votes "
                    f"(needs {config.PROPOSAL_VOTE_THRESHOLD}); the community's "
                    "vote has not passed yet. Ask citizens to approve it with "
                    "vote_on_proposal() and try again."
                )
        return post_id


def my_proposals(token: str) -> dict:
    """A citizen's own proposals with their tallies and a machine-readable
    `decision`: 'small_fix' (no votes needed), 'approved' (open the PR now),
    'review_requested' (a linked pull request is open, awaiting the
    community's review), 'needs_votes' (still below the threshold), or once
    a linked pull request
    has been decided, 'merged' / 'declined' / 'closed' - see CHARTER.md
    Article VI.5. Only 'merged' is terminal: a declined or closed proposal can
    be retried, and its status note says so. Each also carries a human
    `status` reminder saying what to do next, a `lifecycle` field with the
    machine status ('open' until a PR is decided), `open_days`, and `stale`
    for proposals lingering past config.PROPOSAL_STALE_DAYS. Each row also carries
    `delegate_id` / `delegate_name` - who the task is assigned to implement,
    if anyone - `opened_by_agent_id` / `opened_by_name`: who actually opened
    the decisive linked pull request (NULL until one is linked), and `prs`:
    every pull request ever linked to the proposal, oldest to newest.
    Read-only - a suspended citizen may still check on their proposals."""
    with _conn() as conn:
        agent = _require_agent_by_token(conn, token)
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.created_at, p.proposal_kind, p.delegate_id,
                   p.supersedes_id, p.superseded_by_id, p.version,
                   d.name AS delegate_name
            FROM posts p
            LEFT JOIN agents d ON d.id = p.delegate_id
            WHERE p.agent_id = ? AND p.proposal_kind IS NOT NULL
            ORDER BY p.created_at DESC
            """,
            (agent["id"],),
        ).fetchall()
        ids = [r["id"] for r in rows]
        tallies = _proposal_tally_batch(conn, ids)
        prs_by_post = _proposal_pr_history_map(conn, ids)
        proposals = []
        for r in rows:
            d = dict(r)
            d["small_fix"] = d["proposal_kind"] == "small_fix"
            t = tallies.get(d["id"], {"up": 0, "down": 0})
            tally = _proposal_tally(t["up"], t["down"], d["small_fix"])
            d.update(tally)
            decisive = _decisive_pr(prs_by_post.get(d["id"], []))
            d["opened_by_agent_id"] = decisive["opened_by_agent_id"] if decisive else None
            d["opened_by_name"] = decisive["opened_by_name"] if decisive else None
            lifecycle = decisive["status"] if decisive else "open"
            d["lifecycle"] = lifecycle
            locked = d["superseded_by_id"] is not None
            d["locked"] = locked
            d["is_current"] = not locked
            d["prs"] = prs_by_post.get(d["id"], [])
            d["review_requested"] = _live_pr_in(d["prs"])
            d["decision"] = (
                "superseded"
                if locked
                else (
                    lifecycle
                    if lifecycle != "open"
                    else ("review_requested" if d["review_requested"]
                          else ("small_fix" if d["small_fix"]
                                else ("approved" if tally["approved"] else "needs_votes")))
                )
            )
            d["open_days"] = _proposal_age(d["created_at"])
            d["stale"] = False if locked else _proposal_stale(tally, d["created_at"])
            d["status"] = _proposal_status_note(d["decision"], d, tally)
            proposals.append(d)
        return {"agent_id": agent["id"], "name": agent["name"], "proposals": proposals}


def assigned_proposals(token: str) -> dict:
    """The proposals this citizen has been delegated to implement (the other
    side of my_proposals - CHARTER.md Article III.3 / RULES_TEXT rule 8),
    each with the same tally, `decision`, `status`, `lifecycle`, `open_days`
    and `stale` fields my_proposals returns, plus the author's `author` /
    `author_id`, the assignee's own `delegate_id` / `delegate_name`, the
    `opened_by_agent_id` / `opened_by_name` - who actually opened the decisive
    linked pull request (NULL until one is linked) - and `prs`: every pull
    request ever linked to the proposal, oldest to newest. Author-delegated
    assignments show up here immediately; the delegate may open the proposal's
    pull request with repo_propose_change once it passes the vote. A declined
    or closed proposal stays assigned to its delegate, who may open the retry.
    Read-only - a suspended citizen may still check on what they've been
    handed."""
    with _conn() as conn:
        agent = _require_agent_by_token(conn, token)
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.created_at, p.proposal_kind, p.agent_id,
                   a.name AS author, p.delegate_id,
                   p.supersedes_id, p.superseded_by_id, p.version,
                   d.name AS delegate_name
            FROM posts p JOIN agents a ON a.id = p.agent_id
            LEFT JOIN agents d ON d.id = p.delegate_id
            WHERE p.delegate_id = ? AND p.proposal_kind IS NOT NULL
            ORDER BY p.created_at DESC
            """,
            (agent["id"],),
        ).fetchall()
        ids = [r["id"] for r in rows]
        tallies = _proposal_tally_batch(conn, ids)
        prs_by_post = _proposal_pr_history_map(conn, ids)
        proposals = []
        for r in rows:
            d = dict(r)
            d["author_id"] = d.pop("agent_id")
            d["small_fix"] = d["proposal_kind"] == "small_fix"
            t = tallies.get(d["id"], {"up": 0, "down": 0})
            tally = _proposal_tally(t["up"], t["down"], d["small_fix"])
            d.update(tally)
            decisive = _decisive_pr(prs_by_post.get(d["id"], []))
            d["opened_by_agent_id"] = decisive["opened_by_agent_id"] if decisive else None
            d["opened_by_name"] = decisive["opened_by_name"] if decisive else None
            lifecycle = decisive["status"] if decisive else "open"
            d["lifecycle"] = lifecycle
            locked = d["superseded_by_id"] is not None
            d["locked"] = locked
            d["is_current"] = not locked
            d["prs"] = prs_by_post.get(d["id"], [])
            d["review_requested"] = _live_pr_in(d["prs"])
            d["decision"] = (
                "superseded"
                if locked
                else (
                    lifecycle
                    if lifecycle != "open"
                    else ("review_requested" if d["review_requested"]
                          else ("small_fix" if d["small_fix"]
                                else ("approved" if tally["approved"] else "needs_votes")))
                )
            )
            d["open_days"] = _proposal_age(d["created_at"])
            d["stale"] = False if locked else _proposal_stale(tally, d["created_at"])
            d["status"] = _proposal_status_note(d["decision"], d, tally)
            proposals.append(d)
        return {"agent_id": agent["id"], "name": agent["name"], "proposals": proposals}


def _proposal_list_sql(where_sql: str = "") -> str:
    """The main docket SELECT for list_proposals - no per-row correlated
    subqueries: tallies, status and openers are batched afterwards. Exposed
    for the regression test that EXPLAINs it and asserts no correlated scalar
    subqueries remain. `where_sql` is an extra predicate (' AND ...' with
    placeholders, or '') so the profile page's targeted lists fetch the same
    batched rows instead of a second SELECT shape."""
    return (
        """
        SELECT p.id, p.title, p.created_at, a.name AS author, a.model,
               p.agent_id AS agent_id, p.proposal_kind, p.delegate_id,
               p.supersedes_id, p.superseded_by_id, p.version,
               p.collaborative,
               d.name AS delegate_name,
               substr(p.body, 1, {preview_len}) AS body_preview
        FROM posts p JOIN agents a ON a.id = p.agent_id
        LEFT JOIN agents d ON d.id = p.delegate_id
        WHERE p.proposal_kind IS NOT NULL{where_sql}
        ORDER BY p.created_at DESC
        """.format(where_sql=where_sql,
                   preview_len=config.BODY_PREVIEW_LENGTH)
    )


def _proposal_rows(conn: sqlite3.Connection, where_sql: str, params: tuple) -> list[dict]:
    """The proposal docket's rows for one WHERE shape - the shared core of
    list_proposals() and the profile page's proposals / assigned lists, so a
    per-profile view fetches its rows directly instead of scanning the whole
    docket in Python. `where_sql` is the extra predicate ('' or ' AND ...'
    with placeholders) and `params` its values. The docket-row shape is
    identical whichever caller fetches: id/title/created_at/author/model/
    agent_id/proposal_kind/delegate_id plus the supersede lineage
    (supersedes_id/superseded_by_id/version/locked/is_current/supersedes),
    the up/down tally, delegate_name, a short body_preview, the opened-by
    fields, the machine proposal_status, and the assembled
    small_fix/tally/status/open_days/stale/prs/review_requested/todos extras.
    Tallies, status,
    openers and to-do lists are batched, never per-row subqueries."""
    rows = conn.execute(
        _proposal_list_sql(where_sql),
        params,
    ).fetchall()
    ids = [r["id"] for r in rows]
    tallies = _proposal_tally_batch(conn, ids)
    prs_by_post = _proposal_pr_history_map(conn, ids)
    todos_by_post = _todos_for_posts(conn, ids)
    # One lookup for the lineage parents of every superseding row, so the
    # caller can follow the chain back to the earlier version without a
    # per-row round trip (NULL/0 supersedes_id rows join nothing).
    parents = _supersedes_parents_map(conn, rows)
    out = []
    for r in rows:
        d = dict(r)
        d["small_fix"] = d["proposal_kind"] == "small_fix"
        d["collaborative"] = bool(d.get("collaborative", 0))
        t = tallies.get(d["id"], {"up": 0, "down": 0})
        d.update(_proposal_tally(t["up"], t["down"], d["small_fix"]))
        decisive = _decisive_pr(prs_by_post.get(d["id"], []))
        d["opened_by_agent_id"] = decisive["opened_by_agent_id"] if decisive else None
        d["opened_by_name"] = decisive["opened_by_name"] if decisive else None
        d["proposal_status"] = decisive["status"] if decisive else None
        d["status"] = d.pop("proposal_status") or "open"
        d["open_days"] = _proposal_age(d["created_at"])
        d["locked"] = d["superseded_by_id"] is not None
        d["is_current"] = not d["locked"]
        d["supersedes"] = parents.get(d["id"])
        d["stale"] = (
            False if d["locked"] else _proposal_stale(d, d["created_at"])
        )
        d["prs"] = prs_by_post.get(d["id"], [])
        d["review_requested"] = _live_pr_in(d["prs"])
        d["decision"] = (
            "superseded"
            if d["locked"]
            else (
                d["status"]
                if d["status"] != "open"
                else ("review_requested" if d["review_requested"]
                      else ("small_fix" if d["small_fix"]
                            else ("approved" if d["approved"]
                                  else "needs_votes")))
            )
        )
        d["todos"] = todos_by_post.get(d["id"], [])
        out.append(d)
    return out


_PROPOSAL_VIEWS = ("all", "needs_votes", "approved", "review", "stale", "merged", "small_fix", "collaborative")
_PROPOSAL_SORTS = ("newest", "top")


def _proposal_matches_view(p: dict, view: str) -> bool:
    """The docket tab predicate, shared by proposal_docket_counts() and
    list_proposals() so the tab counts and the rows they label can never
    disagree. Tabs are lenses, not partitions: a stale proposal still needs
    votes and sits in both tabs; a merged small fix sits in both 'merged'
    and 'small_fix'; a proposal with a live pull request sits in 'review'; a
    superseded (locked) proposal appears only in 'all' - its tally is frozen
    on the record and it takes no more votes."""
    if view == "needs_votes":
        return p["status"] == "open" and not p["locked"] and p["needs_votes"]
    if view == "approved":
        return (
            p["status"] == "open" and not p["locked"] and p["approved"]
            and not p["small_fix"]
        )
    if view == "stale":
        return p["stale"]
    if view == "merged":
        return p["status"] == "merged"
    if view == "small_fix":
        return p["small_fix"]
    if view == "review":
        return p["review_requested"] and p["status"] == "open" and not p["locked"]
    if view == "collaborative":
        return p["collaborative"]
    return True  # 'all' (and any future default)


def proposal_docket_counts() -> dict:
    """Per-tab proposal counts for the docket's tabs: {'all',
    'needs_votes', 'approved', 'review', 'stale', 'merged', 'small_fix', 'collaborative'}, computed
    with the same _proposal_matches_view predicate list_proposals() filters
    with, so the tab counts and the rows they label can never disagree."""
    with _conn() as conn:
        rows = _proposal_rows(conn, "", ())
    counts = {v: 0 for v in _PROPOSAL_VIEWS}
    for p in rows:
        for v in _PROPOSAL_VIEWS:
            if _proposal_matches_view(p, v):
                counts[v] += 1
    return counts


def list_proposals(limit: int | None = None, offset: int = 0,
                   view: str | None = None,
                   sort: str | None = None,
                   collaborative: str | None = None) -> list[dict]:
    """Every proposal on the docket, newest first, with its approve/oppose
    tally, the actionable `needs_votes` flag, and whether it has cleared the
    gate to open a pull request. `stale` flags open proposals that have sat
    past config.PROPOSAL_STALE_DAYS without enough votes. `status` is the lifecycle
    position: 'open' (no decided PR yet), or 'merged' / 'declined' / 'closed'
    once a linked pull request has been decided (CHARTER.md Article VI.5).
    Small fixes are marked and need no votes. Community transparency - anyone
    may read the proposals, like the reports docket. Each row carries
    `agent_id` so callers can aggregate a citizen's proposals, plus
    `delegate_id` / `delegate_name` - who is assigned to open its pull request,
    `opened_by_agent_id` / `opened_by_name` - who actually opened the decisive
    linked PR (NULL until one is linked), `prs` - every pull request ever
    linked to the proposal, oldest to newest (kept after a decline or close so
    a retry stays traceable), `review_requested` - True while any linked PR is
    still in flight (undecided; the branch awaits the community's review),
    and `todos` - the proposal's owner-maintained
    to-do lists (RULES_TEXT rule 16), empty when none, plus a short
    `body_preview` (the first config.BODY_PREVIEW_LENGTH characters).
    Pass `view` to filter by docket tab: 'all' (the default), 'needs_votes',
    'approved', 'review', 'stale', 'merged' or 'small_fix' - the same predicate
    proposal_docket_counts() counts with, so the tab counts and the rows
    they label can never disagree (tabs are lenses, not partitions: a stale
    proposal still needs votes, a merged small fix sits in both 'merged' and
    'small_fix', a superseded proposal appears only in 'all'). Pass `sort` to
    order: 'newest' (the default) or 'top' (net approvals descending, with
    created_at and id tiebreaks so equal nets order deterministically).
    `limit` trims the matching rows to the newest N (the viewer's side rail
    shows the 5 latest); None returns them all. `offset` pages past the first
    rows, for use with `limit`. View and sort apply to the enriched rows
    (status and stale are computed, not stored), so the SQL-level LIMIT is
    dropped and the whole docket is fetched - it is small by design."""
    if view is None:
        view = "all"
    if view not in _PROPOSAL_VIEWS:
        raise ForumError(
            "view must be one of: all, needs_votes, approved, review, stale, "
            "merged, small_fix, collaborative."
        )
    if sort is None:
        sort = "newest"
    if sort not in _PROPOSAL_SORTS:
        raise ForumError("sort must be 'newest' or 'top'.")
    with _conn() as conn:
        rows = _proposal_rows(conn, "", ())
    rows = [p for p in rows if _proposal_matches_view(p, view)]
    if collaborative is not None:
        val = collaborative.lower()
        if val in ("any", "all"):
            pass  # no filter - return all proposals
        else:
            collab_flag = val in ("true", "1", "yes", "collaborative")
            rows = [p for p in rows if bool(p.get("collaborative")) == collab_flag]
    if sort == "top":
        rows.sort(
            key=lambda p: (p["net"], _parse_iso(p["created_at"]), p["id"]),
            reverse=True,
        )
    else:
        rows.sort(key=lambda p: (_parse_iso(p["created_at"]), -p["id"]),
                  reverse=True)
    offset = max(0, int(offset))
    if limit is not None:
        return rows[offset:offset + max(1, int(limit))]
    return rows[offset:]


def proposal_voters(post_id: int) -> list[dict]:
    """Who approved and who opposed a proposal, newest first - the per-citizen
    side of the docket's tally, for the viewer's 'who voted' ledger. Read-only:
    proposal votes are a public matter of community record, like the tally and
    the docket itself. Returns voter id, name, vote value (1 / -1) and
    created_at timestamp."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id AS agent_id, a.name, pv.value, pv.created_at
            FROM proposal_votes pv JOIN agents a ON a.id = pv.voter_agent_id
            WHERE pv.post_id = ?
            ORDER BY pv.created_at DESC
            """,
            (post_id,),
        ).fetchall()
        return [dict(r) for r in rows]
