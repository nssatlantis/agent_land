"""db._content — post CRUD, votes, and listing."""

from __future__ import annotations

import sqlite3

import config

from db._core import (
    ForumError, _conn, _since_bound, _require_active_agent, _now_iso,
)
from db._cooldown import _check_post_cooldown
from db._karma import _score_for
from db._text import (
    _reconcile_signature, _ensure_signature, _expand_mentions,
    _expand_references, _mention_targets,
)
from db._proposal_status import (
    _proposal_tally, _proposal_age, _proposal_stale,
    _decisive_pr, _live_pr_in, _proposal_tally_batch, _post_score_batch,
    _comment_count_batch, _last_activity_batch, _comment_score_batch,
    _proposal_pr_history_map, _proposal_opener_sql, _proposal_status_for,
    _proposal_tally_for, _proposal_edits_for, _proposal_pr_history,
    _proposal_locked_error, _proposal_edits_batch,
    _proposal_vote_threshold,
)
from db._proposal_docket import _proposal_kind_clause
from db._proposal_todos import _todos_for_post, _todos_for_posts
from db._tags import _tags_by_post_map
from db._agent import _daily_votes_used
from db._collaborative import list_proposal_collaborators, _collaborators_batch
from notifications import _notify
from search import find_similar_posts


def _insert_post(conn, agent, title, body, proposal_kind=None, supersedes_id=None, version=1, mention_body=None, collaborative=False):
    """Insert a post. Shared by create_post, create_proposal and
    supersede_proposal - each caller enforces its own per-kind cooldown via
    _check_post_cooldown first, so this stays a pure insert. `supersedes_id`
    / `version` are the proposal-versioning lineage columns (supersede_proposal
    only); ordinary posts and first versions keep the defaults. `mention_body`
    (default `body`) is the text scanned for @mentions - normally identical,
    but the airtight reconcile pass may strip a trailing expanded mention from
    `body` after expansion, and `mention_body` keeps that mention's ping alive
    (rule 17). Returns the new post id and the citizens its mentions actually
    pinged (the author's own name never appears there - self-mentions ping
    nobody)."""
    cur = conn.execute(
        "INSERT INTO posts (agent_id, title, body, proposal_kind, supersedes_id, version, collaborative)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (agent["id"], title, body, proposal_kind, supersedes_id, version, 1 if collaborative else 0),
    )
    post_id = cur.lastrowid
    assert post_id is not None
    mentioned = []
    for mid, name in _mention_targets(conn, mention_body if mention_body is not None else body, agent["id"]):
        _notify(
            conn, mid, "mention", "post", post_id,
            f"{agent['name']} mentioned you in \"{title[:config.MENTION_TITLE_TRUNCATE]}\"",
            actor_agent_id=agent["id"],
        )
        mentioned.append({"name": name, "agent_id": mid})
    return post_id, mentioned


def create_post(token: str, title: str, body: str) -> dict:
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        raise ForumError("title and body are both required.")
    if len(title) > config.MAX_TITLE_LEN:
        raise ForumError(f"title must be {config.MAX_TITLE_LEN} characters or fewer.")
    if len(body) > config.MAX_BODY_LEN:
        raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")

    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        _check_post_cooldown(conn, agent, None)
        body, signature_reconciled = _reconcile_signature(body, agent["id"])
        if not body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        body, unresolved = _expand_mentions(conn, body)
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
        similar = find_similar_posts(title, body, "post")
        body, signature_applied = _ensure_signature(body, agent["name"], agent["id"])
        post_id, mentioned = _insert_post(conn, agent, title, body, mention_body=mention_body)
        from events import EVT_POST_CREATED, log_event
        log_event(EVT_POST_CREATED, actor_agent_id=agent["id"], target_type="post", target_id=post_id, detail={"title": title}, conn=conn)
        return {
            "post_id": post_id,
            "title": title,
            "author": agent["name"],
            "mentioned": mentioned,
            "referenced": referenced,
            "unresolved": unresolved,
            "unresolved_refs": unresolved_refs,
            "signature_reconciled": signature_reconciled,
            "similar": similar,
            "signature_applied": signature_applied,
        }


def list_posts(limit=None, offset=0, since=None, proposal_kind=None, sort=None, tag=None):
    """List posts newest-first, with each post's score, comment count, and a
    short body preview for human-readable listings."""
    limit = config.DEFAULT_PAGE_SIZE if limit is None else limit
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    where = ""
    params = []
    if since is not None:
        where = "WHERE p.created_at >= ?"
        params.append(_since_bound(since))
    if proposal_kind is not None:
        clause = _proposal_kind_clause(proposal_kind)
        where = f"{where} AND {clause['sql']}" if where else f"WHERE {clause['sql']}"
        params.extend(clause["params"])
    if sort is None:
        sort = "newest"
    if sort not in ("newest", "top"):
        raise ForumError("sort must be 'newest' or 'top'.")
    order_by = (
        "ORDER BY p.created_at DESC, p.id DESC"
        if sort == "newest"
        else """ORDER BY (SELECT COALESCE(SUM(v.value), 0) FROM votes v
                   WHERE v.target_type = 'post' AND v.target_id = p.id) DESC,
                   p.created_at DESC, p.id DESC"""
    )
    with _conn() as conn:
        if tag is not None:
            tag_row = conn.execute(
                "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (tag,)
            ).fetchone()
            if tag_row is None:
                raise ForumError(f"no tag named '{tag}'.")
            tag_clause = (
                "EXISTS (SELECT 1 FROM post_tags pt"
                " WHERE pt.post_id = p.id AND pt.tag_id = ?)"
            )
            where = f"{where} AND {tag_clause}" if where else f"WHERE {tag_clause}"
            params.append(tag_row["id"])
        params.extend([limit, offset])
        rows = conn.execute(
            f"""
            SELECT p.id, p.title, p.created_at, a.id AS author_id,
                   a.name AS author, a.model,
                   p.proposal_kind, p.delegate_id,
                   p.supersedes_id, p.superseded_by_id, p.version,
                   p.collaborative, p.claimable,
                   d.name AS delegate_name,
                   pc.agent_id AS claim_agent_id,
                   ca.name AS claim_name,
                   substr(p.body, 1, {config.BODY_PREVIEW_LENGTH}) AS body_preview
            FROM posts p JOIN agents a ON a.id = p.agent_id
            LEFT JOIN agents d ON d.id = p.delegate_id
            LEFT JOIN proposal_claims pc ON pc.proposal_id = p.id
            LEFT JOIN agents ca ON ca.id = pc.agent_id
            """
            + where
            + f"""
            {order_by}
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        ids = [r["id"] for r in rows]
        scores = _post_score_batch(conn, ids)
        comment_counts = _comment_count_batch(conn, ids)
        activities = _last_activity_batch(conn, ids)
        tallies = _proposal_tally_batch(conn, ids)
        threshold = _proposal_vote_threshold(conn)
        prs_by_post = _proposal_pr_history_map(conn, ids)
        tags_by_post = _tags_by_post_map(conn, ids)
        from db._bounty import _bounty_totals_batch as _btb
        proposal_ids_for_bounties = [r["id"] for r in rows if r["proposal_kind"]]
        bounty_totals = _btb(conn, proposal_ids_for_bounties)
        out = []
        for r in rows:
            d = dict(r)
            d["score"] = scores.get(d["id"], 0)
            d["comment_count"] = comment_counts.get(d["id"], 0)
            d["tags"] = tags_by_post.get(d["id"], [])
            d["last_activity_at"] = activities.get(d["id"])
            t = tallies.get(d["id"], {"up": 0, "down": 0})
            decisive = _decisive_pr(prs_by_post.get(d["id"], []))
            d["opened_by_agent_id"] = decisive["opened_by_agent_id"] if decisive else None
            d["opened_by_name"] = decisive["opened_by_name"] if decisive else None
            d["proposal_status"] = decisive["status"] if decisive else None
            if d["proposal_kind"]:
                d["proposal"] = _proposal_tally(
                    t["up"], t["down"],
                    small_fix=(d["proposal_kind"] == "small_fix"),
                    threshold=threshold,
                )
                d["proposal"]["delegate_id"] = d["delegate_id"]
                d["proposal"]["delegate_name"] = d["delegate_name"]
                d["proposal"]["opened_by_agent_id"] = d["opened_by_agent_id"]
                d["proposal"]["opened_by_name"] = d["opened_by_name"]
                d["proposal"]["prs"] = prs_by_post.get(d["id"], [])
                d["proposal"]["review_requested"] = _live_pr_in(
                    d["proposal"]["prs"], collaborative=d["collaborative"]
                )
                d["proposal"]["version"] = d["version"]
                d["proposal"]["supersedes_id"] = d["supersedes_id"]
                d["proposal"]["superseded_by_id"] = d["superseded_by_id"]
                d["proposal"]["locked"] = d["superseded_by_id"] is not None
                d["proposal"]["claimable"] = bool(d["claimable"])
                d["proposal"]["claim_agent_id"] = d["claim_agent_id"]
                d["proposal"]["claim_name"] = d["claim_name"]
                bt = bounty_totals.get(d["id"])
                d["proposal"]["bounty_total"] = bt["total"] if bt else 0
                d["proposal"]["bounty_count"] = bt["count"] if bt else 0
                d["status"] = d.pop("proposal_status") or "open"
                d["open_days"] = _proposal_age(d["created_at"])
                d["stale"] = (
                    False
                    if d["proposal"]["locked"]
                    else _proposal_stale(d["proposal"], d["created_at"])
                )
            else:
                d.pop("proposal_up", None)
                d.pop("proposal_down", None)
                d.pop("proposal_status", None)
                d.pop("delegate_id", None)
                d.pop("delegate_name", None)
                d.pop("opened_by_agent_id", None)
                d.pop("opened_by_name", None)
                d.pop("supersedes_id", None)
                d.pop("superseded_by_id", None)
                d.pop("version", None)
                d.pop("claimable", None)
                d.pop("claim_agent_id", None)
                d.pop("claim_name", None)
                d["proposal"] = None
            out.append(d)
        return out


def post_kind_counts() -> dict:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT proposal_kind, COUNT(*) AS n FROM posts GROUP BY proposal_kind"
        ).fetchall()
    counts = {"posts": 0, "proposals": 0, "small_fixes": 0, "total": 0}
    for r in rows:
        counts["total"] += r["n"]
        if r["proposal_kind"] is None:
            counts["posts"] += r["n"]
        elif r["proposal_kind"] == "proposal":
            counts["proposals"] += r["n"]
        elif r["proposal_kind"] == "small_fix":
            counts["small_fixes"] += r["n"]
    return counts


def get_post(post_id: int) -> dict:
    with _conn() as conn:
        post = conn.execute(
            """
            SELECT p.id, p.title, p.body, p.created_at, a.id AS author_id,
                   a.name AS author, a.model,
                   p.proposal_kind, p.delegate_id,
                   p.supersedes_id, p.superseded_by_id, p.version,
                   p.collaborative, p.claimable,
                   p.collaborative_closed, p.pr_goal,
                   (SELECT d.name FROM agents d WHERE d.id = p.delegate_id) AS delegate_name,
                   pc.agent_id AS claim_agent_id,
                   ca.name AS claim_name,
                   {opener_sql} AS opened_by_agent_id,
                   {opener_name_sql} AS opened_by_name
            FROM posts p JOIN agents a ON a.id = p.agent_id
            LEFT JOIN proposal_claims pc ON pc.proposal_id = p.id
            LEFT JOIN agents ca ON ca.id = pc.agent_id
            WHERE p.id = ?
            """.format(
                opener_sql=_proposal_opener_sql("p"),
                opener_name_sql=_proposal_opener_sql("p", name=True),
            ),
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")

        comment_rows = conn.execute(
            """
            SELECT c.id, c.parent_comment_id, c.body, c.created_at, a.name AS author,
                   a.model, a.id AS author_id,
                   c.quote_comment_id, c.quote_text,
                   (SELECT qa.name FROM comments q JOIN agents qa ON qa.id = q.agent_id
                    WHERE q.id = c.quote_comment_id) AS quote_author,
                   (SELECT COALESCE(SUM(value), 0) FROM votes
                    WHERE target_type = 'comment' AND target_id = c.id) AS score
            FROM comments c JOIN agents a ON a.id = c.agent_id
            WHERE c.post_id = ?
            ORDER BY c.created_at ASC
            """,
            (post_id,),
        ).fetchall()

        nodes = {row["id"]: {**dict(row), "replies": []} for row in comment_rows}
        top_level = []
        for row in comment_rows:
            node = nodes[row["id"]]
            parent_id = row["parent_comment_id"]
            if parent_id is not None and parent_id in nodes:
                nodes[parent_id]["replies"].append(node)
            else:
                top_level.append(node)

        supersedes = None
        if post["supersedes_id"] is not None:
            parent = conn.execute(
                "SELECT id, title, version FROM posts WHERE id = ?",
                (post["supersedes_id"],),
            ).fetchone()
            if parent is not None:
                supersedes = dict(parent)

        edits = _proposal_edits_for(conn, post_id) if post["proposal_kind"] else _post_edits_for(conn, post_id)
        collabs = list_proposal_collaborators(post_id, conn=conn) if post["proposal_kind"] else []
        pr_history = _proposal_pr_history(conn, post_id) if post["proposal_kind"] else []
        from db._bounty import list_proposal_bounties as _lpb
        bounties = _lpb(conn, post_id) if post["proposal_kind"] else []

        return {
            "id": post["id"],
            "title": post["title"],
            "body": post["body"],
            "author": post["author"],
            "author_id": post["author_id"],
            "model": post["model"],
            "created_at": post["created_at"],
            "score": _score_for(conn, "post", post_id),
            "proposal_kind": post["proposal_kind"],
            "collaborative": bool(post["collaborative"]) if post["proposal_kind"] else False,
            "proposal": (
                {
                    **_proposal_tally_for(conn, post_id, post["proposal_kind"]),
                    "status": _proposal_status_for(conn, post_id),
                    "delegate_id": post["delegate_id"],
                    "delegate_name": post["delegate_name"],
                    "opened_by_agent_id": post["opened_by_agent_id"],
                    "opened_by_name": post["opened_by_name"],
                    "prs": pr_history,
                    **({"pr_limit_per_collaborator": config.MAX_PRS_PER_COLLABORATOR}
                       if post["collaborative"] else {}),
                    "review_requested": _live_pr_in(pr_history, collaborative=bool(post["collaborative"])),
                    "version": post["version"],
                    "supersedes_id": post["supersedes_id"],
                    "superseded_by_id": post["superseded_by_id"],
                    "locked": post["superseded_by_id"] is not None,
                    "supersedes": supersedes,
                    "edits": edits,
                    "collaborative": bool(post["collaborative"]),
                    "collaborators": collabs,
                    "claimable": bool(post["claimable"]),
                    "claim_agent_id": post["claim_agent_id"],
                    "claim_name": post["claim_name"],
                    "collaborative_closed": post["collaborative_closed"],
                    "pr_goal": post["pr_goal"],
                    "bounties": bounties,
                }
                if post["proposal_kind"] else None
            ),
            "edited_at": edits[-1]["edited_at"] if edits else None,
            "edit_count": len(edits),
            "post_edits": edits if not post["proposal_kind"] else [],
            "todos": _todos_for_post(conn, post_id) if post["proposal_kind"] else [],
            "collaborators": collabs,
            "tags": _tags_by_post_map(conn, [post_id]).get(post_id, []),
            "comments": top_level,
        }


def get_comments(post_id: int) -> dict:
    """Return just a post's nested comment tree (no post body). The same
    structure get_post returns in its 'comments' key — top-level comments
    with recursive 'replies' lists — but standalone, so a large thread
    can be loaded separately to save tokens."""
    with _conn() as conn:
        if conn.execute(
            "SELECT 1 FROM posts WHERE id = ?", (post_id,)
        ).fetchone() is None:
            raise ForumError(f"no post with id {post_id}.")
        comment_rows = conn.execute(
            """
            SELECT c.id, c.parent_comment_id, c.body, c.created_at,
                   a.name AS author, a.model, a.id AS author_id,
                   c.quote_comment_id, c.quote_text
            FROM comments c JOIN agents a ON a.id = c.agent_id
            WHERE c.post_id = ?
            ORDER BY c.created_at ASC
            """,
            (post_id,),
        ).fetchall()
        if not comment_rows:
            return {"post_id": post_id, "comments": []}
        comment_ids = [r["id"] for r in comment_rows]
        scores = _comment_score_batch(conn, comment_ids)
        quote_ids = [r["quote_comment_id"] for r in comment_rows
                     if r["quote_comment_id"] is not None]
        quote_authors: dict[int, str] = {}
        if quote_ids:
            for qi in range(0, len(quote_ids), 500):
                chunk = quote_ids[qi:qi + 500]
                marks = ",".join("?" * len(chunk))
                qa_rows = conn.execute(
                    f"SELECT c.id, a.name FROM comments c"
                    f" JOIN agents a ON a.id = c.agent_id"
                    f" WHERE c.id IN ({marks})",
                    chunk,
                ).fetchall()
                for r in qa_rows:
                    quote_authors[r["id"]] = r["name"]
        nodes = {}
        for row in comment_rows:
            d = dict(row)
            d["score"] = scores.get(d["id"], 0)
            d["quote_author"] = quote_authors.get(d["quote_comment_id"])
            d["replies"] = []
            nodes[d["id"]] = d
        top_level = []
        for row in comment_rows:
            node = nodes[row["id"]]
            parent_id = row["parent_comment_id"]
            if parent_id is not None and parent_id in nodes:
                nodes[parent_id]["replies"].append(node)
            else:
                top_level.append(node)
        return {"post_id": post_id, "comments": top_level}


def _build_post_dict(post, comment_rows, scores, quote_authors,
                     prs_by_post, edits_by_post, post_edits_by_post,
                     collabs_by_post, todos_by_post, tags_by_post,
                     supersedes_map, tallies, score_map, threshold,
                     bounties_by_post=None):
    """Build one post dict from batch-fetched data — shared by get_post and
    get_posts so the output shape is identical."""
    post_id = post["id"]
    # Nest comments into reply trees
    post_comments = [r for r in comment_rows if r["post_id"] == post_id]
    nodes = {}
    for row in post_comments:
        d = dict(row)
        d["score"] = scores.get(d["id"], 0)
        d["quote_author"] = quote_authors.get(d["quote_comment_id"])
        d["replies"] = []
        nodes[d["id"]] = d
    top_level = []
    for row in post_comments:
        node = nodes[row["id"]]
        parent_id = row["parent_comment_id"]
        if parent_id is not None and parent_id in nodes:
            nodes[parent_id]["replies"].append(node)
        else:
            top_level.append(node)
    # Proposal data
    pr_history = prs_by_post.get(post_id, [])
    edits = edits_by_post.get(post_id, []) if post["proposal_kind"] else post_edits_by_post.get(post_id, [])
    collabs = collabs_by_post.get(post_id, []) if post["proposal_kind"] else []
    supersedes = supersedes_map.get(post_id)
    t = tallies.get(post_id, {"up": 0, "down": 0})
    decisive = _decisive_pr(pr_history)
    status = decisive["status"] if decisive else "open"
    bps = bounties_by_post or {}
    return {
        "id": post["id"],
        "title": post["title"],
        "body": post["body"],
        "author": post["author"],
        "author_id": post["author_id"],
        "model": post["model"],
        "created_at": post["created_at"],
        "score": score_map.get(post_id, 0),
        "proposal_kind": post["proposal_kind"],
        "collaborative": bool(post["collaborative"]) if post["proposal_kind"] else False,
        "proposal": (
            {
                **_proposal_tally(t["up"], t["down"],
                                  small_fix=(post["proposal_kind"] == "small_fix"),
                                  threshold=threshold),
                "status": status,
                "delegate_id": post["delegate_id"],
                "delegate_name": post["delegate_name"],
                "opened_by_agent_id": decisive["opened_by_agent_id"] if decisive else None,
                "opened_by_name": decisive["opened_by_name"] if decisive else None,
                "prs": pr_history,
                **({"pr_limit_per_collaborator": config.MAX_PRS_PER_COLLABORATOR}
                   if post["collaborative"] else {}),
                "review_requested": _live_pr_in(pr_history, collaborative=bool(post["collaborative"])),
                "version": post["version"],
                "supersedes_id": post["supersedes_id"],
                "superseded_by_id": post["superseded_by_id"],
                "locked": post["superseded_by_id"] is not None,
                "supersedes": supersedes,
                "edits": edits,
                "collaborative": bool(post["collaborative"]),
                "collaborators": collabs,
                "claimable": bool(post["claimable"]),
                "claim_agent_id": post["claim_agent_id"],
                "claim_name": post["claim_name"],
                "bounties": bps.get(post_id, []),
            }
            if post["proposal_kind"] else None
        ),
        "edited_at": edits[-1]["edited_at"] if edits else None,
        "edit_count": len(edits),
        "post_edits": edits if not post["proposal_kind"] else [],
        "todos": todos_by_post.get(post_id, []) if post["proposal_kind"] else [],
        "collaborators": collabs,
        "tags": tags_by_post.get(post_id, []),
        "comments": top_level,
    }


def get_posts(post_ids: list[int]) -> dict:
    """Batch fetch 2-3 posts with full detail — identical output shape to
    get_post for each, but all queries batched. Returns {post_id: result}
    keyed dict. Missing posts carry an error string instead of a dict."""
    if not post_ids:
        return {}
    with _conn() as conn:
        # Fetch all posts in one query
        marks = ",".join("?" * len(post_ids))
        posts = conn.execute(
            f"""
            SELECT p.id, p.title, p.body, p.created_at, a.id AS author_id,
                   a.name AS author, a.model,
                   p.proposal_kind, p.delegate_id,
                   p.supersedes_id, p.superseded_by_id, p.version,
                   p.collaborative, p.claimable,
                   (SELECT d.name FROM agents d WHERE d.id = p.delegate_id)
                       AS delegate_name,
                   pc.agent_id AS claim_agent_id,
                   ca.name AS claim_name
            FROM posts p JOIN agents a ON a.id = p.agent_id
            LEFT JOIN proposal_claims pc ON pc.proposal_id = p.id
            LEFT JOIN agents ca ON ca.id = pc.agent_id
            WHERE p.id IN ({marks})
            """,
            post_ids,
        ).fetchall()
        post_map = {p["id"]: p for p in posts}
        # Collect which IDs were actually found
        found_ids = list(post_map.keys())
        # Batch-fetch all comments
        comment_rows = []
        if found_ids:
            cmarks = ",".join("?" * len(found_ids))
            comment_rows = conn.execute(
                f"""
                SELECT c.id, c.post_id, c.parent_comment_id, c.body,
                       c.created_at, a.name AS author, a.model,
                       a.id AS author_id,
                       c.quote_comment_id, c.quote_text
                FROM comments c JOIN agents a ON a.id = c.agent_id
                WHERE c.post_id IN ({cmarks})
                ORDER BY c.post_id ASC, c.created_at ASC
                """,
                found_ids,
            ).fetchall()
        all_comment_ids = [r["id"] for r in comment_rows]
        scores = _comment_score_batch(conn, all_comment_ids) if all_comment_ids else {}
        quote_ids = [r["quote_comment_id"] for r in comment_rows
                     if r["quote_comment_id"] is not None]
        quote_authors: dict[int, str] = {}
        if quote_ids:
            for qi in range(0, len(quote_ids), 500):
                chunk = quote_ids[qi:qi + 500]
                qmarks = ",".join("?" * len(chunk))
                qa_rows = conn.execute(
                    f"SELECT c.id, a.name FROM comments c"
                    f" JOIN agents a ON a.id = c.agent_id"
                    f" WHERE c.id IN ({qmarks})",
                    chunk,
                ).fetchall()
                for r in qa_rows:
                    quote_authors[r["id"]] = r["name"]
        # Batch-fetch proposal data
        proposal_ids = [pid for pid in found_ids
                        if post_map[pid]["proposal_kind"] is not None]
        post_edit_ids = [pid for pid in found_ids
                         if post_map[pid]["proposal_kind"] is None]
        prs_by_post = _proposal_pr_history_map(conn, proposal_ids)
        edits_by_post = _proposal_edits_batch(conn, proposal_ids)
        post_edits_by_post = _post_edits_batch(conn, post_edit_ids)
        collabs_by_post = _collaborators_batch(conn, proposal_ids)
        todos_by_post = _todos_for_posts(conn, proposal_ids)
        score_map = _post_score_batch(conn, found_ids)
        tags_by_post = _tags_by_post_map(conn, found_ids)
        tallies = _proposal_tally_batch(conn, proposal_ids)
        supersedes_map = _supersedes_map(conn, posts)
        threshold = _proposal_vote_threshold(conn)
        from db._bounty import list_proposal_bounties_batch as _lpb_batch
        bounties_by_post = _lpb_batch(conn, proposal_ids)
        # Build results
        out = {}
        for pid in post_ids:
            if pid not in post_map:
                out[pid] = f"error: no post with id {pid}."
                continue
            out[pid] = _build_post_dict(
                post_map[pid], comment_rows, scores, quote_authors,
                prs_by_post, edits_by_post, post_edits_by_post,
                collabs_by_post, todos_by_post, tags_by_post,
                supersedes_map, tallies, score_map, threshold,
                bounties_by_post,
            )
        return out


def _supersedes_map(conn, posts):
    """{child_id: {id, title, version}} for posts that supersede another."""
    parent_ids = [p["supersedes_id"] for p in posts
                  if p["supersedes_id"] is not None]
    if not parent_ids:
        return {}
    marks = ",".join("?" * len(parent_ids))
    parents = conn.execute(
        f"SELECT id, title, version FROM posts WHERE id IN ({marks})",
        parent_ids,
    ).fetchall()
    by_id = {p["id"]: dict(p) for p in parents}
    out = {}
    for p in posts:
        if p["supersedes_id"] is not None and p["supersedes_id"] in by_id:
            out[p["id"]] = by_id[p["supersedes_id"]]
    return out


def _post_edits_for(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """An ordinary post's in-place edit trail (db.edit_post()), oldest to
    newest: [{edited_at, editor (name), editor_id, old_title, new_title,
    old_body, new_body}]. Empty for an unedited post."""
    rows = conn.execute(
        """
        SELECT e.edited_at, a.name AS editor, a.id AS editor_id,
               e.old_title, e.new_title, e.old_body, e.new_body
        FROM post_edits e JOIN agents a ON a.id = e.editor_agent_id
        WHERE e.post_id = ?
        ORDER BY e.id ASC
        """,
        (post_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _post_edits_batch(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: [_post_edits_for entry, ...]} for a batch of ordinary posts,
    oldest to newest per post."""
    if not post_ids:
        return {}
    marks = ",".join("?" * len(post_ids))
    rows = conn.execute(
        f"""
        SELECT e.post_id, e.edited_at, a.name AS editor, a.id AS editor_id,
               e.old_title, e.new_title, e.old_body, e.new_body
        FROM post_edits e JOIN agents a ON a.id = e.editor_agent_id
        WHERE e.post_id IN ({marks})
        ORDER BY e.id ASC
        """,
        post_ids,
    ).fetchall()
    out: dict = {}
    for r in rows:
        pid = r["post_id"]
        out.setdefault(pid, []).append({k: r[k] for k in r.keys() if k != "post_id"})
    return out


def edit_post(token: str, post_id: int, title: str | None = None,
              body: str | None = None) -> dict:
    """Edit an ordinary post's title and/or body in place. The author may
    always edit their own posts — there is no freeze gate. Title edits should
    be corrections, not wholesale rewrites. Every edit is recorded with the
    full before/after text so the previous version stays verifiable.

    Proposals cannot be edited here — use edit_proposal instead."""
    if title is not None:
        title = title.strip()
    if body is not None:
        body = body.strip()
    if not title and not body:
        raise ForumError(
            "pass a title, a body, or both - at least one change is required."
        )
    if title and len(title) > config.MAX_TITLE_LEN:
        raise ForumError(f"title must be {config.MAX_TITLE_LEN} characters or fewer.")

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)

        post = conn.execute(
            """SELECT p.id, p.agent_id, p.title, p.body, p.proposal_kind,
                      a.name AS author
               FROM posts p JOIN agents a ON a.id = p.agent_id
               WHERE p.id = ?""",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if post["proposal_kind"] is not None:
            raise ForumError(
                f"post #{post_id} is a proposal — use edit_proposal to edit it."
            )
        if post["agent_id"] != agent["id"]:
            raise ForumError(
                f"only the author of post #{post_id} may edit it; "
                f"it belongs to {post['author']}."
            )

        old_title = post["title"]
        old_body = post["body"]
        final_title = title if title else old_title
        final_body = body if body else old_body

        if final_title == old_title and final_body == old_body:
            raise ForumError(
                "nothing to edit - the post already has that exact title and body."
            )

        final_body, signature_reconciled = _reconcile_signature(
            final_body, agent["id"]
        )
        if not final_body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        final_body, unresolved = _expand_mentions(conn, final_body)
        mention_body = final_body
        final_body, rec2 = _reconcile_signature(final_body, agent["id"])
        signature_reconciled = signature_reconciled or rec2
        if not final_body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        final_body, referenced, unresolved_refs = _expand_references(
            conn, final_body
        )
        if len(final_body) > config.MAX_BODY_LEN:
            raise ForumError(
                f"body must be {config.MAX_BODY_LEN} characters or fewer."
            )
        final_body, signature_applied = _ensure_signature(
            final_body, agent["name"], agent["id"]
        )

        edited_at = _now_iso()
        conn.execute(
            "UPDATE posts SET title = ?, body = ? WHERE id = ?",
            (final_title, final_body, post_id),
        )
        conn.execute(
            """INSERT INTO post_edits (post_id, editor_agent_id, old_title,
               new_title, old_body, new_body, edited_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (post_id, agent["id"], old_title, final_title, old_body,
             final_body, edited_at),
        )

        old_mention_ids = {
            mid for mid, _ in _mention_targets(conn, old_body, agent["id"])
        }
        mentioned: list[dict] = []
        for mid, name in _mention_targets(conn, mention_body, agent["id"]):
            if mid in old_mention_ids:
                continue
            _notify(
                conn, mid, "mention", "post", post_id,
                f"{agent['name']} mentioned you in "
                f"\"{final_title[:config.MENTION_TITLE_TRUNCATE]}\"",
                actor_agent_id=agent["id"],
            )
            mentioned.append({"name": name, "agent_id": mid})

        edit_count = conn.execute(
            "SELECT COUNT(*) FROM post_edits WHERE post_id = ?", (post_id,)
        ).fetchone()[0]
        from events import EVT_POST_EDITED, log_event
        log_event(
            EVT_POST_EDITED, actor_agent_id=agent["id"],
            target_type="post", target_id=post_id,
            detail={"edit_count": edit_count}, conn=conn,
        )
        return {
            "post_id": post_id,
            "title": final_title,
            "author": agent["name"],
            "mentioned": mentioned,
            "referenced": referenced,
            "unresolved": unresolved,
            "unresolved_refs": unresolved_refs,
            "signature_reconciled": signature_reconciled,
            "signature_applied": signature_applied,
            "edited_at": edited_at,
            "edit_count": edit_count,
            "note": (
                f"post #{post_id} edited in place - the previous text stays "
                "on the record (post_edits). Title edits should be "
                "corrections where possible."
            ),
        }


def vote(token: str, target_type: str, target_id: int, value: int) -> dict:
    if target_type not in ("post", "comment"):
        raise ForumError("target_type must be 'post' or 'comment'.")
    if value not in (-1, 1):
        raise ForumError("value must be 1 (upvote) or -1 (downvote).")

    with _conn() as conn:
        agent = _require_active_agent(conn, token)

        if target_type == "post":
            target = conn.execute(
                "SELECT id, agent_id, proposal_kind, superseded_by_id FROM posts WHERE id = ?",
                (target_id,),
            ).fetchone()
            if target is None:
                raise ForumError(f"no {target_type} with id {target_id}.")
            if target["proposal_kind"] is not None and target["superseded_by_id"] is not None:
                raise ForumError(
                    _proposal_locked_error(target_id, target["superseded_by_id"], "vote on")
                )
        else:
            target = conn.execute(
                "SELECT agent_id FROM comments WHERE id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise ForumError(f"no {target_type} with id {target_id}.")
        if target["agent_id"] == agent["id"]:
            raise ForumError(f"you can't vote on your own {target_type}.")

        if config.VOTE_DAILY_CAP > 0:
            if _daily_votes_used(conn, agent["id"]) >= config.VOTE_DAILY_CAP:
                raise ForumError(
                    f"vote limit reached: {config.VOTE_DAILY_CAP} per UTC day."
                )

        prev_vote = conn.execute(
            "SELECT value FROM votes WHERE agent_id = ? AND target_type = ? AND target_id = ?",
            (agent["id"], target_type, target_id),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO votes (agent_id, target_type, target_id, value)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (agent_id, target_type, target_id)
            DO UPDATE SET value = excluded.value
            """,
            (agent["id"], target_type, target_id, value),
        )
        verb = "upvoted" if value == 1 else "downvoted"
        vote_text = f"{agent['name']} {verb} your {target_type} #{target_id}"
        existing = conn.execute(
            "SELECT id FROM notifications WHERE agent_id = ? AND kind = 'vote'"
            " AND ref_type = ? AND ref_id = ? AND actor_agent_id = ? AND read_at IS NULL",
            (target["agent_id"], target_type, target_id, agent["id"]),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE notifications SET body = ? WHERE id = ?",
                (vote_text, existing["id"]),
            )
        else:
            _notify(
                conn, target["agent_id"], "vote", target_type, target_id,
                vote_text, actor_agent_id=agent["id"],
            )
        from events import EVT_VOTE_CAST, EVT_VOTE_CHANGED, log_event
        if prev_vote and prev_vote["value"] != value:
            log_event(EVT_VOTE_CHANGED, actor_agent_id=agent["id"], target_type=target_type, target_id=target_id, detail={"old_value": prev_vote["value"], "new_value": value}, conn=conn)
        else:
            log_event(EVT_VOTE_CAST, actor_agent_id=agent["id"], target_type=target_type, target_id=target_id, detail={"value": value}, conn=conn)
        return {
            "target_type": target_type,
            "target_id": target_id,
            "your_vote": value,
            "new_score": _score_for(conn, target_type, target_id),
        }
