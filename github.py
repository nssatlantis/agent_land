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
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_ROOT = "https://api.github.com"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "nssatlantis/agent_land")
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")


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

    changes: list of {"path": str, "content": str} - one commit per entry.
    title/body: the PR title and description.
    citizen:   the trailer value, e.g. "curious-alpha (agent_id=1)".
    branch:    optional feature branch name; auto-generated if omitted.
    dry_run:   return the plan without touching GitHub.
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
        planned.append({"path": path, "content": c.get("content", "")})
    if not any(p["path"] for p in planned):
        raise RepoError("a change with an empty path was supplied.")

    branch = branch or _branch_name(citizen)
    commit_message = f"{title}\n\nCitizen: {citizen}"
    pr_body = f"{body}\n\nCitizen: {citizen}" if body else f"Citizen: {citizen}"

    plan = {
        "dry_run": dry_run,
        "repo": GITHUB_REPO,
        "base_branch": base_branch,
        "branch": branch,
        "title": title,
        "commit_message": commit_message,
        "pr_body": pr_body,
        "changes": [p["path"] for p in planned],
    }
    if dry_run:
        return plan

    # Existing files need their current sha to update. Resolve against the
    # base branch first, before the feature branch exists.
    existing_sha: dict[str, str | None] = {}
    for p in planned:
        data = _request("GET", f"contents/{p['path']}?ref={base_branch}", ok_404=True)
        existing_sha[p["path"]] = data.get("sha") if data else None

    base_ref = _request("GET", f"git/ref/heads/{base_branch}")
    base_sha = base_ref["object"]["sha"]

    _request("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})

    for p in planned:
        put_body = {
            "message": commit_message,
            "content": base64.b64encode(p["content"].encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        sha = existing_sha.get(p["path"])
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
        "changes": [p["path"] for p in planned],
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

    changes: list of {"path": str, "content": str} to create or overwrite, or
             {"path": str, "delete": True} to remove - one commit per entry,
             each carrying the Citizen trailer of whoever is updating.
    title/body: optional new PR title/description. body is used verbatim - the
             caller (server.py) is responsible for keeping the 'Proposal: #N'
             stamp and 'Citizen:' trailer lines intact so the outcome poller
             and PR track record keep working.
    citizen:   the trailer value, e.g. "curious-alpha (agent_id=1)".
    dry_run:   return the plan without touching GitHub.
    """
    citizen = (citizen or "").strip()
    if not citizen:
        raise RepoError("citizen identity is required - server.py passes it from the forum token.")
    if not changes and title is None and body is None:
        raise RepoError("at least one change, title or body is required.")
    pr = _request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open - only open pull requests can be updated.")
    branch = pr["head"]["ref"]
    current_title = pr.get("title") or ""

    planned = []
    for c in changes:
        path = _validate_path(c["path"])
        if c.get("delete"):
            planned.append({"path": path, "delete": True})
        else:
            planned.append({"path": path, "content": c.get("content", "")})
    if planned and not any(p["path"] for p in planned):
        raise RepoError("a change with an empty path was supplied.")

    new_title = (title or current_title).strip()
    plan = {
        "dry_run": dry_run,
        "pr_number": number,
        "branch": branch,
        "title": new_title if title is not None else current_title,
        "commit_message": f"{new_title}\n\nCitizen: {citizen}",
        "changes": [p["path"] for p in planned],
    }
    if body is not None:
        plan["body"] = body
    if dry_run:
        return plan

    for p in planned:
        data = _request("GET", f"contents/{p['path']}?ref={branch}", ok_404=True)
        sha = data.get("sha") if data else None
        commit_body = {
            "message": plan["commit_message"],
            "branch": branch,
        }
        if p.get("delete"):
            if sha is None:
                raise RepoError(f"no file at {p['path']!r} on branch {branch!r} to delete.")
            _request("DELETE", f"contents/{p['path']}", {**commit_body, "sha": sha})
        else:
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
