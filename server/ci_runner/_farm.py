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

import json
import urllib.request
from datetime import datetime, timezone

import config
import db
import events


def _now() -> str:
    return db._now_iso()


def _row_to_dict(row) -> dict:
    return dict(row)


# --- registry CRUD ---------------------------------------------------------


def register_runner(name: str, url: str, token: str = "") -> dict:
    """Insert a runner row (status unknown until its first ping). Returns the row."""
    now = _now()
    with db._conn(immediate=True) as conn:
        cur = conn.execute(
            "INSERT INTO ci_runners"
            " (name, url, token, status, last_heartbeat, created_at, updated_at)"
            " VALUES (?, ?, ?, 'unknown', NULL, ?, ?)",
            (name, url, token, now, now),
        )
        row = conn.execute(
            "SELECT * FROM ci_runners WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
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


def _heartbeat_age_seconds(last: str | None) -> float | None:
    if not last:
        return None
    try:
        return (
            datetime.now(timezone.utc).timestamp()
            - datetime.fromisoformat(last).timestamp()
        )
    except Exception:
        return None


def pick_runner() -> dict | None:
    """Pick a healthy, available runner.

    Pings each candidate live (short timeout), skips stale or busy ones, and
    stamps the heartbeat on success. Returns the row dict or None.
    """
    for row in list_runners():
        if row.get("status") == "removed":
            continue
        age = _heartbeat_age_seconds(row.get("last_heartbeat"))
        if age is not None and age > config.CI_FARM_STALE_SECONDS:
            continue
        ping = _ping(row["url"], row.get("token") or "")
        if ping is None:
            _mark(row["id"], "stale")
            continue
        if ping.get("busy"):
            _mark(row["id"], "busy", heartbeat=True)
            continue
        _mark(row["id"], "healthy", heartbeat=True)
        return row
    return None


# --- dispatch --------------------------------------------------------------


def dispatch_to_runner(runner: dict, payload: dict) -> dict | None:
    """POST /run to the runner. Returns the result dict or None on failure."""
    url = runner["url"].rstrip("/") + "/run"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {runner.get('token') or ''}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.CI_RUN_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body if isinstance(body, dict) else None
    except Exception:
        return None


def _fold_output(detail: dict, remote: dict) -> dict:
    """Fold the runner's summary/output_tail/failed_files into the ledger detail."""
    summary = remote.get("summary")
    if isinstance(summary, dict):
        detail["summary"] = summary
    tail = remote.get("output_tail")
    if tail and not remote.get("ok"):
        cap = int(config.CI_RUN_EVENT_TAIL_BYTES)
        detail["output_tail"] = tail[-cap:]
    failed = remote.get("failed_files")
    if failed:
        detail["failed_files"] = failed
    return detail


def _map_and_log(
    remote: dict,
    checks: str,
    agent_id: int,
    name: str,
    kind_event: str,
    run_id: str | None,
    runner: dict,
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
    if is_bench or branch_mode or tree is not None:
        return None
    runner = pick_runner()
    if runner is None:
        return None
    mode = "local" if local_mode else "main"
    payload: dict = {"checks": checks, "mode": mode}
    if local_mode:
        payload["files"] = files
    remote = dispatch_to_runner(runner, payload)
    if remote is None or not isinstance(remote, dict) or "error" in remote:
        return None
    return _map_and_log(remote, checks, agent_id, name, kind_event, run_id, runner)
