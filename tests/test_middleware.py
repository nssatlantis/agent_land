"""Unit tests for the ClientSeenRecording /mcp body cap + bounded replay.

The middleware reads the JSON-RPC request body in a per-request ASGI task to
attribute the call to an agent, then replays the body to the mounted MCP
app. An unbounded body would be fully buffered in memory; the cap bounds
the buffer and forwards the remainder lazily. These tests exercise the
replay contract directly with a minimal receive/send stub - no server, no
DB, no MCP transport - so the cap behaviour is locked down in isolation.
"""

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.middleware import ClientSeenRecording

SCOPE = {"type": "http", "method": "POST", "path": "/mcp", "client": None}

# A JSON-RPC body that carries no resolvable token (not a tools/call), so
# recording's DB path is never touched - the replay mechanics are the thing
# under test.
BODY = b'{"jsonrpc":"2.0","method":"initialize","params":{}}'

_CAP_ENV = "FORUM_MCP_BODY_CAP"


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


if __name__ == "__main__":
    test_uncapped_single_message_replay()
    test_capped_forwards_remainder_unbuffered()
    test_capped_single_oversized_chunk_preserved()
    print("test_middleware: all ok")
