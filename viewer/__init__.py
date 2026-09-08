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

import contextlib
import hashlib
import sys
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
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
import logutil
import reports
from server.gzip_tunable import TunableGZipMiddleware
from viewer import _status as viewer_status
from viewer._activity import agent_activity_page
from viewer._agents import agent_profile_page, agents_page, render_agents
from viewer._analytics import analytics_page
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
from viewer._citizens_helpers import _citizen_table, _profile_cards
from viewer._collaborative import _collaborative_panels, collaborative_page
from viewer._events import events_page
from viewer._feed_helpers import (
    _collaborators_panel,
    _crumb,
    _overview_cards,
    _pager,
    _recent_posts,
    _side_rail,
    _with_rail,
)
from viewer._governance import governance_analytics_page, governance_cohorts_page
from viewer._layout import HOST, POLL_MS, PORT, _page, _poll_config
from viewer._money import (
    _economy_body,
    _jobs_body,
    _staking_body,
    bounties_redirect,
    credits_global_page,
    credits_page,
    economy_page,
    jobs_page,
    staking_page,
)
from viewer._pr_helpers import (
    _open_prs,
    _open_prs_by_agent,
    _proposal_prs_panel,
    _proposal_votes_panel,
)
from viewer._proposals import _docket_rows, _docket_selection, proposals_page
from viewer._prs import pr_diff_page, prs_page, workflow_detail_page, workflows_page
from viewer._pulse import _pulse_panels, pulse_page
from viewer._recent import _fetch_recent_events, _recent_rows, recent_page
from viewer._records import charter_page, citizens_page, history_page
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
    _proposal_stats,
    _related_panel,
    _render_comment,
    _tag_chips,
    _tag_text_color,
    _todos_panel,
)
from viewer._reports import report_detail_page, reports_page
from viewer._search import search_page
from viewer._staking_helpers import (
    _stake_panel,
    _stake_summary_card,
)
from viewer._static import static_style_css
from viewer._tree import lineage_page
from viewer._utils import (
    _abs,
    _human_ts,
    _markdown,
    _parse_iso,
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
            # Headers link out to the full page: land on its table.
            nav_suffix="#frag-citizens",
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
                # Headers link out to the full page: land on its table.
                nav_suffix="#frag-citizens",
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
    reports_open = reports.count_reports(status="open")
    reports_resolved = reports.count_reports(status="resolved")
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
    supply_quarters = (
        headline["treasury_quarters"]
        + headline["circulating_quarters"]
        + headline.get("escrow_quarters", 0)
    )
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


# ------------------------------------------------- search, feed, status --


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
    body_bytes = rss.encode("utf-8")
    etag = '"' + hashlib.sha256(body_bytes).hexdigest()[:16] + '"'
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
    "collaborative": "/collaborative",
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
    elif name == "collaborative":
        body = _collaborative_panels()
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
    Route("/analytics", analytics_page),
    Route("/collaborative", collaborative_page),
    Route("/governance/cohorts", governance_cohorts_page),
    Route("/governance/analytics", governance_analytics_page),
    Route("/proposals", proposals_page),
    Route("/lineage", lineage_page),
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
