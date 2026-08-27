"""server/_app.py — Starlette app + lifespan, extracted from server.py."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Mount

import config
import db
import viewer
from server import admin
from server._mcp import mcp
from server.middleware import ClientSeenRecording
from server.poller import _ci_failure_poller, _pr_outcome_poller
import logutil

_host = config.FORUM_HOST
_port = config.FORUM_PORT

mcp_app = mcp.streamable_http_app(host=_host)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    # Bootstrap on any entry point (python -m server or uvicorn server:app):
    # a missing database file is recreated with a fresh schema instead of the
    # app serving a schema-less file. Idempotent, so __main__ may call it too.
    db.init_db()
    poller = asyncio.create_task(_pr_outcome_poller())
    ci_poller = asyncio.create_task(_ci_failure_poller())
    watcher = config.spawn_env_watcher()
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        watcher.cancel()
        poller.cancel()
        ci_poller.cancel()
        # Debounced ticker from server/tools/repo.py (15s coalesce) — fire-and-forget
        try:
            from server.tools.repo import _cancel_ticker

            _cancel_ticker()
        except Exception:
            pass  # domain: degrade-silently - ticker cancel must not stall shutdown
        try:
            await poller
            await ci_poller
        except asyncio.CancelledError:
            pass


app = Starlette(
    routes=admin.ROUTES + viewer.ROUTES + [Mount("/", app=mcp_app)],
    lifespan=lifespan,
    middleware=[
        Middleware(GZipMiddleware, minimum_size=500),
        Middleware(logutil.RequestLogging),
        Middleware(ClientSeenRecording),
    ],
)
