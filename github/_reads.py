"""github._reads - read-side surface of the society's GitHub client.

Tree/file reads, PR listings (open / closed / since-filtered), the citizen-
and proposal-stamp parsers and strippers shared with server.py, the per-PR
composite reads (get_pr / pr_diff / pr_files / pr_comments / pr_commits)
and their pagination helpers. Every function here is a pure read: writes
live in github._writes, local-git flows in github._gitops.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime

import config

from . import _checks, _core
from ._core import (
    GITHUB_BASE_BRANCH,
    GITHUB_REPO,
    RepoError,
    _validate_path,
    _validate_ref,
)

# Cap on lines per repo_read_file range read. Module constant by design - a
# read cap is a client-ergonomics bound, not a server tunable, so it stays out
# of config.py and the drift manifest.
_MAX_READ_FILE_LINES = 1000

# GitHub silently caps pulls?per_page= at 100 regardless of what is asked.
# Clamp to that so a caller (or FORUM_GITHUB_PRS_PER_PAGE above the cap) can
# never get a short page that the pagination loops below mistake for the end
# of the listing.
_MAX_GITHUB_PERPAGE = 100


def repo_spec() -> str:
    """The owner/name the tools are wired to, e.g. 'nssatlantis/agent_land'."""
    return GITHUB_REPO


def base_branch() -> str:
    """The protected branch all proposals are based on and pointed at."""
    return GITHUB_BASE_BRANCH


def list_tree(ref: str | None = None) -> dict:
    """List every file in the base branch, newest shape.  Cached for
    GITHUB_TREE_CACHE_SECONDS (default 5 min) -- the tree only changes on
    merge to the base branch, so a long window is safe. `ref` (optional)
    names the branch/tag/commit to list; defaults to the base branch and the
    response echoes the ref it read."""
    ref = _validate_ref(ref)
    cache_key = ("tree", ref)
    cached = _core._tree_cache.get(cache_key, config.GITHUB_TREE_CACHE_SECONDS)
    if cached is not None:
        return cached
    tree = _core._request("GET", f"git/trees/{ref}?recursive=1")
    entries = []
    for item in tree.get("tree", []):
        if item.get("type") == "blob":
            entries.append({"path": item["path"], "size": item.get("size", 0)})
    result = {
        "repo": GITHUB_REPO,
        "branch": ref,
        "files": entries,
        "truncated": tree.get("truncated", False),
    }
    _core._tree_cache.set(cache_key, result)
    return result


async def alist_tree(ref: str | None = None) -> dict:
    """Native-await twin of list_tree - same cache, same shape, non-blocking
    I/O. The hot repo_list_tree tool path runs this directly on the event
    loop instead of occupying a worker thread."""
    ref = _validate_ref(ref)
    cache_key = ("tree", ref)
    cached = _core._tree_cache.get(cache_key, config.GITHUB_TREE_CACHE_SECONDS)
    if cached is not None:
        return cached
    tree = await _core._on_bg(_core._arequest("GET", f"git/trees/{ref}?recursive=1"))
    entries = [
        {"path": item["path"], "size": item.get("size", 0)}
        for item in tree.get("tree", [])
        if item.get("type") == "blob"
    ]
    result = {
        "repo": GITHUB_REPO,
        "branch": ref,
        "files": entries,
        "truncated": tree.get("truncated", False),
    }
    _core._tree_cache.set(cache_key, result)
    return result


def read_file(
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
    ref: str | None = None,
) -> dict:
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
    ref = _validate_ref(ref)
    cache_key = ("read_file", path, ref)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        data = cached
    else:
        data = _core._request("GET", f"contents/{path}?ref={ref}", ok_404=True)
        if data is None:
            raise RepoError(f"no file at {path!r} in {GITHUB_REPO}@{ref}.")
        _core._pr_cache.set(cache_key, data)
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


async def aread_file(
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
    ref: str | None = None,
) -> dict:
    """Native-await twin of read_file - same contract, non-blocking I/O."""
    path = _validate_path(path)
    ref = _validate_ref(ref)
    cache_key = ("read_file", path, ref)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        data = cached
    else:
        data = await _core._on_bg(
            _core._arequest("GET", f"contents/{path}?ref={ref}", ok_404=True)
        )
        if data is None:
            raise RepoError(f"no file at {path!r} in {GITHUB_REPO}@{ref}.")
        _core._pr_cache.set(cache_key, data)
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
    return "\n".join(lines[line_start - 1 : line_end]), total_lines


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


_MD_ESCAPES = str.maketrans(
    {
        "\\": "\\\\",
        "*": "\\*",
        "_": "\\_",
        "[": "\\[",
        "]": "\\]",
        "`": "\\`",
    }
)


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


# Open-PR list cache -- shared by repo_list_prs, repo_my_prs, my_profile.
# The viewer keeps its own outer cache on top.  TTL is read live from
# config.PR_CACHE_SECONDS so a .env change applies without a restart
# (matching every other cache in this package).
def _open_pulls_page(per_page: int, page: int) -> list:
    """One page of the open pulls listing, newest by created (GitHub's
    default sort for state=open). Mirrors _closed_pulls_page - including the
    per_page clamp, so a FORUM_GITHUB_PRS_PER_PAGE above GitHub's silent cap
    can't produce a short first page that pagination mistakes for the end."""
    per_page = min(per_page, _MAX_GITHUB_PERPAGE)
    return _core._request(
        "GET",
        f"pulls?state=open&per_page={per_page}&page={page}",
    )


async def _aopen_pulls_page(per_page: int, page: int) -> list:
    """Native-await twin of _open_pulls_page."""
    per_page = min(per_page, _MAX_GITHUB_PERPAGE)
    return await _core._on_bg(
        _core._arequest(
            "GET",
            f"pulls?state=open&per_page={per_page}&page={page}",
        )
    )


def _paginated_open_pulls(per_page: int) -> list:
    """Every page of the open pulls listing, newest by created, stopping
    at the first short page. Mirrors _paginated_closed_pulls."""
    out: list = []
    page = 1
    while True:
        batch = _open_pulls_page(per_page, page)
        out.extend(batch)
        if len(batch) < per_page or page >= _PR_PAGE_CAP:
            return out
        page += 1


async def _apaginated_open_pulls(per_page: int) -> list:
    """Native-await twin of _paginated_open_pulls."""
    out: list = []
    page = 1
    while True:
        batch = await _aopen_pulls_page(per_page, page)
        out.extend(batch)
        if len(batch) < per_page or page >= _PR_PAGE_CAP:
            return out
        page += 1


def _parse_open_pr_row(p: dict) -> dict:
    """One open-PR row shape - factored so open_prs and aopen_prs stay in lockstep."""
    return {
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
        # Label *names* (flattened from GitHub's [{name, color, url}, ...]).
        # Carried through open_prs so label checks - most notably the
        # poller's proposal-hold gate - can reuse the very row they already
        # fetched instead of issuing another API call per PR.
        "labels": [l.get("name", "") for l in (p.get("labels") or [])],
    }


def open_prs() -> list[dict]:
    """Open pull requests, newest first, cached briefly (PR_CACHE_SECONDS).
    Paginates past the first GITHUB_PRS_PER_PAGE so a busier repo stops
    silently dropping every open PR past page 1 (mirrors the closed-PR
    pagination added in 2.1).

    Rows carry the head sha and the parsed 'Citizen: ...' trailer alongside
    the usual fields - the CI-failure poller needs both and gets them with
    the same list call. ``citizen`` is a hint: ownership checks prefer
    db.pr_opener() (the record written from the forum token at open time).
    """
    cached = _core._open_prs_cache.get("open_prs", config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    try:
        pulls = _paginated_open_pulls(config.GITHUB_PRS_PER_PAGE)
        result = [_parse_open_pr_row(p) for p in pulls]
    except RepoError as exc:
        if _core._CACHE_FAILURES:
            _core._open_prs_cache.set("open_prs", exc)
        raise
    _core._open_prs_cache.set("open_prs", result)
    return result


async def aopen_prs() -> list[dict]:
    """Native-await twin of open_prs - same cache key, same row shape.
    Cold fetch runs through the page loop natively."""
    cached = _core._open_prs_cache.get("open_prs", config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    try:
        pulls = await _apaginated_open_pulls(config.GITHUB_PRS_PER_PAGE)
        result = [_parse_open_pr_row(p) for p in pulls]
    except RepoError as exc:
        if _core._CACHE_FAILURES:
            _core._open_prs_cache.set("open_prs", exc)
        raise
    _core._open_prs_cache.set("open_prs", result)
    return result


def list_repo_labels() -> list[str]:
    """Every repo-level label *definition* (names only), paged.  Distinct
    from the labels sitting on a given PR: definitions persist in the repo
    even after they are unlinked from every issue, which is exactly why
    the vote-label GC sweeps them (see server.poller._sweep_orphan_vote_labels)."""
    return [l.get("name", "") for l in _paginated_get("labels")]


def open_pr_labels() -> set[str]:
    """Label names currently applied to ANY open pull request, as a set.
    One open-PR listing carries every PR's labels, so the whole set is a
    single paged fetch.  Used by cleanup sweeps to decide whether a
    repo-level label definition is still 'live' (referenced by an open PR)
    before deleting it."""
    labels: set[str] = set()
    page = 1
    while True:
        batch = _core._request(
            "GET", f"pulls?state=open&per_page={_PR_PAGE_SIZE}&page={page}"
        )
        for p in batch:
            for l in p.get("labels") or []:
                labels.add(l.get("name", ""))
        if len(batch) < _PR_PAGE_SIZE or page >= _PR_PAGE_CAP:
            return labels
        page += 1


def _closed_pulls_page(state: str, per_page: int, page: int) -> list:
    """One page of the closed/all pulls listing, newest by updated."""
    per_page = min(per_page, _MAX_GITHUB_PERPAGE)
    return _core._request(
        "GET",
        f"pulls?state={state}&sort=updated&direction=desc&per_page={per_page}&page={page}",
    )


async def _aclosed_pulls_page(state: str, per_page: int, page: int) -> list:
    """Native-await twin of _closed_pulls_page."""
    per_page = min(per_page, _MAX_GITHUB_PERPAGE)
    return await _core._on_bg(
        _core._arequest(
            "GET",
            f"pulls?state={state}&sort=updated&direction=desc&per_page={per_page}&page={page}",
        )
    )


def _paginated_closed_pulls(state: str, per_page: int) -> list:
    """Every page of the closed/all pulls listing, newest by updated, stopping
    at the first short page. The single-page read (2.1) silently dropped every
    PR past the newest page once closed PRs outgrew one page; a page loop
    bounded by _PR_PAGE_CAP makes a full listing explicit and complete.
    Clamp per_page to GitHub's 100 cap so the short-page stop can't be fooled
    by a caller (or FORUM_GITHUB_PRS_PER_PAGE) above it."""
    per_page = min(per_page, _MAX_GITHUB_PERPAGE)
    out: list = []
    page = 1
    while True:
        batch = _closed_pulls_page(state, per_page, page)
        out.extend(batch)
        if len(batch) < per_page or page >= _PR_PAGE_CAP:
            return out
        page += 1


async def _apaginated_closed_pulls(state: str, per_page: int) -> list:
    """Native-await twin of _paginated_closed_pulls."""
    per_page = min(per_page, _MAX_GITHUB_PERPAGE)
    out: list = []
    page = 1
    while True:
        batch = await _aclosed_pulls_page(state, per_page, page)
        out.extend(batch)
        if len(batch) < per_page or page >= _PR_PAGE_CAP:
            return out
        page += 1


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
    # DB fast-path: the closed/all listing is served from pr_rows when the
    # cache is populated (returns None when unpopulated - a fresh database
    # must never read 'no PRs'). 'all' composes live open PRs with the DB's
    # closed rows; failure anywhere falls back to the live GitHub listing.
    db_rows: list[dict] | None = None
    try:
        import db

        db_rows = db.list_pr_rows(state, since=since)
    except Exception:  # domain: degrade-silently - cache is optional enrichment
        db_rows = None
    if db_rows is not None and state == "closed":
        return db_rows
    if db_rows is not None and state == "all":
        try:
            open_rows = open_prs()
        except Exception:  # domain: degrade-silently - open half stays live-or-empty
            open_rows = []
        combined = [_list_row_for_open(r) for r in open_rows] + db_rows
        if since:
            combined = [
                r
                for r in combined
                if (r["updated_at"] or r["created_at"] or "") >= since
            ]
        combined.sort(
            key=lambda r: (r["updated_at"] or r["created_at"] or "", r["number"]),
            reverse=True,
        )
        return combined
    pulls = _paginated_closed_pulls(state, config.GITHUB_PRS_PER_PAGE)
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
        rows = await aopen_prs()
        return [r for r in rows if r["created_at"] >= since] if since else rows
    # DB fast-path: mirror of list_prs's - pr_rows serves the closed/all
    # listing when populated; 'all' composes live open PRs with DB closed
    # rows; any failure falls back to the live GitHub listing.
    db_rows: list[dict] | None = None
    try:
        import db

        db_rows = db.list_pr_rows(state, since=since)
    except Exception:  # domain: degrade-silently - cache is optional enrichment
        db_rows = None
    if db_rows is not None and state == "closed":
        return db_rows
    if db_rows is not None and state == "all":
        try:
            open_rows = await aopen_prs()
        except Exception:  # domain: degrade-silently - open half stays live-or-empty
            open_rows = []
        combined = [_list_row_for_open(r) for r in open_rows] + db_rows
        if since:
            combined = [
                r
                for r in combined
                if (r["updated_at"] or r["created_at"] or "") >= since
            ]
        combined.sort(
            key=lambda r: (r["updated_at"] or r["created_at"] or "", r["number"]),
            reverse=True,
        )
        return combined
    pulls = await _apaginated_closed_pulls(state, config.GITHUB_PRS_PER_PAGE)
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


def _closed_row_from_raw(p: dict) -> dict:
    """One closed/all-PR row in the feed's life-cycle shape - factored so
    recently_closed_prs, arecently_closed_prs and the pr_rows backfill
    stay in lockstep. Carries the label names, the outcome vocabulary
    (merged / declined / closed), the decline-reason suffix and the
    forum-side stamps (Citizen trailer + 'Proposal: #N') the outcome
    poller records from."""
    labels = [label["name"] for label in (p.get("labels") or [])]
    return {
        "number": p["number"],
        "title": p["title"],
        "body": p.get("body") or "",
        "head": (p.get("head") or {}).get("ref", ""),
        "head_sha": (p.get("head") or {}).get("sha", ""),
        "base": (p.get("base") or {}).get("ref", ""),
        "author": (p.get("user") or {}).get("login"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "state": p.get("state"),
        "merged_at": p.get("merged_at"),
        "closed_at": p.get("closed_at"),
        "html_url": p.get("html_url", ""),
        "labels": labels,
        "declined": _pr_outcome(p) == "declined",
        "decline_reason": _parse_decline_reason(p),
        "citizen": _parse_citizen(p.get("body") or ""),
        "proposal_post_id": _parse_proposal(p.get("body") or ""),
    }


def _list_row_for_open(r: dict) -> dict:
    """Normalize one open_prs() row (the open view's shape) into the
    closed/all listing's life-cycle shape, so 'all' compression keeps one
    row vocabulary. `updated_at` is not in the open view - None forces the
    compose sort to fall back on created_at (DB rows carry updated_at and
    win the natural ordering)."""
    return {
        "number": r["number"],
        "title": r["title"],
        "head": r["head"],
        "base": r["base"],
        "author": r["author"],
        "created_at": r["created_at"],
        "updated_at": None,
        "state": "open",
        "merged_at": None,
        "closed_at": None,
        "outcome": "open",
        "html_url": r["html_url"],
    }


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
    # The outcome poller's ingest is a *recent* read, not a full historic
    # scan: it re-processes the newest closed PRs each sweep and relies on
    # INSERT OR IGNORE idempotency for anything already recorded. Full
    # pagination here would re-record every historical PR's karma for real
    # citizens on a fresh database (2.1 pagination belongs on the user-facing
    # list_prs closed/all listing, not this poller feed), so fetch one page.
    pulls = _closed_pulls_page("closed", per_page, 1)
    return [_closed_row_from_raw(p) for p in pulls]


async def arecently_closed_prs(
    per_page: int = config.GITHUB_PRS_PER_PAGE,
) -> list[dict]:
    """Native-await twin of recently_closed_prs - the outcome poller's hot
    fetch, now off the worker threads entirely."""
    pulls = await _aclosed_pulls_page("closed", per_page, 1)
    return [_closed_row_from_raw(p) for p in pulls]


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


_VALID_DECLINE_REASONS = frozenset({"fault", "infra", "proof"})


def _pr_outcome(pr: dict) -> str:
    """Classify one GitHub pull request as 'open', 'merged', 'declined' or
    'closed' - merged when `merged_at` is set, declined when a 'declined'
    label is attached (including suffixed forms like 'declined:fault'),
    closed-other otherwise. Mirrors the vocabulary of a proposal's
    lifecycle in db."""
    if pr.get("state") != "closed":
        return "open"
    if pr.get("merged_at"):
        return "merged"
    labels = [label.get("name", "") for label in (pr.get("labels") or [])]
    return (
        "declined"
        if any(label.lower().startswith("declined") for label in labels)
        else "closed"
    )


def _parse_decline_reason(pr: dict) -> str:
    """Extract the decline-reason suffix from a 'declined' label on a PR.

    Recognized suffixes: ``fault``, ``infra``, ``proof``.  A bare
    ``declined`` label (or an unrecognised suffix) maps to
    ``'unspecified'``.  Returns ``''`` when the PR was not declined."""
    if _pr_outcome(pr) != "declined":
        return ""
    labels = [label.get("name", "") for label in (pr.get("labels") or [])]
    for label in labels:
        low = label.lower()
        if low.startswith("declined"):
            suffix = low[len("declined") :].lstrip(":").strip()
            return suffix if suffix in _VALID_DECLINE_REASONS else "unspecified"
    return "unspecified"


async def aconditional_raw_pr(
    number: int, etag: str | None = None
) -> tuple[dict | None, str | None]:
    """Conditional fetch of one PR's raw header: carries If-None-Match when
    a cached ETag is known so an unchanged PR answers 304 without a body.
    Returns (payload, etag): payload is the parsed JSON on a 200 (None on a
    304 / empty 2xx), etag is the validator to store from a 200 (None when
    the server sent none). The repo_get_pr revalidation seam uses this so
    the closed-PR header read is served from the DB once cached - a 304
    costs one small request instead of the full composite - while a 2xx
    refreshes both the payload and its validator."""
    return await _core._on_bg(
        _core._arequest_with_etag("GET", f"pulls/{number}", etag=etag)
    )


def _synthetic_pr_raw(row: dict) -> dict:
    """Rebuild the minimal raw GitHub PR header the composite readers need
    from a cached pr_rows row - the 304 branch of revalidation, where the
    cached copy is provably current so the whole composite (checks, files,
    comments) reads as far as its other caches allow. The `head`/`base`
    refs are exact (stored live) and `head.sha` rides in the cache row too
    (immutable for a closed PR, so the stored value is still exact), so the
    checks chain resolves against the real commit."""
    return {
        "number": row["number"],
        "title": row["title"],
        "body": row["body"],
        "head": {"ref": row["head"], "sha": row["head_sha"]},
        "base": {"ref": row["base"]},
        "user": {"login": row["author"]},
        "state": row["state"] or "closed",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "merged_at": row["merged_at"],
        "closed_at": row["closed_at"],
        "html_url": row["html_url"],
        "labels": [{"name": label} for label in row["labels"]],
    }


def _pr_raw(number: int) -> dict:
    """The raw GitHub /pulls/{number} payload, TTL-cached under its own key so
    the several reads that need it (get_pr, pr_diff, pr_has_label) share one
    API call per PR_CACHE_SECONDS window instead of each fetching it afresh
    (Item B of the rate-limit reduction).  Lives here in the read package
    because it is purely a read; the poller's proposal-hold gate calls it
    through ``pr_has_label`` only when a caller did not already hold the row.
    """
    cache_key = ("pr_raw", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr = _core._request("GET", f"pulls/{number}")
    _core._pr_cache.set(cache_key, pr)
    return pr


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
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr = _pr if _pr is not None else _pr_raw(number)
    checks = _checks._checks_for_head(pr["head"]["sha"])
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
    _core._pr_cache.set(cache_key, result)
    return result


def pr_diff(number: int) -> dict:
    """One pull request's diff as per-file sections with add/delete counts
    (the shape of GitHub's files endpoint), so a citizen reviewing a change
    gets the map before the lines. Each section carries the path, status,
    the add/delete counts, and the unified-diff `patch` text; binary files
    come back with no patch (None).

    Cached for PR_CACHE_SECONDS (default 30 s)."""
    cache_key = ("pr_diff", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr = _pr_raw(number)
    # GitHub pages the files endpoint at 100 per request; page through so a
    # large PR's diff is never silently truncated at the first page.
    files: list[dict] = []
    page = 1
    while True:
        batch = _core._request("GET", f"pulls/{number}/files?per_page=100&page={page}")
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
    _core._pr_cache.set(cache_key, result)
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
        batch = _core._request("GET", f"{path}?per_page={_PR_PAGE_SIZE}&page={page}")
        out.extend(batch)
        if len(batch) < _PR_PAGE_SIZE or page >= _PR_PAGE_CAP:
            return out
        page += 1


async def _apaginated_get(path: str) -> list:
    """Async twin of _paginated_get for the native read surface."""
    out: list = []
    page = 1
    while True:
        batch = await _core._arequest(
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
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
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
    _core._pr_cache.set(cache_key, result)
    return result


def pr_comments(number: int) -> list[dict]:
    """All comments on a pull request, newest first.  Two GitHub sources:
    `issue` comments (the conversation thread repo_comment_on_pr writes to)
    and `review` comments (inline notes on specific diff lines). Both
    sources are paginated (per_page=100) so long conversations are not
    silently truncated at GitHub's default 30-item page.

    Cached for PR_CACHE_SECONDS (default 30 s)."""
    cache_key = ("pr_comments", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    comments: list[dict] = []
    for kind, path in (
        ("issue", f"issues/{number}/comments"),
        ("review", f"pulls/{number}/comments"),
    ):
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
    _core._pr_cache.set(cache_key, comments)
    return comments


def pr_commits(number: int) -> dict:
    """One pull request's commits, oldest first - sha, message, author name
    and date - so a reviewer can audit the change shape (one commit per
    file), trace a fix trail onto the final head, and see who actually
    committed. Paginated like pr_diff so no commit is silently dropped.

    Cached for PR_CACHE_SECONDS (default 30 s)."""
    cache_key = ("pr_commits", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr = _core._request("GET", f"pulls/{number}")
    commits = _paginated_get(f"pulls/{number}/commits")
    result = {
        "number": number,
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "commits": [
            {
                "sha": c["sha"],
                "message": (c.get("commit") or {}).get("message") or "",
                "author_name": ((c.get("commit") or {}).get("author") or {}).get(
                    "name"
                ),
                "author_date": ((c.get("commit") or {}).get("author") or {}).get(
                    "date"
                ),
            }
            for c in commits
        ],
    }
    _core._pr_cache.set(cache_key, result)
    return result
