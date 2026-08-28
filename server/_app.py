"""server/_app.py — Starlette app + lifespan, extracted from server.py."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

import config
import db
import viewer
from server import admin
from server._mcp import mcp
from server.middleware import ClientSeenRecording, GracefulRestartMiddleware
from server.poller import _ci_failure_poller, _pr_outcome_poller
import logutil

_host = config.FORUM_HOST
_port = config.FORUM_PORT

mcp_app = mcp.streamable_http_app(host=_host)

_START_TIME = time.monotonic()
_RESTART_COUNT_FILE = Path(config.DATA_DIR) / ".restart_count"
_LAST_RESTART_FILE = Path(config.DATA_DIR) / ".last_restart"


def _read_restart_count() -> int:
    try:
        return int(_RESTART_COUNT_FILE.read_text().strip())
    except Exception:  # domain: degrade-silently - restart count is best-effort metrics, missing file means 0
        return 0


def _bump_restart_count() -> int:
    try:
        c = _read_restart_count() + 1
        _RESTART_COUNT_FILE.write_text(str(c))
        _LAST_RESTART_FILE.write_text(str(time.time()))
        return c
    except Exception:  # domain: degrade-silently - metrics must not block boot
        return 0


async def healthz(request: Request) -> JSONResponse:
    """Unauthenticated liveness/readiness for agents and load-balancers.

    Returns 200 with uptime, restart count, last restart, and sha when
    not shutting down. During the 10s drain the GracefulRestartMiddleware
    already returns 503 with Retry-After, so this handler is only hit when
    live. Standard: unauthenticated, no DB write, tiny.
    """
    # Check shutting_down flag set by lifespan (also covered by middleware, but
    # keep explicit for direct calls)
    shutting = bool(getattr(request.app.state, "shutting_down", False))
    if shutting:
        retry = 10
        try:
            retry = int(config.RESTART_RETRY_AFTER_SECONDS)
        except Exception:  # domain: degrade-silently - retry_after tunable fallback to 10
            pass
        return JSONResponse(
            {"status": "restarting", "retry_after": retry},
            status_code=503,
            headers={"Retry-After": str(retry)},
        )
    uptime = time.monotonic() - _START_TIME
    # Best-effort git sha (no throw)
    sha = ""
    try:
        import subprocess

        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(config.REPO_DIR),
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except Exception:  # domain: degrade-silently - git sha is best-effort, empty on failure
        pass
    return JSONResponse(
        {
            "status": "ok",
            "uptime_s": round(uptime, 1),
            "restart_count": _read_restart_count(),
            "last_restart": _LAST_RESTART_FILE.read_text().strip() if _LAST_RESTART_FILE.exists() else None,
            "sha": sha,
        }
    )


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    # Bootstrap on any entry point (python -m server or uvicorn server:app):
    # a missing database file is recreated with a fresh schema instead of the
    # app serving a schema-less file. Idempotent, so __main__ may call it too.
    db.init_db()
    # F3: bump restart count on every successful boot (after DB is known good)
    try:
        rc = _bump_restart_count()
        logutil.log("restart_complete", restart_count=rc, uptime_s=0)
    except Exception:  # domain: degrade-silently - metrics must not block boot
        pass
    poller = asyncio.create_task(_pr_outcome_poller())
    ci_poller = asyncio.create_task(_ci_failure_poller())
    watcher = config.spawn_env_watcher()
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        # D3: graceful 10s drain — mark shutting down, serve 503 with Retry-After
        # instead of RST, let in-flight tool calls finish.
        try:
            app.state.shutting_down = True
            logutil.log("restart_draining", graceful_s=int(config.GRACEFUL_SHUTDOWN_SECONDS))
        except Exception:  # domain: degrade-silently - draining flag is best-effort
            pass
        try:
            await asyncio.sleep(int(config.GRACEFUL_SHUTDOWN_SECONDS))
        except asyncio.CancelledError:  # domain: degrade-silently - sleep cancelled on fast shutdown
            pass
        watcher.cancel()
        poller.cancel()
        ci_poller.cancel()
        # Debounced ticker from server/tools/repo.py (15s coalesce) — cancel
        # and await to avoid "Task was destroyed but it is pending" (L2).
        ticker_task = None
        try:
            from server.tools.repo import _TICKER_TASK, _cancel_ticker

            ticker_task = _TICKER_TASK
            _cancel_ticker()
        except Exception:
            pass  # domain: degrade-silently - ticker cancel must not stall shutdown
        try:
            await poller
            await ci_poller
        except asyncio.CancelledError:  # domain: degrade-silently - poller cancel is expected on shutdown
            pass
        if ticker_task is not None and not ticker_task.done():
            try:
                await ticker_task
            except asyncio.CancelledError:
                pass  # domain: degrade-silently - ticker cancel is expected
            except Exception:
                pass  # domain: degrade-silently - ticker must not stall shutdown


app = Starlette(
    routes=[
        Route("/healthz", healthz),
        *admin.ROUTES,
        *viewer.ROUTES,
        Mount("/", app=mcp_app),
    ],
    lifespan=lifespan,
    middleware=[
        Middleware(GracefulRestartMiddleware),
        Middleware(GZipMiddleware, minimum_size=500),
        Middleware(logutil.RequestLogging),
        Middleware(ClientSeenRecording),
    ],
)
