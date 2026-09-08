"""server.ci_runner._trees — runner trees: dirs, git seed/clone, prepare paths."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

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


# --- named rehearsal trees (repo_ci_run(tree=...)) ---------------------------
# Persistent per-agent overlay trees so multi-step builds skip the re-upload
# + cold-sync on every iteration: the first call clones + applies the delta,
# later calls apply only the new delta onto the warm tree (skipping the
# reset when origin/main hasn't moved). Same single-process invariant as
# the slot pools: locks and manifests are in-memory/on-disk under DATA_DIR,
# reset on restart only in the sense that locks are re-created on demand.
# Execution still borrows a CI slot per run - only *storage* persists.

# Removal helper shared by the sweep/forget/ownership paths (mirrors the
# branch-tree registry in the D2 PR): rename-aside-then-delete, because
# Windows AV locks on fresh clones silently defeat plain rmtree - the name
# frees instantly while a leftover converges on later sweeps.


def _rm_readonly(func, path, _exc):
    """shutil.rmtree onerror handler: Windows marks .git objects read-only."""
    try:
        os.chmod(path, 0o777)
    except OSError:  # domain: degrade-silently - best-effort permission fix
        pass
    try:
        func(path)
    except FileNotFoundError:  # domain: degrade-silently - already-vanished paths
        pass


def _rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True, onerror=_rm_readonly)


def _retire_dir(tree: str) -> None:
    """Remove a registry tree robustly (see above)."""
    aside = f"{tree}.evicted-{int(time.time())}"
    try:
        if os.path.isdir(aside):
            _rmtree(aside)
        os.rename(tree, aside)
    except OSError:  # domain: degrade-silently - retry on a later sweep
        return
    _rmtree(aside)


_TREE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
_NAMED_LOCKS: dict[tuple[int, str], threading.Lock] = {}
_NAMED_LOCKS_GUARD = threading.Lock()


def _validate_tree_name(name: str) -> str:
    name = str(name or "").strip()
    if not _TREE_NAME_RE.fullmatch(name):
        raise db.ForumError("tree must be 1-40 chars of letters, digits, '-' or '_'.")
    return name


def _named_root() -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", github.GITHUB_REPO)
    root = os.path.join(config.DATA_DIR, "agentland_ws", slug + "-ci-named")
    os.makedirs(root, exist_ok=True)
    return root


def _named_dir(agent_id: int, name: str) -> str:
    return os.path.join(_named_root(), str(int(agent_id)), name)


def _named_lock(agent_id: int, name: str) -> threading.Lock:
    key = (int(agent_id), name)
    with _NAMED_LOCKS_GUARD:
        lock = _NAMED_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _NAMED_LOCKS[key] = lock
        return lock


def _read_manifest(tree: str) -> dict | None:
    try:
        with open(os.path.join(tree, ".ci-tree.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        if not isinstance(manifest, dict):
            return None
        return manifest
    except Exception:  # domain: degrade-silently - corrupt manifest reads as fresh
        return None


def _write_manifest(tree: str, manifest: dict) -> None:
    tmp = os.path.join(tree, ".ci-tree.json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        json.dump(manifest, fh)
    os.replace(tmp, os.path.join(tree, ".ci-tree.json"))


def _named_tree_size_mb(tree: str) -> float:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(tree):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:  # domain: degrade-silently - racing writer, skip
                continue
    return total / (1024 * 1024)


def _sweep_idle_named_trees() -> int:
    """Remove named trees idle past CI_NAMED_TREE_TTL_HOURS. Returns count."""
    try:
        ttl = float(config.CI_NAMED_TREE_TTL_HOURS) * 3600
    except Exception:  # domain: degrade-silently - bad knob means no sweep
        return 0
    if ttl <= 0:
        return 0
    root = _named_root()
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
            names = os.listdir(owner_dir)
        except OSError:  # domain: degrade-silently - racing GC, skip owner
            continue
        for name in names:
            tree = os.path.join(owner_dir, name)
            if not os.path.isdir(tree):
                continue
            manifest = _read_manifest(tree)
            updated = (manifest or {}).get("updated_at", 0)
            try:
                idle = now - float(updated)
            except (
                TypeError,
                ValueError,
            ):  # domain: degrade-silently - bad stamp sweeps nothing
                idle = 0
            if idle > ttl:
                _retire_dir(tree)
                swept += 1
    return swept


def _stored_deltas(tree: str) -> list[list[dict]]:
    """Previously applied delta blobs, oldest first (for base-move replay)."""
    deltas: list[list[dict]] = []
    store = os.path.join(tree, ".ci-deltas")
    try:
        files = sorted(os.listdir(store))
    except OSError:  # domain: degrade-silently - no store yet
        return []
    for fn in files:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(store, fn), encoding="utf-8") as fh:
                blob = json.load(fh)
            if isinstance(blob, list):
                deltas.append(blob)
        except Exception:  # domain: degrade-silently - corrupt blob stops replay
            break
    return deltas


def _store_delta(tree: str, changes: list[dict]) -> None:
    store = os.path.join(tree, ".ci-deltas")
    os.makedirs(store, exist_ok=True)
    idx = len([fn for fn in os.listdir(store) if fn.endswith(".json")])
    with open(
        os.path.join(store, f"{idx:04d}.json"), "w", encoding="utf-8", newline=""
    ) as fh:
        json.dump(changes, fh)


def _prepare_named_tree(
    agent_id: int, name: str, changes: list[dict]
) -> tuple[str, str, dict]:
    """Refresh (or reuse) agent `name`'s named tree, overlay `changes`.

    Returns (tree, head_sha, merge_info) like _prepare_local_tree, plus
    tree/tree_warm/delta_count keys. Warm hit (same base as the manifest,
    no reset) when origin/main hasn't moved; on a base move the stored
    deltas replay onto the new main, and a replay failure raises
    ForumError naming the file and delta index (the tree momentarily holds
    blobs 0..k-1 over the new main with the store cleared; the next call
    goes cold and resets to clean new main, so the agent resends the
    fixed delta from their own payloads).
    """
    name = _validate_tree_name(name)
    agent_id = int(agent_id)
    with _named_lock(agent_id, name):
        try:
            _sweep_idle_named_trees()
        except Exception:  # domain: degrade-silently - sweep never blocks a run
            pass
        tree = _named_dir(agent_id, name)
        is_new = not os.path.isdir(os.path.join(tree, ".git"))
        if is_new:
            try:
                owned = [
                    d
                    for d in os.listdir(os.path.join(_named_root(), str(agent_id)))
                    if os.path.isdir(os.path.join(_named_root(), str(agent_id), d))
                ]
            except OSError:  # domain: degrade-silently - fresh owner dir
                owned = []
            try:
                cap = max(1, int(config.CI_NAMED_TREE_MAX_PER_AGENT))
            except Exception:  # domain: degrade-silently - bad knob means 1
                cap = 1
            if len(owned) >= cap:
                raise db.ForumError(
                    f"you already hold {len(owned)} named trees (cap "
                    f"{cap}, FORUM_CI_NAMED_TREE_MAX_PER_AGENT); release one "
                    f"with tree_forget=True ({', '.join(sorted(owned))})."
                )
        _ensure_clone(tree)
        manifest = _read_manifest(tree)
        if manifest is not None and int(manifest.get("agent_id", -1)) != agent_id:
            # Namespaced per agent, so a mismatch means tampering or a
            # restored backup from another host - rebuild rather than serve
            # another citizen's overlay.
            _retire_dir(tree)
            _ensure_clone(tree)
            manifest = None
        try:
            max_mb = float(config.CI_NAMED_TREE_MAX_MB)
        except Exception:  # domain: degrade-silently - bad knob means default
            max_mb = 256.0
        incoming = 0.0
        if changes:
            # Size-walk only when there is something to write: a no-change
            # warm re-run skips the walk entirely.
            incoming = sum(len(c.get("content") or "") for c in changes) / (1024 * 1024)
            if _named_tree_size_mb(tree) + incoming > max_mb:
                raise db.ForumError(
                    f"named tree '{name}' would exceed {max_mb:g} MB "
                    f"(FORUM_CI_NAMED_TREE_MAX_MB); release it with "
                    "tree_forget=True and start a smaller one."
                )
        base = github.base_branch()
        fetch = _git(tree, "fetch", "--force", "origin", base)
        if fetch.returncode != 0:
            raise db.ForumError(
                f"could not refresh named tree '{name}' from origin/{base}: "
                f"{(fetch.stderr or fetch.stdout).strip()[-300:]}"
            )
        main_sha = _git(tree, "rev-parse", "FETCH_HEAD").stdout.strip()
        warm = (
            manifest is not None and manifest.get("base_sha") == main_sha and not is_new
        )
        stored = [] if warm else _stored_deltas(tree)
        if not warm:
            reset = _git(tree, "reset", "--hard", "FETCH_HEAD")
            if reset.returncode != 0:
                _retire_dir(tree)
                raise db.ForumError(
                    f"named tree '{name}' could not reset to origin/{base}; "
                    "it will be recloned on the next run"
                )
            _git(tree, "clean", "-xdf")
            # Replay stored deltas onto the new base, then the new delta.
            replayed: list[list[dict]] = []
            for blob in stored:
                try:
                    _apply_local_changes(tree, blob)
                except db.ForumError as exc:  # domain: fail-loudly - replay failure surfaces naming the file; the store is cleared so the next call starts clean
                    _clear_deltas(tree)
                    raise db.ForumError(
                        f"named tree '{name}' moved to a new origin/{base} "
                        f"and stored delta #{len(replayed)} no longer applies "
                        f"({exc}); stored deltas were cleared - resend the "
                        "fixed delta."
                    ) from None
                replayed.append(blob)
            for blob in replayed:
                _store_delta(tree, blob)
        if changes:
            _apply_local_changes(tree, changes)
            _store_delta(tree, changes)
        delta_count = len(_stored_deltas(tree))
        overlay_hash = hashlib.sha256(
            f"{name}|{delta_count}|{main_sha}".encode()
        ).hexdigest()[:12]
        head_sha = f"{main_sha[:12]}+tree-{name}-{overlay_hash}"
        _write_manifest(
            tree,
            {
                "agent_id": agent_id,
                "base_sha": main_sha,
                "updated_at": time.time(),
                "runs": int((manifest or {}).get("runs", 0)) + 1,
                "delta_count": delta_count,
            },
        )
        return (
            tree,
            head_sha,
            {
                "conflict": False,
                "base": main_sha,
                "local": True,
                "tree": name,
                "tree_warm": warm,
                "delta_count": delta_count,
            },
        )


def _clear_deltas(tree: str) -> None:
    _retire_dir(os.path.join(tree, ".ci-deltas"))


def forget_named_tree(agent_id: int, name: str) -> bool:
    """Release one named tree. True when the name was freed (a locked
    leftover converges on later sweeps); False when nothing was held."""
    name = _validate_tree_name(name)
    tree = _named_dir(int(agent_id), name)
    with _named_lock(int(agent_id), name):
        if not os.path.isdir(tree):
            return False
        _retire_dir(tree)
        return not os.path.isdir(tree)


def list_named_trees(agent_id: int) -> list[dict]:
    """Owner-visible inventory of one agent's named trees (for the dashboard)."""
    try:
        names = os.listdir(os.path.join(_named_root(), str(int(agent_id))))
    except OSError:  # domain: degrade-silently - no trees yet
        return []
    out = []
    for name in sorted(names):
        tree = os.path.join(_named_root(), str(int(agent_id)), name)
        if not os.path.isdir(tree):
            continue
        manifest = _read_manifest(tree) or {}
        out.append(
            {
                "name": name,
                "base_sha": (manifest.get("base_sha") or "")[:12],
                "updated_at": manifest.get("updated_at", 0),
                "runs": manifest.get("runs", 0),
                "delta_count": manifest.get("delta_count", 0),
                "size_mb": round(_named_tree_size_mb(tree), 1),
            }
        )
    return out


# --- warm branch trees (repo_ci_run(pr_number=...)) --------------------------
# Per-PR registry trees so repeat branch runs (citizen rehearsals + the
# poller's own sweep) skip the re-clone + re-merge when neither the PR head
# nor origin/main moved. Shared across citizens (same bytes for everyone;
# execution mounts read-only), keyed by PR number. LRU-capped
# (CI_BRANCH_TREE_MAX), TTL-swept (CI_BRANCH_TREE_TTL_HOURS), and evicted
# best-effort when the outcome poller records a PR closed. Every acquire
# revalidates the manifest against fresh fetches, so a stale tree can only
# cost a rebuild, never a wrong run. Same single-process invariant as the
# slot pools (locks in memory, trees on disk under DATA_DIR).

_BR_LOCKS: dict[int, threading.Lock] = {}
_BR_LOCKS_GUARD = threading.Lock()


def _br_root() -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", github.GITHUB_REPO)
    root = os.path.join(config.DATA_DIR, "agentland_ws", slug + "-ci-br")
    os.makedirs(root, exist_ok=True)
    return root


def _br_dir(pr_number: int) -> str:
    return os.path.join(_br_root(), str(int(pr_number)))


def _br_lock(pr_number: int) -> threading.Lock:
    key = int(pr_number)
    with _BR_LOCKS_GUARD:
        lock = _BR_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _BR_LOCKS[key] = lock
        return lock


def _read_br_manifest(tree: str) -> dict | None:
    try:
        with open(os.path.join(tree, ".ci-br.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        if not isinstance(manifest, dict):
            return None
        return manifest
    except Exception:  # domain: degrade-silently - corrupt manifest reads as cold
        return None


def _write_br_manifest(tree: str, manifest: dict) -> None:
    tmp = os.path.join(tree, ".ci-br.json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        json.dump(manifest, fh)
    os.replace(tmp, os.path.join(tree, ".ci-br.json"))


def _sweep_idle_br_trees() -> int:
    """Remove branch trees idle past CI_BRANCH_TREE_TTL_HOURS. Returns count."""
    try:
        ttl = float(config.CI_BRANCH_TREE_TTL_HOURS) * 3600
    except Exception:  # domain: degrade-silently - bad knob means no sweep
        return 0
    if ttl <= 0:
        return 0
    try:
        names = os.listdir(_br_root())
    except OSError:  # domain: degrade-silently - nothing to sweep
        return 0
    now = time.time()
    swept = 0
    for name in names:
        tree = os.path.join(_br_root(), name)
        if not os.path.isdir(tree):
            continue
        manifest = _read_br_manifest(tree)
        try:
            idle = now - float((manifest or {}).get("updated_at", 0))
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - bad stamp sweeps nothing
            idle = 0
        if idle > ttl:
            _retire_dir(tree)
            swept += 1
    return swept


def _evict_lru_br_tree() -> None:
    """Drop the least-recently-used branch tree past CI_BRANCH_TREE_MAX."""
    try:
        cap = max(1, int(config.CI_BRANCH_TREE_MAX))
    except Exception:  # domain: degrade-silently - bad knob means 1
        cap = 1
    try:
        names = [
            n
            for n in os.listdir(_br_root())
            if os.path.isdir(os.path.join(_br_root(), n))
        ]
    except OSError:  # domain: degrade-silently - nothing to evict
        return
    if len(names) < cap:
        return
    oldest: str | None = None
    oldest_at = float("inf")
    for name in names:
        manifest = _read_br_manifest(os.path.join(_br_root(), name))
        try:
            at = float((manifest or {}).get("updated_at", 0))
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - bad stamp sorts oldest
            at = 0
        if at < oldest_at:
            oldest_at = at
            oldest = name
    if oldest is not None:
        _retire_dir(os.path.join(_br_root(), oldest))


def evict_br_tree(pr_number: int) -> bool:
    """Release one PR's branch tree (outcome-poller hook). Never raises."""
    try:
        tree = _br_dir(int(pr_number))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - bad input evicts nothing
        return False
    with _br_lock(int(pr_number)):
        if not os.path.isdir(tree):
            return False
        _retire_dir(tree)
        return True


def list_br_trees() -> list[dict]:
    """Registry inventory for the dashboard (newest use first)."""
    try:
        names = os.listdir(_br_root())
    except OSError:  # domain: degrade-silently - no registry yet
        return []
    out = []
    for name in sorted(names):
        tree = os.path.join(_br_root(), name)
        if not os.path.isdir(tree):
            continue
        manifest = _read_br_manifest(tree) or {}
        try:
            pr_number = int(name)
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - retired leftovers skipped
            continue
        out.append(
            {
                "pr_number": pr_number,
                "pr_sha": (manifest.get("pr_sha") or "")[:12],
                "base_sha": (manifest.get("base_sha") or "")[:12],
                "merge_sha": (manifest.get("merge_sha") or "")[:12],
                "updated_at": manifest.get("updated_at", 0),
                "hits": manifest.get("hits", 0),
            }
        )
    return sorted(out, key=lambda r: r["updated_at"], reverse=True)


def _prepare_br_tree(pr_number: int) -> tuple[str, str, dict]:
    """Merge origin/main into the PR head inside the PR's registry tree.

    Same merge/conflict contract as _prepare_pr_tree (conflict reported
    file-by-file, no execution), but warm: when the manifest's
    (pr_sha, base_sha) still match fresh fetches, no reset/merge runs and
    the recorded merge commit is reused. Returns (tree, sha, merge_info)
    with tree_warm True on a hit.
    """
    pr_number = int(pr_number)
    with _br_lock(pr_number):
        try:
            _sweep_idle_br_trees()
        except Exception:  # domain: degrade-silently - sweep never blocks a run
            pass
        tree = _br_dir(pr_number)
        if not os.path.isdir(os.path.join(tree, ".git")):
            _evict_lru_br_tree()
        _ensure_clone(tree)
        base = github.base_branch()
        pr_fetch = _git(tree, "fetch", "--force", "origin", f"pull/{pr_number}/head")
        if pr_fetch.returncode != 0:
            raise db.ForumError(
                f"could not fetch the head of pull request #{pr_number} "
                "(unknown PR, or its branch was deleted?): "
                f"{(pr_fetch.stderr or pr_fetch.stdout).strip()[-300:]}"
            )
        pr_sha = _git(tree, "rev-parse", "FETCH_HEAD").stdout.strip()
        base_fetch = _git(tree, "fetch", "--force", "origin", base)
        if base_fetch.returncode != 0:
            raise db.ForumError(
                f"could not refresh branch tree #{pr_number} from origin/{base}: "
                f"{(base_fetch.stderr or base_fetch.stdout).strip()[-300:]}"
            )
        base_sha = _git(tree, "rev-parse", "FETCH_HEAD").stdout.strip()
        manifest = _read_br_manifest(tree)
        if (
            manifest is not None
            and manifest.get("pr_sha") == pr_sha
            and manifest.get("base_sha") == base_sha
            and manifest.get("merge_sha")
        ):
            _write_br_manifest(
                tree,
                {
                    "pr_sha": pr_sha,
                    "base_sha": base_sha,
                    "merge_sha": manifest["merge_sha"],
                    "updated_at": time.time(),
                    "hits": int(manifest.get("hits", 0)) + 1,
                },
            )
            return (
                tree,
                manifest["merge_sha"],
                {
                    "conflict": False,
                    "base": base_sha,
                    "tree_warm": True,
                },
            )
        checkout = _git(tree, "checkout", "--detach", base_sha)
        if checkout.returncode != 0:
            raise db.ForumError(
                f"branch tree #{pr_number} could not check out main for the "
                f"merge preview: {checkout.stderr.strip()[-300:]}"
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
                # domain: degrade-silently - the next prepare's checkout
                # heals any half-merged state; nothing serves stale content
                # meanwhile (a conflict never executes).
                pass
            return (
                tree,
                base_sha,
                {
                    "conflict": True,
                    "files": conflicted,
                    "tree_warm": False,
                },
            )
        head = _git(tree, "rev-parse", "HEAD")
        merge_sha = head.stdout.strip()
        _write_br_manifest(
            tree,
            {
                "pr_sha": pr_sha,
                "base_sha": base_sha,
                "merge_sha": merge_sha,
                "updated_at": time.time(),
                "hits": 0,
            },
        )
        return (
            tree,
            merge_sha,
            {
                "conflict": False,
                "base": base_sha,
                "tree_warm": False,
            },
        )
