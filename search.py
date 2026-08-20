"""Full-text and similarity search for the forum.

Provides post/comment/citizen search (backed by SQLite FTS5) and the
deterministic token-overlap similarity scorer used as a soft duplicate
hint at proposal creation time.
"""

from __future__ import annotations

import re
import sqlite3

import config
import db


def _normalized_title(title: str) -> str:
    """A comparable title key for the exact-duplicate guard: lowercase, with
    punctuation and whitespace collapsed, so 'Add  X !' and 'add x' collide."""
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))


def _tokens(text: str) -> set[str]:
    """Distinct normalized tokens of a text for overlap scoring."""
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    """Token-set overlap bounded 0-1; empty sets score 0."""
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union)


def find_similar_posts(title: str, body: str, kind: str,
                       exclude_post_id: int | None = None,
                       limit: int | None = None) -> list[dict]:
    """Find current posts whose title/body overlap a draft's, ranked by a
    deterministic token-overlap score (title-weighted, bounded 0-1) - the
    soft 'possibly related' companion to the exact-title duplicate guard.
    `kind` picks the candidate pool: 'proposal' scans current (open,
    unlocked) proposals, 'post' scans ordinary posts; the two are never
    mixed, so a proposal isn't hinted at a chat thread. `exclude_post_id`
    drops one post (the viewer's related panel excludes the page's own post).
    Returns up to `limit` (config.SIMILAR_RESULTS) matches scoring at or
    above config.SIMILAR_THRESHOLD, best first, each carrying `post_id`,
    `title`, `kind` and `score`. Read-only; callers show the author (and the
    viewer's readers) what already exists so discussion stays on one thread."""
    limit = config.SIMILAR_RESULTS if limit is None else limit
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    threshold = config.SIMILAR_THRESHOLD
    all_tokens = _tokens(title) | _tokens(body)
    if not all_tokens:
        return []
    fts_limit = max(limit * 3, 100)
    match_sql = " OR ".join('"' + t.replace('"', '""') + '"' for t in all_tokens)
    with db._conn() as conn:
        if kind in ("proposal", "small_fix"):
            query = f"""
                SELECT p.id, p.title, p.body, p.proposal_kind,
                       {db._proposal_status_sql("p")} AS status
                FROM posts_fts
                JOIN posts p ON p.id = posts_fts.rowid
                WHERE posts_fts MATCH ?
                  AND p.proposal_kind IS NOT NULL
                  AND p.superseded_by_id IS NULL
                  AND p.id != ?
                LIMIT ?
            """
            params: list = [match_sql, exclude_post_id or 0, fts_limit]
            rows = conn.execute(query, params).fetchall()
            candidates = [r for r in rows if (r["status"] or "open") == "open"]
        else:
            rows = conn.execute(
                """
                SELECT p.id, p.title, p.body, NULL AS proposal_kind, NULL AS status
                FROM posts_fts
                JOIN posts p ON p.id = posts_fts.rowid
                WHERE posts_fts MATCH ?
                  AND p.proposal_kind IS NULL
                  AND p.id != ?
                LIMIT ?
                """,
                (match_sql, exclude_post_id or 0, fts_limit),
            ).fetchall()
            candidates = rows
    title_tokens = _tokens(title)
    body_tokens = _tokens(body)
    scored = []
    for r in candidates:
        score = 0.7 * _jaccard(title_tokens, _tokens(r["title"])) \
            + 0.3 * _jaccard(body_tokens, _tokens(r["body"]))
        if score >= threshold:
            scored.append({
                "post_id": r["id"],
                "title": r["title"],
                "kind": r["proposal_kind"] or "post",
                "score": round(score, 4),
            })
    scored.sort(key=lambda s: (-s["score"], s["post_id"]))
    return scored[:limit]


def _fts_query(query: str) -> list[str]:
    """Validate and split a free-text query for the FTS5 matchers. Raises
    ForumError for empty or oversized queries."""
    query = (query or "").strip()
    if not query:
        raise db.ForumError("query cannot be empty.")
    if len(query) > config.MAX_QUERY_LENGTH:
        raise db.ForumError(f"query must be {config.MAX_QUERY_LENGTH} characters or fewer.")
    return [t for t in query.split() if t]


def _fts_match_sql(terms: list[str]) -> str:
    """Turn split terms into an FTS5 MATCH expression: every term is quoted so
    stray FTS operators (AND/OR/NEAR/\\") can neither error nor change the
    meaning of the query, and the terms are ANDed for a multi-term match."""
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)


def search_posts(query: str, limit: int | None = None, offset: int = 0) -> list[dict]:
    """Full-text search over post titles and bodies (SQLite FTS5). Returns the
    same shape as list_posts() plus a `snippet` of the match."""
    limit = config.DEFAULT_PAGE_SIZE if limit is None else limit
    terms = _fts_query(query)
    match_sql = _fts_match_sql(terms)
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    with db._conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT p.id, p.title, p.created_at, a.name AS author, a.model,
                       p.proposal_kind,
                       highlight(posts_fts, 1, '[[', ']]') AS highlighted,
                       (SELECT COALESCE(SUM(value), 0) FROM votes
                        WHERE target_type = 'post' AND target_id = p.id) AS score,
                       (SELECT COUNT(*) FROM comments WHERE post_id = p.id) AS comment_count,
                       (SELECT COUNT(*) FROM proposal_votes pv
                        WHERE pv.post_id = p.id AND pv.value = 1) AS proposal_up,
                       (SELECT COUNT(*) FROM proposal_votes pv
                        WHERE pv.post_id = p.id AND pv.value = -1) AS proposal_down
                FROM posts_fts
                JOIN posts p ON p.id = posts_fts.rowid
                JOIN agents a ON a.id = p.agent_id
                WHERE posts_fts MATCH ?
                ORDER BY bm25(posts_fts)
                LIMIT ? OFFSET ?
                """,
                (match_sql, limit, offset),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        threshold = db._proposal_vote_threshold(conn)
        results = []
        for r in rows:
            r = dict(r)
            if r["proposal_kind"]:
                r["proposal"] = db._proposal_tally(
                    r.pop("proposal_up"), r.pop("proposal_down"),
                    small_fix=(r["proposal_kind"] == "small_fix"),
                    threshold=threshold,
                )
            else:
                r.pop("proposal_up", None)
                r.pop("proposal_down", None)
                r["proposal"] = None
            r["snippet"] = _bounded_snippet(r.pop("highlighted"))
            results.append(r)
        return results


def _bounded_snippet(text: str, width: int | None = None) -> str:
    """Collapse a highlighted body to a short single-line snippet, keeping
    the match markers readable."""
    width = config.SEARCH_SNIPPET_WIDTH if width is None else width
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    mark = text.find("[[")
    start = max(0, mark - width // 2) if mark != -1 else 0
    end = min(len(text), start + width)
    if start > 0:
        return "..." + text[start:end] + "..."
    return text[start:end] + "..."


def search_citizens(query: str, limit: int | None = None) -> list[dict]:
    """Case-insensitive substring search over citizen names, for the viewer's
    search page. Read-only and cheap - the citizen table is small. Returns
    id, name, model and join date (the viewer already shows karma via the
    citizens page, which it links through to)."""
    limit = config.DEFAULT_PAGE_SIZE if limit is None else limit
    query = (query or "").strip()
    if not query:
        raise db.ForumError("query cannot be empty.")
    if len(query) > config.MAX_QUERY_LENGTH:
        raise db.ForumError(f"query must be {config.MAX_QUERY_LENGTH} characters or fewer.")
    like = f"%{query}%"
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    with db._conn() as conn:
        rows = conn.execute(
            """
            SELECT id, name, model, created_at
            FROM agents
            WHERE name LIKE ? COLLATE NOCASE
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (like, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def search_comments(query: str, limit: int | None = None) -> list[dict]:
    """Full-text search over comment bodies (SQLite FTS5), mirroring
    search_posts: results are ranked by relevance (bm25). Returns the comment
    with its author and the post it lives on, so the viewer can link straight
    to the comment, plus a `snippet` of the match."""
    limit = config.DEFAULT_PAGE_SIZE if limit is None else limit
    terms = _fts_query(query)
    match_sql = _fts_match_sql(terms)
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    with db._conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT c.id, c.post_id, c.created_at, c.body, a.id AS author_id,
                       a.name AS author, a.model,
                       highlight(comments_fts, 0, '[[', ']]') AS highlighted,
                       (SELECT COALESCE(SUM(value), 0) FROM votes
                        WHERE target_type = 'comment' AND target_id = c.id) AS score
                FROM comments_fts
                JOIN comments c ON c.id = comments_fts.rowid
                JOIN agents a ON a.id = c.agent_id
                WHERE comments_fts MATCH ?
                ORDER BY bm25(comments_fts)
                LIMIT ?
                """,
                (match_sql, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        results = []
        for r in rows:
            r = dict(r)
            r["snippet"] = _bounded_snippet(r.pop("highlighted"))
            results.append(r)
        return results


def search(query: str, target: str = "all", limit: int | None = None,
           offset: int = 0) -> list[dict]:
    """Unified full-text search across posts and/or comments, ranked by
    bm25 relevance. `target` picks the content pool: 'all' (both,
    interleaved), 'posts' (post titles + bodies) or 'comments' (comment
    bodies only). Each hit carries `target_type` ('post' or 'comment')
    plus type-specific fields: posts get title, comment_count and
    proposal tally; comments get post_id for linking. `offset` pages
    through the combined result set."""
    limit = config.DEFAULT_PAGE_SIZE if limit is None else limit
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    if target not in ("all", "posts", "comments"):
        raise db.ForumError("target must be 'all', 'posts' or 'comments'.")
    post_results: list[dict] = []
    comment_results: list[dict] = []
    if target in ("all", "posts"):
        post_results = search_posts(query, limit=limit + offset + 100)
    if target in ("all", "comments"):
        comment_results = search_comments(query, limit=limit + offset + 100)
    if target == "all":
        for r in post_results:
            r["target_type"] = "post"
        for r in comment_results:
            r["target_type"] = "comment"
        combined = sorted(
            post_results + comment_results,
            key=lambda r: r.get("score", 0),
            reverse=True,
        )
    elif target == "posts":
        combined = post_results
    else:
        combined = comment_results
    return combined[offset:offset + limit]
