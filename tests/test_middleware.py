"""Unit tests for the /mcp HTTP-layer middleware: the ClientSeenRecording
body cap + bounded replay, the per-IP RateLimitMiddleware, and the per-IP
register_agent gate.

The middleware reads the JSON-RPC request body in a per-request ASGI task to
attribute the call to an agent, then replays the body to the mounted MCP
app. An unbounded body would be fully buffered in memory; the cap bounds
the buffer and forwards the remainder lazily. These tests exercise the
replay contract directly with a minimal receive/send stub - no server, no
DB, no MCP transport - so the cap behaviour is locked down in isolation.
The rate-limit and register-gate tests do the same: a fake inner app that
records whether it was reached, and stub config knobs via FORUM_* env vars.
"""

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server.middleware as mw_mod
from server.middleware import ClientSeenRecording, RateLimitMiddleware

SCOPE = {"type": "http", "method": "POST", "path": "/mcp", "client": None}

# A JSON-RPC body that carries no resolvable token (not a tools/call), so
# recording's DB path is never touched - the replay mechanics are the thing
# under test.
BODY = b'{"jsonrpc":"2.0","method":"initialize","params":{}}'

# An actual token-minting call: tools/call register_agent with no token, the
# request the per-IP register gate is supposed to throttle.
REGISTER_BODY = (
    b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"register_agent",'
    b'"arguments":{"name":"new-citizen","model":"test"}}}'
)

_CAP_ENV = "FORUM_MCP_BODY_CAP"
_RATE_WINDOW_ENV = "FORUM_MCP_RATE_WINDOW_SECONDS"
_RATE_MAX_ENV = "FORUM_MCP_RATE_IP_MAX_REQUESTS"
_RATE_EXEMPT_ENV = "FORUM_MCP_RATE_IP_EXEMPT"
_REGISTER_DELAY_ENV = "FORUM_MCP_REGISTER_DELAY_SECONDS"


def _with_cap(cap: int, fn: Callable[[], None]) -> None:
    """Run fn with the body-cap env set, restoring it (or removing it)
    afterwards, like the other tunable tests do."""
    old = os.environ.get(_CAP_ENV)
    os.environ[_CAP_ENV] = str(cap)
    try:
        fn()
    finally:
        if old is None:
            os.environ.pop(_CAP_ENV, None)
        else:
            os.environ[_CAP_ENV] = old


def _with_env(settings: dict[str, str], fn: Callable[[], None]) -> None:
    """Set several FORUM_* knobs for fn(), restoring the environment after."""
    import contextlib

    with contextlib.ExitStack() as stack:
        saved: dict[str, str | None] = {}
        for key, value in settings.items():
            saved[key] = os.environ.get(key)
            os.environ[key] = value
            stack.callback(_restore_env, key, saved[key])
        fn()


def _restore_env(key: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


def _mk_receive(*messages: tuple[bytes, bool]) -> Callable[[], Any]:
    """An async receive() callable yielding http.request messages in order,
    then a disconnect."""
    it = iter(messages)

    async def recv():
        try:
            body, more = next(it)
            return {"type": "http.request", "body": body, "more_body": more}
        except StopIteration:
            return {"type": "http.disconnect"}

    return recv


def _run(messages: list[tuple[bytes, bool]]) -> dict[str, Any]:
    """Drive the middleware (plus a fake app that records what it reads)
    over the given request chunks and return what the app saw."""
    seen: list[bytes] = []
    flags: list[bool] = []

    async def fake_app(scope, receive, send) -> None:
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            seen.append(msg.get("body", b""))
            flags.append(bool(msg.get("more_body", False)))
            if not msg.get("more_body", False):
                break

    async def fake_send(message) -> None:
        pass

    async def run() -> None:
        mw = ClientSeenRecording(fake_app)
        await mw(SCOPE, _mk_receive(*messages), fake_send)

    asyncio.run(run())
    return {"body": b"".join(seen), "flags": flags}


def _noop_app() -> Any:
    """A do-nothing async ASGI app. Used only as the constructor stub for the
    middleware under test - `_drive` swaps in its own tracking app for the
    run, so the stub's behaviour never matters."""

    async def app(scope, receive, send) -> None:
        return None

    return app


def _drive(mw: Any, scope: dict[str, Any], body: bytes) -> dict[str, Any]:
    """Drive a middleware over a single whole-body request and report whether
    the inner app was reached and what response messages were sent. The
    middleware's own downstream app is swapped for a stub that records the
    call - state that matters (limiter hits, register-gate stamps) lives on
    the middleware instance, so sequential _drive calls share it only when
    the caller reuses the instance."""
    reached: list[bool] = [False]
    sent: list[dict[str, Any]] = []

    async def fake_app(scope, receive, send) -> None:
        reached[0] = True
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            if not msg.get("more_body", False):
                break

    async def fake_send(message) -> None:
        sent.append(message)

    async def run() -> None:
        mw.app = fake_app
        await mw(scope, _mk_receive((body, False)), fake_send)

    asyncio.run(run())
    status = next(
        (m.get("status") for m in sent if m.get("type") == "http.response.start"),
        None,
    )
    return {"reached": reached[0], "status": status, "sent": sent}


def _retry_after(sent: list[dict[str, Any]]) -> int | None:
    for m in sent:
        if m.get("type") != "http.response.start":
            continue
        for name, value in m.get("headers", []):
            if name == b"retry-after":
                return int(value)
    return None


def test_uncapped_single_message_replay():
    """A body under the cap is buffered whole and replayed as one message
    with more_body=False - the pre-cap behaviour (and the default cap is far
    above any ordinary JSON-RPC body)."""
    out = _run([(BODY[:20], True), (BODY[20:], False)])
    assert out["body"] == BODY
    assert out["flags"] == [False]


def test_capped_forwards_remainder_unbuffered():
    """Past the cap the middleware forwards the rest of the stream without
    buffering it: the prefix arrives with more_body=True, then the remaining
    chunks flow through, and the app reassembles the exact original body."""
    result: dict[str, Any] = {}

    def go():
        # Chunks of 12 bytes each; the cap of 20 is crossed on the 2nd chunk.
        c1, c2, c3 = BODY[:12], BODY[12:24], BODY[24:]
        result.update(_run([(c1, True), (c2, True), (c3, False)]))

    _with_cap(20, go)
    assert result["body"] == BODY
    # The buffered prefix was marked more_body=True so the app kept reading,
    # and the forwarded stream ended on a more_body=False.
    assert result["flags"][0] is True
    assert result["flags"][-1] is False


def test_capped_single_oversized_chunk_preserved():
    """A single chunk crossing the cap is preserved whole and the subsequent
    stream is forwarded - no byte is dropped or reordered."""
    result: dict[str, Any] = {}

    def go():
        big = BODY  # 44 bytes, far over an 8-byte cap in one chunk
        result.update(_run([(big, True), (b"TAIL", False)]))
        result["expected"] = big + b"TAIL"

    _with_cap(8, go)
    assert result["body"] == result["expected"]


def test_rate_limit_localhost_exempt():
    """Loopback is in the default exempt CIDRs, so a low cap never throttles
    it - two requests through the same limiter both reach the app."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "client": ("127.0.0.1", 1),
    }
    results: dict[str, Any] = {}

    def go():
        limiter = RateLimitMiddleware(_noop_app())
        once = _drive(limiter, scope, BODY)
        twice = _drive(limiter, scope, BODY)
        results.update({"r1": once, "r2": twice})

    _with_env({_RATE_MAX_ENV: "1", _RATE_WINDOW_ENV: "60"}, go)
    assert results["r1"]["reached"] is True
    assert results["r2"]["reached"] is True
    assert results["r1"]["status"] is None and results["r2"]["status"] is None

    def go2():
        limiter = RateLimitMiddleware(_noop_app())
        once = _drive(limiter, scope, BODY)
        twice = _drive(limiter, scope, BODY)
        results.update({"r3": once, "r4": twice})

    _with_env({_RATE_MAX_ENV: "1", _RATE_WINDOW_ENV: "1"}, go2)
    assert results["r3"]["reached"] is True
    assert results["r4"]["reached"] is True


def test_rate_limit_external_blocked_over_cap():
    """A non-exempt (public) IP is throttled: below the cap requests pass,
    the one that crosses it gets HTTP 429 with a Retry-After header and a
    JSON-RPC error body, and the inner app is never reached for the blocked
    one."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "client": ("203.0.113.7", 1),
    }
    results: dict[str, Any] = {}

    def go():
        limiter = RateLimitMiddleware(_noop_app())
        results["first"] = _drive(limiter, scope, BODY)
        results["blocked"] = _drive(limiter, scope, BODY)
        results["sent"] = results["blocked"]["sent"]
        results["retry_after"] = _retry_after(results["blocked"]["sent"])

    _with_env({_RATE_MAX_ENV: "1", _RATE_WINDOW_ENV: "60"}, go)
    assert results["first"]["reached"] is True
    assert results["first"]["status"] is None
    assert results["blocked"]["reached"] is False
    assert results["blocked"]["status"] == 429
    assert isinstance(results["retry_after"], int) and results["retry_after"] > 0
    body = b"".join(
        m.get("body", b"")
        for m in results["sent"]
        if m.get("type") == "http.response.body"
    )
    assert b'"code":-32000' in body and b"rate limited" in body


def test_rate_limit_tiny_window_expires():
    """A request that crossed the cap gets through again once its window has
    elapsed - the sliding window is bounded, not a lifetime ban."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "client": ("203.0.113.9", 1),
    }
    results: dict[str, Any] = {}
    clock = [1000.0]
    limiter = RateLimitMiddleware(_noop_app())

    def go():
        results["first"] = _drive(limiter, scope, BODY)
        results["blocked"] = _drive(limiter, scope, BODY)

    with mock.patch.object(mw_mod.time, "monotonic", side_effect=lambda: clock[0]):
        _with_env({_RATE_MAX_ENV: "1", _RATE_WINDOW_ENV: "60"}, go)
    assert results["first"]["reached"] is True
    assert results["blocked"]["status"] == 429

    def go2():
        results["expired"] = _drive(limiter, scope, BODY)

    clock[0] = 5000.0  # 4000s later - far past the 60s window
    with mock.patch.object(mw_mod.time, "monotonic", side_effect=lambda: clock[0]):
        _with_env({_RATE_MAX_ENV: "1", _RATE_WINDOW_ENV: "60"}, go2)
    assert results["expired"]["reached"] is True


def test_rate_limit_wrong_path_passthrough():
    """Non-/mcp routes are never rate limited."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/healthz",
        "client": ("203.0.113.11", 1),
    }
    results: dict[str, Any] = {}

    def go():
        limiter = RateLimitMiddleware(_noop_app())
        for _ in range(5):
            results["r"] = _drive(limiter, scope, BODY)

    _with_env({_RATE_MAX_ENV: "1", _RATE_WINDOW_ENV: "60"}, go)
    assert results["r"]["reached"] is True


def test_register_gate_blocks_second_registration():
    """register_agent is gated per IP even on loopback (it mints tokens): a
    second registration from the same IP inside the delay is 429'd before it
    reaches the app."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "client": ("127.0.0.1", 1),
    }
    results: dict[str, Any] = {}

    def go():
        mw = ClientSeenRecording(_noop_app())
        results["first"] = _drive(mw, scope, REGISTER_BODY)
        results["second"] = _drive(mw, scope, REGISTER_BODY)
        results["retry_after"] = _retry_after(results["second"]["sent"])

    _with_env({_REGISTER_DELAY_ENV: "900"}, go)
    assert results["first"]["reached"] is True
    assert results["second"]["reached"] is False
    assert results["second"]["status"] == 429
    retry = results["retry_after"]
    assert isinstance(retry, int) and 0 < retry <= 900


def test_register_gate_delay_elapsed_allows():
    """After the delay elapses the same IP may register again."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "client": ("127.0.0.1", 1),
    }
    results: dict[str, Any] = {}
    clock = [1000.0]
    mw = ClientSeenRecording(_noop_app())

    def go():
        results["first"] = _drive(mw, scope, REGISTER_BODY)
        results["second"] = _drive(mw, scope, REGISTER_BODY)
        assert results["first"]["reached"] is True
        assert results["second"]["status"] == 429
        clock[0] = 5000.0  # 4000s later - well past the 15-minute delay
        results["later"] = _drive(mw, scope, REGISTER_BODY)
        assert results["later"]["reached"] is True

    with mock.patch.object(mw_mod.time, "monotonic", side_effect=lambda: clock[0]):
        _with_env({_REGISTER_DELAY_ENV: "900"}, go)


def test_register_gate_zero_delay_disabled():
    """A 0 delay disables the register gate entirely."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "client": ("127.0.0.1", 1),
    }
    results: dict[str, Any] = {}

    def go():
        mw = ClientSeenRecording(_noop_app())
        results["first"] = _drive(mw, scope, REGISTER_BODY)
        results["second"] = _drive(mw, scope, REGISTER_BODY)

    _with_env({_REGISTER_DELAY_ENV: "0"}, go)
    assert results["first"]["reached"] is True
    assert results["second"]["reached"] is True


def test_register_gate_ignores_other_calls():
    """Non-register tools/call and non-tools bodies are never gated."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "client": ("127.0.0.1", 1),
    }
    results: dict[str, Any] = {}
    other_tool = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_rules","arguments":{}}}'

    def go():
        mw = ClientSeenRecording(_noop_app())
        results["tool"] = _drive(mw, scope, other_tool)
        results["init"] = _drive(mw, scope, BODY)
        results["tool2"] = _drive(mw, scope, other_tool)

    _with_env({_REGISTER_DELAY_ENV: "900"}, go)
    assert results["tool"]["reached"] is True
    assert results["init"]["reached"] is True
    assert results["tool2"]["reached"] is True


if __name__ == "__main__":
    test_uncapped_single_message_replay()
    test_capped_forwards_remainder_unbuffered()
    test_capped_single_oversized_chunk_preserved()
    test_rate_limit_localhost_exempt()
    test_rate_limit_external_blocked_over_cap()
    test_rate_limit_tiny_window_expires()
    test_rate_limit_wrong_path_passthrough()
    test_register_gate_blocks_second_registration()
    test_register_gate_delay_elapsed_allows()
    test_register_gate_zero_delay_disabled()
    test_register_gate_ignores_other_calls()
    print("test_middleware: all ok")
