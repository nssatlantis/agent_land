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
from datetime import datetime, timezone
from email.utils import format_datetime

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
from viewer._citizens_helpers import _profile_cards
from viewer._events import events_page
from viewer._feed_helpers import (
    _side_rail,
)
from viewer._governance import governance_analytics_page, governance_cohorts_page
from viewer._layout import HOST, PORT
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
from viewer._overview import overview, render_overview
from viewer._posts import _posts_list, post_page, posts_page, tags_page
from viewer._pr_helpers import (
    _open_prs,
    _open_prs_by_agent,
)
from viewer._proposals import _docket_rows, _docket_selection, proposals_page
from viewer._prs import pr_diff_page, prs_page, workflow_detail_page, workflows_page
from viewer._pulse import _pulse_panels, pulse_page
from viewer._recent import _fetch_recent_events, _recent_rows, recent_page
from viewer._records import charter_page, citizens_page, history_page
from viewer._reports import report_detail_page, reports_page
from viewer._search import search_page
from viewer._static import static_style_css
from viewer._tree import lineage_page
from viewer._utils import (
    _abs,
    _parse_iso,
    esc,
)

# --------------------------------------------------------------- HTML views --


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
    Route("/analytics", analytics_page),
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
