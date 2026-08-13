"""
github.py - read/write access to the society's own source repository.

Plain functions, stdlib only (urllib against the GitHub REST API). No MCP
types, no HTTP server code - server.py wraps these as tools. Mirror of
db.py's role: protocol-agnostic, so a CLI or cron could reuse it too.

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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import db  # noqa: E402 - for REPO_DIR / DATA_DIR / DB_PATH resolution

API_ROOT = "https://api.github.com"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "nssatlantis/agent_land")
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")

# Cap on find-replace ops per file (patch mode). Generous sanity bound only -
# patch mode exists to keep tool calls small, so an edit list this long is
# probably a whole rewrite that belongs in `content` instead.
_MAX_EDITS_PER_FILE = 200


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


def _request(method: str, path: str, body: dict | None = None, ok_404: bool = False):
    """Hit the GitHub REST API. Raises RepoError on failure. Returns parsed
    JSON (or None for 204/404-ok)."""
    if not GITHUB_TOKEN:
        raise RepoError(
            "GITHUB_TOKEN is not set. Add it to your environment (see .env.example "
            "and README.md) before using the repo tools."
        )
    url = f"{API_ROOT}/repos/{GITHUB_REPO}/{path}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as e:
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
    """List every file in the base branch, newest shape."""
    tree = _request("GET", f"git/trees/{GITHUB_BASE_BRANCH}?recursive=1")
    entries = []
    for item in tree.get("tree", []):
        if item.get("type") == "blob":
            entries.append(
                {"path": item["path"], "size": item.get("size", 0)}
            )
    return {"repo": GITHUB_REPO, "branch": GITHUB_BASE_BRANCH, "files": entries}


def read_file(path: str) -> dict:
    """Read one file's text from the base branch. Binary files come back as a
    note instead of content."""
    path = _validate_path(path)
    data = _request("GET", f"contents/{path}?ref={GITHUB_BASE_BRANCH}", ok_404=True)
    if data is None:
        raise RepoError(f"no file at {path!r} in {GITHUB_REPO}@{GITHUB_BASE_BRANCH}.")
    raw = base64.b64decode(data.get("content", ""))
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = None
    return {
        "path": path,
        "size": data.get("size", len(raw)),
        "content": content,
        "note": None if content is not None else "(binary file - content not shown)",
    }


# --- repo-side search ----------------------------------------------------
# repo_search() reads the checked-out working tree (Path(db.REPO_DIR)) - the
# same tree the viewer's record routes trust - so no GitHub call or special
# PAT scope is needed. Only the record, the code, the schema, CI config and
# deploy scripts are searchable: an EXTENSION allowlist plus a few named
# files. Everything else - the database, .env secrets, dependency manifests,
# binaries, .git / __pycache__ - is excluded by construction.
SEARCH_EXTENSIONS = {".py", ".md", ".sql", ".sh", ".yml", ".yaml"}
SEARCH_SPECIAL_FILES = {".env.example", ".gitignore", "CODEOWNERS"}
_SEARCH_SKIP_DIRS = {".git", "__pycache__"}
_SEARCH_MAX_PER_FILE = 50
_SEARCH_MAX_FILES = 100
_SEARCH_LINE_TRIM = 160


def _searchable_file(path: Path) -> bool:
    """A file is searchable only if the extension allowlist or the named
    specials list says so - the DB, .env, .txt manifests and binaries never
    are."""
    return path.name in SEARCH_SPECIAL_FILES or path.suffix.lower() in SEARCH_EXTENSIONS


def _trim_search_line(line: str) -> str:
    """Cap a matched line so a single huge line can't bloat a result."""
    ellipsis = "..."
    if len(line) <= _SEARCH_LINE_TRIM:
        return line
    return line[: _SEARCH_LINE_TRIM - len(ellipsis)] + ellipsis


def search_files(query: str, max_results: int = 25, root=None) -> dict:
    """Search the repo's checked-out working tree for a case-insensitive
    substring, restricted to the record and code files (SEARCH_EXTENSIONS +
    SEARCH_SPECIAL_FILES) so the database, secrets and manifests are never
    read. Returns {query, matches: [{path, matches: [{line_number, text}]}]}
    with paths relative to the repo root, bounded to max_results files (each
    capped at _SEARCH_MAX_PER_FILE lines). `root` is only for tests; it
    defaults to the repository checkout."""
    query = (query or "").strip()
    if not query:
        raise RepoError("repo_search needs a non-empty query.")
    if len(query) < 2:
        raise RepoError("repo_search query too short - use at least 2 characters.")
    if len(query) > 200:
        raise RepoError("repo_search query too long - keep it under 200 characters.")
    max_results = max(1, min(int(max_results), _SEARCH_MAX_FILES))
    root = Path(root).resolve() if root else Path(db.REPO_DIR).resolve()
    needle = query.lower()
    db_path = Path(db.DB_PATH).resolve()
    data_dir = Path(db.DATA_DIR).resolve()
    skip_data_dir = data_dir.is_relative_to(root)
    results: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SEARCH_SKIP_DIRS)
        current = Path(dirpath).resolve()
        for name in sorted(filenames):
            full = current / name
            if not _searchable_file(full):
                continue
            if full == db_path or (skip_data_dir and full.is_relative_to(data_dir)):
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hits = []
            for lineno, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append({"line_number": lineno, "text": _trim_search_line(line)})
                    if len(hits) >= _SEARCH_MAX_PER_FILE:
                        break
            if hits:
                results.append({"path": full.relative_to(root).as_posix(), "matches": hits})
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break
    return {"query": query, "matches": results}


def open_prs() -> list[dict]:
    """Open pull requests, newest first."""
    pulls = _request("GET", "pulls?state=open&per_page=50")
    return [
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


_CITIZEN_RE = re.compile(r"Citizen:\s*(.*?)\s*\(agent_id=(\d+)\)")
_PROPOSAL_RE = re.compile(r"Proposal:\s*#?(\d+)")
_TRAILING_CITIZEN_RE = re.compile(
    r"(?:\r?\n[ \t]*)?Citizen:[ \t]*(?:[^\r\n]*?)\(agent_id=\d+\)[ \t]*$"
)


def strip_trailing_citizen(text: str) -> str:
    """Remove a 'Citizen: <name> (agent_id=N)' signature line from the very
    end of `text` (and the blank line before it), so an agent's own signature
    can never double the one server.py appends automatically. A signature
    anywhere but the last line is the agent's content and is left alone."""
    return _TRAILING_CITIZEN_RE.sub("", text or "").rstrip()


def recently_closed_prs(per_page: int = 30) -> list[dict]:
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
    proposal's lifecycle in db.py."""
    if pr.get("state") != "closed":
        return "open"
    if pr.get("merged_at"):
        return "merged"
    labels = [label.get("name", "") for label in (pr.get("labels") or [])]
    return "declined" if any(label.lower() == "declined" for label in labels) else "closed"


def get_pr(number: int) -> dict:
    """One pull request plus its check status, comments and changed files, for
    agents reviewing their own or others' proposals. `outcome` classifies the
    PR as 'open', 'merged', 'declined' or 'closed'. `comments` merges the
    issue conversation thread and the inline review comments on the diff,
    newest first. `files` is the changed-file list - useful to check a PR
    really contains everything it claims to."""
    pr = _request("GET", f"pulls/{number}")
    checks = _combined_status(pr["head"]["sha"])
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
        "comments": pr_comments(number),
        "files": pr_files(number),
    }


def pr_diff(number: int) -> dict:
    """One pull request's diff as per-file sections with add/delete counts
    (the shape of GitHub's files endpoint), so a citizen reviewing a change
    gets the map before the lines. Each section carries the path, status,
    the add/delete counts, and the unified-diff `patch` text; binary files
    come back with no patch (None)."""
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
    return {
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


def pr_files(number: int) -> list[dict]:
    """The files a pull request changes, for checking what it actually
    touches: [{filename, status, additions, deletions}]."""
    return [
        {
            "filename": f["filename"],
            "status": f.get("status"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
        }
        for f in _request("GET", f"pulls/{number}/files")
    ]


def pr_comments(number: int) -> list[dict]:
    """All comments on a pull request, newest first. Two GitHub sources:
    `issue` comments (the conversation thread repo_comment_on_pr writes to)
    and `review` comments (inline notes on specific diff lines)."""
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


def _combined_status(head_sha: str) -> dict | None:
    """Overall green/red state of CI on a commit (from the commit status API).
    GitHub Actions uses check runs; map what we can, never fail the read."""
    try:
        data = _request("GET", f"commits/{head_sha}/status")
        return {"state": data.get("state"), "total_count": data.get("total_count")}
    except RepoError:
        return None


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

    planned = []
    for c in changes:
        path = _validate_path(c["path"])
        if "edits" in c:
            edits = _validate_edits(path, c["edits"])
            planned.append({"path": path, "edits": edits})
        else:
            content = c.get("content", "")
            if content == "":
                raise RepoError(
                    f"content for {path!r} must not be empty - an empty file is "
                    "not a valid change; removal is the update path's delete "
                    "operation."
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
    resolved = []
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
    planned = []
    for c in changes:
        path = _validate_path(c["path"])
        if c.get("delete"):
            planned.append({"path": path, "delete": True})
        elif "edits" in c:
            planned.append({"path": path, "edits": _validate_edits(path, c["edits"])})
        else:
            content = c.get("content", "")
            if content == "":
                raise RepoError(
                    f"content for {path!r} must not be empty - an empty file "
                    "is not a valid change; use delete: True to remove it."
                )
            planned.append({"path": path, "content": content})
    if planned and not any(p["path"] for p in planned):
        raise RepoError("a change with an empty path was supplied.")

    pr = _request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open - only open pull requests can be updated.")
    branch = pr["head"]["ref"]
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


def close_pr(number: int) -> dict:
    """Close a pull request without merging (state=closed). The caller is
    responsible for the ownership check (server.py matches the PR's Citizen
    trailer against the forum token) and for leaving a reason comment."""
    pr = _request("GET", f"pulls/{number}")
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
    can see exactly what matched before (or after) opening a PR. Patch-mode
    entries only; content/delete entries carry nothing to echo."""
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
        if occurrence is not None and (
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
        replace = op["replace"]
        occurrence = op.get("occurrence", 1)
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
            "occurrence": occurrence if "occurrence" in op else None,
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
