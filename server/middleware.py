"""server/middleware.py — ClientSeenRecording ASGI middleware, extracted from server.py."""

from __future__ import annotations

import json
from typing import Any

from collections.abc import MutableMapping

from starlette.types import ASGIApp, Receive, Scope, Send

import db
import moderation


def _client_ip(scope: MutableMapping[str, Any]) -> str | None:
    """The caller's address for an HTTP request - the direct TCP peer, never
    a client-supplied header (X-Forwarded-For is attacker-controlled and
    there is no proxy in the LAN deployment). None when the transport did
    not provide one."""
    client = scope.get("client")
    return client[0] if client else None


def _agent_token_from_jsonrpc(body: bytes) -> str | None:
    """Pull the `token` argument out of a JSON-RPC tools/call message so the
    HTTP layer can attribute the request to an agent. Returns None for
    anything that is not such a message (initialize, notifications, batches
    without a token, malformed JSON) and never raises. The token itself is
    used only to resolve an agent id - it is never logged."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    messages = data if isinstance(data, list) else [data]
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("method") != "tools/call":
            continue
        params = msg.get("params")
        args = params.get("arguments") if isinstance(params, dict) else None
        token = args.get("token") if isinstance(args, dict) else None
        if isinstance(token, str) and token:
            return token
    return None


class GracefulRestartMiddleware:
    """Return 503 + Retry-After during the 10s graceful drain instead of RST.

    When server/_app.lifespan sets `app.state.shutting_down = True` before
    cancelling pollers, this outer middleware still runs (Starlette's stack
    stays up until lifespan finally exits). Every HTTP hit during the drain
    gets a retryable JSON-RPC error for MCP and a plain 503 for viewer/healthz,
    so agents see `retry_after` not `ECONNREFUSED`.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        # app.state.shutting_down is set by lifespan's finally before cancels
        app_obj = scope.get("app")
        shutting = False
        try:
            shutting = bool(getattr(getattr(app_obj, "state", None), "shutting_down", False))
        except Exception:  # domain: degrade-silently - shutting_down flag is best-effort, default to not shutting
            shutting = False
        if shutting:
            retry = 10
            try:
                import config  # live tunable
                retry = int(config.RESTART_RETRY_AFTER_SECONDS)
            except Exception:  # domain: degrade-silently - retry_after tunable fallback to 10
                pass
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp"):
                body = (
                    b'{"jsonrpc":"2.0","error":{"code":-32000,"message":"restarting",'
                    b'"data":{"retry_after":' + str(retry).encode() + b'}}}'
                )
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"retry-after", str(retry).encode()],
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            # Viewer/healthz/other GETs: plain 503
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [[b"retry-after", str(retry).encode()], [b"content-type", b"text/plain"]],
                }
            )
            await send({"type": "http.response.body", "body": b"restarting, retry in a few seconds"})
            return
        await self.app(scope, receive, send)


class ClientSeenRecording:
    """Pure-ASGI middleware: record each authenticated MCP call's address as
    the agent's last-seen IP / stamp (moderation.record_agent_seen, which throttles
    rewrites). This has to happen on the HTTP request task - the MCP
    transport dispatches tool handlers inside a long-lived session task that
    never sees the request scope - so the middleware reads the JSON-RPC body,
    resolves the token to an agent, records, then replays the body to the
    mounted MCP app. Recording is best-effort: any failure is swallowed so it
    can never break an MCP call, and the token is never logged."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/mcp"
        ):
            await self.app(scope, receive, send)
            return
        try:
            chunks = []
            while True:
                message = await receive()
                if message.get("type") != "http.request":
                    break
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
        except Exception:
            await self.app(scope, receive, send)
            return
        try:
            token = _agent_token_from_jsonrpc(body)
            if token:
                agent_id = db.agent_id_for_token(token)
                if agent_id:
                    moderation.record_agent_seen(agent_id, _client_ip(scope))
        except Exception:
            pass  # recording must never break the call; retry on the next one

        delivered = False

        async def replay_receive() -> MutableMapping[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)
