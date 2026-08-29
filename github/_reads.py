"""github._reads - read-side surface of the society's GitHub client.

Tree/file reads, PR listings (open / closed / since-filtered), the citizen-
and proposal-stamp parsers and strippers shared with server.py, the per-PR
composite reads (get_pr / pr_diff / pr_files / pr_comments / pr_commits)
and their pagination helpers. Every function here is a pure read: writes
live in github._writes, local-git flows in github._gitops.
"""

from __future__ import annotations

import asyncio
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
def open_prs() -> list[dict]:
    """Open pull requests, newest first, cached briefly (PR_CACHE_SECONDS).

    Rows carry the head sha and the parsed 'Citizen: ...' trailer alongside
    the usual fields - the CI-failure poller needs both and gets them with
    the same list call. ``citizen`` is a hint: ownership checks prefer
    db.pr_opener() (the record written from the forum token at open time).
    """
    cached = _core._open_prs_cache.get("open_prs", config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    try:
        pulls = _core._request(
            "GET", f"pulls?state=open&per_page={config.GITHUB_PRS_PER_PAGE}"
        )
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
    pulls = _core._request(
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
    pulls = await _core._on_bg(
        _core._arequest(
            "GET",
            f"pulls?state={state}&sort=updated&direction=desc&per_page={config.GITHUB_PRS_PER_PAGE}",
        )
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
    pulls = _core._request(
        "GET", f"pulls?state=closed&sort=updated&direction=desc&per_page={per_page}"
    )
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
                "decline_reason": _parse_decline_reason(p),
                "citizen": _parse_citizen(p.get("body") or ""),
                "proposal_post_id": _parse_proposal(p.get("body") or ""),
            }
        )
    return closed


async def arecently_closed_prs(
    per_page: int = config.GITHUB_PRS_PER_PAGE,
) -> list[dict]:
    """Native-await twin of recently_closed_prs - the outcome poller's hot
    fetch, now off the worker threads entirely."""
    pulls = await _core._on_bg(
        _core._arequest(
            "GET", f"pulls?state=closed&sort=updated&direction=desc&per_page={per_page}"
        )
    )
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
                "decline_reason": _parse_decline_reason(p),
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
    pr = _pr or _core._request("GET", f"pulls/{number}")
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
    pr = _core._request("GET", f"pulls/{number}")
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
