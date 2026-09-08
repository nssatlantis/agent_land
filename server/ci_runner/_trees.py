"""server.ci_runner._trees — runner trees: dirs, git seed/clone, prepare paths."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess

import config
import db
import github
from github._core import _validate_path


def _runner_dir_impl(slot: int) -> str:
    """Core path construction for runner trees â€” slot 0 is the historic
    base, slot N is sharded. Never patched directly; tests patch _runner_dir."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", github.GITHUB_REPO)
    base = os.path.join(config.DATA_DIR, "agentland_ws", slug + "-ci")
    d = f"{base}-{slot}" if slot != 0 else base
    os.makedirs(d, exist_ok=True)
    return d


def _runner_dir() -> str:
    """Legacy single runner checkout â€” kept for backwards compatibility in
    tests that import it directly. New code uses _runner_dir_for_slot()."""
    return _runner_dir_impl(0)


_ORIG_RUNNER_DIR = _runner_dir  # for mock detection


def _runner_dir_for_slot(slot: int) -> str:
    """Dedicated runner checkout for *slot* beside the rebase pool slots â€”
    same durable home (AGENTLAND_DATA_DIR/agentland_ws) but never a pool
    slot, so a long suite can never starve conflict/rebase flows. Two
    slots (CI_RUN_CONCURRENCY=2) give two independent -ci trees. NOTE:
    these CI trees (agentland_ws/<slug>-ci[-N]) are a SEPARATE system from
    the git workspace pool (agentland_ws/<slug>/slotN, github/_gitops.py)
    - independent lifecycles, never interchanged."""
    # If tests have monkeypatched _runner_dir to a stub, respect it for any
    # slot â€” the fixture's tree is the same temp dir for all slots in that test.
    if _runner_dir is not _ORIG_RUNNER_DIR:
        return _runner_dir()
    return _runner_dir_impl(slot)


def _git(tree: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", tree, *args],
        capture_output=True,
        text=True,
        timeout=config.CI_RUN_GIT_TIMEOUT,
    )


def _local_seed_available() -> bool:
    try:
        return os.path.isdir(os.path.join(str(config.REPO_DIR), ".git"))
    except Exception:
        # domain: degrade-silently - REPO_DIR unreadable, no local seed
        return False


def _try_clone_from_local(tree: str, base: str) -> bool:
    """Attempt to clone the CI runner tree from the local REPO_DIR seed.
    Returns True on success, False to fall back to origin. The seed is the
    auto-update checkout (always up-to-date); we rewire origin afterwards."""
    if not _local_seed_available():
        return False
    # Never use local seed when tests mock the remote to a file:// bare fixture
    try:
        origin_url = github._repo_url()
    except Exception:
        # domain: degrade-silently - _repo_url failed, fallback to origin
        return False
    if not origin_url.startswith("https://github.com/"):
        return False
    local_path = str(config.REPO_DIR)
    # Clone from local path (file://) â€” no network, always up-to-date
    try:
        res = subprocess.run(
            ["git", "clone", "--branch", base, "--single-branch", local_path, tree],
            capture_output=True,
            text=True,
            timeout=config.CI_RUN_CLONE_TIMEOUT,
        )
        if res.returncode != 0:
            return False
        # Rewire origin to canonical GitHub URL for later fetches
        subprocess.run(
            ["git", "-C", tree, "remote", "set-url", "origin", origin_url],
            capture_output=True,
            text=True,
            timeout=config.CI_RUN_GIT_TIMEOUT,
        )
        return True
    except Exception:
        # domain: degrade-silently - local seed failed, fallback to origin
        return False


def _ensure_clone(tree: str) -> None:
    base = github.base_branch()
    if os.path.isdir(os.path.join(tree, ".git")):
        return
    # Prefer local seed (auto-update checkout) â€” always up-to-date, no network
    if _try_clone_from_local(tree, base):
        github._seed_identity(tree)
        return
    clone = subprocess.run(
        ["git", "clone", "--branch", base, "--single-branch", github._repo_url(), tree],
        capture_output=True,
        text=True,
        timeout=config.CI_RUN_CLONE_TIMEOUT,
    )
    if clone.returncode != 0:
        raise db.ForumError(
            f"could not clone the repository for the CI runner: "
            f"{(clone.stderr or clone.stdout).strip()[-300:]}"
        )
    # Merge-preview creates merge commits, which need a committer identity;
    # production hosts may have no global git config (see #382).
    github._seed_identity(tree)


def _refresh_main(tree: str) -> str:
    """Fetch and hard-reset onto origin/<base>; returns the main sha."""
    base = github.base_branch()
    fetch = _git(tree, "fetch", "--force", "origin", base)
    if fetch.returncode != 0:
        raise db.ForumError(
            f"could not refresh the CI runner tree from origin/{base}: "
            f"{(fetch.stderr or fetch.stdout).strip()[-300:]}"
        )
    reset = _git(tree, "reset", "--hard", "FETCH_HEAD")
    if reset.returncode != 0:
        # domain: degrade-loudly - an unrestorable tree must not silently
        # serve stale code; recreate it from scratch on the next attempt.
        shutil.rmtree(tree, ignore_errors=True)
        raise db.ForumError(
            "CI runner tree could not be reset to origin/"
            f"{base}; it will be recloned on the next run"
        )
    clean = _git(tree, "clean", "-xdf")
    if clean.returncode != 0:
        # domain: degrade-silently - leftover untracked files slow runs but
        # reset --hard already pinned tracked content to origin/main.
        pass
    head = _git(tree, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise db.ForumError("CI runner tree has no resolvable HEAD after refresh")
    return head.stdout.strip()


def _prepare_tree(slot: int | None = None) -> tuple[str, str]:
    """Return (tree_dir, head_sha) for a fresh origin/main checkout."""
    tree = _runner_dir_for_slot(slot) if slot is not None else _runner_dir()
    _ensure_clone(tree)
    return tree, _refresh_main(tree)


def _prepare_pr_tree(pr_number: int, slot: int | None = None) -> tuple[str, str, dict]:
    """Merge origin/main into the PR head inside the runner tree and return
    ``(tree, merge_commit_sha, merge_info)``.  On conflict no execution
    happens: the caller reports the conflicting files instead."""
    tree = _runner_dir_for_slot(slot) if slot is not None else _runner_dir()
    _ensure_clone(tree)
    pr_fetch = _git(tree, "fetch", "--force", "origin", f"pull/{pr_number}/head")
    if pr_fetch.returncode != 0:
        raise db.ForumError(
            f"could not fetch the head of pull request #{pr_number} "
            "(unknown PR, or its branch was deleted?): "
            f"{(pr_fetch.stderr or pr_fetch.stdout).strip()[-300:]}"
        )
    pr_sha = _git(tree, "rev-parse", "FETCH_HEAD").stdout.strip()
    main_sha = _refresh_main(tree)
    checkout = _git(tree, "checkout", "--detach", main_sha)
    if checkout.returncode != 0:
        raise db.ForumError(
            f"CI runner could not check out main for the merge preview: "
            f"{checkout.stderr.strip()[-300:]}"
        )
    merge = _git(tree, "merge", "--no-edit", pr_sha)
    if merge.returncode != 0:
        conflicted = [
            line.strip()
            for line in _git(
                tree, "diff", "--name-only", "--diff-filter=U"
            ).stdout.splitlines()
            if line.strip()
        ]
        abort = _git(tree, "merge", "--abort")
        if abort.returncode != 0:
            # domain: degrade-silently - the next run's reset --hard heals
            # any half-merged state; nothing serves stale content meanwhile.
            pass
        return tree, main_sha, {"conflict": True, "files": conflicted}
    head = _git(tree, "rev-parse", "HEAD")
    return tree, head.stdout.strip(), {"conflict": False, "base": main_sha}


def _apply_local_changes(tree: str, changes: list[dict]) -> None:
    """Apply a `files` change list onto `tree` â€” content writes and
    find-replace edits resolved against the tree's current files. Mirrors
    github._writes._apply_edits but reads from the filesystem, not the API.
    Used by local rehearsal (repo_ci_run(files=...)) so an agent can test
    an unpushed diff without a PR."""
    for c in changes:
        # Host-side write â€” must be gated like every other write path.
        # _changes_for_repo_propose is shape-only (see its docstring), so
        # validate here before any os.path.join / open.
        path = _validate_path(c["path"])
        full = os.path.join(tree, path)
        # Content write â€” create/overwrite.
        if "content" in c:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            import github._writes as _writes_c  # local import to avoid cycle

            # Detect base EOL if file exists, else canonical LF.
            target = "\n"
            if os.path.isfile(full):
                try:
                    with open(full, encoding="utf-8", newline="") as _bfh:
                        _base_text = _bfh.read()
                    target = _writes_c._target_eol_for_text(_base_text)
                except Exception:  # domain:degrade-silently - EOL probe fallback
                    target = "\n"
            content = _writes_c._normalize_eol(c["content"], target)
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
            continue
        # Patch write â€” find-replace against the file on disk.
        if "edits" in c:
            if not os.path.isfile(full):
                raise db.ForumError(
                    f"no file at {path!r} to patch - patch mode edits an existing "
                    "file; use 'content' to create a new one."
                )
            # Read without universal-newline translation so a CRLF file stays
            # CRLF in memory - byte-faithful with the open/PR path (which
            # decodes the raw blob with no EOL conversion). Otherwise the
            # file's CRLF becomes LF while \r\n payload replacements survive,
            # leaving MIXED line endings that ruff format --check flags.
            try:
                with open(full, encoding="utf-8", newline="") as fh:
                    text = fh.read()
            except (
                UnicodeDecodeError
            ):  # domain: fail-loudly - a binary patch target surfaces as a user error
                raise db.ForumError(
                    f"cannot patch {path!r} - it is not UTF-8 text (binary file)."
                ) from None
            # Reuse the strict engine from github._writes â€” same errors.
            import github._writes as _writes  # local import to avoid cycle

            target = _writes._target_eol_for_text(text)
            normalized_edits = []
            for _op in c["edits"]:
                _neo = {
                    "find": _writes._normalize_eol(_op["find"], target),
                    "replace": _writes._normalize_eol(_op["replace"], target),
                }
                if "occurrence" in _op:
                    _neo["occurrence"] = _op["occurrence"]
                normalized_edits.append(_neo)
            new_text, _log = _writes._apply_edits(path, text, normalized_edits)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            # Write verbatim (newline="") so CRLF originals and \r\n
            # replacements land byte-faithful, like the open/PR path.
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_text)
            continue
        # Should not reach â€” validated earlier.
        raise db.ForumError(f"change for {path!r} has no content or edits.")


def _prepare_local_tree(
    changes: list[dict], slot: int | None = None
) -> tuple[str, str, dict]:
    """Refresh onto origin/main in `slot`'s runner tree, overlay `changes`,
    and return (tree, head_sha, info). No merge, no fetch of a PR head â€”
    this is the pre-push rehearsal path. The tree is left dirty with the
    overlay; the next _refresh_main heals it."""
    tree = _runner_dir_for_slot(slot) if slot is not None else _runner_dir()
    _ensure_clone(tree)
    main_sha = _refresh_main(tree)
    # Overlay the draft changes â€” each path is gated by
    # github._core._validate_path in _apply_local_changes before any host
    # write (repo_helpers is shape-only).
    _apply_local_changes(tree, changes)
    # Head is main plus overlay; hash the overlay for an auditable sha.
    overlay_hash = hashlib.sha256(
        "|".join(f"{c['path']}:{c.get('content', '')[:64]}" for c in changes).encode()
    ).hexdigest()[:12]
    head_sha = f"{main_sha[:12]}+local-{overlay_hash}"
    return tree, head_sha, {"conflict": False, "base": main_sha, "local": True}
