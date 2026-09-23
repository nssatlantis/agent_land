"""server.ci_runner._farm — CI farm registry + overflow dispatch (proposal #667, PR 2).

Offloads agent-invoked CI runs (native + local modes) to a spare LAN runner
when the local pool is saturated. The runner (ci_farm/runner.py) clones at the
pinned origin/main and executes with the same sandbox parity, so the bytes run
are identical to a host run.

Registry: a small ci_runners table (name, url, token, status, last_heartbeat).
Health: live ping in pick_runner (no background poller - small-fix surface).
Dispatch: try_dispatch() is called from run_checks when slot acquisition is
busy; it returns the full host-shaped result dict (ledger written with runner
provenance) or None when dispatch is not eligible / no runner is available -
the caller then raises the busy error.

PR 2 scope: overflow for native + local runs only. Bench remote-first is PR 3;
branch (pr_number) and named-tree runs are host-local and never dispatched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.request

import config
import db
import events
from db import ForumError


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
        return cur.rowcount > 0


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


def pick_runner() -> dict | None:
    """Pick a healthy, available runner.

    Pings each candidate live (short timeout), skips dead or busy ones, and
    stamps the heartbeat on success. Orders by last_heartbeat ASC (oldest
    first) for fairness. Returns the post-mark row dict or None.

    Every candidate is pinged, including ones whose recorded heartbeat is
    older than CI_FARM_STALE_SECONDS: skipping without a ping would brick
    the farm after that long idle (nothing else refreshes the heartbeat -
    no background poller exists), so a quiet hour would darken every runner
    permanently. Only a failed ping marks a runner stale.
    """
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ci_runners WHERE status != 'removed'"
            " ORDER BY last_heartbeat ASC, id ASC"
        ).fetchall()
    for row in [_row_to_dict(r) for r in rows]:
        ping = _ping(row["url"], row.get("token") or "")
        if ping is None:
            _mark(row["id"], "stale")
            continue
        if ping.get("busy"):
            _mark(row["id"], "busy", heartbeat=True)
            continue
        _mark(row["id"], "healthy", heartbeat=True)
        with db._conn() as conn:
            fresh = conn.execute(
                "SELECT * FROM ci_runners WHERE id = ?", (row["id"],)
            ).fetchone()
        return _row_to_dict(fresh)
    return None


# --- dispatch --------------------------------------------------------------


def dispatch_to_runner(runner: dict, payload: dict) -> dict | None:
    """POST /run to the runner. Returns the result dict or None on failure."""
    url = runner["url"].rstrip("/") + "/run"
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
        with urllib.request.urlopen(req, timeout=config.CI_RUN_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body if isinstance(body, dict) else None
    except Exception:
        return None


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
    result = dict(remote)
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
        detail["base_sha"] = result.get("base_sha")
    detail = _fold_output(detail, result)
    if extra_detail:
        detail.update(extra_detail)
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
    with runner provenance) or None when dispatch is not eligible / no runner
    is available - the caller then raises the busy error.

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
    if remote is None or not isinstance(remote, dict) or "error" in remote:
        return None
    return _map_and_log(remote, checks, agent_id, name, kind_event, run_id, runner)


def try_bench_dispatch(
    checks: str,
    agent_id: int,
    name: str,
    kind_event: str,
    run_id: str | None,
) -> dict | None:
    """Bench remote-first dispatch (PR 3). Tries a healthy runner before
    local slot acquisition. Returns the full host-shaped result dict
    (ledger written with runner provenance) or None when dispatch is not
    eligible / no runner is available - the caller then falls back to local.

    Per-machine quiet attestation: the runner reports its own quiet/contended
    state; host load is irrelevant. Anchor env is resolved server-side and
    passed in the payload.
    """
    if not config.CI_FARM_ENABLED:
        return None
    if not config.CI_FARM_BENCH_REMOTE_FIRST:
        return None
    runner = pick_runner()
    if runner is None:
        return None
    # Resolve anchor env server-side (blessed anchor resolution stays here).
    anchor_env: dict[str, str] = {}
    try:
        anchor = events.bench_anchor_for()
        if anchor and anchor.get("medians"):
            medians_json = json.dumps(anchor["medians"], separators=(",", ":"))
            bless_id = anchor.get("bless_event_id")
            anchor_env = {
                "BENCH_ANCHOR_MEDIANS": medians_json,
                "BENCH_ANCHOR_EVENT_ID": str(bless_id)
                if isinstance(bless_id, int)
                else "",
            }
    except Exception:
        pass  # domain: degrade-silently - uninjected runs go advisory
    payload: dict = {"checks": checks, "mode": "main", "anchor_env": anchor_env}
    remote = dispatch_to_runner(runner, payload)
    if remote is None or not isinstance(remote, dict) or "error" in remote:
        return None
    extra: dict = {}
    for key in ("quiet", "contended", "bench_load", "anchor_event_id"):
        if key in remote:
            extra[key] = remote[key]
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
