"""server.ci_runner._farm — CI farm registry + overflow dispatch (proposal #667, PR 2).

Offloads agent-invoked CI runs (native + local modes) to a spare LAN runner
when the local pool is saturated. The runner (ci_farm/runner.py) clones at the
pinned origin/main and executes with the same sandbox parity, so the bytes run
are identical to a host run.

Registry: a small ci_runners table (name, url, token, status, last_heartbeat).
Health: live ping in pick_runner (no background poller - small-fix surface).
Dispatch: try_dispatch() is called from run_checks when slot acquisition is
busy; it returns the full host-shaped result dict (ledger written with runner
provenance), raises _FarmRetryLocal when a picked runner fails (the caller
retries once locally), or returns None when dispatch is not eligible / no
runner is available - the caller then raises the busy error. try_bench_dispatch
keeps the older None-on-error contract (silent local fallback); the two lanes
are documented, not unified. Both lanes ledger a dropped dispatch
(ci_farm_dispatch_failed) before falling back, so the fallback stays silent
to the caller while the failure stops disappearing entirely.

PR 2 scope: overflow for native + local modes only. Bench remote-first is PR 3;
branch (pr_number) and named-tree runs are host-local and never dispatched.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from typing import NoReturn

import config
import db
import events
from db import ForumError

_ACTIVE_RUNS: dict[int, int] = {}
_ACTIVE_LOCK = threading.Lock()

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class _FarmRetryLocal(Exception):
    """try_dispatch raises this when a picked runner fails (transport failure,
    unreadable reply, or runner-reported error with no result shape).

    Out-of-band by design: a result dict must always be a real CI result, so
    any present-or-future caller can treat a returned dict as success-shaped
    (the deferred P3-4 poller path must catch this too). Carries the
    runner-side reason for the audit row.
    """


def _now() -> str:
    return db._now_iso()


def _row_to_dict(row) -> dict:
    return dict(row)


# --- registry CRUD ---------------------------------------------------------


def register_runner(name: str, url: str, token: str = "") -> dict:
    """Insert a runner row (status unknown until its first ping). Returns the row."""
    now = _now()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
    try:
        with db._conn(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO ci_runners"
                " (name, url, token, token_hash, status, last_heartbeat,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'unknown', NULL, ?, ?)",
                (name, url, token, token_hash, now, now),
            )
            row = conn.execute(
                "SELECT * FROM ci_runners WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
    except sqlite3.IntegrityError:
        raise ForumError(
            f"cannot register runner {name!r}: duplicate or invalid."
        ) from None
    return _row_to_dict(row)


def remove_runner(runner_id: int) -> bool:
    """Delete a runner row. Returns True if a row was deleted."""
    with db._conn(immediate=True) as conn:
        cur = conn.execute("DELETE FROM ci_runners WHERE id = ?", (runner_id,))
        deleted = cur.rowcount > 0
    if deleted:
        with _ACTIVE_LOCK:
            _ACTIVE_RUNS.pop(runner_id, None)
    return deleted


def list_runners() -> list:
    """All runners, newest first."""
    with db._conn() as conn:
        rows = conn.execute("SELECT * FROM ci_runners ORDER BY id DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def _mark(runner_id: int, status: str, heartbeat: bool = False) -> None:
    """Best-effort status/heartbeat update (degrade-silently)."""
    now = _now()
    try:
        with db._conn(immediate=True) as conn:
            if heartbeat:
                conn.execute(
                    "UPDATE ci_runners"
                    " SET status = ?, last_heartbeat = ?, updated_at = ?"
                    " WHERE id = ?",
                    (status, now, now, runner_id),
                )
            else:
                conn.execute(
                    "UPDATE ci_runners SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, runner_id),
                )
    except Exception:
        # domain: degrade-silently - registry bookkeeping is advisory
        pass


# --- health + selection ----------------------------------------------------


def _ping(url: str, token: str) -> dict | None:
    """GET /health with a short timeout. Returns {ok, busy} or None on failure."""
    req = urllib.request.Request(url.rstrip("/") + "/health")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=config.CI_FARM_HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not isinstance(body, dict) or not body.get("ok"):
            return None
        return body
    except Exception:
        return None


def _runner_head_sha(ping: dict) -> str | None:
    """The runner's OWN checkout sha from /health, validated, or None.

    Deliberately NOT folded into the ledger's `head_sha`: that field is
    the tested TREE's sha (origin/main, fetched fresh per run), while
    this is the pinned ORCHESTRATION code the runner imported at startup -
    the half that can actually go stale, and the half the parity claim in
    this module's header rests on (#B190).

    A value that is not a 40-hex sha is dropped rather than laundered,
    matching how output_sha256 is handled in _map_and_log below: a runner
    that will not name its tree must not get a plausible-looking one.
    """
    sha = ping.get("head_sha")
    if isinstance(sha, str) and _COMMIT_RE.fullmatch(sha) is not None:
        return sha
    return None


def _release(runner_id: int) -> None:
    """Release one reserved active-run slot, never retaining a negative count."""
    with _ACTIVE_LOCK:
        left = _ACTIVE_RUNS.get(runner_id, 0) - 1
        if left <= 0:
            _ACTIVE_RUNS.pop(runner_id, None)
        else:
            _ACTIVE_RUNS[runner_id] = left


def _audit_dispatch_failed(
    runner: dict,
    checks: str,
    agent_id: int,
    name: str,
    reason: str,
    lane: str,
) -> None:
    """Ledger one dropped farm dispatch.

    Both lanes degrade to the host after a picked runner returns nothing
    usable - overflow raises _FarmRetryLocal, bench returns None - and the
    run then completes locally, so a runner that fails EVERY dispatch (no
    buildx plugin, a dead image build) leaves no trace: its /health answers
    200 and no ci_* event ever names a runner. This row is the durable,
    public answer to "why is my farm never used?". Best-effort by contract.
    A record only: nothing keys runner eligibility off it, because a
    skip-on-failure rule with no self-clearing path would brick dispatch
    until an operator intervened (see _LAST_ERROR in ci_farm/runner.py).
    """
    try:
        events.log_event(
            events.EVT_CI_FARM_DISPATCH_FAILED,
            actor_agent_id=agent_id,
            actor_name=name,
            detail={
                "runner": str(runner.get("name") or ""),
                "runner_id": runner.get("id"),
                "url": str(runner.get("url") or ""),
                "checks": checks,
                "lane": lane,
                "error": reason[:200],
                "runner_head_sha": runner.get("_runner_head_sha"),
            },
        )
    except Exception:
        # domain: degrade-silently - the audit row is best-effort; the
        # caller's fallback must happen either way.
        pass


def _retry_local(
    runner: dict,
    reason: str,
    checks: str,
    agent_id: int,
    name: str,
    *,
    skip_reason: str | None = None,
) -> NoReturn:
    """Audit the row, then raise the caller's retry-locally signal.

    Every picked-but-unusable runner reply funnels through here, so the
    overflow lane gets one durable ledger row per dropped dispatch instead of
    a silent host fallback. Annotated NoReturn (not None) so mypy - and the
    next reader - know control never continues past the call.

    skip_reason labels the row a capacity SKIP rather than a dispatch failure.
    It matters that this still RAISES rather than returning None: on this
    lane a None return means "no runner available", and _runs.py turns that
    into a BUSY error for the agent. A declined run did no work, so it has to
    keep taking the local-retry path it always took - with the corrected
    label, not a new refusal. Bench is different: that lane's documented
    contract is a silent local fallback, so it returns None there.
    """
    if skip_reason is not None:
        log_skip(
            skip_reason,
            "overflow",
            checks,
            agent_id,
            name,
            runner=str(runner.get("name") or ""),
            runner_id=runner.get("id"),
            url=str(runner.get("url") or ""),
        )
    else:
        _audit_dispatch_failed(runner, checks, agent_id, name, reason, "overflow")
    raise _FarmRetryLocal(reason)


# A picked runner that turned out unusable for a reason that is NOT a capacity
# event. These are dispatch reasons too, defined here because SKIP_REASONS
# needs them and the dispatch sites sit further down: one string, both uses,
# so the ledger label and the dispatch reason cannot drift apart.
RUNNER_UNCONFIGURED = "runner_unconfigured"
PAYLOAD_UNSERIALISABLE = "payload_unserialisable"

# Closed vocabulary for ci_farm_skipped rows. A skip is an ELIGIBLE dispatch
# that found no usable runner - never a shape the gate refused by design, or
# every branch-CI run would write a row claiming the farm declined it.
SKIP_REASONS = (
    "no_runner_registered",
    "disabled",
    "unhealthy",
    "at_capacity",
    "busy",
    "ineligible_tree",
    RUNNER_UNCONFIGURED,
    PAYLOAD_UNSERIALISABLE,
)
# Probe budget for the admin panel. Concurrency AND a cap, because a serial
# N-pings-at-CI_FARM_HTTP_TIMEOUT loop (default 8s) on a page that auto-reloads
# every 5s while CI is in flight stacks overlapping to_thread renders on the
# default executor - the probe becomes an amplifier of what it reports.
FARM_PROBE_WORKERS = 8
FARM_PROBE_MAX = 16
# The runner's own ceilings, mirrored so a payload that would be rejected
# after a multi-MB upload is refused before it is sent. The gate's own comment
# records paying that upload "to learn the cap" as owed; this is the pre-check.
# Kept as module constants rather than read from the runner, because the runner
# is a separate process on another box and importing its module here would be
# the wrong dependency direction.
FARM_MAX_FILES_COUNT = 50
FARM_MAX_FILES_BYTES = 5 * 1024 * 1024

# Closed vocabulary for a dispatch that was attempted and produced nothing.
# The distinction that matters operationally is rejected vs unknown: a 4xx
# means the runner declined and provably did NO work, while a timeout means
# the run may or may not have executed remotely - in which case the caller's
# local retry may be the second execution of the same work. urlopen raises
# HTTPError on any 4xx, which is why every cause below used to collapse into
# the single string "runner reply unreadable".
DISPATCH_REJECTED = "runner_rejected"
DISPATCH_MAY_HAVE_RUN = "may_have_executed"
# The two refusals that are NOT capacity events. Both lanes send
# DISPATCH_REJECTED to a "busy" skip because a 4xx really is the farm being
# full; these two are not, so they keep their own labels instead.
_REJECTED_NOT_A_CAPACITY_EVENT = (RUNNER_UNCONFIGURED, PAYLOAD_UNSERIALISABLE)


def log_skip(
    reason: str,
    lane: str,
    checks: str,
    agent_id: int,
    name: str,
    **extra,
) -> None:
    """Ledger one eligible dispatch that found no usable runner.

    The sibling of _audit_dispatch_failed, and the half that function's own
    docstring asks for: that row needs a runner to have been PICKED first, so a
    farm that is switched off, has no runner registered, or whose runners are
    all down logged nothing at all - and "the farm is idle" read identically to
    "the farm is broken". A record only; nothing keys eligibility off it,
    because a skip-on-failure rule with no self-clearing path would brick
    dispatch until an operator intervened.
    """
    if reason not in SKIP_REASONS:
        reason = "unhealthy"
    try:
        events.log_event(
            events.EVT_CI_FARM_SKIPPED,
            actor_agent_id=agent_id,
            actor_name=name,
            detail={
                "reason": reason,
                "lane": lane,
                "checks": checks,
                "runner": str(extra.pop("runner", "") or ""),
                "runner_id": extra.pop("runner_id", None),
                "url": str(extra.pop("url", "") or ""),
                **extra,
            },
        )
    except Exception:
        # domain: degrade-silently - the audit row is best-effort; the
        # caller's local fallback must happen either way.
        pass


def tree_payload(
    agent_id: int, tree: str, files: list | None, base_ref: str
) -> tuple[list[dict] | None, str]:
    """The `files` a named-tree run must ship to reproduce the tree locally.

    Returns (payload, reason); reason is "" when the tree may be dispatched.

    PARITY, which is the whole reason this is safe to do at all: the local
    path (_prepare_named_tree) resets the tree to current origin/<base> and
    replays the stored deltas and then the incoming ones, and the runner's
    local path does the same thing to a fresh clone of that same ref. So both
    sides run "current base + stored + incoming", and the payload that makes
    them equal is the UNION - not the request's newest delta alone.

    base_ref is a TRUTHINESS PRE-CHECK ONLY. The caller passes it and this
    function returns it in no form: it is not sent to the runner and it does
    not build the payload, so the union's parity rests on the runner's own
    default clone ref coinciding with github.base_branch() - a property nothing
    here verifies. try_dispatch's executed_base_sha check does NOT run for this
    lane either, because the caller passes base_ref=None: the runner's base_ref
    echo is unverified on the live box, and guessing it makes every tree
    dispatch fall back local, i.e. P2 silently delivering nothing. _runs.py
    carries the authoritative statement of that gap and its residual exposure.

    Refuses rather than guesses. A tree is ineligible when it has no incoming
    delta to ship, or when the union would exceed the runner's ceilings -
    shipping a payload the runner will reject costs a multi-MB round trip and
    then falls back locally, which is the failure the pre-check exists to stop.
    """
    incoming = [f for f in (files or []) if isinstance(f, dict)]
    if not incoming:
        return None, "ineligible_tree"
    try:
        from server.ci_runner import _trees as _trees_mod

        stored = _trees_mod.named_tree_deltas(agent_id, tree)
    except Exception:
        # domain: degrade-silently - if we cannot read the store we cannot
        # prove the union is complete, so the tree stays local
        return None, "ineligible_tree"
    if stored is None:
        # The store EXISTS but cannot be read whole - a corrupt blob makes the
        # private reader stop and hand back only the prefix it managed to read.
        # That is NOT the same answer as a tree holding no deltas, and it is
        # not visible here: without this arm the loop below would happily
        # concatenate a truncated prefix with the incoming delta and ship it,
        # so the runner would reset to base, apply a SUBSET of the tree, and
        # return its green as this run's verdict. That is the one direction
        # that reports success for a tree nobody tested. Refuse instead.
        return None, "ineligible_tree"
    payload: list[dict] = []
    for blob in stored:
        payload.extend(f for f in blob if isinstance(f, dict))
    payload.extend(incoming)
    if len(payload) > FARM_MAX_FILES_COUNT:
        return None, "ineligible_tree"
    total = 0
    for entry in payload:
        content = entry.get("content")
        if content:
            total += len(content.encode("utf-8"))
            if total > FARM_MAX_FILES_BYTES:
                return None, "ineligible_tree"
    if not base_ref:
        return None, "ineligible_tree"
    return payload, ""


def classify_no_pick() -> tuple[str, int]:
    """Why did the pick_runner() that just came back empty find nothing?

    Re-derived from state the failed attempt already wrote, so it costs no
    extra ping and reserves nothing: pick_runner calls _mark(.., "stale") on a
    ping failure and _mark(.., "busy", heartbeat=True) on a busy runner, so
    the registry and _ACTIVE_RUNS already hold the answer.

    Precedence, stated precisely because the first cut of this sentence
    overclaimed it. `unhealthy` is the fall-through, so it is the only value
    that cannot mask another. `at_capacity` and `busy` are each an `any()`
    over the registry, and `at_capacity` is tested FIRST, so with one runner
    at its cap and another busy this reports `at_capacity` even though both
    populations are present. That is a deliberate choice - a saturated farm
    is the more actionable fact - and not an ordering bug, but it is not the
    "one runner cannot mask another" property the old wording claimed.
    """
    try:
        with db._conn() as conn:
            rows = [
                _row_to_dict(r)
                for r in conn.execute(
                    "SELECT * FROM ci_runners WHERE status != 'removed'"
                ).fetchall()
            ]
    except Exception:  # domain: degrade-silently - an unreadable registry is
        return "no_runner_registered", 0  # not distinguishable from "none".
    if not rows:
        return "no_runner_registered", 0
    cap = max(0, int(config.CI_FARM_RUNNER_MAX_ACTIVE))
    if cap == 0:
        # pick_runner returns None at this same knob BEFORE it reads the
        # registry or pings anything, so the recorded state says nothing about
        # why. Falling through reported a deliberately switched-off farm as
        # `unhealthy` - every runner down - which is the one confusion this
        # whole feature exists to remove, produced by the operator's own
        # off-switch. Tested before capacity: `cap and ...` skipped the check
        # entirely at zero and dropped to the fall-through.
        return "disabled", len(rows)
    if any(_ACTIVE_RUNS.get(r["id"], 0) >= cap for r in rows):
        return "at_capacity", len(rows)
    if any(str(r.get("status") or "") == "busy" for r in rows):
        return "busy", len(rows)
    return "unhealthy", len(rows)


def probe_runners() -> list[dict]:
    """Live /health for every registered runner, for the admin panel.

    Read-only on purpose: it pings, but never reserves a slot, never calls
    _mark and never writes the registry. A panel that reused pick_runner would
    consume a dispatch slot and stamp last_heartbeat on every render - which
    would destroy the one thing that column means. A failed ping here reports
    reachable=False and writes nothing, so viewing the panel can never change
    what it reports.

    Probed CONCURRENTLY and capped at FARM_PROBE_MAX - see
    FARM_PROBE_WORKERS for why the serial version was an amplifier rather than
    a probe. Returns one entry per probed runner and nothing else; a caller
    that renders a cap must disclose it, because showing 16 of 20 registered
    runners and saying nothing reads as "those are all of them".
    """
    rows = list_runners()
    if not rows:
        return []
    shown = rows[:FARM_PROBE_MAX]

    def _ping_row(row: dict) -> dict | None:
        return _ping(row.get("url") or "", row.get("token") or "")

    workers = min(FARM_PROBE_WORKERS, len(shown))
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        pings = list(pool.map(_ping_row, shown))
    out: list[dict] = []
    for row, ping in zip(shown, pings, strict=True):
        entry: dict = {
            "id": row.get("id"),
            "name": str(row.get("name") or ""),
            "url": str(row.get("url") or ""),
            "status": str(row.get("status") or "unknown"),
            "last_heartbeat": row.get("last_heartbeat"),
        }
        if str(row.get("status") or "") == "removed":
            entry["reachable"] = None
        else:
            entry["reachable"] = ping is not None
            if ping is not None:
                entry["busy"] = bool(ping.get("busy"))
                entry["head_sha"] = _runner_head_sha(ping)
        out.append(entry)
    # No sentinel row for the overflow. The panel keys its lookup on
    # int(p["id"]), so an id=None entry would raise inside that comprehension
    # and degrade EVERY runner to "not probed" - trading a disclosed cap for a
    # silent false negative. The caller discloses the cap instead, which is
    # where the disclosure has to be rendered anyway.
    return out


def pick_runner() -> dict | None:
    """Pick a healthy, available runner and reserve one active-run slot.

    Pings each candidate live (short timeout), skips dead or busy ones, and
    stamps the heartbeat on success. Orders by last_heartbeat ASC (oldest
    first) for fairness. The cap check and the reservation happen
    atomically under _ACTIVE_LOCK, so two concurrent overflows cannot
    double-book a single-flight runner. The reservation releases in
    dispatch_to_runner's finally (and remove_runner drops it); callers that
    pick without dispatching (tests) must call _release().

    Every candidate is pinged, however old its recorded heartbeat: skipping
    without a ping would brick the farm after one idle spell (nothing else
    refreshes the heartbeat - there is no background poller), so a quiet hour
    would darken every runner permanently. Only a failed ping marks a runner
    stale. There is deliberately no staleness-threshold knob: CI_FARM_STALE_
    SECONDS existed, was read nowhere, and its docstring described exactly
    the skip this function refuses to do.

    P3-3: skips runners at their active_runs cap.

    Returns the post-mark row dict or None.
    """
    if config.CI_FARM_RUNNER_MAX_ACTIVE <= 0:
        # Explicitly disabled: a non-positive cap admits nothing. Previously
        # this fell out of the >= comparison silently - same behavior, said
        # out loud so a zero knob reads as off, not broken.
        return None
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ci_runners WHERE status != 'removed'"
            " ORDER BY last_heartbeat ASC, id ASC"
        ).fetchall()
    for row in [_row_to_dict(r) for r in rows]:
        if _ACTIVE_RUNS.get(row["id"], 0) >= config.CI_FARM_RUNNER_MAX_ACTIVE:
            continue
        ping = _ping(row["url"], row.get("token") or "")
        if ping is None:
            _mark(row["id"], "stale")
            continue
        if ping.get("busy"):
            _mark(row["id"], "busy", heartbeat=True)
            continue
        with _ACTIVE_LOCK:
            if _ACTIVE_RUNS.get(row["id"], 0) >= config.CI_FARM_RUNNER_MAX_ACTIVE:
                continue
            _ACTIVE_RUNS[row["id"]] = _ACTIVE_RUNS.get(row["id"], 0) + 1
        _mark(row["id"], "healthy", heartbeat=True)
        with db._conn() as conn:
            fresh = conn.execute(
                "SELECT * FROM ci_runners WHERE id = ?", (row["id"],)
            ).fetchone()
        picked = _row_to_dict(fresh)
        # /health publishes this and pick_runner used to read only
        # `ping is None` and `ping.get("busy")`, so the one field that could
        # falsify the parity claim crossed the wire and was discarded.
        picked["_runner_head_sha"] = _runner_head_sha(ping)
        return picked
    return None


# --- dispatch --------------------------------------------------------------


def dispatch_to_runner(runner: dict, payload: dict) -> dict | None:
    """POST /run to the runner. Returns the result dict or None on failure.

    On failure the reason is stashed on `runner` under "_dispatch_reason" -
    the same private-key convention pick_runner already uses for
    "_runner_head_sha", for the same reason: it rides on the dict the caller
    already holds. The vocabulary is DISPATCH_REJECTED (the runner declined,
    so provably no work was done), DISPATCH_MAY_HAVE_RUN (a timeout or
    transport break, where the run may or may not have executed remotely - so
    the caller's local retry may be its second execution), and
    "unreadable_body" (a 2xx whose payload was not a dict). Splitting these is
    the point: they shared one string with a 409, which is a capacity skip, so
    a full farm was reported as a broken one. The two refusals that are NOT
    capacity events carry their own values instead - RUNNER_UNCONFIGURED (the
    row is not routable) and PAYLOAD_UNSERIALISABLE (we could not encode the
    payload) - because both lanes file DISPATCH_REJECTED as a "busy" skip, so
    sharing that value would report a full farm that was never full.

    Deliberately still `-> dict | None` rather than a (result, reason) tuple.
    Three existing test modules mock this function with a bare result dict;
    returning a tuple made them unpack a dict's keys and raised
    ValueError. Changing the contract to carry new information would have
    meant editing pins that have nothing to do with this change.

    The active-run slot was reserved by pick_runner. Every path INSIDE the
    try releases it in the finally; the id/url guard sits ABOVE that try and
    so releases explicitly, as does the payload-serialisation except. The
    earlier version of this paragraph claimed the finally covered the guard.
    It does not, and a comment asserting a release the reader cannot see is
    the same defect class this module exists to remove (finding #118). A
    runner dict without an id or url fails closed (None) rather than raising
    KeyError out of the degrade-silently contract, and is recorded as a
    dispatch rejection rather than a skip: a runner WAS picked and a slot WAS
    reserved here, so this is not one of the populations classify_no_pick
    reads off the registry.
    """
    rid = runner.get("id")
    url = (runner.get("url") or "").rstrip("/") + "/run"
    if rid is None or not runner.get("url"):
        # Release explicitly: the finally below is the normal release path, but
        # this return sits BEFORE the try, so it stranded the slot pick_runner
        # had already reserved. A stranded _ACTIVE_RUNS entry holds that
        # runner at its cap until remove_runner - the one failure here with no
        # self-clearing path.
        #
        # Reachability, measured rather than assumed (finding #118):
        # `ci_runners.url` is `TEXT NOT NULL` with NO check constraint, so the
        # column itself does permit an empty url. What keeps such a row out
        # today is the single production writer - the admin farm-register
        # route, which refuses an empty or non-http url before calling
        # register_runner - plus pick_runner, which can only return a row
        # whose /health answered. So this guards a latent leak rather than a
        # live one; a future writer that skips that validation makes it live.
        #
        # Labelled RUNNER_UNCONFIGURED rather than DISPATCH_REJECTED. Nothing
        # was sent, so "rejected" is true but useless, and try_dispatch routes
        # DISPATCH_REJECTED to a "busy" CAPACITY skip - so sharing that value
        # would report a saturated farm for a farm that was never saturated
        # (finding #118).
        if rid is not None:
            _release(rid)
        runner["_dispatch_reason"] = RUNNER_UNCONFIGURED
        return None
    try:
        body = json.dumps(payload).encode("utf-8")
    except Exception:
        # domain: degrade-silently - a payload we cannot serialise was never
        # sent, so nothing ran remotely: release the slot, and give it its own
        # label rather than may_have_executed (which would lie about execution)
        # or DISPATCH_REJECTED (which try_dispatch files as a "busy" capacity
        # skip). Same reason the build sits inside its own try.
        _release(rid)
        runner["_dispatch_reason"] = PAYLOAD_UNSERIALISABLE
        return None
    headers = {"Content-Type": "application/json"}
    token = runner.get("token") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req, timeout=config.CI_FARM_DISPATCH_TIMEOUT
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if isinstance(body, dict):
            return body
        runner["_dispatch_reason"] = "unreadable_body"
        return None
    except urllib.error.HTTPError as exc:
        # domain:http - urlopen RAISES on any 4xx/5xx, so a runner that
        # DECLINED the run landed in the bare handler below and was recorded as
        # "runner reply unreadable", byte-identical to the string a mid-run
        # socket reset produced. A 4xx is provably rejected: no work was done,
        # so a local retry is the only execution. A 5xx is the opposite - it
        # may have started before failing - so it stays may_have_executed.
        code = int(getattr(exc, "code", 0) or 0)
        runner["_dispatch_reason"] = (
            DISPATCH_REJECTED if 400 <= code < 500 else DISPATCH_MAY_HAVE_RUN
        )
        return None
    except TimeoutError:
        # domain:http - socket.timeout is TimeoutError on py311+; the run's
        # fate is unknown, which is why the caller must not read this as a
        # clean skip.
        runner["_dispatch_reason"] = DISPATCH_MAY_HAVE_RUN
        return None
    except Exception:
        # domain: degrade-silently - transport failure reads as no result;
        # try_dispatch turns a picked-but-failed dispatch into a local retry
        runner["_dispatch_reason"] = DISPATCH_MAY_HAVE_RUN
        return None
    finally:
        _release(rid)


def _fold_output(detail: dict, remote: dict) -> dict:
    """Fold the runner's summary/output_tail/failed_files into the ledger detail.

    Byte-caps the tail, sets output_truncated, and carries all red-condition
    fields (timed_out, exit_code, merge_conflict, conflict_files, pr_number,
    quiet, contended).
    """
    summary = remote.get("summary")
    if isinstance(summary, dict):
        detail["summary"] = summary
    tail = remote.get("output_tail")
    red = (
        not remote.get("ok")
        or remote.get("timed_out")
        or (remote.get("exit_code") not in (None, 0))
        or remote.get("merge_conflict")
    )
    if tail and red:
        cap = int(config.CI_RUN_EVENT_TAIL_BYTES)
        raw = tail.encode("utf-8")
        if len(raw) > cap:
            detail["output_tail"] = raw[-cap:].decode("utf-8", errors="replace")
            detail["output_truncated"] = True
        else:
            detail["output_tail"] = tail
    failed = remote.get("failed_files")
    if failed:
        detail["failed_files"] = failed
    for key in ("merge_conflict", "conflict_files", "pr_number", "quiet", "contended"):
        if key in remote:
            detail[key] = remote[key]
    return detail


def _map_and_log(
    remote: dict,
    checks: str,
    agent_id: int,
    name: str,
    kind_event: str,
    run_id: str | None,
    runner: dict,
    extra_detail: dict | None = None,
) -> dict:
    """Map the runner result to the host shape and write the ledger with runner
    provenance. Returns the result dict."""
    result: dict = {}
    for key in (
        "checks",
        "mode",
        "sandboxed",
        "ok",
        "timed_out",
        "exit_code",
        "duration_seconds",
        "head_sha",
        "output_tail",
        "output_truncated",
        "summary",
        "failed_files",
        "local",
        "base_sha",
        "base_ref",
        "executed_base_sha",
        "pr_number",
        "quiet",
        "contended",
        "bench_load",
        "merge_conflict",
        "conflict_files",
    ):
        if key in remote:
            result[key] = remote[key]
    if result.get("mode") == "main":
        result["mode"] = "native"
    result["runner"] = runner["name"]
    detail = {
        "checks": checks,
        "mode": result.get("mode"),
        "sandboxed": result.get("sandboxed"),
        "ok": result.get("ok"),
        "timed_out": result.get("timed_out"),
        "exit_code": result.get("exit_code"),
        "duration_seconds": result.get("duration_seconds"),
        "head_sha": result.get("head_sha"),
        "runner": runner["name"],
    }
    runner_sha = runner.get("_runner_head_sha")
    if runner_sha is not None:
        detail["runner_head_sha"] = runner_sha
    if result.get("local"):
        detail["local"] = True
        for key in ("base_ref", "base_sha", "executed_base_sha"):
            if result.get(key) is not None:
                detail[key] = result[key]
    detail = _fold_output(detail, result)
    if extra_detail:
        detail.update(extra_detail)
    sha = remote.get("output_sha256")
    if isinstance(sha, str) and _SHA256_RE.fullmatch(sha):
        # Audit-only carriage, written after the extra_detail merge so a
        # runner echo can never be clobbered by (or clobber) bench keys, and
        # written explicitly into both detail and result (the known-keys loop
        # above allowlists result keys, so only an explicit write rides it):
        # no retained tail exists to re-verify against (green runs keep no
        # tail by design) and the runner-side producer ships separately, so
        # a hash here is a correlation key, not proof. Anything else
        # (including the "deadbeef" placeholder shape) is dropped from both
        # ledger and result so a lying runner's hash is never laundered.
        detail["output_sha256"] = sha
        result["output_sha256"] = sha
    if run_id is not None:
        detail["run_id"] = run_id
        result["run_id"] = run_id
    try:
        events.log_event(
            kind_event,
            actor_agent_id=agent_id,
            actor_name=name,
            detail=detail,
        )
    except Exception:
        # domain: degrade-silently - audit row is best-effort
        pass
    return result


def try_dispatch(
    checks: str,
    local_mode: bool,
    branch_mode: bool,
    is_bench: bool,
    pr_number: int | None,
    files: list | None,
    tree: str | None,
    quiet: bool | None,
    base_ref: str | None,
    agent_id: int,
    name: str,
    kind_event: str,
    run_id: str | None,
) -> dict | None:
    """Overflow dispatch entry point (called from run_checks when the local
    pool is busy). Returns the full host-shaped result dict (ledger written
    with runner provenance), raises _FarmRetryLocal when a picked runner
    fails (the caller retries once locally), or returns None when dispatch
    is not eligible / no runner is available - the caller then raises the
    busy error.

    PR 2 scope: overflow for native + local modes only. Bench remote-first is
    PR 3; branch (pr_number) and named-tree runs are host-local and never
    dispatched.
    """
    if not config.CI_FARM_ENABLED:
        return None
    if is_bench or pr_number is not None or tree is not None:
        return None
    if local_mode and files is None:
        return None
    runner = pick_runner()
    if runner is None:
        reason, candidates = classify_no_pick()
        log_skip(reason, "overflow", checks, agent_id, name, candidates=candidates)
        return None
    mode = "local" if local_mode else "main"
    payload: dict = {"checks": checks, "mode": mode}
    if local_mode:
        payload["files"] = files
    if quiet is not None:
        payload["quiet"] = quiet
    if base_ref is not None:
        payload["base_ref"] = base_ref
    remote = dispatch_to_runner(runner, payload)
    why = str(runner.get("_dispatch_reason") or "")
    if remote is None and why in _REJECTED_NOT_A_CAPACITY_EVENT:
        # A picked-but-unusable runner, for a reason that is not the farm being
        # full. Each keeps its own label: folding either into "busy" would
        # report a saturated farm when nothing was saturated (finding #118).
        # Still raises, so the caller retries locally exactly as it did before
        # - only the recorded cause changes. Every value in the routing tuple is
        # a member of SKIP_REASONS, so log_skip records it verbatim rather than
        # coercing it to "unhealthy".
        _retry_local(runner, why, checks, agent_id, name, skip_reason=why)
    if remote is None and why == DISPATCH_REJECTED:
        # The runner declined this run (409 busy, or another 4xx): it did no
        # work, so this is a CAPACITY skip, not a dispatch failure - filing it
        # as a failure would mislabel the cause and imply a broken farm when
        # the farm is merely full. Still raises, so the caller retries here
        # exactly as it did before the taxonomy existed.
        _retry_local(
            runner,
            "runner declined the run",
            checks,
            agent_id,
            name,
            skip_reason="busy",
        )
    if not isinstance(remote, dict):
        # Transport failure, a timeout, or an unreadable body AFTER a runner
        # was picked: the run may or may not have executed remotely, so retry
        # once locally instead of reporting busy (P3-2). The reason now says
        # which of those it was; all three shared one string before.
        _retry_local(runner, why or "unreadable_body", checks, agent_id, name)
    if not isinstance(remote.get("ok"), bool):
        _retry_local(
            runner,
            str(remote.get("error") or "runner reply missing boolean ok")[:200],
            checks,
            agent_id,
            name,
        )
    if "error" in remote and not any(
        key in remote for key in ("exit_code", "summary", "head_sha", "base_sha")
    ):
        # Validation and compatibility failures are not completed CI runs;
        # retry once against the host instead of turning them into red results.
        _retry_local(runner, str(remote.get("error"))[:200], checks, agent_id, name)
    if base_ref is not None:
        from github._core import _validate_ref

        try:
            expected_ref = _validate_ref(base_ref)
        except Exception:
            # Not a swallow: _retry_local audits the drop and re-raises the
            # typed signal the caller retries on.
            _retry_local(runner, "invalid base_ref", checks, agent_id, name)
        if remote.get("base_ref") != expected_ref:
            _retry_local(runner, "runner base_ref mismatch", checks, agent_id, name)
        executed_base = remote.get("executed_base_sha")
        result_base = remote.get("base_sha")
        if (
            not isinstance(executed_base, str)
            or _COMMIT_RE.fullmatch(executed_base) is None
            or result_base != executed_base
        ):
            _retry_local(
                runner,
                "runner base metadata missing or inconsistent",
                checks,
                agent_id,
                name,
            )
    return _map_and_log(remote, checks, agent_id, name, kind_event, run_id, runner)


def try_bench_dispatch(
    checks: str,
    agent_id: int,
    name: str,
    kind_event: str,
    run_id: str | None,
    pr_number: int | None = None,
    files: list | None = None,
    tree: str | None = None,
    base_ref: str | None = None,
    allow_remote: bool = False,
) -> dict | None:
    """Bench remote-first dispatch (PR 3). Tries a healthy runner before
    local slot acquisition. Returns the full host-shaped result dict
    (ledger written with runner provenance) or None when dispatch is not
    eligible / no runner is available - the caller then falls back to local.

    Per-machine quiet attestation: the runner reports its own quiet/contended
    state; host load is irrelevant. Anchor env is resolved server-side and
    passed in the payload as extra_env (the runner's wire key).

    Mode guard: bench only runs on origin/main reference. If pr_number,
    files, tree, or base_ref are set, this is not a reference bench and
    dispatch returns None (local fallback).

    Keeps the older None-on-error contract (silent local fallback): a runner
    error here never raises _FarmRetryLocal, unlike try_dispatch. Deliberate
    divergence, not drift - the bench lane has no retry of its own yet.
    """
    if not config.CI_FARM_ENABLED:
        return None
    if not config.CI_FARM_BENCH_REMOTE_FIRST and not allow_remote:
        return None
    if agent_id == 0:
        return None  # system/heartbeat benches: local only
    if pr_number is not None or files is not None or tree is not None:
        return None
    if base_ref is not None:
        # A stacked-diff bench measures the base_ref overlay, never
        # origin/main: dispatching it as mode=main would return the wrong
        # tree's numbers as the overlay result.
        return None
    runner = pick_runner()
    if runner is None:
        reason, candidates = classify_no_pick()
        log_skip(reason, "bench", checks, agent_id, name, candidates=candidates)
        return None
    # Resolve anchor env server-side via the shared helper (deferred import
    # to avoid circular dependency with _runs).
    anchor_env: dict[str, str] = {}
    bless_id: int | None = None
    try:
        from server.ci_runner._runs import _bench_anchor_env

        anchor_env, bless_id = _bench_anchor_env()
    except Exception:
        pass  # domain: degrade-silently - uninjected runs go advisory
    # Wire key is extra_env (the runner's _run_job reads payload["extra_env"]).
    # Env key must be in the runner's _EXTRA_ENV_ALLOWLIST (widened to 8192
    # chars alongside PR 1 so real medians tables round-trip).
    payload: dict = {"checks": checks, "mode": "main", "extra_env": anchor_env}
    remote = dispatch_to_runner(runner, payload)
    why = str(runner.get("_dispatch_reason") or "")
    if remote is None and why in (DISPATCH_REJECTED, *_REJECTED_NOT_A_CAPACITY_EVENT):
        # Declined, not broken. Silent local fallback is this lane's documented
        # contract, but the skip row is what makes a farm that is switched off
        # legible from the ledger instead of silent. A refusal that is not a
        # capacity event keeps its own label instead of being filed as "busy"
        # (finding #118); a genuine 4xx still is a capacity skip.
        log_skip(
            "busy" if why == DISPATCH_REJECTED else why,
            "bench",
            checks,
            agent_id,
            name,
            runner=str(runner.get("name") or ""),
            runner_id=runner.get("id"),
            url=str(runner.get("url") or ""),
        )
        return None
    if (
        not isinstance(remote, dict)
        or not isinstance(remote.get("ok"), bool)
        or "error" in remote
    ):
        _audit_dispatch_failed(
            runner,
            checks,
            agent_id,
            name,
            (
                (why or "unreadable_body")
                if not isinstance(remote, dict)
                else str(remote.get("error") or "runner reply unusable")[:200]
            ),
            "bench",
        )
        return None
    extra: dict = {}
    for key in ("quiet", "contended"):
        if key in remote:
            extra[key] = remote[key]
    # Stamp anchor_event_id server-side from the blessed anchor (not from
    # the runner echo, which is absent until attestation lands).
    if bless_id is not None:
        extra["anchor_event_id"] = bless_id
    return _map_and_log(
        remote,
        checks,
        agent_id,
        name,
        kind_event,
        run_id,
        runner,
        extra_detail=extra,
    )
