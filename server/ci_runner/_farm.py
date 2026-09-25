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

import hashlib
import json
import re
import sqlite3
import threading
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
            },
        )
    except Exception:
        # domain: degrade-silently - the audit row is best-effort; the
        # caller's fallback must happen either way.
        pass


def _retry_local(
    runner: dict, reason: str, checks: str, agent_id: int, name: str
) -> NoReturn:
    """Audit the failure, then raise the caller's retry-locally signal.

    Every picked-but-unusable runner reply funnels through here, so the
    overflow lane gets one durable ledger row per dropped dispatch instead
    of a silent host fallback. Annotated NoReturn (not None) so mypy - and
    the next reader - know control never continues past the call.
    """
    _audit_dispatch_failed(runner, checks, agent_id, name, reason, "overflow")
    raise _FarmRetryLocal(reason)


def pick_runner() -> dict | None:
    """Pick a healthy, available runner and reserve one active-run slot.

    Pings each candidate live (short timeout), skips dead or busy ones, and
    stamps the heartbeat on success. Orders by last_heartbeat ASC (oldest
    first) for fairness. The cap check and the reservation happen
    atomically under _ACTIVE_LOCK, so two concurrent overflows cannot
    double-book a single-flight runner. The reservation releases in
    dispatch_to_runner's finally (and remove_runner drops it); callers that
    pick without dispatching (tests) must call _release().

    Every candidate is pinged, including ones whose recorded heartbeat is
    older than CI_FARM_STALE_SECONDS: skipping without a ping would brick
    the farm after that long idle (nothing else refreshes the heartbeat -
    no background poller exists), so a quiet hour would darken every runner
    permanently. Only a failed ping marks a runner stale.

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
        return _row_to_dict(fresh)
    return None


# --- dispatch --------------------------------------------------------------


def dispatch_to_runner(runner: dict, payload: dict) -> dict | None:
    """POST /run to the runner. Returns the result dict or None on failure.

    The active-run slot was reserved by pick_runner; it releases here in the
    finally via _release(). A runner dict without an id or url fails closed
    (None) instead of raising KeyError out of the degrade-silently contract.
    """
    rid = runner.get("id")
    url = (runner.get("url") or "").rstrip("/") + "/run"
    if rid is None or not runner.get("url"):
        return None
    headers = {"Content-Type": "application/json"}
    token = runner.get("token") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req, timeout=config.CI_FARM_DISPATCH_TIMEOUT
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body if isinstance(body, dict) else None
    except Exception:
        # domain: degrade-silently - transport failure reads as no result;
        # try_dispatch turns a picked-but-failed dispatch into a local retry
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
    if not isinstance(remote, dict):
        # Transport failure or unreadable body AFTER a runner was picked: the
        # run may or may not have executed remotely, so retry once locally
        # instead of reporting busy (P3-2).
        _retry_local(runner, "runner reply unreadable", checks, agent_id, name)
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
        return None  # system/heartbeat benches: local only, can never bless
    if pr_number is not None or files is not None or tree is not None:
        return None
    if base_ref is not None:
        # A stacked-diff bench measures the base_ref overlay, never
        # origin/main: dispatching it as mode=main would return the wrong
        # tree's numbers as the overlay result.
        return None
    runner = pick_runner()
    if runner is None:
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
                "runner reply unreadable"
                if not isinstance(remote, dict)
                else str(remote.get("error") or "runner reply unusable")[:200]
            ),
            "bench",
        )
        return None
    extra: dict = {}
    for key in ("quiet", "contended", "bench_load"):
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
