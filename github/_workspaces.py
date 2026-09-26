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

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import config

from . import _core
from ._core import (
    GITHUB_BASE_BRANCH,
    GITHUB_REPO,
    RepoError,
    _validate_path,
    _validate_ref,
)
from ._eol import _normalize_eol, _target_eol_for_text
from ._gitops import (
    _git,
    _git_bytes,
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


@contextmanager
def workspace_lock(dest: str, *, allow_missing: bool = False):
    if not allow_missing and not _has_git(dest):
        raise RepoError("no workspace tree held - claim it first.")
    # The lock file is a permanent rendezvous, never unlinked: the same
    # triple reclaims the identical path, and any unlink would split the
    # inode a live holder is locked on (POSIX) while fixing nothing.
    lock_path = dest + ".workspace.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+b") as lock:
        if os.name == "nt":
            msvcrt: Any = __import__("msvcrt")
            locking = msvcrt.locking
            lock_mode = msvcrt.LK_NBLCK
            unlock_mode = msvcrt.LK_UNLCK
            lock.seek(0)
            lock.write(b"\0")
            lock.flush()
            while True:
                try:
                    lock.seek(0)
                    locking(lock.fileno(), lock_mode, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                locking(lock.fileno(), unlock_mode, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


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
    # Meter working-tree bytes only: .git internals are fixed clone
    # overhead (~25MB steady state), not agent work, so the per-agent
    # budget gates what the agent controls (proposal #571).
    total = 0
    for dirpath, dirnames, filenames in os.walk(dest):
        if ".git" in dirnames:
            dirnames.remove(".git")
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


def _retire_claim_tree_locked(dest: str) -> bool:
    return _retire_dir(dest)


def _retire_claim_tree(dest: str) -> bool:
    with workspace_lock(dest, allow_missing=True):
        return _retire_claim_tree_locked(dest)


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
        "claim_id": manifest.get("claim_id"),
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


def _manifest_claim_matches(manifest: dict, claim_id: int | None) -> bool:
    if claim_id is None:
        return True
    try:
        return str(manifest.get("claim_id")) == str(claim_id)
    except (TypeError, ValueError):
        return False


def ensure_claim_tree(
    agent_id: int, proposal_id: int, name: str, claim_id: int | None = None
) -> dict:
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
    if manifest is not None and (
        not _manifest_owner_matches(manifest, agent_id, proposal_id, clean_name)
        or not _manifest_claim_matches(manifest, claim_id)
    ):
        _retire_claim_tree_locked(dest)
        manifest = None
    if manifest is None and not _has_git(dest):
        check_claim_budget(agent_id)
        _clone_claim_tree(dest)
        manifest = {
            "agent_id": int(agent_id),
            "proposal_id": int(proposal_id),
            "name": clean_name,
            "claim_id": int(claim_id) if claim_id is not None else None,
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
            "claim_id": int(claim_id) if claim_id is not None else None,
            "created_at": time.time(),
            "updated_at": time.time(),
            "head_sha": _head_sha(dest),
        }
    if claim_id is not None:
        manifest["claim_id"] = int(claim_id)
    manifest["updated_at"] = time.time()
    manifest["head_sha"] = _head_sha(dest)
    _write_manifest(dest, manifest)
    return _tree_dict(dest, manifest, resumed=True)


def retire_claim_tree(agent_id: int, proposal_id: int, name: str) -> bool:
    """Best-effort removal of one claim tree. True when gone."""
    return _retire_claim_tree(_claim_dir(agent_id, proposal_id, name))


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


def touch_claim_tree(
    agent_id: int, proposal_id: int, name: str, claim_id: int | None = None
) -> bool:
    """Refresh one claim tree's idle clock (manifest updated_at + head_sha)."""
    dest = _claim_dir(agent_id, proposal_id, name)
    manifest = _read_manifest(dest)
    if manifest is None or not os.path.isdir(dest):
        return False
    if claim_id is not None and not _manifest_claim_matches(manifest, claim_id):
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


def _changed_paths(dest: str) -> list:
    """Paths a tree actually changed vs its HEAD: tracked edits (working
    tree vs HEAD, staged or not) plus untracked additions. Deleted
    tracked paths are dropped - they're absent from the walk anyway.

    This is the delta set the rehearsal snapshot rides on (bug #90): a
    claim tree cloned from an earlier base must not re-upload its stale
    copies of untouched files - the runner refreshes onto origin/main
    first and the overlay would flatten that fresh base back to old
    bytes, rehearsing against a tree that never was.
    """
    res = _git(dest, "diff", "--name-only", "HEAD", "-z", check=False)
    if res.returncode != 0:
        changed: list = []
    else:
        changed = [p for p in res.stdout.split("\0") if p]
    changed.extend(_untracked_paths(dest))
    return changed


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


def _canonical_base_ref(ref: str) -> str:
    """Collapse a ref spelling onto the short name the guard and the CI
    fetch both resolve: `refs/heads/x` -> `x`, `refs/tags/x` -> `x`,
    `refs/remotes/origin/x` -> `x`, `origin/x` -> `x`, recursively (a
    nested `origin/refs/heads/x` collapses to `x` too); anything else
    (short names, raw shas) returns unchanged. Idempotent."""
    while True:
        for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/origin/", "origin/"):
            if ref.startswith(prefix):
                ref = ref[len(prefix) :]
                break
        else:
            return ref


def _stacked_layers(dest: str, base: str | None = None) -> int:
    """Commits on the tree's HEAD not reachable from its own base ref.

    The base defaults to the repo base branch; `workspace_rehearse`
    threads its validated `base_ref` through, so a stacked rehearsal
    against a non-main base compares against that base (its layers ARE
    the ancestor), not origin/main. A claim tree's remote refs never
    auto-advance (reads never fetch and v1 has no commit tool), so HEAD
    sits ahead of the base ref exactly when a push - or a manual commit
    - added layers to the tree. A delta-vs-HEAD snapshot then drops
    those layers, rehearsing a phantom tree missing the early layers
    (bug #97). A merely-stale tree (the remote advanced after the claim)
    is NOT flagged: both refs still sit at the clone sha, so the delta
    stays honest.

    The base spelling is canonicalized first (a full `refs/heads/x`
    spelling used to build a never-existing `origin/refs/heads/x`, which
    degrades to 0 - the silent bypass). The claimed base must then
    resolve to a real ref of this tree; an unresolvable base (a raw sha,
    or a branch/tag the tree never fetched) FAILS CLOSED with RepoError
    instead of returning the flat 0.
    """
    canonical = _canonical_base_ref(base or GITHUB_BASE_BRANCH)
    ref = None
    for candidate in (f"origin/{canonical}", f"refs/tags/{canonical}"):
        ok = _git(
            dest,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{candidate}^{{commit}}",
            check=False,
        )
        if ok.returncode == 0:
            ref = candidate
            break
    if ref is None:
        if base is None:
            return 0
        raise RepoError(
            f"cannot resolve rehearsal base {base!r} to a ref of this "
            "claim tree - the tree holds no origin/<name> or refs/tags/"
            "<name> to compare the stacked guard against (bug #97). "
            "Fetch the base into the tree first, or use its branch/tag "
            "name."
        )
    res = _git(dest, "rev-list", "--count", f"{ref}..HEAD", check=False)
    if res.returncode != 0:
        return 0
    try:
        return int(res.stdout.strip() or "0")
    except ValueError:  # domain: degrade-silently - unparsable count, assume flat
        return 0


_SNAPSHOT_MAX_MB = 32.0


def snapshot_claim_tree(
    agent_id: int,
    proposal_id: int,
    name: str,
    *,
    delta: bool = False,
    base: str | None = None,
) -> dict:
    """Read one claim tree into a files-overlay ({path, content} entries).

    Skips .git, the managed manifest, .github (no v1 path can modify
    tree .github content and the write gates refuse it, so it always
    equals base), empty files (the files overlay refuses empty
    content), symlinks, and non-UTF-8 files (counted skips, never
    executed). Raw bytes count toward _SNAPSHOT_MAX_MB via getsize
    before the read, so the cap trips before a hostile file is
    materialized.

    With delta=True only the tree's own changes ride the overlay (see
    _changed_paths) - untouched tracked files are left out, so a stale
    claim tree cannot flatten a freshly-refreshed rehearsal base back
    to its old bytes (bug #90). A tree whose HEAD carries pushed layers
    is refused outright (bug #97): the delta would drop them and
    rehearse a phantom tree (the stacked-refusal base defaults to
    origin/main; a rehearsal `base` compares against its own ancestor,
    so a valid stacked tree against a non-main base is not flagged).
    Whole-tree stays the default: the push manifest must cover every
    file the PR would carry.
    """
    dest = _claim_dir(agent_id, proposal_id, name)
    if not _has_git(dest):
        raise RepoError("no workspace tree held - claim it first.")
    if delta:
        changed = set(_changed_paths(dest))
        base_name = _canonical_base_ref(base or GITHUB_BASE_BRANCH)
        extra = _stacked_layers(dest, base_name)
        if extra:
            raise RepoError(
                f"workspace HEAD is {extra} commit(s) ahead of "
                f"origin/{base_name} - the tree's pushed layers "
                "live in HEAD, which a delta-vs-HEAD snapshot drops, so "
                "rehearsing would build a phantom tree without them "
                "(bug #97). Release this claim (release_workspace) and "
                "claim a fresh tree for stacked work, or rehearse once the "
                "earlier layers land on the base."
            )
    else:
        changed = None
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
            if changed is not None and rel not in changed:
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
    manifest = [
        {
            "path": f["path"],
            "content_bytes": len(f["content"].encode("utf-8")),
            "content_sha256": hashlib.sha256(f["content"].encode("utf-8")).hexdigest(),
        }
        for f in files
    ]
    return {
        "head_sha": _head_sha(dest),
        "files": files,
        "content_manifest": manifest,
        "skipped_binaries": skipped_binaries,
        "skipped_empty": skipped_empty,
        "skipped_protected": skipped_protected,
        "skipped_symlinks": skipped_symlinks,
        "total_bytes": total,
    }


def sync_claim_tree(
    agent_id: int, proposal_id: int, name: str, base_branch: str | None = None
) -> dict:
    """Fetch origin/<base> (or `base_branch`) and hard-reset a CLEAN tree onto it.

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
    from github._core import _validate_ref

    base = _validate_ref(base_branch)
    fetch = _git(dest, "fetch", "--force", "origin", base, check=False)
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
        "base": base,
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


def _claim_head_ref(row: dict) -> str:
    """Push-branch name from a PR row: the API shape (head.ref) or the
    test transport shape (branch)."""
    head = (row or {}).get("head")
    if isinstance(head, dict):
        return head.get("ref") or ""
    return head or (row or {}).get("branch") or ""


def _find_open_claim_prs_for_proposal(agent_id: int, proposal_id: int) -> list[dict]:
    """Open PRs on any push branch of one citizen's proposal (#B116).
    The per-branch lookup above cannot see a second workspace's branch,
    so the per-proposal lookup closes the duplicate-PR hole. Paginated:
    a claim PR older than one listing page still refuses."""
    from github._reads import _paginated_open_pulls

    prefix = f"claim/{int(agent_id)}/{int(proposal_id)}/"
    return [
        r
        for r in _paginated_open_pulls(config.GITHUB_PRS_PER_PAGE)
        if _claim_head_ref(r).startswith(prefix)
    ]


def _strip_wip_prefix(text: str) -> str:
    """Compare titles modulo the proposal-hold 'WIP: ' prefix: the poller
    strips it on hold-lift while a later push may still carry it, and that
    prefix-only delta must not count as a revised title (post-green review
    on #1409 - the re-add window is cosmetic-only and self-healing, but
    there is no reason to ever write it)."""
    s = text or ""
    return s[4:].lstrip() if s.upper().startswith("WIP:") else s


_MIRROR_START = "<!-- findings-board:start -->"
_MIRROR_END = "<!-- findings-board:end -->"


def _strip_mirror_span(text: str) -> str:
    """Remove one ordered findings-board mirror block for comparison:
    the push-text sync compares live vs caller prose, and the mirror
    block lives only on the live side - it must never count as revised
    prose (proposal #710 part 5). Comparison only; never written."""
    s = text or ""
    start = s.find(_MIRROR_START)
    if start == -1:
        return s
    end = s.find(_MIRROR_END, start + len(_MIRROR_START))
    if end == -1:
        return s
    return s[:start] + s[end + len(_MIRROR_END) :]


def _open_or_reuse_claim_pr(
    branch: str, base: str, title: str, body: str, prior: dict | None
) -> tuple[dict, bool, bool]:
    """Open the PR for one pushed branch, or reuse its open one.

    Returns (pr, first_push, text_updated). On reuse, a revised title
    and/or body is PATCHed onto the live PR when it differs from what
    the PR currently carries - a follow-up push must never silently drop
    the caller's prose. Identical text makes no request. The body
    compare ignores one ordered findings-board mirror block, which lives
    only on the live side (proposal #710 part 5). A PATCH failure
    raises: the commit already landed, and the retry replays this exact
    comparison idempotently (the already-pushed path re-enters here).
    """
    if prior is not None:
        _core._invalidate_pr(int(prior["number"]))
        patch: dict = {}
        if _strip_wip_prefix(prior.get("title") or "") != _strip_wip_prefix(title):
            patch["title"] = title
        prior_known = _strip_mirror_span(prior.get("body") or "").rstrip()
        if prior_known != (body or "").rstrip():
            patch["body"] = body
        if patch:
            _core._request("PATCH", f"pulls/{prior['number']}", patch)
            _core._invalidate_pr(int(prior["number"]))
            prior = dict(prior)
            prior.update(patch)
        return prior, False, bool(patch)
    pr = _core._request(
        "POST", "pulls", {"title": title, "head": branch, "base": base, "body": body}
    )
    _core._open_prs_cache._store.pop("open_prs", None)
    return pr, True, False


def _resolve_tree_commit(dest: str, ref: str) -> tuple[str, str]:
    """Validate `ref` and resolve it to a commit SHA inside one tree.

    Tries the ref as given, then under `origin/` (a branch fetched by
    an earlier sync but never checked out locally). Returns
    (winning_candidate, commit_sha) - the candidate that resolved, so
    callers can echo provenance (`origin/<ref>` when fallback hit).
    Unknown refs fail loudly naming the ref - syncing (`workspace_sync`,
    which fetches origin/<base>) is the way new refs arrive; reads
    never fetch.
    """
    validated = _validate_ref(ref)
    for candidate in (validated, f"origin/{validated}"):
        res = _git(
            dest, "rev-parse", "--verify", f"{candidate}^{{commit}}", check=False
        )
        if res.returncode == 0 and res.stdout.strip():
            return candidate, res.stdout.strip()
    raise RepoError(
        f"unknown ref {validated!r} - no such branch, tag or commit in "
        "this workspace tree; sync it first (`workspace_sync`) to fetch "
        "origin refs."
    )


def _read_file_at_ref(
    dest: str, clean: str, ref: str, *, require_regular: bool
) -> tuple[bytes, str, str]:
    validated, commit = _resolve_tree_commit(dest, ref)
    listed = _git(dest, "ls-tree", "--long", "-z", commit, "--", clean, check=False)
    if listed.returncode != 0:
        raise RepoError(f"could not list {clean!r} at ref {validated!r}.")
    entry: list[str] | None = None
    for record in listed.stdout.split("\0"):
        meta, _, name = record.partition("\t")
        if name == clean:
            entry = meta.split()
            break
    if entry is None or len(entry) < 4 or entry[1] != "blob":
        if entry is not None and len(entry) >= 2 and entry[1] == "tree":
            raise RepoError(f"path {clean!r} is a directory at ref {validated!r}.")
        if entry is not None and len(entry) >= 2 and entry[1] == "commit":
            raise RepoError(
                f"path {clean!r} is a submodule at ref {validated!r} - its "
                "content lives in another repository."
            )
        raise RepoError(f"no file at {clean!r} in the tree at ref {validated!r}.")
    if require_regular and entry[0] not in {"100644", "100755"}:
        raise RepoError(f"path {clean!r} is not a regular file at ref {validated!r}.")
    try:
        size = int(entry[3])
    except ValueError:
        size = _transfer_file_cap_bytes() + 1
    cap = _transfer_file_cap_bytes()
    if size > cap:
        raise RepoError(
            f"{clean!r} is {size} bytes at ref {validated!r}, over the "
            f"{cap / (1 << 20):g}MB read cap."
        )
    blob = _git_bytes(dest, "cat-file", "-p", entry[2], check=False)
    if blob.returncode != 0:
        raise RepoError(f"could not read {clean!r} at ref {validated!r}.")
    return blob.stdout, validated, entry[0]


def read_file_at_ref(dest: str, clean: str, ref: str) -> tuple[bytes, str]:
    """Committed bytes of one tree-relative path at `ref` (plus the winning
    ref candidate - `origin/<ref>` when fallback resolves).

    No checkout, no worktree touch: dirty edits are invisible here by
    design, so a fix trail can be audited against the branch itself.
    The blob is located via `ls-tree -z` (NUL-split, unquoted: a quote,
    backslash or newline in the name can never break the parse - and a
    `:` in the name can never split a `rev:path` arg, since no such arg
    is built) and materialized with `cat-file -p` over the bytes path, so
    binaries read like the live path does (decoded with replacement
    downstream). The transfer cap is enforced from the `ls-tree --long`
    size before any byte moves. Symlink blobs read as their target text;
    directories, submodules and missing paths refuse.
    """
    data, validated, _mode = _read_file_at_ref(dest, clean, ref, require_regular=False)
    return data, validated


def read_regular_file_at_ref(dest: str, clean: str, ref: str) -> tuple[bytes, str, str]:
    """Committed bytes, winning ref, and git mode for a regular file."""
    return _read_file_at_ref(dest, clean, ref, require_regular=True)


def _transfer_file_cap_bytes() -> int:
    """Per-file transfer cap (mirrors the 1MB MCP read cap): the data
    plane carries no token cost, so this is purely a safety rail. A
    non-positive knob falls back to the 1MB default (a cap of zero would
    either disable transfers or uncap them - neither is an opt-out with
    a safe reading, so the default wins, like the ticket TTL floor)."""
    try:
        mb = float(config.TRANSFER_MAX_FILE_MB)
    except Exception:  # domain: degrade-silently - a bad knob falls back to default
        return 1 << 20
    if mb <= 0:
        return 1 << 20
    return int(mb * (1 << 20))


def _refuse_symlink_components(dest: str, clean: str) -> None:
    """Refuse any path with a symlink component (proposal #597): realpath
    containment alone resolves an intra-tree `evil -> .git/hooks/x` link
    to an inside-dest path, so name checks pass while reads/writes land
    in .git internals. Walk every component lexically - islink needs no
    target to exist, so dangling links refuse too.
    """
    cur = dest
    for part in clean.split("/"):
        cur = os.path.join(cur, part)
        try:
            linked = os.path.islink(cur)
        except (
            OSError
        ):  # domain: degrade-silently - an unreadable component reads as plain
            return
        if linked:
            raise RepoError(
                f"path {clean!r} walks through a symlink - links never"
                " address workspace content (snapshot skips them too)."
            )


def _guard_transfer_path(dest: str, path: str) -> tuple[str, str]:
    """Engine-level path guard for transfer reads/writes (mirrors the
    MCP layer's _guard_tree_path: writes refuse protected paths, the
    managed manifest and .git are never addressable, nothing escapes).

    .git is refused explicitly (not via _MANAGED, which would also hide
    a top-level .git gitlink from status/diff bookkeeping): the engine
    is the data plane's last line of defense for direct minters.
    """
    clean = _validate_path(path, allow_protected=False)
    if clean == ".git" or clean.startswith(".git/"):
        raise RepoError(f"path {path!r} is managed by the workspace itself.")
    if clean.split("/", 1)[0] in _MANAGED:
        raise RepoError(f"path {path!r} is managed by the workspace itself.")
    _refuse_symlink_components(dest, clean)
    real = os.path.realpath(dest)
    full = os.path.realpath(os.path.join(dest, clean))
    if full != real and not full.startswith(real + os.sep):
        raise RepoError(f"path {path!r} escapes the workspace.")
    return clean, full


def read_transfer_bytes(
    agent_id: int,
    proposal_id: int,
    name: str,
    path: str,
    *,
    claim_validator: Callable[[], None] | None = None,
    after_read: Callable[[], None] | None = None,
) -> tuple[str, bytes]:
    """Raw bytes of one tree file for ticket download (binary-safe: the
    data plane never decodes). Over-cap files refuse before reading."""
    clean_name = _validate_claim_name(name)
    dest = _claim_dir(agent_id, proposal_id, clean_name)
    with workspace_lock(dest):
        if claim_validator is not None:
            claim_validator()
        result = _read_transfer_bytes(clean_name, dest, path)
        if after_read is not None:
            after_read()
        return result


def _read_transfer_bytes(clean_name: str, dest: str, path: str) -> tuple[str, bytes]:
    if not _has_git(dest):
        raise RepoError("no workspace tree held - claim it first.")
    clean, full = _guard_transfer_path(dest, path)
    if os.path.isdir(full):
        raise RepoError(f"path {clean!r} is a directory - only files transfer.")
    try:
        size = os.path.getsize(full)
    except OSError as exc:  # domain: fail-loudly - a missing tree file surfaces
        raise RepoError(f"no file at {clean!r} in the workspace.") from exc
    cap = _transfer_file_cap_bytes()
    if size > cap:
        raise RepoError(
            f"path {clean!r} is {size} bytes, over the {cap} byte transfer cap."
        )
    try:
        with open(full, "rb") as fh:
            data = fh.read()
    except OSError as exc:  # domain: fail-loudly - an unreadable tree file surfaces
        raise RepoError(f"could not read {clean!r} in the workspace.") from exc
    return clean, data


def apply_transfer_bytes(
    agent_id: int,
    proposal_id: int,
    name: str,
    path: str,
    data: bytes,
    *,
    expect_sha256: str | None = None,
    claim_validator: Callable[[], None] | None = None,
    after_apply: Callable[[], None] | None = None,
) -> dict:
    clean_name = _validate_claim_name(name)
    dest = _claim_dir(agent_id, proposal_id, clean_name)
    with workspace_lock(dest):
        if claim_validator is not None:
            claim_validator()
        result = _apply_transfer_bytes(
            agent_id,
            proposal_id,
            clean_name,
            path,
            data,
            expect_sha256=expect_sha256,
        )
        if after_apply is not None:
            after_apply()
        return result


def _apply_transfer_bytes(
    agent_id: int,
    proposal_id: int,
    name: str,
    path: str,
    data: bytes,
    *,
    expect_sha256: str | None = None,
) -> dict:
    """Write one whole file from transfer bytes (the POST data-plane
    apply). Same write contract as the MCP content path: EOL-normalized
    to the existing file's target (LF for new files), budget-checked,
    empty refused, non-UTF-8 refused, identical bytes a quiet no-op.

    expect_sha256 pins the read the upload was built from (the ticket's
    pin when the agent passed one): a mismatch refuses before any byte
    moves, so a stale download can never silently revert newer work.
    """
    clean_name = _validate_claim_name(name)
    dest = _claim_dir(agent_id, proposal_id, clean_name)
    if not _has_git(dest):
        raise RepoError("no workspace tree held - claim it first.")
    clean, full = _guard_transfer_path(dest, path)
    if os.path.isdir(full):
        raise RepoError(f"path {clean!r} is a directory - only files transfer.")
    existing: bytes | None = None
    if os.path.isfile(full):
        try:
            with open(full, "rb") as fh:
                existing = fh.read()
        except OSError as exc:  # domain: fail-loudly - an unreadable tree file surfaces
            raise RepoError(f"could not read {clean!r} in the workspace.") from exc
    if expect_sha256 is not None:
        have = hashlib.sha256(existing).hexdigest() if existing is not None else None
        if have != expect_sha256:
            raise RepoError(
                f"stale base for {clean!r}: expected"
                f" {str(expect_sha256)[:12]}..., tree holds"
                f" {have[:12] + '...' if have else 'nothing'} - fetch again"
                " and rebase the upload."
            )
    try:
        text = bytes(data).decode("utf-8")
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise RepoError(
            f"cannot write {clean!r} - upload is not UTF-8 text (binary files"
            " don't ride transfers in v1)."
        ) from exc
    if not text:
        raise RepoError(
            f"upload for {clean!r} is empty - deletion goes through"
            " workspace_delete_file."
        )
    try:
        target = _target_eol_for_text(
            existing.decode("utf-8") if existing is not None else ""
        )
    except UnicodeDecodeError:  # domain: degrade-silently - binary base takes LF target
        target = "\n"
    new_text = _normalize_eol(text, target)
    new_bytes = new_text.encode("utf-8")
    new_sha = hashlib.sha256(new_bytes).hexdigest()
    if existing is not None and new_bytes == existing:
        return {
            "path": clean,
            "bytes": len(new_bytes),
            "content_sha256": new_sha,
            "changed": False,
        }
    check_claim_budget(int(agent_id), incoming_mb=len(new_bytes) / (1024 * 1024))
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
    except OSError as exc:  # domain: fail-loudly - an unwritable tree file surfaces
        raise RepoError(f"could not write {clean!r} in the workspace.") from exc
    return {
        "path": clean,
        "bytes": len(new_bytes),
        "content_sha256": new_sha,
        "changed": True,
    }


def _check_expect_shas(manifest: list, expect_shas: dict) -> None:
    """Refuse when the snapshot's sha256 manifest misses an expected pin
    (proposal #507 P1: the rehearse-then-push integrity check; raises
    before any git mutation so a mismatch never commits)."""
    have = {m["path"]: m["content_sha256"] for m in manifest}
    for path, want in expect_shas.items():
        got = have.get(path)
        if got is None:
            raise RepoError(
                f"expect_shas names {path!r}, which is not among this "
                "workspace's files."
            )
        if got != want:
            raise RepoError(
                f"sha mismatch for {path!r}: expected "
                f"{str(want)[:12]}..., snapshot {got[:12]}... - rehearse"
                " again and retry."
            )


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
    expect_shas: dict[str, str] | None = None,
) -> dict:
    """Push one claim tree as a single-commit pull request.

    The first push creates branch ``claim/<agent>/<proposal>/<name>``
    at the tree's HEAD, stages everything but the managed manifest
    (deletions and renames included via -A), commits once (``title`` +
    Citizen trailer), pushes with a plain push (never force), and opens
    the PR. Follow-up pushes from the same tree append one new commit
    on the same branch and reuse its open PR, PATCHing a revised title
    and/or body onto the live PR when they differ (reported as
    ``text_updated``; identical text makes no request). A proposal that
    already has an open PR from another workspace is refused naming it -
    push follow-ups from the owning tree, or update its PR directly
    (``repo_update_pr``); a second PR would fragment review. The claim
    stays active afterwards -
    release is manual. Failures never strand the tree: a failed push
    soft-resets (the retry re-commits once), and a pushed-but-unlinked
    tree finishes opening its PR on retry. dry_run returns the plan
    (branch, file counts) without mutating git or GitHub.
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
    cur = _current_branch(dest)
    dirty = _is_dirty(dest)
    manifest_now = _read_manifest(dest) or {}
    already = (
        not dirty and manifest_now.get("pushed_branch") == branch and cur == branch
    )
    if not dirty and not already:
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
        "already_pushed": already,
        "content_manifest": snap["content_manifest"],
    }
    if expect_shas is not None:
        if not isinstance(expect_shas, dict):
            raise RepoError("expect_shas must be a {path: sha256} mapping.")
        _check_expect_shas(plan["content_manifest"], expect_shas)
    if dry_run:
        return plan
    _core._ensure_token()
    prior = _find_open_claim_pr(branch)
    pr_body = f"{body}\n\nCitizen: {citizen}" if body else f"Citizen: {citizen}"
    commit_sha = _head_sha(dest) or ""
    if prior is None:
        # Per-proposal duplicate guard (#B116): the branch-keyed lookup
        # above cannot see another workspace's branch, so a second tree
        # would silently fork a second PR. Runs before the already-retry
        # below so a retried push cannot fork either (a same-branch open
        # PR is already in `prior`, so this only fires cross-branch).
        for other in _find_open_claim_prs_for_proposal(agent_id, proposal_id):
            other_branch = _claim_head_ref(other)
            if other_branch and other_branch != branch:
                raise RepoError(
                    f"proposal #{int(proposal_id)} already has open PR"
                    f" #{other.get('number')} on branch '{other_branch}' -"
                    " push follow-ups from the tree that opened it, or update"
                    " its PR directly (repo_update_pr); a second PR would"
                    " fragment review."
                )
    if already:
        # Retry after a pushed-but-unlinked outcome (commit + push +
        # manifest landed while the PR POST failed): the tree already
        # holds exactly the pushed state, so finish opening its PR
        # instead of demanding new dirt or stacking a junk commit.
        pr, first, text_updated = _open_or_reuse_claim_pr(
            branch, base, title, pr_body, prior
        )
        plan.update(
            {
                "pr_number": pr["number"],
                "html_url": pr.get("html_url"),
                "commit_sha": commit_sha,
                "first_push": first,
                "text_updated": text_updated,
            }
        )
        return plan
    if prior is not None and cur != branch:
        raise RepoError(
            f"branch '{branch}' already has open PR #{prior['number']} from an "
            "earlier push - push follow-ups from the tree that opened it, "
            "or update its PR directly (repo_update_pr)."
        )
    if cur != branch:
        # First push from this tree: refuse a retained remote branch
        # (past life whose PR is closed) before creating ours - a plain
        # push would die non-fast-forward after the commit.
        remote = _git(dest, "ls-remote", "--heads", "origin", branch, check=False)
        if remote.returncode == 0 and remote.stdout.strip():
            raise RepoError(
                f"branch '{branch}' already exists on origin (its PR is "
                "closed or missing) - push this work under a new workspace name."
            )
        # checkout -b fails loudly when the branch somehow exists
        # locally - refusing beats guessing.
        _git(dest, "checkout", "-b", branch)
    if cur == branch:
        # Freshness gate (proposal #748): a fixer may have pushed since
        # this tree last synced.  Refuse behind-trees here with the
        # release pointer instead of committing first and dying
        # non-fast-forward after (workspace_sync refuses pushed trees
        # lest it orphan the PR branch, so no auto-sync: release and
        # claim again when clean, read work out first when dirty).
        # Fail-open: any check failure falls through to today's path
        # and the push itself decides.
        try:
            _fetch = _git(dest, "fetch", "origin", branch, check=False)
            if _fetch.returncode != 0:
                _tip = None
            else:
                _tip = _git(dest, "rev-parse", "FETCH_HEAD", check=False).stdout.strip()
            _head = _head_sha(dest) or ""
            if _tip and _tip != _head:
                _is_anc = _git(
                    dest, "merge-base", "--is-ancestor", _head, _tip, check=False
                )
                if _is_anc.returncode == 0:
                    _cnt = _git(
                        dest, "rev-list", "--count", f"{_head}..{_tip}", check=False
                    )
                    _n = _cnt.stdout.strip() or "many"
                    raise RepoError(
                        f"branch '{branch}' is {_n} commit(s) ahead of this tree"
                        " - release it and claim again to rebase pushed work"
                        " (read your work out first when dirty), then push again."
                    )
        except RepoError:
            raise
        except Exception:
            pass  # domain: degrade-silently - gate fail-open, push decides
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
    try:
        with _push_auth(dest):
            _git(dest, "push", "origin", _push_ref(branch))
    except RepoError:
        # Restore the dirty state the gate understands: without this the
        # commit sits invisibly on the branch and every retry is refused
        # as clean. The retry then re-commits once - no junk accumulates.
        _git(dest, "reset", "--soft", "HEAD~1", check=False)
        raise
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
    pr, first, text_updated = _open_or_reuse_claim_pr(
        branch, base, title, pr_body, prior
    )
    plan.update(
        {
            "pr_number": pr["number"],
            "html_url": pr.get("html_url"),
            "commit_sha": commit_sha,
            "first_push": first,
            "text_updated": text_updated,
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
                try:
                    with workspace_lock(dest, allow_missing=True):
                        manifest = _read_manifest(dest)
                        try:
                            idle = now - float((manifest or {}).get("updated_at", 0))
                        except (
                            TypeError,
                            ValueError,
                        ):  # domain: degrade-silently - bad stamp sweeps nothing
                            continue
                        if idle > ttl and _retire_claim_tree_locked(dest):
                            swept += 1
                except (
                    OSError,
                    RepoError,
                ):  # domain: degrade-silently - sweep one tree
                    continue
    return swept


def sweep_released_claim_trees(live: set | Callable[[], set]) -> int:
    """Retire claim trees whose record is gone (merge/close release records).

    `live` holds (agent_id, proposal_id, name) triples with an active
    record; anything else on disk retires. A callable is re-read under each
    tree lock. Manifest-less dirs are left for the idle sweep - without a
    manifest there is no owner to judge, and a foreign manifest rebuilds
    on next claim instead.
    """
    try:
        root = _claims_root()
    except RepoError:  # domain: degrade-silently - no home, nothing to sweep
        return 0
    swept = 0
    try:
        owners = os.listdir(root)
    except OSError:  # domain: degrade-silently - nothing to sweep
        return 0
    for owner in owners:
        try:
            agent_id = int(owner)
        except (TypeError, ValueError):  # domain: degrade-silently - skip odd dirs
            continue
        owner_dir = os.path.join(root, owner)
        if not os.path.isdir(owner_dir):
            continue
        try:
            proposals = os.listdir(owner_dir)
        except OSError:  # domain: degrade-silently - racing GC, skip owner
            continue
        for pid in proposals:
            try:
                proposal_id = int(pid)
            except (TypeError, ValueError):  # domain: degrade-silently - skip odd dirs
                continue
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
                try:
                    with workspace_lock(dest, allow_missing=True):
                        manifest = _read_manifest(dest)
                        if manifest is None:
                            continue
                        try:
                            key = (
                                int(manifest.get("agent_id", -1)),
                                int(manifest.get("proposal_id", -1)),
                                str(manifest.get("name", "")),
                            )
                        except (
                            TypeError,
                            ValueError,
                        ):  # domain: degrade-silently - no owner
                            continue
                        if key != (agent_id, proposal_id, claim):
                            continue
                        current_live = live() if callable(live) else live
                        if current_live is None:
                            continue
                        if key not in current_live and _retire_claim_tree_locked(dest):
                            swept += 1
                except (
                    OSError,
                    RepoError,
                ):  # domain: degrade-silently - sweep one tree
                    continue
    return swept
