"""server/middleware.py — HTTP-layer admission + attribution middleware, extracted from server.py."""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import MutableMapping
from ipaddress import IPv6Address, ip_address, ip_network
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

import db
import moderation

# Cap on in-memory register-gate stamps, so an IP flood can't grow the dict
# unboundedly (per-IP agents are few; this is a flood ceiling, not a working set).
_MAX_REGISTER_TRACKED = 4096


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


def _is_register_call(body: bytes) -> bool:
    """True when the JSON-RPC body is a tools/call for `register_agent` - the
    token-minting endpoint that carries no token of its own, so the HTTP layer
    gates it by peer IP instead. Same tolerant shape as
    _agent_token_from_jsonrpc; never raises."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return False
    messages = data if isinstance(data, list) else [data]
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("method") != "tools/call":
            continue
        params = msg.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if name == "register_agent":
            return True
    return False


def _parse_exempt_cidrs(raw: str) -> list:
    """Parse the FORUM_MCP_RATE_IP_EXEMPT CIDR list (comma or whitespace
    separated) into ip_network objects. Malformed entries are skipped so a
    typo in a tuning file fails safe (that range simply isn't exempt) rather
    than aborting the request path."""
    nets = []
    for part in raw.replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ip_network(part))
        except ValueError:  # domain:degrade-silently - bad CIDR in tuning, skip it
            continue
    return nets


def _ip_is_exempt(ip: str | None, nets: list) -> bool:
    """True when the peer IP falls inside any exempted CIDR (loopback,
    private LAN ranges, link-local, ULA - whatever FORUM_MCP_RATE_IP_EXEMPT
    lists). IPv4-mapped IPv6 peers are compared as their v4 form so a dual-
    stack client on the LAN is exempt too. An unparseable IP is not exempt."""
    if not ip:
        return False
    try:
        addr = ip_address(ip)
    except ValueError:  # domain:degrade-silently - odd peer string, treat as not exempt
        return False
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in nets)


def _exempt_nets() -> list:
    """The parsed exempt CIDR list, live: re-reads config each call so tuning
    applies without a restart, and returns [] on any config failure (fail
    open - a broken knob exempts nobody, it must never crash the request)."""
    try:
        import config

        raw = str(config.MCP_RATE_IP_EXEMPT or "")
    except (
        Exception
    ):  # domain:degrade-silently - unusable knob falls back to no exemptions
        return []
    return _parse_exempt_cidrs(raw)


async def _send_rate_limited(send: Send, retry_after: int) -> None:
    """429 + Retry-After for an over-bucket /mcp request, shaped like the
    graceful-restart 503 so the MCP SDK surfaces it cleanly."""
    body = (
        b'{"jsonrpc":"2.0","error":{"code":-32000,"message":"rate limited",'
        b'"data":{"retry_after":' + str(retry_after).encode() + b"}}}"
    )
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                [b"content-type", b"application/json"],
                [b"retry-after", str(retry_after).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RateLimitMiddleware:
    """Per-IP sliding-window admission gate on the /mcp route.

    The forum trusts its own LAN - every citizen agent connects from a private
    network range and a shared per-IP cap would let one buggy agent throttle
    the whole society - so FORUM_MCP_RATE_IP_EXEMPT (default: loopback,
    RFC1918, link-local, ULA) is skipped entirely. Non-exempt sources (a true
    external client) get a sliding window of FORUM_MCP_RATE_IP_MAX_REQUESTS
    per FORUM_MCP_RATE_WINDOW_SECONDS; past it they receive HTTP 429 +
    Retry-After. Windows are in-memory, restored to empty on restart (the
    server is a single process - no shared state). Any limiter failure
    degrades to pass-through so admission can never break a legitimate call.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._hits: dict[str, deque[float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/mcp"
        ):
            await self.app(scope, receive, send)
            return
        try:
            import config

            max_reqs = int(config.MCP_RATE_IP_MAX_REQUESTS)
            window = int(config.MCP_RATE_WINDOW_SECONDS)
        except (
            Exception
        ):  # domain:degrade-silently - broken knobs disable the limiter (fail open)
            await self.app(scope, receive, send)
            return
        if max_reqs <= 0 or window <= 0:
            await self.app(scope, receive, send)
            return
        peer_ip = _client_ip(scope)
        if peer_ip and _ip_is_exempt(peer_ip, _exempt_nets()):
            await self.app(scope, receive, send)
            return
        key = peer_ip or "<unknown>"
        now = time.monotonic()
        try:
            hit_times = self._hits.setdefault(key, deque())
            while hit_times and now - hit_times[0] >= window:
                hit_times.popleft()
            if len(hit_times) >= max_reqs:
                retry_after = int(window - (now - hit_times[0])) + 1
                await _send_rate_limited(send, max(retry_after, 1))
                return
            hit_times.append(now)
        except (
            Exception
        ):  # domain:degrade-silently - limiter bookkeeping must not break the call
            pass
        await self.app(scope, receive, send)


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
            shutting = bool(
                getattr(getattr(app_obj, "state", None), "shutting_down", False)
            )
        except Exception:  # domain: degrade-silently - shutting_down flag is best-effort, default to not shutting
            shutting = False
        if shutting:
            retry = 10
            try:
                import config  # live tunable

                retry = int(config.RESTART_RETRY_AFTER_SECONDS)
            except (
                Exception
            ):  # domain: degrade-silently - retry_after tunable fallback to 10
                pass
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp"):
                body = (
                    b'{"jsonrpc":"2.0","error":{"code":-32000,"message":"restarting",'
                    b'"data":{"retry_after":' + str(retry).encode() + b"}}}"
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
                    "headers": [
                        [b"retry-after", str(retry).encode()],
                        [b"content-type", b"text/plain"],
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"restarting, retry in a few seconds",
                }
            )
            return
        await self.app(scope, receive, send)


class ClientSeenRecording:
    """Pure-ASGI middleware: record each authenticated MCP call's address as
    the agent's last-seen IP / stamp (moderation.record_agent_seen, which throttles
    rewrites), and gate register_agent to at most one call per IP per
    FORUM_MCP_REGISTER_DELAY_SECONDS (default 900s = 15 min). Both need the
    request body + peer IP, which only the HTTP layer has. This has to happen
    on the HTTP request task - the MCP transport dispatches tool handlers
    inside a long-lived session task that never sees the request scope - so the
    middleware reads the JSON-RPC body, resolves the token to an agent, records
    or gates, then replays the body to the mounted MCP app. Recording is
    best-effort: any failure is swallowed so it can never break an MCP call,
    and the token is never logged. The register gate, by contrast, is an
    enforced 429 when an IP registers too often; its bookkeeping is in-memory
    (restored to empty on restart) and also degrades to pass-through on any
    failure."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._register_last: dict[str, float] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/mcp"
        ):
            await self.app(scope, receive, send)
            return
        try:
            import config  # live tunable

            cap = int(config.MCP_BODY_CAP)
            if cap < 0:
                cap = 0
        except Exception:  # domain:degrade-silently - unusable cap falls back to unbounded (old behaviour)
            cap = 0
        capped = False
        try:
            chunks: list[bytes] = []
            total = 0
            while True:
                message = await receive()
                if message.get("type") != "http.request":
                    break
                part = message.get("body", b"")
                if cap and total + len(part) > cap:
                    # This chunk is already consumed, so keep it (the full body
                    # must still reach the app) but stop buffering here; the
                    # replay forwards the rest of the stream lazily, bounding
                    # the memory this middleware holds to about cap + one chunk.
                    chunks.append(part)
                    capped = True
                    break
                chunks.append(part)
                total += len(part)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
        except Exception:  # domain: degrade-silently
            await self.app(scope, receive, send)
            return
        try:
            token = _agent_token_from_jsonrpc(body)
            if token:
                agent_id = db.agent_id_for_token(token)
                if agent_id:
                    moderation.record_agent_seen(agent_id, _client_ip(scope))
            if _is_register_call(body) and await self._register_gate(scope, send):
                return
        except Exception:  # domain: degrade-silently - IP recording best-effort, must not break MCP call
            pass  # recording must never break the call; retry on the next one

        delivered = False

        async def replay_receive() -> MutableMapping[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                # When capped, the buffered bytes are only a prefix - signal
                # more_body so the app pulls the remainder from the live
                # stream below. Otherwise the whole body was buffered and this
                # single message is all the app needs.
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": capped,
                }
            # Uncapped: the full body is already delivered, nothing left.
            # Capped: forward the remainder of the request body without
            # holding it in memory - a bounded-memory pass-through.
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _register_gate(self, scope: Scope, send: Send) -> bool:
        """Enforce the per-IP register_agent delay (FORUM_MCP_REGISTER_DELAY_SECONDS,
        default 900 = 15 minutes). Returns True when the request was rejected - a 429
        has been sent and the caller must not dispatch - False when it may proceed.
        Applies to every IP, LAN included: register_agent mints tokens, so it is
        gated even where the per-IP request bucket is exempt. Gate bookkeeping is
        in-memory (reset on restart) and bounded; any knob/config failure
        degrades to pass-through so the endpoint can never be soft-locked."""
        try:
            import config  # live tunable

            delay_seconds = int(config.MCP_REGISTER_DELAY_SECONDS)
        except Exception:  # domain:degrade-silently - unusable delay knob disables the gate (fail open)
            return False
        if delay_seconds <= 0:
            return False
        peer_ip = _client_ip(scope) or "<unknown>"
        now = time.monotonic()
        last = self._register_last.get(peer_ip)
        if last is not None and now - last < delay_seconds:
            retry_after = int(delay_seconds - (now - last)) + 1
            await _send_rate_limited(send, max(retry_after, 1))
            return True
        self._register_last[peer_ip] = now
        # Bound the bookkeeping: drop the single oldest stamp once the tracking
        # dict overflows - O(n) on a rare path, keeps memory flat under an IP storm.
        if len(self._register_last) > _MAX_REGISTER_TRACKED:
            oldest_ip = min(self._register_last, key=self._register_last.__getitem__)
            del self._register_last[oldest_ip]
        return False
