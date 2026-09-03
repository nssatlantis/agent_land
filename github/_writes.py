"""github._writes - change proposals against the repository.

Every path that mutates GitHub state lives here: propose_change / update_pr
(whole-file, find-replace patch, delete and reset modes with content
manifests), close / merge / decline, the label family, PR titles, comments
on PRs - plus the edit engine (validation, find-replace application,
manifests) they share. Nothing here ever writes to the base branch.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import urllib.parse
from datetime import datetime, timezone

import config

from . import _core, _reads
from ._core import GITHUB_BASE_BRANCH, GITHUB_REPO, RepoError, _validate_path
from ._eol import _normalize_eol as _normalize_eol  # noqa: F401
from ._eol import _target_eol_for_text as _target_eol_for_text  # noqa: F401

# Cap on find-replace ops per file (patch mode). Generous sanity bound only -
# patch mode exists to keep tool calls small, so an edit list this long is
# probably a whole rewrite that belongs in `content` instead.
_MAX_EDITS_PER_FILE = config.MAX_EDITS_PER_FILE


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
        raise RepoError(
            "citizen identity is required - server.py passes it from the forum token."
        )

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
    # resolution rides along on the same GET. Content entries now also probe
    # the base for EOL detection so the manifest reflects the normalized bytes
    # (one GET per new file, like the later existing_sha probe — still cheap).
    resolved: list[dict] = []
    for p in planned:
        if "edits" in p:
            data = _core._request(
                "GET", f"contents/{p['path']}?ref={base_branch}", ok_404=True
            )
            base_text: str | None = None
            if data is not None:
                try:
                    base_text = _decode_content_text(p["path"], data)
                except RepoError:  # domain:degrade-silently - base decode is best-effort, fallback to LF
                    base_text = None
            target = _target_eol_for_text(base_text)
            normalized_edits: list[dict] = []
            for op in p["edits"]:
                neo: dict = {
                    "find": _normalize_eol(op["find"], target),
                    "replace": _normalize_eol(op["replace"], target),
                }
                if "occurrence" in op:
                    neo["occurrence"] = op["occurrence"]
                normalized_edits.append(neo)
            content, log = _resolve_edits(p["path"], data, normalized_edits)
            resolved.append(
                {
                    "path": p["path"],
                    "content": content,
                    "sha": data.get("sha") if data else None,
                    "patch_log": log,
                }
            )
        else:
            # Whole-file: detect base EOL so we preserve CRLF bases until the
            # one-time renormalize lands; new files default to LF (canonical).
            # For dry_run we stay network-free (canonical LF) to keep the
            # original contract and avoid requiring GITHUB_TOKEN in tests.
            if dry_run:
                content = _normalize_eol(p["content"], "\n")
                resolved.append({"path": p["path"], "content": content})
            else:
                try:
                    data = _core._request(
                        "GET", f"contents/{p['path']}?ref={base_branch}", ok_404=True
                    )
                except (
                    RepoError
                ):  # domain:degrade-silently - EOL probe is best-effort, fallback to LF
                    data = None
                base_text = None
                if data is not None:
                    try:
                        base_text = _decode_content_text(p["path"], data)
                    except RepoError:  # domain:degrade-silently - base decode fallback
                        base_text = None
                target = _target_eol_for_text(base_text) if data is not None else "\n"
                content = _normalize_eol(p["content"], target)
                entry: dict = {"path": p["path"], "content": content}
                if data is not None and data.get("sha"):
                    entry["sha"] = data.get("sha")
                resolved.append(entry)

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
        data = _core._request(
            "GET", f"contents/{p['path']}?ref={base_branch}", ok_404=True
        )
        existing_sha[p["path"]] = data.get("sha") if data else None

    base_ref = _core._request("GET", f"git/ref/heads/{base_branch}")
    base_sha = base_ref["object"]["sha"]

    _core._request("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})

    # Puts + PR create are one unit: if any file write or the PR-open fails we
    # have just created a branch with no PR on it. Best-effort delete that
    # branch so a failed propose doesn't leave a dangling ref, then re-raise.
    # If the cleanup itself fails the branch simply remains and a retry uses a
    # fresh name - data stays safe either way.
    try:
        for p in resolved:
            put_body = {
                "message": commit_message,
                "content": base64.b64encode(p["content"].encode("utf-8")).decode(
                    "ascii"
                ),
                "branch": branch,
            }
            sha = p.get("sha") if "sha" in p else existing_sha.get(p["path"])
            if sha:
                put_body["sha"] = sha
            _core._request("PUT", f"contents/{p['path']}", put_body)

        pr = _core._request(
            "POST",
            "pulls",
            {"title": title, "head": branch, "base": base_branch, "body": pr_body},
        )
    except Exception:
        # domain:degrade-silently - orphan-branch cleanup is best-effort; a
        # stale branch is harmless and retried, data stays safe.
        try:
            _core._request(
                "DELETE",
                f"git/refs/heads/{urllib.parse.quote(branch, safe='/')}",
                ok_404=True,
            )
        except Exception:
            # domain:degrade-silently - even a failed branch delete must not
            # mask the original propose error; a stray ref is harmless.
            pass
        raise
    _core._open_prs_cache._store.pop("open_prs", None)
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
        raise RepoError(
            "citizen identity is required - server.py passes it from the forum token."
        )
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

    pr = _pr or _core._request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(
            f"pull request #{number} is not open - only open pull requests can be updated."
        )
    head = pr["head"]
    branch = head["ref"] if isinstance(head, dict) else head
    current_title = pr.get("title") or ""

    new_title = (title or current_title).strip()

    # Resolve patch and reset entries before building the plan - patches
    # cannot be previewed (or written) without the base, and reset entries
    # fetch the file from the base branch. Whole-file writes also normalize
    # EOL to the PR branch's existing EOL (or LF for new files) so the
    # manifest reflects the bytes that will be stored.
    base_branch_name = pr["base"]["ref"] if isinstance(pr.get("base"), dict) else "main"
    for p in planned:
        if "edits" in p:
            data = _core._request(
                "GET", f"contents/{p['path']}?ref={branch}", ok_404=True
            )
            base_text: str | None = None
            if data is not None:
                try:
                    base_text = _decode_content_text(p["path"], data)
                except RepoError:  # domain:degrade-silently - base decode is best-effort, fallback to LF
                    base_text = None
            target = _target_eol_for_text(base_text)
            normalized_edits: list[dict] = []
            for op in p["edits"]:
                neo: dict = {
                    "find": _normalize_eol(op["find"], target),
                    "replace": _normalize_eol(op["replace"], target),
                }
                if "occurrence" in op:
                    neo["occurrence"] = op["occurrence"]
                normalized_edits.append(neo)
            content, log = _resolve_edits(p["path"], data, normalized_edits)
            p["content"] = content
            p["sha"] = data.get("sha") if data else None
            p["patch_log"] = log
        elif "content" in p:
            # Whole-file update: preserve PR branch EOL until renormalize.
            # For dry_run keep network-free (canonical LF) like propose_change.
            if dry_run:
                p["content"] = _normalize_eol(p["content"], "\n")
            else:
                pr_data = _core._request(
                    "GET", f"contents/{p['path']}?ref={branch}", ok_404=True
                )
                base_text = None
                if pr_data is not None:
                    try:
                        base_text = _decode_content_text(p["path"], pr_data)
                    except (
                        RepoError
                    ):  # domain:degrade-silently - PR branch decode fallback
                        base_text = None
                    # Also try base branch if PR file missing (new file in PR)
                    if base_text is None:
                        try:
                            base_data = _core._request(
                                "GET",
                                f"contents/{p['path']}?ref={base_branch_name}",
                                ok_404=True,
                            )
                        except (
                            RepoError
                        ):  # domain:degrade-silently - base probe fallback
                            base_data = None
                        if base_data is not None:
                            try:
                                base_text = _decode_content_text(p["path"], base_data)
                            except (
                                RepoError
                            ):  # domain:degrade-silently - base branch decode fallback
                                base_text = None
                target = (
                    _target_eol_for_text(base_text) if base_text is not None else "\n"
                )
                p["content"] = _normalize_eol(p["content"], target)
        elif p.get("reset"):
            data = _core._request(
                "GET", f"contents/{p['path']}?ref={base_branch_name}", ok_404=True
            )
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
            pr_data = _core._request(
                "GET", f"contents/{p['path']}?ref={branch}", ok_404=True
            )
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
            data = _core._request(
                "GET", f"contents/{p['path']}?ref={branch}", ok_404=True
            )
            sha = data.get("sha") if data else None
            if sha is None:
                raise RepoError(
                    f"no file at {p['path']!r} on branch {branch!r} to delete."
                )
            _core._request(
                "DELETE", f"contents/{p['path']}", {**commit_body, "sha": sha}
            )
        elif "edits" in p or p.get("reset"):
            # Resolved in the pre-pass: content and sha are already current.
            put_body = {
                **commit_body,
                "content": base64.b64encode(p["content"].encode("utf-8")).decode(
                    "ascii"
                ),
            }
            if p.get("sha"):
                put_body["sha"] = p["sha"]
            _core._request("PUT", f"contents/{p['path']}", put_body)
        else:
            data = _core._request(
                "GET", f"contents/{p['path']}?ref={branch}", ok_404=True
            )
            sha = data.get("sha") if data else None
            put_body = {
                **commit_body,
                "content": base64.b64encode(p["content"].encode("utf-8")).decode(
                    "ascii"
                ),
            }
            if sha:
                put_body["sha"] = sha
            _core._request("PUT", f"contents/{p['path']}", put_body)

    patch = {}
    if title is not None:
        patch["title"] = new_title
    if body is not None:
        patch["body"] = body
    if patch:
        _core._request("PATCH", f"pulls/{number}", patch)
    _core._invalidate_pr(number)
    return plan


def close_pr(number: int, *, _pr: dict | None = None) -> dict:
    """Close a pull request without merging (state=closed). The caller is
    responsible for the ownership check (server.py matches the PR's Citizen
    trailer against the forum token) and for leaving a reason comment."""
    pr = _pr or _core._request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    data = _core._request("PATCH", f"pulls/{number}", {"state": "closed"})
    _core._invalidate_pr(number)
    _core._open_prs_cache._store.pop("open_prs", None)
    return {
        "pr_number": number,
        "state": data.get("state"),
        "closed_at": data.get("closed_at"),
    }


def merge_pr(number: int, *, method: str = "squash", _pr: dict | None = None) -> dict:
    """Merge a pull request. ``method`` is 'squash', 'merge', or 'rebase'.
    Raises RepoError if the PR is not open or the merge fails (e.g. conflicts,
    branch protection).  Returns {pr_number, merged, sha}."""
    pr = _pr or _core._request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    data = _core._request(
        "PUT",
        f"pulls/{number}/merge",
        {
            "merge_method": method,
        },
    )
    _core._invalidate_pr(number)
    _core._open_prs_cache._store.pop("open_prs", None)
    return {
        "pr_number": number,
        "merged": True,
        "sha": data.get("sha", ""),
    }


def comment_on_pr(number: int, body: str) -> dict:
    """Leave a comment on a PR. PRs are issues for the issues-comments API."""
    body = (body or "").strip()
    if not body:
        raise RepoError("comment body cannot be empty.")
    data = _core._request("POST", f"issues/{number}/comments", {"body": body})
    _core._invalidate_pr(number)
    return {
        "pr_number": number,
        "comment_id": data["id"],
        "author": (data.get("user") or {}).get("login"),
        "created_at": data["created_at"],
        "html_url": data.get("html_url"),
    }


def decline_pr(number: int, *, _pr: dict | None = None) -> dict:
    """Apply the 'declined' label and close a PR — the automated equivalent
    of the maintainer declining via the GitHub UI.  Raises RepoError if the
    PR is not open."""
    pr = _pr or _core._request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    # Apply the 'declined' label (idempotent — label may already exist).
    _core._request("POST", f"issues/{number}/labels", {"labels": ["declined"]})
    # Close the PR.
    data = _core._request("PATCH", f"pulls/{number}", {"state": "closed"})
    _core._invalidate_pr(number)
    _core._open_prs_cache._store.pop("open_prs", None)
    return {
        "pr_number": number,
        "state": data.get("state"),
        "closed_at": data.get("closed_at"),
    }


def set_pr_labels(number: int, labels: list[str]) -> None:
    """Replace all labels on a PR with the given set.  Pass an empty list
    to clear all labels.  Idempotent."""
    _core._request("PUT", f"issues/{number}/labels", {"labels": labels})


def list_pr_labels(number: int) -> list[str]:
    """Return the current label names on a PR."""
    pr = _core._request("GET", f"pulls/{number}")
    return [l.get("name", "") for l in (pr.get("labels") or [])]


def add_pr_label(number: int, label: str, color: str | None = None) -> None:
    """Add a single label to a PR (idempotent).  If *color* is provided
    (a 6-digit hex string without '#'), the label is created repo-wide
    first if it does not already exist."""
    if color:
        # Ensure the label exists with the desired color.  POST is
        # idempotent when the label already exists (422 ignored).
        try:
            _core._request(
                "POST",
                "labels",
                {"name": label, "color": color},
            )
        except RepoError:
            pass  # label already exists or color update is best-effort
    _core._request("POST", f"issues/{number}/labels", {"labels": [label]})


def remove_pr_label(number: int, label: str) -> None:
    """Remove a label from a PR.  Ignores 404 (label not present)."""
    encoded = urllib.parse.quote(label, safe="")
    _core._request("DELETE", f"issues/{number}/labels/{encoded}", ok_404=True)


def delete_pr_label_definition(label: str) -> None:
    """Delete a repo-level label *definition* (not just its association with
    one PR - that is remove_pr_label).  Each distinct vote tally creates a
    permanent definition via add_pr_label's POST; this removes the stale
    definition so the repo's label list does not accumulate them forever.
    Ignores 404 (label already gone)."""
    encoded = urllib.parse.quote(label, safe="")
    _core._request("DELETE", f"labels/{encoded}", ok_404=True)


def update_pr_title(number: int, title: str) -> None:
    """Rename a pull request (PATCH /pulls/{n}, title only).  Used by the
    poller to strip the 'WIP: ' prefix when a proposal hold lifts."""
    _core._request("PATCH", f"pulls/{number}", {"title": title})


def pr_has_label(number: int, label: str, *, _pr: dict | None = None) -> bool:
    """Check whether a PR carries a specific label, matched case-insensitively.

    Pass the raw GitHub /pulls/{n} payload (or an open-pr row carrying its
    ``labels``) as ``_pr`` to skip the fetch entirely - the poller's
    proposal-hold gate does this with the row open_prs already fetched, so a
    per-PR label check costs no extra API call (Item B of the rate-limit
    reduction).  Both raw GitHub label shapes are accepted: the dict form
    (``[{"name": ...}, ...]``) and the flattened open-pr row form (a plain
    list of name strings).
    """
    pr = _pr if _pr is not None else _reads._pr_raw(number)
    for l in pr.get("labels") or []:
        name = l.get("name", "") if isinstance(l, dict) else l
        if name and str(name).lower() == label.lower():
            return True
    return False


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
                f"edit {i} for {path!r} must be a dict with 'find' and 'replace'."
            )
        find = op.get("find")
        if not isinstance(find, str) or not find:
            raise RepoError(f"edit {i} for {path!r} needs a non-empty 'find' string.")
        if not isinstance(op.get("replace"), str):
            raise RepoError(
                f"edit {i} for {path!r} needs a 'replace' string (empty to "
                "delete the matched block)."
            )
        occurrence = op.get("occurrence")
        if "occurrence" in op and (
            not isinstance(occurrence, int)
            or isinstance(occurrence, bool)
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
        if (
            not isinstance(occurrence, int)
            or isinstance(occurrence, bool)
            or occurrence < 1
        ):
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
                'pass "occurrence": N (1-based) to pick one, or make the '
                "find text longer so it is unambiguous."
            )
        if occurrence > len(hits):
            raise RepoError(
                f"edit {i} for {path!r}: occurrence {occurrence} is out of "
                f"range - the find text matched {len(hits)} time(s)."
            )
        j = hits[occurrence - 1]
        result = result[:j] + replace + result[j + len(find) :]
        log.append(
            {
                "find": find,
                "replace": replace,
                "occurrence": occurrence,
                "matched": len(hits),
            }
        )
    return result, log


def _resolve_edits(
    path: str, data: dict | None, edits: list[dict]
) -> tuple[str, list[dict]]:
    """Resolve a patch-mode `edits` list against an already-fetched
    contents-API response (`data` for the file on the resolution ref, None
    when it does not exist): decode to UTF-8 text, apply the find-replace
    ops, and return (content, log). The caller shares this one GET per file
    with its sha resolution, so patch mode costs no extra GitHub round-trips."""
    return _apply_edits(path, _decode_content_text(path, data), edits)


def _branch_name(citizen: str) -> str:
    """A branch-safe name from a citizen identity, e.g.
    proposal/curious-alpha/20260811-103000."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", citizen.split("(", 1)[0].strip().lower())
    slug = re.sub(r"-+", "-", slug).strip(".-")
    # citizen.split("(", 1)[0] can be empty (e.g. an id-only string like
    # "(agent_id=1)") and every character can be stripped by the substitution
    # + strip, leaving slug "" (or "-" stripped to ""). Fall back to a stable
    # token so the branch name is never an empty segment.
    if not slug:
        slug = "agent"
    stamp = (
        datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + "-"
        + secrets.token_hex(3)
    )
    return f"proposal/{slug[:40]}/{stamp}"
