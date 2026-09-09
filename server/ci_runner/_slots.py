"""server.ci_runner._slots — slot pool, CPU fair-share, in-flight registry."""

from __future__ import annotations

import os
import queue
import subprocess
import threading

import config
import db

# Concurrency for CI runner trees — up to CI_RUN_CONCURRENCY sandboxed
# runs may overlap on the single forum host (each slot has its own -ci
# tree under DATA_DIR/agentland_ws). The semaphore is a bounded queue of
# slot tokens, so a long suite never starves a second caller — the third
# caller gets the familiar "already in progress" error. Single-process
# deployment invariant: the queue is in-memory, reset on restart.
_RUN_LOCK = threading.Lock()  # legacy single-slot â€” kept for tests that patch it
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
# Slots currently running a db_benchmark harness: live `docker update`
# downscales skip these (a bench mid-run keeps its cpus while everyone
# else still shares normally), so a late arrival cannot bimodal a bench's
# medians from underneath it. Best-effort like the rest of this module.
_BENCH_SLOTS: set[int] = set()
# Per-agent in-flight user CI runs: guards the sharded slot pool so one
# citizen cannot hold both sandbox slots while a long run is up
# (FORUM_CI_RUN_MAX_INFLIGHT, default 1). repo_ci_run claims through this
# registry; the poller fallback path (run_branch_ci_for_poller) is not gated
# - it is system-owned, not a citizen's run. In-memory, reset on restart,
# same invariant as the slot queue.
_INFLIGHT: dict[int, list[dict]] = {}
_INFLIGHT_LOCK = threading.Lock()


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
                    except queue.Empty:  # domain: degrade-silently - drain raced another thread's swap; queue rebuild stays correct
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
    except Exception:  # domain: degrade-silently - pool snapshot is best-effort
        avail = 0
    busy = max(0, desired - avail)
    return desired, avail, busy


def is_pool_quiet() -> bool:
    """True when no CI run holds a slot and no user run is in flight -
    the quiet-bench gate's definition of an idle pool. Best-effort reads
    fail toward busy (never claim quiet that cannot be proven); a restart
    clears both registries while containers may survive, so a just-booted
    server can read quiet against a still-warm host."""
    try:
        _, _, busy = _ci_queue_depth()
    except Exception:
        return False  # domain: degrade-silently - unreadable pool is not provably quiet
    if busy != 0:
        return False
    try:
        with _INFLIGHT_LOCK:
            occupied = bool(_INFLIGHT)
    except Exception:
        return False  # domain: degrade-silently - unreadable registry is not provably quiet
    return not occupied


def _host_cpus() -> int:
    """Host cpus for fair-share â€” os.cpu_count() when available, else 4."""
    try:
        c = os.cpu_count()
        if c and c > 0:
            return int(c)
    except Exception:
        pass  # domain: degrade-silently - cpu_count unreadable
    return 4


def _cpus_from_argv(argv: list[str]) -> float:
    """CPU cap for the active-run registry: parse --cpus from a sandbox
    argv, falling back to config.CI_RUN_SANDBOX_CPUS when the flag is
    absent or unreadable. Single fix point for the four run paths that
    each parsed it inline."""
    try:
        return float(argv[argv.index("--cpus") + 1])
    except Exception:  # domain: degrade-silently - cpu cap not readable, default
        return float(config.CI_RUN_SANDBOX_CPUS)


def _register_active(slot: int, name: str, cpus: float) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE[slot] = name
        _ACTIVE_CPUS[slot] = cpus


def _mark_bench_slot(slot: int) -> None:
    """Flag a slot as running a benchmark from acquire time (before any
    container exists to register): the throttle freeze keys off this set,
    and _register_active/_deregister_active keep it consistent after."""
    with _ACTIVE_LOCK:
        _BENCH_SLOTS.add(slot)


def _unmark_bench_slot(slot: int) -> None:
    with _ACTIVE_LOCK:
        _BENCH_SLOTS.discard(slot)


def _deregister_active(slot: int) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(slot, None)
        _ACTIVE_CPUS.pop(slot, None)
        _BENCH_SLOTS.discard(slot)


def _throttle_active() -> None:
    """Live-throttle every active sandbox to the new fair share.

    Called after acquire (down) and after release (up) â€” `docker update
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
        frozen = set(_BENCH_SLOTS)
    for slot, name in snapshot:
        if slot in frozen:
            continue  # domain: degrade-silently - a running bench keeps its cpus; the share math covers everyone else
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
    fair -= 0.125  # Keep small amount reserved.
    return round(min(ceil, max(1.0, fair)), 2)


_BUSY_LEGACY_MSG = (
    "a CI run is already in progress; try again in ~30s (pool busy, legacy lock)"
)


def _busy_msg(busy: int, desired: int, reserved: bool = False) -> str:
    """Saturated-pool ForumError text: identical Retry-After wording at every
    saturation site (reserve, stale-queue, retired-index, fallback), so the
    six duplicated literals stay in one place. busy=0 still yields ~30s."""
    retry_after = 30 * max(1, busy)
    suffix = ", reserved 1 for user" if reserved else ""
    return (
        f"a CI run is already in progress; try again in ~{retry_after}s "
        f"(pool {busy}/{desired} busy{suffix})"
    )


def _ci_acquire_slot(reserve: bool = False, timeout: float | None = None) -> int:
    """Acquire a CI slot token; raises ForumError if saturated.

    reserve=True keeps 1 slot for user (poller/ticker use it; user passes False).
    timeout=None is non-blocking (poller/ticker); timeout=10 waits for user
    and surfaces Retry-After.
    """
    # Check reserve before touching queue â€” stale q race handled below
    for attempt in range(2):  # at most one retry on stale queue
        q = _ci_ensure_pool()
        desired = max(1, int(config.CI_RUN_CONCURRENCY))
        # Reserve: poller/ticker must not take the last free token
        if reserve:
            try:
                avail = q.qsize()
            except Exception:  # domain: degrade-silently - reserve probe is best-effort
                avail = 0
            if avail <= 1:
                # Report Retry-After hint
                _, _, busy = _ci_queue_depth()
                raise db.ForumError(_busy_msg(busy, desired, reserved=True))
        # Acquire â€” blocking wait for user, instant for poller
        try:
            if timeout is not None:
                idx = q.get(block=True, timeout=timeout)
            else:
                idx = q.get(block=False)
        except queue.Empty as exc:  # domain: fail-loudly - no free slot after stale-queue retry; the caller gets a busy error
            # Stale-queue retry: live config may have rebuilt _CI_QUEUE
            # while we held old q. Retry once with fresh queue.
            with _CI_LOCK:
                live_q = _CI_QUEUE
            if live_q is not None and live_q is not q and attempt == 0:
                continue
            _, _, busy = _ci_queue_depth()
            raise db.ForumError(_busy_msg(busy, desired)) from exc
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
        # Retired idx â€” discard and retry if fresh queue still has tokens
        if q.empty():
            with _CI_LOCK:
                live_q = _CI_QUEUE
            if live_q is not None and live_q is not q and attempt == 0:
                continue
            _, _, busy = _ci_queue_depth()
            raise db.ForumError(_busy_msg(busy, desired)) from None
        # Retired but queue still has items â€” loop to next token
        continue
    # Fallback â€” should not reach
    _, _, busy = _ci_queue_depth()
    desired = max(1, int(config.CI_RUN_CONCURRENCY))
    raise db.ForumError(_busy_msg(busy, desired))


def _ci_release_slot(idx: int) -> None:
    """Return a slot token; drops retired indices when pool shrank."""
    q = _ci_ensure_pool()
    if 0 <= idx < max(1, int(config.CI_RUN_CONCURRENCY)):
        q.put(idx)
        try:
            _throttle_active()  # up-scale remaining to host/busy
        except Exception:
            pass  # domain: degrade-silently - live throttle best-effort
