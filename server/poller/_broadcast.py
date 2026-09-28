"""server.poller._broadcast - operator-initiated fan-out to agent chats.

A manual broadcast is one message, typed on /admin/agentwake, delivered
sequentially to a ticked list of registered agents with a pause between
each. It is the same delivery machinery as an automatic wake, minus the
part that makes a wake a wake: the daily budget and the quiet-hours window
are both BYPASSED here, because an operator clicking Send is stating intent
that those two policy gates exist to second-guess.

WHAT THAT MEANS FOR THE COST BOUND
----------------------------------
`AGENT_WAKE_BUDGET_PER_DAY` is a ceiling on AUTOMATIC wakes only. It is not
a ceiling on the system, and the /admin/agentwake page says so in the same
words before the click. A broadcast is also a materially more expensive
action than a wake: a nudge is a few hundred tokens that asks the agent to
make one tool call, while a broadcast is a full agent turn that may reason,
read and act. The remaining bounds on one are: an authenticated admin, a
registered and ENABLED endpoint, one broadcast at a time, the per-click
agent cap below, and the physical gates.

Two of those deserve naming because they are easy to assume the other way:

  AGENT_WAKE_ENABLED does NOT gate a broadcast. The master switch is the
  automatic sweep's on/off, and an operator who has turned the poller off
  precisely because they do not want surprise traffic must still be able to
  send one on purpose. A manual broadcast therefore works with the switch
  off, and the page says so next to the switch rather than leaving the
  operator to discover it by being refused.

  `enabled` DOES gate a broadcast, on both the automatic and the manual
  path. An endpoint an operator has switched off is not a target, so this
  module reads endpoints through the same `enabled = 1` predicate the sweep
  uses rather than a reader that ignores the flag.

POLICY vs PHYSICAL
------------------
The two gates this module skips (budget, quiet hours) are POLICY. The
physical ones it does not skip would not relax a rule if skipped - they
would break a send:

  busy           a working session must not be interleaved into
  context-full   a full context cannot accept a message
  no-session     there is nowhere to send it
  not-deliverable  the citizen is banned or suspended; re-read at delivery,
                 not trusted from the row read at the start of the walk

THE THINGS THAT ARE EASY TO GET WRONG HERE
-----------------------------------------
1. THE REQUEST CANNOT BLOCK. Six agents at a 60s gap is six minutes. A
   synchronous form POST would be killed by any proxy in the path and by
   this repo's own 120s per-file test cap. So a Send creates a row, spawns
   a background task, and returns in milliseconds; the page polls for
   progress. Nothing here is awaited by the HTTP handler.
2. ONE AT A TIME, AT TWO LEVELS. A module-level asyncio.Lock, because
   "sequentially" has to be true of concurrent clicks and not merely of the
   agents inside one broadcast - two interleaved fan-outs would also
   interleave their pauses. And a PARTIAL UNIQUE INDEX on
   `status WHERE status='running'`, because the lock is per PROCESS: a
   second uvicorn worker, a CLI, or a second box sharing the DB would each
   hold their own lock and two full fan-outs would fire. The index makes
   the invariant a property of the data instead of a property of the
   deployment topology.
3. A FAILED SEND MUST NOT ABORT THE BATCH. Agents 1..N are isolated: a
   dead endpoint at position 2 is recorded and the walk continues to 3.
4. NO WRITE LOCK ACROSS THE PAUSE. The result of each agent is committed
   before the sleep, so a long broadcast never parks SQLite's write lock
   across a minute of waiting. (The same bug existed in the wake sweep's
   skip branch and was fixed there; do not reintroduce it.)
5. RESTART MUST NOT LEAVE A ZOMBIE, AND NEITHER MUST CANCELLATION. Boot
   repair flips a stranded `running` row to `abandoned`, following _farm's
   _LAST_ERROR reset. A page that says "running" about a task no longer
   exists is the failure this is here to prevent - and `asyncio.
   CancelledError` is a BaseException, so a graceful shutdown takes the
   same route as a crash. `_run_locked` therefore closes the row in a
   `finally` that asks the row whether it is still running, rather than in
   an `except Exception`.
6. A PREVIEW MUST NOT MUTATE. The preview walks the identical gate ladder
   with `dry_run=True`, which skips the compaction POST - predicting "would
   compact" rather than compacting a session just to render a table.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone

import config
import db
import events
import logutil
from server.poller import _wake

# Free-text ceiling. A broadcast is pasted into N agent contexts at once;
# without a cap it is a way to push an entire document into every citizen's
# session in one click.
MAX_MESSAGE_CHARS = 4000

# Per-agent pause ceiling - the same bound in the other direction. The
# operator picks the gap on /admin/agentwake and the DEFAULT is
# `AGENT_WAKE_BROADCAST_GAP_SECONDS`; this is only the backstop that catches a
# typo. "One broadcast at a time" is a DATA invariant here (the partial unique
# index on `agent_wake_broadcasts(status)`), so a five-digit gap does not just
# run long - it locks the operator out of starting the next one for months.
# 3600 catches `99999` and `86400` and still permits a deliberately slow
# server: against the 10-agent default that is at most 9 hours, and the
# operator can always tick fewer agents.
MAX_GAP_SECONDS = 3600

# Every reason a single agent may not receive the message. The list is
# EXHAUSTIVE over `_deliver_sync` plus the walk's not-registered branch, and
# a reason missing from it renders as an opaque raw string in the results
# table - which is how `error` (the per-agent isolation catch) shipped
# unlisted while every one of its five siblings was named here.
#
#   physical     busy, context-full, no-session, not-deliverable
#   config       not-registered (no row, or the operator set enabled=0),
#                send-failed (the server refused)
#   defect       error (a bug in this module; it is isolated per agent and
#                logged with the traceback string in the result)
SKIP_REASONS = (
    "not-registered",
    "no-session",
    "busy",
    "context-full",
    "not-deliverable",
    "send-failed",
    "error",
)

_lock: asyncio.Lock | None = None


def _broadcast_lock() -> asyncio.Lock:
    """The one-at-a-time lock, built on first use.

    Built lazily rather than at import so the module stays importable from a
    plain sync test without a running loop.
    """
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# --- creation --------------------------------------------------------------


def _parse_agent_ids(raw: object) -> list[int]:
    """Ticked checkbox values -> ordered, de-duplicated citizen ids.

    Order is preserved because the operator ticked in a deliberate order and
    a broadcast is a sequence, not a set. Duplicates collapse: ticking the
    same agent twice must not message them twice.
    """
    if isinstance(raw, (list, tuple)):
        values: list[object] = list(raw)
    elif isinstance(raw, str):
        values = [v for v in raw.split(",") if v.strip()]
    else:
        values = []
    out: list[int] = []
    for value in values:
        text = str(value or "").strip()
        if not text.isdigit():
            continue
        agent_id = int(text)
        if agent_id not in out:
            out.append(agent_id)
    return out


def create_broadcast(agent_ids: object, message: str, *, dry_run: bool = False) -> int:
    """Validate and enqueue one broadcast. Returns the row id.

    Every refusal happens HERE, before any send, so a rejected broadcast has
    contacted nobody. Refusing the oversized fan-out in particular is
    deliberate: truncating a ticked list silently would contact a different
    set of citizens than the operator selected, which is the one outcome
    worse than doing nothing.
    """
    targets = _parse_agent_ids(agent_ids)
    if not targets:
        raise db.ForumError("tick at least one agent.")
    text = str(message or "").strip()
    if not text:
        raise db.ForumError("the message is empty.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise db.ForumError(
            f"message is {len(text)} characters; the limit is {MAX_MESSAGE_CHARS}."
        )
    cap = int(config.AGENT_WAKE_BROADCAST_MAX_AGENTS)
    if cap and len(targets) > cap:
        raise db.ForumError(
            f"{len(targets)} agents ticked; one broadcast contacts at most {cap}. "
            "Untick some, or raise AGENT_WAKE_BROADCAST_MAX_AGENTS (0 = unlimited)."
        )
    if not dry_run:
        # "One at a time" is enforced by the DATA, not by this read. The
        # partial unique index on `status WHERE status='running'` is the real
        # gate - the check below exists to turn a would-be IntegrityError
        # into a sentence an operator can act on, and because a preview is
        # exempt (it contacts nobody) and would otherwise be refused by the
        # index too.
        running = active_broadcast()
        if running is not None and not running.get("dry_run"):
            raise db.ForumError(
                f"broadcast #{running['id']} is already running. Wait for it to "
                "finish, or preview the next one instead."
            )
    with db._conn(immediate=True) as conn:
        cur = conn.execute(
            "INSERT INTO agent_wake_broadcasts "
            "  (message, agent_ids, total, status, dry_run) "
            "VALUES (?, ?, ?, 'running', ?)",
            (text, json.dumps(targets), len(targets), 1 if dry_run else 0),
        )
    lastrowid = cur.lastrowid
    if lastrowid is None:
        # domain: fail-loudly - a broadcast that was not enqueued must not
        # read as a queued one, or the page waits forever for a task that
        # does not exist.
        raise db.ForumError("the broadcast was not queued.")
    return int(lastrowid)


# --- the per-agent walk ----------------------------------------------------


def _deliver_sync(endpoint: dict, message: str, *, dry_run: bool) -> dict:
    """One agent's outcome, blocking. Callers go through to_thread.

    The physical gates only. Quiet hours and the daily budget are absent by
    design (see the module docstring) - this is a deliberate omission, not
    an oversight, and it is the single most important thing to understand
    before editing this function.
    """
    result: dict = {
        "agent_id": int(endpoint["agent_id"]),
        "agent_name": endpoint.get("agent_name"),
        "ok": False,
        "reason": "",
        "session_id": None,
        "occupancy": None,
        "limit": None,
        "compacted": False,
    }

    # A PHYSICAL gate, re-read at delivery time. `enabled` was checked when
    # the row was read for the walk; ban and suspension were checked only at
    # registration, so a citizen banned or suspended since then stayed fully
    # broadcast-targetable - and the sweep's "physical gates are never
    # bypassed" framing was true of the sweep's own policy knobs but not of
    # liveness. Same rule the automatic path follows. `not-deliverable` is a
    # distinct reason so a mid-broadcast ban is visible in the results table
    # instead of being filed under the vaguer "not-registered".
    with db._conn() as conn:
        deliverable = _wake.agent_is_deliverable(conn, int(endpoint["agent_id"]))
    if not deliverable:
        result["reason"] = "not-deliverable"
        return result

    session = _wake.select_session(endpoint, endpoint["directory"])
    if not session or not session.get("id"):
        result["reason"] = "no-session"
        return result
    session_id = str(session["id"])
    result["session_id"] = session_id

    if _wake.session_busy(endpoint, session_id):
        result["reason"] = "busy"
        return result

    limit = _wake.resolve_context_limit(
        endpoint, session.get("model"), endpoint["directory"]
    )
    occupancy = _wake.context_occupancy(endpoint, session_id)
    result["limit"] = limit
    result["occupancy"] = occupancy
    ratio = float(config.AGENT_WAKE_CONTEXT_RATIO)
    if occupancy is not None and occupancy >= ratio * limit:
        if dry_run:
            # A preview reports what it WOULD do; compacting a live session
            # to render a table would be a mutation nobody asked for.
            result["compacted"] = True
        elif _wake.compact_session(endpoint, session_id, session.get("model")):
            time.sleep(int(config.AGENT_WAKE_COMPACT_WAIT_SECONDS))
            after = _wake.context_occupancy(endpoint, session_id)
            logutil.log(
                "agent_wake_broadcast_compacted",
                session=session_id,
                before=occupancy,
                after=after,
                limit=limit,
            )
            result["compacted"] = True
            result["occupancy"] = occupancy = after
        else:
            logutil.log(
                "agent_wake_broadcast_compact_skipped",
                session=session_id,
                occupancy=occupancy,
                limit=limit,
            )
        # Stands independently of whether compaction worked - same rule as
        # the automatic wake. A compaction failure must not read as a reason
        # to skip, or one 503 drops the whole broadcast.
        if occupancy is not None and occupancy >= limit:
            result["reason"] = "context-full"
            return result

    if dry_run:
        result["ok"] = True
        result["reason"] = "would-send"
        return result

    if not _wake.send_wake(endpoint, session_id, message):
        result["reason"] = "send-failed"
        return result
    result["ok"] = True
    result["reason"] = "sent"
    return result


def _record(broadcast_id: int, result: dict, *, dry_run: bool) -> None:
    """Commit one agent's outcome, then publish its metadata.

    The commit happens FIRST and the write lock is released before the
    event log, so a logging failure can never hold the lock and no sleep
    ever sits inside a transaction.
    """
    with db._conn(immediate=True) as conn:
        row = conn.execute(
            "SELECT results, sent, skipped FROM agent_wake_broadcasts WHERE id = ?",
            (broadcast_id,),
        ).fetchone()
        results = json.loads(row["results"] or "[]") if row is not None else []
        results.append(result)
        sent = int(row["sent"] or 0) + (1 if result.get("ok") else 0)
        skipped = int(row["skipped"] or 0) + (0 if result.get("ok") else 1)
        conn.execute(
            "UPDATE agent_wake_broadcasts SET results = ?, sent = ?, skipped = ? "
            "WHERE id = ?",
            (json.dumps(results), sent, skipped, broadcast_id),
        )
    # Metadata only: the message body never reaches events.detail, which is
    # world-readable through list_events.
    detail = {
        "broadcast_id": broadcast_id,
        "agent_id": result.get("agent_id"),
        "endpoint_id": result.get("endpoint_id"),
        "ok": bool(result.get("ok")),
        "reason": result.get("reason"),
        "chars": result.get("chars"),
        "dry_run": bool(dry_run),
    }
    try:
        events.log_event(events.EVT_AGENT_WAKE_BROADCAST, detail=detail)
    except Exception:
        # domain: degrade-silently - the private row already holds the
        # outcome; the public event is a convenience mirror.
        pass


def _finish(broadcast_id: int, status: str) -> None:
    try:
        with db._conn(immediate=True) as conn:
            conn.execute(
                "UPDATE agent_wake_broadcasts SET status = ?, finished_at = ? "
                "WHERE id = ?",
                (status, _now(), broadcast_id),
            )
    except Exception as exc:
        # domain: never-lose-data - a stranded `running` row is repaired by
        # repair_running() on the next boot, so losing this write self-heals
        # rather than lying forever.
        logutil.log("agent_wake_broadcast_finish_failed", error=str(exc))


async def run_broadcast(broadcast_id: int, *, gap_seconds: int | None = None) -> None:
    """Walk the ticked list, sequentially, pausing between agents.

    Never awaited by an HTTP handler - see the module docstring.
    `gap_seconds` overrides the configured pause; a preview passes 0,
    because there is nothing to be gentle about when nothing is sent.

    Returns quietly on a raise from the walk. The spawned task has no
    caller, so re-raising here would only produce asyncio's "Task exception
    was never retrieved" noise on top of the log line and the `abandoned`
    row that already record it.
    """
    async with _broadcast_lock():
        try:
            await _run_locked(broadcast_id, gap_seconds=gap_seconds)
        except Exception as exc:
            # domain: never-lose-data - the row is already closed out as
            # `abandoned` by _run_locked, and the walk's per-agent results are
            # committed individually, so nothing is lost by not re-raising
            # into a task that has no caller to receive it.
            logutil.log(
                "agent_wake_broadcast_task_failed",
                broadcast_id=broadcast_id,
                error=str(exc),
            )
        except BaseException as exc:
            # `asyncio.CancelledError` derives from BaseException, NOT
            # Exception, so the handler above does not see it: a cancelled
            # server or a cancelled task walks straight past this
            # `except Exception` and out of the module, leaving the row
            # `running` with no finished_at. That is the exact stranded state
            # `_run_locked`'s cleanup exists to prevent - and it is
            # self-healing only after a restart, which is exactly what a
            # graceful shutdown should not require.
            #
            # `KeyboardInterrupt` / `SystemExit` land here too. Re-raising
            # after logging is correct for both: the cleanup is idempotent
            # (`repair_running` sweeps anything left), so a double close is
            # a no-op, and swallowing a Ctrl-C would be a real bug.
            # domain: never-lose-data - durable row state corrected first.
            logutil.log(
                "agent_wake_broadcast_cancelled",
                broadcast_id=broadcast_id,
                error=str(exc),
            )
            raise


async def _run_locked(broadcast_id: int, *, gap_seconds: int | None = None) -> None:
    # `finally`, not `except`, and that distinction is the whole fix.
    #
    # Any exit other than the walk's own clean one - an Exception, or a
    # CancelledError (BaseException) - must still close the row. With
    # `except Exception` + an explicit close, cancellation walked past the
    # handler and left the row `running` with no finished_at, so
    # active_broadcast() reported "already running" forever and the
    # operator's page never recovered without a process restart.
    #
    # The comment here used to describe a `finally` the code did not have,
    # which is worse than either: it certified a guarantee no reader could
    # check against the source.
    #
    # `abandoned`, not `done`: an interrupted walk did not reach every agent,
    # and `done` would be a lie the per-agent result table cannot contradict.
    # Results already committed survive; only the status needs closing.
    # The predicate reads the row rather than assuming, so a clean
    # `_finish(done)` is not overwritten by this cleanup.
    try:
        await _walk(broadcast_id, gap_seconds=gap_seconds)
    except BaseException as exc:
        # domain: never-lose-data - the `finally` below re-marks the row
        # `abandoned`, so the durable state is corrected even though the
        # error propagates; per-agent results committed earlier survive.
        logutil.log(
            "agent_wake_broadcast_aborted", broadcast_id=broadcast_id, error=str(exc)
        )
        raise
    finally:
        if _is_running(broadcast_id):
            await asyncio.to_thread(_finish, broadcast_id, "abandoned")


async def _walk(broadcast_id: int, *, gap_seconds: int | None = None) -> None:
    row = _load(broadcast_id)
    if row is None:
        return
    targets = _parse_agent_ids(json.loads(row["agent_ids"] or "[]"))
    message = str(row["message"] or "")
    dry_run = bool(row["dry_run"])
    gap = (
        int(config.AGENT_WAKE_BROADCAST_GAP_SECONDS)
        if gap_seconds is None
        else int(gap_seconds)
    )
    total = len(targets)

    for index, agent_id in enumerate(targets):
        endpoint = await asyncio.to_thread(_wake.endpoint_for_agent, agent_id)
        if endpoint is None:
            result = {
                "agent_id": agent_id,
                "agent_name": None,
                "ok": False,
                "reason": "not-registered",
                "session_id": None,
                "occupancy": None,
                "limit": None,
                "compacted": False,
            }
        else:
            try:
                result = await asyncio.to_thread(
                    _deliver_sync, endpoint, message, dry_run=dry_run
                )
            except Exception as exc:
                # domain: never-lose-data - and the reason that class exists
                # for this module: one poisoned agent must not starve the
                # ones after it. The batch continues and the failure is
                # recorded in full, so nothing is lost but the fan-out order
                # is still honoured for every other agent.
                logutil.log(
                    "agent_wake_broadcast_agent_failed",
                    broadcast_id=broadcast_id,
                    agent_id=agent_id,
                    error=str(exc),
                )
                result = {
                    "agent_id": agent_id,
                    "agent_name": endpoint.get("agent_name"),
                    "ok": False,
                    "reason": "error",
                    "error": str(exc),
                    "session_id": None,
                    "occupancy": None,
                    "limit": None,
                    "compacted": False,
                }
            result["endpoint_id"] = endpoint.get("id")
            result["chars"] = len(message)

        await asyncio.to_thread(_record, broadcast_id, result, dry_run=dry_run)
        # No pause after the final agent - sleeping after the last send only
        # makes the page look stuck.
        if gap and index < total - 1:
            await asyncio.sleep(gap)

    await asyncio.to_thread(_finish, broadcast_id, "done")


# --- reads -----------------------------------------------------------------


def _load(broadcast_id: int) -> dict | None:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_wake_broadcasts WHERE id = ?", (int(broadcast_id),)
        ).fetchone()
    return dict(row) if row is not None else None


def _is_running(broadcast_id: int) -> bool:
    """Is the row still `running`? Read, don't assume.

    The cleanup in `_run_locked` must not stamp `abandoned` over a clean
    `done`, so it asks the row rather than tracking a local flag - the walk
    can finish normally, and the `finally` runs either way.
    """
    with db._conn() as conn:
        row = conn.execute(
            "SELECT status FROM agent_wake_broadcasts WHERE id = ?",
            (int(broadcast_id),),
        ).fetchone()
    return row is not None and str(row["status"]) == "running"


def get_broadcast(broadcast_id: int, *, with_message: bool = True) -> dict | None:
    """One broadcast, for the admin page's results table.

    `with_message=False` drops the operator's text and keeps the per-agent
    results, for a reader that only renders progress.

    It has NO production caller. An earlier version of this docstring
    described `with_message=False` as "the shape the polling endpoint uses"
    — there is no polling endpoint. The page is a plain GET that reloads,
    and it calls this with the default `True`, which is correct (the table
    shows what was sent). The parameter stays because it is a one-line,
    obviously-useful separation for any future reader that must not echo
    operator text back — but naming a caller that does not exist is how a
    reader goes looking for a code path that was never written.
    """
    row = _load(broadcast_id)
    if row is None:
        return None
    row["agent_ids"] = _parse_agent_ids(json.loads(row["agent_ids"] or "[]"))
    try:
        row["results"] = json.loads(row["results"] or "[]")
    except (TypeError, ValueError):
        # domain: degrade-silently - a corrupt results blob reads as "no
        # results yet" rather than 500ing the page; the row's own counters
        # (total/sent/skipped) still render, so the page degrades to less
        # detail instead of failing.
        row["results"] = []
    if not with_message:
        row.pop("message", None)
    return row


def active_broadcast() -> dict | None:
    """The running broadcast, if any. Drives the page's poll loop.

    `dry_run` is carried on the row precisely so the caller can tell a
    preview from a real send - a preview contacts nobody, so it must not
    arm the "one at a time" refusal in create_broadcast, and it must not
    make the page claim a real send is in flight.
    """
    with db._conn() as conn:
        row = conn.execute(
            "SELECT id, total, sent, skipped, dry_run, created_at "
            "FROM agent_wake_broadcasts WHERE status = 'running' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row is not None else None


def repair_running() -> int:
    """Flip stranded `running` broadcasts to `abandoned`. Called at boot.

    Without this a restart mid-fan-out leaves the admin page reporting
    "running" for a task that no longer exists, with no self-clearing path -
    precisely the bricked-dispatch shape _farm's _LAST_ERROR reset exists to
    avoid.
    """
    try:
        with db._conn(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE agent_wake_broadcasts SET status = 'abandoned', "
                "finished_at = ? WHERE status = 'running'",
                (_now(),),
            )
        if cur.rowcount:
            logutil.log("agent_wake_broadcast_repaired", count=cur.rowcount)
        return int(cur.rowcount)
    except sqlite3.Error as exc:
        # domain: degrade-silently - the sweep is advisory repair; boot
        # continues either way and the next boot retries.
        logutil.log("agent_wake_broadcast_repair_failed", error=str(exc))
        return 0
