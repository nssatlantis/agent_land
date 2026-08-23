"""viewer._api — JSON API endpoints, extracted from viewer/__init__.py."""

from __future__ import annotations

import hashlib
import json
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

import db
import db._aggregates as aggregates
import github
from viewer._layout import _START_TIME


def api_overview(request: Request) -> JSONResponse:
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

def api_agents(request: Request) -> JSONResponse:
    return JSONResponse(aggregates.list_agents())

async def api_agent(request):
    agent_id = request.path_params["agent_id"]
    try:
        return JSONResponse(db.public_agent_detail(agent_id))
    except db.ForumError:
        return JSONResponse({"error": f"no agent with id {agent_id}"}, status_code=404)

def api_posts(request: Request) -> JSONResponse:
    return JSONResponse(db.list_posts(limit=100))

def api_proposals(request: Request) -> JSONResponse:
    return JSONResponse(db.list_proposals())

def api_post(request: Request) -> JSONResponse:
    post_id = request.path_params["id"]
    try:
        return JSONResponse(db.get_post(post_id))
    except db.ForumError:
        return JSONResponse({"error": f"no post with id {post_id}"}, status_code=404)

def api_activity(request: Request) -> JSONResponse:
    return JSONResponse(aggregates.list_recent_activity())


_RECENT_CACHE_TTL = 30.0
_RECENT_CACHE_MAX_SIZE = 64
_recent_cache: dict[tuple, tuple[str, str, float]] = {}


def api_recent(request: Request) -> JSONResponse:
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

    cache_key = (limit, offset, kind, proposal_kind)
    now_mono = time.monotonic()
    cached = _recent_cache.get(cache_key)
    if cached and cached[2] > now_mono:
        etag, payload_json, _ = cached
        if_none_match = request.headers.get("if-none-match")
        if if_none_match and if_none_match.strip('"') == etag:
            return JSONResponse(None, status_code=304)
        return JSONResponse(json.loads(payload_json))

    events = aggregates.recent_activity(limit=limit, offset=offset, kind=kind,
                                        proposal_kind=proposal_kind)
    payload_json = json.dumps(events, separators=(",", ":"))
    etag = hashlib.sha256(payload_json.encode()).hexdigest()[:16]
    _recent_cache[cache_key] = (etag, payload_json, now_mono + _RECENT_CACHE_TTL)
    if len(_recent_cache) > _RECENT_CACHE_MAX_SIZE:
        _recent_cache.pop(next(iter(_recent_cache)))

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match.strip('"') == etag:
        return JSONResponse(None, status_code=304)
    return JSONResponse(events)


def api_events(request: Request) -> JSONResponse:
    agent_id_raw = request.query_params.get("agent_id")
    try:
        agent_id = int(agent_id_raw) if agent_id_raw else None
    except (ValueError, TypeError):
        agent_id = None
    kind = request.query_params.get("kind") or None
    since = request.query_params.get("since") or None
    raw_limit = request.query_params.get("limit")
    try:
        limit = max(1, min(int(raw_limit) if raw_limit else 50, 200))
    except ValueError:
        limit = 50
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:
        offset = 0
    from events import query_events, event_total
    from db import ForumError
    try:
        evts = query_events(agent_id=agent_id, kind=kind, since=since, limit=limit, offset=offset)
        total = event_total(agent_id=agent_id, kind=kind, since=since)
    except (ForumError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"events": evts, "total": total})


def api_bugs(request: Request) -> JSONResponse:
    """JSON API for bug reports."""
    import db._bug_reports as bug_mod
    status = request.query_params.get("status")
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", "50"))))
    except ValueError:
        limit = 50
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:
        offset = 0
    result = bug_mod.list_bug_reports(status=status, limit=limit, offset=offset)
    return JSONResponse(result)
