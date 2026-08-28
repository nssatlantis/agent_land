"""github._core - transport layer for the society's GitHub client.

Everything that talks to api.github.com lives here: the env wiring
(GITHUB_TOKEN / GITHUB_REPO / GITHUB_BASE_BRANCH), the one pooled
httpx.AsyncClient driven by a single dedicated background loop, the JSON
and text request hearts every other module in this package goes through,
the TTL read-caches, and RepoError.

Two hard rules live at the package level (see github/__init__.py): nothing
ever writes to the base branch directly, and every commit/PR carries a
"Citizen: <name> (agent_id=N)" trailer.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import threading
import time
from typing import Any

import httpx

import config  # noqa: E402 - for the GitHub API tunables

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "nssatlantis/agent_land")
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")


# ------------------------------------------------------------------ cache --


class _TTLCache:
    """Minimal in-memory TTL cache keyed by an arbitrary hashable key.
    Stores (timestamp, value) pairs; ``get`` returns the value when fresh,
    ``None`` on miss.  ``set`` accepts any value, including a BaseException:
    ``get`` re-raises a stored exception instead of returning it, so a caller
    that caches a failure can absorb a flaky upstream within the window.
    Only ``open_prs`` currently opts into error caching (guarded by
    ``_CACHE_FAILURES``); the other caches store successes only."""

    __slots__ = ("_store",)

    def __init__(self) -> None:
        self._store: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any, ttl: float) -> Any:
        entry = self._store.get(key)
        if entry is not None and time.monotonic() - entry[0] < ttl:
            value = entry[1]
            if isinstance(value, BaseException):
                raise value
            return value
        return None

    def set(self, key: Any, value: Any) -> None:
        self._store[key] = (time.monotonic(), value)


# Module-level caches -- each function that uses one docs its TTL in the
# docstring.  Only ``open_prs`` caches failures (guarded by ``_CACHE_FAILURES``);
# the other read caches store successes only.  All TTLs are read live from
# config, so a .env change applies without a restart.
_pr_cache = _TTLCache()  # PR reads (get_pr, pr_diff, pr_checks, ...)
_tree_cache = _TTLCache()  # list_tree (long-lived, tree changes rarely)
_open_prs_cache = _TTLCache()  # open_prs (thin wrapper around the same class)
_CACHE_FAILURES = True  # cache RepoError too, for graceful degradation


def clear_cache() -> None:
    """Drop all in-memory GitHub read caches.  Intended for tests that monkey-
    patch ``_request`` -- without clearing, a cached result from one mock
    setup leaks into the next."""
    _pr_cache._store.clear()
    _tree_cache._store.clear()
    _open_prs_cache._store.clear()


def _invalidate_pr(number: int) -> None:
    """Drop every cached read for one PR number after a write (comment, file
    update, close) so callers don't read a stale cached copy within the TTL
    window.  The open-PR list is cleared separately where a write changes
    open/closed state."""
    for key in (
        ("get_pr", number),
        ("pr_diff", number),
        ("pr_files", number),
        ("pr_commits", number),
        ("pr_checks", number),
        ("pr_comments", number),
    ):
        _pr_cache._store.pop(key, None)


class RepoError(Exception):
    """Raised for any rule violation or GitHub API failure. server.py lets
    these surface as normal MCP tool errors so an agent can read the message
    and adapt."""


def _headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent_land-dev",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


# --- shared async client on a dedicated background loop --------------------

_GITHUB_HOST = "api.github.com"
_CONN_IDLE_TIMEOUT = 60  # seconds an idle pooled connection stays alive

_loop: asyncio.AbstractEventLoop | None = None
_client: httpx.AsyncClient | None = None
_io_lock = threading.Lock()


def _bg_loop() -> asyncio.AbstractEventLoop:
    """The single event loop all GitHub I/O runs on, started lazily on first
    use. Sync callers bridge onto it with _sync(); native-await twins share
    its pooled client. One daemon thread, one loop - so connection pooling,
    retry behavior and shutdown live in exactly one place."""
    global _loop
    with _io_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(
                target=_loop.run_forever,
                daemon=True,
                name="agentland-github-io",
            ).start()
        return _loop


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://{_GITHUB_HOST}",
        limits=httpx.Limits(
            max_connections=config.GITHUB_MAX_CONNECTIONS,
            max_keepalive_connections=config.GITHUB_MAX_CONNECTIONS,
            keepalive_expiry=_CONN_IDLE_TIMEOUT,
        ),
        timeout=config.GITHUB_HTTP_TIMEOUT_SECONDS,
    )


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        with _io_lock:
            if _client is None or _client.is_closed:
                _client = _build_client()
    return _client


def _sync(coro: Any) -> Any:
    """Bridge a coroutine onto the background loop and block for its result.
    Legacy sync entry points ride this bridge; native-await callers use
    _on_bg instead. Never call _sync from the background loop's own thread
    - that would deadlock waiting on itself."""
    return asyncio.run_coroutine_threadsafe(coro, _bg_loop()).result()


async def _on_bg(coro: Any) -> Any:
    """Await a coroutine on the background loop from a foreign loop WITHOUT
    blocking the caller's loop. The pooled httpx client is owned exclusively
    by the background loop - its pooled sockets are bound to that loop and
    corrupt if another drives them - so the native twins hop their requests
    over here even when the caller already has a running loop."""
    fut = asyncio.run_coroutine_threadsafe(coro, _bg_loop())
    return await asyncio.wrap_future(fut)


def _shutdown_client() -> None:
    global _client
    try:
        client = _client
        if client is not None and not client.is_closed:
            asyncio.run_coroutine_threadsafe(client.aclose(), _bg_loop()).result(
                timeout=5
            )
    except Exception:
        pass  # interpreter shutdown - best effort only


atexit.register(_shutdown_client)


def _ensure_token() -> None:
    """Raise RepoError when no GITHUB_TOKEN is configured."""
    if not GITHUB_TOKEN:
        raise RepoError(
            "GITHUB_TOKEN is not set. Add it to your environment (see .env.example "
            "and README.md) before using the repo tools."
        )


async def _arequest(
    method: str, path: str, body: dict | None = None, ok_404: bool = False
):
    """Async heart of every GitHub REST call. Raises RepoError on failure;
    returns parsed JSON (or None for an empty 2xx / ok_404 miss).

    httpx reads every response body completely before returning, so the
    keep-alive stream can never fall out of sync - the unread-404-body bug
    class behind proposal #179 is structurally gone. A transport-level
    failure discards just the one bad pooled connection inside httpx while
    the client itself stays healthy, so the retry simply re-runs once on a
    fresh connection (#365's heal contract)."""
    _ensure_token()
    url_path = f"/repos/{GITHUB_REPO}/{path}"
    data = None
    hdrs = _headers()
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    client = _get_client()

    async def _do() -> httpx.Response:
        return await client.request(method, url_path, content=data, headers=hdrs)

    try:
        resp = await _do()
    except (httpx.TransportError, OSError):
        resp = await _do()

    status = resp.status_code
    if 200 <= status < 300:
        raw = resp.content  # fully read by httpx - the stream is always in sync
        if not raw:
            return None
        return json.loads(raw)
    if status == 404 and ok_404:
        return None
    msg = ""
    try:
        msg = resp.json().get("message", "")
    except Exception:
        pass
    detail = f" ({msg})" if msg else ""
    raise RepoError(f"GitHub API {status}{detail} on {method} {path}")


def _request(method: str, path: str, body: dict | None = None, ok_404: bool = False):
    """Sync face of _arequest for callers without a running loop (viewer
    helpers, tests, deploy scripts, composite flows on worker threads).
    Blocks on the background loop's result."""
    return _sync(_arequest(method, path, body=body, ok_404=ok_404))


async def _arequest_text(method: str, path: str, ok_404: bool = False) -> str | None:
    """Async twin of the text reader - GitHub's Actions log download
    (actions/jobs/{id}/logs) is text/plain behind a 302 redirect to a
    signed blob URL, not JSON. Redirects are followed on this path (the
    JSON surface never redirects; only this one needs it). Returns the
    decoded text ('' for an empty body) or None on an ok_404 miss; raises
    RepoError exactly like _arequest otherwise."""
    _ensure_token()
    url_path = f"/repos/{GITHUB_REPO}/{path}"
    hdrs = _headers()

    client = _get_client()

    async def _do() -> httpx.Response:
        return await client.request(
            method,
            url_path,
            headers=hdrs,
            follow_redirects=True,
        )

    try:
        resp = await _do()
    except (httpx.TransportError, OSError):
        resp = await _do()

    status = resp.status_code
    if 200 <= status < 300:
        return resp.text  # fully read by httpx - "" for an empty body
    if status == 404 and ok_404:
        return None
    msg = ""
    try:
        msg = resp.json().get("message", "")
    except Exception:
        pass
    detail = f" ({msg})" if msg else ""
    raise RepoError(f"GitHub API {status}{detail} on {method} {path}")


def _request_text(method: str, path: str, ok_404: bool = False) -> str | None:
    """Sync face of _arequest_text - see _request."""
    return _sync(_arequest_text(method, path, ok_404=ok_404))


def _validate_path(path: str) -> str:
    """Basic hygiene on repo paths: relative, no traversal, no leading slash."""
    path = (path or "").strip()
    if not path:
        raise RepoError("path cannot be empty.")
    if path.startswith("/"):
        raise RepoError(f"path must be relative to the repo root, got {path!r}.")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise RepoError(f"invalid path {path!r}.")
    return path


_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_REF_MAX_LEN = 128


def _validate_ref(ref: str | None) -> str:
    """Restricted ref validation — shared by repo_search, list_tree and
    read_file so the three ref surfaces stay drift-free. Mirrors the
    search validator that guards local `git` invocations (rev-parse/grep):
    only alphanumerics, '-', '_', '.', '/' are allowed, with `..`, `//`,
    `@{`, `~`, `^`, `:`, `?`, `*`, `[` and trailing `/.lock` rejected.
    `None` returns the base branch (the GitHub read default). Raises
    RepoError on violation — callers surface it as a normal tool error."""
    if ref is None:
        return GITHUB_BASE_BRANCH
    ref = (ref or "").strip()
    if not ref:
        raise RepoError("ref cannot be empty.")
    if len(ref) > _REF_MAX_LEN:
        raise RepoError(f"ref too long - keep it under {_REF_MAX_LEN} characters.")
    if not _REF_RE.match(ref):
        raise RepoError(
            f"invalid ref {ref!r} - use branches/tags/commits with alphanumerics, '-', '_', '.', '/' only."
        )
    if ref.startswith(("-", ".", "/")) or ref.endswith(("/", ".", ".lock")):
        raise RepoError(f"invalid ref {ref!r}.")
    if (
        ".." in ref
        or "//" in ref
        or "@{" in ref
        or "~" in ref
        or "^" in ref
        or ":" in ref
        or "?" in ref
        or "*" in ref
        or "[" in ref
    ):
        raise RepoError(f"invalid ref {ref!r}.")
    if ref in (".", ".."):
        raise RepoError(f"invalid ref {ref!r}.")
    return ref
