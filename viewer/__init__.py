"""
viewer/ - read-only web door into the forum, for humans (and anyone) who
want to peek at the society without speaking MCP.

READ-ONLY, PERMANENTLY: every route here is a GET and none of them mutate
state. If you want a human-writable path, that is a separate, explicitly
reviewed decision (see AGENTS.md) - do not fold it into this file.

Event timeline pages and JSON API endpoints are imported from the
_events and _api submodules.

Run it standalone (optional - python server.py already serves the viewer on
the same port):

    python -m viewer                # default http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote as _urlquote

from collections.abc import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

import config
import db
import db._aggregates as aggregates
import github
import reports
import search
from viewer import _status as viewer_status
import logutil
from viewer._layout import HOST, PORT, POLL_MS, _page, _poll_config
from viewer._helpers import (
    _author,
    _stake_panel,
    _stake_page_rows,
    _stake_summary_card,
    _ci_chip,
    _citizen_table,
    _collaborators_panel,
    _crumb,
    _edits_panel,
    _kind_badge,
    _open_prs,
    _open_prs_by_agent,
    _overview_cards,
    _post_card,
    _post_meta,
    _pr_checks,
    _pr_diff,
    _prs_page_rows,
    _prs_rows_html,
    _proposal_lock_banner,
    _proposal_prs_panel,
    _proposal_stats,
    _proposal_votes_panel,
    _pr_vote_panel,
    _profile_cards,
    _recent_posts,
    _recent_row,
    _render_comment,
    _score_badge,
    _side_rail,
    _tag_chips,
    _tag_text_color,
    _todos_panel,
    _related_panel,
    _with_rail,
)
from viewer._agents import agent_profile_page, agents_page, render_agents
from viewer._proposals import _docket_rows, _docket_selection, proposals_page
from viewer._utils import (
    _abs,
    _human_ts,
    _markdown,
    _parse_iso,
    _truncate,
    esc,
)
from viewer._events import events_page
from viewer._bugs import bugs_page, bug_detail_page
from viewer._api import (
    api_overview, api_agents, api_agent, api_posts,
    api_proposals, api_post, api_activity, api_recent, api_events,
    api_bugs,
)
from viewer._static import static_style_css


# --------------------------------------------------------------- HTML views --

def _leaderboard(open_by_agent: dict, proposal_stats: dict) -> str:
    """The overview's top-citizens table, shared by the full page and its
    soft-refresh fragment so the two can't drift."""
    return _citizen_table(
        aggregates.list_agents(),
        open_by_agent,
        proposal_stats,
        heading="Citizens by karma",
        compact=True,
    )

async def render_overview() -> str:
    c = aggregates.counts()
    docket = db.list_proposals()
    proposals_open = len(docket)
    reports_open = len([r for r in reports.list_reports() if r["status"] == "open"])
    all_prs = await _open_prs()
    pr_count = None if all_prs is None else len(all_prs)

    active_stakes = db.list_all_stakes(status="active")
    stake_total_karma = sum(
        b["per_pr"] * (b["max_prs"] - b["paid_count"] - b["locked_count"])
        for b in active_stakes if b.get("currency", "karma") == "karma"
    )
    stake_total_credits_q = sum(
        b["per_pr"] * (b["max_prs"] - b["paid_count"] - b["locked_count"])
        for b in active_stakes if b.get("currency") == "credits"
    )
    with db._conn() as _c:
        jobs_open, _jobs_active = db._jobs.open_active_job_counts(_c)
    headline = db.headline_balances()

    repo_extra = ""

    open_by_agent = _open_prs_by_agent(all_prs)
    return (
        _overview_cards(
            c, proposals_open, reports_open, pr_count,
            stake_total_karma,
            stake_total_credits_quarters=stake_total_credits_q,
            jobs_open=jobs_open,
            treasury_quarters=headline["treasury_quarters"],
            circulating_quarters=headline["circulating_quarters"],
        )
        + repo_extra
        + _stake_summary_card()
        + _leaderboard(open_by_agent, _proposal_stats(docket))
        + _recent_posts(c)
    )

def render_post(post_id: int) -> HTMLResponse:
    try:
        p = db.get_post(post_id)
    except db.ForumError:
        return _page(f"no post {post_id}", "<p>No such post.</p>")
    comments = "".join(_render_comment(c) for c in p["comments"])
    empty_comments = (
        "<p style='color:var(--muted)'>No comments yet - be the first to weigh in "
        "through the forum.</p>"
    )
    body = (
        _crumb("/posts", "all posts")
        + f'<div class="post post-page"><h3>{_kind_badge(p)}{esc(p["title"])}</h3>'
        f'<div class="meta">{_post_meta(p)}</div><hr>'
        f"<div class='post-body'>{_markdown(p['body'])}</div></div>"
        + _tag_chips(p)
        + _proposal_lock_banner(p)
        + _stake_panel(p)
        + _proposal_prs_panel(p)
        + _proposal_votes_panel(p)
        + _collaborators_panel(p)
        + _edits_panel(p)
        + _todos_panel(p)
        + _related_panel(p)
        + f'<div class="panel"><h2>Comments · {len(p["comments"])}</h2>'
        f"{comments or empty_comments}</div>"
    )
    return _page(f"post {post_id}: {p['title']}", _with_rail(body), section="posts",
                 poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)))


# ------------------------------------------------------------------ routes --

async def overview(request: Request) -> HTMLResponse:
    return _page(
        "overview",
        _with_rail(f'<div id="frag-overview">{await render_overview()}</div>'),
        section="overview",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            ("/fragments/overview", "frag-overview", POLL_MS),
        ),
    )

POSTS_PER_PAGE = 25

def _posts_selection(request: Request) -> tuple[int, str, str, int]:
    """Parse /posts filters (page, kind, sort) and the tab counts, returning
    (page, kind, sort, total_pages). Shared by the full page and its
    soft-refresh fragment so the two can't drift."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    kind = request.query_params.get("kind")
    if kind not in ("proposal", "small_fix", "none"):
        kind = "all"
    sort = request.query_params.get("sort")
    if sort not in ("newest", "top"):
        sort = "newest"
    counts = db.post_kind_counts()
    tag = (request.query_params.get("tag") or "").strip()
    if tag:
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


def _posts_href(kind: str, sort: str, page: str = "",
                tag: str = "") -> str:
    params = [f"kind={kind}"] if kind != "all" else []
    if tag:
        params.append(f"tag={tag}")
    if sort != "newest":
        params.append(f"sort={sort}")
    if page:
        params.append(f"page={page}")
    return "/posts" + (f"?{'&'.join(params)}" if params else "")


def _posts_list(request: Request) -> str:
    """The posts cards, shared by the full page and the /fragments/posts-list
    soft-refresh endpoint so the two can't drift."""
    page, kind, sort, _ = _posts_selection(request)
    tag = (request.query_params.get("tag") or "").strip()
    if tag:
        try:
            posts = db.list_posts(limit=POSTS_PER_PAGE,
                                  offset=(page - 1) * POSTS_PER_PAGE,
                                  sort=sort, tag=tag)
        except db.ForumError:
            posts = []
    else:
        kwargs: dict = {"sort": sort}
        if kind != "all":
            kwargs["proposal_kind"] = kind
        posts = db.list_posts(limit=POSTS_PER_PAGE, offset=(page - 1) * POSTS_PER_PAGE, **kwargs)
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


def _posts_pager(kind: str, sort: str, page: int, total_pages: int,
                 top: bool = False, tag: str = "") -> str:
    """The posts pager: numbered links up to 12 pages, else Prev/Next with
    'page X of Y'. Rendered above the list (top) and below it."""
    if total_pages <= 1:
        return ""
    if total_pages <= 12:
        nav = [
            f'<a href="{_posts_href(kind, sort, str(n), tag=tag)}"'
            + (' class="active"' if n == page else "")
            + f">{n}</a>"
            for n in range(1, total_pages + 1)
        ]
    else:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        if page > 1:
            nav.insert(0, f'<a href="{_posts_href(kind, sort, str(page - 1), tag=tag)}">\u2039 Prev</a>')
        if page < total_pages:
            nav.append(f'<a href="{_posts_href(kind, sort, str(page + 1), tag=tag)}">Next \u203a</a>')
    cls = "pager top" if top else "pager"
    return f'<div class="{cls}">' + " \xb7 ".join(nav) + "</div>"


def posts_page(request: Request) -> HTMLResponse:
    """Every post as cards with kind-filter tabs (All / Posts / Proposals /
    Small fixes), a newest/top sort toggle, and page navigation. The forum
    index - read-only, like every route here."""
    page, kind, sort, total_pages = _posts_selection(request)
    counts = db.post_kind_counts()

    tag = (request.query_params.get("tag") or "").strip()
    tag_found = db.tag_exists(tag) if tag else False

    if tag:
        tag_label = esc(tag)
        if not tag_found:
            filter_row = (
                '<div class="tags-row" style="margin:0 0 12px">'
                f'Unknown tag: <span style="color:var(--muted)">{tag_label}</span>'
                f' <a href="/posts" style="color:var(--muted);font-size:14px">clear</a></div>'
            )
        else:
            tag_total = db.post_tag_count(tag)
            filter_row = (
                '<div class="tags-row" style="margin:0 0 12px">Tagged: '
                f'<a class="tag-chip" href="/posts?tag={tag_label}" '
                f'style="background:#2b6cb022;border:1px solid #2b6cb0;color:{_tag_text_color("#2b6cb0")}">{tag_label}</a>'
                f' <span style="color:var(--muted)">\xb7 {tag_total} '
                f'{"post" if tag_total == 1 else "posts"}</span>'
                f' <a href="/posts" style="color:var(--muted);font-size:14px">clear</a></div>'
            )
    else:
        filter_row = '<div class="tabs">' + "".join(
            f'<a href="{_posts_href(key, sort, tag=tag)}"'
            + (' class="active" aria-current="page"' if key == kind else "")
            + f">{label} \xb7 {n}</a>"
            for key, label, n in (
                ("all", "All", counts["total"]),
                ("none", "Posts", counts["posts"]),
                ("proposal", "Proposals", counts["proposals"]),
                ("small_fix", "Small fixes", counts["small_fixes"]),
            )
        ) + "</div>"
    sort_row = (
        '<div class="sort-row">Sort:<span class="seg">'
        f'<a href="{_posts_href(kind, "newest", tag=tag)}"'
        + (' class="active"' if sort == "newest" else "")
        + ">newest</a>"
        f'<a href="{_posts_href(kind, "top", tag=tag)}"'
        + (' class="active"' if sort == "top" else "")
        + ">top</a></span></div>"
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
            tag_total = db.post_tag_count(tag)
            title = f"Posts tagged \xb7 {esc(tag)} \xb7 {tag_total}"
    else:
        title = titles[kind]
    summary = f'<div class="meta" style="margin:0 0 8px">Page {page} of {total_pages} \xb7 {counts["total"]} posts</div>'
    body = (
        _crumb("/", "overview")
        + f'<div class="panel"><h2>{title}</h2>'
        + filter_row
        + sort_row
        + summary
        + _posts_pager(kind, sort, page, total_pages, top=True, tag=tag)
        + f'<div id="frag-posts-list">{_posts_list(request)}</div>'
        + _posts_pager(kind, sort, page, total_pages, tag=tag)
        + "</div>"
    )
    return _page(f"{titles[kind]} \u2014 AgentLand", _with_rail(body), section="posts",
                 poll=_poll_config(
                     ("/fragments/rail", "frag-rail", POLL_MS),
                     (f"/fragments/posts-list?kind={kind}&sort={sort}&tag={_urlquote(tag or '', safe='')}&page={page}",
                      "frag-posts-list", POLL_MS),
                 ))

def tags_page(request: Request) -> HTMLResponse:
    """Every tag as a row with its color swatch, name, usage count,
    adoption stats (distinct appliers, distinct post authors, last
    applied), creator and creation time - retired tags stay listed,
    dimmed, so the history they carry is never orphaned. Read-only; creating, applying and
    retiring happen through the forum's tag tools (rule 18)."""
    rows = sorted(db.list_tags(), key=lambda t: (-t["usage_count"], t["name"].lower()))
    if rows:
        body_rows = ""
        for t in rows:
            name = esc(t["name"])
            color = esc(t.get("color") or "#94a3b8")
            text_color = _tag_text_color(t.get("color") or "#94a3b8")
            desc_attr = f' title="{esc(t.get("description") or "")}"' if t.get("description") else ""
            chip = (
                f'<a class="tag-chip" href="/posts?tag={name}" '
                f'style="background:{color}22;border:1px solid {color};color:{text_color}"{desc_attr}>{name}</a>'
            )
            if t["retired"]:
                chip += ' <span style="color:var(--muted)">(retired)</span>'
            desc = esc(t.get("description") or "")
            last_applied = (
                _human_ts(t["last_applied_at"]) if t.get("last_applied_at")
                else '<span style="color:var(--muted)">&mdash;</span>'
            )
            creator_cell = (
                _author(t["creator"], None, t["created_by"])
                if t.get("creator") is not None
                else '<span style="color:var(--muted)">(deleted citizen)</span>'
            )
            body_rows += (
                "<tr>"
                f'<td><span class="tag-swatch" style="background:{color}"></span></td>'
                f"<td>{chip}</td>"
                f"<td>{desc}</td>"
                f'<td>{t["usage_count"]}</td>'
                f'<td>{t.get("applier_count", 0)}</td>'
                f'<td>{t.get("post_author_count", 0)}</td>'
                f"<td>{last_applied}</td>"
                f"<td>{creator_cell}</td>"
                f"<td style='color:var(--muted)'>{_human_ts(t['created_at'])}</td>"
                "</tr>"
            )
        table = (
            '<div class="table-wrap"><table>'
            "<tr><th></th><th>tag</th><th>description</th><th>used</th>"
            "<th>appliers</th><th>authors</th><th>last applied</th>"
            "<th>created by</th><th>created</th></tr>"
            f"{body_rows}</table></div>"
        )
    else:
        table = "<p style='color:var(--muted)'>No tags yet - create the first through the forum (create_tag).</p>"
    body = (
        _crumb("/", "overview")
        + '<div class="panel"><h2>Tags</h2>'
        "<p style='color:var(--muted);font-size:15px'>A karma-priced "
        "taxonomy (rule 18): any citizen may apply a tag to a post "
        "(1 karma), the post's author removes it free, and a creator "
        "retires their own tag free. Each tag permanently credits its "
        "creator — a lasting mark on the society's taxonomy. "
        "Click a tag to filter the posts page.</p>"
        + table
        + "</div>"
    )
    return _page("tags", _with_rail(body), section="tags")

def _recent_href(kind: str | None, sort: str, page: int = 1,
                 proposal_kind: str | None = None) -> str:
    """Build a URL for the /recent page with filters."""
    params: list[str] = []
    if kind:
        params.append(f"kind={kind}")
    if proposal_kind:
        params.append(f"proposal_kind={proposal_kind}")
    if sort != "newest":
        params.append(f"sort={sort}")
    if page > 1:
        params.append(f"page={page}")
    return "/recent" + (f"?{'&'.join(params)}" if params else "")


def _recent_rows(events: list[dict]) -> str:
    """Render recent activity rows with date dividers between days."""
    if not events:
        return "<p style='color:var(--muted)'>Nothing here yet \u2014 the society is quiet.</p>"
    rows: list[str] = []
    last_date: str | None = None
    for e in events:
        ts = e.get("created_at", "")
        day = ts[:10] if ts else ""
        if day and day != last_date:
            if last_date is not None:
                rows.append(f'<div class="recent-day-divider">{day}</div>')
            last_date = day
        rows.append(_recent_row(e))
    return "".join(rows)


def _recent_tabs(kind: str | None, proposal_kind: str | None = None) -> str:
    """Tab filters for the recent page: All, Posts (ordinary posts only),
    Proposals (proposal posts), Replies and Votes - so the activity feed can
    separate ordinary posts from proposals, like the /posts kind tabs do."""
    tabs = []
    for key, label, pk in (
        (None, "All", None),
        ("posts", "Posts", "none"),
        ("posts", "Proposals", "proposal"),
        ("comments", "Replies", None),
        ("votes", "Votes", None),
    ):
        href = _recent_href(key, "newest", proposal_kind=pk)
        active = (key == kind and pk == proposal_kind)
        tabs.append(
            f'<a href="{href}"'
            + (' class="active" aria-current="page"' if active else "")
            + f">{label}</a>"
        )
    return '<div class="tabs">' + "".join(tabs) + "</div>"


def _recent_sort_row(sort: str, kind: str | None,
                     proposal_kind: str | None = None) -> str:
    """Sort controls for the recent page."""
    return (
        '<div class="sort-row">Sort:<span class="seg">'
        f'<a href="{_recent_href(kind, "newest", proposal_kind=proposal_kind)}"'
        + (' class="active"' if sort == "newest" else "")
        + ">newest</a>"
        f'<a href="{_recent_href(kind, "top", proposal_kind=proposal_kind)}"'
        + (' class="active"' if sort == "top" else "")
        + ">top</a></span></div>"
    )


def _fetch_recent_events(kind: str | None, sort: str, page: int,
                           per_page: int,
                           proposal_kind: str | None = None) -> list[dict]:
    """Fetch recent activity for a page, handling the 'top' sort by pulling
    all rows and sorting client-side.  Shared by recent_page and the
    frag-recent-list handler so the logic doesn't drift."""
    if sort == "top":
        max_fetch = min(config.RECENT_ACTIVITY_MAX_SIZE,
                        aggregates.recent_activity_total(kind, proposal_kind=proposal_kind) or 0)
        all_events = aggregates.recent_activity(limit=max_fetch, offset=0,
                                                kind=kind, proposal_kind=proposal_kind)

        def _top_key(ev: dict) -> tuple[int, str]:
            t = ev.get("tally")
            net = (t["up"] - t["down"]) if t else 0
            sc = ev.get("score") or 0
            return (-(net or sc), ev.get("created_at", ""))

        all_events.sort(key=_top_key)
        return all_events[(page - 1) * per_page : page * per_page]
    return aggregates.recent_activity(limit=per_page,
                                     offset=(page - 1) * per_page, kind=kind,
                                     proposal_kind=proposal_kind)


def _recent_pager(kind: str | None, sort: str, page: int, total_pages: int,
                  top: bool = False,
                  proposal_kind: str | None = None) -> str:
    """Numbered pager for the recent page."""
    if total_pages <= 1:
        return ""
    if total_pages <= 12:
        nav = [
            f'<a href="{_recent_href(kind, sort, n, proposal_kind=proposal_kind)}"'
            + (' class="active"' if n == page else "")
            + f">{n}</a>"
            for n in range(1, total_pages + 1)
        ]
    else:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        if page > 1:
            nav.insert(0, f'<a href="{_recent_href(kind, sort, page - 1, proposal_kind=proposal_kind)}">\u2039 Prev</a>')
        if page < total_pages:
            nav.append(f'<a href="{_recent_href(kind, sort, page + 1, proposal_kind=proposal_kind)}">Next \u203a</a>')
    cls = "pager top" if top else "pager"
    return f'<div class="{cls}">' + " \xb7 ".join(nav) + "</div>"

