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
from viewer._utils import TTLCache


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
    except (  # domain: degrade-silently - missing agent returns JSON 404, not 500
        db.ForumError
    ):
        return JSONResponse({"error": f"no agent with id {agent_id}"}, status_code=404)


def api_posts(request: Request) -> JSONResponse:
    raw_limit = request.query_params.get("limit")
    try:
        limit = max(1, min(int(raw_limit) if raw_limit else 100, 200))
    except ValueError:  # domain: degrade-silently - garbage limit param means 100
        limit = 100
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:  # domain: degrade-silently - garbage offset param means 0
        offset = 0
    since = request.query_params.get("since") or None
    proposal_kind = request.query_params.get("proposal_kind") or None
    if proposal_kind is not None and proposal_kind not in (
        "none",
        "proposal",
        "small_fix",
        "any",
    ):
        return JSONResponse(
            {"error": "proposal_kind must be 'proposal', 'small_fix', 'any' or 'none'"},
            status_code=400,
        )
    tag = request.query_params.get("tag") or None
    try:
        return JSONResponse(
            db.list_posts(
                limit=limit,
                offset=offset,
                since=since,
                proposal_kind=proposal_kind,
                tag=tag,
            )
        )
    except (  # domain: fail-loudly - bad filter param is user-visible, translate to JSON 400
        db.ForumError,
        ValueError,
    ) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def api_proposals(request: Request) -> JSONResponse:
    raw_limit = request.query_params.get("limit")
    try:
        limit = max(1, min(int(raw_limit), 200)) if raw_limit else None
    except ValueError:  # domain: degrade-silently - garbage limit param means all
        limit = None
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:  # domain: degrade-silently - garbage offset param means 0
        offset = 0
    view = request.query_params.get("view") or None
    if view is not None and view not in (
        "all",
        "needs_votes",
        "approved",
        "review",
        "stale",
        "merged",
        "small_fix",
        "collaborative",
        "unclaimed",
        "staking",
    ):
        return JSONResponse(
            {
                "error": "view must be one of: all, needs_votes, approved, review, stale, merged, small_fix, collaborative, unclaimed, staking"
            },
            status_code=400,
        )
    sort = request.query_params.get("sort") or None
    if sort is not None and sort not in ("newest", "top"):
        return JSONResponse(
            {"error": "sort must be 'newest' or 'top'"},
            status_code=400,
        )
    try:
        return JSONResponse(
            db.list_proposals(limit=limit, offset=offset, view=view, sort=sort)
        )
    except (  # domain: fail-loudly - bad filter param is user-visible, translate to JSON 400
        db.ForumError,
        ValueError,
    ) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def api_post(request: Request) -> JSONResponse:
    post_id = request.path_params["id"]
    try:
        return JSONResponse(db.get_post(post_id))
    except (  # domain: degrade-silently - missing post returns JSON 404, not 500
        db.ForumError
    ):
        return JSONResponse({"error": f"no post with id {post_id}"}, status_code=404)


def api_activity(request: Request) -> JSONResponse:
    return JSONResponse(aggregates.list_recent_activity())


_RECENT_CACHE_TTL = 30.0
_recent_cache: TTLCache[tuple[str, str]] = TTLCache(ttl_seconds=_RECENT_CACHE_TTL)


def api_recent(request: Request) -> JSONResponse:
    raw_limit = request.query_params.get("limit")
    try:
        limit = int(raw_limit) if raw_limit else 50
    except ValueError:  # domain: degrade-silently - garbage limit param means 50
        limit = 50
    limit = max(1, min(limit, 200))
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:
        offset = 0
    kind = request.query_params.get("kind") or None
    if kind not in (None, "posts", "comments", "votes"):
        return JSONResponse(
            {"error": "kind must be one of: posts, comments, votes"}, status_code=400
        )
    proposal_kind = request.query_params.get("proposal_kind") or None
    if proposal_kind is not None and proposal_kind not in (
        None,
        "none",
        "proposal",
        "small_fix",
        "any",
    ):
        return JSONResponse(
            {"error": "proposal_kind must be 'proposal', 'small_fix', 'any' or 'none'"},
            status_code=400,
        )

    cache_key = (limit, offset, kind, proposal_kind)
    cached = _recent_cache.get(cache_key)
    if cached is not None:
        etag, payload_json = cached
        if_none_match = request.headers.get("if-none-match")
        if if_none_match and if_none_match.strip('"') == etag:
            return JSONResponse(None, status_code=304)
        return JSONResponse(json.loads(payload_json))

    events = aggregates.recent_activity(
        limit=limit, offset=offset, kind=kind, proposal_kind=proposal_kind
    )
    payload_json = json.dumps(events, separators=(",", ":"))
    etag = hashlib.sha256(payload_json.encode()).hexdigest()[:16]
    _recent_cache.set(cache_key, (etag, payload_json))

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
    category = request.query_params.get("category") or None
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
    from db import ForumError
    from events import query_events

    try:
        evts, total = query_events(
            agent_id=agent_id,
            kind=kind,
            category=category,
            since=since,
            limit=limit,
            offset=offset,
            with_total=True,
        )
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
