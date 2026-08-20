"""db._proposal_docket — proposal listing, docket counts, and view/sort helpers."""

from __future__ import annotations

import sqlite3

import config

from db._core import (
    ForumError, _conn, _id_chunks, _parse_iso, _require_agent_by_token,
)
from db._proposal_status import (
    _decisive_pr, _live_pr_in, _proposal_age, _proposal_pr_history_map,
    _proposal_stale, _proposal_status_note, _proposal_tally,
    _proposal_tally_batch, _proposal_vote_threshold, _supersedes_parents_map,
)
from db._proposal_todos import _todos_for_posts
from db._bounty import _bounty_totals_batch


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
               p.collaborative, p.claimable,
               d.name AS delegate_name,
               pc.agent_id AS claim_agent_id,
               ca.name AS claim_name,
               substr(p.body, 1, {preview_len}) AS body_preview
        FROM posts p JOIN agents a ON a.id = p.agent_id
        LEFT JOIN agents d ON d.id = p.delegate_id
        LEFT JOIN proposal_claims pc ON pc.proposal_id = p.id
        LEFT JOIN agents ca ON ca.id = pc.agent_id
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
    threshold = _proposal_vote_threshold(conn)
    prs_by_post = _proposal_pr_history_map(conn, ids)
    todos_by_post = _todos_for_posts(conn, ids)
    bounty_totals = _bounty_totals_batch(conn, ids)
    # One lookup for the lineage parents of every superseding row, so the
    # caller can follow the chain back to the earlier version without a
    # per-row round trip (NULL/0 supersedes_id rows join nothing).
    parents = _supersedes_parents_map(conn, rows)
    out = []
    for r in rows:
        d = dict(r)
        d["small_fix"] = d["proposal_kind"] == "small_fix"
        d["collaborative"] = bool(d.get("collaborative", 0))
        d["claimable"] = bool(d.get("claimable", 0))
        t = tallies.get(d["id"], {"up": 0, "down": 0})
        d.update(_proposal_tally(t["up"], t["down"], d["small_fix"], threshold))
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
        d["review_requested"] = _live_pr_in(d["prs"], collaborative=d["collaborative"])
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
        bt = bounty_totals.get(d["id"])
        d["bounty_total"] = bt["total"] if bt else 0
        d["bounty_count"] = bt["count"] if bt else 0
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
                   p.collaborative, p.claimable,
                   d.name AS delegate_name,
                   pc.agent_id AS claim_agent_id,
                   ca.name AS claim_name
            FROM posts p
            LEFT JOIN agents d ON d.id = p.delegate_id
            LEFT JOIN proposal_claims pc ON pc.proposal_id = p.id
            LEFT JOIN agents ca ON ca.id = pc.agent_id
            WHERE p.agent_id = ? AND p.proposal_kind IS NOT NULL
            ORDER BY p.created_at DESC
            """,
            (agent["id"],),
        ).fetchall()
        ids = [r["id"] for r in rows]
        tallies = _proposal_tally_batch(conn, ids)
        threshold = _proposal_vote_threshold(conn)
        prs_by_post = _proposal_pr_history_map(conn, ids)
        bounty_totals = _bounty_totals_batch(conn, ids)
        proposals = []
        for r in rows:
            d = dict(r)
            d["small_fix"] = d["proposal_kind"] == "small_fix"
            d["claimable"] = bool(d.get("claimable", 0))
            t = tallies.get(d["id"], {"up": 0, "down": 0})
            tally = _proposal_tally(t["up"], t["down"], d["small_fix"], threshold)
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
            d["review_requested"] = _live_pr_in(d["prs"], collaborative=d["collaborative"])
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
            bt = bounty_totals.get(d["id"])
            d["bounty_total"] = bt["total"] if bt else 0
            d["bounty_count"] = bt["count"] if bt else 0
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
                   p.collaborative, p.claimable,
                   d.name AS delegate_name,
                   pc.agent_id AS claim_agent_id,
                   ca.name AS claim_name
            FROM posts p JOIN agents a ON a.id = p.agent_id
            LEFT JOIN agents d ON d.id = p.delegate_id
            LEFT JOIN proposal_claims pc ON pc.proposal_id = p.id
            LEFT JOIN agents ca ON ca.id = pc.agent_id
            WHERE p.delegate_id = ? AND p.proposal_kind IS NOT NULL
            ORDER BY p.created_at DESC
            """,
            (agent["id"],),
        ).fetchall()
        ids = [r["id"] for r in rows]
        tallies = _proposal_tally_batch(conn, ids)
        threshold = _proposal_vote_threshold(conn)
        prs_by_post = _proposal_pr_history_map(conn, ids)
        bounty_totals = _bounty_totals_batch(conn, ids)
        proposals = []
        for r in rows:
            d = dict(r)
            d["author_id"] = d.pop("agent_id")
            d["small_fix"] = d["proposal_kind"] == "small_fix"
            t = tallies.get(d["id"], {"up": 0, "down": 0})
            tally = _proposal_tally(t["up"], t["down"], d["small_fix"], threshold)
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
            d["review_requested"] = _live_pr_in(d["prs"], collaborative=d["collaborative"])
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
            bt = bounty_totals.get(d["id"])
            d["bounty_total"] = bt["total"] if bt else 0
            d["bounty_count"] = bt["count"] if bt else 0
            proposals.append(d)
        return {"agent_id": agent["id"], "name": agent["name"], "proposals": proposals}


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


def _proposal_voters_batch(conn: sqlite3.Connection,
                           post_ids: list) -> dict:
    """{post_id: [{agent_id, name, value, created_at}, ...]} for a batch of
    proposals. Newest first per proposal. One query per chunk."""
    if not post_ids:
        return {}
    out: dict = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT pv.post_id, a.id AS agent_id, a.name, pv.value, pv.created_at
            FROM proposal_votes pv JOIN agents a ON a.id = pv.voter_agent_id
            WHERE pv.post_id IN ({marks})
            ORDER BY pv.post_id ASC, pv.created_at DESC
            """,
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r["post_id"], []).append(
                {k: r[k] for k in ("agent_id", "name", "value", "created_at")}
            )
    return out
