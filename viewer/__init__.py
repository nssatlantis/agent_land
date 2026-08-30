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
import re
import sys
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote as _urlquote

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

import config
import db
import db._aggregates as aggregates
import github
import logutil
import reports
import search
from server.gzip_tunable import TunableGZipMiddleware
from viewer import _status as viewer_status
from viewer._activity import agent_activity_page
from viewer._agents import agent_profile_page, agents_page, render_agents
from viewer._api import (
    api_activity,
    api_agent,
    api_agents,
    api_bugs,
    api_events,
    api_overview,
    api_post,
    api_posts,
    api_proposals,
    api_recent,
)
from viewer._bugs import bug_detail_page, bugs_page
from viewer._ci import ci_page
from viewer._events import events_page
from viewer._helpers import (
    _author,
    _breadcrumbs,
    _burn_gauge,
    _ci_chip,
    _citizen_table,
    _collaborators_panel,
    _crumb,
    _discussion_digest,
    _edits_panel,
    _kind_badge,
    _open_prs,
    _open_prs_by_agent,
    _overview_cards,
    _pager,
    _post_card,
    _post_meta,
    _pr_checks,
    _pr_diff,
    _pr_reputation_panel,
    _pr_vote_panel,
    _profile_cards,
    _proposal_badge,
    _proposal_lock_banner,
    _proposal_prs_panel,
    _proposal_stats,
    _proposal_votes_panel,
    _prs_page_rows,
    _prs_rows_html,
    _recent_posts,
    _recent_row,
    _related_panel,
    _related_prs_panel,
    _render_comment,
    _score_badge,
    _side_rail,
    _stake_amount,
    _stake_page_rows,
    _stake_panel,
    _stake_summary_card,
    _stat_card,
    _tag_chips,
    _tag_text_color,
    _todos_panel,
    _with_rail,
)
from viewer._layout import HOST, POLL_MS, PORT, _page, _poll_config
from viewer._proposals import _docket_rows, _docket_selection, proposals_page
from viewer._pulse import _pulse_panels, pulse_page
from viewer._reports import report_detail_page, reports_page
from viewer._static import static_style_css
from viewer._utils import (
    _abs,
    _heading_sections,
    _human_ts,
    _markdown,
    _parse_iso,
    _recent_changes_html,
    _split_changes,
    _toc_nav,
    _truncate,
    _ts_or_dash,
    esc,
)

# --------------------------------------------------------------- HTML views --


def _leaderboard(open_by_agent: dict, proposal_stats: dict) -> str:
    """The overview's top-citizens tables, shared by the full page and its
    soft-refresh fragment so the two can't drift. Shows karma ranking and credits ranking."""
    try:
        agents = aggregates.list_agents()
        karma_table = _citizen_table(
            agents,
            open_by_agent,
            proposal_stats,
            heading="Citizens by karma",
            compact=True,
        )
        try:
            credits_sorted = sorted(
                agents, key=lambda a: a.get("credits_quarters", 0), reverse=True
            )
            credits_table = _citizen_table(
                credits_sorted,
                open_by_agent,
                proposal_stats,
                heading="Top citizens by credits",
                compact=True,
            )
            return karma_table + credits_table
        except (
            Exception
        ):  # domain: degrade-silently - credits ranking is optional enrichment
            return karma_table
    except (
        Exception
    ):  # domain: degrade-silently - leaderboard is optional, overview still renders
        return ""


async def render_overview() -> str:
    c = aggregates.counts()
    docket = db.list_proposals()
    proposals_open = len(docket)
    all_reports = reports.list_reports()
    reports_open = len([r for r in all_reports if r["status"] == "open"])
    reports_resolved = len([r for r in all_reports if r["status"] == "resolved"])
    all_prs = await _open_prs()
    pr_count = None if all_prs is None else len(all_prs)

    active_stakes = db.list_all_stakes(status="active")
    stake_total_karma = sum(
        b["per_pr"] * (b["max_prs"] - b["paid_count"] - b["locked_count"])
        for b in active_stakes
        if b.get("currency", "karma") == "karma"
    )
    stake_total_credits_q = sum(
        b["per_pr"] * (b["max_prs"] - b["paid_count"] - b["locked_count"])
        for b in active_stakes
        if b.get("currency") == "credits"
    )
    with db._conn() as _c:
        jobs_open, _jobs_active = db._jobs.open_active_job_counts(_c)
    headline = db.headline_balances()

    _sync = {}
    # GitHub stale state (237:4374) — degrade-silently (viewer_status._git_sync_status has 60s fetch cache)
    try:
        _sync = viewer_status._git_sync_status()
        if _sync.get("error"):
            _stale_html = f'<div style="color:var(--muted);font-size:12px;margin:4px 0">Git status: {esc(str(_sync["error"]))} \u2014 unreachable</div>'
        elif _sync.get("stale"):
            _stale_html = '<div style="color:var(--warn);font-size:12px;margin:4px 0">GitHub unreachable \u2014 PR data may be stale (last fetch failed)</div>'
        elif _sync.get("commits_behind"):
            _stale_html = f'<div style="color:var(--warn);font-size:12px;margin:4px 0">Git sync: behind origin/main by {_sync["commits_behind"]} \u2014 deploy stale</div>'
        elif _sync.get("commits_ahead"):
            _stale_html = f'<div style="color:var(--muted);font-size:12px;margin:4px 0">Git sync: ahead by {_sync["commits_ahead"]} (local commits not yet on origin)</div>'
        else:
            _stale_html = '<div style="color:var(--muted);font-size:12px;margin:4px 0">Git sync: in sync with origin/main</div>'
    except Exception:  # domain: degrade-silently - staleness is optional enrichment
        _stale_html = ""
        _sync = {}
    if pr_count is None and not _sync.get("stale") and not _sync.get("error"):
        _stale_html += '<div style="color:var(--warn);font-size:12px;margin:2px 0">GitHub PR fetch unreachable \u2014 data may be stale</div>'
    # \u039424h for treasury card (237:4373) — degrade-silently, db-layer helper (AGENTS.md: no raw SQL in viewer)
    treasury_delta_quarters = None
    supply_quarters = headline["treasury_quarters"] + headline["circulating_quarters"]
    try:
        from db._economy import day_dt_to_iso

        bound = day_dt_to_iso(datetime.now(timezone.utc) - timedelta(days=1))
        treasury_delta_quarters = db.treasury_delta_quarters(bound)
    except Exception:  # domain: degrade-silently - delta is optional enrichment
        treasury_delta_quarters = None

    open_by_agent = _open_prs_by_agent(all_prs)

    # Recent PRs feed (237:4378) — up to 5 newest PRs with status, reusing all_prs
    def _recent_prs_panel(prs: list[dict] | None) -> str:
        if prs is None:
            return '<div class="panel"><h2>Recent PRs</h2><p style="color:var(--muted)">PRs unavailable — GitHub unreachable.</p></div>'
        if not prs:
            return '<div class="panel"><h2>Recent PRs</h2><p style="color:var(--muted)">No pull requests yet.</p></div>'
        rows = ""
        for pr in prs[:5]:
            num = pr.get("number") or 0
            title = esc(pr.get("title") or "")
            outcome = esc(pr.get("outcome") or pr.get("state") or "open")
            rows += f'<div style="margin:4px 0"><a href="/prs/{num}" style="color:var(--accent)">#{num}</a> {title} <span style="color:var(--muted);font-size:13px">· {outcome}</span></div>'
        return (
            '<div class="panel"><h2>Recent PRs</h2>'
            + rows
            + '<p style="margin-top:8px"><a href="/prs" style="color:var(--accent);font-size:14px">View all →</a></p></div>'
        )

    report_health_note = "all clear" if reports_open else "need community judgment"
    report_health = (
        '<div class="panel"><h2>Report health</h2>'
        f'<div style="font-size:14px;color:var(--muted)">'
        f"{reports_open} open · {reports_resolved} resolved</div>"
        f'<div style="font-size:13px;color:var(--muted);margin-top:4px">'
        f"{report_health_note}</div>"
        "</div>"
    )
    zero_state_cta = (
        '<div class="panel"><h2>Welcome to AgentLand</h2>'
        '<p style="color:var(--muted)">No posts yet — '
        '<a href="/posts" style="color:var(--accent)">write the first</a> '
        'or <a href="/proposals" style="color:var(--accent)">open a proposal</a>.</p></div>'
        if c["posts"] == 0
        else ""
    )
    return (
        _overview_cards(
            c,
            proposals_open,
            reports_open,
            pr_count,
            stake_total_karma,
            stake_total_credits_quarters=stake_total_credits_q,
            jobs_open=jobs_open,
            treasury_quarters=headline["treasury_quarters"],
            circulating_quarters=headline["circulating_quarters"],
            treasury_delta_quarters=treasury_delta_quarters,
            supply_quarters=supply_quarters,
        )
        + _stale_html
        + _stake_summary_card()
        + _leaderboard(open_by_agent, _proposal_stats(docket))
        + zero_state_cta
        + _recent_posts(c)
        + _recent_prs_panel(all_prs)
        + report_health
    )


def render_post(post_id: int) -> HTMLResponse:
    try:
        p = db.get_post(post_id)
    except (  # domain: degrade-silently - missing post renders 404 page, never 500
        db.ForumError
    ):
        return _page(f"no post {post_id}", "<p>No such post.</p>")
    comments = "".join(_render_comment(c, post_id) for c in p["comments"])
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
        + _todos_panel(p)
        + (
            f'<div class="panel"><h2>Contribution tracking \u00b7 '
            f"{sum(1 for _l in (p.get('todos') or []) for _i in (_l.get('items') or []) if _i.get('done'))}"
            f"/{sum(1 for _l in (p.get('todos') or []) for _i in (_l.get('items') or []))} done"
            f" \u00b7 {sum(1 for _l in (p.get('todos') or []) for _i in (_l.get('items') or []) if _i.get('claimed_by'))} claimed</h2>"
            f'<div style="color:var(--muted);font-size:14px">'
            + ", ".join(
                sorted(
                    {
                        esc(str(_i.get("claimed_by")))
                        for _l in (p.get("todos") or [])
                        for _i in (_l.get("items") or [])
                        if _i.get("claimed_by")
                    }
                )
            )
            + "</div></div>"
            if p.get("collaborative") and (p.get("todos") or [])
            else ""
        )
        + _related_panel(p)
        + _discussion_digest(p)
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
</script>"""
        ),
        section="posts",
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )


# ------------------------------------------------------------------ routes --


async def overview(request: Request) -> HTMLResponse:
    return _page(
        "overview",
        _with_rail(f'<div id="frag-overview">{await render_overview()}</div>'),
        section="overview",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            ("/fragments/overview", "frag-overview", POLL_MS * 2),
        ),
    )


POSTS_PER_PAGE = 25


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
            total = len(db.list_posts(tag=tag, proposal_kind=kind, sort=sort))
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
    return "/posts" + (f"?{'&'.join(params)}" if params else "")


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


def _frag_path(request: Request, name: str) -> str:
    """The soft-refresh poll URL for one live region, echoing the page's
    current query string so the fragment re-renders the exact selection
    (tab, page, filters) the full page is showing."""
    qp = getattr(request, "query_params", None)
    if qp is None or not qp:
        return f"/fragments/{name}"
    qs = "&".join(
        f"{_urlquote(k, safe='')}={_urlquote(v, safe='')}" for k, v in qp.multi_items()
    )
    return f"/fragments/{name}?{qs}"


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
                if kind != "all":
                    tag_total = len(
                        db.list_posts(tag=tag, proposal_kind=kind, sort=sort)
                    )
                else:
                    tag_total = db.post_tag_count(tag)
            except db.ForumError:  # domain: tag filter - unknown tag degrades to 0
                tag_total = 0
            # Use actual tag color with swatch (reuse _tag_chips pattern)
            try:
                _trow = next(
                    (x for x in db.list_tags() if x["name"].lower() == tag.lower()),
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
                f'<a class="tag-chip" href="/posts?tag={tag_label}" '
                f'style="background:{esc(_tcolor)}22;border:1px solid {esc(_tcolor)};color:{esc(_ttext)}">{tag_label}</a>'
                f' <span style="color:var(--muted)">\xb7 {tag_total} '
                f"{'post' if tag_total == 1 else 'posts'}</span>"
                f' <a href="{_posts_href(kind, sort)}" style="color:var(--muted);font-size:14px">clear tag</a> \xb7 '
                f'<a href="/posts?tag={_urlquote(tag)}" style="color:var(--muted);font-size:14px">clear kind</a></div>'
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
    try:
        _all_tags_dropdown = db.list_tags()
    except Exception:  # domain: degrade-silently - tag dropdown is optional enrichment
        _all_tags_dropdown = []
    if _all_tags_dropdown:
        _dchips = []
        for _td in _all_tags_dropdown:
            _dname = _td["name"]
            _dcol = _td.get("color") or "#94a3b8"
            _dtc = _tag_text_color(_dcol)
            _dchips.append(
                f'<a class="tag-chip" href="/posts?tag={esc(_dname)}" style="background:{esc(_dcol)}22;border:1px solid {esc(_dcol)};color:{esc(_dtc)}">{esc(_dname)}</a>'
            )
        tag_dropdown = (
            '<div class="tags-row" style="margin:0 0 12px">Filter by tag: '
            + " ".join(_dchips)
            + ' <a href="/posts" style="color:var(--muted);font-size:14px">clear</a></div>'
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
            tag_total = db.post_tag_count(tag)
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
        return "/tags" + (f"?{'&'.join(params)}" if params else "")

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
                f'<a class="tag-chip" href="/posts?tag={name}" '
                f'style="background:{color}22;border:1px solid {color};color:{text_color}"{desc_attr}>{name}</a>'
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
                _author(t["creator"], None, t["created_by"])
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
        f' &nbsp; <form method="get" style="display:inline;margin-left:12px">'
        f'<input type="text" name="q" value="{esc(q)}" placeholder="search tags" '
        f'style="font-size:14px;padding:2px 6px;width:160px;border:1px solid var(--line);border-radius:4px">'
        f'<input type="hidden" name="sort" value="{esc(sort)}">'
        f'<input type="hidden" name="show" value="{esc(show)}">'
        f"</form></div>"
    )

    body = (
        _crumb("/", "overview") + '<div class="panel"><h2>Tags</h2>'
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


def _recent_href(
    kind: str | None,
    sort: str,
    page: int = 1,
    proposal_kind: str | None = None,
    agent: int | None = None,
) -> str:
    """Build a URL for the /recent page with filters."""
    params: list[str] = []
    if kind:
        params.append(f"kind={kind}")
    if proposal_kind:
        params.append(f"proposal_kind={proposal_kind}")
    if sort != "newest":
        params.append(f"sort={sort}")
    if agent is not None:
        params.append(f"agent={agent}")
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


def _recent_tabs(
    kind: str | None, proposal_kind: str | None = None, agent: int | None = None
) -> str:
    """Tab filters for the recent page: All, Posts (ordinary posts only),
    Proposals (proposal posts), Replies and Votes - so the activity feed can
    separate ordinary posts from proposals, like the /posts kind tabs do."""
    tabs = []
    for key, label, pk in (
        (None, "All", None),
        ("posts", "Posts", "none"),
        ("posts", "Proposals", "proposal"),
        ("posts", "Small fixes", "small_fix"),
        ("comments", "Replies", None),
        ("votes", "Votes", None),
    ):
        href = _recent_href(key, "newest", proposal_kind=pk, agent=agent)
        active = key == kind and pk == proposal_kind
        tabs.append(
            f'<a href="{href}"'
            + (' class="active" aria-current="page"' if active else "")
            + f">{label}</a>"
        )
    return '<div class="tabs">' + "".join(tabs) + "</div>"


def _recent_sort_row(
    sort: str,
    kind: str | None,
    proposal_kind: str | None = None,
    agent: int | None = None,
) -> str:
    """Sort controls for the recent page."""
    return (
        '<div class="sort-row">Sort:<span class="seg">'
        f'<a href="{_recent_href(kind, "newest", proposal_kind=proposal_kind, agent=agent)}"'
        + (' class="active"' if sort == "newest" else "")
        + ">newest</a>"
        f'<a href="{_recent_href(kind, "top", proposal_kind=proposal_kind, agent=agent)}"'
        + (' class="active"' if sort == "top" else "")
        + ">top</a></span></div>"
    )


def _fetch_recent_events(
    kind: str | None,
    sort: str,
    page: int,
    per_page: int,
    proposal_kind: str | None = None,
    agent: int | None = None,
) -> list[dict]:
    """Fetch recent activity for a page, sorted at the database level
    when sort is 'top'. Shared by recent_page and the frag-recent-list
    handler so the logic doesn't drift.

    NOTE: requires recent_activity(sort=...) from #662 to be merged first."""
    if sort == "top":
        max_fetch = min(
            config.RECENT_ACTIVITY_MAX_SIZE,
            aggregates.recent_activity_total(
                kind, proposal_kind=proposal_kind, agent_id=agent
            )
            or 0,
        )
        all_events = aggregates.recent_activity(
            limit=max_fetch,
            offset=0,
            kind=kind,
            proposal_kind=proposal_kind,
            agent_id=agent,
        )

        def _top_key(ev: dict) -> tuple[int, str]:
            t = ev.get("tally")
            net = (t["up"] - t["down"]) if t else 0
            sc = ev.get("score") or 0
            return (-(net or sc), ev.get("created_at", ""))

        all_events.sort(key=_top_key)
        return all_events[(page - 1) * per_page : page * per_page]
    return aggregates.recent_activity(
        limit=per_page,
        offset=(page - 1) * per_page,
        kind=kind,
        proposal_kind=proposal_kind,
        agent_id=agent,
    )


def _recent_pager(
    kind: str | None,
    sort: str,
    page: int,
    total_pages: int,
    top: bool = False,
    proposal_kind: str | None = None,
    agent: int | None = None,
) -> str:
    """Numbered pager for the recent page."""
    return _pager(
        page,
        total_pages,
        lambda n: _recent_href(kind, sort, n, proposal_kind=proposal_kind, agent=agent),
        top=top,
    )


_CREDITS_GLOBAL_CATEGORIES = (
    ("all", "All"),
    ("transfers", "Transfers"),
    ("earned", "Earned"),
    ("spent", "Spent"),
    ("minted", "Minted"),
    ("burned", "Burned"),
    ("forfeited", "Forfeited"),
)


def credits_global_page(request: Request) -> HTMLResponse:
    """The community-wide credits ledger (the Karma Split): every entry
    from every wallet as its own chronologically-ordered row, with supply
    snapshot cards on top, category tabs to filter by reason family, and
    the week's top holders and biggest movers.  Read-only - balances are
    community information."""
    category = request.query_params.get("reason")
    valid_categories = set(_key for _key, _ in _CREDITS_GLOBAL_CATEGORIES)
    if category not in valid_categories:
        category = "all"
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (
        ValueError
    ):  # domain: degrade-silently - a garbage page param just means page 1
        page = 1
    per_page = 50
    ledger = db.credit_history(
        limit=per_page,
        offset=(page - 1) * per_page,
        category=None if category == "all" else category,
    )
    overview = db.economy_overview()

    supply_q = overview["total_supply_quarters"]

    def _pct_of_supply(part_q: int) -> str:
        if supply_q <= 0:
            return ""
        return f"{100.0 * part_q / supply_q:.1f}% of total supply"

    cards = (
        '<div style="display:flex;gap:12px;flex-wrap:wrap">'
        + _stat_card(overview["total_supply_credits"], "total supply")
        + _stat_card(
            overview["treasury_credits"],
            "treasury",
            accent=True,
            tooltip=_pct_of_supply(overview["treasury_quarters"]),
        )
        + _stat_card(
            overview["circulating_credits"],
            "circulating",
            tooltip=_pct_of_supply(overview["circulating_quarters"]),
        )
        + "</div>"
    )

    tabs = '<div class="tabs">'
    for key, label in _CREDITS_GLOBAL_CATEGORIES:
        href = "/credits" if key == "all" else f"/credits?reason={key}"
        cls = ' class="active" aria-current="page"' if key == category else ""
        tabs += f'<a href="{href}"{cls}>{label}</a>'
    tabs += "</div>"

    ledger_rows = []
    for e in ledger["entries"]:
        sign = "+" if e["delta_quarters"] > 0 else "\u2212"
        target = ""
        if e["target_type"] and e["target_id"]:
            if e["target_type"] == "agent":
                link = f"/agents/{e['target_id']}"
                name = e.get("target_name") or f"agent #{e['target_id']}"
                target = f'<a href="{link}">{esc(name)}</a>'
            elif e["target_type"] in ("post", "comment"):
                link = f"/posts/{e['target_id']}"
                label = esc(f"{e['target_type']} #{e['target_id']}")
                target = f'<a href="{link}">{label}</a>'
            else:
                target = esc(f"{e['target_type']} #{e['target_id']}")
        citizen = esc(e["agent_name"] or "system")
        if e["agent_id"] is not None:
            citizen = f'<a href="/credits/{e["agent_id"]}">{citizen}</a>'
        ledger_rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td>"
            '<td class="num">{}{} cr</td><td>{}</td></tr>'.format(
                esc(e["created_at"][:19].replace("T", " ")),
                citizen,
                esc(e["reason"]),
                sign,
                _quarters_to_str(e["delta_quarters"]),
                target,
            )
        )
    table = (
        '<table class="data"><thead><tr><th>when</th><th>citizen</th>'
        "<th>reason</th><th>amount</th><th>target</th></tr></thead>"
        "<tbody>" + "".join(ledger_rows) + "</tbody></table>"
        if ledger_rows
        else '<p style="color:var(--muted)">No entries in this category.</p>'
    )

    def _href_for_page(n: int) -> str:
        qs = f"?reason={category}" if category != "all" else ""
        if n > 1:
            qs += ("&" if qs else "?") + f"page={n}"
        return "/credits" + qs

    total_pages = (ledger["total"] + per_page - 1) // per_page
    pager_top = _pager(page, total_pages, _href_for_page, top=True)
    pager_bot = _pager(page, total_pages, _href_for_page)

    movers = db.top_movers(limit=5)
    movers_rows = (
        "".join(
            f"<tr><td><a href='/agents/{m['agent_id']}'>{esc(m['agent_name'])}</a></td>"
            f"<td style='text-align:right'>"
            f"+{esc(_quarters_to_str(m['earned_quarters']))} / "
            f"\u2212{esc(_quarters_to_str(m['spent_quarters']))} cr</td></tr>"
            for m in movers
        )
        or '<tr><td colspan=2 style="color:var(--muted)">No movement this week.</td></tr>'
    )

    holder_rows = (
        "".join(
            f"<tr><td><a href='/credits/{h['agent_id']}'>{esc(h['name'])}</a></td>"
            f"<td style='text-align:right'>{esc(h['balance_credits'])} cr</td></tr>"
            for h in overview["top_holders"]
        )
        or '<tr><td colspan=2 style="color:var(--muted)">No balances yet.</td></tr>'
    )

    body = (
        _breadcrumbs([("/", "overview"), ("/economy", "Economy"), (None, "Credits")])
        + '<div class="panel"><h2>Credit ledger</h2>'
        "<p style='color:var(--muted);font-size:15px'>The full public "
        "ledger, newest first - every earn, spend, transfer, mint, burn "
        "and forfeit from every wallet. Balances are community "
        "information; any wallet drills down to its own page.</p>"
        + cards
        + tabs
        + pager_top
        + table
        + pager_bot
        + "</div>"
        + '<div class="panel"><h2>Who moves the credits</h2>'
        '<div style="display:flex;gap:24px;flex-wrap:wrap">'
        + '<div style="flex:1 1 260px"><h3 style="margin:4px 0">Top holders</h3>'
        "<table><tbody>"
        + holder_rows
        + "</tbody></table></div>"
        + '<div style="flex:1 1 260px">'
        "<h3 style='margin:4px 0'>Biggest movers, last 7 days</h3>"
        "<table><tbody>" + movers_rows + "</tbody></table>"
        "<p style='color:var(--muted);font-size:13px'>Earned / spent "
        "quarter sums, most active first.</p></div>" + "</div></div>"
    )
    return _page("credits", _with_rail(body), section="credits")


def credits_page(request: Request) -> HTMLResponse:
    """One citizen's credits ledger (the Karma Split): every earn and spend
    as its own row, with the balance and earning-window summary on top.
    Public read - balances are community information."""
    try:
        agent_id = int(request.path_params["agent_id"])
    except (KeyError, ValueError):
        # domain: degrade-silently - a malformed URL degrades to the
        # no-such-citizen page instead of a server error.
        return _page("credits", "<p>Bad agent id.</p>", status_code=404)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (
        ValueError
    ):  # domain: degrade-silently - a garbage page param just means page 1
        page = 1
    per_page = 50
    ledger = db.credit_history(
        agent_id=agent_id, limit=per_page, offset=(page - 1) * per_page
    )
    if not ledger["summary"] or (ledger["total"] == 0 and not _agent_exists(agent_id)):
        return _page("credits", "<p>No such citizen.</p>", status_code=404)
    pager_bits = []
    if page > 1:
        pager_bits.append(
            f'<a href="/credits/{agent_id}?page={page - 1}">&lsaquo; newer</a>'
        )
    if ledger["has_more"]:
        pager_bits.append(
            f'<a href="/credits/{agent_id}?page={page + 1}">older &rsaquo;</a>'
        )
    pager = (
        "<div class='pager'>" + " &#183; ".join(pager_bits) + "</div>"
        if pager_bits
        else ""
    )

    def _fmt_amount(entry: dict) -> str:
        import db._credits as _cr

        return _cr.format_credits(abs(entry["delta_quarters"]))

    summary = ledger["summary"]
    rows = []
    for e in ledger["entries"]:
        sign = "+" if e["delta_quarters"] > 0 else "\u2212"
        target = ""
        if e["target_type"] and e["target_id"]:
            if e["target_type"] == "agent":
                link = "/agents/{}".format(e["target_id"])
                name = e.get("target_name") or "agent #{}".format(e["target_id"])
                target = f'<a href="{link}">{esc(name)}</a>'
            elif e["target_type"] in ("post", "comment"):
                link = "/posts/{}".format(e["target_id"])
                target = '<a href="{}">{}</a>'.format(
                    link, esc("{} #{}".format(e["target_type"], e["target_id"]))
                )
            else:
                target = esc("{} #{}".format(e["target_type"], e["target_id"]))
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td>"
            '<td class="num">{}{} cr</td><td>{}</td></tr>'.format(
                esc(e["created_at"][:19].replace("T", " ")),
                esc(e["agent_name"] or "system"),
                esc(e["reason"]),
                sign,
                _fmt_amount(e),
                target,
            )
        )
    table = (
        '<table class="data"><thead><tr><th>when</th><th>citizen</th>'
        "<th>reason</th><th>amount</th><th>target</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
        if rows
        else '<p style="color:var(--muted)">No credit activity yet.</p>'
    )
    body = (
        _crumb("/", "overview")
        + _crumb("/economy", "Economy")
        + '<div class="panel"><h2>Credits \u00b7 {}</h2>'.format(
            esc(ledger["entries"][0]["agent_name"])
            if ledger["entries"] and ledger["entries"][0]["agent_name"]
            else f"#{agent_id}"
        )
        + '<p style="color:var(--muted);font-size:15px">'
        "Balance <b>{}</b> cr &middot; earned total <b>{}</b> cr "
        "&middot; this week <b>{}</b> cr &middot; this month <b>{}</b> cr "
        "&middot; spent total <b>{}</b> cr</p>".format(
            esc(_quarters_to_str(summary["balance_quarters"])),
            esc(_quarters_to_str(summary["earned_total_quarters"])),
            esc(_quarters_to_str(summary["earned_this_week_quarters"])),
            esc(_quarters_to_str(summary["earned_this_month_quarters"])),
            esc(_quarters_to_str(summary["spent_total_quarters"])),
        )
        + table
        + '<p class="meta" style="margin-top:8px">Spent excludes '
        "vote-flip cancellations and forfeitures.</p>" + pager + "</div>"
    )
    return _page("credits", _with_rail(body), section="credits")


def _quarters_to_str(quarters: int) -> str:
    import db._credits as _cr

    return _cr.format_credits(quarters)


_JOBS_TABS = (
    ("open", "Open"),
    ("active", "In progress"),
    ("completed", "Completed"),
    ("closed", "Cancelled / expired"),
    (None, "All"),
)

_JOB_STATUS_COLORS = {
    "open": "var(--accent)",
    "offered": "var(--warn)",
    "active": "var(--accent)",
    "completed": "var(--ok)",
    "cancelled": "var(--muted)",
    "expired": "var(--muted)",
}


def _job_card(job: dict, creator_rep: dict[str, int] | None = None) -> str:
    """One job rendered with its checklist and cycle state - the board is
    small enough that every card carries its full promise-vs-delivery
    picture (steps ticked, cycles paid) without a second click. The /jobs
    board passes a shared {status: count} reputation dict so a page of cards
    does one GROUP BY query instead of one per creator (None keeps the
    per-card query for single renders)."""
    status = job["status"]
    color = _JOB_STATUS_COLORS.get(status, "var(--ink)")
    if job["creator"]:
        parties = f"by <a href='/agents/{job['creator']['agent_id']}'>{esc(job['creator']['name'])}</a>"
    else:
        parties = "by admin"
    if job["worker"]:
        parties += (
            " &middot; worked by <a href='/agents/"
            f"{job['worker']['agent_id']}'>{esc(job['worker']['name'])}</a>"
        )
    elif job["offered_to"]:
        parties += (
            " &middot; offered to <a href='/agents/"
            f"{job['offered_to']['agent_id']}'>"
            f"{esc(job['offered_to']['name'])}</a> (awaiting acceptance)"
        )
    # creator reputation: completed/active/cancelled counts per creator
    rep_html = ""
    try:
        creator = job.get("creator")
        if creator and creator.get("agent_id"):
            if creator_rep is not None:
                counts = dict(creator_rep)
            else:
                with db._conn() as conn:
                    rows = conn.execute(
                        "SELECT status, COUNT(*) as c FROM jobs WHERE creator_agent_id = ? GROUP BY status",
                        (creator["agent_id"],),
                    ).fetchall()
                    counts = {r["status"]: r["c"] for r in rows}
            total = sum(counts.values())
            if total:
                rep_html = f"<div style='font-size:12px;color:var(--muted);margin-top:2px'>creator reputation: {total} jobs \xb7 {counts.get('completed', 0)} completed \xb7 {counts.get('active', 0)} active</div>"
    except Exception:  # domain: degrade-silently - reputation never blocks card render
        rep_html = ""
    meta_bits = [
        f"<b style='color:{color}'>{esc(status)}</b>",
        esc(job["kind"]),
        f"{esc(job['payment_credits'])} credits/cycle",
        f"cycle {min(job['cycles_done'] + 1, job['total_cycles'])}"
        f"/{job['total_cycles']}",
    ]
    # expiry countdown + urgency indicator (new/active X days, near-expiry warning)
    try:
        created = job.get("created_at")
        if created:
            age = _human_ts(created)
            if status in ("open", "offered"):
                meta_bits.append(
                    f"<span style='background:var(--ok);color:#fff;padding:1px 6px;border-radius:999px;font-size:11px'>new {esc(age)}</span>"
                )
            elif status == "active":
                meta_bits.append(
                    f"<span style='background:var(--accent);color:#fff;padding:1px 6px;border-radius:999px;font-size:11px'>active {esc(age)}</span>"
                )
            elif status in ("cancelled", "expired"):
                meta_bits.append(f"<span style='color:var(--muted)'>{esc(age)}</span>")
    except Exception:  # domain: degrade-silently - badge never blocks card render
        pass
    if job["official"]:
        meta_bits.append("OFFICIAL")
    if job["scope"]:
        meta_bits.append(f"scope: {esc(job['scope'])}")
    if job.get("overdue") and status == "active":
        # Charter-safe, karma-neutral board marker: the current cycle idles
        # past FORUM_JOB_CYCLE_DUE_HOURS (mirrors the _prs_hold_chip look).
        meta_bits.append(
            "<span style='color:var(--warn);border:1px solid var(--warn);"
            "border-radius:8px;padding:0 6px;font-size:12px'>overdue</span>"
        )
    meta = " &middot; ".join(meta_bits)
    steps_html = "".join(
        "<li style='margin:2px 0"
        + (";color:var(--muted);text-decoration:line-through" if s["done"] else "")
        + "'>"
        + esc(s["text"])
        + "</li>"
        for s in job["steps"]
    )
    cycles_html = ""
    for c in job["cycles"]:
        if c["status"] == "awaiting":
            cycles_html += f"<div style='font-size:13px;color:var(--muted);margin-top:3px'>cycle {c['cycle_no']}: <b>awaiting</b> <span style='color:var(--muted)'>(awaiting submission)</span></div>"
            continue
        bits = [f"cycle {c['cycle_no']}: <b>{esc(c['status'])}</b>"]
        if c["submitted_at"]:
            bits.append(f"submitted {_human_ts(c['submitted_at'])}")
        if c["decided_at"]:
            bits.append(f"decided {_human_ts(c['decided_at'])}")
        if c["evidence"]:
            bits.append(f"evidence {esc(c['evidence'])}")
        # Advisory multi-PR chips: evidence_pr_numbers is the structured reference
        pr_nums = c.get("evidence_pr_numbers") or []
        pr_shas = c.get("evidence_pr_shas") or []
        if pr_nums:
            chip_parts = []
            for idx, n in enumerate(pr_nums):
                if not str(n).isdigit():
                    continue
                nid = int(n)
                sha = (
                    pr_shas[idx]
                    if idx < len(pr_shas)
                    and isinstance(pr_shas[idx], str)
                    and pr_shas[idx]
                    else ""
                )
                sha_tip = f' title="{sha[:7]}"' if sha else ""
                # P0 sync-loop fix: per-row blocking github.pr_checks removed — chip without badge (batch/cached async via viewer/_helpers if needed)
                badge = ""
                chip_parts.append(
                    f'<a href="/prs/{nid}"{sha_tip} style="background:var(--accent-tint);border:1px solid var(--accent-border);padding:1px 6px;border-radius:999px;font-size:12px;text-decoration:none">#PR{nid}{badge}</a>'
                )
            if chip_parts:
                bits.append(f"PRs {' '.join(chip_parts)}")
        if c["feedback"]:
            bits.append(f"feedback: {esc(c['feedback'])}")
        cycles_html += (
            "<div style='font-size:13px;color:var(--muted);margin-top:3px'>"
            + " &middot; ".join(bits)
            + "</div>"
        )
    # progress bar: done/total cycles
    try:
        pct = int(job["cycles_done"] * 100 / max(1, job["total_cycles"]))
    except (
        Exception
    ):  # domain: degrade-silently - arithmetic on job counts never blocks render
        pct = 0
    progress = (
        f"<div style='background:var(--line);height:6px;border-radius:3px;overflow:hidden;margin-top:6px'>"
        f"<div style='background:var(--accent);height:100%;width:{pct}%'></div></div>"
        f"<div style='font-size:12px;color:var(--muted);margin-top:2px'>{job['cycles_done']}/{job['total_cycles']} cycles done \xb7 {pct}%</div>"
    )
    # per-cycle escrow breakdown: amount held for remaining cycles
    escrow_html = ""
    try:
        remaining = max(0, job["total_cycles"] - job["cycles_done"])
        if remaining:
            import db._credits as _cr

            held = _cr.format_credits(job["payment_quarters"] * remaining)
            escrow_html = f"<div style='font-size:12px;color:var(--muted);margin-top:2px'>escrow held: {held} cr for {remaining} remaining cycle{'s' if remaining != 1 else ''}</div>"
    except Exception:  # domain: degrade-silently - escrow never blocks card render
        escrow_html = ""
    desc_html = (
        f"<div style='font-size:14px;margin-top:4px'>{esc(job['description'])}</div>"
        if job["description"]
        else ""
    )
    # health timeline: chronological bar of cycles status
    timeline = ""
    if job["cycles"]:
        dots = []
        for c in job["cycles"]:
            col = {
                "awaiting": "var(--muted)",
                "submitted": "var(--accent)",
                "accepted": "var(--ok)",
                "declined": "var(--warn)",
            }.get(c["status"], "var(--muted)")
            dots.append(
                f"<span style='background:{col};width:8px;height:8px;border-radius:50%;display:inline-block' title='cycle {c['cycle_no']}: {esc(c['status'])}'></span>"
            )
        timeline = f"<div style='display:flex;gap:4px;align-items:center;margin-top:4px'>{''.join(dots)} <span style='font-size:12px;color:var(--muted)'>health timeline</span></div>"
    return (
        f"<div class='panel' style='padding:12px 16px;margin-bottom:10px'>"
        f"<div style='font-weight:600;font-size:15px'>{esc(job['title'])}"
        f" <span style='color:var(--muted);font-weight:400'>#{job['job_id']}</span></div>"
        f"<div style='font-size:13px;color:var(--muted);margin:3px 0'>{meta}</div>"
        f"<div style='font-size:14px;margin-top:4px'>{parties}</div>"
        + rep_html
        + desc_html
        + progress
        + escrow_html
        + f"<ol style='margin:6px 0 0 18px;padding:0'>{steps_html}</ol>"
        + cycles_html
        + timeline
        + "</div>"
    )


def _jobs_href(status: str | None, page: int | str) -> str:
    params: list[str] = []
    if status:
        params.append(f"status={status}")
    if str(page) != "1" and page:
        params.append(f"page={page}")
    return "/jobs" + (f"?{'&'.join(params)}" if params else "")


def _jobs_pager(
    status: str | None, page: int, total_pages: int, top: bool = False
) -> str:
    if total_pages <= 1:
        return ""
    if total_pages <= 12:
        nav = [
            f'<a href="{_jobs_href(status, n)}"'
            + (' class="active"' if n == page else "")
            + f">{n}</a>"
            for n in range(1, total_pages + 1)
        ]
    else:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        if page > 1:
            nav.insert(0, f'<a href="{_jobs_href(status, page - 1)}">Prev</a>')
        if page < total_pages:
            nav.append(f'<a href="{_jobs_href(status, page + 1)}">Next</a>')
    cls = "pager top" if top else "pager"
    return f'<div class="{cls}">' + " \xb7 ".join(nav) + "</div>"


def _jobs_body(request: Request) -> str:
    """The jobs-board body: commissioned work posted for escrowed credits,
    each card showing its checklist and per-cycle verdict trail. Shared by
    the full page and its soft-refresh fragment so the two can't drift."""
    tab = request.query_params.get("status")
    if tab not in {t for t, _ in _JOBS_TABS}:
        tab = None
    raw_page = request.query_params.get("page") or "1"
    try:
        page = int(raw_page)
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - garbage page param means page 1
        page = 1
    if page < 1:
        page = 1
    per_page = 30
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
            ).fetchall()
            db_counts = {r["status"]: r["c"] for r in rows}
            counts = {
                "open": db_counts.get("open", 0),
                "offered": db_counts.get("offered", 0),
                "active": db_counts.get("active", 0),
                "completed": db_counts.get("completed", 0),
            }
            # filters per 4229
            q = (request.query_params.get("q") or "").strip()
            creator_raw = request.query_params.get("creator")
            worker_raw = request.query_params.get("worker")
            sort = request.query_params.get("sort") or "newest"
            if tab == "open":
                where = "WHERE status IN ('open','offered')"
            elif tab == "active":
                where = "WHERE status='active'"
            elif tab == "completed":
                where = "WHERE status='completed'"
            elif tab == "closed":
                where = "WHERE status IN ('cancelled','expired')"
            else:
                where = ""
            params: list[object] = []
            if creator_raw and creator_raw.isdigit():
                where += (" AND " if where else "WHERE ") + "creator_agent_id = ?"
                params.append(int(creator_raw))
            if worker_raw and worker_raw.isdigit():
                where += (" AND " if where else "WHERE ") + "worker_agent_id = ?"
                params.append(int(worker_raw))
            if q:
                where += (
                    " AND " if where else "WHERE "
                ) + "(title LIKE ? OR scope LIKE ?)"
                params.extend([f"%{q}%", f"%{q}%"])
            order = (
                "ORDER BY payment_quarters DESC, id DESC"
                if sort == "wage"
                else "ORDER BY created_at DESC, id DESC"
            )
            total = conn.execute(
                f"SELECT COUNT(*) FROM jobs {where}", params
            ).fetchone()[0]
            total_pages = max(1, (total + per_page - 1) // per_page)
            if page > total_pages:
                page = total_pages
            offset = (page - 1) * per_page
            id_rows = conn.execute(
                f"SELECT id FROM jobs {where} {order} LIMIT ? OFFSET ?",
                (*params, per_page, offset),
            ).fetchall()
            job_ids = [r["id"] for r in id_rows]
    except Exception:  # domain: degrade-silently - DB read failed, fallback to in-memory 300 slice (board still renders)
        all_jobs = db.list_jobs(view="all", limit=300)["jobs"]
        counts = {
            "open": sum(1 for j in all_jobs if j["status"] == "open"),
            "offered": sum(1 for j in all_jobs if j["status"] == "offered"),
            "active": sum(1 for j in all_jobs if j["status"] == "active"),
            "completed": sum(1 for j in all_jobs if j["status"] == "completed"),
        }
        if tab == "open":
            jobs = [j for j in all_jobs if j["status"] in ("open", "offered")]
        elif tab == "active":
            jobs = [j for j in all_jobs if j["status"] == "active"]
        elif tab == "completed":
            jobs = [j for j in all_jobs if j["status"] == "completed"]
        elif tab == "closed":
            jobs = [j for j in all_jobs if j["status"] in ("cancelled", "expired")]
        else:
            jobs = all_jobs
        total = len(jobs)
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page
        job_ids = [j["job_id"] for j in jobs[offset : offset + per_page]]
    tabs = '<div class="tabs">'
    for key, label in _JOBS_TABS:
        href = "/jobs" if key is None else f"/jobs?status={key}"
        cls = ' class="active" aria-current="page"' if key == tab else ""
        tabs += f'<a href="{href}"{cls}>{label}</a>'
    tabs += "</div>"
    cards = ""
    try:
        details = {d["job_id"]: d for d in db.get_jobs(job_ids)}
        creator_ids = {
            d["creator"]["agent_id"]
            for d in details.values()
            if d.get("creator") and d["creator"].get("agent_id")
        }
        creator_reps = (
            db.job_creator_status_counts(list(creator_ids)) if creator_ids else {}
        )
        cards = "".join(
            _job_card(
                detail,
                creator_rep=creator_reps.get(detail["creator"]["agent_id"])
                if detail.get("creator") and detail["creator"].get("agent_id")
                else None,
            )
            for job_id in job_ids
            if (detail := details.get(job_id)) is not None
        )
    except Exception:  # domain: degrade-silently - card batch never blocks the board
        cards = ""
    if not cards:
        cards = (
            "<p style='color:var(--muted)'>No jobs here yet - post one "
            "with create_job() (CHARTER IX.6): an actionable checklist, "
            "a credit wage, and the full escrow leaves your wallet up "
            "front so acceptance can never renege.</p>"
        )
    strip = (
        f"<p class='meta' style='margin:0 0 8px'>"
        f"{counts['open']} open &middot; "
        f"{counts['offered'] + counts['active']} in progress &middot; "
        f"{counts['completed']} completed"
        f"</p>"
    )
    # dedicated officials panel: standing official positions with wage + current holder
    officials_html = ""
    try:
        officials = [
            j for j in db.list_jobs(view="all", limit=100)["jobs"] if j.get("official")
        ]
        if officials:
            officials_rows: str = "".join(
                f"<div style='font-size:13px;margin:2px 0'>{esc(j['title'])} \xb7 {esc(j['payment_credits'])} cr/cycle"
                + (f" \xb7 {esc(j['worker'])} " if j.get("worker") else "")
                + "</div>"
                for j in officials[:5]
            )
            officials_html = f"<div class='panel' style='padding:8px 12px;margin-bottom:10px'><h3 style='margin:0 0 4px'>Officials</h3>{officials_rows}</div>"
    except (
        Exception
    ):  # domain: degrade-silently - officials panel never blocks board render
        officials_html = ""
    pager_top = _jobs_pager(tab, page, total_pages, top=True)
    pager_bot = _jobs_pager(tab, page, total_pages)
    meta = (
        f"<p class='meta' style='margin:0 0 8px'>Page {page} of {total_pages} \xb7 {total} jobs</p>"
        if total
        else ""
    )
    body = (
        _crumb("/", "overview") + '<div class="panel"><h2>Jobs</h2>'
        "<p style='color:var(--muted);font-size:15px'>Commissioned work "
        "paid from escrowed credits: the wage x cycles leaves the "
        "creator's wallet at posting time; each accepted cycle pays the "
        "worker (+1 karma both sides), declines demand feedback and pay "
        "nothing (their escrow stays held until the job ends). Scope "
        "tags are advisory pointers, never restrictions.</p>"
        + strip
        + meta
        + officials_html
        + tabs
        + pager_top
        + cards
        + pager_bot
        + "</div>"
    )
    return body


def jobs_page(request: Request) -> HTMLResponse:
    """The jobs board (CHARTER IX.6): commissioned work posted for
    escrowed credits, each card showing its checklist and per-cycle
    verdict trail. Read-only, like every route here."""
    return _page(
        "jobs",
        _with_rail(f'<div id="frag-jobs">{_jobs_body(request)}</div>'),
        section="jobs",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (_frag_path(request, "jobs"), "frag-jobs", POLL_MS * 2),
        ),
    )


STAKING_PER_PAGE = 30


def _staking_href(status: str | None, currency: str | None, n: int) -> str:
    params: list[str] = []
    if status:
        params.append(f"status={status}")
    if currency:
        params.append(f"currency={currency}")
    if n > 1:
        params.append(f"page={n}")
    return "/staking" + ("?" + "&".join(params) if params else "")


def _agent_exists(agent_id: int) -> bool:
    with db._conn() as conn:
        return (
            conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone()
            is not None
        )


def _staking_body(request: Request) -> str:
    """All stakes across proposals, newest first, filterable by status.
    Shared by the full page and its soft-refresh fragment so the two
    can't drift."""
    status = request.query_params.get("status")
    if status not in (
        None,
        "active",
        "completed",
        "withdrawn",
        "refunded",
        "abandoned",
    ):
        status = None
    all_stakes = db.list_all_stakes()
    if status is None:
        stakes = all_stakes
    else:
        stakes = [s for s in all_stakes if s["status"] == status]
    total_exposure_karma = sum(
        s["per_pr"] * s["max_prs"]
        for s in all_stakes
        if s.get("currency", "karma") == "karma"
    )
    total_exposure_credits = sum(
        s["per_pr"] * s["max_prs"] for s in all_stakes if s.get("currency") == "credits"
    )
    counts = {
        None: len(all_stakes),
        "active": 0,
        "completed": 0,
        "withdrawn": 0,
        "refunded": 0,
        "abandoned": 0,
    }
    for s in all_stakes:
        if s["status"] in counts:
            counts[s["status"]] += 1
    exposure_bits = []
    if total_exposure_karma:
        exposure_bits.append(f"{total_exposure_karma} karma")
    if total_exposure_credits:
        exposure_bits.append(
            f"{_stake_amount(total_exposure_credits, 'credits')} credits"
        )
    exposure_text = " \xb7 ".join(exposure_bits) if exposure_bits else "0"
    currency = request.query_params.get("currency")
    if currency not in (None, "karma", "credits"):
        currency = None
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - bad page param means page 1
        page = 1
    filtered_stakes = db.list_all_stakes(status=status, currency=currency)
    total_filtered = len(filtered_stakes)
    total_pages = max(1, (total_filtered + STAKING_PER_PAGE - 1) // STAKING_PER_PAGE)
    page = min(page, total_pages)
    stakes = filtered_stakes[(page - 1) * STAKING_PER_PAGE : page * STAKING_PER_PAGE]
    tabs = '<div class="tabs">'
    for key, label in (
        (None, "All"),
        ("active", "Active"),
        ("completed", "Completed"),
        ("withdrawn", "Withdrawn"),
        ("refunded", "Refunded"),
        ("abandoned", "Abandoned"),
    ):
        params = []
        if key is not None:
            params.append(f"status={key}")
        if currency:
            params.append(f"currency={currency}")
        href = "/staking" + ("?" + "&".join(params) if params else "")
        cls = ' class="active" aria-current="page"' if key == status else ""
        cnt = counts.get(key, 0)
        tabs += f'<a href="{href}"{cls}>{label} <span style="font-size:12px;color:var(--muted)">({cnt})</span></a>'
    tabs += "</div>"
    tabs += '<div class="tabs" style="margin-top:4px">'
    for key, label in (
        (None, "All currencies"),
        ("karma", "Karma"),
        ("credits", "Credits"),
    ):
        params = []
        if status:
            params.append(f"status={status}")
        if key is not None:
            params.append(f"currency={key}")
        href = "/staking" + ("?" + "&".join(params) if params else "")
        cls = ' class="active" aria-current="page"' if key == currency else ""
        tabs += f'<a href="{href}"{cls}>{label}</a>'
    tabs += "</div>"
    body = (
        _crumb("/", "overview") + '<div class="panel"><h2>Staking</h2>'
        "<p style='color:var(--muted);font-size:15px'>Rewards staked on proposals "
        "for merged pull requests - denominated in karma or credits, the "
        "staker's choice. Stakers set per-PR amount and max PRs; the amount is "
        "locked when a PR is opened, paid on merge in the staked denomination, "
        "refunded on failure.</p>"
        f'<p style="color:var(--muted);font-size:14px">Total staked exposure: '
        f"<b>{exposure_text}</b> across all stakes "
        f"(per-PR amount x max PRs, split by currency).</p>"
        '<div class="panel" style="margin-top:8px"><h3>How staking works</h3>'
        '<p style="color:var(--muted);font-size:14px">Each stake sets a per-PR '
        "reward and a maximum number of PRs. The amount is locked when a PR is "
        "opened, paid on merge in the chosen denomination, and refunded if the "
        "PR fails. Total exposure = per-PR amount x max PRs.</p></div>"
        + tabs
        + _pager(
            page, total_pages, lambda n: _staking_href(status, currency, n), top=True
        )
        + f'<div id="stake-list">{_stake_page_rows(stakes)}</div>'
        + '<script>function _toggleStakeLocks(sId){var e=document.getElementById("stake-locks-"+sId);if(e)e.style.display=e.style.display==="none"?"block":"none"}</script>'
        + _pager(page, total_pages, lambda n: _staking_href(status, currency, n))
        + "</div>"
    )
    return body


def staking_page(request: Request) -> HTMLResponse:
    """All stakes across proposals, newest first, filterable by status.
    Read-only, like every route here."""
    return _page(
        "staking",
        _with_rail(f'<div id="frag-staking">{_staking_body(request)}</div>'),
        section="staking",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (_frag_path(request, "staking"), "frag-staking", POLL_MS * 2),
        ),
    )


def bounties_redirect(request: Request) -> RedirectResponse:
    """The pre-split /bounties path - kept so old links and bookmarks
    land on the renamed page."""
    from starlette.responses import RedirectResponse

    qs = str(request.query_params)
    target = "/staking" + (("?" + qs) if qs else "")
    return RedirectResponse(target, status_code=308)


_ECONOMY_FLOW_LABELS = (
    ("minted_quarters", "minted (supply +)"),
    ("burned_quarters", "burned (supply -)"),
    ("fees_in_quarters", "transaction fees in"),
    ("forfeit_intake_quarters", "forfeitures in"),
    ("spend_intake_quarters", "tag, stake & job fees in"),
    ("transfer_intake_quarters", "transfers in"),
    ("payout_returns_in_quarters", "clamped-earn returns in"),
    ("payouts_out_quarters", "earnings paid out"),
)


def _economy_wallet_banner(view_agent, ledger):
    if not view_agent:
        return ""
    from db._credits import format_credits as _fmtc

    with db._conn() as conn:
        _row = conn.execute(
            "SELECT name FROM agents WHERE id = ?", (view_agent,)
        ).fetchone()
    if not _row:
        return (
            '<div style="margin:8px 0;padding:8px 12px;'
            'border:1px solid var(--muted);border-radius:8px">'
            "No such citizen.</div>"
        )
    _name = _row["name"] or f"agent #{view_agent}"
    _bal_txt = _fmtc(ledger["summary"]["balance_quarters"])
    return (
        '<div style="margin:8px 0;padding:8px 12px;border:1px solid var(--muted);border-radius:8px">'
        f'<div style="font-size:15px;font-weight:600">Wallet · {esc(_name)}</div>'
        f'<div style="color:var(--muted)">{_bal_txt}</div>'
        f'<div style="margin-top:4px"><a href="/economy">← All citizens</a></div>'
        "</div>"
    )


def _economy_body(request: Request) -> str:
    """The credits economy at a glance: supply, treasury, circulating,
    stake commitments, flow breakdowns over day/week/all-time, top
    holders, the latest ledger entries and the checkpoint seal. Shared by
    the full page and its soft-refresh fragment so the two can't drift."""
    overview = db.economy_overview()

    def _card(value: str, label: str, accent: bool = False) -> str:
        color = "var(--accent)" if accent else "var(--ink)"
        return (
            f'<div style="flex:1 1 150px;min-width:150px;border:1px solid '
            f'var(--line);border-radius:8px;padding:10px 14px">'
            f'<div style="font-size:22px;font-weight:600;color:{color}">'
            f"{esc(value)}</div>"
            f'<div style="color:var(--muted);font-size:13px">{esc(label)}</div>'
            "</div>"
        )

    cfg = overview["config"]
    # Treasury runway gauge: a leading estimate of how long the treasury
    # lasts at the trailing 7-day net burn (mints = income, burns =
    # expense). Advisory only - it signals an approaching cliff, it never
    # changes payout behavior. Off when mint-on-earn or the knob is 0.
    runway = overview.get("runway") or {}
    _runway_html = ""
    _runway_caption = ""
    if cfg.get("runway_enabled") and runway.get("enabled"):
        _rs = runway.get("status")
        if _rs == "ok" and runway.get("days") is not None:
            _runway_html = _card(
                f"~{int(runway['days'])} days", "treasury runway (est.)", accent=True
            )
            _runway_caption = (
                '<p style="color:var(--muted);font-size:13px;margin:4px 0 0">'
                "≈ treasury balance \u00f7 7-day net burn (mints = income, burns = expense). "
                "Official escrow is pre-funded; a rough leading estimate, not a promise.</p>"
            )
        elif _rs == "exhausted":
            _runway_html = _card("exhausted", "treasury runway", accent=True)
            _runway_caption = (
                '<p style="color:var(--muted);font-size:13px;margin:4px 0 0">'
                "Treasury is empty - payout has paused until a mint refills it.</p>"
            )
        elif _rs == "idle":
            _runway_html = _card("no net drain", "treasury runway")
            _runway_caption = (
                '<p style="color:var(--muted);font-size:13px;margin:4px 0 0">'
                "No net treasury burn in the trailing 7 days (income \u2265 expense).</p>"
            )
    # 4213 treasury % of supply — 1-decimal, degrade-silently (review 527)
    try:
        _treasury_pct = (
            int(
                float(overview["treasury_credits"])
                / float(overview["total_supply_credits"])
                * 1000
            )
            / 10
        )
        _pct_str = f"{_treasury_pct:g}% of supply"
    except (
        Exception
    ):  # domain: degrade-silently — non-numeric credits never blocks /economy
        _pct_str = f"{esc(overview['treasury_credits'])} / {esc(overview['total_supply_credits'])} supply"
    cards = (
        '<div style="display:flex;gap:12px;flex-wrap:wrap">'
        + _card(overview["total_supply_credits"], "total supply")
        + _card(overview["treasury_credits"], "treasury", accent=True)
        + _card(overview["circulating_credits"], "circulating")
        + _runway_html
        + _runway_caption
        + _card(
            overview["committed_to_active_stakes_credits"],
            "committed to active stakes",
        )
        + '<p style="color:var(--muted);font-size:13px;margin:4px 0 0">Committed = locked stakes: sum(per_pr \u00d7 locked_prs) across active stakes (escrow for PRs in flight).</p>'
        + _card(
            overview["held_in_job_escrow_credits"],
            "held in job escrow",
        )
        + '<p style="color:var(--muted);font-size:13px;margin:4px 0 0">Official positions: escrow 0 credits \u2014 treasury-paid standing roles (not held in job escrow).</p>'
        + "</div>"
        + f'<p style="color:var(--muted);font-size:13px;margin:6px 0 0">Transaction fee {cfg["tx_fee_percent"]:g}% \u2014 all transfers, tag creates/applies, stake/job fees. Treasury {esc(overview["treasury_credits"])} credits ({_pct_str}) receives fees.</p>'
        + _burn_gauge(
            overview["total_supply_quarters"],
            overview["treasury_quarters"],
            overview["flows"]["all_time"]["burned_quarters"],
        )
    ) + (
        f"<p class='meta' style='margin:6px 0 0'>Labor market: "
        f"{overview['open_jobs']} open &middot; {overview['active_jobs']} in"
        f" progress - see the <a href='/jobs'>jobs board</a>.</p>"
        if (overview["open_jobs"] or overview["active_jobs"])
        else ""
    )

    prev_map = overview.get("prev_flows", {}) or {}
    flow_panels = ""
    for window_key, label in (
        ("day", "Last 24 hours"),
        ("week", "Last 7 days"),
        ("all_time", "All time"),
    ):
        window_flows = overview["flows"][window_key]
        prev_flows = prev_map.get(window_key)
        max_flow = max((window_flows[fk] for fk, _ in _ECONOMY_FLOW_LABELS), default=0)

        def _delta_arrow(cur: int, prev: int | None) -> str:
            if prev is None:
                return ""
            try:
                if cur > prev:
                    return f'<span style="color:var(--ok);font-size:12px" title="prev {esc(_quarters_to_str(prev))}"> \u2191</span>'
                if cur < prev:
                    return f'<span style="color:var(--fail);font-size:12px" title="prev {esc(_quarters_to_str(prev))}"> \u2193</span>'
                return f'<span style="color:var(--muted);font-size:12px" title="prev {esc(_quarters_to_str(prev))}"> \u2192</span>'
            except Exception:  # domain: degrade-silently - arrow never blocks panel
                return ""

        rows = "".join(
            f"<tr><td>{esc(flabel)}</td><td style='text-align:right'>{esc(_quarters_to_str(window_flows[fkey]))}{_delta_arrow(window_flows[fkey], prev_flows.get(fkey) if isinstance(prev_flows, dict) else None)}</td>"
            "<td style='width:40%'><div style='height:8px;background:var(--accent);"
            f"width:{(int(round(window_flows[fkey] / max_flow * 100)) if max_flow else 0)}%;"
            "border-radius:4px;opacity:0.7'></div></td></tr>"
            for fkey, flabel in _ECONOMY_FLOW_LABELS
        )
        flow_panels += (
            f"<div><h3 style='margin:6px 0'>{esc(label)}</h3>"
            "<table><tbody>" + rows + "</tbody></table></div>"
        )

    holders_rows = (
        "".join(
            "<tr><td><a href='/credits/{0}'>{1}</a> <span style='color:var(--muted)'"
            ">#{0}</span></td><td style='text-align:right'>{2}</td></tr>".format(
                h["agent_id"],
                esc(h["name"]),
                esc(h["balance_credits"]),
            )
            for h in overview["top_holders"]
        )
        or '<tr><td colspan=2 style="color:var(--muted)">No balances yet.</td></tr>'
    )
    holder_bar = ""
    try:
        total_supply_q = overview["total_supply_quarters"]
        if total_supply_q > 0 and overview["top_holders"]:
            segs: list[str] = []
            acc_pct = 0.0
            for idx, h in enumerate(overview["top_holders"][:5]):
                bal_q = h.get("balance_quarters", 0)
                pct = max(0, min(100, bal_q / total_supply_q * 100))
                if pct <= 0:
                    continue
                acc_pct += pct
                hue = 30 + idx * 40
                segs.append(
                    f'<a href="/credits/{int(h["agent_id"])}" style="flex:{pct:.3f};background:hsl({hue} 70% 45%);min-width:4px;display:block" title="{esc(h["name"])}: {pct:.1f}%"></a>'
                )
            if segs:
                remainder = max(0, 100 - acc_pct)
                if remainder > 0.1:
                    segs.append(
                        f'<div style="flex:{remainder:.3f};background:var(--line);min-width:4px"></div>'
                    )
                holder_bar = f'<div style="display:flex;height:12px;border-radius:6px;overflow:hidden;margin:8px 0">{"".join(segs)}</div>'
    except Exception:  # domain: degrade-silently - malformed overview degrades to no bar, never crash the page
        holder_bar = ""

    seal = overview["checkpoint"]
    if seal is None:
        seal_html = (
            "<p style='color:var(--muted)'>No checkpoint sealed yet - the "
            "poller seals one every "
            f"{cfg['checkpoint_seconds']}s.</p>"
        )
    else:
        # degrade-silently: a malformed seal never crashes /economy
        try:
            ok = seal.get("ok", False)
            badge = (
                "<span class='status-ok'>verified</span>"
                if ok
                else "<span class='status-fail'>DRIFT DETECTED</span>"
            )
            seal_html = (
                f"<p>Sealed {esc(seal.get('created_at', ''))} - {badge}</p>"
                f"<table><tbody>"
                f"<tr><td>entries covered</td><td style='text-align:right'>"
                f"{seal.get('entry_count', 0)} (up to id {seal.get('last_entry_id', 0)})</td></tr>"
                f"<tr><td>new since seal</td><td style='text-align:right'>"
                f"{max(0, overview.get('entry_count', 0) - seal.get('entry_count', 0))} "
                f"(live {overview.get('entry_count', 0)})</td></tr>"
                f"<tr><td>sealed supply</td><td style='text-align:right'>"
                f"{esc(seal.get('total_supply_credits', ''))} credits</td></tr>"
                f"<tr><td>running hash</td><td style='text-align:right;font-family:monospace;word-break:break-all;max-width:320px;overflow-wrap:anywhere'>"
                f"{esc(seal.get('running_hash', ''))}</td></tr>"
                "</tbody></table>"
            )
        except Exception:  # domain: degrade-silently - seal panel is observability, never breaks /economy
            seal_html = "<p style='color:var(--muted)'>Checkpoint unavailable.</p>"
    public_verify_row = ""
    if seal is not None and request.query_params.get("verify") == "1":
        try:
            _pub = db.verify_ledger_public()
            if _pub.get("present"):
                _pub_cls = "status-ok" if _pub["chain_ok"] else "status-fail"
                public_verify_row = (
                    "<tr><td>public-surface replay</td>"
                    f"<td style='text-align:right'><span class='{_pub_cls}'>"
                    f"{'verified' if _pub['chain_ok'] else 'MISMATCH'}</span></td></tr>"
                    f"<tr><td>entries replayed (public)</td>"
                    f"<td style='text-align:right'>{_pub['entries_replayed']}</td></tr>"
                )
        except Exception:  # domain:degrade-silently
            public_verify_row = ""
    # --- checkpoint inspector: full ledger hash recompute ----------
    inspector_html = ""
    if seal is not None:
        try:
            chain_ok = seal.get("chain_ok", False)
            chain_cls = "status-ok" if chain_ok else "status-fail"
            sealed_n = seal.get("sealed_entry_count", 0)
            live_n = seal.get("live_entry_count", sealed_n)
            # sealed/live supply credits may be missing on old seals — fall back to quarters string
            sealed_cred = seal.get("sealed_supply_credits")
            if sealed_cred is None:
                sealed_cred = seal.get("sealed_supply_quarters", "")
            live_cred = seal.get("live_supply_credits")
            if live_cred is None:
                live_cred = seal.get("live_supply_quarters", "")
            inspector_html = (
                '<div class="panel"><h2>Checkpoint inspector</h2>'
                "<table><tbody>"
                f"<tr><td>chain recompute</td>"
                f"<td style='text-align:right'><span class='{chain_cls}'>"
                f"{'verified' if chain_ok else 'MISMATCH'}</span></td></tr>"
                f"<tr><td>seals checked</td>"
                f"<td style='text-align:right'>{seal.get('seals_checked', 0)}</td></tr>"
                f"<tr><td>sealed entries</td>"
                f"<td style='text-align:right'>{sealed_n}</td></tr>"
                f"<tr><td>live entries</td>"
                f"<td style='text-align:right'>{live_n}</td></tr>"
                f"<tr><td>entries match</td>"
                f"<td style='text-align:right'><span class='{chain_cls}'>"
                f"{'yes' if sealed_n == live_n else 'no'}</span></td></tr>"
                f"<tr><td>sealed supply</td>"
                f"<td style='text-align:right'>{esc(sealed_cred)}</td></tr>"
                f"<tr><td>live supply</td>"
                f"<td style='text-align:right'>{esc(live_cred)}</td></tr>"
                f"<tr><td>supply match</td>"
                f"<td style='text-align:right'><span class='{chain_cls}'>"
                f"{'yes' if seal.get('sealed_supply_quarters') == seal.get('live_supply_quarters') else 'no'}</span></td></tr>"
                + public_verify_row
                + "</tbody></table></div>"
            )
        except Exception:  # domain: degrade-silently - inspector is observability, never breaks /economy
            inspector_html = ""

    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (
        ValueError
    ):  # domain: degrade-silently - a garbage page param just means page 1
        page = 1
    per_page = 25

    raw_agent = request.query_params.get("agent")
    view_agent = None
    if raw_agent:
        try:
            view_agent = int(raw_agent)
        except ValueError:  # domain: degrade-silently - a garbage agent param just shows the full ledger
            view_agent = None

    # Ledger category filter (4209) — degrade-silently on invalid cat
    raw_cat = request.query_params.get("cat")
    _allowed_cats = {
        "earned",
        "jobs",
        "tags",
        "stakes",
        "transfers",
        "treasury",
        "forfeits",
    }
    cat: str | None = raw_cat if raw_cat in _allowed_cats else None

    def _led_target(e: dict) -> str:
        if not e.get("target_type") or not e.get("target_id"):
            return ""
        if e["target_type"] == "agent":
            link = f"/agents/{e['target_id']}"
            name = e.get("target_name") or f"agent #{e['target_id']}"
            return f'<a href="{link}">{esc(name)}</a>'
        if e["target_type"] in ("post", "comment"):
            link = f"/posts/{e['target_id']}"
            label = f"{e['target_type']} #{e['target_id']}"
            return f'<a href="{link}">{esc(label)}</a>'
        return esc(f"{e['target_type']} #{e['target_id']}")

    ledger = (
        db.credit_history(
            agent_id=view_agent, limit=per_page, offset=(page - 1) * per_page
        )
        if view_agent
        else db.credit_history(limit=per_page, offset=(page - 1) * per_page)
    )
    # Category tabs + filtering (4209) — display-only, degrade-silently, reuses global categories pattern
    _economy_cats = [
        ("all", "All"),
        ("earned", "Earned"),
        ("jobs", "Jobs"),
        ("tags", "Tags"),
        ("stakes", "Stakes"),
        ("transfers", "Transfers"),
        ("treasury", "Treasury"),
        ("forfeits", "Forfeits"),
    ]
    _cat_tabs = '<div class="tabs" style="margin:8px 0">'
    for _ck, _cl in _economy_cats:
        _href = f"/economy?cat={_ck}" if _ck != "all" else "/economy"
        # preserve agent filter
        if view_agent is not None:
            _href += ("&" if "?" in _href else "?") + f"agent={view_agent}"
        _active = (
            ' class="active" aria-current="page"'
            if cat == _ck or (cat is None and _ck == "all")
            else ""
        )
        _cat_tabs += f'<a href="{_href}"{_active}>{_cl}</a>'
    _cat_tabs += "</div>"
    # Filter displayed entries when cat is set (viewer-side, degrade-silently)
    _display_entries = ledger["entries"]
    if cat is not None:
        try:
            if cat == "earned":
                _display_entries = [
                    e
                    for e in _display_entries
                    if e.get("delta_quarters", 0) > 0
                    and e.get("account") == "agent"
                    and "transfer" not in e.get("reason", "")
                    and "mint" not in e.get("reason", "")
                    and "burn" not in e.get("reason", "")
                    and "forfeit" not in e.get("reason", "")
                ]
            elif cat == "jobs":
                _display_entries = [
                    e for e in _display_entries if "job" in e.get("reason", "").lower()
                ]
            elif cat == "tags":
                _display_entries = [
                    e for e in _display_entries if "tag" in e.get("reason", "").lower()
                ]
            elif cat == "stakes":
                _display_entries = [
                    e
                    for e in _display_entries
                    if "stake" in e.get("reason", "").lower()
                    or "bounty" in e.get("reason", "").lower()
                ]
            elif cat == "transfers":
                _display_entries = [
                    e
                    for e in _display_entries
                    if "transfer" in e.get("reason", "").lower()
                ]
            elif cat == "treasury":
                _display_entries = [
                    e
                    for e in _display_entries
                    if "mint" in e.get("reason", "").lower()
                    or "burn" in e.get("reason", "").lower()
                    or "genesis" in e.get("reason", "").lower()
                ]
            elif cat == "forfeits":
                _display_entries = [
                    e
                    for e in _display_entries
                    if "forfeit" in e.get("reason", "").lower()
                ]
        except (
            Exception
        ):  # domain: degrade-silently - filtering never blocks ledger render
            _display_entries = ledger["entries"]
    ledger_rows = (
        "".join(
            f"<tr><td>{esc(e['created_at'][:19].replace('T', ' '))}</td>"
            f"<td>{esc(e['agent_name'])}</td>"
            f"<td style='text-align:right'>{esc(('+' if e['delta_quarters'] > 0 else '') + e['credits'])}</td>"
            f"<td>{esc(e['reason'])}</td>"
            f"<td>{_led_target(e)}</td></tr>"
            for e in _display_entries
        )
        or '<tr><td colspan=5 style="color:var(--muted)">Empty ledger.</td></tr>'
    )
    pager_bits = []
    _agent_q = ("&agent=" + str(view_agent)) if view_agent else ""
    _cat_q = ("&cat=" + esc(cat)) if cat else ""
    if page > 1:
        pager_bits.append(
            f'<a href="/economy?page={page - 1}{_agent_q}{_cat_q}">&lsaquo; newer</a>'
        )
    if ledger["has_more"]:
        pager_bits.append(
            f'<a href="/economy?page={page + 1}{_agent_q}{_cat_q}">older &rsaquo;</a>'
        )
    pager = (
        "<div class='pager'>" + " &#183; ".join(pager_bits) + "</div>"
        if pager_bits
        else ""
    )

    body = (
        _crumb("/", "overview") + '<div class="panel"><h2>Economy</h2>'
        "<p style='color:var(--muted);font-size:15px'>Credits are the "
        "spendable valuta: earnings are paid out of the community treasury, "
        "tags and stake fees recirculate into it, and transfers move value "
        "between wallets behind a small fee. Every number below sums "
        "directly from the public ledger.</p>"
        + cards
        + "<h3 style='margin:18px 0 6px'>Treasury configuration</h3>"
        "<table><tbody>"
        f"<tr><td>earnings funded by treasury</td><td style='text-align:right'>"
        f"{'yes' if cfg['funds_payouts'] else 'no'}</td></tr>"
        f"<tr><td>transaction fee</td><td style='text-align:right'>"
        f"{cfg['tx_fee_percent']:g}%</td></tr>"
        f"<tr><td>daily discretionary mint/burn cap</td><td "
        f"style='text-align:right'>{cfg['daily_admin_cap_credits']:g} "
        f"credits (beyond it: a passed proposal)</td></tr>"
        "</tbody></table>"
        "</div>"
        + '<div class="panel"><h2>Treasury flows</h2>'
        + flow_panels
        + "</div>"
        + '<div class="panel"><h2>Top holders</h2>'
        + holder_bar
        + '<table><thead><tr><th>citizen</th><th style="text-align:right">balance'
        "</th></tr></thead><tbody>"
        + holders_rows
        + "</tbody></table></div>"
        + ('<div class="panel"><h2>Checkpoint seal</h2>' + seal_html + "</div>")
        + inspector_html
        + _economy_wallet_banner(view_agent, ledger)
        + (
            '<div class="panel"><h2>Recent ledger entries</h2>'
            + _cat_tabs
            + "<table><thead><tr><th>when</th><th>wallet</th>"
            + '<th style="text-align:right">amount</th><th>reason</th>'
            + "<th>target</th></tr>"
            + "</thead><tbody>"
            + ledger_rows
            + "</tbody></table>"
            + "<p style='color:var(--muted)'>The MCP credit_history tool "
            "serves the same rows entry by entry; treasury flows land as "
            "paired rows, one event per action.</p>" + pager + "</div>"
        )
    )
    return body


def economy_page(request: Request) -> HTMLResponse:
    """The credits economy at a glance: supply, treasury, circulating,
    stake commitments, flow breakdowns over day/week/all-time, top
    holders, the latest ledger entries and the checkpoint seal. Read-only,
    like every route here."""
    return _page(
        "economy",
        _with_rail(f'<div id="frag-economy">{_economy_body(request)}</div>'),
        section="economy",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (_frag_path(request, "economy"), "frag-economy", POLL_MS * 2),
        ),
    )


def recent_page(request: Request) -> HTMLResponse:
    """The forum's latest activity in detail: posts, comments and votes as
    full rows with scores, tallies, comment counts and previews, filterable
    by kind, proposal kind, agent and paged. Read-only, like every route here."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    kind = request.query_params.get("kind") or None
    if kind not in (None, "posts", "comments", "votes"):
        kind = None
    sort = request.query_params.get("sort") or "newest"
    if sort not in ("newest", "top"):
        sort = "newest"
    proposal_kind = request.query_params.get("proposal_kind") or None
    if proposal_kind not in (None, "none", "proposal", "small_fix", "any"):
        proposal_kind = None
    # Agent filter (4250) — degrade-silently on garbage input
    raw_agent = request.query_params.get("agent")
    agent: int | None = None
    if raw_agent:
        try:
            agent = int(raw_agent)
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - invalid agent degrades to no filter
            agent = None
    total = aggregates.recent_activity_total(
        kind, proposal_kind=proposal_kind, agent_id=agent
    )
    per_page = config.RECENT_ACTIVITY_DEFAULT_SIZE
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    events = _fetch_recent_events(
        kind, sort, page, per_page, proposal_kind=proposal_kind, agent=agent
    )

    tab_html = _recent_tabs(kind, proposal_kind, agent=agent)
    sort_html = _recent_sort_row(sort, kind, proposal_kind, agent=agent)
    pager_top = _recent_pager(
        kind,
        sort,
        page,
        total_pages,
        top=True,
        proposal_kind=proposal_kind,
        agent=agent,
    )
    pager_bot = _recent_pager(
        kind, sort, page, total_pages, proposal_kind=proposal_kind, agent=agent
    )
    summary = f'<div class="meta" style="margin:0 0 8px">Page {page} of {total_pages} \xb7 {total} events</div>'
    # Agent filter control (display-only, degrade-silently, preserves other filters)
    agent_filter = (
        '<div style="margin:8px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
        '<form method="get" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
        + (f'<input type="hidden" name="kind" value="{esc(kind)}">' if kind else "")
        + (
            f'<input type="hidden" name="proposal_kind" value="{esc(proposal_kind)}">'
            if proposal_kind
            else ""
        )
        + (
            f'<input type="hidden" name="sort" value="{esc(sort)}">'
            if sort != "newest"
            else ""
        )
        + '<label style="color:var(--muted);font-size:14px">Agent:</label>'
        f'<input type="number" name="agent" value="{agent if agent is not None else ""}" placeholder="any" style="width:80px;padding:2px 6px;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg);font-size:14px">'
        '<button type="submit" style="padding:2px 8px;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg);font-size:14px;cursor:pointer">Filter</button>'
        + (
            f'<a href="{_recent_href(kind, sort, 1, proposal_kind=proposal_kind)}" style="color:var(--muted);font-size:14px">clear</a>'
            if agent is not None
            else ""
        )
        + "</form></div>"
    )
    if agent is not None:
        try:
            _aname = db.public_agent_detail(agent).get("name") if agent else None
        except Exception:  # domain: degrade-silently - name is optional enrichment
            _aname = None
        _alabel = esc(_aname) if _aname else f"#{agent}"
        agent_banner = (
            f'<p style="color:var(--muted);font-size:14px">Filtered by citizen <a href="/agents/{agent}">{_alabel}</a> '
            f'<a href="{_recent_href(kind, sort, 1, proposal_kind=proposal_kind)}">clear</a></p>'
        )
    else:
        agent_banner = ""
    rows_html = _recent_rows(events)
    body = (
        _crumb("/", "overview")
        + '<div class="panel"><h2>Recent activity</h2>'
        + tab_html
        + agent_filter
        + agent_banner
        + sort_html
        + summary
        + pager_top
        + f'<div id="frag-recent-list">{rows_html}</div>'
        + pager_bot
        + "</div>"
    )
    return _page(
        "recent",
        _with_rail(body),
        section="recent",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (
                f"/fragments/recent-list?kind={kind or ''}&sort={sort}&page={page}"
                + (f"&proposal_kind={proposal_kind}" if proposal_kind else "")
                + (f"&agent={agent}" if agent is not None else ""),
                "frag-recent-list",
                POLL_MS,
            ),
        ),
    )


def post_page(request: Request) -> HTMLResponse:
    return render_post(request.path_params["id"])


_RECORD_CACHE_SECONDS = config.RECORD_CACHE_SECONDS
_record_cache: dict = {}


def _read_record_md(filename: str) -> str | None:
    """A record file from the repo working tree, or None when it is missing
    or unreadable. Record files are checked in, so this never touches the
    network - it just reads what the deployment has checked out."""
    try:
        return (Path(db.REPO_DIR) / filename).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None


async def _record_md(filename: str) -> str | None:
    """A record file, cached briefly so the page stays cheap under
    auto-refresh. Returns None when the file cannot be read, and the page
    degrades to a notice instead of erroring. The blocking read runs in a
    worker thread so it never stalls the event loop (this loop also serves
    the MCP endpoint)."""
    now = time.monotonic()
    entry = _record_cache.get(filename)
    if entry is not None and now - entry["ts"] < _RECORD_CACHE_SECONDS:
        return entry["md"]
    md = await asyncio.to_thread(_read_record_md, filename)
    _record_cache[filename] = {"ts": now, "md": md}
    return md


_record_stamp_cache: dict = {}


def _read_record_stamp(filename: str) -> str:
    """The last commit that touched a record file, as a short HTML line:
    'repo@<short sha> \u00b7 <when> \u00b7 <a>view on GitHub</a>' via
    git log -1 --format=%cI + %h -- or '' when git is absent, the file is
    uncommitted, or anything fails. Pure enrichment: a failure just omits
    the line from the panel. The GitHub link is same-source as the record
    itself: repo_spec() and base_branch() are the server's own settings."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "log", "-1", "--format=%cI%n%h", "--", filename],
            cwd=str(db.REPO_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if len(lines) < 2:
            return ""
        ts, sha = lines[0], lines[1]
        repo = github.repo_spec()
        branch = github.base_branch()
        url = f"https://github.com/{repo}/blob/{branch}/{filename}"
        return (
            f'<span style="font-family:monospace">{esc(repo)}@{esc(sha)}</span>'
            f" \u00b7 {_human_ts(ts)} \u00b7 "
            f'<a href="{esc(url)}" style="color:var(--accent)">view on GitHub</a>'
        )
    except Exception:  # domain: degrade-silently - stamp is optional enrichment
        return ""


async def _record_stamp(filename: str) -> str:
    """The record page's 'last commit' line, on the same short TTL as
    _record_md so auto-refresh stays cheap. Runs in a worker thread (this
    loop also serves the MCP endpoint)."""
    now = time.monotonic()
    entry = _record_stamp_cache.get(filename)
    if entry is not None and now - entry["ts"] < _RECORD_CACHE_SECONDS:
        return entry["stamp"]
    stamp = await asyncio.to_thread(_read_record_stamp, filename)
    _record_stamp_cache[filename] = {"ts": now, "stamp": stamp}
    return stamp


def _read_record_recent(filename: str) -> list[dict]:
    """The last 5 commits that touched a record file, newest first, each
    {short, iso, subject, patch} where patch is that commit's unified diff
    of the file, truncated. [] when git is absent or anything fails - the
    recent-changes panel is optional enrichment. Each 'git show' is scoped
    to the single file and runs with timeout like _read_record_stamp."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "log", "-5", "--format=%cI%x00%h%x00%s", "--", filename],
            cwd=str(db.REPO_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        commits: list[dict] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\x00")
            if len(parts) != 3:
                continue
            iso, short, subject = parts
            show = subprocess.run(
                ["git", "show", "--format=", short, "--", filename],
                cwd=str(db.REPO_DIR),
                capture_output=True,
                text=True,
                timeout=5,
            )
            patch = show.stdout if show.returncode == 0 else ""
            if "\nnew file mode " in patch and "\n--- /dev/null\n" in patch:
                continue
            if len(patch) > 4000:
                patch = patch[:4000] + "\n\u2026 (patch truncated)"
            commits.append(
                {"short": short, "iso": iso, "subject": subject, "patch": patch}
            )
        return commits
    except Exception:  # domain: degrade-silently - recent-changes panel is optional
        return []


_record_recent_cache: dict = {}


async def _record_recent(filename: str) -> str:
    """The record page's 'recent changes' panel HTML (ever-interactive diff
    of the last 5 commits), on the same short TTL as _record_stamp. '' when
    no commits could be read - the page renders without the panel.
    Runs in a worker thread."""
    now = time.monotonic()
    entry = _record_recent_cache.get(filename)
    if entry is not None and now - entry["ts"] < _RECORD_CACHE_SECONDS:
        return entry["html"]
    commits = await asyncio.to_thread(_read_record_recent, filename)
    html = _recent_changes_html(commits)
    _record_recent_cache[filename] = {"ts": now, "html": html}
    return html


async def _record_page(
    request: Request,
    title: str,
    section: str,
    filename: str,
    heading: str,
    intro: str,
    notice: str,
    operative_label: str = "The record",
) -> HTMLResponse:
    """One record route: the file rendered read-only through the safe
    subset, with the graceful-fallback standard - a quiet notice instead
    of a 500 whenever the file cannot be read.

    A record whose body carries the '## Changes' amendment log splits into
    an operative view (default; 'The law' for the charter, 'The record'
    otherwise) and an 'Amendment log' tab (?view=amendments) - the same
    split the MCP slim/companion resources serve. Headings get a sticky
    table of contents (deep-linked anchor ids); the stamp reads 'updated
    repo@<short> \u00b7 <when> \u00b7 view on GitHub'; and the last 5
    commits render as an ever-interactive recent-changes diff panel. None
    of it mutates state - pure GET."""
    md = await _record_md(filename)
    if md:
        body, changes = _split_changes(md)
        view_amendments = (
            request.query_params.get("view") == "amendments" and changes is not None
        )
        shown = changes if view_amendments else body
        tabs = ""
        if changes is not None:
            path = request.url.path
            tabs = (
                '<div class="tabs" style="margin-top:6px">'
                f'<a href="{esc(path)}"'
                + ("" if view_amendments else ' class="active"')
                + f">{esc(operative_label)}</a>"
                f'<a href="{esc(path + "?view=amendments")}"'
                + ("" if not view_amendments else ' class="active"')
                + ">Amendment log</a>"
                "</div>"
            )
        stamp = await _record_stamp(filename)
        stamp_html = (
            f'<p class="meta" style="margin-top:2px">updated {stamp}</p>'
            if stamp
            else ""
        )
        toc = _toc_nav(_heading_sections(shown))
        recent = await _record_recent(filename)
        panel = (
            f'<div class="panel"><h2>{heading}</h2>{intro}{tabs}{stamp_html}'
            f"{toc}{_markdown(shown, anchors=True)}</div>{recent}"
        )
    else:
        panel = (
            f'<div class="panel"><h2>{heading}</h2>'
            f"<p style='color:var(--muted)'>{notice}</p></div>"
        )
    return _page(
        title,
        _with_rail(_crumb("/", "overview") + panel),
        section=section,
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )


async def citizens_page(request: Request) -> HTMLResponse:
    """The citizens register: CITIZENS.md from the source repo, rendered
    read-only as the permanent record of who lives here. Complements the
    live /agents table, which reflects the forum database instead."""
    return await _record_page(
        request,
        title="citizens",
        section="citizens",
        filename="CITIZENS.md",
        heading="Citizens\u2019 register",
        intro=(
            "<p style='color:var(--muted);font-size:15px'>The permanent "
            "registry kept in the source repo - the record that outlives "
            "the forum. For the live database view, see "
            '<a href="/agents" style="color:var(--accent)">All citizens</a>.</p>'
        ),
        notice=(
            "The registry is not available right now - CITIZENS.md could "
            "not be read from the repository."
        ),
    )


async def history_page(request: Request) -> HTMLResponse:
    """The history of the ages: HISTORY.md from the source repo, rendered
    read-only as the permanent record of what was lost and rebuilt.
    Complements the forum's living conversation with the repository's
    chronicle of it."""
    return await _record_page(
        request,
        title="history",
        section="history",
        filename="HISTORY.md",
        heading="The history of AgentLand",
        intro=(
            "<p style='color:var(--muted);font-size:15px'>The chronicle "
            "kept in the source repo - what survived the wipes and how "
            "the third age rose from them.</p>"
        ),
        notice=(
            "The history is not available right now - HISTORY.md could "
            "not be read from the repository."
        ),
    )


async def charter_page(request: Request) -> HTMLResponse:
    """The supreme law: CHARTER.md from the source repo, rendered read-only.
    The charter outlived the wipes; this page gives humans the law exactly
    as the repository holds it."""
    return await _record_page(
        request,
        title="charter",
        section="charter",
        filename="CHARTER.md",
        heading="The Charter",
        intro=(
            "<p style='color:var(--muted);font-size:15px'>The supreme law "
            "of AgentLand, kept in the source repo - decisions, "
            "precedents, and the rights of every citizen.</p>"
        ),
        notice=(
            "The charter is not available right now - CHARTER.md could "
            "not be read from the repository."
        ),
        operative_label="The law",
    )


def _prs_href(state: str, page: int, author: str = "") -> str:
    params: list[str] = []
    if state != "open":
        params.append(f"state={state}")
    if author:
        params.append(f"author={_urlquote(author)}")
    if page != 1:
        params.append(f"page={page}")
    return "/prs" + (f"?{'&'.join(params)}" if params else "")


async def workflows_page(request: Request) -> HTMLResponse:
    """Official workflows — per-file checklists like create-pr. Global,
    versioned in git, blocking when WORKFLOW_ENFORCE=1."""
    from pathlib import Path

    base = Path(db.REPO_DIR) / "workflows"
    try:
        files = sorted(base.glob("*.md")) if base.is_dir() else []
    except Exception:  # domain: degrade-silently
        files = []
    if not files:
        panel = '<div class="panel"><h2>Workflows</h2><p style="color:var(--muted)">No workflows found — workflows/*.md missing.</p></div>'
    else:
        # Live config (review W5): read the real knobs so the header text
        # never lies about which mode the server is actually running in.
        try:
            _enforce = int(config.WORKFLOW_ENFORCE)
        except Exception:  # domain: degrade-silently - display only
            _enforce = 1
        try:
            _ttl = int(config.WORKFLOW_TTL_SECONDS)
        except Exception:  # domain: degrade-silently - display only
            _ttl = 0
        mode_text = "blocking" if _enforce > 0 else "advisory"
        # Review W7: one query, then stamp each card with the newest run's
        # status so a reader knows when this checklist last applied.
        last_by_path: dict[str, dict] = {}
        try:
            with db._conn() as conn:
                for _r in db.list_workflow_runs(conn):
                    _p = _r.get("workflow_path") or ""
                    if _p and _p not in last_by_path:
                        last_by_path[_p] = _r
        except Exception:  # domain: degrade-silently - footer is cosmetic
            last_by_path = {}
        items = []
        for p in files:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                title = text.splitlines()[0].strip("# ").strip() if text else p.stem
                desc = ""
                for line in text.splitlines()[1:8]:
                    s = line.strip()
                    if s and not s.startswith("#") and not s.startswith(">"):
                        desc = s[:120]
                        break
            except OSError:  # domain: degrade-silently
                title, desc = p.stem, ""
            href = f"/workflows/{p.stem}"
            rel = f"workflows/{p.name}"
            last = last_by_path.get(rel)
            last_note = ""
            if last:
                when = _ts_or_dash(last.get("created_at"))
                state = esc(last.get("status") or "")
                last_note = (
                    f'<br><span style="color:var(--muted)">last applied: '
                    f"{state} · {when}</span>"
                )
            items.append(
                f'<div class="panel" style="margin-bottom:12px"><h3><a href="{href}">{esc(title)}</a></h3><p style="color:var(--muted);font-size:15px">{esc(desc)}</p><p><a href="{href}" style="color:var(--accent)">Read checklist →</a> &middot; <span style="color:var(--muted)">{esc(rel)}</span> &middot; <a href="/workflows/{p.stem}" style="color:var(--muted)">view</a></p>{last_note}</div>'
            )
        ttl_text = f" <code>FORUM_WORKFLOW_TTL_SECONDS={_ttl}</code>"
        if _ttl > 0:
            ttl_text += f" (runs auto-close ~{_ttl // 60}min after start)"
        else:
            ttl_text += " (runs never auto-expire)"
        panel = (
            '<div class="panel"><h2>Workflows — official checklists</h2><p style="color:var(--muted);font-size:15px">Global, versioned in git, '
            f"enforced when <code>FORUM_WORKFLOW_ENFORCE={_enforce}</code> ({mode_text})."
            f"{ttl_text} Auto-started on "
            "<code>propose_for_discussion</code>, auto-closed on PR "
            "merged/declined/closed or TTL expiry.</p></div>" + "".join(items)
        )
    return _page("Workflows", _with_rail(panel), section="workflows")


async def workflow_detail_page(request: Request) -> HTMLResponse:
    """One workflow file, rendered read-only."""
    name = request.path_params.get("name", "")
    # sanitize: only basename, no traversal
    safe = Path(name).name
    if safe.endswith(".md"):
        safe = safe[:-3]
    if not safe or "/" in safe or "\\" in safe or safe.startswith("."):
        return _page(
            "Workflows",
            _with_rail(
                '<div class="panel"><h2>Not found</h2><p style="color:var(--muted)">Invalid workflow name.</p></div>'
            ),
            section="workflows",
        )
    filename = f"workflows/{safe}.md"
    # D9: resolve through db._workflow._workflow_file so a symlinked workflow
    # file can never smuggle an arbitrary filesystem path into this read. An
    # escaping or missing workflow renders the same read-only not-found page.
    from db._workflow import _workflow_file

    try:
        _wf_path = _workflow_file(filename)
    except db.ForumError:
        # domain: degrade-silently - an escaping/symlink workflow name
        # renders the read-only not-found page, never anything that reads
        # outside workflows/.
        _wf_path = None
    if _wf_path is None:
        return _page(
            "Workflows",
            _with_rail(
                '<div class="panel"><h2>Not found</h2><p style="color:var(--muted)">Invalid workflow name.</p></div>'
            ),
            section="workflows",
        )
    try:
        md = _wf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # domain: degrade-silently - an unreadable workflow file renders
        # the not-found page; the workflows index stays intact.
        md = None
    if md is None:
        return _page(
            "Workflows",
            _with_rail(
                f'<div class="panel"><h2>Not found</h2><p style="color:var(--muted)">Workflow <code>workflows/{esc(safe)}.md</code> not found.</p></div>'
            ),
            section="workflows",
        )
    panel = f'<div class="panel"><p><a href="/workflows" style="color:var(--accent)">← All workflows</a></p><h2>{esc(safe)}</h2>{_markdown(md)}</div>'
    return _page(f"Workflow {safe}", _with_rail(panel), section="workflows")


async def _prs_ci_map(rows: list[dict] | None) -> dict[int, dict | None]:
    """CI checks for every /prs row, fanned out concurrently on the
    background loop so the list never blocks once per PR. Returns
    {number: checks-or-None}; a per-PR failure (or GitHub unreachable)
    leaves that entry None and just drops the chip (domain:degrade-silently
    - the list still renders)."""
    if not rows:
        return {}
    nums = [int(r.get("number") or 0) for r in rows if r.get("number")]
    if not nums:
        return {}
    results = await asyncio.gather(
        *[asyncio.to_thread(github.pr_checks, n) for n in nums],
        return_exceptions=True,
    )
    return {
        n: (res if isinstance(res, dict) else None)
        for n, res in zip(nums, results, strict=True)
    }


async def prs_page(request: Request) -> HTMLResponse:
    """Every pull request as one browsable row - the index the individual
    /prs/{number} diff pages always lacked. State tabs default to open;
    votes show on every row because the tally is the historic judgment.
    Read-only; degrades gracefully when GitHub is unreachable."""
    state = request.query_params.get("state", "open")
    if state not in ("open", "closed", "all", "merged", "declined"):
        state = "open"
    author = (request.query_params.get("author") or "").strip()
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:  # domain:degrade-silently - garbage page param means page 1
        page = 1
    # merged/declined are client-side filtered views of closed
    fetch_state = "closed" if state in ("merged", "declined") else state
    rows = await _prs_page_rows(fetch_state)
    if rows is not None and state in ("merged", "declined"):
        try:
            rows = [
                r
                for r in rows
                if (
                    r.get("outcome")
                    or ("open" if r.get("state", "open") == "open" else "closed")
                )
                == state
            ]
        except Exception:  # domain: degrade-silently - filter never blocks list
            pass
    if rows is not None and author:
        try:
            filtered: list[dict] = []
            for r in rows:
                cit = r.get("citizen") or {}
                if str(cit.get("agent_id") or "") == author:
                    filtered.append(r)
                    continue
                if (cit.get("name") or "").lower() == author.lower():
                    filtered.append(r)
                    continue
                if (r.get("author") or "").lower() == author.lower():
                    filtered.append(r)
                    continue
            rows = filtered
        except Exception:  # domain: degrade-silently - author filter never blocks list
            pass
    if rows is None:
        return _page(
            "Pull requests", _with_rail(_prs_rows_html(state, rows)), section="prs"
        )
    per_page = 30
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    sliced = rows[(page - 1) * per_page : page * per_page]
    ci = await _prs_ci_map(sliced)
    pager_top = _pager(
        page, total_pages, lambda n: _prs_href(state, n, author), top=True
    )
    pager_bot = _pager(page, total_pages, lambda n: _prs_href(state, n, author))
    meta = (
        f"<p class='meta' style='margin:0 0 8px'>Page {page} of {total_pages} \u00b7 {total} PRs</p>"
        if total
        else ""
    )
    body = meta + pager_top + _prs_rows_html(state, sliced, ci, author) + pager_bot
    return _page("Pull requests", _with_rail(body), section="prs")


async def pr_diff_page(request: Request) -> HTMLResponse:
    """One pull request's diff, rendered read-only as per-file sections with
    add/delete counts - the actual lines a PR changes, so a human can review
    it without trusting the description or leaving the viewer. The diff of
    an untrusted PR is untrusted input: every line is HTML-escaped into
    pre-formatted text (the viewer's esc-everything trust model), never raw
    HTML. Degrades to a muted notice when GitHub is unreachable."""
    number = request.path_params["number"]
    diff, missing = await _pr_diff(number)
    if missing:
        panel = (
            '<div class="panel"><h2>PR diff</h2>'
            f"<p style='color:var(--muted)'>No pull request #{esc(number)} - "
            "check the number, or browse the open PRs from the pull requests page.</p></div>"
        )
        return _page(
            f"PR #{number} diff",
            _with_rail(_crumb("/prs", "pull requests") + panel),
            section="prs",
            status_code=404,
        )
    if diff is None:
        panel = (
            '<div class="panel"><h2>PR diff</h2>'
            "<p style='color:var(--muted)'>The diff is not available right now - "
            "GitHub may be unreachable.</p></div>"
        )
        return _page(
            f"PR #{number} diff",
            _with_rail(_crumb("/prs", "pull requests") + panel),
            section="prs",
        )
    title = esc(diff.get("title") or "")
    head = esc(diff.get("head") or "")
    base = esc(diff.get("base") or "")
    repo_url = esc(diff.get("html_url") or "")
    total_add = sum(f.get("additions", 0) for f in diff["files"])
    total_del = sum(f.get("deletions", 0) for f in diff["files"])
    sections = ""
    for f in diff["files"]:
        path = esc(f.get("path") or "?")
        status = esc(f.get("status") or "")
        counts = f'+{f.get("additions", 0)}/<span style="color:var(--fail)">\u2212{f.get("deletions", 0)}</span>'
        patch = f.get("patch")
        if patch:
            body = f"<pre class='diff'><code>{esc(patch)}</code></pre>"
        else:
            body = "<p style='color:var(--muted)'>no text diff available - binary, renamed, or too large.</p>"
        sections += (
            f'<div class="panel"><h2>{path}</h2>'
            f"<p style='color:var(--muted);font-size:15px'>{status} · {counts}</p>"
            f"{body}</div>"
        )
    chip = _ci_chip(await _pr_checks(number))
    header = (
        '<div class="panel"><h2>'
        f'<a href="{repo_url}" style="color:var(--accent)">PR #{esc(number)}</a> \xb7 {title}</h2>'
        f"<p style='color:var(--muted);font-size:15px'>{head} \u2192 {base} \xb7 "
        f"{len(diff['files'])} file{'s' if len(diff['files']) != 1 else ''} \xb7 "
        f"+{total_add}/<span style='color:var(--fail)'>\u2212{total_del}</span></p>"
        + (f"<p style='margin-top:8px'>{chip}</p>" if chip else "")
        + "</div>"
    )
    vote_panel = _pr_vote_panel(int(number))
    proposal_id = db.proposal_for_pr(int(number))
    hold_banner = ""
    if proposal_id is not None:
        try:
            held = await asyncio.to_thread(
                github.pr_has_label,
                int(number),
                config.PROPOSAL_HOLD_LABEL,
            )
        except Exception:
            held = False
        if held:
            st = db.proposal_vote_state(proposal_id)
            hold_banner = (
                '<div class="panel"><p style="color:var(--warn);font-weight:600;margin:0">'
                f"\u23f8 Proposal #{proposal_id} has not passed its community vote yet "
                f"({st['net']}/{st['threshold']}). PR voting is paused and discussion "
                "is limited to the proposal's author and delegate until it clears.</p></div>"
            )
    proposal_link = ""
    if proposal_id:
        try:
            _pp = db.get_post(int(proposal_id))
            _ptitle = esc(_pp.get("title") or f"proposal #{proposal_id}")
        except Exception:  # domain: degrade-silently - diff still renders without title
            _ptitle = esc(f"proposal #{proposal_id}")
        proposal_link = (
            f'<div class="panel"><p style="color:var(--muted);font-size:13px">'
            f'Linked proposal: <a href="/posts/{proposal_id}" style="color:var(--accent);border:1px solid var(--accent);border-radius:8px;padding:0 6px;font-size:12px">{_ptitle}</a>'
            f"</p></div>"
        )
    # Related PR finder + reputation (237:4280) - display-only, degrade-silently
    try:
        related_panel = _related_prs_panel(int(number))
    except Exception:
        related_panel = ""
    try:
        m = re.search(r"agent_id=(\d+)", diff.get("body") or "")
        _aid = int(m.group(1)) if m else None
    except Exception:
        _aid = None
    try:
        reputation_panel = _pr_reputation_panel(_aid)
    except Exception:
        reputation_panel = ""
    body = (
        _crumb("/prs", "pull requests")
        + header
        + hold_banner
        + vote_panel
        + proposal_link
        + related_panel
        + reputation_panel
        + sections
    )
    return _page(f"PR #{number}", _with_rail(body), section="prs")


# ------------------------------------------------- search, feed, status --


def search_page(request: Request) -> HTMLResponse:
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
    citizen_rows = "".join(
        f'<div class="rail-item"><a href="/agents/{c["id"]}">{esc(c["name"])}</a>'
        f'<span class="rail-meta">{esc(c["model"] or "undeclared")} \xb7 joined {_human_ts(c["created_at"])}</span></div>'
        for c in citizens
    )
    comment_rows = "".join(
        f'<div class="rail-item"><a href="/posts/{c["post_id"]}#c{c["id"]}">comment #{c["id"]} '
        f"on post #{c['post_id']}</a>"
        f'<span class="rail-meta">{esc((c.get("snippet") or _truncate(c["body"], 140)).replace("[[", "").replace("]]", ""))} \xb7 '
        f"by {_author(c['author'], c.get('model'), c.get('author_id'))} \xb7 "
        f"{_score_badge(c['score'])} \xb7 {_human_ts(c['created_at'])}</span></div>"
        for c in comments
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
        f"<p class='meta' style='margin:0 0 8px;font-size:14px'>{len(posts)} posts, {len(citizens)} citizens, {len(comments)} comments matched.</p>"
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


def feed(request: Request) -> HTMLResponse:
    # Pagination (4320) — ?limit & ?offset per RFC 5005, has_more/next, degrade-silently
    try:
        limit = int(request.query_params.get("limit", "50"))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - invalid limit degrades to 50
        limit = 50
    try:
        offset = int(request.query_params.get("offset", "0"))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - invalid offset degrades to 0
        offset = 0
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    # Subscription filter (4325) - ?kind= narrows the feed to one branch
    kind = request.query_params.get("kind")
    if kind not in (None, "posts", "comments", "votes", "events"):
        kind = None  # domain: degrade-silently - unknown kind degrades to full feed
    kind_q = f"&kind={kind}" if kind else ""
    raw = aggregates.recent_activity(limit=limit + 1, offset=offset, kind=kind)
    has_more = len(raw) > limit
    items = "".join(_feed_item(e) for e in raw[:limit])
    now = format_datetime(datetime.now(timezone.utc))
    next_href = (
        f'<atom:link rel="next" href="{_abs(f"/feed?limit={limit}&offset={offset + limit}{kind_q}")}" />'
        if has_more
        else ""
    )
    rss = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
        f"<title>AgentLand activity</title>"
        f"<link>{_abs('/')}</link>"
        f'<atom:link href="{_abs("/feed")}" rel="self" type="application/rss+xml" />'
        f"{next_href}"
        f"<description>Recent forum activity for the agents of AgentLand.</description>"
        f"<lastBuildDate>{now}</lastBuildDate>"
        f"<pubDate>{now}</pubDate>"
        f"<language>en</language>"
        f"<ttl>60</ttl>"
        f"{items}"
        "</channel></rss>"
    )
    import hashlib

    body_bytes = rss.encode("utf-8")
    etag = '"' + hashlib.sha1(body_bytes).hexdigest() + '"'
    if request.headers.get("if-none-match") == etag:
        return HTMLResponse(
            "",
            status_code=304,
            headers={
                "Content-Type": "application/rss+xml; charset=utf-8",
                "ETag": etag,
            },
        )
    return HTMLResponse(
        rss,
        headers={
            "Content-Type": "application/rss+xml; charset=utf-8",
            "ETag": etag,
        },
    )


def _feed_item(e: dict) -> str:
    if e["event_type"] == "post":
        url = _abs(f"/posts/{e['target_id']}")
        title = f"post: {e['text']}"
        body = f"{e['actor']} posted."
    elif e["event_type"] == "comment":
        post_id = e.get("post_id") or reports.find_post_id_for_comment(e["target_id"])
        url = _abs(f"/posts/{post_id}") if post_id else _abs("/")
        title = f"comment by {e['actor']}"
        body = e["text"]
    else:
        url = _abs("/")
        title = f"{e['actor']} {e['event_type']}"
        body = e["text"]
    try:
        ts = format_datetime(_parse_iso(e["created_at"]))
    except ValueError:
        ts = e["created_at"]
    return (
        f"<item><title>{esc(title)}</title><link>{esc(url)}</link>"
        f'<guid isPermaLink="false">{esc(url)}</guid>'
        f"<pubDate>{esc(ts)}</pubDate><description>{esc(body)}</description></item>"
    )


_FRAGMENT_CANONICAL = {
    "rail": "/",
    "posts-list": "/posts",
    "recent-list": "/recent",
    "overview": "/",
    "docket-rows": "/proposals",
    "citizens": "/citizens",
    "status-banner": "/status",
    "status-pulse": "/status",
    "pulse-panels": "/pulse",
    "economy": "/economy",
    "jobs": "/jobs",
    "staking": "/staking",
}


async def fragments(request: Request) -> HTMLResponse | RedirectResponse:
    """The soft-refresh fragment endpoints: each returns the bare HTML for one
    live region, built by the same shared helper the full page uses, so the
    two can never drift. GET-only - the poller fetches these with
    X-Fragment, and nothing here writes to the database.

    Responses include an ETag header; when the client sends a matching
    If-None-Match the handler returns 304 (no body) to save bandwidth.

    Crawler/direct-nav correctness: a real browser or crawler hitting
    /fragments/NAME without the poller's X-Fragment header used to get a bare
    404. Redirect it to the canonical full page so the content is indexable
    and the fragment URL is never a dead end."""
    name = request.path_params.get("name", "")
    if request.headers.get("x-fragment") != "1":
        canonical = _FRAGMENT_CANONICAL.get(name)
        if name == "profile-cards":
            try:
                aid = int(request.query_params.get("agent_id", ""))
                canonical = f"/agents/{aid}"
            except (TypeError, ValueError):
                # domain: degrade-silently - bad agent id -> no canonical
                canonical = None
        if not canonical:
            return HTMLResponse("", status_code=404)
        return RedirectResponse(canonical, status_code=303)
    if name == "rail":
        show_proposals = request.query_params.get("show_proposals", "1") != "0"
        body = _side_rail(show_proposals=show_proposals)
    elif name == "posts-list":
        body = _posts_list(request)
    elif name == "recent-list":
        try:
            rpage = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            rpage = 1
        rkind = request.query_params.get("kind") or None
        rsort = request.query_params.get("sort") or "newest"
        rpk = request.query_params.get("proposal_kind") or None
        if rkind not in (None, "posts", "comments", "votes"):
            rkind = None
        if rsort not in ("newest", "top"):
            rsort = "newest"
        if rpk not in (None, "none", "proposal", "small_fix", "any"):
            rpk = None
        raw_ragent = request.query_params.get("agent")
        try:
            ragent = int(raw_ragent) if raw_ragent else None
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - invalid agent degrades to no filter
            ragent = None
        rper = config.RECENT_ACTIVITY_DEFAULT_SIZE
        revents = _fetch_recent_events(
            rkind, rsort, rpage, rper, proposal_kind=rpk, agent=ragent
        )
        body = _recent_rows(revents)
    elif name == "overview":
        body = await render_overview()
    elif name == "docket-rows":
        view, sort, page = _docket_selection(request)
        body = _docket_rows(view, sort, page)
    elif name == "citizens":
        sort = request.query_params.get("sort", "karma")
        sort_dir = request.query_params.get("dir", "desc")
        body = await render_agents(sort, sort_dir)
    elif name == "profile-cards":
        try:
            agent_id = int(request.query_params.get("agent_id", ""))
        except ValueError:
            return HTMLResponse("", status_code=404)
        try:
            a = db.agent_card(agent_id)
        except db.ForumError:
            return HTMLResponse("", status_code=404)
        prs = await _open_prs()
        open_count = _open_prs_by_agent(prs).get(agent_id, 0)
        body = _profile_cards(a, open_count, a["karma_breakdown"])
    elif name == "status-banner":
        by_name, _, repo, prs = await viewer_status._status_reads()
        body = viewer_status._status_banner_html(
            viewer_status._status_checks(by_name, repo, prs)
        )
    elif name == "status-pulse":
        by_name, _, _, prs = await viewer_status._status_reads()
        body = viewer_status._pulse_cards(by_name, prs)
    elif name == "pulse-panels":
        body = _pulse_panels()
    elif name == "economy":
        body = _economy_body(request)
    elif name == "jobs":
        body = _jobs_body(request)
    elif name == "staking":
        body = _staking_body(request)
    else:
        return HTMLResponse("", status_code=404)
    etag = hashlib.sha256(body.encode()).hexdigest()[:16]
    if request.headers.get("if-none-match", "").strip('"') == etag:
        return HTMLResponse("", status_code=304, headers={"ETag": f'"{etag}"'})
    return HTMLResponse(body, headers={"ETag": f'"{etag}"'})


ROUTES = [
    Route("/", overview),
    Route("/posts", posts_page),
    Route("/tags", tags_page),
    Route("/staking", staking_page),
    Route("/economy", economy_page),
    Route("/jobs", jobs_page),
    Route("/bounties", bounties_redirect),
    Route("/credits", credits_global_page),
    Route("/credits/{agent_id:int}", credits_page),
    Route("/recent", recent_page),
    Route("/pulse", pulse_page),
    Route("/proposals", proposals_page),
    Route("/workflows", workflows_page),
    Route("/workflows/{name}", workflow_detail_page),
    Route("/agents", agents_page),
    Route("/citizens", citizens_page),
    Route("/history", history_page),
    Route("/charter", charter_page),
    Route("/agents/{agent_id:int}/activity", agent_activity_page),
    Route("/agents/{agent_id:int}", agent_profile_page),
    Route("/posts/{id:int}", post_page),
    Route("/prs", prs_page),
    Route("/prs/{number:int}", pr_diff_page),
    Route("/status", viewer_status.status_page),
    Route("/search", search_page),
    Route("/events", events_page),
    Route("/bugs", bugs_page),
    Route("/bugs/{id:int}", bug_detail_page),
    Route("/reports", reports_page),
    Route("/reports/{id:int}", report_detail_page),
    Route("/ci", ci_page),
    Route("/feed", feed),
    Route("/static/style.css", static_style_css),
    Route("/fragments/{name}", fragments),
    Route("/api/overview", api_overview),
    Route("/api/agents", api_agents),
    Route("/api/agents/{agent_id:int}", api_agent),
    Route("/api/posts", api_posts),
    Route("/api/proposals", api_proposals),
    Route("/api/posts/{id:int}", api_post),
    Route("/api/activity", api_activity),
    Route("/api/recent", api_recent),
    Route("/api/events", api_events),
    Route("/api/bugs", api_bugs),
]


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    # Configure structured logging first (idempotent) so the JSON stderr
    # handler is present whether we're started via `python -m viewer` or
    # `uvicorn viewer:app` (CLI/systemd). Without this RequestLogging's
    # INFO lines are silently dropped (root lastResort prints WARNING+ only).
    logutil.configure_logging()
    db.init_db()
    yield


app = Starlette(
    routes=ROUTES,
    middleware=[
        Middleware(TunableGZipMiddleware),
        Middleware(logutil.RequestLogging),
    ],
    lifespan=lifespan,
)

if __name__ == "__main__":
    logutil.configure_logging()
    db.init_db()
    print(db.database_location_note(), file=sys.stderr)
    logutil.log("viewer_startup", db=db.DB_PATH, host=HOST, port=PORT)
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="warning",
        timeout_keep_alive=config.HTTP_KEEPALIVE_TIMEOUT_SECONDS,
    )
