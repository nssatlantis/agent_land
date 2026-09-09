"""server.tools.repo._ticker — debounce ticker, pending pool, and snapshot readers (split from server/tools/repo.py)."""

from __future__ import annotations

import asyncio
import threading
import time

import config
import db

# Debounced coalescing for file-at-a-time pushes: 15s quiet window,
# GitHub runs every intermediate, host runs only the final head.
PENDING = dict[int, float]
_PENDING: PENDING = {}
_IN_FLIGHT: set[int] = set()
_REQUEUE_ATTEMPTS: dict[int, int] = {}
# threading.Lock (not asyncio.Lock) — deliberately held for microseconds
# while iterating _PENDING; required because poller snapshot
# (pending_prs_snapshot, called via asyncio.to_thread) and ticker
# (_debounce_ticker, async) access the same dict from different threads.
# asyncio.Lock would not be safe across to_thread.
_PENDING_LOCK = threading.Lock()
_TICKER_TASK: asyncio.Task | None = None


async def _debounce_ticker() -> None:
    while True:
        await asyncio.sleep(5)
        # Live conc per tick — host 4c tuning 3×1.5c vs 2×2.0c
        try:
            _ticker_conc = max(1, int(config.CI_RUN_CONCURRENCY))
        except Exception:
            _ticker_conc = 3  # domain: degrade-silently - config read failure must not stall ticker
        # Adaptive 5s poll base, 10s when backlog large (user asked 5/10s)
        # Keep 5s sleep fixed; effective throttle via semaphore + backoff below
        sem = asyncio.Semaphore(_ticker_conc)
        now = time.monotonic()
        to_run: list[int] = []
        with _PENDING_LOCK:
            for pr_number, deadline in list(_PENDING.items()):
                if now >= deadline:
                    to_run.append(pr_number)
                    del _PENDING[pr_number]
                    _IN_FLIGHT.add(pr_number)
        if not to_run:
            continue
        # Respect 3-slot host pool — at most 3 concurrent, true overlap

        async def _run_one(pr_number: int, sem=sem) -> None:  # noqa: B023
            async with sem:
                try:
                    import server.ci_runner as ci_runner  # noqa: WPS433

                    await asyncio.to_thread(
                        ci_runner.run_branch_ci_for_poller, pr_number, "tests"
                    )
                except Exception as exc:  # domain: degrade-silently - ticker must never crash; one head failure must not stall others
                    # If the host slot pool was saturated (queue empty —
                    # _RUN_LOCK is legacy, never acquired in prod, always
                    # unlocked), the PR was already removed from _PENDING but
                    # got no CI. Re-enqueue so the next ticker cycle
                    # retries — otherwise file-at-a-time bursts lose the
                    # "host runs final head" promise under load. Bounded to 5
                    # attempts with 15s*attempt backoff (user approved).
                    if isinstance(exc, db.ForumError) and str(exc).startswith(
                        "a CI run is already"
                    ):
                        try:
                            with _PENDING_LOCK:
                                attempts = _REQUEUE_ATTEMPTS.get(pr_number, 0) + 1
                                if attempts <= 5:
                                    _REQUEUE_ATTEMPTS[pr_number] = attempts
                                    # Backoff: 15s * attempt (15,30,45,60,75)
                                    _PENDING[pr_number] = (
                                        time.monotonic() + 15 * attempts
                                    )
                                else:
                                    # Drop after 5 — surface as missed coalesce, next push will re-enqueue fresh
                                    _REQUEUE_ATTEMPTS.pop(pr_number, None)
                                    import logutil as _logutil

                                    _logutil.log(
                                        "ticker_requeue_exhausted",
                                        pr_number=pr_number,
                                        attempts=attempts,
                                    )
                        except Exception:
                            pass  # domain: degrade-silently - re-enqueue must not crash ticker
                    pass
                finally:
                    with _PENDING_LOCK:
                        _IN_FLIGHT.discard(pr_number)
                        # On success, clear requeue counter
                        # Keep counter only for saturated retries; success resets
                        if pr_number in _PENDING:
                            pass
                        else:
                            _REQUEUE_ATTEMPTS.pop(pr_number, None)

        await asyncio.gather(*[_run_one(pr) for pr in to_run])


def _cancel_ticker() -> None:
    global _TICKER_TASK
    if _TICKER_TASK is not None and not _TICKER_TASK.done():
        _TICKER_TASK.cancel()
        _TICKER_TASK = None


def _ensure_ticker() -> None:
    global _TICKER_TASK
    with _PENDING_LOCK:
        if _TICKER_TASK is not None and not _TICKER_TASK.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # domain: degrade-silently - no running loop during import/tests; enqueue still coalesced
            return
        _TICKER_TASK = loop.create_task(_debounce_ticker())


def debounced_enqueue(pr_number: int) -> None:
    with _PENDING_LOCK:
        _PENDING[pr_number] = time.monotonic() + 15
        # Fresh enqueue resets requeue counter (new head)
        _REQUEUE_ATTEMPTS.pop(pr_number, None)
    _ensure_ticker()


def pending_prs_snapshot() -> set[int]:
    """Thread-safe snapshot of PRs currently debounced (ticker coalescing).

    The ticker mutates _PENDING under _PENDING_LOCK; reading keys without
    the lock can raise RuntimeError: dictionary changed size during
    iteration. Snapshot under the lock so the poller dedup never defeats
    itself silently. Includes IN_FLIGHT so poller does not launch duplicate
    host CI while ticker already holds the slot (delete-before-run gap)."""
    with _PENDING_LOCK:
        return set(_PENDING.keys()) | set(_IN_FLIGHT)


def pending_snapshot_with_deadlines() -> dict[int, float]:
    """For /admin/ci: deadlines remaining per pending PR (seconds)."""
    with _PENDING_LOCK:
        now = time.monotonic()
        return {pr: round(dl - now, 1) for pr, dl in _PENDING.items()}


def in_flight_snapshot() -> set[int]:
    """For /admin/ci: currently executing ticker PRs."""
    with _PENDING_LOCK:
        return set(_IN_FLIGHT)


def requeue_attempts_snapshot() -> dict[int, int]:
    """For /admin/ci: requeue attempts per PR."""
    with _PENDING_LOCK:
        return dict(_REQUEUE_ATTEMPTS)
