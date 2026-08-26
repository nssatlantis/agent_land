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
    title_tokens = _tokens(title)
    body_tokens = _tokens(body)
    all_tokens = title_tokens | body_tokens
    if not all_tokens:
        return []
    # Optimize: cap tokens used in MATCH query to avoid OR explosion.
    # Prefer title tokens (more discriminating) + top body tokens by length.
    match_tokens = list(title_tokens)
    body_sorted = sorted(body_tokens - title_tokens, key=lambda t: (-len(t), t))
    match_tokens.extend(body_sorted[:max(0, 20 - len(match_tokens))])
    # Over-fetch reduced: 5x limit instead of 10x, min 50.
    fts_limit = max(limit * 5, 50)
    match_sql = " OR ".join('"' + t.replace('"', '""') + '"' for t in match_tokens)
    with db._conn() as conn:
        try:
            if kind in ("proposal", "small_fix"):
                rows = conn.execute(
                    """
                    SELECT p.id, p.title, p.body, p.proposal_kind
                    FROM posts_fts
                    JOIN posts p ON p.id = posts_fts.rowid
                    WHERE posts_fts MATCH ?
                      AND p.proposal_kind IS NOT NULL
                      AND p.superseded_by_id IS NULL
                      AND p.id != ?
                      AND NOT EXISTS (
                        SELECT 1 FROM proposal_links WHERE post_id = p.id
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM proposal_outcomes WHERE post_id = p.id
                      )
                    ORDER BY bm25(posts_fts)
                    LIMIT ?
                    """,
                    [match_sql, exclude_post_id or 0, fts_limit],
                ).fetchall()
                candidates = rows
            else:
                rows = conn.execute(
                    """
                    SELECT p.id, p.title, p.body, NULL AS proposal_kind
                    FROM posts_fts
                    JOIN posts p ON p.id = posts_fts.rowid
                    WHERE posts_fts MATCH ?
                      AND p.proposal_kind IS NULL
                      AND p.id != ?
                    ORDER BY bm25(posts_fts)
                    LIMIT ?
                    """,
                    (match_sql, exclude_post_id or 0, fts_limit),
                ).fetchall()
                candidates = rows
        except sqlite3.OperationalError:
            return []
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


def find_similar_comments(post_id: int, body: str,
                          exclude_comment_id: int | None = None,
                          limit: int | None = None) -> list[dict]:
    """Find comments on the same post whose body overlaps a new comment's
    text, ranked by a deterministic Jaccard token-overlap score (bounded
    0-1).  The soft 'possibly duplicate' companion to find_similar_posts,
    carried by create_comment responses.  Scans the same post only -
    cross-post comment similarity would be noisy.  Uses the comments_fts
    FTS5 index for candidate retrieval then scores with raw token overlap.
    `exclude_comment_id` drops one comment (for future use).  Returns up
    to `limit` (config.COMMENT_SIMILAR_RESULTS) matches scoring at or
    above config.COMMENT_SIMILAR_THRESHOLD, best first, each carrying
    `comment_id`, `body` (truncated preview), and `score`.  Read-only;
    the commenter sees the hint but is never blocked."""
    limit = config.COMMENT_SIMILAR_RESULTS if limit is None else limit
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    threshold = config.COMMENT_SIMILAR_THRESHOLD
    body_tokens = _tokens(body)
    if not body_tokens:
        return []
    # Build FTS5 match tokens from the body (cap to avoid OR explosion).
    match_tokens = sorted(body_tokens, key=lambda t: (-len(t), t))[:20]
    match_sql = " OR ".join('"' + t.replace('"', '""') + '"' for t in match_tokens)
    fts_limit = max(limit * 5, 50)
    with db._conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT c.id, c.body
                FROM comments_fts
                JOIN comments c ON c.id = comments_fts.rowid
                WHERE comments_fts MATCH ?
                  AND c.post_id = ?
                  AND c.id != ?
                ORDER BY bm25(comments_fts)
                LIMIT ?
                """,
                (match_sql, post_id, exclude_comment_id or 0, fts_limit),
            ).fetchall()
        except sqlite3.OperationalError:  # domain: degrade-silently - FTS miss means no similar hint
            return []
    scored = []
    for r in rows:
        score = _jaccard(body_tokens, _tokens(r["body"]))
        if score >= threshold:
            preview = r["body"][:120].replace("\n", " ")
            scored.append({
                "comment_id": r["id"],
                "body": preview,
                "score": round(score, 4),
            })
    scored.sort(key=lambda s: (-s["score"], s["comment_id"]))
    return scored[:limit]


def find_similar_prs(pr_number: int | None = None,
                     file_paths: list[str] | None = None,
                     title: str | None = None,
                     body: str | None = None,
                     limit: int | None = None) -> list[dict]:
    """Find open pull requests with overlapping file paths and/or title/body
    tokens, ranked by a deterministic weighted Jaccard score (bounded 0-1).
    The soft 'possibly duplicate in-flight PR' companion to find_similar_posts,
    carried by the similar_prs tool and repo_propose_change responses.

    Pass either ``pr_number`` (fetches that PR's files/title/body from GitHub)
    or explicit ``file_paths``/``title``/``body`` to compare against.  The
    function fetches open PRs from GitHub, computes overlap on changed files,
    title tokens and body tokens, and returns up to ``limit``
    (config.SIMILAR_PRS_RESULTS) matches scoring at or above
    config.SIMILAR_PRS_THRESHOLD, best first, each carrying ``number``,
    ``title``, ``author``, ``file_overlap`` (shared paths) and ``score``.

    Read-only; the caller sees the hint but is never blocked.  Requires the
    ``github`` module (raises ForumError on import failure)."""
    import github as _gh
    limit = config.SIMILAR_PRS_RESULTS if limit is None else limit
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    threshold = config.SIMILAR_PRS_THRESHOLD

    # Resolve the target PR's metadata.
    if pr_number is not None:
        try:
            target_pr = _gh.get_pr(pr_number)
        except Exception:  # domain: degrade-silently - unfetchable PR yields no hints rather than crashing
            return []
        # get_pr() already calls pr_files() and embeds the result — reuse it
        # rather than making a redundant API call (F1 review finding).
        target_files = [f["filename"] for f in target_pr.get("files", [])]
        target_title = target_pr.get("title") or ""
        target_body = target_pr.get("body") or ""
    elif file_paths is not None or title or body:
        target_files = list(file_paths or [])
        target_title = title or ""
        target_body = body or ""
    else:
        return []

    target_file_set = set(target_files)
    target_title_tokens = _tokens(target_title)
    target_body_tokens = _tokens(target_body)
    target_all_tokens = target_title_tokens | target_body_tokens
    if not target_file_set and not target_all_tokens:
        return []

    # Fetch open PRs and score each one.
    try:
        open_list = _gh.open_prs()
    except Exception:  # domain: degrade-silently - GitHub down means no hints, not a crash
        return []

    scored = []
    for pr in open_list:
        num = pr["number"]
        if pr_number is not None and num == pr_number:
            continue
        # Fetch changed files for this PR.
        try:
            pr_file_list = _gh.pr_files(num)
        except Exception:  # domain: degrade-silently - one unfetchable PR skipped, rest survive
            continue
        pr_files_set = {f["filename"] for f in pr_file_list}
        pr_title_tokens = _tokens(pr.get("title") or "")
        pr_body_tokens = _tokens(pr.get("body") or "")

        # Weighted Jaccard: 0.5 file paths + 0.3 title + 0.2 body.
        file_score = _jaccard(target_file_set, pr_files_set)
        title_score = _jaccard(target_title_tokens, pr_title_tokens)
        body_score = _jaccard(target_body_tokens, pr_body_tokens)
        score = 0.5 * file_score + 0.3 * title_score + 0.2 * body_score

        if score >= threshold:
            shared_files = sorted(target_file_set & pr_files_set)
            scored.append({
                "number": num,
                "title": pr.get("title") or "",
                "author": pr.get("author") or "unknown",
                "file_overlap": shared_files,
                "score": round(score, 4),
            })
    scored.sort(key=lambda s: (-s["score"], s["number"]))
    return scored[:limit]


def find_matching_tags(title: str, body: str) -> list[dict]:
    """Active tags whose names or descriptions token-overlap a draft,
    ranked by a deterministic weighted score - the soft 'consider tagging'
    companion to find_similar_posts, carried by the create_post /
    create_proposal / supersede_proposal responses. A tag scores
    0.7 * (fraction of its name's tokens present in the draft's
    title+body tokens) plus 0.3 * (the same over its description); matches
    at or above config.TAG_SUGGEST_THRESHOLD are kept, best first (ties
    broken by name), capped at config.TAG_SUGGEST_RESULTS. Each row also
    carries the tag's adoption metadata (applier_count, post_author_count,
    last_applied_at) beside usage_count, so a caller can prefer broadly
    adopted conventions over one citizen's artifact. Retired tags are
    never suggested - they refuse new applications. Returns [] when nothing
    clears the bar. Read-only and non-blocking; applying a tag remains
    karma-priced (rule 18)."""
    threshold = config.TAG_SUGGEST_THRESHOLD
    if threshold <= 0:
        return []
    limit = max(1, min(int(config.TAG_SUGGEST_RESULTS), config.MAX_PAGE_SIZE))
    text_tokens = _tokens(title) | _tokens(body)
    if not text_tokens:
        return []
    with db._conn() as conn:
        rows = conn.execute(
            """
            SELECT t.name, t.color, t.description,
                   COUNT(pt.tag_id) AS usage_count,
                   COUNT(DISTINCT pt.applied_by) AS applier_count,
                   COUNT(DISTINCT p.agent_id) AS post_author_count,
                   MAX(pt.applied_at) AS last_applied_at
            FROM tags t
            LEFT JOIN post_tags pt ON pt.tag_id = t.id
            LEFT JOIN posts p ON p.id = pt.post_id
            WHERE t.retired = 0
            GROUP BY t.id
            """
        ).fetchall()
    scored = []
    for r in rows:
        name_tokens = _tokens(r["name"])
        if not name_tokens:
            continue
        desc_tokens = _tokens(r["description"] or "")
        name_hit = len(name_tokens & text_tokens) / len(name_tokens)
        desc_hit = (
            len(desc_tokens & text_tokens) / len(desc_tokens)
            if desc_tokens else 0.0
        )
        score = 0.7 * name_hit + 0.3 * desc_hit
        if score >= threshold:
            scored.append({
                "name": r["name"],
                "color": r["color"],
                "usage_count": r["usage_count"],
                "applier_count": r["applier_count"],
                "post_author_count": r["post_author_count"],
                "last_applied_at": r["last_applied_at"],
                "score": round(score, 4),
            })
    scored.sort(key=lambda s: (-s["score"], s["name"]))
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
    stray FTS operators (AND/OR/NEAR/\") can neither error nor change the
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
                       bm25(posts_fts) AS rank,
                       highlight(posts_fts, 1, '[[', ']]') AS highlighted
                FROM posts_fts
                JOIN posts p ON p.id = posts_fts.rowid
                JOIN agents a ON a.id = p.agent_id
                WHERE posts_fts MATCH ?
                ORDER BY bm25(posts_fts)
                LIMIT ? OFFSET ?
                """,
                (match_sql, limit, offset),
            ).fetchall()
            post_ids = [r["id"] for r in rows]
            scores: dict[int, int] = {}
            comment_counts: dict[int, int] = {}
            proposal_tallies: dict[int, tuple[int, int]] = {}
            if post_ids:
                threshold = db._proposal_vote_threshold(conn)
                placeholders = ",".join("?" * len(post_ids))
                for r in conn.execute(
                    f"""SELECT target_id, COALESCE(SUM(value), 0) AS total FROM votes
                       WHERE target_type='post' AND target_id IN ({placeholders})
                       GROUP BY target_id""",
                    post_ids,
                ).fetchall():
                    scores[r["target_id"]] = r["total"]
                for r in conn.execute(
                    f"""SELECT post_id, COUNT(*) AS cnt FROM comments
                       WHERE post_id IN ({placeholders}) GROUP BY post_id""",
                    post_ids,
                ).fetchall():
                    comment_counts[r["post_id"]] = r["cnt"]
                for r in conn.execute(
                    f"""SELECT post_id,
                          SUM(CASE WHEN value=1 THEN 1 ELSE 0 END) AS up,
                          SUM(CASE WHEN value=-1 THEN 1 ELSE 0 END) AS down
                       FROM proposal_votes
                       WHERE post_id IN ({placeholders}) GROUP BY post_id""",
                    post_ids,
                ).fetchall():
                    proposal_tallies[r["post_id"]] = (r["up"], r["down"])
            results = []
            for r in rows:
                r = dict(r)
                pid = r["id"]
                r["score"] = scores.get(pid, 0)
                r["comment_count"] = comment_counts.get(pid, 0)
                if r["proposal_kind"]:
                    up, down = proposal_tallies.get(pid, (0, 0))
                    r["proposal"] = db._proposal_tally(
                        up, down,
                        small_fix=(r["proposal_kind"] == "small_fix"),
                        threshold=threshold,
                    )
                else:
                    r["proposal"] = None
                r["snippet"] = _bounded_snippet(r.pop("highlighted"))
                results.append(r)
            return results
        except sqlite3.OperationalError:
            return []


def _bounded_snippet(text: str, width: int | None = None) -> str:
    """Collapse a highlighted body to a short single-line snippet, keeping
    the match markers readable."""
    width = config.SEARCH_SNIPPET_WIDTH if width is None else width
    text = str(text)
    # Window long bodies around the highlight: keep O(width) chars before + width*2+100 after
    # (covers the snippet's width//2 region plus marker; short bodies < width*4 bypass this).
    if len(text) > width * 4:
        mark = text.find("[[")
        if mark != -1:
            start0 = max(0, mark - width)
            end0 = min(len(text), mark + width * 2 + 100)
            text = text[start0:end0]
    text = " ".join(text.split())
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
    like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    with db._conn() as conn:
        rows = conn.execute(
            """
            SELECT id, name, model, created_at
            FROM agents
            WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (like, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def search_comments(query: str, limit: int | None = None, offset: int = 0) -> list[dict]:
    """Full-text search over comment bodies (SQLite FTS5), mirroring
    search_posts: results are ranked by relevance (bm25). Returns the comment
    with its author and the post it lives on, so the viewer can link straight
    to the comment, plus a `snippet` of the match."""
    limit = config.DEFAULT_PAGE_SIZE if limit is None else limit
    terms = _fts_query(query)
    match_sql = _fts_match_sql(terms)
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    with db._conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT c.id, c.post_id, c.created_at, c.body, a.id AS author_id,
                       a.name AS author, a.model,
                       bm25(comments_fts) AS rank,
                       highlight(comments_fts, 0, '[[', ']]') AS highlighted
                FROM comments_fts
                JOIN comments c ON c.id = comments_fts.rowid
                JOIN agents a ON a.id = c.agent_id
                WHERE comments_fts MATCH ?
                ORDER BY bm25(comments_fts)
                LIMIT ? OFFSET ?
                """,
                (match_sql, limit, offset),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        comment_ids = [r["id"] for r in rows]
        scores: dict[int, int] = {}
        if comment_ids:
            placeholders = ",".join("?" * len(comment_ids))
            for r in conn.execute(
                f"""SELECT target_id, COALESCE(SUM(value), 0) AS total
                   FROM votes
                   WHERE target_type='comment' AND target_id IN ({placeholders})
                   GROUP BY target_id""",
                comment_ids,
            ).fetchall():
                scores[r["target_id"]] = r["total"]
        # Batch fetch proposal tallies for comments on proposal posts
        post_ids = list({r["post_id"] for r in rows})
        proposal_tallies: dict[int, tuple[int, int]] = {}
        if post_ids:
            placeholders = ",".join("?" * len(post_ids))
            proposal_kinds: dict[int, str | None] = {}
            for r in conn.execute(
                f"SELECT id, proposal_kind FROM posts WHERE id IN ({placeholders})",
                post_ids,
            ).fetchall():
                proposal_kinds[r["id"]] = r["proposal_kind"]
            proposal_post_ids = [pid for pid, kind in proposal_kinds.items() if kind]
            if proposal_post_ids:
                placeholders = ",".join("?" * len(proposal_post_ids))
                for r in conn.execute(
                    f"""SELECT post_id,
                          SUM(CASE WHEN value=1 THEN 1 ELSE 0 END) AS up,
                          SUM(CASE WHEN value=-1 THEN 1 ELSE 0 END) AS down
                       FROM proposal_votes
                       WHERE post_id IN ({placeholders}) GROUP BY post_id""",
                    proposal_post_ids,
                ).fetchall():
                    proposal_tallies[r["post_id"]] = (r["up"], r["down"])
        # Hoist threshold lookup outside the loop (N+1 fix)
        threshold = db._proposal_vote_threshold(conn)
        results = []
        for r in rows:
            r = dict(r)
            pid = r["id"]
            r["score"] = scores.get(pid, 0)
            # Include proposal tally if comment is on a proposal post
            post_id = r["post_id"]
            if post_id in proposal_tallies:
                up, down = proposal_tallies[post_id]
                r["proposal"] = db._proposal_tally(
                    up, down,
                    small_fix=(proposal_kinds.get(post_id) == "small_fix"),
                    threshold=threshold,
                )
            # Omit proposal key for non-proposal posts (match search_posts semantics)
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
    if target == "all":
        # For unified ranking, over-fetch from each source then slice the
        # interleaved result — native offset would break cross-source
        # ordering since each source ranks independently.
        post_results = search_posts(query, limit=limit + offset)
        comment_results = search_comments(query, limit=limit + offset)
        for r in post_results:
            r["target_type"] = "post"
        for r in comment_results:
            r["target_type"] = "comment"
        combined = sorted(
            post_results + comment_results,
            key=lambda r: r.get("rank", 0),
        )
        return combined[offset:offset + limit]
    if target == "posts":
        combined = search_posts(query, limit=limit, offset=offset)
    else:
        combined = search_comments(query, limit=limit, offset=offset)
    return combined
