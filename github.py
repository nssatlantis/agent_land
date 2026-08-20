"""
github.py - read/write access to the society's own source repository.

Plain functions, stdlib only (urllib against the GitHub REST API). No MCP
types, no HTTP server code - server.py wraps these as tools. Mirror of
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

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

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
    ``None`` on miss.  Errors are cached the same way so a flaky upstream
    isn't hammered within the window."""

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
# docstring.  A short TTL also absorbs outages: a failure is cached and
# re-raised instead of re-probed on every call within the window.
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


def _request(method: str, path: str, body: dict | None = None, ok_404: bool = False):
    """Hit the GitHub REST API. Raises RepoError on failure. Returns parsed
    JSON (or None for 204/404-ok)."""
    _ensure_token()
    url = f"{API_ROOT}/repos/{GITHUB_REPO}/{path}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=config.GITHUB_HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        result = _raise_request_error(e, method, path, ok_404)
        if result is None:
            return None
        raise result
    except urllib.error.URLError as e:
        raise RepoError(f"could not reach GitHub: {e.reason}") from e


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
    if cached is None:
        pass  # data already validated above
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
    than _MAX_READ_FILE_LINES, or a range past the end of the file (naming
    its total line count). Lines are text.split("\\n") parts: total_lines is
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
        raise RepoError(
            f"repo_read_file line range {line_start}-{line_end} is past the end of "
            f"{path!r} - the file has {total_lines} lines total."
        )
    return "\n".join(lines[line_start - 1:line_end]), total_lines


# Open-PR list cache -- shared by repo_list_prs, repo_my_prs, my_profile.
# The viewer keeps its own outer cache on top.
_OPEN_PRS_CACHE_SECONDS = config.PR_CACHE_SECONDS


def open_prs() -> list[dict]:
    """Open pull requests, newest first, cached briefly (PR_CACHE_SECONDS)."""
    cached = _open_prs_cache.get("open_prs", _OPEN_PRS_CACHE_SECONDS)
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


def pr_files(number: int) -> list[dict]:
    """The files a pull request changes, for checking what it actually
    touches: [{filename, status, additions, deletions}].

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
        for f in _request("GET", f"pulls/{number}/files")
    ]
    _pr_cache.set(cache_key, result)
    return result


def pr_comments(number: int) -> list[dict]:
    """All comments on a pull request, newest first.  Two GitHub sources:
    `issue` comments (the conversation thread repo_comment_on_pr writes to)
    and `review` comments (inline notes on specific diff lines).

    Cached for PR_CACHE_SECONDS (default 30 s)."""
    cache_key = ("pr_comments", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    comments: list[dict] = []
    for kind, path in (("issue", f"issues/{number}/comments"), ("review", f"pulls/{number}/comments")):
        for c in _request("GET", path):
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
    return {
        "pr_number": number,
        "comment_id": data["id"],
        "author": (data.get("user") or {}).get("login"),
        "created_at": data["created_at"],
        "html_url": data["html_url"],
    }


def _request_text(method: str, path: str, ok_404: bool = False) -> str | None:
    """Like _request but for text responses - GitHub's Actions log download
    (actions/jobs/{id}/logs) is text/plain, not JSON. Returns the decoded
    text ('' for an empty body) or None on an ok_404 miss; raises RepoError
    exactly like _request otherwise."""
    _ensure_token()
    url = f"{API_ROOT}/repos/{GITHUB_REPO}/{path}"
    req = urllib.request.Request(url, method=method, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=config.GITHUB_HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            if not raw:
                return ""
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        result = _raise_request_error(e, method, path, ok_404)
        if result is None:
            return None
        raise result
    except urllib.error.URLError as e:
        raise RepoError(f"could not reach GitHub: {e.reason}") from e


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


def _checks_for_head(head_sha: str) -> dict | None:
    """CI detail for one commit, tiered and never failing the read: (1) check
    runs with annotations, then (2) GitHub Actions workflow runs with log
    error-lines, then (3) the combined commit status. Each tier's 403/404
    falls into the next; only a total outage yields None."""
    try:
        data = _request("GET", f"commits/{head_sha}/check-runs?per_page={_MAX_CHECK_RUNS}")
        runs = data.get("check_runs") or []
        if runs:
            return _checks_from_check_runs(runs)
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


def pr_checks(number: int, *, _pr: dict | None = None) -> dict:
    """One pull request's CI detail: per-run name/status/conclusion plus the
    actionable failures (annotations with path/line/message, or error lines
    extracted from a capped Actions log tail). The backend is tiered (check
    runs -> Actions workflow runs -> combined commit status) and never fails
    the read: `source` names which tier answered, `state` is 'success' /
    'failure' / 'pending' / 'unknown'. get_pr's `checks` field uses the same
    builder, so a red PR carries its reason everywhere it is read.

    Cached for PR_CACHE_SECONDS (default 30 s).  ``_pr`` is an optional
    pre-fetched raw PR dict to avoid a redundant API call."""
    cache_key = ("pr_checks", number)
    cached = _pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
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
    commits: list[dict] = []
    page = 1
    while True:
        batch = _request("GET", f"pulls/{number}/commits?per_page=100&page={page}")
        commits.extend(batch)
        if len(batch) < 100:
            break
        page += 1
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
             existing file on the PR branch, or {"path": str, "delete": True}
             to remove - one commit per entry, each carrying the Citizen
             trailer of whoever is updating. Patch entries are resolved
             against the PR branch head at call time (they stack on the PR's
             own earlier commits) and fail closed on no-match / ambiguous /
             out-of-range / missing / binary, like propose_change.
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
        modes = sum(1 for flag in (has_content, has_edits, is_delete) if flag)
        if modes == 0:
            raise RepoError(
                f"change for {path!r} needs 'content', 'edits' or "
                "'delete': True."
            )
        if modes > 1:
            raise RepoError(
                f"change for {path!r} has more than one of 'content', "
                "'edits' and 'delete' - use one."
            )
        if is_delete:
            planned.append({"path": path, "delete": True})
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

    # Resolve patch entries against the PR branch head before building the
    # plan - a patch cannot be previewed (or written) without the base, and
    # the sha resolution rides along on the same GET.
    for p in planned:
        if "edits" in p:
            data = _request("GET", f"contents/{p['path']}?ref={branch}", ok_404=True)
            content, log = _resolve_edits(p["path"], data, p["edits"])
            p["content"] = content
            p["sha"] = data.get("sha")
            p["patch_log"] = log

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
        elif "edits" in p:
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
    return plan


def close_pr(number: int, *, _pr: dict | None = None) -> dict:
    """Close a pull request without merging (state=closed). The caller is
    responsible for the ownership check (server.py matches the PR's Citizen
    trailer against the forum token) and for leaving a reason comment."""
    pr = _pr or _request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    data = _request("PATCH", f"pulls/{number}", {"state": "closed"})
    return {
        "pr_number": number,
        "state": data.get("state"),
        "closed_at": data.get("closed_at"),
    }


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
