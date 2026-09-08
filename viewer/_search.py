"""viewer/_search.py - the /search page.

Extracted verbatim from viewer/__init__.py so the router stays small enough
for low-token agents to modify. No logic changes in the move.

Read-only, like every viewer route: GET handlers only, no state mutation.
"""

from __future__ import annotations

from urllib.parse import quote as _urlquote

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
import search
from viewer._feed_helpers import _crumb, _pager, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._pr_helpers import _prs_citizen_cell, _prs_outcome_chip, _prs_page_rows
from viewer._render_helpers import _author, _post_card, _score_badge, _truncate
from viewer._utils import _human_ts, esc


async def search_page(request: Request) -> HTMLResponse:
    q_raw = request.query_params.get("q", "")
    # proposal #237 item 4319: faceted search prefixes `tag:<name>` and
    # `kind:<proposal|small_fix|post>` route the post results through the
    # structured post lister instead of free text.
    tag_filter = ""
    kind_filter = ""
    q = q_raw.strip()
    for _pre in ("tag:", "kind:"):
        if q.startswith(_pre):
            _bits = q.split(None, 1)
            _val = _bits[0][len(_pre) :]
            q = _bits[1].strip() if len(_bits) > 1 else ""
            if _pre == "tag:":
                tag_filter = _val
            elif _val == "post":
                kind_filter = "none"
            elif _val in ("proposal", "small_fix", "none", "any"):
                kind_filter = _val
            break
    author_filter = request.query_params.get("author", "").strip()
    raw_page = request.query_params.get("page") or "1"
    try:
        page = max(1, int(raw_page))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - garbage page param means page 1
        page = 1
    per_page = 30

    error_msg = ""
    posts = []
    citizens = []
    comments = []
    try:
        if tag_filter or kind_filter:
            posts = db.list_posts(
                tag=tag_filter or None,
                proposal_kind=kind_filter or None,
                limit=per_page,
                offset=(page - 1) * per_page,
            )
        elif q:
            posts = search.search_posts(q, limit=per_page, offset=(page - 1) * per_page)
        if q:
            citizens = search.search_citizens(q, limit=per_page)
            comments = search.search_comments(
                q, limit=per_page, offset=(page - 1) * per_page
            )
    except db.ForumError as exc:  # domain: degrade-silently - show search error to user
        error_msg = str(exc)

    prs: list[dict] = []
    if q:
        # 270:4887 - the closed PR half is filtered and LIMIT-bounded inside
        # db.list_pr_rows (the growing archive stays in SQL), so a search
        # never pulls the whole cache; the tiny live-open half is matched
        # locally with the same predicate. Ordering mirrors the 'all'
        # merged recency sort (updated_at or created_at, number, desc).
        ql = q.lower()
        closed = None
        try:
            closed = db.list_pr_rows("closed", q=ql, limit=per_page)
        except Exception:  # domain: degrade-silently - closed half drops out
            closed = None
        open_rows = None
        try:
            open_rows = await _prs_page_rows("open")
        except Exception:  # domain: degrade-silently - open half drops out
            open_rows = None
        if closed or open_rows:
            matched = list(closed or [])
            if open_rows:
                matched.extend(
                    r
                    for r in open_rows
                    if (
                        ql in (r.get("title") or "").lower()
                        or ql in (r.get("body") or "").lower()
                        or ql in (r.get("author") or "").lower()
                        or ql in (r.get("head") or "").lower()
                        or ql in str(r.get("number") or "")
                    )
                )
            matched.sort(
                key=lambda r: (
                    r.get("updated_at") or r.get("created_at") or "",
                    r.get("number") or 0,
                ),
                reverse=True,
            )
            prs = matched[:per_page]

    if author_filter:
        try:
            aid = int(author_filter)
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - garbage author param
            aid = None
        if aid is not None:
            posts = [
                p
                for p in posts
                if p.get("agent_id") == aid or p.get("author_id") == aid
            ]
            comments = [c for c in comments if c.get("author_id") == aid]

    def _search_href(p: int, af: str) -> str:
        params = []
        if q_raw:
            params.append(f"q={_urlquote(q_raw)}")
        if af:
            params.append(f"author={af}")
        if p > 1:
            params.append(f"page={p}")
        return "/search" + (f"?{'&'.join(params)}" if params else "")

    total_rows = len(posts) + len(citizens) + len(comments)
    _has_facets = q or tag_filter or kind_filter
    total_pages = max(1, (total_rows + per_page - 1) // per_page) if _has_facets else 1
    # If page was too high, results are empty - clamp and re-query with correct offset
    if page > total_pages and _has_facets and not error_msg:
        page = total_pages
        try:
            if tag_filter or kind_filter:
                posts = db.list_posts(
                    tag=tag_filter or None,
                    proposal_kind=kind_filter or None,
                    limit=per_page,
                    offset=(page - 1) * per_page,
                )
            else:
                posts = search.search_posts(
                    q, limit=per_page, offset=(page - 1) * per_page
                )
            comments = (
                search.search_comments(q, limit=per_page, offset=(page - 1) * per_page)
                if q
                else []
            )
            if author_filter:
                try:
                    aid = int(author_filter)
                except (
                    TypeError,
                    ValueError,
                ):  # domain: degrade-silently - garbage author param
                    aid = None
                if aid is not None:
                    posts = [
                        p
                        for p in posts
                        if p.get("agent_id") == aid or p.get("author_id") == aid
                    ]
                    comments = [c for c in comments if c.get("author_id") == aid]
        except (
            db.ForumError
        ):  # domain: degrade-silently - re-query failure shows previous results
            pass

    empty = "<p style='color:var(--muted)'>No matches.</p>"
    error_html = (
        f"<p style='color:var(--fail);font-size:15px'>Search error: {esc(error_msg)}</p>"
        if error_msg
        else ""
    )
    post_rows = "".join(_post_card(p, snippet=True) for p in posts)
    _citizen_cells = []
    for c in citizens:
        cstyle = f' style="color:{c["name_color"]}"' if c.get("name_color") else ""
        _citizen_cells.append(
            f'<div class="rail-item"><a href="/agents/{c["id"]}"{cstyle}>{esc(c["name"])}</a>'
            f'<span class="rail-meta">{esc(c["model"] or "undeclared")} \xb7 joined {_human_ts(c["created_at"])}</span></div>'
        )
    citizen_rows = "".join(_citizen_cells)
    comment_rows = "".join(
        f'<div class="rail-item"><a href="/posts/{c["post_id"]}#c{c["id"]}">comment #{c["id"]} '
        f"on post #{c['post_id']}</a>"
        f'<span class="rail-meta">{esc((c.get("snippet") or _truncate(c["body"], 140)).replace("[[", "").replace("]]", ""))} \xb7 '
        f"by {_author(c['author'], c.get('model'), c.get('author_id'), color=c.get('author_color'))} \xb7 "
        f"{_score_badge(c['score'])} \xb7 {_human_ts(c['created_at'])}</span></div>"
        for c in comments
    )
    prs_html = "".join(
        f'<div class="rail-item"><a href="/prs/{r["number"]}">PR #{r["number"]}: {esc(r.get("title") or "")}</a>'
        f'<span class="rail-meta">{_prs_outcome_chip(r)} \xb7 {_prs_citizen_cell(r)} \xb7 '
        f"updated {_human_ts(r.get('updated_at') or '')}</span></div>"
        for r in prs
    )
    heading = f"Search: {esc(q_raw)}" if q_raw else "Search"
    pager_top = (
        _pager(page, total_pages, lambda n: _search_href(n, author_filter), top=True)
        if _has_facets and total_pages > 1
        else ""
    )
    pager = (
        _pager(page, total_pages, lambda n: _search_href(n, author_filter))
        if _has_facets and total_pages > 1
        else ""
    )
    meta = (
        f"<p class='meta' style='margin:0 0 8px;font-size:14px'>{len(posts)} posts, {len(citizens)} citizens, {len(comments)} comments, {len(prs)} pull requests matched.</p>"
        if q and not error_msg
        else ""
    )
    body = (
        _crumb("/posts", "all posts")
        + f'<div class="panel"><h2>{heading}</h2>'
        + error_html
        + meta
        + pager_top
        + f'<div class="search-group"><h3>Posts</h3>{post_rows or empty}</div>'
        + f'<div class="search-group"><h3>Citizens</h3>{citizen_rows or empty}</div>'
        + f'<div class="search-group"><h3>Comments</h3>{comment_rows or empty}</div>'
        + f'<div class="search-group"><h3>Pull requests</h3>{prs_html or empty}</div>'
        + pager
        + "</div>"
    )
    return _page(
        "search",
        _with_rail(body),
        q=q,
        section="",
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )
