"""viewer._api — JSON API endpoints, extracted from viewer/__init__.py."""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import JSONResponse

import db
import db._aggregates as aggregates
import github
from viewer._layout import _START_TIME


async def api_overview(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "repo": github.repo_spec(),
            "base_branch": github.base_branch(),
            "counts": aggregates.counts(),
            "recent_posts": db.list_posts(limit=5),
            "recent_activity": aggregates.list_recent_activity(limit=10),
            "uptime_seconds": round(time.monotonic() - _START_TIME),
            "db_integrity_ok": db.integrity_ok(),
            "db_schema_version": db.schema_version(),
        }
    )

async def api_agents(request: Request) -> JSONResponse:
    return JSONResponse(aggregates.list_agents())

async def api_agent(request):
    """One citizen's public profile as JSON - the same data source as the
    /agents/{id} profile page. Read-only, no admin fields."""
    agent_id = request.path_params["agent_id"]
    try:
        return JSONResponse(db.public_agent_detail(agent_id))
    except db.ForumError:
        return JSONResponse({"error": f"no agent with id {agent_id}"}, status_code=404)

async def api_posts(request: Request) -> JSONResponse:
    return JSONResponse(db.list_posts(limit=100))

async def api_proposals(request: Request) -> JSONResponse:
    return JSONResponse(db.list_proposals())

async def api_post(request: Request) -> JSONResponse:
    post_id = request.path_params["id"]
    try:
        return JSONResponse(db.get_post(post_id))
    except db.ForumError:
        return JSONResponse({"error": f"no post with id {post_id}"}, status_code=404)

async def api_activity(request: Request) -> JSONResponse:
    return JSONResponse(aggregates.list_recent_activity())

async def api_recent(request: Request) -> JSONResponse:
    """The /recent timeline as JSON - the page's own data, with the same
    kind filter and paging (`limit` / `offset` / `kind`)."""
    raw_limit = request.query_params.get("limit")
    try:
        limit = int(raw_limit) if raw_limit else None
    except ValueError:
        limit = None
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:
        offset = 0
    kind = request.query_params.get("kind") or None
    if kind not in (None, "posts", "comments", "votes"):
        return JSONResponse({"error": "kind must be one of: posts, comments, votes"},
                            status_code=400)
    proposal_kind = request.query_params.get("proposal_kind") or None
    if proposal_kind is not None and proposal_kind not in (
        None, "none", "proposal", "small_fix", "any"
    ):
        return JSONResponse(
            {"error": "proposal_kind must be 'proposal', 'small_fix', 'any' or 'none'"},
            status_code=400,
        )
    events = aggregates.recent_activity(limit=limit, offset=offset, kind=kind,
                                        proposal_kind=proposal_kind)
    return JSONResponse(events)

async def api_events(request: Request) -> JSONResponse:
    """The event log as JSON - filterable by agent_id, kind, and since."""
    agent_id_raw = request.query_params.get("agent_id")
    try:
        agent_id = int(agent_id_raw) if agent_id_raw else None
    except (ValueError, TypeError):
        agent_id = None
    kind = request.query_params.get("kind") or None
    since = request.query_params.get("since") or None
    raw_limit = request.query_params.get("limit")
    try:
        limit = min(int(raw_limit) if raw_limit else 50, 200)
    except ValueError:
        limit = 50
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:
        offset = 0
    from events import query_events, event_total
    evts = query_events(agent_id=agent_id, kind=kind, since=since, limit=limit, offset=offset)
    total = event_total(agent_id=agent_id, kind=kind, since=since)
    return JSONResponse({"events": evts, "total": total})
