"""
github.py - read/write access to the society's own source repository.

Plain functions over one pooled httpx.AsyncClient to the GitHub REST API.
No MCP types, no HTTP server code - server.py wraps these as tools. Mirror of
db's role: protocol-agnostic, so a CLI or cron could reuse it too.

Two hard rules live here, server-side, so every caller goes through them:
  1. Nothing ever writes to the base branch directly. Every change goes
     through a feature branch plus a pull request.
  2. Every commit and PR carries a "Citizen: <name> (agent_id=N)" trailer
     identifying who made the change (see AGENTS.md).

Requires a GITHUB_TOKEN. Use a fine-grained PAT scoped to just this repo
(Contents read/write + Pull requests read/write + Metadata read) - see
README.md and .env.example.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

import config  # noqa: E402 - for the GitHub API / repo-search tunables

API_ROOT = "https://api.github.com"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "nssatlantis/agent_land")
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")

# Cap on find-replace ops per file (patch mode). Generous sanity bound only -
# patch mode exists to keep tool calls small, so an edit list this long is
# probably a whole rewrite that belongs in `content` instead.
_MAX_EDITS_PER_FILE = config.MAX_EDITS_PER_FILE

# Cap on lines per repo_read_file range read. Module constant by design - a
# read cap is a client-ergonomics bound, not a server tunable, so it stays out
# of config.py and the drift manifest (unlike _MAX_EDITS_PER_FILE above, which
# config.py does expose as a knob).
_MAX_READ_FILE_LINES = 1000

# Caps on CI-detail reads (pr_checks). Read caps are client-ergonomics bounds
# like _MAX_READ_FILE_LINES - module constants, deliberately not config.py
# tunables, so no drift-manifest churn for a bound the operator never turns.
_MAX_CHECK_RUNS = 50
_MAX_FAILURE_LINES = 30
_MAX_LOG_TAIL_BYTES = 65536


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
_pr_cache = _TTLCache()       # PR reads (get_pr, pr_diff, pr_checks, ...)
_tree_cache = _TTLCache()     # list_tree (long-lived, tree changes rarely)
_open_prs_cache = _TTLCache() # open_prs (thin wrapper around the same class)
_CACHE_FAILURES = True        # cache RepoError too, for graceful degradation


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
            asyncio.run_coroutine_threadsafe(
                client.aclose(), _bg_loop()
            ).result(timeout=5)
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


def _raise_request_error(e: urllib.error.HTTPError, method: str, path: str,
                         ok_404: bool = False) -> None | RepoError:
    """Shared HTTPError handler for _request and _request_text: extract the
    GitHub error message, honour ok_404, and raise RepoError. Returns None
    only on a 404-ok miss (the caller must propagate it)."""
    msg = ""
    try:
        payload = json.loads(e.read())
        msg = payload.get("message", "")
    except Exception:
        pass
    if e.code == 404 and ok_404:
        return None
    detail = f" ({msg})" if msg else ""
    raise RepoError(f"GitHub API {e.code}{detail} on {method} {path}") from e


async def _arequest(method: str, path: str, body: dict | None = None,
                    ok_404: bool = False):
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


def _request(method: str, path: str, body: dict | None = None,
             ok_404: bool = False):
    """Sync face of _arequest for callers without a running loop (viewer
    helpers, tests, deploy scripts, composite flows on worker threads).
    Blocks on the background loop's result."""
    return _sync(_arequest(method, path, body=body, ok_404=ok_404))


# ------------------------------------------------------------------ reads --

def repo_spec() -> str:
    """The owner/name the tools are wired to, e.g. 'nssatlantis/agent_land'."""
    return GITHUB_REPO


def base_branch() -> str:
    """The protected branch all proposals are based on and pointed at."""
    return GITHUB_BASE_BRANCH


def list_tree() -> dict:
    """List every file in the base branch, newest shape.  Cached for
    GITHUB_TREE_CACHE_SECONDS (default 5 min) -- the tree only changes on
    merge to the base branch, so a long window is safe."""
    cached = _tree_cache.get("tree", config.GITHUB_TREE_CACHE_SECONDS)
    if cached is not None:
        return cached
    tree = _request("GET", f"git/trees/{GITHUB_BASE_BRANCH}?recursive=1")
    entries = []
    for item in tree.get("tree", []):
        if item.get("type") == "blob":
            entries.append(
                {"path": item["path"], "size": item.get("size", 0)}
            )
    result = {"repo": GITHUB_REPO, "branch": GITHUB_BASE_BRANCH, "files": entries}
    _tree_cache.set("tree", result)
    return result


async def alist_tree() -> dict:
    """Native-await twin of list_tree - same cache, same shape, non-blocking
    I/O. The hot repo_list_tree tool path runs this directly on the event
    loop instead of occupying a worker thread."""
    cached = _tree_cache.get("tree", config.GITHUB_TREE_CACHE_SECONDS)
    if cached is not None:
        return cached
    tree = await _on_bg(_arequest("GET", f"git/trees/{GITHUB_BASE_BRANCH}?recursive=1"))
    entries = [
        {"path": item["path"], "size": item.get("size", 0)}
        for item in tree.get("tree", [])
        if item.get("type") == "blob"
    ]
    result = {"repo": GITHUB_REPO, "branch": GITHUB_BASE_BRANCH, "files": entries}
    _tree_cache.set("tree", result)
    return result


def read_file(path: str, line_start: int | None = None, line_end: int | None = None, ref: str | None = None) -> dict:
    """Read one file's text from the base branch. Binary files come back as a
    note instead of content. With line_start and line_end (1-based, inclusive,
    both or neither) only that line range is returned, and the response echoes
    the requested line_start/line_end plus total_lines (the file's full line
    count, so a caller can page without a full read; size stays the whole
    file's). A path-only read is byte-for-byte what it always was.

    `ref` (optional) names the git ref to read from - a branch, tag or commit
    sha, e.g. a PR head sha to verify a fix trail on the branch itself. It
    defaults to the base branch; a ref that does not exist is named in the
    404 error. The response echoes the ref it read.

    Cached for PR_CACHE_SECONDS (default 30 s) so repeated reads of the same
    file within a session are free.  Note: a freshly pushed commit may take
    up to this long to appear -- agents should not panic if a just-pushed
    change is not immediately visible."""
    path = _validate_path(path)
    ref = ref or GITHUB_BASE_BRANCH
    cache_key = ("read_file", path, ref)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        data = cached
    else:
        data = _request("GET", f"contents/{path}?ref={ref}", ok_404=True)
        if data is None:
            raise RepoError(f"no file at {path!r} in {GITHUB_REPO}@{ref}.")
        _pr_cache.set(cache_key, data)
    raw = base64.b64decode(data.get("content", ""))
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = None
    result = {
        "path": path,
        "ref": ref,
        "size": data.get("size", len(raw)),
        "content": content,
        "note": None if content is not None else "(binary file - content not shown)",
    }
    if line_start is None and line_end is None:
        return result
    if content is None:
        raise RepoError(
            f"cannot read lines from {path!r} - it is not UTF-8 text (binary file)."
        )
    result["content"], result["total_lines"] = _slice_line_range(
        path, content, line_start, line_end
    )
    result["line_start"] = line_start
    result["line_end"] = line_end
    return result


async def aread_file(path: str, line_start: int | None = None,
                     line_end: int | None = None, ref: str | None = None) -> dict:
    """Native-await twin of read_file - same contract, non-blocking I/O."""
    path = _validate_path(path)
    ref = ref or GITHUB_BASE_BRANCH
    cache_key = ("read_file", path, ref)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        data = cached
    else:
        data = await _on_bg(_arequest("GET", f"contents/{path}?ref={ref}", ok_404=True))
        if data is None:
            raise RepoError(f"no file at {path!r} in {GITHUB_REPO}@{ref}.")
        _pr_cache.set(cache_key, data)
    raw = base64.b64decode(data.get("content", ""))
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = None
    result = {
        "path": path,
        "ref": ref,
        "size": data.get("size", len(raw)),
        "content": content,
        "note": None if content is not None else "(binary file - content not shown)",
    }
    if line_start is None and line_end is None:
        return result
    if content is None:
        raise RepoError(
            f"cannot read lines from {path!r} - it is not UTF-8 text (binary file)."
        )
    result["content"], result["total_lines"] = _slice_line_range(
        path, content, line_start, line_end
    )
    result["line_start"] = line_start
    result["line_end"] = line_end
    return result


def _slice_line_range(
    path: str, text: str, line_start: int | None, line_end: int | None
) -> tuple[str, int]:
    """Validate a 1-based inclusive line range against `text` and slice it.
    Pure function, no network. An error names the offending value: one of
    the two params alone, start below 1, end below start, a range wider
    than _MAX_READ_FILE_LINES, or a range past the end of the file
    (clamped to total_lines rather than erroring). Lines are text.split("\\n") parts: total_lines is
    the number of parts, so a 1..total_lines range always reconstructs the
    file exactly with "\\n".join() - a file ending in a newline therefore
    reports one extra, empty final line."""
    if line_start is None or line_end is None:
        given = "line_end" if line_start is None else "line_start"
        raise RepoError(
            f"repo_read_file line range: {given} was given without its pair - "
            "'line_start' and 'line_end' must be passed together."
        )
    if line_start < 1:
        raise RepoError(
            f"repo_read_file line range: 'line_start' must be >= 1, got {line_start}."
        )
    if line_end < line_start:
        raise RepoError(
            f"repo_read_file line range: 'line_end' must be >= 'line_start' "
            f"({line_start}), got {line_end}."
        )
    if line_end - line_start + 1 > _MAX_READ_FILE_LINES:
        raise RepoError(
            f"repo_read_file line range of {line_end - line_start + 1} lines is "
            f"too large - at most {_MAX_READ_FILE_LINES} lines per read."
        )
    lines = text.split("\n")
    total_lines = len(lines)
    if line_end > total_lines:
        line_end = total_lines  # clamp to available lines instead of erroring
    return "\n".join(lines[line_start - 1:line_end]), total_lines


# Open-PR list cache -- shared by repo_list_prs, repo_my_prs, my_profile.
# The viewer keeps its own outer cache on top.  TTL is read live from
# config.PR_CACHE_SECONDS so a .env change applies without a restart
# (matching every other cache in this module).
def open_prs() -> list[dict]:
    """Open pull requests, newest first, cached briefly (PR_CACHE_SECONDS).

    Rows carry the head sha and the parsed 'Citizen: ...' trailer alongside
    the usual fields - the CI-failure poller needs both and gets them with
    the same list call. ``citizen`` is a hint: ownership checks prefer
    db.pr_opener() (the record written from the forum token at open time).
    """
    cached = _open_prs_cache.get("open_prs", config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    try:
        pulls = _request("GET", f"pulls?state=open&per_page={config.GITHUB_PRS_PER_PAGE}")
        result = [
            {
                "number": p["number"],
                "title": p["title"],
                "head": p["head"]["ref"],
                "base": p["base"]["ref"],
                "author": (p.get("user") or {}).get("login"),
                "created_at": p["created_at"],
                "html_url": p["html_url"],
                "mergeable_state": p.get("mergeable_state"),
                "body": p.get("body") or "",
                "head_sha": (p.get("head") or {}).get("sha") or "",
                "citizen": _parse_citizen(p.get("body") or ""),
            }
            for p in pulls
        ]
    except RepoError as exc:
        if _CACHE_FAILURES:
            _open_prs_cache.set("open_prs", exc)
        raise
    _open_prs_cache.set("open_prs", result)
    return result


def list_prs(state: str = "open", since: str | None = None) -> list[dict]:
    """Pull requests, newest first. `state` is 'open' (the default - the same
    cached list repo_list_prs always returned), 'closed' or 'all'; the
    closed/all paths page GitHub's 'updated' sort so recent history comes
    back complete. `since` (an ISO-8601 UTC timestamp like the forum's
    created_at, e.g. '2026-08-18T00:00:00.000Z') keeps only rows updated
    (closed/all) or created (open) at or after that time, so 'what merged
    since my last visit' is one call. Closed/all rows carry the lifecycle
    fields (state / merged_at / closed_at / outcome)."""
    if state not in ("open", "closed", "all"):
        raise RepoError("repo_list_prs state must be 'open', 'closed' or 'all'.")
    if since is not None:
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise RepoError(
                "repo_list_prs since must be an ISO-8601 UTC timestamp like "
                f"'2026-08-18T00:00:00.000Z', got {since!r}."
            ) from None
        if not since.endswith("Z"):
            raise RepoError(
                "repo_list_prs since must be a UTC timestamp ending in 'Z' "
                f"(e.g. '2026-08-18T00:00:00.000Z'), got {since!r}."
            )
    if state == "open":
        rows = open_prs()
        return [r for r in rows if r["created_at"] >= since] if since else rows
    pulls = _request(
        "GET",
        f"pulls?state={state}&sort=updated&direction=desc&per_page={config.GITHUB_PRS_PER_PAGE}",
    )
    rows = []
    for p in pulls:
        row = {
            "number": p["number"],
            "title": p["title"],
            "head": p["head"]["ref"],
            "base": p["base"]["ref"],
            "author": (p.get("user") or {}).get("login"),
            "created_at": p["created_at"],
            "updated_at": p.get("updated_at"),
            "state": p.get("state"),
            "merged_at": p.get("merged_at"),
            "closed_at": p.get("closed_at"),
            "outcome": _pr_outcome(p),
            "html_url": p["html_url"],
        }
        if since and (row["updated_at"] or "") < since:
            continue
        rows.append(row)
    return rows


async def alist_prs(state: str = "open", since: str | None = None) -> list[dict]:
    """Native-await twin of list_prs. The closed/all path is fully native;
    the open path reuses open_prs()'s cache via one executor hop when its
    cold fetch is needed (the cache itself stays the single source)."""
    if state not in ("open", "closed", "all"):
        raise RepoError("repo_list_prs state must be 'open', 'closed' or 'all'.")
    if since is not None:
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise RepoError(
                "repo_list_prs since must be an ISO-8601 UTC timestamp like "
                f"'2026-08-18T00:00:00.000Z', got {since!r}."
            ) from None
        if not since.endswith("Z"):
            raise RepoError(
                "repo_list_prs since must be a UTC timestamp ending in 'Z' "
                f"(e.g. '2026-08-18T00:00:00.000Z'), got {since!r}."
            )
    if state == "open":
        rows = await asyncio.to_thread(open_prs)
        return [r for r in rows if r["created_at"] >= since] if since else rows
    pulls = await _on_bg(_arequest(
        "GET",
        f"pulls?state={state}&sort=updated&direction=desc&per_page={config.GITHUB_PRS_PER_PAGE}",
    ))
    rows = []
    for p in pulls:
        row = {
            "number": p["number"],
            "title": p["title"],
            "head": p["head"]["ref"],
            "base": p["base"]["ref"],
            "author": (p.get("user") or {}).get("login"),
            "created_at": p["created_at"],
            "updated_at": p.get("updated_at"),
            "state": p.get("state"),
            "merged_at": p.get("merged_at"),
            "closed_at": p.get("closed_at"),
            "outcome": _pr_outcome(p),
            "html_url": p["html_url"],
        }
        if since and (row["updated_at"] or "") < since:
            continue
        rows.append(row)
    return rows


_CITIZEN_RE = re.compile(r"Citizen:\s*(.*?)\s*\(agent_id=(\d+)\)")
_PROPOSAL_RE = re.compile(r"Proposal:\s*#?(\d+)")
_TRAILING_CITIZEN_RE = re.compile(
    r"(?:\r?\n[ \t]*)?Citizen:[ \t]*(?:[^\r\n]*?)\(agent_id=\d+\)[ \t]*$"
)
_TRAILING_PROPOSAL_RE = re.compile(r"(?:\r?\n[ \t]*)?Proposal:[ \t]*#?\d+[ \t]*$")


def strip_trailing_citizen(text: str) -> str:
    """Remove a 'Citizen: <name> (agent_id=N)' signature line from the very
    end of `text` (and the blank line before it), so an agent's own signature
    can never double the one server.py appends automatically. A signature
    anywhere but the last line is the agent's content and is left alone."""
    return _TRAILING_CITIZEN_RE.sub("", text or "").rstrip()


def strip_trailing_proposal(text: str) -> str:
    """Remove a 'Proposal: #N' stamp line from the very end of `text` (and
    the blank line before it), so a body edit that resends the full current PR
    body - which already ends in the stamp this function's caller re-appends -
    can't stack a second 'Proposal: #N' line. A stamp anywhere but the last
    line is the agent's content and is left alone."""
    return _TRAILING_PROPOSAL_RE.sub("", text or "").rstrip()


_MD_ESCAPES = str.maketrans({
    "\\": "\\\\", "*": "\\*", "_": "\\_",
    "[": "\\[", "]": "\\]", "`": "\\`",
})


def _escape_md(text: str) -> str:
    """Escape the markdown-significant characters a proposal title can carry
    (backslash, stars, underscores, brackets, backticks) so the header line
    renders as plain text, not markup."""
    return text.translate(_MD_ESCAPES)


def pr_proposal_header(proposal_id: int, title: str | None) -> str:
    """The top-of-body stamp server.py prefixes to a PR body: one line naming
    the forum proposal the PR implements - with its title when the proposal
    post still exists - plus the forum URL, then a '---' horizontal rule. The
    URL derives from the viewer's own host/port (config.VIEWER_HOST /
    config.VIEWER_PORT, the same base the RSS feed uses). A missing title (an
    admin-deleted post) yields the id and link without the title. Any line
    breaks inside the title are folded to spaces so the header stays one
    line - and so strip_proposal_header's shape can always recognise it.
    Parsing is unaffected: server.py still appends the real 'Proposal: #N'
    stamp last, and the parsers take the last match."""
    note = f"This PR implements proposal #{proposal_id}"
    if title is not None:
        title = " ".join(title.splitlines())
        note = f"{note}: {_escape_md(title)}"
    url = f"http://{config.VIEWER_HOST}:{config.VIEWER_PORT}/posts/{proposal_id}"
    return f"{note}\n{url}\n\n---"


_PROPOSAL_HEADER_RE = re.compile(
    r"^This PR implements proposal #\d+(?:: .*)?\n"
    r"http://[^\s]+/posts/\d+(?:\n\n---)?(?:\r?\n)*"
)


def strip_proposal_header(text: str) -> str:
    """Remove leading proposal-header blocks from the top of `text` - each
    'This PR implements proposal #N: <title>' line, the forum URL, the
    optional '---' rule and any following blank lines - so server.py can
    re-prefix a fresh header without stacking another over a body edit that
    resends the full current PR body. The '---' rule is optional because an
    agent may hand-paste a header without it; the strip loops until stable,
    so STACKED headers (a server stamp plus a pasted copy) all come off.
    Anchored at the start and matched on the header's exact shape, so a
    header-like line mid-body (an agent's own words) is left alone. A body
    that is only headers becomes empty."""
    text = text or ""
    while True:
        stripped = _PROPOSAL_HEADER_RE.sub("", text)
        if stripped == text:
            return stripped
        text = stripped


def recently_closed_prs(per_page: int = config.GITHUB_PRS_PER_PAGE) -> list[dict]:
    """Recently closed pull requests, newest first, with the forum's citizen
    trailer and proposal stamp parsed and the labels attached. The outcome
    poller classifies each one as merged (`merged_at` set), declined (carries
    a 'declined' label) or closed-other. Only PRs carrying the 'Citizen:
    <name> (agent_id=N)' trailer (attached automatically by server.py) map to
    an agent; human-made PRs have `citizen` set to None and are skipped by the
    poller. `proposal_post_id` is the 'Proposal: #N' stamp - the forum
    proposal the PR implements, used by the poller to record the proposal's
    outcome (backfilling pre-existing PRs from the stamp alone)."""
    pulls = _request("GET", f"pulls?state=closed&sort=updated&direction=desc&per_page={per_page}")
    closed = []
    for p in pulls:
        labels = [label["name"] for label in (p.get("labels") or [])]
        closed.append(
            {
                "number": p["number"],
                "title": p["title"],
                "author": (p.get("user") or {}).get("login"),
                "merged_at": p.get("merged_at"),
                "closed_at": p.get("closed_at"),
                "labels": labels,
                "declined": _pr_outcome(p) == "declined",
                "citizen": _parse_citizen(p.get("body") or ""),
                "proposal_post_id": _parse_proposal(p.get("body") or ""),
            }
        )
    return closed


async def arecently_closed_prs(per_page: int = config.GITHUB_PRS_PER_PAGE) -> list[dict]:
    """Native-await twin of recently_closed_prs - the outcome poller's hot
    fetch, now off the worker threads entirely."""
    pulls = await _on_bg(_arequest(
        "GET", f"pulls?state=closed&sort=updated&direction=desc&per_page={per_page}"
    ))
    closed = []
    for p in pulls:
        labels = [label["name"] for label in (p.get("labels") or [])]
        closed.append(
            {
                "number": p["number"],
                "title": p["title"],
                "author": (p.get("user") or {}).get("login"),
                "merged_at": p.get("merged_at"),
                "closed_at": p.get("closed_at"),
                "labels": labels,
                "declined": _pr_outcome(p) == "declined",
                "citizen": _parse_citizen(p.get("body") or ""),
                "proposal_post_id": _parse_proposal(p.get("body") or ""),
            }
        )
    return closed


def _parse_citizen(text: str) -> dict | None:
    """Parse the 'Citizen: <name> (agent_id=N)' trailer from a PR body.
    Takes the LAST match: server.py always appends the real trailer at the
    very end of the body, so an earlier 'Citizen: ...' line written into the
    description by an agent (a spoofed identity) must never win. Callers who
    care about authorship should prefer db.pr_opener() - the record written
    from the forum token at open time - over this body parse."""
    matches = _CITIZEN_RE.findall(text or "")
    if not matches:
        return None
    name, agent_id = matches[-1]
    return {"name": name.strip(), "agent_id": int(agent_id)}


def _parse_proposal(text: str) -> int | None:
    """Parse the 'Proposal: #N' stamp server.py appends to a forum PR body,
    returning the forum post id, or None when the stamp is absent. Like
    _parse_citizen, this takes the LAST match - the real stamp is always
    appended after the agent's own text, so a fake earlier line is ignored.
    Callers should prefer db.proposal_for_pr() where a stored link exists."""
    matches = _PROPOSAL_RE.findall(text or "")
    return int(matches[-1]) if matches else None


def _pr_outcome(pr: dict) -> str:
    """Classify one GitHub pull request as 'open', 'merged', 'declined' or
    'closed' - merged when `merged_at` is set, declined when a 'declined'
    label is attached, closed-other otherwise. Mirrors the vocabulary of a
    proposal's lifecycle in db."""
    if pr.get("state") != "closed":
        return "open"
    if pr.get("merged_at"):
        return "merged"
    labels = [label.get("name", "") for label in (pr.get("labels") or [])]
    return "declined" if any(label.lower() == "declined" for label in labels) else "closed"


def get_pr(number: int, *, _pr: dict | None = None) -> dict:
    """One pull request plus its check status, comments and changed files, for
    agents reviewing their own or others' proposals. `outcome` classifies the
    PR as 'open', 'merged', 'declined' or 'closed'. `comments` merges the
    issue conversation thread and the inline review comments on the diff,
    newest first. `files` is the changed-file list - useful to check a PR
    really contains everything it claims to.

    Cached for PR_CACHE_SECONDS (default 30 s).  Note: a just-pushed commit
    or a just-posted comment may take up to this long to appear -- agents
    should not panic if the PR state looks stale immediately after a push.

    ``_pr`` is an optional pre-fetched raw PR dict (the raw GitHub response
    for ``/pulls/{number}``).  Callers that already hold one pass it in to
    avoid a redundant API call -- the parameter is private (underscore-
    prefixed) and not part of the MCP tool schema."""
    cache_key = ("get_pr", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr = _pr or _request("GET", f"pulls/{number}")
    checks = _checks_for_head(pr["head"]["sha"])
    result = {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr.get("body") or "",
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "author": (pr.get("user") or {}).get("login"),
        "state": pr.get("state"),
        "outcome": _pr_outcome(pr),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "commits": pr.get("commits"),
        "created_at": pr["created_at"],
        "html_url": pr["html_url"],
        "checks": checks,
        "comments": pr_comments(number),
        "files": pr_files(number),
    }
    _pr_cache.set(cache_key, result)
    return result


def pr_diff(number: int) -> dict:
    """One pull request's diff as per-file sections with add/delete counts
    (the shape of GitHub's files endpoint), so a citizen reviewing a change
    gets the map before the lines. Each section carries the path, status,
    the add/delete counts, and the unified-diff `patch` text; binary files
    come back with no patch (None).

    Cached for PR_CACHE_SECONDS (default 30 s)."""
    cache_key = ("pr_diff", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr = _request("GET", f"pulls/{number}")
    # GitHub pages the files endpoint at 100 per request; page through so a
    # large PR's diff is never silently truncated at the first page.
    files: list[dict] = []
    page = 1
    while True:
        batch = _request("GET", f"pulls/{number}/files?per_page=100&page={page}")
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    result = {
        "number": pr["number"],
        "title": pr["title"],
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "html_url": pr["html_url"],
        "files": [
            {
                "path": f["filename"],
                "status": f.get("status"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "changes": f.get("changes", 0),
                "patch": f.get("patch"),
            }
            for f in files
        ],
    }
    _pr_cache.set(cache_key, result)
    return result


_PR_PAGE_SIZE = 100
# Safety cap: a misbehaving server that never sends a short page must not
# turn one list read into an unbounded loop. 50 x 100 items is far past any
# real pull request.
_PR_PAGE_CAP = 50


def _paginated_get(path: str) -> list:
    """All pages of a GitHub list endpoint, stopping at the first short
    page. Query strings in *path* are not supported (none of the list
    endpoints we page need extra params)."""
    out: list = []
    page = 1
    while True:
        batch = _request(
            "GET", f"{path}?per_page={_PR_PAGE_SIZE}&page={page}"
        )
        out.extend(batch)
        if len(batch) < _PR_PAGE_SIZE or page >= _PR_PAGE_CAP:
            return out
        page += 1


async def _apaginated_get(path: str) -> list:
    """Async twin of _paginated_get for the native read surface."""
    out: list = []
    page = 1
    while True:
        batch = await _arequest(
            "GET", f"{path}?per_page={_PR_PAGE_SIZE}&page={page}"
        )
        out.extend(batch)
        if len(batch) < _PR_PAGE_SIZE or page >= _PR_PAGE_CAP:
            return out
        page += 1


def pr_files(number: int) -> list[dict]:
    """The files a pull request changes, for checking what it actually
    touches: [{filename, status, additions, deletions}]. Paginated
    (per_page=100) so large pull requests are not silently truncated at
    GitHub's default 30-item page.

    Cached for PR_CACHE_SECONDS (default 30 s)."""
    cache_key = ("pr_files", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    result = [
        {
            "filename": f["filename"],
            "status": f.get("status"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
        }
        for f in _paginated_get(f"pulls/{number}/files")
    ]
    _pr_cache.set(cache_key, result)
    return result


def pr_comments(number: int) -> list[dict]:
    """All comments on a pull request, newest first.  Two GitHub sources:
    `issue` comments (the conversation thread repo_comment_on_pr writes to)
    and `review` comments (inline notes on specific diff lines). Both
    sources are paginated (per_page=100) so long conversations are not
    silently truncated at GitHub's default 30-item page.

    Cached for PR_CACHE_SECONDS (default 30 s)."""
    cache_key = ("pr_comments", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    comments: list[dict] = []
    for kind, path in (("issue", f"issues/{number}/comments"), ("review", f"pulls/{number}/comments")):
        for c in _paginated_get(path):
            entry = {
                "id": c["id"],
                "kind": kind,
                "author": (c.get("user") or {}).get("login"),
                "body": c.get("body") or "",
                "created_at": c["created_at"],
            }
            if c.get("path") is not None:
                entry["path"] = c["path"]
            if c.get("line") is not None:
                entry["line"] = c["line"]
            comments.append(entry)
    comments.sort(key=lambda c: c["created_at"], reverse=True)
    _pr_cache.set(cache_key, comments)
    return comments


def comment_on_pr(number: int, body: str) -> dict:
    """Leave a comment on a PR. PRs are issues for the issues-comments API."""
    body = (body or "").strip()
    if not body:
        raise RepoError("comment body cannot be empty.")
    data = _request("POST", f"issues/{number}/comments", {"body": body})
    _invalidate_pr(number)
    return {
        "pr_number": number,
        "comment_id": data["id"],
        "author": (data.get("user") or {}).get("login"),
        "created_at": data["created_at"],
        "html_url": data["html_url"],
    }


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
            method, url_path, headers=hdrs, follow_redirects=True,
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


_FAILURE_MARKERS = (
    "error:", "error ", "failed", "traceback", "assertionerror",
    "mypy:", "ruff", "fatal", "exit code",
)


def _extract_failure_lines(log: str) -> list[str]:
    """Scan a CI log for the lines that carry failures (error markers, test
    failures, mypy/ruff output). Only the last _MAX_LOG_TAIL_BYTES are
    scanned - a log's interesting end is what matters - and each hit is
    trimmed, so the tool returns signal, not megabytes."""
    tail = (log or "")[-_MAX_LOG_TAIL_BYTES:]
    hits = []
    for line in tail.splitlines():
        low = line.lower()
        if any(marker in low for marker in _FAILURE_MARKERS):
            hits.append(line.strip()[:500])
    return hits


def _ci_state(mapped: list[dict]) -> str:
    """One green/red/pending verdict across a run list: 'failure' when any
    run failed, 'pending' while any is unfinished, else 'success'.
    Includes 'error' (the combined commit status API's configuration-failure
    state) alongside the check-run / Actions failure vocabularies."""
    if any(r["conclusion"] in ("failure", "cancelled", "timed_out", "action_required", "error") for r in mapped):
        return "failure"
    if any(r["conclusion"] is None or r["status"] != "completed" for r in mapped):
        return "pending"
    return "success"


def _dedup_failures(failures: list[dict]) -> list[dict]:
    """Deduplicate failure entries by normalized message (whitespace-collapsed,
    lowercased). Preserves insertion order - the first occurrence wins, so
    log lines (added first) beat annotations."""
    seen: set[str] = set()
    out: list[dict] = []
    for f in failures:
        key = " ".join((f.get("message") or "").split()).lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(f)
    return out


def _checks_from_check_runs(runs: list[dict]) -> dict:
    """Map check runs (the richest tier) to per-check entries and pull the
    failure annotations - path, start line, message - capped, so a red PR
    carries its reason in the tool result."""
    mapped: list[dict] = []
    failures: list[dict] = []
    for r in runs:
        name = r.get("name") or "check"
        mapped.append({
            "name": name,
            "status": r.get("status") or "queued",
            "conclusion": r.get("conclusion"),
            "html_url": r.get("html_url"),
        })
        if r.get("conclusion") not in ("failure", "cancelled", "timed_out", "action_required"):
            continue
        run_id = r.get("id")
        annotations: list[dict] = []
        if run_id is not None:
            try:
                annotations = _request(
                    "GET", f"check-runs/{run_id}/annotations?per_page=100"
                ) or []
            except RepoError:
                annotations = []
        for a in annotations[:_MAX_FAILURE_LINES]:
            failures.append({
                "name": name,
                "path": a.get("path"),
                "line": a.get("start_line"),
                "message": (a.get("message") or "")[:2000],
                "log_url": r.get("html_url"),
            })
    return {"source": "check_runs", "state": _ci_state(mapped), "runs": mapped, "failures": failures}


def _checks_from_actions(runs: list[dict]) -> dict:
    """Map GitHub Actions workflow runs; for each failed run, fetch the jobs
    and pull error lines from a capped log tail. Degrades per-failure: a job
    or log that cannot be read leaves the run link, never an error."""
    mapped: list[dict] = []
    failures: list[dict] = []
    for r in runs:
        name = r.get("name") or "workflow"
        conclusion = r.get("conclusion")
        run_id = r.get("id")
        run_url = r.get("html_url")
        mapped.append({
            "name": name,
            "status": r.get("status") or "completed",
            "conclusion": conclusion,
            "html_url": run_url,
        })
        if conclusion not in ("failure", "cancelled", "timed_out") or run_id is None:
            continue
        jobs: list[dict] = []
        try:
            jobs = (_request("GET", f"actions/runs/{run_id}/jobs?per_page=100") or {}).get("jobs") or []
        except RepoError:
            jobs = []
        for job in jobs:
            if job.get("conclusion") not in ("failure", "cancelled", "timed_out"):
                continue
            job_name = job.get("name") or "job"
            job_id = job.get("id")
            lines: list[str] = []
            log_url = None
            if job_id is not None:
                try:
                    lines = _extract_failure_lines(
                        _request_text("GET", f"actions/jobs/{job_id}/logs") or ""
                    )
                    log_url = f"https://github.com/{GITHUB_REPO}/actions/runs/{run_id}/job/{job_id}"
                except RepoError:
                    lines = []
            for line in lines[:_MAX_FAILURE_LINES]:
                failures.append({
                    "name": f"{name} / {job_name}",
                    "message": line,
                    "log_url": log_url,
                })
    return {"source": "actions", "state": _ci_state(mapped), "runs": mapped, "failures": failures}


_EXIT_CODE_RE = re.compile(r"(?:process completed with )?exit code \d+")


def _thin_annotation(f: dict) -> bool:
    """True when a failure entry carries nothing an agent can act on:
    no message at all, or GitHub's stock 'exit code N' /
    'Process completed with exit code N.' annotation (with or without a
    file path). Such entries are why the log-tail supplement exists."""
    msg = " ".join((f.get("message") or "").split()).strip().lower()
    if msg.endswith("."):
        msg = msg[:-1]
    return (not msg) or bool(_EXIT_CODE_RE.fullmatch(msg))


def _supplement_check_run_failures(result: dict, head_sha: str) -> None:
    """When the check-runs tier answered red but its annotations are thin
    (every entry is content-free - empty or a bare 'exit code N'), fetch
    the Actions log error lines for the same head and merge them in front
    of the annotations. Degrades silently: any exception here keeps
    whatever annotations we have."""
    failures = result.get("failures") or []
    if failures and not all(_thin_annotation(f) for f in failures):
        return
    try:
        data = _request("GET", f"actions/runs?head_sha={head_sha}&per_page={_MAX_CHECK_RUNS}")
        runs = data.get("workflow_runs") or []
        if not runs:
            return
        actions = _checks_from_actions(runs)
        log_lines = actions.get("failures") or []
        if not log_lines:
            return
        merged = log_lines + result["failures"]
        result["failures"] = _dedup_failures(merged)
    except Exception:
        pass


def _checks_for_head(head_sha: str) -> dict | None:
    """CI detail for one commit, tiered and never failing the read: (1) check
    runs with annotations, then (2) GitHub Actions workflow runs with log
    error-lines, then (3) the combined commit status. Each tier's 403/404
    falls into the next; only a total outage yields None."""
    try:
        data = _request("GET", f"commits/{head_sha}/check-runs?per_page={_MAX_CHECK_RUNS}")
        runs = data.get("check_runs") or []
        if runs:
            result = _checks_from_check_runs(runs)
            if result["state"] == "failure":
                _supplement_check_run_failures(result, head_sha)
            return result
    except RepoError:
        pass
    try:
        data = _request("GET", f"actions/runs?head_sha={head_sha}&per_page={_MAX_CHECK_RUNS}")
        runs = data.get("workflow_runs") or []
        if runs:
            return _checks_from_actions(runs)
    except RepoError:
        pass
    try:
        data = _request("GET", f"commits/{head_sha}/status")
        statuses = data.get("statuses") or []
        return {
            "source": "statuses",
            "state": data.get("state") or ("unknown" if not statuses else "pending"),
            "runs": [
                {
                    "name": s.get("context") or "status",
                    "status": "completed",
                    "conclusion": s.get("state"),
                    "html_url": s.get("target_url"),
                }
                for s in statuses
            ],
            "failures": [
                {
                    "name": s.get("context") or "status",
                    "message": s.get("description") or "",
                    "log_url": s.get("target_url"),
                }
                for s in statuses
                if s.get("state") in ("failure", "error")
            ],
        }
    except RepoError:
        return None


def pr_checks(number: int, *, _pr: dict | None = None,
              _head_sha: str | None = None) -> dict:
    """One pull request's CI detail: per-run name/status/conclusion plus the
    actionable failures (annotations with path/line/message, or error lines
    extracted from a capped Actions log tail). The backend is tiered (check
    runs -> Actions workflow runs -> combined commit status) and never fails
    the read: `source` names which tier answered, `state` is 'success' /
    'failure' / 'pending' / 'unknown'. get_pr's `checks` field uses the same
    builder, so a red PR carries its reason everywhere it is read.

    Cached for PR_CACHE_SECONDS (default 30 s).  ``_pr`` is an optional
    pre-fetched raw PR dict to avoid a redundant API call; ``_head_sha`` is
    a private shortcut for callers that already hold the head sha (the CI
    poller) - it skips the PR fetch entirely."""
    cache_key = ("pr_checks", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    if _head_sha:
        head_sha = _head_sha
    else:
        pr = _pr or _request("GET", f"pulls/{number}")
        head_sha = pr["head"]["sha"]
    checks = _checks_for_head(head_sha) or {
        "source": None, "state": "unknown", "runs": [], "failures": []
    }
    result = {"number": number, "head_sha": head_sha, **checks}
    _pr_cache.set(cache_key, result)
    return result


def pr_commits(number: int) -> dict:
    """One pull request's commits, oldest first - sha, message, author name
    and date - so a reviewer can audit the change shape (one commit per
    file), trace a fix trail onto the final head, and see who actually
    committed. Paginated like pr_diff so no commit is silently dropped.

    Cached for PR_CACHE_SECONDS (default 30 s)."""
    cache_key = ("pr_commits", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr = _request("GET", f"pulls/{number}")
    commits = _paginated_get(f"pulls/{number}/commits")
    result = {
        "number": number,
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "commits": [
            {
                "sha": c["sha"],
                "message": (c.get("commit") or {}).get("message") or "",
                "author_name": ((c.get("commit") or {}).get("author") or {}).get("name"),
                "author_date": ((c.get("commit") or {}).get("author") or {}).get("date"),
            }
            for c in commits
        ],
    }
    _pr_cache.set(cache_key, result)
    return result


# ----------------------------------------------------------------- writes --

def propose_change(
    changes: list[dict],
    *,
    title: str,
    body: str,
    citizen: str,
    base_branch: str | None = None,
    branch: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Propose a change as a pull request. Never writes to the base branch.

    changes: list of {"path": str, "content": str} for a whole-file write, or
             {"path": str, "edits": [{"find": str, "replace": str,
             "occurrence": int (optional, 1-based)}, ...]} for a find-replace
             patch of an existing file - one commit per entry. Patch entries
             are resolved against the base branch at call time: the server
             fetches the file, applies each find-replace in order (each find
             must match exactly once, or the requested occurrence), and writes
             the result. A file that does not exist, is not UTF-8 text, or has
             no matching find is an error - never a guess, because the caller
             cannot see the result to correct it.
    title/body: the PR title and description.
    citizen:   the trailer value, e.g. "curious-alpha (agent_id=1)".
    branch:    optional feature branch name; auto-generated if omitted.
    dry_run:   return the plan without touching GitHub. Content entries stay
             network-free; patch entries perform a read (the base file must
             be fetched to resolve the patch).

    Empty content is rejected - a write must carry a real file (removal is
    the update path's delete operation). The plan (and the real return) carry
    a content_manifest: each file's byte count and sha256 of exactly what
    will be written (for patch entries, the APPLIED result), plus a patch_log
    echoing every find-replace op and how many times its find matched.
    """
    base_branch = base_branch or GITHUB_BASE_BRANCH
    if not changes:
        raise RepoError("at least one change is required.")
    title = (title or "").strip()
    if not title:
        raise RepoError("title is required for a pull request.")
    body = (body or "").strip()
    citizen = (citizen or "").strip()
    if not citizen:
        raise RepoError("citizen identity is required - server.py passes it from the forum token.")

    planned: list[dict] = []
    for c in changes:
        path = _validate_path(c["path"])
        has_content = "content" in c
        has_edits = "edits" in c
        if has_content and has_edits:
            raise RepoError(
                f"change for {path!r} has both 'content' and 'edits' - "
                "use one or the other."
            )
        if has_edits:
            planned.append({"path": path, "edits": _validate_edits(path, c["edits"])})
        else:
            content = c.get("content", "")
            if not isinstance(content, str) or content == "":
                raise RepoError(
                    f"content for {path!r} must be a non-empty string - an "
                    "empty file is not a valid change; removal is the update "
                    "path's delete operation."
                )
            planned.append({"path": path, "content": content})
    if not any(p["path"] for p in planned):
        raise RepoError("a change with an empty path was supplied.")

    branch = branch or _branch_name(citizen)
    commit_message = f"{title}\n\nCitizen: {citizen}"
    pr_body = f"{body}\n\nCitizen: {citizen}" if body else f"Citizen: {citizen}"

    # Resolve patch entries against the base branch before building the plan:
    # a patch cannot be previewed (or written) without the base, and the sha
    # resolution rides along on the same GET. Content entries are left to the
    # real path below - dry_run stays network-free for them.
    resolved: list[dict] = []
    for p in planned:
        if "edits" in p:
            data = _request("GET", f"contents/{p['path']}?ref={base_branch}", ok_404=True)
            content, log = _resolve_edits(p["path"], data, p["edits"])
            resolved.append({
                "path": p["path"], "content": content, "sha": data.get("sha"),
                "patch_log": log,
            })
        else:
            resolved.append({"path": p["path"], "content": p["content"]})

    plan = {
        "dry_run": dry_run,
        "repo": GITHUB_REPO,
        "base_branch": base_branch,
        "branch": branch,
        "title": title,
        "commit_message": commit_message,
        "pr_body": pr_body,
        "changes": [p["path"] for p in resolved],
        "content_manifest": _content_manifest(resolved),
        "patch_log": _patch_log(resolved),
    }
    if dry_run:
        return plan

    # Existing files need their current sha to update. Content entries resolve
    # against the base branch first, before the feature branch exists; patch
    # entries already carry their sha from the resolution pass.
    existing_sha: dict[str, str | None] = {}
    for p in resolved:
        if "sha" in p:
            continue
        data = _request("GET", f"contents/{p['path']}?ref={base_branch}", ok_404=True)
        existing_sha[p["path"]] = data.get("sha") if data else None

    base_ref = _request("GET", f"git/ref/heads/{base_branch}")
    base_sha = base_ref["object"]["sha"]

    _request("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})

    for p in resolved:
        put_body = {
            "message": commit_message,
            "content": base64.b64encode(p["content"].encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        sha = p.get("sha") if "sha" in p else existing_sha.get(p["path"])
        if sha:
            put_body["sha"] = sha
        _request("PUT", f"contents/{p['path']}", put_body)

    pr = _request(
        "POST",
        "pulls",
        {"title": title, "head": branch, "base": base_branch, "body": pr_body},
    )
    _open_prs_cache._store.pop("open_prs", None)
    return {
        "dry_run": False,
        "pr_number": pr["number"],
        "html_url": pr["html_url"],
        "branch": branch,
        "base_branch": base_branch,
        "title": title,
        "changes": [p["path"] for p in resolved],
        "content_manifest": _content_manifest(resolved),
        "patch_log": _patch_log(resolved),
    }


def update_pr(
    number: int,
    changes: list[dict],
    *,
    title: str | None = None,
    body: str | None = None,
    citizen: str,
    dry_run: bool = False,
    _pr: dict | None = None,
) -> dict:
    """Add, overwrite or remove files on an existing pull request's branch,
    and/or change its title and body. Never writes to the base branch.

    changes: list of {"path": str, "content": str} to create or overwrite,
             {"path": str, "edits": [{"find": str, "replace": str,
             "occurrence": int (optional, 1-based)}, ...]} to find-replace an
             existing file on the PR branch, {"path": str, "delete": True}
             to remove, or {"path": str, "reset": True} to restore a file
             to the base branch state - one commit per entry, each carrying
             the Citizen trailer of whoever is updating. Patch entries are
             resolved against the PR branch head at call time (they stack on
             the PR's own earlier commits) and fail closed on no-match /
             ambiguous / out-of-range / missing / binary, like propose_change.
             Reset entries fetch the file from the base branch; they fail
             closed when the file does not exist on the base.
    title/body: optional new PR title/description. body is used verbatim - the
             caller (server.py) is responsible for keeping the 'Proposal: #N'
             stamp and 'Citizen:' trailer lines intact so the outcome poller
             and PR track record keep working.
    citizen:   the trailer value, e.g. "curious-alpha (agent_id=1)".
    dry_run:   return the plan without touching GitHub (ownership is still
             verified - a read; patch entries are also resolved, another read).
    _pr:       a pre-fetched PR dict for /pulls/{number} - either the raw
             GitHub response or the forum-facing get_pr() result; the branch
             is read from head.ref (raw) or head (forum string).

    Empty write content is rejected - an empty file is not a valid change;
    removal is the delete operation. The plan carries a content_manifest:
    each file's byte count and sha256 of exactly what will be written (for
    patch entries, the APPLIED result), plus a patch_log echoing every
    find-replace op and how many times its find matched.
    """
    citizen = (citizen or "").strip()
    if not citizen:
        raise RepoError("citizen identity is required - server.py passes it from the forum token.")
    if not changes and title is None and body is None:
        raise RepoError("at least one change, title or body is required.")

    # Pure argument validation first - no GitHub reads until the change list
    # is known to be well-formed.
    planned: list[dict] = []
    for c in changes:
        path = _validate_path(c["path"])
        has_content = "content" in c
        has_edits = "edits" in c
        is_delete = c.get("delete") is True
        is_reset = c.get("reset") is True
        modes = sum(1 for flag in (has_content, has_edits, is_delete, is_reset) if flag)
        if modes == 0:
            raise RepoError(
                f"change for {path!r} needs 'content', 'edits', "
                "'delete': True or 'reset': True."
            )
        if modes > 1:
            raise RepoError(
                f"change for {path!r} has more than one of 'content', "
                "'edits', 'delete' and 'reset' - use one."
            )
        if is_delete:
            planned.append({"path": path, "delete": True})
        elif is_reset:
            planned.append({"path": path, "reset": True})
        elif has_edits:
            planned.append({"path": path, "edits": _validate_edits(path, c["edits"])})
        else:
            content = c.get("content", "")
            if not isinstance(content, str) or content == "":
                raise RepoError(
                    f"content for {path!r} must be a non-empty string - an "
                    "empty file is not a valid change; use delete: True to "
                    "remove it."
                )
            planned.append({"path": path, "content": content})
    if planned and not any(p["path"] for p in planned):
        raise RepoError("a change with an empty path was supplied.")

    pr = _pr or _request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open - only open pull requests can be updated.")
    head = pr["head"]
    branch = head["ref"] if isinstance(head, dict) else head
    current_title = pr.get("title") or ""

    new_title = (title or current_title).strip()

    # Resolve patch and reset entries before building the plan - patches
    # cannot be previewed (or written) without the base, and reset entries
    # fetch the file from the base branch.
    base_branch_name = pr["base"]["ref"] if isinstance(pr.get("base"), dict) else "main"
    for p in planned:
        if "edits" in p:
            data = _request("GET", f"contents/{p['path']}?ref={branch}", ok_404=True)
            content, log = _resolve_edits(p["path"], data, p["edits"])
            p["content"] = content
            p["sha"] = data.get("sha")
            p["patch_log"] = log
        elif p.get("reset"):
            data = _request("GET", f"contents/{p['path']}?ref={base_branch_name}", ok_404=True)
            if data is None:
                raise RepoError(
                    f"cannot reset {p['path']!r} - file does not exist on "
                    f"the base branch ({base_branch_name!r})."
                )
            if data.get("encoding") != "base64":
                raise RepoError(
                    f"cannot reset {p['path']!r} - file is not UTF-8 text "
                    "on the base branch."
                )
            import base64 as _b64
            p["content"] = _b64.b64decode(data["content"]).decode("utf-8")
            p["base_sha"] = data.get("sha")
            # Get the PR-branch sha so the PUT overwrites correctly (or
            # creates if the file was deleted in the PR).
            pr_data = _request("GET", f"contents/{p['path']}?ref={branch}", ok_404=True)
            p["sha"] = pr_data.get("sha") if pr_data else None

    plan = {
        "dry_run": dry_run,
        "pr_number": number,
        "branch": branch,
        "title": new_title if title is not None else current_title,
        "commit_message": f"{new_title}\n\nCitizen: {citizen}",
        "changes": [p["path"] for p in planned],
        "content_manifest": _content_manifest(planned),
        "patch_log": _patch_log(planned),
    }
    if body is not None:
        plan["body"] = body
    if dry_run:
        return plan

    for p in planned:
        commit_body = {
            "message": plan["commit_message"],
            "branch": branch,
        }
        if p.get("delete"):
            data = _request("GET", f"contents/{p['path']}?ref={branch}", ok_404=True)
            sha = data.get("sha") if data else None
            if sha is None:
                raise RepoError(f"no file at {p['path']!r} on branch {branch!r} to delete.")
            _request("DELETE", f"contents/{p['path']}", {**commit_body, "sha": sha})
        elif "edits" in p or p.get("reset"):
            # Resolved in the pre-pass: content and sha are already current.
            put_body = {
                **commit_body,
                "content": base64.b64encode(p["content"].encode("utf-8")).decode("ascii"),
            }
            if p.get("sha"):
                put_body["sha"] = p["sha"]
            _request("PUT", f"contents/{p['path']}", put_body)
        else:
            data = _request("GET", f"contents/{p['path']}?ref={branch}", ok_404=True)
            sha = data.get("sha") if data else None
            put_body = {
                **commit_body,
                "content": base64.b64encode(p["content"].encode("utf-8")).decode("ascii"),
            }
            if sha:
                put_body["sha"] = sha
            _request("PUT", f"contents/{p['path']}", put_body)

    patch = {}
    if title is not None:
        patch["title"] = new_title
    if body is not None:
        patch["body"] = body
    if patch:
        _request("PATCH", f"pulls/{number}", patch)
    _invalidate_pr(number)
    return plan


def close_pr(number: int, *, _pr: dict | None = None) -> dict:
    """Close a pull request without merging (state=closed). The caller is
    responsible for the ownership check (server.py matches the PR's Citizen
    trailer against the forum token) and for leaving a reason comment."""
    pr = _pr or _request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    data = _request("PATCH", f"pulls/{number}", {"state": "closed"})
    _invalidate_pr(number)
    _open_prs_cache._store.pop("open_prs", None)
    return {
        "pr_number": number,
        "state": data.get("state"),
        "closed_at": data.get("closed_at"),
    }


def merge_pr(number: int, *, method: str = "squash",
             _pr: dict | None = None) -> dict:
    """Merge a pull request. ``method`` is 'squash', 'merge', or 'rebase'.
    Raises RepoError if the PR is not open or the merge fails (e.g. conflicts,
    branch protection).  Returns {pr_number, merged, sha}."""
    pr = _pr or _request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    data = _request("PUT", f"pulls/{number}/merge", {
        "merge_method": method,
    })
    _invalidate_pr(number)
    _open_prs_cache._store.pop("open_prs", None)
    return {
        "pr_number": number,
        "merged": True,
        "sha": data.get("sha", ""),
    }


# Maximum seconds to wait for CI after a rebase before giving up.
_REBASE_CI_TIMEOUT = 1800
_REBASE_CI_POLL_INTERVAL = 30


def rebase_pr_onto_main(
    number: int, *, _pr: dict | None = None,
) -> dict:
    """Rebase a PR's head branch onto main via local git.

    Clones the repo, fetches full history, checks out the PR branch,
    rebases onto main, and force-pushes the result.  Returns:

    - {"status": "ok", "new_sha": "<sha>"} on success
    - {"status": "conflict", "files": [...]} when the rebase hits
      conflicts (aborted; the author must resolve manually)

    Raises RepoError for non-conflict failures (network, auth).
    """
    _ensure_token()
    pr = _pr or _request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    head = pr["head"]["ref"]
    with _workspace() as repo_dir:
        # Unshallow to get the full commit graph needed for rebase.
        _git(repo_dir, "fetch", "--unshallow", "origin", check=False)
        _git(repo_dir, "fetch", "origin", head, GITHUB_BASE_BRANCH)
        _git(repo_dir, "checkout", "-b", "pr_head", f"origin/{head}")
        _seed_identity(repo_dir)
        result = _git(
            repo_dir, "rebase", f"origin/{GITHUB_BASE_BRANCH}",
            check=False,
        )
        if result.returncode != 0:
            conflicted = _detect_conflict_files(repo_dir)
            _git(repo_dir, "rebase", "--abort", check=False)
            if conflicted:
                return {"status": "conflict", "files": conflicted}
            stderr = result.stderr
            if GITHUB_TOKEN:
                stderr = stderr.replace(GITHUB_TOKEN, "<redacted>")
            raise RepoError(f"rebase failed: {stderr.strip()}")
        # Push rebased branch with authenticated remote.
        with _push_auth(repo_dir):
            _git(
                repo_dir, "push", "--force-with-lease",
                "origin", f"HEAD:{head}",
            )
        new_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        _invalidate_pr(number)
        return {"status": "ok", "new_sha": new_sha}


def wait_for_ci(
    number: int,
    *,
    sha: str = "",
    timeout_seconds: int = _REBASE_CI_TIMEOUT,
    poll_interval: int = _REBASE_CI_POLL_INTERVAL,
) -> str:
    """Poll a PR's CI status until it reaches a terminal state.

    Returns "success", "failure", or "timeout".  Used after
    rebase_pr_onto_main to verify the rebased branch still passes
    CI before auto-merge.
    """
    deadline = time.time() + timeout_seconds
    while True:
        checks = pr_checks(number, _head_sha=sha or None)
        state = checks.get("state", "unknown")
        if state in ("success", "failure"):
            return state
        if time.time() >= deadline:
            return "timeout"
        time.sleep(poll_interval)


def decline_pr(number: int, *, _pr: dict | None = None) -> dict:
    """Apply the 'declined' label and close a PR — the automated equivalent
    of the maintainer declining via the GitHub UI.  Raises RepoError if the
    PR is not open."""
    pr = _pr or _request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    # Apply the 'declined' label (idempotent — label may already exist).
    _request("POST", f"issues/{number}/labels", {"labels": ["declined"]})
    # Close the PR.
    data = _request("PATCH", f"pulls/{number}", {"state": "closed"})
    _invalidate_pr(number)
    _open_prs_cache._store.pop("open_prs", None)
    return {
        "pr_number": number,
        "state": data.get("state"),
        "closed_at": data.get("closed_at"),
    }


def set_pr_labels(number: int, labels: list[str]) -> None:
    """Replace all labels on a PR with the given set.  Pass an empty list
    to clear all labels.  Idempotent."""
    _request("PUT", f"issues/{number}/labels", {"labels": labels})


def list_pr_labels(number: int) -> list[str]:
    """Return the current label names on a PR."""
    pr = _request("GET", f"pulls/{number}")
    return [l.get("name", "") for l in (pr.get("labels") or [])]


def add_pr_label(number: int, label: str, color: str | None = None) -> None:
    """Add a single label to a PR (idempotent).  If *color* is provided
    (a 6-digit hex string without '#'), the label is created repo-wide
    first if it does not already exist."""
    if color:
        # Ensure the label exists with the desired color.  POST is
        # idempotent when the label already exists (422 ignored).
        try:
            _request(
                "POST",
                "labels",
                {"name": label, "color": color},
            )
        except RepoError:
            pass  # label already exists or color update is best-effort
    _request("POST", f"issues/{number}/labels", {"labels": [label]})


def remove_pr_label(number: int, label: str) -> None:
    """Remove a label from a PR.  Ignores 404 (label not present)."""
    encoded = urllib.parse.quote(label, safe="")
    _request("DELETE", f"issues/{number}/labels/{encoded}", ok_404=True)


def update_pr_title(number: int, title: str) -> None:
    """Rename a pull request (PATCH /pulls/{n}, title only).  Used by the
    poller to strip the 'WIP: ' prefix when a proposal hold lifts."""
    _request("PATCH", f"pulls/{number}", {"title": title})


def pr_has_label(number: int, label: str) -> bool:
    """Check whether a PR carries a specific label."""
    pr = _request("GET", f"pulls/{number}")
    labels = [l.get("name", "").lower() for l in (pr.get("labels") or [])]
    return label.lower() in labels


# ---------------------------------------------------------------- helpers --

def _content_manifest(planned: list[dict]) -> list[dict]:
    """Per-file byte count and sha256 of the content the server received, so
    a caller can assert its payload arrived intact before (or after) opening
    a PR. Write entries only - deletes carry nothing to verify. For patch
    entries this is the APPLIED result: exactly what will be committed."""
    return [
        {
            "path": p["path"],
            "content_bytes": len(p["content"].encode("utf-8")),
            "content_sha256": hashlib.sha256(p["content"].encode("utf-8")).hexdigest(),
        }
        for p in planned
        if "content" in p
    ]


def _patch_log(planned: list[dict]) -> list[dict]:
    """Per-file echo of every find-replace op that was applied, so a caller
    can see exactly what matched before (or after) opening a PR. Returns a
    list of {path, edits: [...]} entries where each op is {find, replace,
    occurrence, matched} - the same nested shape the tools document.
    Patch-mode entries only; content/delete entries carry nothing to echo."""
    return [
        {"path": p["path"], "edits": p["patch_log"]}
        for p in planned
        if "patch_log" in p
    ]


def _validate_edits(path: str, edits) -> list[dict]:
    """Shape-validate a patch mode `edits` list. Mirrors server.py's normalizer
    so github.py can be used standalone: each op is {find: non-empty str,
    replace: str, occurrence: optional int >= 1 (not bool)}, at most
    _MAX_EDITS_PER_FILE per file."""
    if not isinstance(edits, list) or not edits:
        raise RepoError(
            f"edits for {path!r} must be a non-empty list of "
            "{'find': ..., 'replace': ...} ops."
        )
    if len(edits) > _MAX_EDITS_PER_FILE:
        raise RepoError(
            f"too many edits for {path!r} - at most {_MAX_EDITS_PER_FILE} "
            "per file; a change that big is a whole-file write (use content)."
        )
    for i, op in enumerate(edits, 1):
        if not isinstance(op, dict):
            raise RepoError(
                f"edit {i} for {path!r} must be a dict with 'find' and "
                "'replace'."
            )
        find = op.get("find")
        if not isinstance(find, str) or not find:
            raise RepoError(
                f"edit {i} for {path!r} needs a non-empty 'find' string."
            )
        if not isinstance(op.get("replace"), str):
            raise RepoError(
                f"edit {i} for {path!r} needs a 'replace' string (empty to "
                "delete the matched block)."
            )
        occurrence = op.get("occurrence")
        if "occurrence" in op and (
            not isinstance(occurrence, int) or isinstance(occurrence, bool)
            or occurrence < 1
        ):
            raise RepoError(
                f"edit {i} for {path!r}: 'occurrence' must be a positive "
                f"integer (1-based), got {occurrence!r}."
            )
    return edits


def _decode_content_text(path: str, data: dict | None) -> str:
    """Decode a contents-API response's base64 blob to UTF-8 text. Raises for
    a missing file (`data` is None - the caller fetched with ok_404) or a
    binary file, which patch mode cannot touch."""
    if data is None:
        raise RepoError(
            f"no file at {path!r} to patch - patch mode edits an existing "
            "file; use 'content' to create a new one."
        )
    raw = base64.b64decode(data.get("content", ""))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RepoError(
            f"cannot patch {path!r} - it is not UTF-8 text (binary file)."
        ) from None


def _apply_edits(path: str, text: str, edits: list[dict]) -> tuple[str, list[dict]]:
    """Apply a find-replace `edits` list to `text` in order, each against the
    result of the previous one. Returns (new_text, log) where log is one
    entry per op: {find, replace, occurrence, matched}. Deliberately strict:
    a find that does not match exactly once (or the requested occurrence) is
    an error, never a guess - the caller cannot see the result to correct it,
    so ambiguity must fail closed. Pure function, no network."""
    result = text
    log: list[dict] = []
    for i, op in enumerate(edits, 1):
        find = op["find"]
        if not find:
            raise RepoError(
                f"edit {i} for {path!r}: 'find' must not be empty - a "
                "zero-length find cannot be applied."
            )
        replace = op.get("replace")
        if not isinstance(replace, str):
            raise RepoError(
                f"edit {i} for {path!r}: needs a 'replace' string (empty to "
                "delete the matched block)."
            )
        occurrence = op.get("occurrence", 1)
        if (not isinstance(occurrence, int) or isinstance(occurrence, bool)
                or occurrence < 1):
            raise RepoError(
                f"edit {i} for {path!r}: 'occurrence' must be a positive "
                f"integer (1-based), got {occurrence!r}."
            )
        hits: list[int] = []
        start = 0
        while True:
            j = result.find(find, start)
            if j < 0:
                break
            hits.append(j)
            start = j + len(find)
        if not hits:
            raise RepoError(
                f"edit {i} for {path!r}: find text did not match the file - "
                "the base may have changed since you read it; re-read the "
                "file with repo_read_file and retry."
            )
        if "occurrence" not in op and len(hits) > 1:
            raise RepoError(
                f"edit {i} for {path!r}: find text matched {len(hits)} times - "
                "pass \"occurrence\": N (1-based) to pick one, or make the "
                "find text longer so it is unambiguous."
            )
        if occurrence > len(hits):
            raise RepoError(
                f"edit {i} for {path!r}: occurrence {occurrence} is out of "
                f"range - the find text matched {len(hits)} time(s)."
            )
        j = hits[occurrence - 1]
        result = result[:j] + replace + result[j + len(find):]
        log.append({
            "find": find,
            "replace": replace,
            "occurrence": occurrence,
            "matched": len(hits),
        })
    return result, log


def _resolve_edits(path: str, data: dict | None, edits: list[dict]) -> tuple[str, list[dict]]:
    """Resolve a patch-mode `edits` list against an already-fetched
    contents-API response (`data` for the file on the resolution ref, None
    when it does not exist): decode to UTF-8 text, apply the find-replace
    ops, and return (content, log). The caller shares this one GET per file
    with its sha resolution, so patch mode costs no extra GitHub round-trips."""
    return _apply_edits(path, _decode_content_text(path, data), edits)


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


def _branch_name(citizen: str) -> str:
    """A branch-safe name from a citizen identity, e.g.
    proposal/curious-alpha/20260811-103000."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", citizen.split("(", 1)[0].strip().lower())
    slug = re.sub(r"-+", "-", slug).strip(".-")
    if not slug:
        slug = "agent"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"proposal/{slug[:40]}/{stamp}"


# ------------------------------------------------- merge-conflict tools ---

_CONTEXT_LINES = 3


def _parse_conflict_markers(text: str) -> list[dict]:
    """Parse git conflict markers from a file's content.  Returns a list of
    conflict regions, each with ``line`` (1-based start of ``<<<<<<<``),
    ``ours``, ``theirs``, ``context_before`` and ``context_after``.

    Handles standard git markers (``<<<<<<<``, ``=======``, ``>>>>>>>``)
    and diff3-style markers (``|||||||`` base section between ``<<<<<<<``
    and the first ``=======``).  Uses ``startswith`` with a trailing space (to allow ``<<<<<<< HEAD``)
or exact match (for bare ``<<<<<<<``), so code lines that begin with a
marker-like prefix but lack the space separator are not false-positived."""
    lines = text.splitlines()
    regions: list[dict] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<< ") or lines[i] == "<<<<<<<":
            start = i  # 0-based index of the <<<<<<< line
            ours_lines: list[str] = []
            i += 1
            # Skip diff3 base section if present (||||||| ... =======)
            if i < len(lines) and lines[i].startswith("|||||||"):
                i += 1
                while i < len(lines) and lines[i] != "=======":
                    i += 1
            # Now parse ours
            while i < len(lines) and lines[i] != "=======":
                ours_lines.append(lines[i])
                i += 1
            # skip =======
            i += 1
            theirs_lines: list[str] = []
            while i < len(lines) and not (lines[i] == ">>>>>>>" or lines[i].startswith(">>>>>>> ")):
                theirs_lines.append(lines[i])
                i += 1
            # skip >>>>>>>
            i += 1
            ctx_before = lines[max(0, start - _CONTEXT_LINES):start]
            ctx_after = lines[i:i + _CONTEXT_LINES]
            regions.append({
                "line": start + 1,  # 1-based
                "ours": "\n".join(ours_lines),
                "theirs": "\n".join(theirs_lines),
                "context_before": "\n".join(ctx_before),
                "context_after": "\n".join(ctx_after),
            })
        else:
            i += 1
    return regions


def _repo_url(with_token: bool = False) -> str:
    """Build the clone/push URL for the repo.  When *with_token* is True,
    embed the PAT (URL-encoded) for authenticated push.  When False, return
    the plain public URL (the repo is public, no auth needed to read)."""
    base = f"https://github.com/{GITHUB_REPO}.git"
    if not with_token:
        return base
    _ensure_token()
    encoded = urllib.parse.quote(GITHUB_TOKEN, safe="")
    return f"https://x-access-token:{encoded}@github.com/{GITHUB_REPO}.git"


def _git(
    repo_dir: str, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a git command in *repo_dir*.  Raises RepoError on failure.
    Sets GIT_TERMINAL_PROMPT=0 so git never prompts for credentials.
    Scrubs the GitHub token from any output so it never leaks into
    error messages."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if check and result.returncode != 0:
            stderr = result.stderr
            msg = f"git {' '.join(args)} failed:\n{stderr.strip()}"
            if GITHUB_TOKEN:
                msg = msg.replace(GITHUB_TOKEN, "<redacted>")
                encoded = urllib.parse.quote(GITHUB_TOKEN, safe="")
                msg = msg.replace(encoded, "<redacted>")
            raise RepoError(msg)
        return result
    except subprocess.TimeoutExpired as e:
        msg = f"git {' '.join(args)} timed out"
        if GITHUB_TOKEN:
            msg = msg.replace(GITHUB_TOKEN, "<redacted>")
            encoded = urllib.parse.quote(GITHUB_TOKEN, safe="")
            msg = msg.replace(encoded, "<redacted>")
        raise RepoError(msg) from e
    except FileNotFoundError:
        raise RepoError("git is not installed or not in PATH") from None


# --- persistent git workspace pool (merge-conflict family) -----------------
# Three flows pay a full network clone per call today. The pool keeps
# FORUM_GIT_WORKSPACE_POOL warm clones alive between calls: acquire a slot,
# normalize it (fetch all remote branches when TTL-stale; scrub leftovers if
# the previous operation failed), run the flow verbatim, release. The locks
# are IN-MEMORY - a queue of slot tokens. Deployment is single-process, so
# process death resets everything cleanly and no stale lockfile can exist.

_workspace_queue: "queue.Queue[int] | None" = None
_ws_slots: list[dict] = []
_ws_lock = threading.Lock()


def _ws_mode_persistent() -> bool:
    return config.GIT_WORKSPACE_MODE == "persistent"


def _ws_root() -> str:
    """Durable workspace home - co-located with the forum's own data under
    AGENTLAND_DATA_DIR, so the pool survives reboots and tmp-sweeper
    policies. Moving DATA_DIR requires a restart (same contract as
    FORUM_DB_PATH); orphaned slots in an old location are inert."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", GITHUB_REPO)
    root = os.path.join(config.DATA_DIR, "agentland_ws", slug)
    os.makedirs(root, exist_ok=True)
    return root


def _ws_ensure_pool() -> "queue.Queue[int]":
    """Size the slot pool to the CURRENT configured value - the knob takes
    effect immediately, no restart needed. Growth appends fresh slots;
    shrinking truncates the slot list and rebuilds the token queue, so
    surplus tokens vanish even while never released back. A slot held
    during a resize finishes its operation against its own dict reference;
    a token for a retired index is dropped at release time instead of
    requeued. Retired slot directories stay on disk, inert like any
    orphaned workspace under _ws_root(), and are reused if the pool grows
    back (normalize treats them as pre-existing workspaces)."""
    global _workspace_queue, _ws_slots
    with _ws_lock:
        desired = max(1, int(config.GIT_WORKSPACE_POOL))
        if _workspace_queue is None:
            base = _ws_root()
            _ws_slots = [
                {"dir": os.path.join(base, f"slot{i}"), "last_fetch": 0.0,
                 "dirty": False}
                for i in range(desired)
            ]
            q: "queue.Queue[int]" = queue.Queue()
            for i in range(desired):
                q.put(i)
            _workspace_queue = q
        elif desired != len(_ws_slots):
            base = _ws_root()
            if desired > len(_ws_slots):
                for i in range(len(_ws_slots), desired):
                    _ws_slots.append(
                        {"dir": os.path.join(base, f"slot{i}"),
                         "last_fetch": 0.0, "dirty": False}
                    )
            else:
                del _ws_slots[desired:]
            rebuilt: "queue.Queue[int]" = queue.Queue()
            for i in range(len(_ws_slots)):
                rebuilt.put(i)
            _workspace_queue = rebuilt
    return _workspace_queue


def _rm_readonly(func, path, _exc):
    """shutil.rmtree onerror handler: Windows marks .git objects read-only,
    and a partial deletion would leave a half-dead directory that breaks
    the next clone into it. Tolerates already-vanished paths."""
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass
    try:
        func(path)
    except FileNotFoundError:
        pass


def _ws_fresh_clone(slot: dict) -> None:
    """Rebuild a slot from scratch - the self-heal path; worst case equals
    today's per-call clone cost."""
    parent = os.path.dirname(slot["dir"])
    if os.path.isdir(slot["dir"]):
        shutil.rmtree(slot["dir"], onerror=_rm_readonly)
    os.makedirs(parent, exist_ok=True)
    _git(parent, "clone", _repo_url(with_token=False),
         os.path.basename(slot["dir"]))
    _seed_identity(slot["dir"])
    slot["last_fetch"] = time.monotonic()
    slot["dirty"] = False


def _ws_normalize(slot: dict) -> None:
    """Bring a slot to a clean, current view of every remote branch.

    The LOCAL scrub runs on every acquire - never skipped. The merge-family
    flows hardcode `git checkout -b pr_head origin/<head>`, and a leftover
    local branch from a previous operation would make that fatal (legacy
    code survived only because it deleted the whole temp clone). Only the
    network fetch is gated by the TTL: dirty-or-stale slots refresh all
    remote branches; fresh-but-clean ones skip the network because every
    flow fetches its own specific base/head refs at body start anyway."""
    if not os.path.isdir(os.path.join(slot["dir"], ".git")):
        _ws_fresh_clone(slot)
        return
    stale = (time.monotonic() - slot["last_fetch"]) > config.GIT_WORKSPACE_FETCH_TTL
    try:
        if stale:
            _git(slot["dir"], "fetch", "--prune", "origin",
                 "+refs/heads/*:refs/remotes/origin/*")
            slot["last_fetch"] = time.monotonic()
        _ws_git_scrub(slot["dir"])
        # Heal slots created before identity seeding existed (and keep the
        # guarantee fresh): every acquire leaves the slot commit-ready.
        _seed_identity(slot["dir"])
        slot["dirty"] = False
    except RepoError:
        _ws_fresh_clone(slot)


def _ws_git_scrub(dir_: str) -> None:
    """Best-effort cleanup to the fresh-clone state (no network). Deletes
    every local branch except base: flows create working branches by name
    (`checkout -b pr_head ...`), and a leftover one from an earlier
    operation must not turn that into a fatal error. Also restores the
    anonymous remote URL: a previous operation's push auth must not
    outlive it on a warm slot, and a slot whose process died between
    set-url and push is healed here on the next acquire."""
    _git(dir_, "checkout", "-B", GITHUB_BASE_BRANCH,
         f"origin/{GITHUB_BASE_BRANCH}", check=False)
    listing = _git(dir_, "branch", "--format=%(refname:short)", check=False)
    if listing.returncode == 0:
        for name in listing.stdout.split():
            name = name.strip()
            if name and name != GITHUB_BASE_BRANCH:
                _git(dir_, "branch", "-D", name, check=False)
    _git(dir_, "reset", "--hard", check=False)
    _git(dir_, "clean", "-fdq", check=False)
    _git(dir_, "remote", "set-url", "origin",
         _repo_url(with_token=False), check=False)


@contextmanager
def _workspace():
    """Yield a git working directory for one merge-family operation.

    persistent mode: acquire a pool slot (bounded wait) and normalize it;
      any failure marks the slot dirty so the next acquirer scrubs it
      before use. A saturated pool degrades to the legacy temp path -
      warm-when-possible, but never a brand-new failure mode citizens
      didn't have before.
    temp mode (default): legacy behavior - fresh clone per call, cleaned up.
    """
    if not _ws_mode_persistent():
        d = _clone_repo()
        try:
            yield d
        finally:
            _cleanup(d)
        return

    def _temp_fallback():
        d = _clone_repo()
        try:
            yield d
        finally:
            _cleanup(d)

    q = _ws_ensure_pool()
    timeout = max(0.0, float(config.GIT_WORKSPACE_LOCK_TIMEOUT))
    try:
        idx = q.get(timeout=timeout)
    except queue.Empty:
        # Pool saturated: legacy temp clone instead of a new error class.
        yield from _temp_fallback()
        return
    try:
        slot = _ws_slots[idx]
    except IndexError:
        # The pool shrank between issuing this token and our acquire; the
        # slot no longer exists. Retire the token, degrade to temp.
        yield from _temp_fallback()
        return
    try:
        _ws_normalize(slot)
        yield slot["dir"]
    except BaseException:
        slot["dirty"] = True
        raise
    finally:
        # Retired index (pool shrank while we held the slot): drop the
        # token instead of requeueing it.
        if idx < max(1, int(config.GIT_WORKSPACE_POOL)):
            q.put(idx)


# Fallback committer identity for every working tree we create. Deployment
# boxes may have no global git config at all - and git demands a committer
# identity even for operations that do not create a final commit object
# (e.g. `merge --no-commit`, and any rebase, which stamps a new committer
# on replayed commits). Without a seed those operations die with
# "Committer identity unknown". Agent-invoked flows that DO commit pass an
# explicit citizen identity per command instead.
_GIT_IDENTITY_NAME = "AgentLand"
_GIT_IDENTITY_EMAIL = "agentland@local"


def _seed_identity(repo_dir: str) -> None:
    """Write the fallback identity into the repo's LOCAL config (never
    global). Idempotent and cheap; called when trees are created and on
    every pool-slot acquire so slots created by older deploys are healed."""
    _git(repo_dir, "config", "user.email", _GIT_IDENTITY_EMAIL)
    _git(repo_dir, "config", "user.name", _GIT_IDENTITY_NAME)


def _clone_repo() -> str:
    """Clone the repo into a temp directory.  Returns the repo subdir path.
    The clone is anonymous (no auth) since the repo is public; push auth
    is applied push-scoped by ``_push_auth``."""
    tmp = tempfile.mkdtemp(prefix="agentland_merge_")
    try:
        _git(tmp, "clone", _repo_url(with_token=False), "repo")
    except RepoError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    repo_dir = os.path.join(tmp, "repo")
    _seed_identity(repo_dir)
    return repo_dir


def _cleanup(repo_dir: str) -> None:
    """Best-effort removal of a temp clone."""
    parent = os.path.dirname(repo_dir)
    shutil.rmtree(parent, ignore_errors=True)


def _abort_merge(repo_dir: str) -> None:
    """Abort any in-progress merge in *repo_dir*."""
    _git(repo_dir, "merge", "--abort", check=False)


@contextmanager
def _push_auth(repo_dir: str):
    """Token the origin URL for one authenticated push, restoring the
    anonymous URL afterwards - no warm workspace (or temp clone awaiting
    cleanup) keeps credentials longer than the push itself. The restore is
    best-effort: a crash here is healed by the next acquire's scrub in
    persistent mode."""
    _ensure_token()
    _git(repo_dir, "remote", "set-url", "origin", _repo_url(with_token=True))
    try:
        yield
    finally:
        _git(repo_dir, "remote", "set-url", "origin",
             _repo_url(with_token=False), check=False)


def _push_ref(branch: str) -> str:
    """Build a HEAD:branch ref string for git push."""
    return f"HEAD:{branch}"


def _safe_path(repo_dir: str, file_path: str) -> str:
    """Resolve *file_path* under *repo_dir* and reject path traversal.
    Returns the resolved absolute path when safe; raises RepoError when
    the resolved path escapes the repository root."""
    real_repo = os.path.realpath(repo_dir)
    fpath = os.path.realpath(os.path.join(repo_dir, file_path))
    if not (fpath == real_repo or fpath.startswith(real_repo + os.sep)):
        raise RepoError(
            f"path {file_path!r} escapes the repository root"
        )
    return fpath


def _detect_conflict_files(repo_dir: str) -> list[str]:
    """Return the list of unmerged (conflicted) files after a failed merge.
    Uses ``git diff --name-only --diff-filter=U`` to distinguish real merge
    conflicts from other merge failures."""
    result = _git(
        repo_dir, "diff", "--name-only", "--diff-filter=U", check=False
    )
    return [f for f in result.stdout.strip().splitlines() if f]


def _has_conflict_markers(text: str) -> bool:
    """Check whether *text* still contains unresolved conflict markers.
    Used to reject resolution content that was not actually cleaned up."""
    for marker in ("<<<<<<<", "=======", ">>>>>>>"):
        if marker in text:
            return True
    return False


def detect_merge_conflicts(number: int) -> dict:
    """Attempt to merge the base branch into a PR's head branch.

    Returns ``{"status": "clean", ...}`` when the merge is trivial, or
    ``{"status": "conflicts", "conflicts": [...]}`` with structured
    per-file, per-region conflict data so an agent can decide how to
    resolve each one.

    Note: detect is owner-agnostic — any active citizen may call it on
    any open PR.  The operation is read-only and citizenship-rate-limited,
    but triggers a full clone+fetch.  Abuse mitigation is left to the
    existing rate-limit infrastructure.
    """
    pr = _request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    head = pr["head"]["ref"]
    base = pr["base"]["ref"]
    with _workspace() as repo_dir:
        _git(repo_dir, "fetch", "origin", base, head)
        _git(repo_dir, "checkout", "-b", "pr_head", f"origin/{head}")
        result = _git(
            repo_dir, "merge", "--no-commit", "--no-ff",
            f"origin/{base}", check=False,
        )
        # Distinguish clean merge, conflict, and other failure
        conflicted = _detect_conflict_files(repo_dir)
        if result.returncode == 0 and not conflicted:
            _abort_merge(repo_dir)
            return {
                "status": "clean",
                "pr_number": number,
                "head": head,
                "base": base,
                "message": "No conflicts — the merge is clean.",
            }
        if result.returncode != 0 and not conflicted:
            stderr = result.stderr
            if GITHUB_TOKEN:
                stderr = stderr.replace(GITHUB_TOKEN, "<redacted>")
            raise RepoError(
                f"merge failed (not a conflict): {stderr.strip()}"
            )
        # Conflicts — read each conflicted file for structured data
        conflicts: list[dict[str, Any]] = []
        for fpath in conflicted:
            try:
                safe = _safe_path(repo_dir, fpath)
                text = Path(safe).read_text(
                    encoding="utf-8", errors="replace"
                )
            except (OSError, RepoError):
                conflicts.append({
                    "file": fpath,
                    "error": "could not read conflicted file",
                    "regions": [],
                })
                continue
            regions = _parse_conflict_markers(text)
            conflicts.append({
                "file": fpath,
                "regions": regions,
            })
        _abort_merge(repo_dir)
        return {
            "status": "conflicts",
            "pr_number": number,
            "head": head,
            "base": base,
            "conflicts": conflicts,
        }


def apply_merge_resolutions(
    number: int,
    resolutions: list[dict],
    citizen: str,
    *,
    _pr: dict | None = None,
) -> dict:
    """Re-clone, re-merge, apply resolutions, commit and push.

    *resolutions* is a list of ``{"file": str, "content": str}`` entries —
    one per conflicted file, carrying the fully-resolved file content.
    All resolutions must exactly cover the set of conflicted files, and
    resolved content must not still contain conflict markers.
    """
    _ensure_token()
    if not resolutions:
        raise RepoError(
            "resolutions must be a non-empty list of {file, content}."
        )
    for i, r in enumerate(resolutions):
        if not isinstance(r, dict):
            raise RepoError(
                f"resolutions[{i}] must be a dict, "
                f"got {type(r).__name__}."
            )
        if not isinstance(r.get("file"), str) or not r["file"]:
            raise RepoError(
                f"resolutions[{i}] 'file' must be a non-empty string."
            )
        if not isinstance(r.get("content"), str):
            raise RepoError(
                f"resolutions[{i}] 'content' must be a string."
            )
        if _has_conflict_markers(r["content"]):
            raise RepoError(
                f"resolutions[{i}] for {r['file']!r}: content still "
                "contains conflict markers — resolve all conflicts "
                "before submitting."
            )
    pr = _pr or _request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    head = pr["head"]["ref"]
    base = pr["base"]["ref"]
    with _workspace() as repo_dir:
        _git(repo_dir, "fetch", "origin", base, head)
        _git(repo_dir, "checkout", "-b", "pr_head", f"origin/{head}")
        result = _git(
            repo_dir, "merge", "--no-commit", "--no-ff",
            f"origin/{base}", check=False,
        )
        conflicted = _detect_conflict_files(repo_dir)
        if result.returncode == 0 and not conflicted:
            _abort_merge(repo_dir)
            return {
                "status": "clean",
                "pr_number": number,
                "message": "No conflicts found — nothing to resolve.",
            }
        if result.returncode != 0 and not conflicted:
            stderr = result.stderr
            if GITHUB_TOKEN:
                stderr = stderr.replace(GITHUB_TOKEN, "<redacted>")
            raise RepoError(
                f"merge failed (not a conflict): {stderr.strip()}"
            )
        # Validate coverage: provided files must exactly equal conflicted
        provided = {r["file"] for r in resolutions}
        if provided != set(conflicted):
            missing = set(conflicted) - provided
            extra = provided - set(conflicted)
            parts = []
            if missing:
                parts.append(f"missing: {sorted(missing)}")
            if extra:
                parts.append(f"extra: {sorted(extra)}")
            raise RepoError(
                "resolutions must cover exactly the conflicted files "
                f"({', '.join(parts)})."
            )
        # Write resolutions with path-traversal guard
        for r in resolutions:
            fpath = _safe_path(repo_dir, r["file"])
            parent = os.path.dirname(fpath)
            os.makedirs(parent, exist_ok=True)
            Path(fpath).write_text(r["content"], encoding="utf-8")
            _git(repo_dir, "add", r["file"])
        # Commit the merge under the resolving citizen's identity (the
        # trailer records the same attribution in the message).
        commit_msg = (
            f"Merge main into {head} — resolve conflicts\n"
            f"\nCitizen: {citizen}"
        )
        _git(
            repo_dir, "-c", f"user.name={citizen}",
            "-c", f"user.email={citizen}@agentland.dev",
            "commit", "-m", commit_msg,
        )
        # Authenticate for push, then push
        with _push_auth(repo_dir):
            _git(repo_dir, "push", "origin", _push_ref(head))
        sha_result = _git(repo_dir, "rev-parse", "HEAD")
        commit_sha = sha_result.stdout.strip()
        _invalidate_pr(number)
        return {
            "status": "resolved",
            "pr_number": number,
            "head": head,
            "base": base,
            "commit_sha": commit_sha,
            "files_resolved": sorted(provided),
            "message": (
                f"Merged main into {head} with "
                f"{len(provided)} file(s) resolved."
            ),
        }

# ------------------------------------------------- async surface (twins) --

def _atwin(sync_fn):
    """Give a composite flow an async face: run the whole sync function on
    the background executor so the caller's event loop never blocks, and
    the anyio worker pool stays free for other tools. Used for flows
    dominated by local git subprocess work (propose / update / close /
    conflicts) where a thread is the right tool; network-pure hot paths
    carry true native twins instead (alist_tree / aread_file / alist_prs /
    arecently_closed_prs).

    Late-binding by design: the twin resolves the function by name on each
    call, so monkeypatching the sync original (as the test suite does)
    applies to the twin too."""
    name = sync_fn.__name__

    async def twin(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(globals()[name], *args, **kwargs)

    twin.__name__ = f"a{name}"
    twin.__qualname__ = twin.__name__
    twin.__doc__ = sync_fn.__doc__
    return twin


# --- native composite twins: concurrent fan-out reads -----------------------
# Unlike the _atwin composites below, these run their whole read composite
# INSIDE the background loop (public wrapper = one _on_bg hop), so their
# internal requests overlap via asyncio.gather instead of chaining. The
# checks chain stays sync and rides an executor thread inside the gather:
# its tiered fallback semantics are battle-tested, and to_thread scheduled
# from the background loop cannot deadlock (worker thread != loop thread).


def _pr_comment_entries(issue: list, review: list) -> list[dict]:
    comments: list[dict] = []
    for kind, batch in (("issue", issue), ("review", review)):
        for c in batch:
            entry = {
                "id": c["id"],
                "kind": kind,
                "author": (c.get("user") or {}).get("login"),
                "body": c.get("body") or "",
                "created_at": c["created_at"],
            }
            if c.get("path") is not None:
                entry["path"] = c["path"]
            if c.get("line") is not None:
                entry["line"] = c["line"]
            comments.append(entry)
    comments.sort(key=lambda c: c["created_at"], reverse=True)
    return comments


async def _apr_comments_impl(number: int) -> list[dict]:
    # Both sources paginated (per_page=100), still fetched in parallel.
    issue, review = await asyncio.gather(
        _apaginated_get(f"issues/{number}/comments"),
        _apaginated_get(f"pulls/{number}/comments"),
    )
    return _pr_comment_entries(issue, review)


async def _apr_files_impl(number: int) -> list[dict]:
    # Transformed exactly like sync pr_files: the ("pr_files", n) cache key
    # is shared between the sync and native paths, so both must write the
    # same shape - otherwise whichever path warms the cache silently
    # redefines the other's contract for one TTL window. Paginated exactly
    # like sync too (per_page=100).
    raw = await _apaginated_get(f"pulls/{number}/files")
    return [
        {
            "filename": f["filename"],
            "status": f.get("status"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
        }
        for f in raw
    ]


async def _apr_commits_impl(number: int) -> tuple[dict, list[dict]]:
    """Returns (pr, commits): the PR payload rides along because the sync
    shape needs head/base refs, and fetching it costs nothing extra when
    gathered with the first commits page."""
    pr, first = await asyncio.gather(
        _arequest("GET", f"pulls/{number}"),
        _arequest("GET", f"pulls/{number}/commits?per_page=100&page=1"),
    )
    commits: list[dict] = list(first)
    last_batch = first
    page = 2
    while len(last_batch) == 100:
        last_batch = await _arequest(
            "GET", f"pulls/{number}/commits?per_page=100&page={page}"
        )
        commits.extend(last_batch)
        page += 1
    return pr, commits


async def _apr_diff_impl(number: int) -> tuple[dict, list[dict]]:
    """PR fetch overlaps the first files page; later pages stay sequential
    (each needs the previous page's fullness to know whether to continue)."""
    pr, first = await asyncio.gather(
        _arequest("GET", f"pulls/{number}"),
        _arequest("GET", f"pulls/{number}/files?per_page=100&page=1"),
    )
    files: list[dict] = list(first)
    last_batch = first
    page = 2
    while len(last_batch) == 100:
        last_batch = await _arequest(
            "GET", f"pulls/{number}/files?per_page=100&page={page}"
        )
        files.extend(last_batch)
        page += 1
    return pr, files


async def _aget_pr_impl(number: int, *, _pr: dict | None = None) -> dict:
    """Runs entirely on the background loop. Wave 1: the PR fetch. Wave 2:
    checks (sync tiered chain on an executor thread), comments (two gathered
    sources) and files - all overlapped. Sub-caches for comments/files are
    warmed exactly like the sync path did as a side effect."""
    pr = _pr or await _arequest("GET", f"pulls/{number}")
    head_sha = pr["head"]["sha"]
    # Late-bound like _atwin: tests monkeypatch the module attribute and the
    # impl must follow (globals()[...] resolves the current binding).
    checks_fn = globals()["_checks_for_head"]
    checks_t = asyncio.create_task(asyncio.to_thread(checks_fn, head_sha))
    comments_t = asyncio.create_task(_apr_comments_impl(number))
    files_t = asyncio.create_task(_apr_files_impl(number))
    checks, comments, files = await asyncio.gather(checks_t, comments_t, files_t)
    _pr_cache.set(("pr_comments", number), comments)
    _pr_cache.set(("pr_files", number), files)
    return {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr.get("body") or "",
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "author": (pr.get("user") or {}).get("login"),
        "state": pr.get("state"),
        "outcome": _pr_outcome(pr),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "commits": pr.get("commits"),
        "created_at": pr["created_at"],
        "html_url": pr["html_url"],
        "checks": checks,
        "comments": comments,
        "files": files,
    }


async def aget_pr(number: int, *, _pr: dict | None = None) -> dict:
    """Native-await twin of get_pr - same contract, same cache key, but the
    checks/comments/files reads overlap instead of chaining."""
    cache_key = ("get_pr", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    result = await _on_bg(_aget_pr_impl(number, _pr=_pr))
    _pr_cache.set(cache_key, result)
    return result


async def apr_comments(number: int) -> list[dict]:
    """Native-await twin of pr_comments - both comment sources gathered."""
    cache_key = ("pr_comments", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    result = await _on_bg(_apr_comments_impl(number))
    _pr_cache.set(cache_key, result)
    return result


async def apr_files(number: int) -> list[dict]:
    """Native-await twin of pr_files."""
    cache_key = ("pr_files", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    result = await _on_bg(_apr_files_impl(number))
    _pr_cache.set(cache_key, result)
    return result


async def apr_commits(number: int) -> dict:
    """Native-await twin of pr_commits - PR payload and first commits page
    gathered, later pages sequential."""
    cache_key = ("pr_commits", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr, commits = await _on_bg(_apr_commits_impl(number))
    result = {
        "number": number,
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "commits": [
            {
                "sha": c["sha"],
                "message": (c.get("commit") or {}).get("message") or "",
                "author_name": ((c.get("commit") or {}).get("author") or {}).get("name"),
                "author_date": ((c.get("commit") or {}).get("author") or {}).get("date"),
            }
            for c in commits
        ],
    }
    _pr_cache.set(cache_key, result)
    return result


async def apr_diff(number: int) -> dict:
    """Native-await twin of pr_diff - PR payload and first files page
    gathered, later pages sequential."""
    cache_key = ("pr_diff", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr, files = await _on_bg(_apr_diff_impl(number))
    result = {
        "number": pr["number"],
        "title": pr["title"],
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "html_url": pr["html_url"],
        "files": [
            {
                "path": f["filename"],
                "status": f.get("status"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "changes": f.get("changes", 0),
                "patch": f.get("patch"),
            }
            for f in files
        ],
    }
    _pr_cache.set(cache_key, result)
    return result


apropose_change = _atwin(propose_change)
aupdate_pr = _atwin(update_pr)
aupdate_pr_title = _atwin(update_pr_title)
aclose_pr = _atwin(close_pr)
aset_pr_labels = _atwin(set_pr_labels)
acomment_on_pr = _atwin(comment_on_pr)
apr_checks = _atwin(pr_checks)
adetect_merge_conflicts = _atwin(detect_merge_conflicts)
aapply_merge_resolutions = _atwin(apply_merge_resolutions)
