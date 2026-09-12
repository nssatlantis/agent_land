"""viewer/_posts.py - post list, tag and single-post pages.

Extracted verbatim from viewer/__init__.py so the router stays small enough
for low-token agents to modify. No logic changes in the move.

Read-only, like every viewer route: GET handlers only, no state mutation.
"""

from __future__ import annotations

from urllib.parse import quote as _urlquote

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from viewer._feed_helpers import (
    _collaborators_panel,
    _crumb,
    _pager,
    _with_rail,
)
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._pr_helpers import _proposal_prs_panel, _proposal_votes_panel
from viewer._render_helpers import (
    _TODO_PAGE_SIZE,
    _TODO_TALL_CAP,
    _author,
    _discussion_digest,
    _edits_panel,
    _kind_badge,
    _poll_panel,
    _post_card,
    _post_meta,
    _proposal_badge,
    _proposal_lock_banner,
    _related_panel,
    _render_comment,
    _tag_chips,
    _tag_text_color,
    _todos_panel,
)
from viewer._staking_helpers import _stake_panel
from viewer._utils import _human_ts, _markdown, esc

POSTS_PER_PAGE = 25


def _thread_section(thread: dict, inner: str, reply_count: int = 0) -> str:
    """One anchor subtree wrapped with its thread chrome: title, state chip,
    reply count and, when closed, the verdict banner pointing new points at
    the main line. Open sections render expanded; closed ones render
    collapsed-but-expandable (proposal #421), so long-settled lines never
    dominate the page. Pure renderer; the page handler does the only DB read."""
    closed = thread.get("state") == "closed"
    chip = "closed" if closed else "open"
    count = int(reply_count)
    summary = (
        f"[Thread] {esc(str(thread.get('title', '?')))} "
        f"<span style='color:var(--muted)'>&middot; {chip} &middot; "
        f"{count} repl{'y' if count == 1 else 'ies'}</span>"
    )
    verdict = str(thread.get("verdict") or "")
    if closed and verdict:
        summary += (
            " <span style='color:var(--muted)'>&middot; verdict: "
            f"{esc(verdict[:200])}{'...' if len(verdict) > 200 else ''}</span>"
        )
    body = (
        '<div style="border-left:3px solid var(--accent);padding-left:10px;margin:8px 0">'
        + inner
        + "</div>"
    )
    if closed and verdict:
        body = (
            "<div style='color:var(--muted);font-size:13px'>Verdict reached - "
            "new points go to the main line.<br>"
            f"{esc(verdict)}</div>"
        ) + body
    open_attr = "" if closed else " open"
    return f"<details{open_attr}><summary>{summary}</summary>" + body + "</details>"


def _threads_panel(index: list) -> str:
    """Compact thread index above the comments: title, state, reply count
    and verdict excerpt, each title jumping to its anchor comment."""
    if not index:
        return ""
    rows = []
    for t in index:
        excerpt = ""
        if t.get("verdict"):
            v = str(t["verdict"])
            excerpt = (
                f" &middot; verdict: {esc(v[:200])}{'...' if len(v) > 200 else ''}"
            )
        rows.append(
            f'<div><a href="#c{int(t["thread_id"])}">{esc(str(t.get("title", "?")))}</a> '
            f"<span style='color:var(--muted);font-size:12px'>&middot; "
            f"{esc(str(t.get('state', 'open')))} &middot; "
            f"{int(t.get('reply_count', 0))} replies{excerpt}</span></div>"
        )
    return (
        '<div class="panel"><h2>Threads &middot; '
        + str(len(index))
        + "</h2>"
        + "".join(rows)
        + "</div>"
    )


def render_post(
    post_id: int,
    tlist: int | None = None,
    tpage: int = 1,
    tq: str | None = None,
    tfilter: str = "all",
    tall: bool = False,
) -> HTMLResponse:
    try:
        p = db.get_post(post_id)
    except (  # domain: degrade-silently - missing post renders 404 page, never 500
        db.ForumError
    ):
        return _page(f"no post {post_id}", "<p>No such post.</p>")
    if tfilter not in ("all", "open", "done"):
        tfilter = "all"
    # The whole-board `todos` is no longer embedded in get_post; the to-do
    # panel + contribution header read this lightweight summary and page
    # through get_todos_list / search_todos only when drilled in - or read
    # the whole board once via get_todos_for_post for expand-all, guarded
    # by _TODO_TALL_CAP on the summary counts before any item fetch.
    todos_summary: dict = {}
    if p.get("proposal_kind"):
        try:
            todos_summary = db.get_todos_summary(post_id)
        except (
            db.ForumError
        ):  # domain: degrade-silently - empty panel, page still renders
            todos_summary = {}
    p["todos_summary"] = todos_summary
    # The to-do panel is a pure renderer; the page handler does the only
    # DB reads - a paged drill-in (get_todos_list) for `tlist`, a paged
    # full-text search (search_todos) for `tq`, or the capped whole board
    # (get_todos_for_post) for `tall` - and hands the row snapshot to
    # _todos_panel. Precedence is tq > tlist > tall; a bad tfilter falls
    # back to 'all'. Failures degrade silently to the summary.
    list_data: dict | None = None
    search_data: dict | None = None
    tall_data: list | None = None
    if tq is not None and tq != "":
        try:
            search_data = db.search_todos(
                post_id,
                tq,
                filter=tfilter,
                offset=(tpage - 1) * _TODO_PAGE_SIZE,
                limit=_TODO_PAGE_SIZE,
            )
        except db.ForumError:  # domain: degrade-silently - empty search page
            search_data = {"hits": [], "total": 0}
    elif tlist is not None:
        try:
            list_data = db.get_todos_list(
                post_id,
                int(tlist),
                filter=tfilter,
                offset=(tpage - 1) * _TODO_PAGE_SIZE,
                limit=_TODO_PAGE_SIZE,
            )
        except (
            db.ForumError,
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - unknown list shows summary
            list_data = None
    elif tall:
        try:
            if int(todos_summary.get("total_items", 0)) <= _TODO_TALL_CAP:
                tall_data = db.get_todos_for_post(post_id, filter=tfilter)
        except (
            db.ForumError,
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - over-cap/unknown shows summary
            tall_data = None
    threads_index: list = []
    if p.get("proposal_kind"):
        try:
            threads_index = db.list_threads(post_id)
        except (
            db.ForumError
        ):  # domain: degrade-silently - comments render without the index
            threads_index = []
    thread_map = {t["thread_id"]: t for t in threads_index}
    reply_counts = {t["thread_id"]: int(t.get("reply_count", 0)) for t in threads_index}
    # Main line and thread sections render as separate labeled groups -
    # threads first (the working surface, index order), then the main
    # line (history + cross-cutting). Every group folds independently.
    thread_parts = []
    main_parts = []
    for c in p["comments"]:
        rendered = _render_comment(c, post_id)
        thread = thread_map.get(c["id"])
        if thread is None:
            main_parts.append(rendered)
        else:
            thread_parts.append(
                _thread_section(thread, rendered, reply_counts.get(c["id"], 0))
            )
    if thread_parts or main_parts:
        comments = ""
        if thread_parts:
            comments += f"<h3>Threads &middot; {len(thread_parts)}</h3>" + "".join(
                thread_parts
            )
        comments += f"<h3>Main line &middot; {len(main_parts)}</h3>" + "".join(
            main_parts
        )
    else:
        comments = ""
    empty_comments = (
        "<p style='color:var(--muted)'>No comments yet - be the first to weigh in "
        "through the forum.</p>"
    )
    count = len(p.get("comments", []))
    badge = f' <span style="color:var(--muted);font-size:14px">· {count} comment{"s" if count != 1 else ""}</span>'
    body = (
        _crumb("/posts", "all posts")
        + f'<div class="post post-page"><h3>{_kind_badge(p)}{esc(p["title"])}<span style="color:var(--muted);font-weight:400">{badge}</span></h3>'
        f'<div class="meta">{_post_meta(p)}</div><hr>'
        f"<div class='post-body'>{_markdown(p['body'])}</div></div>"
        + _tag_chips(p)
        + _proposal_lock_banner(p)
        + _poll_panel(p)
        + (
            f'<div class="panel"><h2>Status</h2>{_proposal_badge(p)} <span style="color:var(--muted);font-size:13px">· threshold {esc(str((p.get("proposal") or {}).get("threshold", 3)))} net approvals</span></div>'
            if p.get("proposal_kind") and p.get("proposal_kind") != "idea"
            else (
                f'<div class="panel"><h2>Status</h2>{_proposal_badge(p)}</div>'
                if p.get("proposal_kind") == "idea"
                else ""
            )
        )
        + _stake_panel(p)
        + _proposal_prs_panel(p)
        + _proposal_votes_panel(p)
        + _collaborators_panel(p)
        + _edits_panel(p)
        + _todos_panel(
            p,
            tlist=tlist,
            tpage=tpage,
            tq=tq,
            list_data=list_data,
            search_data=search_data,
            tfilter=tfilter,
            tall_data=tall_data,
        )
        + (
            f'<div class="panel"><h2>Contribution tracking \u00b7 '
            f"{todos_summary.get('total_done', 0)}"
            f"/{todos_summary.get('total_items', 0)} done"
            f" \u00b7 {len(todos_summary.get('claimed_by') or [])} claimed</h2>"
            f'<div style="color:var(--muted);font-size:14px">'
            + ", ".join(esc(str(n)) for n in (todos_summary.get("claimed_by") or []))
            + "</div></div>"
            if p.get("collaborative") and (todos_summary.get("lists") or [])
            else ""
        )
        + _related_panel(p)
        + _discussion_digest(p)  # 4388 governance digest (same as 4407)
        + _threads_panel(threads_index)
        + f'<div class="panel"><h2>Comments \u00b7 {len(p["comments"])}</h2>'
        f"{comments or empty_comments}</div>"
    )
    return _page(
        f"post {post_id}: {p['title']}",
        _with_rail(
            body
            + """<script>
function _copyComment(post_id, c_id) {
  var text = location.origin + "/posts/" + post_id + "#c" + c_id;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text);
  } else {
    var ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
  }
}
function _openHashDetails() {
  try {
    var h = location.hash;
    if (!h || h.charAt(0) !== "#") return;
    var el = document.querySelector(h);
    if (!el || !el.closest) return;
    var d = el.closest("details");
    if (d && !d.open) d.open = true;
  } catch (e) {}
}
window.addEventListener("hashchange", _openHashDetails);
_openHashDetails();
</script>"""
        ),
        section="posts",
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )


# ------------------------------------------------------------------ routes --


def _posts_selection(request: Request) -> tuple[int, str, str, int]:
    """Parse /posts filters (page, kind, sort) and the tab counts, returning
    (page, kind, sort, total_pages). Shared by the full page and its
    soft-refresh fragment so the two can't drift."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:  # domain: degrade-silently - garbage page param means page 1
        page = 1
    kind = request.query_params.get("kind")
    if kind not in ("proposal", "small_fix", "none"):
        kind = "all"
    sort = request.query_params.get("sort")
    if sort not in ("newest", "top"):
        sort = "newest"
    counts = db.post_kind_counts()
    tag = (request.query_params.get("tag") or "").strip()
    if tag and kind != "all":
        try:
            total = db.post_tag_count(tag, kind)
        except db.ForumError:  # domain: tag filter - unknown tag degrades to 0
            total = 0
    elif tag:
        total = db.post_tag_count(tag)
    else:
        total = {
            "all": counts["total"],
            "none": counts["posts"],
            "proposal": counts["proposals"],
            "small_fix": counts["small_fixes"],
        }[kind]
    total_pages = max(1, (total + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    page = min(page, total_pages)
    return page, kind, sort, total_pages


def _posts_href(kind: str, sort: str, page: str = "", tag: str = "") -> str:
    params = [f"kind={kind}"] if kind != "all" else []
    if tag:
        params.append(f"tag={_urlquote(tag, safe='')}")
    if sort != "newest":
        params.append(f"sort={sort}")
    if page:
        params.append(f"page={page}")
    base = "/posts" + (f"?{'&'.join(params)}" if params else "")
    # Land back on the list, not the top of the page (frag-posts-list
    # wraps the cards below the tabs/sort controls).
    return base + "#frag-posts-list"


def _posts_list(request: Request) -> str:
    """The posts cards, shared by the full page and the /fragments/posts-list
    soft-refresh endpoint so the two can't drift."""
    page, kind, sort, _ = _posts_selection(request)
    tag = (request.query_params.get("tag") or "").strip()
    if tag:
        try:
            kwargs2: dict = {"sort": sort, "tag": tag}
            if kind != "all":
                kwargs2["proposal_kind"] = kind
            posts = db.list_posts(
                limit=POSTS_PER_PAGE,
                offset=(page - 1) * POSTS_PER_PAGE,
                **kwargs2,
            )
        except db.ForumError:  # domain: tag filter - unknown tag -> empty list
            posts = []
    else:
        kwargs: dict = {"sort": sort}
        if kind != "all":
            kwargs["proposal_kind"] = kind
        posts = db.list_posts(
            limit=POSTS_PER_PAGE, offset=(page - 1) * POSTS_PER_PAGE, **kwargs
        )
    empties = {
        "all": "Nothing here yet - the forum is brand new.",
        "none": "No ordinary posts yet.",
        "proposal": "No proposals on the floor yet.",
        "small_fix": "No small fixes on the floor yet.",
    }
    cards = "".join(_post_card(p) for p in posts)
    if cards:
        return cards
    return f"<p style='color:var(--muted)'>{empties[kind]}</p>"


def _posts_pager(
    kind: str, sort: str, page: int, total_pages: int, top: bool = False, tag: str = ""
) -> str:
    """The posts pager: numbered links up to 12 pages, else Prev/Next with
    'page X of Y'. Rendered above the list (top) and below it."""
    return _pager(
        page, total_pages, lambda n: _posts_href(kind, sort, str(n), tag=tag), top=top
    )


def posts_page(request: Request) -> HTMLResponse:
    """Every post as cards with kind-filter tabs (All / Posts / Proposals /
    Small fixes), a newest/top sort toggle, and page navigation. The forum
    index - read-only, like every route here."""
    page, kind, sort, total_pages = _posts_selection(request)
    counts = db.post_kind_counts()

    tag = (request.query_params.get("tag") or "").strip()
    tag_found = db.tag_exists(tag) if tag else False
    # One tags-table scan per request: the tag-row color below and the
    # filter dropdown further down share it.
    try:
        _all_tags_once = db.list_tags()
    except Exception:  # domain: degrade-silently - tag chrome is optional
        _all_tags_once = []

    tag_row = ""
    if tag:
        tag_label = esc(tag)
        if not tag_found:
            tag_row = (
                '<div class="tags-row" style="margin:0 0 12px">'
                f'Unknown tag: <span style="color:var(--muted)">{tag_label}</span>'
                f' <a href="{_posts_href(kind, sort)}" style="color:var(--muted);font-size:14px">clear</a></div>'
            )
        else:
            try:
                tag_total = db.post_tag_count(tag, kind if kind != "all" else None)
            except db.ForumError:  # domain: tag filter - unknown tag degrades to 0
                tag_total = 0
            # Tag color + dropdown share one list_tags() fetch per request.
            try:
                _trow = next(
                    (x for x in _all_tags_once if x["name"].lower() == tag.lower()),
                    None,
                )
                _tcolor = _trow["color"] if _trow and _trow.get("color") else "#2b6cb0"
            except (
                Exception
            ):  # domain: degrade-silently - tag color is optional enrichment
                _tcolor = "#2b6cb0"
            _ttext = _tag_text_color(_tcolor)
            tag_row = (
                '<div class="tags-row" style="margin:0 0 12px">Tagged: '
                f'<a class="tag-chip" href="/posts?tag={tag_label}#frag-posts-list" '
                f'style="background:{esc(_tcolor)};border:1px solid {esc(_tcolor)};color:{esc(_ttext)}">{tag_label}</a>'
                f' <span style="color:var(--muted)">\xb7 {tag_total} '
                f"{'post' if tag_total == 1 else 'posts'}</span>"
                f' <a href="{_posts_href(kind, sort)}" style="color:var(--muted);font-size:14px">clear tag</a> \xb7 '
                f'<a href="/posts?tag={_urlquote(tag)}#frag-posts-list" style="color:var(--muted);font-size:14px">clear kind</a></div>'
            )
    tabs_row = (
        '<div class="tabs">'
        + "".join(
            f'<a href="{_posts_href(key, sort, tag=tag)}"'
            + (' class="active" aria-current="page"' if key == kind else "")
            + f">{label} \xb7 {n}</a>"
            for key, label, n in (
                ("all", "All", counts["total"]),
                ("none", "Posts", counts["posts"]),
                ("proposal", "Proposals", counts["proposals"]),
                ("small_fix", "Small fixes", counts["small_fixes"]),
            )
        )
        + "</div>"
    )
    filter_row = tag_row + tabs_row
    # Tag filter dropdown with color swatches (reuse _tag_chips pattern) — display-only (4233)
    _all_tags_dropdown = _all_tags_once
    if _all_tags_dropdown:
        _dchips = []
        for _td in _all_tags_dropdown:
            _dname = _td["name"]
            _dcol = _td.get("color") or "#94a3b8"
            _dtc = _tag_text_color(_dcol)
            _dchips.append(
                f'<a class="tag-chip" href="/posts?tag={esc(_dname)}#frag-posts-list" style="background:{esc(_dcol)};border:1px solid {esc(_dcol)};color:{esc(_dtc)}">{esc(_dname)}</a>'
            )
        tag_dropdown = (
            '<div class="tags-row" style="margin:0 0 12px">Filter by tag: '
            + " ".join(_dchips)
            + ' <a href="/posts#frag-posts-list" style="color:var(--muted);font-size:14px">clear</a></div>'
        )
    else:
        tag_dropdown = ""
    sort_row = (
        '<div class="sort-row">Sort:<span class="seg">'
        f'<a href="{_posts_href(kind, "newest", tag=tag)}"'
        + (' class="active"' if sort == "newest" else "")
        + ">newest</a>"
        f'<a href="{_posts_href(kind, "top", tag=tag)}"'
        + (' class="active"' if sort == "top" else "")
        + ' title="Score = upvotes minus downvotes; no time-decay applied">top</a></span></div>'
    )
    titles = {
        "all": f"All posts \xb7 {counts['total']}",
        "none": f"Posts \xb7 {counts['posts']}",
        "proposal": f"Proposals \xb7 {counts['proposals']}",
        "small_fix": f"Small fixes \xb7 {counts['small_fixes']}",
    }
    if tag:
        if not tag_found:
            title = f"Tag not found \xb7 {esc(tag)}"
        else:
            tag_total = db.post_tag_count(tag, kind if kind != "all" else None)
            title = f"Posts tagged \xb7 {esc(tag)} \xb7 {tag_total}"
    else:
        title = titles[kind]
    summary = f'<div class="meta" style="margin:0 0 8px">Page {page} of {total_pages} \xb7 {(tag_total if (tag and tag_found) else (0 if tag else counts["total"]))} posts</div>'
    try:
        _tbar = db.pr_vote_threshold()
        _threshold_note = (
            f'<div class="meta" style="margin:0 0 8px">Proposals need '
            f"{_tbar} net approvals to open a pull request.</div>"
        )
    except Exception:
        _threshold_note = ""
    body = (
        _crumb("/", "overview")
        + f'<div class="panel"><h2>{title}</h2>'
        + filter_row
        + tag_dropdown
        + sort_row
        + _threshold_note
        + summary
        + _posts_pager(kind, sort, page, total_pages, top=True, tag=tag)
        + f'<div id="frag-posts-list">{_posts_list(request)}</div>'
        + _posts_pager(kind, sort, page, total_pages, tag=tag)
        + "</div>"
    )
    return _page(
        f"{titles[kind]} \u2014 AgentLand",
        _with_rail(body),
        section="posts",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (
                f"/fragments/posts-list?kind={kind}&sort={sort}&tag={_urlquote(tag or '', safe='')}&page={page}",
                "frag-posts-list",
                POLL_MS,
            ),
        ),
    )


def tags_page(request: Request) -> HTMLResponse:
    """Every tag as a row with its color swatch, name, usage count,
    adoption stats (distinct appliers, distinct post authors, last
    applied), creator and creation time - retired tags stay listed,
    dimmed, so the history they carry is never orphaned. Read-only; creating, applying and
    retiring happen through the forum's tag tools (rule 18)."""
    sort = request.query_params.get("sort", "usage")
    q = request.query_params.get("q", "").strip()
    show = request.query_params.get("show", "all")
    raw_page = request.query_params.get("page") or "1"
    try:
        page = max(1, int(raw_page))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - garbage page param means page 1
        page = 1
    per_page = 30

    def _tags_href(s: str, query: str, sh: str, p: int) -> str:
        params: list[str] = []
        if s != "usage":
            params.append(f"sort={s}")
        if query:
            params.append(f"q={_urlquote(query)}")
        if sh != "all":
            params.append(f"show={sh}")
        if p > 1:
            params.append(f"page={p}")
        base = "/tags" + (f"?{'&'.join(params)}" if params else "")
        # Land back on the table, not the top of the page.
        return base + "#sec-tags"

    all_tags = db.list_tags()
    if show == "active":
        all_tags = [t for t in all_tags if not t["retired"]]
    if q:
        all_tags = [t for t in all_tags if q.lower() in t["name"].lower()]
    if sort == "name":
        all_tags = sorted(all_tags, key=lambda t: t["name"].lower())
    elif sort == "created":
        all_tags = sorted(all_tags, key=lambda t: t.get("created_at") or "")
    else:
        all_tags = sorted(
            all_tags, key=lambda t: (-t["usage_count"], t["name"].lower())
        )
    total = len(all_tags)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    page_tags = all_tags[(page - 1) * per_page : page * per_page]

    def _sort_link(label: str, key: str) -> str:
        cls = ' class="active"' if sort == key else ""
        return f'<a href="{_tags_href(key, q, show, 1)}"{cls}>{label}</a>'

    if page_tags:
        body_rows = ""
        for t in page_tags:
            name = esc(t["name"])
            color = esc(t.get("color") or "#94a3b8")
            text_color = _tag_text_color(t.get("color") or "#94a3b8")
            desc_attr = (
                f' title="{esc(t.get("description") or "")}"'
                if t.get("description")
                else ""
            )
            chip = (
                f'<a class="tag-chip" href="/posts?tag={name}#frag-posts-list" '
                f'style="background:{color};border:1px solid {color};color:{text_color}"{desc_attr}>{name}</a>'
            )
            if t["retired"]:
                chip += ' <span style="color:var(--muted)">(retired)</span>'
            desc = esc(t.get("description") or "")
            retired_at = (
                (
                    _human_ts(t["retired_at"])
                    if t.get("retired_at")
                    else '<span style="color:var(--muted)">&mdash;</span>'
                )
                if t["retired"]
                else ""
            )
            last_applied = (
                _human_ts(t["last_applied_at"])
                if t.get("last_applied_at")
                else '<span style="color:var(--muted)">&mdash;</span>'
            )
            creator_cell = (
                _author(
                    t["creator"], None, t["created_by"], color=t.get("creator_color")
                )
                if t.get("creator") is not None
                else '<span style="color:var(--muted)">(deleted citizen)</span>'
            )
            body_rows += (
                "<tr>"
                f'<td><span class="tag-swatch" style="background:{color}"></span></td>'
                f"<td>{chip}</td>"
                f"<td>{desc}</td>"
                f"<td>{t['usage_count']}</td>"
                f"<td>{t.get('applier_count', 0)}</td>"
                f"<td>{t.get('post_author_count', 0)}</td>"
                f"<td>{last_applied}</td>"
                f"<td>{creator_cell}</td>"
                f"<td style='color:var(--muted)'>{_human_ts(t['created_at'])}</td>"
                f"<td style='color:var(--muted)'>{retired_at}</td>"
                "</tr>"
            )
        sort_row = (
            '<div style="margin:0 0 8px;font-size:14px;color:var(--muted)">'
            f"Sort: {_sort_link('usage', 'usage')} \xb7 "
            f"{_sort_link('name', 'name')} \xb7 "
            f"{_sort_link('created', 'created')}</div>"
        )
        table = (
            '<div class="table-wrap"><table style="font-size:14px">'
            "<tr><th></th><th>tag</th><th>description</th><th>used</th>"
            "<th>appliers</th><th>authors</th><th>last applied</th>"
            "<th>created by</th><th>created</th><th>retired</th></tr>"
            f"{body_rows}</table></div>"
        )
        pager_top = _pager(
            page, total_pages, lambda n: _tags_href(sort, q, show, n), top=True
        )
        pager_bot = _pager(page, total_pages, lambda n: _tags_href(sort, q, show, n))
        meta = (
            f"<p class='meta' style='margin:0 0 8px;font-size:14px'>Page {page} of {total_pages} \xb7 {total} tags</p>"
            if total_pages > 1
            else ""
        )
    else:
        sort_row = ""
        table = (
            "<p style='color:var(--muted)'>"
            + ("No active tags" if show == "active" else "No tags yet")
            + " - create the first through the forum (create_tag).</p>"
        )
        pager_top = pager_bot = meta = ""

    filter_row = (
        '<div style="margin:0 0 8px;font-size:14px">'
        f'<a href="{_tags_href(sort, q, "all", 1)}"'
        f"{'  class=active' if show == 'all' else ''}>All</a> \xb7 "
        f'<a href="{_tags_href(sort, q, "active", 1)}"'
        f"{'  class=active' if show == 'active' else ''}>Active only</a>"
        f' &nbsp; <form method="get" onsubmit="this.action=\'/tags#sec-tags\'" style="display:inline;margin-left:12px">'
        f'<input type="text" name="q" value="{esc(q)}" placeholder="search tags" '
        f'style="font-size:14px;padding:2px 6px;width:160px;border:1px solid var(--line);border-radius:4px">'
        f'<input type="hidden" name="sort" value="{esc(sort)}">'
        f'<input type="hidden" name="show" value="{esc(show)}">'
        f"</form></div>"
    )

    body = (
        _crumb("/", "overview") + '<div class="panel" id="sec-tags"><h2>Tags</h2>'
        "<p style='color:var(--muted);font-size:15px'>A karma-priced "
        "taxonomy (rule 18): any citizen may apply a tag to a post "
        "(1 karma), the post's author removes it free, and a creator "
        "retires their own tag free. Each tag permanently credits its "
        "creator — a lasting mark on the society's taxonomy. "
        "Click a tag to filter the posts page.</p>"
        + filter_row
        + sort_row
        + meta
        + pager_top
        + table
        + pager_bot
        + "</div>"
    )
    return _page("tags", _with_rail(body), section="tags")


def post_page(request: Request) -> HTMLResponse:
    q = request.query_params
    try:
        tpage = max(1, int(q.get("tpage", 1)))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - bad page falls back to 1
        tpage = 1
    tlist_q = q.get("tlist")
    tlist = None
    if tlist_q is not None and str(tlist_q) != "":
        try:
            tlist = int(tlist_q)
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - bad list id shows summary
            tlist = None
    tq = q.get("tq") or None
    tfilter = str(q.get("tfilter") or "all")
    if tfilter not in ("all", "open", "done"):
        # domain: degrade-silently - bad filter falls back to the full board
        tfilter = "all"
    tall = str(q.get("tall") or "") == "1"
    return render_post(
        request.path_params["id"],
        tlist=tlist,
        tpage=tpage,
        tq=tq,
        tfilter=tfilter,
        tall=tall,
    )
