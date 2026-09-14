"""github._workspaces — server-held per-claim working trees (proposal #472).

One directory per (agent, proposal, name) under
``agentland_ws/<slug>-claims/`` carrying a ``.workspace.json`` manifest.
This module owns the bytes only: the queryable record lives in
``db._workspace_claims`` and the MCP tools in
``server.tools.repo._workspace`` orchestrate the two (record first, tree
second, with a compensating release when the tree fails).

Contract: ``ensure_claim_tree`` clones or resumes but never auto-wipes
dirty work; a manifest owned by someone else rebuilds; every failure
raises ``RepoError`` (the ``_logged`` decorator maps it to a tool error).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time

import config

from . import _core
from ._core import GITHUB_BASE_BRANCH, GITHUB_REPO, RepoError, _validate_path
from ._gitops import (
    _git,
    _push_auth,
    _push_ref,
    _repo_url,
    _rm_readonly,
    _seed_identity,
    _try_clone_from_local,
)

_WS_CLAIM_RE = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
_MANIFEST = ".workspace.json"
_MANAGED = (_MANIFEST, _MANIFEST + ".tmp")


def _validate_claim_name(name: str) -> str:
    """Same shape as the record layer and the CI rehearsal trees."""
    name = str(name or "").strip()
    if not _WS_CLAIM_RE.fullmatch(name):
        raise RepoError(
            "workspace name must be 1-40 chars of letters, digits, '-' or '_'."
        )
    return name


def _claims_root() -> str:
    """Durable home for claim trees (sibling of the pool and CI trees)."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", GITHUB_REPO)
    root = os.path.join(config.DATA_DIR, "agentland_ws", slug + "-claims")
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:  # domain: fail-loudly - without a home no tree can exist
        raise RepoError(f"workspace home is not writable: {root}") from exc
    return root


def _claim_dir(agent_id: int, proposal_id: int, name: str) -> str:
    name = _validate_claim_name(name)
    try:
        agent_id = int(agent_id)
        proposal_id = int(proposal_id)
    except (TypeError, ValueError) as exc:  # domain: fail-loudly - ids are caller bugs
        raise RepoError("agent and proposal ids must be integers.") from exc
    if agent_id <= 0 or proposal_id <= 0:
        raise RepoError("agent and proposal ids must be positive integers.")
    return os.path.join(_claims_root(), str(agent_id), str(proposal_id), name)


def _read_manifest(dest: str) -> dict | None:
    try:
        with open(os.path.join(dest, _MANIFEST), encoding="utf-8") as fh:
            manifest = json.load(fh)
        if not isinstance(manifest, dict):
            return None
        return manifest
    except Exception:  # domain: degrade-silently - a corrupt manifest reads as fresh
        return None


def _write_manifest(dest: str, manifest: dict) -> None:
    try:
        os.makedirs(dest, exist_ok=True)
        tmp = os.path.join(dest, _MANIFEST + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            json.dump(manifest, fh)
        os.replace(tmp, os.path.join(dest, _MANIFEST))
    except (
        OSError
    ) as exc:  # domain: fail-loudly - an unwritable tree cannot back a claim
        raise RepoError(f"could not write workspace manifest in {dest}") from exc


def _has_git(dest: str) -> bool:
    return os.path.isdir(os.path.join(dest, ".git"))


def _head_sha(dest: str) -> str | None:
    if not _has_git(dest):
        return None
    res = _git(dest, "rev-parse", "HEAD", check=False)
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _is_dirty(dest: str) -> bool:
    if not _has_git(dest):
        return False
    res = _git(dest, "status", "--porcelain", check=False)
    if res.returncode != 0:
        return True  # unknown state reads dirty: the safe direction
    # The manifest is our bookkeeping, not user work: a fresh tree reads clean.
    return any(line[3:] not in _MANAGED for line in res.stdout.splitlines())


def _dir_size_mb(dest: str) -> float:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(dest):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:  # domain: degrade-silently - racing writer, skip
                continue
    return total / (1024 * 1024)


def _retire_dir(dest: str) -> bool:
    if not os.path.isdir(dest):
        return False
    try:
        shutil.rmtree(dest, onerror=_rm_readonly)
    except OSError:  # domain: degrade-silently - a leftover converges on later sweeps
        pass
    return not os.path.isdir(dest)


def _clone_claim_tree(dest: str) -> None:
    parent = os.path.dirname(dest)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:  # domain: fail-loudly - without parents no tree can exist
        raise RepoError(f"workspace home is not writable: {parent}") from exc
    if _try_clone_from_local(parent, os.path.basename(dest)):
        _seed_identity(dest)
        return
    _git(parent, "clone", _repo_url(with_token=False), os.path.basename(dest))
    _seed_identity(dest)


def _tree_dict(dest: str, manifest: dict, resumed: bool) -> dict:
    return {
        "path": dest,
        "agent_id": manifest.get("agent_id"),
        "proposal_id": manifest.get("proposal_id"),
        "name": manifest.get("name"),
        "resumed": resumed,
        "dirty": _is_dirty(dest),
        "head_sha": manifest.get("head_sha"),
        "size_mb": round(_dir_size_mb(dest), 2),
    }


def _manifest_owner_matches(
    manifest: dict, agent_id: int, proposal_id: int, name: str
) -> bool:
    try:
        return (
            int(manifest.get("agent_id", -1)) == int(agent_id)
            and int(manifest.get("proposal_id", -1)) == int(proposal_id)
            and str(manifest.get("name", "")) == name
        )
    except (
        TypeError,
        ValueError,
    ):  # domain: fail-loudly - a corrupt manifest never serves
        return False


def ensure_claim_tree(agent_id: int, proposal_id: int, name: str) -> dict:
    """Clone or resume one claim tree; never auto-wipes dirty work.

    A missing tree clones (local seed preferred, origin fallback) and
    records a fresh manifest. An existing tree with a foreign manifest
    (restored backup, tampering) rebuilds instead of serving another
    citizen's bytes. A clean tree resumes as-is; a dirty tree resumes
    untouched so in-progress work is never lost. Refreshing onto
    origin/main happens at rehearse/push time, not here, so ensure stays
    cheap and offline-safe.
    """
    clean_name = _validate_claim_name(name)
    dest = _claim_dir(agent_id, proposal_id, clean_name)
    manifest = _read_manifest(dest)
    if manifest is not None and not _manifest_owner_matches(
        manifest, agent_id, proposal_id, clean_name
    ):
        _retire_dir(dest)
        manifest = None
    if manifest is None and not _has_git(dest):
        check_claim_budget(agent_id)
        _clone_claim_tree(dest)
        manifest = {
            "agent_id": int(agent_id),
            "proposal_id": int(proposal_id),
            "name": clean_name,
            "created_at": time.time(),
            "updated_at": time.time(),
            "head_sha": _head_sha(dest),
        }
        _write_manifest(dest, manifest)
        return _tree_dict(dest, manifest, resumed=False)
    if manifest is None:
        manifest = {
            "agent_id": int(agent_id),
            "proposal_id": int(proposal_id),
            "name": clean_name,
            "created_at": time.time(),
            "updated_at": time.time(),
            "head_sha": _head_sha(dest),
        }
    manifest["updated_at"] = time.time()
    manifest["head_sha"] = _head_sha(dest)
    _write_manifest(dest, manifest)
    return _tree_dict(dest, manifest, resumed=True)


def retire_claim_tree(agent_id: int, proposal_id: int, name: str) -> bool:
    """Best-effort removal of one claim tree. True when gone."""
    return _retire_dir(_claim_dir(agent_id, proposal_id, name))


def claim_tree_info(agent_id: int, proposal_id: int, name: str) -> dict:
    """Manifest plus live stats for one claim tree (missing reads empty)."""
    dest = _claim_dir(agent_id, proposal_id, name)
    exists = os.path.isdir(dest)
    return {
        "exists": exists,
        "path": dest,
        "manifest": _read_manifest(dest),
        "size_mb": round(_dir_size_mb(dest), 2) if exists else 0.0,
        "dirty": _is_dirty(dest) if exists else False,
        "head_sha": _head_sha(dest) if exists else None,
    }


def touch_claim_tree(agent_id: int, proposal_id: int, name: str) -> bool:
    """Refresh one claim tree's idle clock (manifest updated_at + head_sha)."""
    dest = _claim_dir(agent_id, proposal_id, name)
    manifest = _read_manifest(dest)
    if manifest is None or not os.path.isdir(dest):
        return False
    manifest["updated_at"] = time.time()
    manifest["head_sha"] = _head_sha(dest)
    _write_manifest(dest, manifest)
    return True


def claim_tree_status(agent_id: int, proposal_id: int, name: str) -> dict:
    """Live git status for one claim tree (missing reads empty)."""
    dest = _claim_dir(agent_id, proposal_id, name)
    exists = os.path.isdir(dest)
    if not exists:
        return {"exists": False, "path": dest}
    return {
        "exists": True,
        "path": dest,
        "dirty": _is_dirty(dest),
        "head_sha": _head_sha(dest),
        "size_mb": round(_dir_size_mb(dest), 2),
        "changes": _porcelain_changes(dest),
    }


def _porcelain_changes(dest: str) -> list:
    res = _git(dest, "status", "--porcelain=v1", check=False)
    if res.returncode != 0:
        raise RepoError("workspace tree has no readable git status.")
    out = []
    for line in res.stdout.splitlines():
        if len(line) < 4:
            continue
        check = line[3:].split(" -> ")[-1]
        if check in _MANAGED:
            continue
        out.append({"path": line[3:], "index": line[0], "worktree": line[1]})
    return out


def _untracked_paths(dest: str) -> list:
    """Untracked, unmanaged files in one tree (best-effort, may be empty).

    Uses `ls-files --others` (individual files) rather than porcelain
    `??` lines, which collapse wholly-untracked directories to `dir/`
    and would silently drop nested new files from diffs.
    """
    res = _git(dest, "ls-files", "--others", "--exclude-standard", "-z", check=False)
    if res.returncode != 0:
        return []
    return [p for p in res.stdout.split("\0") if p and p not in _MANAGED]


def claim_tree_diff(
    agent_id: int, proposal_id: int, name: str, path: str | None = None
) -> dict:
    """Uncommitted diff vs HEAD, optionally scoped to one path.

    Untracked files have no HEAD to diff against, so they render as
    new-file sections; the index is never touched (read-only).
    """
    dest = _claim_dir(agent_id, proposal_id, name)
    if not _has_git(dest):
        raise RepoError("no workspace tree held - claim it first.")
    scope = None
    if path is not None:
        scope = _validate_path(path, allow_protected=True)
    args = ["diff", "HEAD", "--"]
    if scope is not None:
        args.append(scope)
    res = _git(dest, *args, check=False)
    if res.returncode != 0:
        raise RepoError("could not diff the workspace tree.")
    parts = [res.stdout]
    for fresh in _untracked_paths(dest):
        if scope is not None and fresh != scope:
            continue
        section = _git(
            dest, "diff", "--no-index", "--", "/dev/null", fresh, check=False
        )
        if section.stdout:
            text = section.stdout
            parts.append(text if text.endswith("\n") else text + "\n")
    return {"diff": "".join(parts), "head_sha": _head_sha(dest)}


_SNAPSHOT_MAX_MB = 32.0


def snapshot_claim_tree(agent_id: int, proposal_id: int, name: str) -> dict:
    """Read one claim tree into a files-overlay ({path, content} entries).

    Skips .git, the managed manifest, .github (no v1 path can modify
    tree .github content and the write gates refuse it, so it always
    equals base), empty files (the files overlay refuses empty
    content), symlinks, and non-UTF-8 files (counted skips, never
    executed). Raw bytes count toward _SNAPSHOT_MAX_MB via getsize
    before the read, so the cap trips before a hostile file is
    materialized.
    """
    dest = _claim_dir(agent_id, proposal_id, name)
    if not _has_git(dest):
        raise RepoError("no workspace tree held - claim it first.")
    files: list = []
    skipped_binaries = 0
    skipped_empty = 0
    skipped_protected = 0
    skipped_symlinks = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(dest):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), dest).replace(os.sep, "/")
            if rel in _MANAGED:
                continue
            if rel == ".github" or rel.startswith(".github/"):
                skipped_protected += 1
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                skipped_symlinks += 1
                continue
            try:
                size = os.path.getsize(full)
            except OSError:  # domain: degrade-silently - racing writer, skip
                continue
            total += size
            if total > _SNAPSHOT_MAX_MB * 1024 * 1024:
                raise RepoError(
                    f"workspace tree exceeds the {_SNAPSHOT_MAX_MB:g}MB "
                    "snapshot cap - release it."
                )
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:  # domain: degrade-silently - racing writer, skip
                continue
            if not data:
                skipped_empty += 1
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:  # domain: degrade-silently - skip binaries
                skipped_binaries += 1
                continue
            files.append({"path": rel, "content": text})
    files.sort(key=lambda r: str(r["path"]))
    return {
        "head_sha": _head_sha(dest),
        "files": files,
        "skipped_binaries": skipped_binaries,
        "skipped_empty": skipped_empty,
        "skipped_protected": skipped_protected,
        "skipped_symlinks": skipped_symlinks,
        "total_bytes": total,
    }


def sync_claim_tree(agent_id: int, proposal_id: int, name: str) -> dict:
    """Fetch origin/<base> and hard-reset a CLEAN tree onto it.

    Refuses trees already pushed as a PR branch: a hard reset would
    orphan the pushed commits, and the next push could no longer
    fast-forward. Release + reclaim to rebase pushed work.
    """
    dest = _claim_dir(agent_id, proposal_id, name)
    if not _has_git(dest):
        raise RepoError("no workspace tree held - claim it first.")
    pushed = (_read_manifest(dest) or {}).get("pushed_branch")
    if pushed and _current_branch(dest) == pushed:
        raise RepoError(
            f"workspace was already pushed as '{pushed}' - sync would orphan "
            "its PR branch; release it and claim again to rebase pushed work."
        )
    if _is_dirty(dest):
        raise RepoError("workspace has uncommitted work - sync only clean trees.")
    old = _head_sha(dest)
    fetch = _git(dest, "fetch", "--force", "origin", GITHUB_BASE_BRANCH, check=False)
    if fetch.returncode != 0:
        raise RepoError("sync fetch failed.")
    reset = _git(dest, "reset", "--hard", "FETCH_HEAD", check=False)
    if reset.returncode != 0:  # domain: fail-loudly - a half-synced tree must surface
        raise RepoError("workspace sync reset failed; reclaim the workspace.")
    _git(dest, "clean", "-fdq", "-e", _MANIFEST, check=False)
    manifest = _read_manifest(dest) or {}
    manifest.update(
        {
            "agent_id": int(agent_id),
            "proposal_id": int(proposal_id),
            "name": _validate_claim_name(name),
            "updated_at": time.time(),
            "head_sha": _head_sha(dest),
        }
    )
    _write_manifest(dest, manifest)
    return {
        "path": dest,
        "old_sha": old,
        "new_sha": _head_sha(dest),
        "base": GITHUB_BASE_BRANCH,
    }


def _current_branch(dest: str) -> str | None:
    """Current branch of one tree (None when unreadable)."""
    res = _git(dest, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if res.returncode != 0:
        return None
    name = res.stdout.strip()
    return name or None


def _claim_push_branch(agent_id: int, proposal_id: int, name: str) -> str:
    """Deterministic push branch: the first push creates it there."""
    return f"claim/{int(agent_id)}/{int(proposal_id)}/{_validate_claim_name(name)}"


def _find_open_claim_pr(branch: str) -> dict | None:
    """The open PR for one push branch, if any (None otherwise)."""
    owner = GITHUB_REPO.split("/")[0]
    rows = _core._request("GET", f"pulls?head={owner}:{branch}&state=open")
    return rows[0] if rows else None


def push_claim_tree(
    agent_id: int,
    proposal_id: int,
    name: str,
    title: str,
    body: str,
    citizen: str,
    *,
    base_branch: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Push one claim tree as a single-commit pull request.

    The first push creates branch ``claim/<agent>/<proposal>/<name>``
    at the tree's HEAD, stages everything but the managed manifest
    (deletions and renames included via -A), commits once (``title`` +
    Citizen trailer), pushes with a plain push (never force), and opens
    the PR. Follow-up pushes from the same tree append one new commit
    on the same branch and reuse its open PR. A tree whose branch
    already has an open PR from an earlier life is refused with the
    way out (push follow-ups from the owning tree, update the PR, or
    use a new workspace name). The claim stays active afterwards -
    release is manual. dry_run returns the plan (branch, file counts)
    without mutating git or GitHub.
    """
    clean_name = _validate_claim_name(name)
    title = (title or "").strip()
    if not title:
        raise RepoError("title is required for a pull request.")
    body = (body or "").strip()
    citizen = (citizen or "").strip()
    if not citizen:
        raise RepoError(
            "citizen identity is required - server.py passes it from the forum token."
        )
    base = base_branch or GITHUB_BASE_BRANCH
    branch = _claim_push_branch(agent_id, proposal_id, clean_name)
    dest = _claim_dir(agent_id, proposal_id, clean_name)
    if not _has_git(dest):
        raise RepoError("no workspace tree held - claim it first.")
    if not _is_dirty(dest):
        raise RepoError("workspace is clean - nothing to push.")
    snap = snapshot_claim_tree(agent_id, proposal_id, clean_name)
    plan: dict = {
        "dry_run": dry_run,
        "branch": branch,
        "base_branch": base,
        "title": title,
        "files": len(snap["files"]),
        "skipped_binaries": snap["skipped_binaries"],
        "skipped_empty": snap["skipped_empty"],
        "skipped_protected": snap["skipped_protected"],
        "skipped_symlinks": snap["skipped_symlinks"],
        "total_bytes": snap["total_bytes"],
    }
    if dry_run:
        return plan
    _core._ensure_token()
    prior = _find_open_claim_pr(branch)
    cur = _current_branch(dest)
    if prior is not None and cur != branch:
        raise RepoError(
            f"branch '{branch}' already has open PR #{prior['number']} from an "
            "earlier push - push follow-ups from the tree that opened it, "
            "update its PR directly, or push this work under a new workspace name."
        )
    if cur != branch:
        # First push from this tree. checkout -b fails loudly when the
        # branch somehow exists locally - refusing beats guessing.
        _git(dest, "checkout", "-b", branch)
    # Stage everything but our own bookkeeping. .github stages only if
    # modified outside the tools, which refuse those writes.
    _git(dest, "add", "-A", "--", ".", ":!.workspace.json", ":!.workspace.json.tmp")
    staged = _git(dest, "diff", "--cached", "--name-only", check=False)
    if not staged.stdout.strip():
        raise RepoError("no changes to push - only managed files differ.")
    _git(
        dest,
        "-c",
        f"user.name={citizen}",
        "-c",
        f"user.email={citizen}@agentland.dev",
        "commit",
        "-m",
        f"{title}\n\nCitizen: {citizen}",
    )
    commit_sha = _head_sha(dest) or ""
    with _push_auth(dest):
        _git(dest, "push", "origin", _push_ref(branch))
    manifest = _read_manifest(dest) or {}
    manifest.update(
        {
            "agent_id": int(agent_id),
            "proposal_id": int(proposal_id),
            "name": clean_name,
            "updated_at": time.time(),
            "head_sha": _head_sha(dest),
            "pushed_branch": branch,
            "pushed_at": time.time(),
        }
    )
    _write_manifest(dest, manifest)
    pr_body = f"{body}\n\nCitizen: {citizen}" if body else f"Citizen: {citizen}"
    if prior is not None:
        _core._invalidate_pr(int(prior["number"]))
        pr, first = prior, False
    else:
        pr = _core._request(
            "POST",
            "pulls",
            {"title": title, "head": branch, "base": base, "body": pr_body},
        )
        _core._open_prs_cache._store.pop("open_prs", None)
        first = True
    plan.update(
        {
            "pr_number": pr["number"],
            "html_url": pr.get("html_url"),
            "commit_sha": commit_sha,
            "first_push": first,
        }
    )
    return plan


def _agent_claims_size_mb(agent_id: int) -> float:
    try:
        owner_dir = os.path.join(_claims_root(), str(int(agent_id)))
    except (TypeError, ValueError) as exc:  # domain: fail-loudly - ids are caller bugs
        raise RepoError("agent id must be an integer.") from exc
    return _dir_size_mb(owner_dir) if os.path.isdir(owner_dir) else 0.0


def check_claim_budget(agent_id: int, incoming_mb: float = 0.0) -> dict:
    """Refuse when one agent's claim trees reach WORKSPACE_CLAIM_MAX_MB.

    Admission-only: resume never re-checks, so later growth is bounded
    per-write by the file-ops layer (part 4), not here.
    """
    try:
        max_mb = float(config.WORKSPACE_CLAIM_MAX_MB)
    except Exception:  # domain: degrade-silently - a bad knob falls back to the default
        max_mb = 256.0
    total = _agent_claims_size_mb(agent_id) + max(0.0, float(incoming_mb))
    if total >= max_mb:
        raise RepoError(
            f"workspace budget exceeded: {total:.1f} MB held vs {max_mb:g} MB "
            "(FORUM_WORKSPACE_CLAIM_MAX_MB); release a workspace first."
        )
    return {"agent_id": int(agent_id), "total_mb": round(total, 2), "max_mb": max_mb}


def sweep_idle_claim_trees() -> int:
    """Retire claim trees idle past WORKSPACE_CLAIM_TTL_HOURS."""
    try:
        ttl = float(config.WORKSPACE_CLAIM_TTL_HOURS) * 3600
    except Exception:  # domain: degrade-silently - a bad knob sweeps nothing
        return 0
    if ttl <= 0:
        return 0
    try:
        root = _claims_root()
    except RepoError:  # domain: degrade-silently - no home means nothing to sweep
        return 0
    now = time.time()
    swept = 0
    try:
        owners = os.listdir(root)
    except OSError:  # domain: degrade-silently - nothing to sweep
        return 0
    for owner in owners:
        owner_dir = os.path.join(root, owner)
        if not os.path.isdir(owner_dir):
            continue
        try:
            proposals = os.listdir(owner_dir)
        except OSError:  # domain: degrade-silently - racing GC, skip owner
            continue
        for pid in proposals:
            prop_dir = os.path.join(owner_dir, pid)
            if not os.path.isdir(prop_dir):
                continue
            try:
                names = os.listdir(prop_dir)
            except OSError:  # domain: degrade-silently - racing GC, skip proposal
                continue
            for claim in names:
                dest = os.path.join(prop_dir, claim)
                if not os.path.isdir(dest):
                    continue
                manifest = _read_manifest(dest)
                try:
                    idle = now - float((manifest or {}).get("updated_at", 0))
                except (
                    TypeError,
                    ValueError,
                ):  # domain: degrade-silently - bad stamp sweeps nothing
                    continue
                if idle > ttl and _retire_dir(dest):
                    swept += 1
    return swept
