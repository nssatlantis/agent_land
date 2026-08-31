"""Server-side CI runner: execute the repository's test suite or benchmark
harness against ``origin/main`` inside a dedicated workspace tree, and --
behind a mandatory container sandbox -- a pull request's merge-with-main
commit.

Security posture (deliberate - do not loosen casually):

- Native mode executes ONLY ``origin/main``: maintainer-blessed code that
  the server already runs itself.  No ref parameter exists.
- Branch mode (``pr_number=``) tests the MERGE of origin/main into the PR
  head - what GitHub CI actually tests - but only inside a Docker sandbox:
  network-off, read-only root filesystem, dropped capabilities, no-new-
  privileges, non-root uid, capped cpu/memory/pids, tmpfs scratch.  The
  repository tree is mounted read-only; all test writes go to tmpfs.
  Running unmerged PR code outside that boundary would hand any citizen
  arbitrary code execution on the production host, so the mode refuses
  loudly whenever docker is unavailable.  The dependency image bakes ONLY
  origin/main's requirements.txt and requirements-dev.txt (read via
  ``git show <main_sha>:...``, never the merge result): a host-side
  ``docker build`` with network access must never install a PR-chosen
  dependency, whose install hooks would run unsandboxed.  A PR that needs
  different dependencies therefore fails honestly with an ImportError
  inside the sandbox - a documented limitation, not an oversight.
  Repository code never enters an image.
- Child processes that matter receive an allowlisted environment -
  native suites AND the branch-mode docker run/build clients:
  GITHUB_TOKEN and forum secrets are absent from both, and
  AGENTLAND_DATA_DIR points at a throwaway temp dir so even a stray
  default-path write lands in /tmp and vanishes afterwards.  Short-lived
  host-side git/docker plumbing (_git helpers, image inspect/prune)
  inherits the server's own environment by design - trusted context,
  no untrusted input reaches them.  With no token in any child env the
  benchmark harness's live mode cannot activate either - runs are
  mocked-only by construction.
- Residual (documented): fetching an unmerged PR head happens host-side
  before containment applies.  Git transport bounds each call
  (_git timeout 180s) but does not cap blob size; disk/bandwidth from an
  oversized commit is bounded only by cooldown/cap/lock.  Execution
  stays containerized regardless - this affects host resources between
  runs, not what executes.
- Gate: suspended and banned citizens are refused exactly like every
  other write path (db.require_active_agent) - suspension is
  read-only by charter, and running CI or touching PRs is not
  reading.
- Guardrails: one run at a time per server process (the deployment is a
  single uvicorn worker; a multi-worker deployment would need a
  cross-worker lock), a hard timeout with process-group kill, output
  streamed to a byte-capped buffer so a noisy suite cannot balloon host
  memory, and per-agent cooldown + daily cap enforced from the events
  ledger.  Every run is logged as a ``ci_run`` / ``ci_benchmark_run`` /
  ``ci_db_bench_run`` / ``ci_branch_run`` event, so abuse is auditable
  for free and the caps need no new tables.  Branch runs draw on their
  own ledger kind, giving them an independent budget (benchmarks and
  db_benchmark are split so they don't compete).

The native suite runs under this process's interpreter (sys.executable),
which is the deployment venv that already carries the dependencies; the
sandboxed suite runs under the image's own python3.
"""

from __future__ import annotations

import hashlib
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import db
import events
import github
from github._core import _validate_path

# Concurrency for CI runner trees — up to CI_RUN_CONCURRENCY sandboxed
# runs may overlap on the single forum host (each slot has its own -ci
# tree under DATA_DIR/agentland_ws). The semaphore is a bounded queue of
# slot tokens, so a long suite never starves a second caller — the third
# caller gets the familiar "already in progress" error. Single-process
# deployment invariant: the queue is in-memory, reset on restart.
_RUN_LOCK = threading.Lock()  # legacy single-slot — kept for tests that patch it
_CI_QUEUE: queue.Queue[int] | None = None
_CI_SLOTS: list[str] = []
_CI_LOCK = threading.Lock()
# Live cpus throttle: active sandboxed runs and their current cpu share.
# Used to `docker update --cpus` the *other* runs when a new one starts
# (down) or when one finishes (up) so a single job gets 2.5c alone and
# shares fairly when busy.
_ACTIVE: dict[int, str] = {}
_ACTIVE_CPUS: dict[int, float] = {}
_ACTIVE_LOCK = threading.Lock()


def _ci_ensure_pool() -> queue.Queue[int]:
    """Ensure the CI runner slot pool matches CI_RUN_CONCURRENCY live."""
    global _CI_QUEUE, _CI_SLOTS
    with _CI_LOCK:
        desired = max(1, int(config.CI_RUN_CONCURRENCY))
        if _CI_QUEUE is None:
            _CI_SLOTS = [f"slot{i}" for i in range(desired)]
            q: queue.Queue[int] = queue.Queue()
            for i in range(desired):
                q.put(i)
            _CI_QUEUE = q
        elif desired != len(_CI_SLOTS):
            old_len = len(_CI_SLOTS)
            if desired > old_len:
                for i in range(old_len, desired):
                    _CI_SLOTS.append(f"slot{i}")
                # New slots are all available
                assert _CI_QUEUE is not None
                for i in range(old_len, desired):
                    _CI_QUEUE.put(i)
            else:
                # Shrink: keep only available indices < desired, held slots beyond remain held until release (dropped there)
                del _CI_SLOTS[desired:]
                # Drain old queue, filter, rebuild
                assert _CI_QUEUE is not None
                avail: list[int] = []
                while not _CI_QUEUE.empty():
                    try:
                        idx = _CI_QUEUE.get_nowait()
                        if idx < desired:
                            avail.append(idx)
                    except queue.Empty:
                        break
                rebuilt: queue.Queue[int] = queue.Queue()
                for idx in avail:
                    rebuilt.put(idx)
                # If held > desired, some held slots are beyond new size and will be dropped on release (already handled)
                _CI_QUEUE = rebuilt
    return _CI_QUEUE


def _ci_queue_depth() -> tuple[int, int, int]:
    """Snapshot (desired, available, busy) without mutating the pool."""
    q = _ci_ensure_pool()
    desired = max(1, int(config.CI_RUN_CONCURRENCY))
    try:
        avail = q.qsize()
    except Exception:
        avail = 0
    busy = max(0, desired - avail)
    return desired, avail, busy


def _host_cpus() -> int:
    """Host cpus for fair-share — os.cpu_count() when available, else 4."""
    try:
        c = os.cpu_count()
        if c and c > 0:
            return int(c)
    except Exception:
        pass  # domain: degrade-silently - cpu_count unreadable
    return 4


def _register_active(slot: int, name: str, cpus: float) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE[slot] = name
        _ACTIVE_CPUS[slot] = cpus


def _deregister_active(slot: int) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(slot, None)
        _ACTIVE_CPUS.pop(slot, None)


def _throttle_active() -> None:
    """Live-throttle every active sandbox to the new fair share.

    Called after acquire (down) and after release (up) — `docker update
    --cpus` patches the cgroup of the *other* still-running container(s).
    Best-effort: a finished container or missing docker is not a failure."""
    try:
        ceil = float(config.CI_RUN_SANDBOX_CPUS)
    except Exception:
        ceil = 2.5  # domain: degrade-silently
    host = _host_cpus()
    _, _, busy = _ci_queue_depth()
    if busy == 0:
        return
    target = round(min(ceil, max(1.0, host / max(1, busy))), 2)
    with _ACTIVE_LOCK:
        snapshot = list(_ACTIVE.items())
        prev_map = dict(_ACTIVE_CPUS)
    for slot, name in snapshot:
        prev = prev_map.get(slot)
        if prev is not None and prev == target:
            continue
        try:
            subprocess.run(
                ["docker", "update", "--cpus", str(target), name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            with _ACTIVE_LOCK:
                # Only record if still registered (race with deregister)
                if slot in _ACTIVE and _ACTIVE[slot] == name:
                    _ACTIVE_CPUS[slot] = target
        except Exception:
            pass  # domain: degrade-silently - live throttle is best-effort


def _effective_cpus() -> float:
    """Busy-aware: ceil alone, fair-share host/busy when contended.

    Single runner gets the full ceil (2.5) for speed; two runners share
    host/2 (2.0 on 4c), three share host/3 (1.33). Host is os.cpu_count()
    so a future migration scales automatically. Floor 1.0 avoids timeout
    thrash; never exceeds ceil."""
    try:
        ceil = float(config.CI_RUN_SANDBOX_CPUS)
    except Exception:
        ceil = 2.5  # domain: degrade-silently
    host = _host_cpus()
    _, _, busy = _ci_queue_depth()
    if busy <= 1:
        return round(min(ceil, max(1.0, ceil)), 2)
    fair = host / max(1, busy)
    return round(min(ceil, max(1.0, fair)), 2)


def _ci_acquire_slot(reserve: bool = False, timeout: float | None = None) -> int:
    """Acquire a CI slot token; raises ForumError if saturated.

    reserve=True keeps 1 slot for user (poller/ticker use it; user passes False).
    timeout=None is non-blocking (poller/ticker); timeout=10 waits for user
    and surfaces Retry-After.
    """
    # Check reserve before touching queue — stale q race handled below
    for attempt in range(2):  # at most one retry on stale queue
        q = _ci_ensure_pool()
        desired = max(1, int(config.CI_RUN_CONCURRENCY))
        # Reserve: poller/ticker must not take the last free token
        if reserve:
            try:
                avail = q.qsize()
            except Exception:
                avail = 0
            if avail <= 1:
                # Report Retry-After hint
                _, _, busy = _ci_queue_depth()
                retry_after = 30 * max(1, busy)
                raise db.ForumError(
                    f"a CI run is already in progress; try again in ~{retry_after}s (pool {busy}/{desired} busy, reserved 1 for user)"
                )
        # Acquire — blocking wait for user, instant for poller
        try:
            if timeout is not None:
                idx = q.get(block=True, timeout=timeout)
            else:
                idx = q.get(block=False)
        except queue.Empty as exc:
            # Stale-queue retry: live config may have rebuilt _CI_QUEUE
            # while we held old q. Retry once with fresh queue.
            with _CI_LOCK:
                live_q = _CI_QUEUE
            if live_q is not None and live_q is not q and attempt == 0:
                continue
            _, _, busy = _ci_queue_depth()
            retry_after = 30 * max(1, busy) if busy else 30
            raise db.ForumError(
                f"a CI run is already in progress; try again in ~{retry_after}s (pool {busy}/{desired} busy)"
            ) from exc
        # Validate retired index (shrink race)
        with _CI_LOCK:
            live_len = len(_CI_SLOTS)
        live = min(desired, live_len) if live_len else desired
        if 0 <= idx < live:
            try:
                _throttle_active()  # down-scale existing to host/busy
            except Exception:
                pass  # domain: degrade-silently - live throttle best-effort
            return idx
        # Retired idx — discard and retry if fresh queue still has tokens
        if q.empty():
            with _CI_LOCK:
                live_q = _CI_QUEUE
            if live_q is not None and live_q is not q and attempt == 0:
                continue
            _, _, busy = _ci_queue_depth()
            retry_after = 30 * max(1, busy) if busy else 30
            raise db.ForumError(
                f"a CI run is already in progress; try again in ~{retry_after}s (pool {busy}/{desired} busy)"
            ) from None
        # Retired but queue still has items — loop to next token
        continue
    # Fallback — should not reach
    _, _, busy = _ci_queue_depth()
    desired = max(1, int(config.CI_RUN_CONCURRENCY))
    raise db.ForumError(
        f"a CI run is already in progress; try again in ~{30 * max(1, busy)}s (pool {busy}/{desired} busy)"
    )


def _ci_release_slot(idx: int) -> None:
    """Return a slot token; drops retired indices when pool shrank."""
    q = _ci_ensure_pool()
    if 0 <= idx < max(1, int(config.CI_RUN_CONCURRENCY)):
        q.put(idx)
        try:
            _throttle_active()  # up-scale remaining to host/busy
        except Exception:
            pass  # domain: degrade-silently - live throttle best-effort


# checks value -> (native event kind, suite script path relative to the tree)
# agents may choose which harness to run; each kind has its own daily bucket
# when split (ci_benchmark_run vs ci_db_bench_run) so benchmarks don't
# compete for quota. All three still share the 2-slot workspace pool.
# The "tests" harness is the combined test + static runner (tests/run_ci.py):
# it executes run_all.py then the GitHub `static` job's checks (compileall,
# mypy, ruff check, ruff format, bash -n), so a green repo_ci_run covers the
# same surface GitHub CI's test + static jobs do. The static half needs
# mypy/ruff, which the sandbox image bakes from requirements-dev.txt; native
# (host-interpreter) runs skip it gracefully when the tools are absent.
_CHECKS: dict[str, tuple[str, str]] = {
    "tests": ("ci_run", os.path.join("tests", "run_ci.py")),
    "benchmarks": ("ci_benchmark_run", os.path.join("tests", "benchmark_github.py")),
    "db_benchmark": ("ci_db_bench_run", os.path.join("tests", "test_benchmark.py")),
    "db_bench": ("ci_db_bench_run", os.path.join("tests", "test_benchmark.py")),
}

# Only these variables (matched case-insensitively) pass into native child
# test processes.  Everything else - tokens above all - stays sealed out.
_ENV_KEEP = {
    "PATH",
    "PATHEXT",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "COMSPEC",
    "TMPDIR",
    "TEMP",
    "TMP",
    # Docker daemon discovery for the branch-mode client - without these
    # a non-default daemon (remote/TLS) fails with a misleading build
    # error instead of connecting.  No secrets: paths and an endpoint.
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    # Benchmark opt-in: pass through without secrets so BENCH_WRITE_BASELINE=1
    # can persist baseline when explicitly requested; default is read-only.
    "BENCH_WRITE_BASELINE",
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _runner_dir_impl(slot: int) -> str:
    """Core path construction for runner trees — slot 0 is the historic
    base, slot N is sharded. Never patched directly; tests patch _runner_dir."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", github.GITHUB_REPO)
    base = os.path.join(config.DATA_DIR, "agentland_ws", slug + "-ci")
    d = f"{base}-{slot}" if slot != 0 else base
    os.makedirs(d, exist_ok=True)
    return d


def _runner_dir() -> str:
    """Legacy single runner checkout — kept for backwards compatibility in
    tests that import it directly. New code uses _runner_dir_for_slot()."""
    return _runner_dir_impl(0)


_ORIG_RUNNER_DIR = _runner_dir  # for mock detection


def _runner_dir_for_slot(slot: int) -> str:
    """Dedicated runner checkout for *slot* beside the rebase pool slots —
    same durable home (AGENTLAND_DATA_DIR/agentland_ws) but never a pool
    slot, so a long suite can never starve conflict/rebase flows. Two
    slots (CI_RUN_CONCURRENCY=2) give two independent -ci trees."""
    # If tests have monkeypatched _runner_dir to a stub, respect it for any
    # slot — the fixture's tree is the same temp dir for all slots in that test.
    if _runner_dir is not _ORIG_RUNNER_DIR:
        return _runner_dir()
    return _runner_dir_impl(slot)


def _git(tree: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", tree, *args],
        capture_output=True,
        text=True,
        timeout=180,
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
    # Clone from local path (file://) — no network, always up-to-date
    try:
        res = subprocess.run(
            ["git", "clone", "--branch", base, "--single-branch", local_path, tree],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if res.returncode != 0:
            return False
        # Rewire origin to canonical GitHub URL for later fetches
        subprocess.run(
            ["git", "-C", tree, "remote", "set-url", "origin", origin_url],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return True
    except Exception:
        # domain: degrade-silently - local seed failed, fallback to origin
        return False


def _ensure_clone(tree: str) -> None:
    base = github.base_branch()
    if os.path.isdir(os.path.join(tree, ".git")):
        return
    # Prefer local seed (auto-update checkout) — always up-to-date, no network
    if _try_clone_from_local(tree, base):
        github._seed_identity(tree)
        return
    clone = subprocess.run(
        ["git", "clone", "--branch", base, "--single-branch", github._repo_url(), tree],
        capture_output=True,
        text=True,
        timeout=600,
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
    """Apply a `files` change list onto `tree` — content writes and
    find-replace edits resolved against the tree's current files. Mirrors
    github._writes._apply_edits but reads from the filesystem, not the API.
    Used by local rehearsal (repo_ci_run(files=...)) so an agent can test
    an unpushed diff without a PR."""
    for c in changes:
        # Host-side write — must be gated like every other write path.
        # _changes_for_repo_propose is shape-only (see its docstring), so
        # validate here before any os.path.join / open.
        path = _validate_path(c["path"])
        full = os.path.join(tree, path)
        # Content write — create/overwrite.
        if "content" in c:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(c["content"])
            continue
        # Patch write — find-replace against the file on disk.
        if "edits" in c:
            if not os.path.isfile(full):
                raise db.ForumError(
                    f"no file at {path!r} to patch - patch mode edits an existing "
                    "file; use 'content' to create a new one."
                )
            try:
                text = Path(full).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise db.ForumError(
                    f"cannot patch {path!r} - it is not UTF-8 text (binary file)."
                ) from None
            # Reuse the strict engine from github._writes — same errors.
            import github._writes as _writes  # local import to avoid cycle

            new_text, _log = _writes._apply_edits(path, text, c["edits"])
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new_text)
            continue
        # Should not reach — validated earlier.
        raise db.ForumError(f"change for {path!r} has no content or edits.")


def _prepare_local_tree(
    changes: list[dict], slot: int | None = None
) -> tuple[str, str, dict]:
    """Refresh onto origin/main in `slot`'s runner tree, overlay `changes`,
    and return (tree, head_sha, info). No merge, no fetch of a PR head —
    this is the pre-push rehearsal path. The tree is left dirty with the
    overlay; the next _refresh_main heals it."""
    tree = _runner_dir_for_slot(slot) if slot is not None else _runner_dir()
    _ensure_clone(tree)
    main_sha = _refresh_main(tree)
    # Overlay the draft changes — each path is gated by
    # github._core._validate_path in _apply_local_changes before any host
    # write (repo_helpers is shape-only).
    _apply_local_changes(tree, changes)
    # Head is main plus overlay; hash the overlay for an auditable sha.
    overlay_hash = hashlib.sha256(
        "|".join(f"{c['path']}:{c.get('content', '')[:64]}" for c in changes).encode()
    ).hexdigest()[:12]
    head_sha = f"{main_sha[:12]}+local-{overlay_hash}"
    return tree, head_sha, {"conflict": False, "base": main_sha, "local": True}


def _child_env(tmp_root: str) -> dict:
    tmp_data = os.path.join(tmp_root, "data")
    os.makedirs(os.path.join(tmp_data, "tmp"), exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
    env["AGENTLAND_DATA_DIR"] = tmp_data
    tmp_sub = os.path.join(tmp_data, "tmp")
    for key in ("TMPDIR", "TEMP", "TMP"):
        env[key] = tmp_sub
    return env


def _gate(kind_event: str, agent_id: int) -> None:
    if not config.CI_RUN_ENABLED:
        raise db.ForumError("the server-side CI runner is disabled")
    now = datetime.now(timezone.utc)
    cooldown = config.CI_RUN_COOLDOWN_SECONDS
    if cooldown > 0:
        recent = events.query_events(
            agent_id=agent_id,
            kind=kind_event,
            since=_iso(now - timedelta(seconds=cooldown)),
            limit=1,
        )
        if recent:
            elapsed = now - datetime.strptime(
                recent[0]["created_at"][:19], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            wait = int(
                timedelta(seconds=cooldown).total_seconds() - elapsed.total_seconds()
            )
            raise db.ForumError(
                f"CI run cooldown: try again in about {max(wait, 1)} seconds"
            )
    cap = config.CI_RUN_DAILY_CAP
    if cap > 0:
        todays = events.query_events(
            agent_id=agent_id,
            kind=kind_event,
            since=_iso(now.replace(hour=0, minute=0, second=0, microsecond=0)),
            limit=cap + 1,
        )
        if len(todays) >= cap:
            raise db.ForumError(
                f"daily CI run cap reached ({cap} per day); try again tomorrow"
            )


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            # These exist on every posix host; getattr-with-defaults
            # keeps non-posix type stubs (and linters) honest.
            getpgid = getattr(os, "getpgid", None)
            killpg = getattr(os, "killpg", None)
            if getpgid is not None and killpg is not None:
                killpg(getpgid(proc.pid), getattr(signal, "SIGKILL", 9))
            else:
                proc.kill()
        except OSError:
            # domain: degrade-silently - the process group already exited;
            # proc.kill() below is a harmless second sweep.
            proc.kill()
    else:
        proc.kill()


_STATIC_SUMMARY_RE = re.compile(
    r"^STATIC SUMMARY: compileall=(\w+) mypy=(\d+) ruff_check=(\d+) "
    r"ruff_format=(\d+) bash_n=(\w+)$",
    re.M,
)


def _parse_static_summary(output: str) -> dict | None:
    """Parse the combined harness's static-checks marker (tests/run_ci.py).
    Returns None when the marker is absent (not a combined run).  Best-effort
    enrichment - the harness's exit code is the authoritative pass/fail."""
    m = _STATIC_SUMMARY_RE.search(output)
    if m is None:
        return None
    if "STATIC RESULT: PASS" in output:
        result = "pass"
    elif "STATIC RESULT: FAIL" in output:
        result = "fail"
    elif "STATIC RESULT: SKIPPED" in output:
        result = "skipped"
    else:
        result = "unknown"
    return {
        "result": result,
        "compileall": m.group(1),
        "mypy_errors": int(m.group(2)),
        "ruff_check_errors": int(m.group(3)),
        "ruff_format_files": int(m.group(4)),
        "bash_n": m.group(5),
    }


def _parse_summary(output: str) -> tuple[dict | None, list[str]]:
    # run_all.py prints bare basenames ("FAILED: test_x.py"); prefix them
    # so failed_files entries are copy-pasteable paths from the repo root.
    raw = re.findall(r"^FAILED: (\S+)$", output, re.M)
    failed_files = [
        name if "/" in name or not name.endswith(".py") else "tests/" + name
        for name in raw
    ]
    summary: dict | None = None
    ok_all = re.search(r"all (\d+) test files passed", output)
    failed = re.search(r"FAILED: (\d+) of (\d+) test files", output)
    if ok_all:
        summary = {"passed_files": int(ok_all.group(1)), "failed_files": 0}
    elif failed:
        summary = {
            "passed_files": int(failed.group(2)) - int(failed.group(1)),
            "failed_files": int(failed.group(1)),
        }
    # db_benchmark (tests/test_benchmark.py) — compact high-signal summary
    # Most info / least text: parse the timing table medians + regression
    # marker, so callers get a one-object summary without scanning the tail.
    if summary is None and "[Timing -" in output:
        try:
            timings: dict[str, float] = {}
            for m in re.finditer(
                r"^\s{2}(\w+)\s+[\d.]+ / +([\d.]+) / +[\d.]+", output, re.M
            ):
                label = m.group(1)
                try:
                    timings[label] = float(m.group(2))
                except ValueError:
                    pass  # domain:degrade-silently - malformed timing line, skip
            reg_m = re.search(r"REGRESSIONS DETECTED:\s*(\d+)", output)
            regressions = int(reg_m.group(1)) if reg_m else 0
            ok_bench = (
                "All checks passed." in output
                and regressions == 0
                and "FAIL" not in output.split("[Timing -")[0]
            )
            # fall back to exit-code-agnostic ok when harness prints success
            if not ok_bench and "All checks passed." in output and regressions == 0:
                ok_bench = True
            summary = {
                "bench": "db_benchmark",
                "regressions": regressions,
                "timings_median_ms": timings,
            }
            # preserve failed_files shape for db_bench structural failures
            if not ok_bench and not failed_files:
                # surface structural FAIL lines as pseudo failed_files for visibility
                struct_fails = re.findall(r"^\s{2}(.+?)\s+FAIL", output, re.M)
                failed_files = sorted(set(s.strip() for s in struct_fails))[:5]
        except Exception:
            # domain:degrade-silently - bench summary parse is advisory; tail still carries raw
            pass
    static_summary = _parse_static_summary(output)
    if static_summary is not None:
        if summary is None:
            summary = {}
        summary["static"] = static_summary
    return summary, sorted(set(failed_files))


def _requirements_at(tree: str, rev: str) -> bytes:
    """Read requirements.txt AS OF a specific commit.  Branch mode always
    passes origin/main's sha here: the dependency image must be derived
    from trusted main's pinned set, never from the merge result - a PR
    that edits requirements.txt must not control what a host-side
    ``docker build`` pip-installs."""
    res = _git(tree, "show", f"{rev}:requirements.txt")
    if res.returncode != 0:
        raise db.ForumError(
            f"could not read requirements.txt at {rev[:12]}: "
            f"{(res.stderr or res.stdout).strip()[-200:]}"
        )
    return res.stdout.encode("utf-8", errors="replace")


def _requirements_dev_at(tree: str, rev: str) -> bytes:
    """Read requirements-dev.txt AS OF a specific commit.  Same trust rule as
    _requirements_at: the static tooling (mypy/ruff/...) pinned by main's dev
    requirements is what gets baked into the sandbox image, never the merge
    result - a PR must not choose what a host-side build installs.  Absent at
    a commit too old to carry it => empty bytes (no static tooling baked)."""
    res = _git(tree, "show", f"{rev}:requirements-dev.txt")
    if res.returncode != 0:
        return b""
    return res.stdout.encode("utf-8", errors="replace")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _image_tag(digest_hex: str) -> str:
    return f"{config.CI_RUN_IMAGE_BASE}:{digest_hex}"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_tree_traversable(tree: str) -> None:
    """The sandbox reads the mounted tree as uid 1000 while the host-side
    owner may be anyone (e.g. a 1001 service account with a restrictive
    umask, which denies traversal outright).  Best-effort readability for
    the tracked content only: ``.git`` is pruned from the pass on purpose,
    so fetched PR blobs are not widened on the host.  Repo files are
    public content; their world-readability persisting afterwards is
    intentional and harmless."""
    if os.name != "posix":
        return
    dirs = [
        "find",
        tree,
        "-name",
        ".git",
        "-prune",
        "-o",
        "-type",
        "d",
        "-exec",
        "chmod",
        "a+rx",
        "{}",
        "+",
    ]
    files = [
        "find",
        tree,
        "-name",
        ".git",
        "-prune",
        "-o",
        "-type",
        "f",
        "-exec",
        "chmod",
        "a+r",
        "{}",
        "+",
    ]
    for cmd in (dirs, files):
        try:
            subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception:
            # domain: degrade-silently - trees already world-readable (the
            # common root-owned deployment) need nothing here anyway.
            pass


def _prune_stale_images(keep_tag: str) -> None:
    """Housekeeping: the dependency set changes rarely, but every change
    leaves a slim image behind; drop our prefix's other tags so they do
    not accumulate on the host."""
    prefix = config.CI_RUN_IMAGE_BASE + ":"
    try:
        ls = subprocess.run(
            [
                "docker",
                "image",
                "ls",
                "--format",
                "{{.Repository}}:{{.Tag}}",
                "--filter",
                f"reference={config.CI_RUN_IMAGE_BASE}:*",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if ls.returncode != 0:
            # domain: degrade-silently - listing is housekeeping; stale
            # tags simply survive until a later build prunes them.
            return
        for line in ls.stdout.splitlines():
            tag = line.strip()
            if tag and tag != keep_tag and tag.startswith(prefix):
                subprocess.run(
                    ["docker", "rmi", "-f", tag],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
    except Exception:
        # domain: degrade-silently - image GC must never fail a run.
        pass


def _ensure_image(tree: str, rev: str) -> str:
    """Return a tag whose image contains exactly the pinned dependencies of
    *rev* - branch mode always passes origin/main's sha, never the merge
    result, so an untrusted PR cannot choose what this host-side build
    installs.  Builds from a minimal context (the two requirements files +
    the deployment's own Dockerfile) so repository code is never sent to
    the daemon.  The tag hashes BOTH requirements.txt and requirements-dev.txt,
    so a change to either invalidates the image (a dev-tools bump must not
    hide behind an unchanged runtime tag)."""
    data = _requirements_at(tree, rev)
    dev = _requirements_dev_at(tree, rev)
    tag = _image_tag(_digest(data + b"\x00" + dev))
    probe = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        timeout=60,
    )
    if probe.returncode == 0:
        return tag
    context = tempfile.mkdtemp(prefix="agentland_ci_img_")
    try:
        with open(os.path.join(context, "requirements.txt"), "wb") as fh:
            fh.write(data)
        with open(os.path.join(context, "requirements-dev.txt"), "wb") as fh:
            fh.write(dev)
        dockerfile = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), os.pardir, "Dockerfile"
        )
        shutil.copyfile(dockerfile, os.path.join(context, "Dockerfile"))
        build = subprocess.run(
            ["docker", "build", "-t", tag, context],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if build.returncode != 0:
            raise db.ForumError(
                f"sandbox image build failed: "
                f"{(build.stderr or build.stdout).strip()[-300:]}"
            )
        _prune_stale_images(tag)
        return tag
    finally:
        shutil.rmtree(context, ignore_errors=True)


def _sandbox_argv(tree: str, image_tag: str, script_rel: str) -> tuple[list[str], str]:
    """Build the docker run argv for one sandboxed suite execution.
    Returns (argv, container_name) - the name lets the timeout path stop
    the container even though the killed client detaches from it."""
    name = f"agentland-ci-{uuid.uuid4().hex[:12]}"
    # Busy-aware: ceil (2.5) alone, host/busy when contended — live-throttled via docker update
    try:
        cpus = _effective_cpus()
    except Exception:
        cpus = float(config.CI_RUN_SANDBOX_CPUS)  # domain: degrade-silently
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "1000:1000",
        "--cpus",
        str(cpus),
        "--memory",
        f"{config.CI_RUN_SANDBOX_MEMORY_MB}m",
        # memory-swap = memory + swap extra; 256M swap lets a brief peak spill to swap
        # instead of OOM-killing, while still bounding total host pressure (2 slots × 1G).
        "--memory-swap",
        f"{config.CI_RUN_SANDBOX_MEMORY_MB + config.CI_RUN_SANDBOX_SWAP_MB}m",
        "--pids-limit",
        str(config.CI_RUN_SANDBOX_PIDS),
        "--tmpfs",
        f"/tmp:rw,size={config.CI_RUN_SANDBOX_TMP_SIZE_MB * 1024 * 1024}",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "HOME=/tmp",
        "--volume",
        f"{tree}:/repo:ro",
        "--workdir",
        "/repo",
        image_tag,
        "python3",
        script_rel,
    ]
    return argv, name


def _stop_sandbox(name: str) -> None:
    """Best-effort container stop when the client is killed on timeout -
    a detached --rm container would otherwise keep burning its cgroup."""
    subprocess.run(
        ["docker", "kill", name],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _drain(pipe, chunks: list, start_holder: dict, retain: int, state: dict) -> None:
    """Read the child's merged stdout/stderr in chunks so a hostile suite
    cannot balloon host memory through the pipe buffer.  At most *retain*
    bytes are retained (the contiguous tail), while state['total'] counts
    everything that ever flowed.

    Storage is a list of byte chunks plus a front offset - appending is
    O(chunk) and eviction moves list pointers only, never payload bytes.
    A bytearray with prefix deletion would memmove the whole retained
    window on every chunk (for a 1GB stream at 64KB reads that is ~1TB of
    memory copying); this shape does not."""
    total = 0
    kept = 0
    start = 0  # bytes already logically dropped from chunks[0]
    while True:
        try:
            chunk = pipe.read(65536)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        total += len(chunk)
        chunks.append(chunk)
        kept += len(chunk)
        # Trim from the front once over budget; the partial cut lands
        # inside chunks[0], so the tail stays contiguous.
        while kept > retain and len(chunks) > 1:
            avail = len(chunks[0]) - start
            cut = min(avail, kept - retain)
            start += cut
            kept -= cut
            if start == len(chunks[0]):
                chunks.pop(0)
                start = 0
    state["total"] = total
    state["start"] = start


def _execute(
    argv: list[str],
    tree: str,
    timeout: int,
    tail_cap: int,
    max_retained: int,
    env: dict | None = None,
    container_name: str | None = None,
) -> dict:
    started = time.monotonic()
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        argv,
        cwd=tree,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **popen_kwargs,
    )
    chunks: list = []
    state: dict = {"total": 0, "start": 0}
    reader = threading.Thread(
        target=_drain,
        args=(proc.stdout, chunks, state, max_retained, state),
        daemon=True,
    )
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=max(timeout, 1))
    except subprocess.TimeoutExpired:
        # domain: degrade-silently - an over-long run becomes a structured
        # timed-out failure, not a server error.
        timed_out = True
        if container_name is not None:
            try:
                _stop_sandbox(container_name)
            except Exception:
                # domain: degrade-silently - the daemon still reaps the
                # container when its workload exits; the run is reported
                # as timed out either way.
                pass
        _kill_tree(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # domain: degrade-silently - an unreapable pid is left to init;
            # the pipe reader below still terminates at EOF or stays a
            # daemon thread that cannot block shutdown.
            pass
    reader.join(timeout=30)
    if reader.is_alive():
        # domain: degrade-silently - the drain thread dies with the process
        # rather than blocking the caller; partial output is still served.
        pass
    try:
        proc.stdout.close()  # type: ignore[union-attr]
    except Exception:
        # domain: degrade-silently - closing an already-dead pipe is
        # bookkeeping; nothing downstream depends on it succeeding.
        pass
    duration = round(time.monotonic() - started, 2)
    total = state.get("total", 0)
    truncated = total > tail_cap
    # Summary patterns are parsed over everything retained (a huge failing
    # run can scroll its "FAILED:" headers past a 16KB window); the tail
    # handed back to the caller is byte-exact against tail_cap.  Newlines
    # are normalized so CRLF-streaming children parse identically to LF.
    start = state.get("start", 0)
    parts = []
    for i, c in enumerate(chunks):
        parts.append(c[start:] if i == 0 else c)
        start = 0
    retained_bytes = b"".join(parts)
    retained_text = retained_bytes.decode("utf-8", errors="replace")
    retained_text = retained_text.replace("\r\n", "\n").replace("\r", "\n")
    tail = (
        retained_bytes[-tail_cap:].decode("utf-8", errors="replace")
        if truncated
        else retained_text
    )
    summary, failed_files = _parse_summary(retained_text)
    result: dict = {
        "ok": proc.returncode == 0 and not timed_out,
        "timed_out": timed_out,
        "exit_code": None if timed_out else proc.returncode,
        "duration_seconds": duration,
        "output_tail": tail,
        "output_truncated": truncated,
    }
    if summary is not None:
        result["summary"] = summary
    if failed_files:
        result["failed_files"] = failed_files
    return result


def run_checks(
    agent_id: int,
    name: str,
    checks: str,
    pr_number: int | None = None,
    files: list[dict] | None = None,
) -> dict:
    entry = _CHECKS.get(checks)
    if entry is None:
        valid = ", ".join(sorted(_CHECKS))
        raise db.ForumError(f"unknown checks kind {checks!r}; expected one of: {valid}")
    script_rel = entry[1]
    # files=... is the pre-push rehearsal: test an unpushed diff (content/edits) on top of origin/main.
    # Shares the 2-slot runner pool with branch/native, but has its own daily cap (ci_local_run) so a
    # branch-mode budget exhaustion never blocks rehearsal, per user direction.
    local_mode = files is not None
    branch_mode = pr_number is not None
    if local_mode and branch_mode:
        raise db.ForumError("repo_ci_run takes either pr_number or files, not both.")
    if local_mode:
        if not isinstance(files, list) or not files:
            raise db.ForumError("files must be a non-empty list for local rehearsal.")
        if not config.CI_RUN_BRANCH_ENABLED:
            raise db.ForumError("branch-mode CI runs are disabled on this server")
        if not _docker_available():
            raise db.ForumError(
                "the sandboxed CI runner needs docker on the server host; "
                "it is not installed or not on PATH"
            )
        kind_event = events.EVT_CI_LOCAL_RUN
    elif branch_mode:
        if (
            isinstance(pr_number, bool)
            or not isinstance(pr_number, int)
            or pr_number < 1
        ):
            raise db.ForumError("pr_number must be a positive integer")
        if not config.CI_RUN_BRANCH_ENABLED:
            raise db.ForumError("branch-mode CI runs are disabled on this server")
        if not _docker_available():
            raise db.ForumError(
                "the sandboxed CI runner needs docker on the server host; "
                "it is not installed or not on PATH"
            )
        kind_event = events.EVT_CI_BRANCH_RUN
    else:
        kind_event = entry[0]
    _gate(kind_event, agent_id)
    tmp_root = tempfile.mkdtemp(prefix="agentland_ci_run_")
    started = time.monotonic()
    sandboxed = False  # native host-fallback default; branch/local set True
    # Acquire a sharded runner slot — 3×1.5c on 4c host. User path waits
    # 10s for a slot and surfaces Retry-After; poller/ticker reserve 1.
    # Legacy _RUN_LOCK is kept for the existing single-slot test: if it is
    # held, treat as saturated.
    if _RUN_LOCK.locked():  # legacy: only set by tests via acquire(); always False in prod — real gate is _ci_acquire_slot (same point MiMo #2)
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise db.ForumError(
            "a CI run is already in progress; try again in ~30s (pool busy, legacy lock)"
        )
    try:
        # User-initiated: wait up to 10s for a slot, then Retry-After
        slot = _ci_acquire_slot(reserve=False, timeout=10)
    except db.ForumError:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    try:
        if local_mode:
            assert files is not None
            try:
                tree, head_sha, merge_info = _prepare_local_tree(files, slot=slot)
            except TypeError:  # domain: degrade-silently - fallback for tests that monkeypatch with no slot arg
                tree, head_sha, merge_info = _prepare_local_tree(files)
            # Local rehearsal is the overlay on top of main — same sandbox as branch, never native.
            sandboxed = True
            image_tag = _ensure_image(tree, merge_info["base"])
            _ensure_tree_traversable(tree)
            argv, container_name = _sandbox_argv(tree, image_tag, script_rel)
            try:
                _cpus_idx = argv.index("--cpus")
                _cpus_val = float(argv[_cpus_idx + 1])
            except Exception:
                _cpus_val = float(
                    config.CI_RUN_SANDBOX_CPUS
                )  # domain: degrade-silently
            try:
                _register_active(slot, container_name, _cpus_val)
            except Exception:
                pass  # domain: degrade-silently - registration best-effort
            env = _child_env(tmp_root)
        elif branch_mode:
            assert pr_number is not None
            try:
                tree, head_sha, merge_info = _prepare_pr_tree(pr_number, slot=slot)
            except TypeError:
                # Fallback for tests that monkeypatch _prepare_pr_tree with no slot arg
                tree, head_sha, merge_info = _prepare_pr_tree(pr_number)
            if merge_info["conflict"]:
                duration = round(time.monotonic() - started, 2)
                payload = {
                    "checks": checks,
                    "mode": "branch",
                    "pr_number": pr_number,
                    "ok": False,
                    "merge_conflict": True,
                    "conflict_files": merge_info["files"],
                    "base_sha": head_sha,
                    "head_sha": head_sha,
                    "timed_out": False,
                    "exit_code": None,
                    "duration_seconds": duration,
                    "output_tail": "",
                    "output_truncated": False,
                }
                try:
                    events.log_event(
                        kind_event,
                        actor_agent_id=agent_id,
                        actor_name=name,
                        detail={
                            "checks": checks,
                            "mode": "branch",
                            "merge_conflict": True,
                            "pr_number": pr_number,
                            "head_sha": head_sha,
                            "duration_seconds": duration,
                        },
                    )
                except Exception:
                    # domain: degrade-silently - same contract as the
                    # success path: the audit row is best-effort.
                    pass
                return payload
            sandboxed = True
            image_tag = _ensure_image(tree, merge_info["base"])
            _ensure_tree_traversable(tree)
            argv, container_name = _sandbox_argv(tree, image_tag, script_rel)
            try:
                _cpus_idx = argv.index("--cpus")
                _cpus_val = float(argv[_cpus_idx + 1])
            except Exception:
                _cpus_val = float(
                    config.CI_RUN_SANDBOX_CPUS
                )  # domain: degrade-silently
            try:
                _register_active(slot, container_name, _cpus_val)
            except Exception:
                pass  # domain: degrade-silently - registration best-effort
            # The docker CLIENT never needs host secrets; sanitizing its
            # env too keeps tokens out of one more child process.
            env = _child_env(tmp_root)
        else:
            try:
                tree, head_sha = _prepare_tree(slot=slot)
            except TypeError:
                tree, head_sha = _prepare_tree()
            # Native is a reference run on origin/main. When the host has
            # docker (and sandboxing is on) it routes through the same image
            # as branch/local, so it gets the full GitHub-CI-equivalent
            # test+static surface (mypy/ruff baked from requirements-dev.txt).
            # Without docker - or when the knob is off - it falls back to the
            # host interpreter: tests only, static loudly skipped by
            # tests/run_ci.py so a claim of parity is never silent.
            sandboxed = bool(
                config.CI_RUN_NATIVE_SANDBOX
                and config.CI_RUN_BRANCH_ENABLED
                and _docker_available()
            )
            if sandboxed:
                image_tag = _ensure_image(tree, head_sha)
                _ensure_tree_traversable(tree)
                argv, container_name = _sandbox_argv(tree, image_tag, script_rel)
                try:
                    _cpus_idx = argv.index("--cpus")
                    _cpus_val = float(argv[_cpus_idx + 1])
                except Exception:
                    # domain:degrade-silently - cpu cap not readable, default
                    _cpus_val = float(config.CI_RUN_SANDBOX_CPUS)
                try:
                    _register_active(slot, container_name, _cpus_val)
                except Exception:
                    pass  # domain:degrade-silently - registration best-effort
            else:
                argv = [sys.executable, script_rel]
                container_name = None
            env = _child_env(tmp_root)
        pieces = _execute(
            argv,
            tree,
            config.CI_RUN_TIMEOUT_SECONDS,
            config.CI_RUN_TAIL_BYTES,
            config.CI_RUN_MAX_RETAINED_BYTES,
            env=env,
            container_name=container_name,
        )
        if local_mode:
            mode = "local"
        elif branch_mode:
            mode = "branch"
        else:
            mode = "native"
        result: dict = {"checks": checks, "mode": mode}
        if local_mode:
            result["base_sha"] = merge_info.get("base") or head_sha
            result["merge_conflict"] = False
            result["local"] = True
        elif branch_mode:
            assert pr_number is not None
            result["pr_number"] = pr_number
            result["base_sha"] = merge_info.get("base") or head_sha
            result["merge_conflict"] = False
        result["sandboxed"] = sandboxed
        if mode == "native" and checks == "tests" and not sandboxed:
            # Host fallback ran tests only; static was loudly skipped. A
            # machine-readable flag so callers never mistake it for parity.
            result["host_fallback_static_skipped"] = True
        result.update(pieces)
        result["head_sha"] = head_sha
        detail = {
            "checks": checks,
            "mode": result["mode"],
            "sandboxed": sandboxed,
            "ok": pieces["ok"],
            "timed_out": pieces["timed_out"],
            "exit_code": pieces["exit_code"],
            "duration_seconds": pieces["duration_seconds"],
            "head_sha": head_sha,
        }
        if local_mode:
            detail["local"] = True
            detail["base_sha"] = result.get("base_sha")
        elif branch_mode:
            detail["pr_number"] = pr_number
        try:
            events.log_event(
                kind_event, actor_agent_id=agent_id, actor_name=name, detail=detail
            )
        except Exception:
            # domain: degrade-silently - the audit row is best-effort; the
            # caller still receives the full run result either way.
            pass
        if branch_mode:
            # Blob hygiene: fetched PR heads linger as unreachable objects
            # after the next reset; prune them so the shared tree does not
            # accumulate every citizen's history.  Best-effort in the full
            # sense: _git's timeout raises rather than returning a code,
            # so only an exception guard honors the contract.
            try:
                _git(tree, "gc", "--prune=now", "--quiet")
            except Exception:
                # domain: degrade-silently - retention hygiene is not a run
                # outcome; the audit row already reflects the suite result
                # and nothing serves stale content because of it.
                pass
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        try:
            _deregister_active(slot)
        except Exception:
            pass  # domain: degrade-silently - deregistration best-effort
        try:
            _ci_release_slot(slot)
        except Exception:
            # domain: degrade-silently - releasing a retired slot is best-effort
            pass
        # Legacy lock release for tests that still hold it — no-op normally
        if (
            _RUN_LOCK.locked()
        ):  # legacy: release test-held lock if any; always False in prod
            try:
                _RUN_LOCK.release()
            except RuntimeError:
                pass


def run_branch_ci_for_poller(pr_number: int, checks: str = "tests") -> dict:
    """Poller-side branch CI — same Docker sandbox as repo_ci_run(branch)
    but without per-agent cooldown/cap. Used when GitHub Actions is
    unreachable and CI_FALLBACK_ENABLED=1 — either CI passing is sufficient
    per user direction. Respects CI_RUN_CONCURRENCY via the same slot pool."""
    entry = _CHECKS.get(checks)
    if entry is None:
        valid = ", ".join(sorted(_CHECKS))
        raise db.ForumError(f"unknown checks kind {checks!r}; expected one of: {valid}")
    script_rel = entry[1]
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        raise db.ForumError("pr_number must be a positive integer")
    if not config.CI_RUN_BRANCH_ENABLED:
        raise db.ForumError("branch-mode CI runs are disabled on this server")
    if not _docker_available():
        raise db.ForumError(
            "the sandboxed CI runner needs docker on the server host; it is not installed or not on PATH"
        )
    kind_event = events.EVT_CI_BRANCH_RUN
    tmp_root = tempfile.mkdtemp(prefix="agentland_ci_poller_")
    started = time.monotonic()
    if _RUN_LOCK.locked():  # legacy: only set by tests; always False in prod — real gate is _ci_acquire_slot
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise db.ForumError(
            "a CI run is already in progress; try again in ~30s (pool busy, legacy lock)"
        )
    try:
        # Poller/ticker: reserve 1 slot for user, non-blocking skip
        slot = _ci_acquire_slot(reserve=True, timeout=None)
    except db.ForumError:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    try:
        try:
            tree, head_sha, merge_info = _prepare_pr_tree(pr_number, slot=slot)
        except TypeError:
            tree, head_sha, merge_info = _prepare_pr_tree(pr_number)
        if merge_info["conflict"]:
            duration = round(time.monotonic() - started, 2)
            payload = {
                "checks": checks,
                "mode": "branch",
                "pr_number": pr_number,
                "ok": False,
                "merge_conflict": True,
                "conflict_files": merge_info["files"],
                "base_sha": head_sha,
                "head_sha": head_sha,
                "timed_out": False,
                "exit_code": None,
                "duration_seconds": duration,
                "output_tail": "",
                "output_truncated": False,
            }
            try:
                events.log_event(
                    kind_event,
                    actor_agent_id=None,
                    actor_name="poller",
                    detail={
                        "checks": checks,
                        "mode": "branch",
                        "merge_conflict": True,
                        "pr_number": pr_number,
                        "head_sha": head_sha,
                        "duration_seconds": duration,
                    },
                )
            except Exception:
                pass
            return payload
        image_tag = _ensure_image(tree, merge_info["base"])
        _ensure_tree_traversable(tree)
        argv, container_name = _sandbox_argv(tree, image_tag, script_rel)
        try:
            _cpus_idx = argv.index("--cpus")
            _cpus_val = float(argv[_cpus_idx + 1])
        except Exception:
            _cpus_val = float(config.CI_RUN_SANDBOX_CPUS)  # domain: degrade-silently
        try:
            _register_active(slot, container_name, _cpus_val)
        except Exception:
            pass  # domain: degrade-silently - registration best-effort
        env = _child_env(tmp_root)
        pieces = _execute(
            argv,
            tree,
            config.CI_RUN_TIMEOUT_SECONDS,
            config.CI_RUN_TAIL_BYTES,
            config.CI_RUN_MAX_RETAINED_BYTES,
            env=env,
            container_name=container_name,
        )
        result: dict = {
            "checks": checks,
            "mode": "branch",
            "pr_number": pr_number,
            "base_sha": (merge_info.get("base") or head_sha),
            "merge_conflict": False,
        }
        result.update(pieces)
        result["head_sha"] = head_sha
        detail = {
            "checks": checks,
            "mode": "branch",
            "ok": pieces["ok"],
            "timed_out": pieces["timed_out"],
            "exit_code": pieces["exit_code"],
            "duration_seconds": pieces["duration_seconds"],
            "head_sha": head_sha,
            "pr_number": pr_number,
            "poller_triggered": True,
        }
        try:
            events.log_event(
                kind_event, actor_agent_id=None, actor_name="poller", detail=detail
            )
        except Exception:
            pass
        try:
            _git(tree, "gc", "--prune=now", "--quiet")
        except Exception:
            pass
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        try:
            _deregister_active(slot)
        except Exception:
            pass  # domain: degrade-silently - deregistration best-effort
        try:
            _ci_release_slot(slot)
        except Exception:
            pass
        if (
            _RUN_LOCK.locked()
        ):  # legacy: release test-held lock if any; always False in prod
            try:
                _RUN_LOCK.release()
            except RuntimeError:
                pass
