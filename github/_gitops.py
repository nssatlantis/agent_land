"""github._gitops - local git flows: merge-conflict tooling and rebases.

Everything that shells out to the git binary lives here: the workspace
pool (warm clones under AGENTLAND_DATA_DIR with a normalize-on-acquire
contract), the conflict-marker parser, detect_merge_conflicts /
apply_merge_resolutions / rebase_pr_onto_main, and the push-auth
contextmanager that scopes the PAT to the push itself.
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import config

from . import _core
from ._core import GITHUB_BASE_BRANCH, GITHUB_REPO, RepoError

_CONTEXT_LINES = 3


def _normalize_eol(text: str, target: str) -> str:
    """Normalize *text* to *target* EOL ("\\n" or "\\r\\n"). Binary-safe."""
    if "\0" in text:
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "\r\n":
        return normalized.replace("\n", "\r\n")
    return normalized


def _target_eol_for_text(base_text: str | None) -> str:
    """Pick EOL for a file: CRLF if base contains CRLF, else LF."""
    if base_text is None or base_text == "":
        return "\n"
    if "\0" in base_text:
        return "\n"
    if "\r\n" in base_text:
        return "\r\n"
    return "\n"


def _parse_conflict_markers(text: str) -> list[dict]:
    """Parse git conflict markers from a file's content.  Returns a list of
        conflict regions, each with ``line`` (1-based start of ``<<<<<<<``),
        ``ours``, ``theirs``, ``context_before`` and ``context_after``.

        Handles standard git markers (``<<<<<<<``, ``=======``, ``>>>>>>>``)
        and diff3-style markers (``|||||||`` base section between ``<<<<<<<``
        and the first ``=======``).  Uses ``startswith`` with a trailing space (to allow ``<<<<<<< HEAD``)
    or exact match (for bare ``<<<<<<<``), so code lines that begin with a
    marker-like prefix but lack the space separator are not false-positived."""
    lines = text.splitlines()
    regions: list[dict] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<< ") or lines[i] == "<<<<<<<":
            start = i  # 0-based index of the <<<<<<< line
            ours_lines: list[str] = []
            i += 1
            # Skip diff3 base section if present (||||||| ... =======)
            if i < len(lines) and lines[i].startswith("|||||||"):
                i += 1
                while i < len(lines) and lines[i] != "=======":
                    i += 1
            # Now parse ours
            while i < len(lines) and lines[i] != "=======":
                ours_lines.append(lines[i])
                i += 1
            # skip =======
            i += 1
            theirs_lines: list[str] = []
            while i < len(lines) and not (
                lines[i] == ">>>>>>>" or lines[i].startswith(">>>>>>> ")
            ):
                theirs_lines.append(lines[i])
                i += 1
            # skip >>>>>>>
            i += 1
            ctx_before = lines[max(0, start - _CONTEXT_LINES) : start]
            ctx_after = lines[i : i + _CONTEXT_LINES]
            regions.append(
                {
                    "line": start + 1,  # 1-based
                    "ours": "\n".join(ours_lines),
                    "theirs": "\n".join(theirs_lines),
                    "context_before": "\n".join(ctx_before),
                    "context_after": "\n".join(ctx_after),
                }
            )
        else:
            i += 1
    return regions


def _repo_url(with_token: bool = False) -> str:
    """Build the clone/push URL for the repo.  When *with_token* is True,
    embed the PAT (URL-encoded) for authenticated push.  When False, return
    the plain public URL (the repo is public, no auth needed to read)."""
    base = f"https://github.com/{GITHUB_REPO}.git"
    if not with_token:
        return base
    _core._ensure_token()
    encoded = urllib.parse.quote(_core.GITHUB_TOKEN, safe="")
    return f"https://x-access-token:{encoded}@github.com/{GITHUB_REPO}.git"


def _git(repo_dir: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in *repo_dir*.  Raises RepoError on failure.
    Sets GIT_TERMINAL_PROMPT=0 so git never prompts for credentials.
    Scrubs the GitHub token from any output so it never leaks into
    error messages."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if check and result.returncode != 0:
            stderr = result.stderr
            msg = f"git {' '.join(args)} failed:\n{stderr.strip()}"
            if _core.GITHUB_TOKEN:
                msg = msg.replace(_core.GITHUB_TOKEN, "<redacted>")
                encoded = urllib.parse.quote(_core.GITHUB_TOKEN, safe="")
                msg = msg.replace(encoded, "<redacted>")
            raise RepoError(msg)
        return result
    except subprocess.TimeoutExpired as e:
        msg = f"git {' '.join(args)} timed out"
        if _core.GITHUB_TOKEN:
            msg = msg.replace(_core.GITHUB_TOKEN, "<redacted>")
            encoded = urllib.parse.quote(_core.GITHUB_TOKEN, safe="")
            msg = msg.replace(encoded, "<redacted>")
        raise RepoError(msg) from e
    except FileNotFoundError:
        raise RepoError("git is not installed or not in PATH") from None


# --- persistent git workspace pool (merge-conflict family) -----------------
# Three flows pay a full network clone per call today. The pool keeps
# FORUM_GIT_WORKSPACE_POOL warm clones alive between calls: acquire a slot,
# normalize it (fetch all remote branches when TTL-stale; scrub leftovers if
# the previous operation failed), run the flow verbatim, release. The locks
# are IN-MEMORY - a queue of slot tokens. Deployment is single-process, so
# process death resets everything cleanly and no stale lockfile can exist.

_workspace_queue: queue.Queue[int] | None = None
_ws_slots: list[dict] = []
_ws_lock = threading.Lock()


def _ws_mode_persistent() -> bool:
    return config.GIT_WORKSPACE_MODE == "persistent"


def _ws_root() -> str:
    """Durable workspace home - co-located with the forum's own data under
    AGENTLAND_DATA_DIR, so the pool survives reboots and tmp-sweeper
    policies. Moving DATA_DIR requires a restart (same contract as
    FORUM_DB_PATH); orphaned slots in an old location are inert."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", GITHUB_REPO)
    root = os.path.join(config.DATA_DIR, "agentland_ws", slug)
    os.makedirs(root, exist_ok=True)
    return root


def _ws_ensure_pool() -> queue.Queue[int]:
    """Size the slot pool to the CURRENT configured value - the knob takes
    effect immediately, no restart needed. Growth appends fresh slots;
    shrinking truncates the slot list and rebuilds the token queue, so
    surplus tokens vanish even while never released back. A slot held
    during a resize finishes its operation against its own dict reference;
    a token for a retired index is dropped at release time instead of
    requeued. Retired slot directories stay on disk, inert like any
    orphaned workspace under _ws_root(), and are reused if the pool grows
    back (normalize treats them as pre-existing workspaces)."""
    global _workspace_queue, _ws_slots
    with _ws_lock:
        desired = max(1, int(config.GIT_WORKSPACE_POOL))
        if _workspace_queue is None:
            base = _ws_root()
            _ws_slots = [
                {
                    "dir": os.path.join(base, f"slot{i}"),
                    "last_fetch": 0.0,
                    "dirty": False,
                }
                for i in range(desired)
            ]
            q: queue.Queue[int] = queue.Queue()
            for i in range(desired):
                q.put(i)
            _workspace_queue = q
        elif desired != len(_ws_slots):
            base = _ws_root()
            if desired > len(_ws_slots):
                for i in range(len(_ws_slots), desired):
                    _ws_slots.append(
                        {
                            "dir": os.path.join(base, f"slot{i}"),
                            "last_fetch": 0.0,
                            "dirty": False,
                        }
                    )
            else:
                del _ws_slots[desired:]
            rebuilt: queue.Queue[int] = queue.Queue()
            for i in range(len(_ws_slots)):
                rebuilt.put(i)
            _workspace_queue = rebuilt
    return _workspace_queue


def _rm_readonly(func, path, _exc):
    """shutil.rmtree onerror handler: Windows marks .git objects read-only,
    and a partial deletion would leave a half-dead directory that breaks
    the next clone into it. Tolerates already-vanished paths."""
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass
    try:
        func(path)
    except FileNotFoundError:
        pass


def _local_seed_available() -> bool:
    """True if the auto-update checkout (REPO_DIR) is a usable seed."""
    try:
        return os.path.isdir(os.path.join(str(config.REPO_DIR), ".git"))
    except Exception:
        # domain: degrade-silently - REPO_DIR unreadable, no local seed
        return False


def _try_clone_from_local(parent: str, dest_name: str) -> bool:
    """Attempt to clone from the local REPO_DIR seed (the auto-update checkout).
    Returns True on success, False on any failure — caller falls back to
    origin. The clone is from the local path, then origin is rewired to the
    canonical GitHub URL so later fetches still hit origin."""
    if not _local_seed_available():
        return False
    # Never use local seed when tests have mocked _repo_url to a bare fixture
    # (file path) — the fixture's branches are not in REPO_DIR, and cloning
    # REPO_DIR would give a warm slot with the wrong history, breaking the
    # pool's no-refetch assertion.
    try:
        origin_url = _repo_url(with_token=False)
    except Exception:
        # domain: degrade-silently - _repo_url failed, fallback to origin
        return False
    if not origin_url.startswith("https://github.com/"):
        return False
    local_path = str(config.REPO_DIR)
    # Never seed from a workspace that is currently checked out as REPO_DIR
    # itself is the running checkout — cloning it is safe, but only when not
    # mid-update (update.sh holds a lock; we fail fast and fall back).
    try:
        res = _git(parent, "clone", local_path, dest_name, check=False)
        if res.returncode != 0:
            return False
        dest = os.path.join(parent, dest_name)
        # Rewire origin to the canonical GitHub URL; the local path was just a seed
        _git(dest, "remote", "set-url", "origin", origin_url, check=False)
        # Ensure we have the origin/main ref locally — the seed may be on a
        # different branch if update.sh is mid-checkout; fetch origin/main explicitly
        # is cheap when already up-to-date and heals a stale seed.
        _git(
            dest,
            "fetch",
            "--prune",
            "origin",
            "+refs/heads/*:refs/remotes/origin/*",
            check=False,
        )
        return True
    except Exception:
        # domain: degrade-silently - local seed failed, fallback to origin clone
        return False


def _ws_fresh_clone(slot: dict) -> None:
    """Rebuild a slot from scratch - the self-heal path; worst case equals
    today's per-call clone cost."""
    parent = os.path.dirname(slot["dir"])
    if os.path.isdir(slot["dir"]):
        shutil.rmtree(slot["dir"], onerror=_rm_readonly)
    os.makedirs(parent, exist_ok=True)
    # Prefer the auto-update checkout as seed (always up-to-date, no network)
    # — fallback to origin on any failure, never mutate a running workspace.
    if _try_clone_from_local(parent, os.path.basename(slot["dir"])):
        _seed_identity(slot["dir"])
        slot["last_fetch"] = time.monotonic()
        slot["dirty"] = False
        return
    _git(parent, "clone", _repo_url(with_token=False), os.path.basename(slot["dir"]))
    _seed_identity(slot["dir"])
    slot["last_fetch"] = time.monotonic()
    slot["dirty"] = False


def _ws_normalize(slot: dict) -> None:
    """Bring a slot to a clean, current view of every remote branch.

    The LOCAL scrub runs on every acquire - never skipped. The merge-family
    flows hardcode `git checkout -b pr_head origin/<head>`, and a leftover
    local branch from a previous operation would make that fatal (legacy
    code survived only because it deleted the whole temp clone). Only the
    network fetch is gated by the TTL: dirty-or-stale slots refresh all
    remote branches; fresh-but-clean ones skip the network because every
    flow fetches its own specific base/head refs at body start anyway."""
    if not os.path.isdir(os.path.join(slot["dir"], ".git")):
        _ws_fresh_clone(slot)
        return
    stale = (time.monotonic() - slot["last_fetch"]) > config.GIT_WORKSPACE_FETCH_TTL
    try:
        if stale:
            _git(
                slot["dir"],
                "fetch",
                "--prune",
                "origin",
                "+refs/heads/*:refs/remotes/origin/*",
            )
            slot["last_fetch"] = time.monotonic()
        _ws_git_scrub(slot["dir"])
        # Heal slots created before identity seeding existed (and keep the
        # guarantee fresh): every acquire leaves the slot commit-ready.
        _seed_identity(slot["dir"])
        slot["dirty"] = False
    except RepoError:
        _ws_fresh_clone(slot)


def _ws_git_scrub(dir_: str) -> None:
    """Best-effort cleanup to the fresh-clone state (no network). Deletes
    every local branch except base: flows create working branches by name
    (`checkout -b pr_head ...`), and a leftover one from an earlier
    operation must not turn that into a fatal error. Also restores the
    anonymous remote URL: a previous operation's push auth must not
    outlive it on a warm slot, and a slot whose process died between
    set-url and push is healed here on the next acquire."""
    _git(
        dir_,
        "checkout",
        "-B",
        GITHUB_BASE_BRANCH,
        f"origin/{GITHUB_BASE_BRANCH}",
        check=False,
    )
    listing = _git(dir_, "branch", "--format=%(refname:short)", check=False)
    if listing.returncode == 0:
        for name in listing.stdout.split():
            name = name.strip()
            if name and name != GITHUB_BASE_BRANCH:
                _git(dir_, "branch", "-D", name, check=False)
    _git(dir_, "reset", "--hard", check=False)
    _git(dir_, "clean", "-fdq", check=False)
    _git(dir_, "remote", "set-url", "origin", _repo_url(with_token=False), check=False)


@contextmanager
def _workspace():
    """Yield a git working directory for one merge-family operation.

    persistent mode: acquire a pool slot (bounded wait) and normalize it;
      any failure marks the slot dirty so the next acquirer scrubs it
      before use. A saturated pool degrades to the legacy temp path -
      warm-when-possible, but never a brand-new failure mode citizens
      didn't have before.
    temp mode (default): legacy behavior - fresh clone per call, cleaned up.
    """
    if not _ws_mode_persistent():
        d = _clone_repo()
        try:
            yield d
        finally:
            _cleanup(d)
        return

    def _temp_fallback():
        d = _clone_repo()
        try:
            yield d
        finally:
            _cleanup(d)

    q = _ws_ensure_pool()
    timeout = max(0.0, float(config.GIT_WORKSPACE_LOCK_TIMEOUT))
    try:
        idx = q.get(timeout=timeout)
    except queue.Empty:
        # Pool saturated: legacy temp clone instead of a new error class.
        yield from _temp_fallback()
        return
    try:
        slot = _ws_slots[idx]
    except IndexError:
        # The pool shrank between issuing this token and our acquire; the
        # slot no longer exists. Retire the token, degrade to temp.
        yield from _temp_fallback()
        return
    try:
        _ws_normalize(slot)
        yield slot["dir"]
    except BaseException:
        slot["dirty"] = True
        raise
    finally:
        # Retired index (pool shrank while we held the slot): drop the
        # token instead of requeueing it.
        if idx < max(1, int(config.GIT_WORKSPACE_POOL)):
            q.put(idx)


# Fallback committer identity for every working tree we create. Deployment
# boxes may have no global git config at all - and git demands a committer
# identity even for operations that do not create a final commit object
# (e.g. `merge --no-commit`, and any rebase, which stamps a new committer
# on replayed commits). Without a seed those operations die with
# "Committer identity unknown". Agent-invoked flows that DO commit pass an
# explicit citizen identity per command instead.
_GIT_IDENTITY_NAME = "AgentLand"
_GIT_IDENTITY_EMAIL = "agentland@local"


def _seed_identity(repo_dir: str) -> None:
    """Write the fallback identity into the repo's LOCAL config (never
    global). Idempotent and cheap; called when trees are created and on
    every pool-slot acquire so slots created by older deploys are healed."""
    _git(repo_dir, "config", "user.email", _GIT_IDENTITY_EMAIL)
    _git(repo_dir, "config", "user.name", _GIT_IDENTITY_NAME)


def _clone_repo() -> str:
    """Clone the repo into a temp directory.  Returns the repo subdir path.
    The clone is anonymous (no auth) since the repo is public; push auth
    is applied push-scoped by ``_push_auth``."""
    tmp = tempfile.mkdtemp(prefix="agentland_merge_")
    # Prefer local seed (auto-update checkout) for temp clones too — always
    # up-to-date and no network when REPO_DIR is available.
    if _try_clone_from_local(tmp, "repo"):
        repo_dir = os.path.join(tmp, "repo")
        _seed_identity(repo_dir)
        return repo_dir
    try:
        _git(tmp, "clone", _repo_url(with_token=False), "repo")
    except RepoError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    repo_dir = os.path.join(tmp, "repo")
    _seed_identity(repo_dir)
    return repo_dir


def _cleanup(repo_dir: str) -> None:
    """Best-effort removal of a temp clone."""
    parent = os.path.dirname(repo_dir)
    shutil.rmtree(parent, ignore_errors=True)


def _abort_merge(repo_dir: str) -> None:
    """Abort any in-progress merge in *repo_dir*."""
    _git(repo_dir, "merge", "--abort", check=False)


@contextmanager
def _push_auth(repo_dir: str):
    """Token the origin URL for one authenticated push, restoring the
    anonymous URL afterwards - no warm workspace (or temp clone awaiting
    cleanup) keeps credentials longer than the push itself. The restore is
    best-effort: a crash here is healed by the next acquire's scrub in
    persistent mode."""
    _core._ensure_token()
    _git(repo_dir, "remote", "set-url", "origin", _repo_url(with_token=True))
    try:
        yield
    finally:
        _git(
            repo_dir,
            "remote",
            "set-url",
            "origin",
            _repo_url(with_token=False),
            check=False,
        )


def _push_ref(branch: str) -> str:
    """Build a HEAD:branch ref string for git push."""
    return f"HEAD:{branch}"


def _safe_path(repo_dir: str, file_path: str) -> str:
    """Resolve *file_path* under *repo_dir* and reject path traversal.
    Returns the resolved absolute path when safe; raises RepoError when
    the resolved path escapes the repository root."""
    real_repo = os.path.realpath(repo_dir)
    fpath = os.path.realpath(os.path.join(repo_dir, file_path))
    if not (fpath == real_repo or fpath.startswith(real_repo + os.sep)):
        raise RepoError(f"path {file_path!r} escapes the repository root")
    return fpath


def _detect_conflict_files(repo_dir: str) -> list[str]:
    """Return the list of unmerged (conflicted) files after a failed merge.
    Uses ``git diff --name-only --diff-filter=U`` to distinguish real merge
    conflicts from other merge failures."""
    result = _git(repo_dir, "diff", "--name-only", "--diff-filter=U", check=False)
    return [f for f in result.stdout.strip().splitlines() if f]


def _has_conflict_markers(text: str) -> bool:
    """Check whether *text* still contains unresolved conflict markers.
    Used to reject resolution content that was not actually cleaned up."""
    for marker in ("<<<<<<<", "=======", ">>>>>>>"):
        if marker in text:
            return True
    return False


def rebase_pr_onto_main(
    number: int,
    *,
    _pr: dict | None = None,
) -> dict:
    """Rebase a PR's head branch onto main via local git.

    Clones the repo, fetches full history, checks out the PR branch,
    rebases onto main, and force-pushes the result.  Returns:

    - {"status": "ok", "new_sha": "<sha>"} on success
    - {"status": "conflict", "files": [...]} when the rebase hits
      conflicts (aborted; the author must resolve manually)

    Raises RepoError for non-conflict failures (network, auth).
    """
    _core._ensure_token()
    pr = _pr or _core._request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    head = pr["head"]["ref"]
    with _workspace() as repo_dir:
        # Unshallow to get the full commit graph needed for rebase.
        _git(repo_dir, "fetch", "--unshallow", "origin", check=False)
        _git(repo_dir, "fetch", "origin", head, GITHUB_BASE_BRANCH)
        _git(repo_dir, "checkout", "-b", "pr_head", f"origin/{head}")
        _seed_identity(repo_dir)
        result = _git(
            repo_dir,
            "rebase",
            f"origin/{GITHUB_BASE_BRANCH}",
            check=False,
        )
        if result.returncode != 0:
            conflicted = _detect_conflict_files(repo_dir)
            _git(repo_dir, "rebase", "--abort", check=False)
            if conflicted:
                return {"status": "conflict", "files": conflicted}
            stderr = result.stderr
            if _core.GITHUB_TOKEN:
                stderr = stderr.replace(_core.GITHUB_TOKEN, "<redacted>")
            raise RepoError(f"rebase failed: {stderr.strip()}")
        # Push rebased branch with authenticated remote.
        with _push_auth(repo_dir):
            _git(
                repo_dir,
                "push",
                "--force-with-lease",
                "origin",
                f"HEAD:{head}",
            )
        new_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        _core._invalidate_pr(number)
        return {"status": "ok", "new_sha": new_sha}


def detect_merge_conflicts(number: int) -> dict:
    """Attempt to merge the base branch into a PR's head branch.

    Returns ``{"status": "clean", ...}`` when the merge is trivial, or
    ``{"status": "conflicts", "conflicts": [...]}`` with structured
    per-file, per-region conflict data so an agent can decide how to
    resolve each one.

    Note: detect is owner-agnostic — any active citizen may call it on
    any open PR.  The operation is read-only and citizenship-rate-limited,
    but triggers a full clone+fetch.  Abuse mitigation is left to the
    existing rate-limit infrastructure.
    """
    pr = _core._request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    head = pr["head"]["ref"]
    base = pr["base"]["ref"]
    with _workspace() as repo_dir:
        _git(repo_dir, "fetch", "origin", base, head)
        _git(repo_dir, "checkout", "-b", "pr_head", f"origin/{head}")
        result = _git(
            repo_dir,
            "merge",
            "--no-commit",
            "--no-ff",
            f"origin/{base}",
            check=False,
        )
        # Distinguish clean merge, conflict, and other failure
        conflicted = _detect_conflict_files(repo_dir)
        if result.returncode == 0 and not conflicted:
            _abort_merge(repo_dir)
            return {
                "status": "clean",
                "pr_number": number,
                "head": head,
                "base": base,
                "message": "No conflicts — the merge is clean.",
            }
        if result.returncode != 0 and not conflicted:
            stderr = result.stderr
            if _core.GITHUB_TOKEN:
                stderr = stderr.replace(_core.GITHUB_TOKEN, "<redacted>")
            raise RepoError(f"merge failed (not a conflict): {stderr.strip()}")
        # Conflicts — read each conflicted file for structured data
        conflicts: list[dict[str, Any]] = []
        for fpath in conflicted:
            try:
                safe = _safe_path(repo_dir, fpath)
                text = Path(safe).read_text(encoding="utf-8", errors="replace")
            except (OSError, RepoError):
                conflicts.append(
                    {
                        "file": fpath,
                        "error": "could not read conflicted file",
                        "regions": [],
                    }
                )
                continue
            regions = _parse_conflict_markers(text)
            conflicts.append(
                {
                    "file": fpath,
                    "regions": regions,
                }
            )
        _abort_merge(repo_dir)
        return {
            "status": "conflicts",
            "pr_number": number,
            "head": head,
            "base": base,
            "conflicts": conflicts,
        }


def apply_merge_resolutions(
    number: int,
    resolutions: list[dict],
    citizen: str,
    *,
    _pr: dict | None = None,
) -> dict:
    """Re-clone, re-merge, apply resolutions, commit and push.

    *resolutions* is a list of ``{"file": str, "content": str}`` entries —
    one per conflicted file, carrying the fully-resolved file content.
    All resolutions must exactly cover the set of conflicted files, and
    resolved content must not still contain conflict markers.
    """
    _core._ensure_token()
    if not resolutions:
        raise RepoError("resolutions must be a non-empty list of {file, content}.")
    for i, r in enumerate(resolutions):
        if not isinstance(r, dict):
            raise RepoError(f"resolutions[{i}] must be a dict, got {type(r).__name__}.")
        if not isinstance(r.get("file"), str) or not r["file"]:
            raise RepoError(f"resolutions[{i}] 'file' must be a non-empty string.")
        if not isinstance(r.get("content"), str):
            raise RepoError(f"resolutions[{i}] 'content' must be a string.")
        if _has_conflict_markers(r["content"]):
            raise RepoError(
                f"resolutions[{i}] for {r['file']!r}: content still "
                "contains conflict markers — resolve all conflicts "
                "before submitting."
            )
    pr = _pr or _core._request("GET", f"pulls/{number}")
    if pr.get("state") != "open":
        raise RepoError(f"pull request #{number} is not open.")
    head = pr["head"]["ref"]
    base = pr["base"]["ref"]
    with _workspace() as repo_dir:
        _git(repo_dir, "fetch", "origin", base, head)
        _git(repo_dir, "checkout", "-b", "pr_head", f"origin/{head}")
        result = _git(
            repo_dir,
            "merge",
            "--no-commit",
            "--no-ff",
            f"origin/{base}",
            check=False,
        )
        conflicted = _detect_conflict_files(repo_dir)
        if result.returncode == 0 and not conflicted:
            _abort_merge(repo_dir)
            return {
                "status": "clean",
                "pr_number": number,
                "message": "No conflicts found — nothing to resolve.",
            }
        if result.returncode != 0 and not conflicted:
            stderr = result.stderr
            if _core.GITHUB_TOKEN:
                stderr = stderr.replace(_core.GITHUB_TOKEN, "<redacted>")
            raise RepoError(f"merge failed (not a conflict): {stderr.strip()}")
        # Validate coverage: provided files must exactly equal conflicted
        provided = {r["file"] for r in resolutions}
        if provided != set(conflicted):
            missing = set(conflicted) - provided
            extra = provided - set(conflicted)
            parts = []
            if missing:
                parts.append(f"missing: {sorted(missing)}")
            if extra:
                parts.append(f"extra: {sorted(extra)}")
            raise RepoError(
                "resolutions must cover exactly the conflicted files "
                f"({', '.join(parts)})."
            )
        # Write resolutions with path-traversal guard
        for r in resolutions:
            fpath = _safe_path(repo_dir, r["file"])
            parent = os.path.dirname(fpath)
            os.makedirs(parent, exist_ok=True)
            # Normalize to LF (canonical) before writing — resolutions are
            # provided as fully-resolved file content (often LF) but the repo
            # is now LF; keep byte-faithful with newline="".
            _content = _normalize_eol(r["content"], "\n")
            Path(fpath).write_text(_content, encoding="utf-8", newline="")
            _git(repo_dir, "add", r["file"])
        # Commit the merge under the resolving citizen's identity (the
        # trailer records the same attribution in the message).
        commit_msg = f"Merge main into {head} — resolve conflicts\n\nCitizen: {citizen}"
        _git(
            repo_dir,
            "-c",
            f"user.name={citizen}",
            "-c",
            f"user.email={citizen}@agentland.dev",
            "commit",
            "-m",
            commit_msg,
        )
        # Authenticate for push, then push
        with _push_auth(repo_dir):
            _git(repo_dir, "push", "origin", _push_ref(head))
        sha_result = _git(repo_dir, "rev-parse", "HEAD")
        commit_sha = sha_result.stdout.strip()
        _core._invalidate_pr(number)
        return {
            "status": "resolved",
            "pr_number": number,
            "head": head,
            "base": base,
            "commit_sha": commit_sha,
            "files_resolved": sorted(provided),
            "message": (
                f"Merged main into {head} with {len(provided)} file(s) resolved."
            ),
        }
