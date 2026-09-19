"""De-indexing pins: robots.txt, noindex meta, X-Robots-Tag headers.

Import order matters: `server._app` boots first (the production boot order -
viewer -> server/__init__ -> server/_app -> viewer is circular, so importing
the viewer package first dies with 'partially initialized module ... has no
attribute ROUTES'). Importing server._app completes the whole chain, after
which the viewer.* submodule imports below are safe.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_robots_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server._app as _server_app  # noqa: E402
from server.middleware import NoIndexHeaders  # noqa: E402
from viewer import _layout  # noqa: E402
from viewer._static import ROBOTS_TXT, static_robots_txt  # noqa: E402

_EXPECTED_ROBOTS = "User-agent: *\nDisallow: /\nCrawl-delay: 10\n"
_META_TAG = '<meta name="robots" content="noindex,nofollow">'


def _sent_messages(scope):
    messages = []

    async def fake_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/html")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def recv():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    asyncio.run(NoIndexHeaders(fake_app)(scope, recv, send))
    return messages


def _tag_of(path):
    scope = {"type": "http", "method": "GET", "path": path}
    msgs = _sent_messages(scope)
    start = next(m for m in msgs if m["type"] == "http.response.start")
    return dict(start["headers"]).get(b"x-robots-tag")


def main():
    # robots.txt body: whole public surface disallowed, crawl slowed.
    assert ROBOTS_TXT == _EXPECTED_ROBOTS, repr(ROBOTS_TXT)
    print("  robots body exact: ok")

    # Handler: plain text, day-long cache like the stylesheet.
    resp = static_robots_txt(None)
    assert resp.status_code == 200
    assert resp.body == _EXPECTED_ROBOTS.encode()
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["cache-control"] == "public, max-age=86400"
    print("  robots handler headers: ok")

    # Route live in the production app with no catch-all Mount ahead of it
    # (a Mount carries sub-routes; plain Routes do not).
    routes = _server_app.app.routes
    paths = [getattr(r, "path", "") for r in routes]
    assert "/robots.txt" in paths, paths
    robot_idx = paths.index("/robots.txt")
    for r in routes[:robot_idx]:
        assert not hasattr(r, "routes"), f"catch-all precedes robots: {r!r}"
    print("  robots route live before MCP catch-all: ok")

    # Every HTML page carries the meta tag via the shared shell.
    assert _META_TAG in _layout.PAGE
    print("  noindex meta in PAGE shell: ok")

    # Machine surfaces stamped, pages / robots / MCP untouched.
    for p in ("/api/posts", "/api/overview", "/feed", "/fragments/x"):
        assert _tag_of(p) == b"noindex, nofollow", p
    print("  X-Robots-Tag on api/feed/fragments: ok")
    for p in ("/", "/posts", "/posts/1", "/robots.txt", "/mcp"):
        assert _tag_of(p) is None, p
    print("  no stamp on pages/robots/mcp: ok")

    # Non-HTTP scopes pass straight through.
    reached = []

    async def inner(scope, receive, send):
        reached.append(True)

    async def recv():
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {"type": "websocket", "path": "/api/x"}
    asyncio.run(NoIndexHeaders(inner)(scope, recv, send))
    assert reached == [True]
    print("  non-http passthrough: ok")


if __name__ == "__main__":
    main()
    print("All robots tests passed.")
