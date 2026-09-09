"""Shared preamble for the ordered e2e suites (tests/test_e2e_*.py).

Split out of tests/test_client.py: every suite ran the same guard, URL,
result-unwrap and session-open lines. One copy here, imported by all four
files — run order is 01_forum → 02_governance → 03_prs → 04_collab_viewer
on a single shared server DB (see tests/run_e2e.py), because later files
reuse the agents and posts the earlier ones register.

The cross-file context (tokens, post/agents ids) rides a small JSON file
in AGENTLAND_DATA_DIR (the runner's throwaway dir, or the OS temp dir):
file 01 saves it, files 02-04 load it and refuse with a clear message
when run out of order.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from contextlib import asynccontextmanager

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL = f"http://{os.environ.get('FORUM_HOST', '127.0.0.1')}:{int(os.environ.get('FORUM_PORT', '8000'))}/mcp"

CTX_KEYS = ("token1", "token2", "token3", "post_id", "a1_id", "a1_name")


def _is_loopback(host: str) -> bool:
    """True when host is a loopback address or resolves to one."""
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:  # domain: fail-closed - an unresolvable host is not trusted
        return False
    return any(a == "::1" or a.startswith("127.") for a in addrs)


def _assert_safe_target() -> None:
    """Refuse to run the e2e suites against anything but loopback.

    The suites register agents, posts, comments, votes and proposals.
    Pointed at a non-loopback host they would write test fixtures into a
    real forum, so that target requires an explicit opt-in."""
    host = os.environ.get("FORUM_HOST", "127.0.0.1")
    if not _is_loopback(host) and not os.environ.get("FORUM_TEST_ALLOW_REMOTE"):
        sys.exit(
            "refusing to run the e2e suites against a non-loopback host "
            f"({host}) - they would write test fixtures into a real forum.\n"
            "Run them via tests/run_e2e.py (self-isolated on "
            "127.0.0.1 with a throwaway database), or set "
            "FORUM_TEST_ALLOW_REMOTE=1 to explicitly accept a remote target."
        )


def unwrap(result):
    if result.is_error:
        return {"ERROR": result.content[0].text}
    if result.structured_content is not None:
        return result.structured_content
    text = result.content[0].text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


@asynccontextmanager
async def open_session():
    """An initialized MCP client session against URL (the two nested
    `async with` blocks every suite used to open inline)."""
    async with streamable_http_client(URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _ctx_path() -> str:
    data_dir = os.environ.get("AGENTLAND_DATA_DIR") or os.path.join(
        tempfile.gettempdir(), "agentland_e2e"
    )
    return os.path.join(data_dir, "e2e_ctx.json")


def save_ctx(ctx: dict) -> None:
    """Persist the cross-file context (file 01, once its agents + post exist)."""
    missing = [k for k in CTX_KEYS if k not in ctx]
    if missing:
        raise SystemExit(
            f"refusing to save an incomplete e2e context, missing: {missing}"
        )
    os.makedirs(os.path.dirname(_ctx_path()), exist_ok=True)
    with open(_ctx_path(), "w", encoding="utf-8") as fh:
        json.dump({k: ctx[k] for k in CTX_KEYS}, fh)


def load_ctx() -> dict:
    """Read the cross-file context (files 02-04). Refuses with a clear
    message when file 01 has not run first on this server DB."""
    try:
        with open(_ctx_path(), encoding="utf-8") as fh:
            ctx = json.load(fh)
    except (OSError, ValueError):  # domain: fail-closed - no context, refuse order
        ctx = {}
    missing = [k for k in CTX_KEYS if k not in ctx]
    if missing:
        raise SystemExit(
            "e2e context missing - run tests/test_e2e_01_forum.py first "
            "against this server so agents + post exist."
        )
    return ctx
