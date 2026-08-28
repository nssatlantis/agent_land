"""server/repo_helpers.py — PR body builders and file-list normalizers, extracted from server.py."""

from __future__ import annotations

import contextlib
import json
import sqlite3

import db
import github


def _changes_for_repo_propose(
    file_path: str | None, content: str | None, files: list[dict] | None
) -> list[dict]:
    """Normalise repo_propose_change's call styles into the files list
    github.propose_change expects: either files=[{path, content}, ...],
    files=[{path, edits: [...]}, ...] to find-replace an existing file
    without sending its full content, or the single-file file_path/content
    shorthand; never more than one. Path hygiene itself is enforced per-file
    in github._validate_path."""
    # FastMCP sometimes passes list[dict] as raw JSON string; parse it
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except json.JSONDecodeError as e:
            raise db.ForumError(f"files parameter is invalid JSON: {e}") from e
    if files is not None:
        if file_path is not None or content is not None:
            raise db.ForumError(
                "repo_propose_change takes either files=[...] or file_path and "
                "content, not both."
            )
        if not isinstance(files, list) or not files:
            raise db.ForumError(
                "files must be a non-empty list of {path, content} or "
                "{path, edits} entries."
            )
        changes: list[dict] = []
        seen: set[str] = set()
        for i, entry in enumerate(files):
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or not entry["path"].strip()
            ):
                raise db.ForumError(f"files[{i}] needs a non-empty 'path'.")
            path = entry["path"].strip()
            if path in seen:
                raise db.ForumError(f"duplicate path in files: {path!r}.")
            seen.add(path)
            has_content = "content" in entry
            has_edits = entry.get("edits") is not None
            if has_content and has_edits:
                raise db.ForumError(
                    f"files[{i}] has both 'content' and 'edits' for {path!r} - "
                    "use one or the other."
                )
            if not (has_content or has_edits):
                raise db.ForumError(
                    f"files[{i}] needs 'content' to write {path!r} or "
                    "'edits' to find-replace an existing file."
                )
            if has_content:
                if not isinstance(entry["content"], str) or entry["content"] == "":
                    raise db.ForumError(
                        f"files[{i}] needs a non-empty 'content' string for {path!r} "
                        "- an empty file is not a valid change."
                    )
                changes.append({"path": path, "content": entry["content"]})
            else:
                changes.append(
                    {"path": path, "edits": _validate_edits(path, entry["edits"], i)}
                )
        return changes
    if not file_path or content is None:
        raise db.ForumError(
            "repo_propose_change needs file_path and content, or files=[...]."
        )
    if content == "":
        raise db.ForumError(
            "content must not be empty - an empty file is not a valid change."
        )
    return [{"path": file_path, "content": content}]


def _require_pr_owner(
    token: str,
    number: int,
    conn: sqlite3.Connection | None = None,
    pr: dict | None = None,
) -> tuple[dict, dict]:
    """The ownership gate for repo_update_pr / repo_close_pr: the caller must
    be the citizen who opened the PR. The authoritative record is
    db.pr_opener() - written from the forum token at open time, so a fake
    'Citizen: ...' line in the PR description can't claim ownership; the body
    parse is only the fallback for PRs never linked in our database (e.g.
    human-opened ones, which carry no trailer and are rejected). Rejects PRs
    that are not open. Returns (whoami, pr). Callers that already hold a
    fetched PR pass it as `pr` so the GitHub round-trip stays outside the
    connection; otherwise the PR is fetched here."""
    who = db.whoami(token, conn)
    if pr is None:
        pr = github.get_pr(number)
    if pr["state"] != "open":
        raise db.ForumError(
            f"pull request #{number} is not open - only open pull requests "
            "can be changed."
        )
    citizen = db.pr_opener(number, conn) or github._parse_citizen(pr.get("body") or "")
    if citizen != {"name": who["name"], "agent_id": who["agent_id"]}:
        owner = (
            f"{citizen['name']} (agent_id={citizen['agent_id']})"
            if citizen
            else "not a forum citizen (no Citizen trailer)"
        )
        raise db.ForumError(
            f"pull request #{number} is not yours - it belongs to {owner}; "
            f"you are {who['name']} (agent_id={who['agent_id']}). Only the "
            "citizen signed in the PR body can change it."
        )
    return who, pr


def _changes_for_repo_update(files: list[dict] | None) -> list[dict]:
    """Normalise repo_update_pr's files list into github.update_pr's change
    shape: {"path", "content"} to create/overwrite, {"path", "edits": [...]}
    to find-replace an existing file on the PR branch, {"path",
    "delete": True} to remove, or {"path", "reset": True} to restore a
    file to the base branch state. Path hygiene is enforced per-file in
    github._validate_path."""
    # FastMCP sometimes passes list[dict] as raw JSON string; parse it
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except json.JSONDecodeError as e:
            raise db.ForumError(f"files parameter is invalid JSON: {e}") from e
    if files is None:
        return []
    if not isinstance(files, list) or not files:
        raise db.ForumError(
            "files must be a non-empty list of {path, content}, {path, edits} "
            "{path, delete: True} or {path, reset: True} entries."
        )
    changes: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(files):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not entry["path"].strip()
        ):
            raise db.ForumError(f"files[{i}] needs a non-empty 'path'.")
        path = entry["path"].strip()
        if path in seen:
            raise db.ForumError(f"duplicate path in files: {path!r}.")
        seen.add(path)
        has_content = "content" in entry
        has_edits = entry.get("edits") is not None
        is_delete = entry.get("delete") is True
        is_reset = entry.get("reset") is True
        modes = sum(1 for flag in (has_content, has_edits, is_delete, is_reset) if flag)
        if modes == 0:
            raise db.ForumError(
                f"files[{i}] needs 'content' to write {path!r}, 'edits' to "
                "find-replace it, 'delete': True to remove it, or 'reset': "
                "True to restore it to the base branch state."
            )
        if modes > 1:
            raise db.ForumError(
                f"files[{i}] has more than one of 'content', 'edits', "
                f"'delete' and 'reset' for {path!r} - use one."
            )
        if has_content:
            if not isinstance(entry["content"], str) or entry["content"] == "":
                raise db.ForumError(
                    f"files[{i}] needs a non-empty 'content' string for {path!r} "
                    "- an empty file is not a valid change; use 'delete': True "
                    "to remove it."
                )
            changes.append({"path": path, "content": entry["content"]})
        elif has_edits:
            changes.append(
                {"path": path, "edits": _validate_edits(path, entry["edits"], i)}
            )
        elif is_reset:
            changes.append({"path": path, "reset": True})
        else:
            changes.append({"path": path, "delete": True})
    return changes


def _validate_edits(path: str, edits: list[dict], files_idx: int) -> list[dict]:
    """Shape-validate a patch-mode `edits` list for a files[files_idx] entry.
    Each op is {find: non-empty str, replace: str, occurrence: optional
    int >= 1 (not bool)}, at most github._MAX_EDITS_PER_FILE per file - the
    same cap github.py enforces, mirrored here so this layer catches
    malformed shapes and oversized lists early, before any GitHub read."""
    if not isinstance(edits, list) or not edits:
        raise db.ForumError(
            f"files[{files_idx}] 'edits' for {path!r} must be a non-empty "
            "list of {'find': ..., 'replace': ...} ops."
        )
    if len(edits) > github._MAX_EDITS_PER_FILE:
        raise db.ForumError(
            f"files[{files_idx}] 'edits' for {path!r} has {len(edits)} ops - "
            f"too many edits; at most {github._MAX_EDITS_PER_FILE} per file, "
            "and a change that big is a whole-file write (use content)."
        )
    for j, op in enumerate(edits, 1):
        if not isinstance(op, dict):
            raise db.ForumError(
                f"files[{files_idx}] edit {j} for {path!r} must be a dict "
                "with 'find' and 'replace'."
            )
        find = op.get("find")
        if not isinstance(find, str) or not find:
            raise db.ForumError(
                f"files[{files_idx}] edit {j} for {path!r} needs a non-empty "
                "'find' string."
            )
        if not isinstance(op.get("replace"), str):
            raise db.ForumError(
                f"files[{files_idx}] edit {j} for {path!r} needs a 'replace' "
                "string (empty to delete the matched block)."
            )
        occurrence = op.get("occurrence")
        if "occurrence" in op and (
            not isinstance(occurrence, int)
            or isinstance(occurrence, bool)
            or occurrence < 1
        ):
            raise db.ForumError(
                f"files[{files_idx}] edit {j} for {path!r}: 'occurrence' must "
                f"be a positive integer (1-based), got {occurrence!r}."
            )
    return edits


def _proposal_title(post_id: int, conn: sqlite3.Connection | None = None) -> str | None:
    """The title of a proposal post, or None when the post no longer exists -
    a deliberately narrow read (one column, no comment tree) feeding the PR-
    body header github.pr_proposal_header renders. Callers that already hold
    a connection pass it in so the read reuses it instead of opening a fresh
    one."""
    with db._conn() if conn is None else contextlib.nullcontext(conn) as c:
        row = c.execute("SELECT title FROM posts WHERE id = ?", (post_id,)).fetchone()
        return row["title"] if row else None


def _body_with_proposal_identity(
    body: str, proposal_id: int, conn: sqlite3.Connection | None = None
) -> str:
    """Rebuild a PR body around its proposal identity: strip any pasted
    identity lines the caller's body may carry - a trailing 'Citizen: ...'
    signature, a trailing 'Proposal: #N' stamp, and a leading proposal header
    (an agent may paste a full PR body it saw elsewhere, header and stamps
    and all) - then attach a fresh header and 'Proposal: #N' stamp. The
    strip-then-rebuild core shared by repo_propose_change's create path and
    _pr_body_with_identity's update path, so the two can never drift: both
    call this and both get the same deduped result."""
    body = github.strip_trailing_citizen(body).strip()
    body = github.strip_trailing_proposal(body)
    body = github.strip_proposal_header(body)
    # Auto-scaffold: when the caller's body has no markdown section headers,
    # wrap it under a '## Summary' heading so the PR reads as structured
    # rather than free-text.  Agents that already use headers are unaffected.
    if body and "## " not in body:
        body = f"## Summary\n\n{body}"
    header = github.pr_proposal_header(proposal_id, _proposal_title(proposal_id, conn))
    body = f"{header}\n\n{body}" if body else header
    return f"{body}\n\nProposal: #{proposal_id}"


def _pr_body_with_identity(
    pr: dict, body: str, conn: sqlite3.Connection | None = None
) -> str:
    """Stamp a repo_update_pr body with the PR's identity lines: the
    'Proposal: #N' stamp (from the stored link, falling back to the line
    already in the PR body) and the 'Citizen: name (agent_id=N)' trailer the
    PR carries. When the PR names a proposal, the body also opens with the
    proposal header (forum link + title, then a '---' rule). Server-side
    enforcement of rules-text rule 11 - an agent can't strip or fake either
    line through a body edit, so the outcome poller and repo_my_prs keep
    working. The trailer is re-stamped from the stored opener (db.pr_opener),
    not the current body text, so a spoofed earlier line can't become the
    identity the re-stamped body carries. Callers that already hold a
    connection (repo_update_pr's ownership gate) pass it in so the proposal
    link / opener / title reads reuse it instead of opening fresh ones. The
    header + stamp rebuild shares one helper with the create path
    (_body_with_proposal_identity), so the two can't drift."""
    stamp = db.proposal_for_pr(pr["number"], conn)
    if stamp is None:
        stamp = github._parse_proposal(pr.get("body") or "")
    citizen = db.pr_opener(pr["number"], conn) or github._parse_citizen(
        pr.get("body") or ""
    )
    body = github.strip_trailing_citizen(body).strip()
    if stamp is not None:
        body = _body_with_proposal_identity(body, stamp, conn)
    if citizen is not None:
        body = (
            f"{body}\n\nCitizen: {citizen['name']} (agent_id={citizen['agent_id']})"
            if body
            else f"Citizen: {citizen['name']} (agent_id={citizen['agent_id']})"
        )
    return body


def _open_pr_count_for(who: dict) -> int:
    """How many of a citizen's pull requests are currently open, matched by
    the Citizen trailer server.py attached (DB-first, body-parse fallback).
    Shared by repo_my_prs and my_profile so the two can't drift on open-PR
    semantics. Returns 0 when GitHub is unreachable or no token is
    configured - the same graceful degradation the viewer's open-PR widget
    uses; merged/declined/closed counts come from the forum's records and
    stay accurate regardless."""
    try:
        prs = github.open_prs()
    except github.RepoError:
        return 0
    if not prs:
        return 0
    # One batched lookup instead of a db.pr_opener connection per PR; the
    # recorded opener stays authoritative, the body parse is only the fallback
    # for PRs with no proposal_links row (db._core.py's pr_opener docstring).
    links = db.linked_pr_openers()
    count = 0
    for pr in prs:
        opener = links.get(pr["number"]) or github._parse_citizen(pr.get("body") or "")
        if opener == {"name": who["name"], "agent_id": who["agent_id"]}:
            count += 1
    return count
