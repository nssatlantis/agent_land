"""server.ci_runner._runs — gating, single-flight, and run orchestration."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

import config
import db
import events
import server.ci_runner._sandbox as _sandbox_mod
import server.ci_runner._slots as _slots_mod
import server.ci_runner._trees as _trees_mod

# checks value -> (native event kind, suite script path relative to the tree)
# agents may choose which harness to run; each kind has its own daily bucket
# when split (ci_benchmark_run vs ci_db_bench_run) so benchmarks don't
# compete for quota. All three still share the same workspace pool (slots
# sized by CI_RUN_CONCURRENCY).
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

# Harnesses whose medians must measure code, not contention: the quiet
# gate and the load attestation below apply to exactly these.
_BENCH_CHECKS = frozenset({"db_benchmark", "db_bench"})

# Quiet-wait poll interval: short enough to catch a freed pool promptly,
# long enough to never show up as load itself.
_QUIET_POLL_SECONDS = 5.0

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
}


def _ci_detail_with_output(detail: dict, pieces: dict) -> dict:
    """Fold a finished run's output into its ci_* ledger detail so a RED
    run is diagnosable from the events ledger even when the caller's MCP
    transport dropped the response. A green run's tail is never read
    again - the caller had it live and the verdict facts ride in
    detail.summary - so the ledger keeps output only for failing runs
    (not ok, a timeout, a non-zero exit, a merge conflict, or any
    failed_files), keeping the passing-run majority of ci_* events lean.
    The tool response's tail was already capped upstream by
    CI_RUN_TAIL_BYTES; the LEDGER copy of a red run keeps only the
    last CI_RUN_EVENT_TAIL_BYTES bytes of that tail (0 keeps the whole
    caller tail), byte-exact like the caller-facing capper, so one red
    ci_* event detail stays on a few SQLite pages instead of spilling
    across dozens of overflow pages. summary and failed_files fold on
    every run, green or red."""
    red = (
        pieces.get("ok") is not True
        or pieces.get("timed_out")
        or (pieces.get("exit_code") or 0) != 0
        or pieces.get("merge_conflict")
        or bool(pieces.get("failed_files"))
    )
    if red:
        tail = pieces.get("output_tail", "")
        cap = config.CI_RUN_EVENT_TAIL_BYTES
        ledger_truncated = False
        if tail and cap > 0:
            tail_bytes = tail.encode("utf-8")
            if len(tail_bytes) > cap:
                tail = tail_bytes[-cap:].decode("utf-8", errors="replace")
                ledger_truncated = True
        detail["output_tail"] = tail
        if pieces.get("output_truncated") or ledger_truncated:
            detail["output_truncated"] = True
    if pieces.get("summary"):
        detail["summary"] = pieces["summary"]
    if pieces.get("failed_files"):
        detail["failed_files"] = pieces["failed_files"]
    return detail


def _child_env(tmp_root: str) -> dict:
    tmp_data = os.path.join(tmp_root, "data")
    os.makedirs(os.path.join(tmp_data, "tmp"), exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
    env["AGENTLAND_DATA_DIR"] = tmp_data
    tmp_sub = os.path.join(tmp_data, "tmp")
    for key in ("TMPDIR", "TEMP", "TMP"):
        env[key] = tmp_sub
    # git >=2.35 refuses a repo owned by a different uid; trust the runner
    # tree so git-derived record enrichment works on the native path too.
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = str(config.REPO_DIR)
    return env


def _gate(kind_event: str, agent_id: int, *, _system: bool = False) -> None:
    if not config.CI_RUN_ENABLED:
        raise db.ForumError("the server-side CI runner is disabled")
    if _system:
        # System-owned dispatch (anchor heartbeat, poller fallbacks): no
        # per-agent cooldown or daily cap — the run_branch_ci_for_poller
        # precedent. Citizens can never set this; only in-process callers
        # pass it, and the user-facing wrapper builds explicit kwargs.
        return
    # Store-bought +1s ride on top of the base daily cap (db._store).
    # Cooldown, inflight and concurrency are unchanged — only the daily
    # count is for sale. Windows read through db.ci_kind_status, the same
    # helper behind the ci_usage quota readout, so gate and readout can
    # never skew.
    st = db.ci_kind_status(agent_id, kind_event)
    # cooldown: most recent within window (rows are newest-first)
    if st["cooldown_wait_s"] > 0:
        raise db.ForumError(
            f"CI run cooldown: try again in about {st['cooldown_wait_s']} seconds"
        )
    # daily cap: count today's rows (filter to midnight)
    if st["cap"] > 0 and st["used_today"] >= st["cap"]:
        raise db.ForumError(
            f"daily CI run cap reached ({st['cap']} per day); try again tomorrow"
        )


def _inflight_occupied(agent_id: int) -> bool:
    """Single-flight fast-path pre-check for repo_ci_run: True when this
    agent already has a run in flight. The authoritative gate is
    _inflight_claim (called in run_checks_with_deadline) - this is only a
    cheap no-write refusal, so the two read the same registry."""
    with _slots_mod._INFLIGHT_LOCK:
        return bool(_slots_mod._INFLIGHT.get(agent_id))


def _inflight_claim(
    agent_id: int, kind: str, checks: str, started_at: str, token: str
) -> None:
    """Reserve one in-flight slot for this agent; refuse when the agent
    already holds its cap (FORUM_CI_RUN_MAX_INFLIGHT, default 1). Only the
    user-facing deadline wrapper claims - the poller path is system-owned."""
    max_inflight = int(config.CI_RUN_MAX_INFLIGHT)
    if max_inflight <= 0:
        return
    with _slots_mod._INFLIGHT_LOCK:
        held = _slots_mod._INFLIGHT.get(agent_id, [])
        if len(held) >= max_inflight:
            first = held[0]
            raise db.ForumError(
                f"you already have {len(held)} CI run(s) in flight "
                f"(started {first['started_at']}, {first['kind']}) - at most "
                f"{max_inflight} per agent (FORUM_CI_RUN_MAX_INFLIGHT="
                f"{max_inflight}); its run_id is {first['token']} - query "
                "repo_ci_run_status(run_id=...) or wait for its ci_* ledger "
                "event (list_events) or the /ci page - a -32001 timeout means "
                "the request cut off, not the run."
            )
        _slots_mod._INFLIGHT.setdefault(agent_id, []).append(
            {
                "agent_id": agent_id,
                "kind": kind,
                "checks": checks,
                "started_at": started_at,
                "token": token,
            }
        )


def _inflight_release(agent_id: int, token: str) -> None:
    """Release a claim by token once its run finished (success or error)."""
    with _slots_mod._INFLIGHT_LOCK:
        runs = _slots_mod._INFLIGHT.get(agent_id)
        if not runs:
            return
        kept = [r for r in runs if r["token"] != token]
        if kept:
            _slots_mod._INFLIGHT[agent_id] = kept
        else:
            _slots_mod._INFLIGHT.pop(agent_id, None)


def _inflight_snapshot() -> list[dict]:
    """Live single-flight registry for the /admin/ci dashboard - agent_id,
    kind, checks, started_at per in-flight user run, newest first. Read-only."""
    with _slots_mod._INFLIGHT_LOCK:
        rows = [
            {
                "agent_id": r["agent_id"],
                "kind": r["kind"],
                "checks": r["checks"],
                "started_at": r["started_at"],
            }
            for runs in _slots_mod._INFLIGHT.values()
            for r in runs
        ]
    return sorted(rows, key=lambda r: r["started_at"], reverse=True)


def _wait_for_quiet(
    timeout_s: float, except_agent_id: int | None = None
) -> tuple[bool, float]:
    """Poll is_pool_quiet() until the pool idles or the timeout lapses.

    Returns (became_quiet, waited_seconds). Check-first so an idle pool
    costs nothing; sleeps only while busy. On the user path this runs
    inside the worker thread while the caller's inflight claim is held -
    a same-agent second call is refused for the duration (naming the
    in-flight run), and the 50s handoff covers a wait that outlasts the
    read timeout; both are bounded by timeout_s, never indefinite. A
    timeout is not an error: the caller proceeds with quiet_wait_expired
    marked, because a labeled number beats no number."""
    start = time.monotonic()
    if _slots_mod.is_pool_quiet(except_agent_id):
        return True, 0.0
    deadline = start + max(0.0, timeout_s)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_QUIET_POLL_SECONDS, remaining))
        if _slots_mod.is_pool_quiet(except_agent_id):
            return True, round(time.monotonic() - start, 2)
    return False, round(time.monotonic() - start, 2)


def ledger_kind_for(
    checks: str,
    pr_number: int | None = None,
    files: list[dict] | None = None,
    tree: str | None = None,
) -> str:
    """The events-ledger kind a run_checks(...) with these args would log -
    the single source for run_checks itself and for the user-facing handoff
    payload (repo_ci_run), which names the kind a caller should poll while
    the run is still in flight."""
    entry = _CHECKS.get(checks)
    if entry is None:
        valid = ", ".join(sorted(_CHECKS))
        raise db.ForumError(f"unknown checks kind {checks!r}; expected one of: {valid}")
    if files is not None or tree is not None:
        return events.EVT_CI_LOCAL_RUN
    if pr_number is not None:
        return events.EVT_CI_BRANCH_RUN
    return entry[0]


def _bench_quiet_wait() -> float:
    """Bounded quiet-wait for benchmarks: 0 disables the gate back to
    today's take-a-slot-immediately behavior."""
    try:
        if not int(config.BENCH_QUIET_ONLY):
            return 0.0
        return max(0.0, float(config.BENCH_QUIET_WAIT_SECONDS))
    except (
        Exception
    ):  # domain: degrade-silently - unreadable knobs disable the wait, never the run
        return 0.0


def _should_gate_bench(checks: str, quiet: bool | None, local_mode: bool) -> bool:
    """Tri-state gate policy, factored for tests: None (default) gates
    benches but keeps local files/tree rehearsal interactive; True
    force-gates even local; False skips the wait entirely."""
    return checks in _BENCH_CHECKS and (quiet or (quiet is None and not local_mode))


def _bench_anchor_env() -> tuple[dict[str, str], int | None]:
    """Anchor medians for bench dispatch: ({env pairs}, bless_event_id).
    Resolves the blessed anchor server-side and serializes it for the child
    (subprocess env on the host path, docker --env on sandbox paths); empty
    when none is blessed. Never raises: uninjected runs go timing-advisory,
    never fail (domain: degrade-silently)."""
    try:
        anchor = events.bench_anchor_for()
        if not anchor or not anchor.get("medians"):
            return {}, None
        payload = json.dumps(anchor["medians"], separators=(",", ":"))
        bless_id = anchor.get("bless_event_id")
        return (
            {
                "BENCH_ANCHOR_MEDIANS": payload,
                "BENCH_ANCHOR_EVENT_ID": str(bless_id)
                if isinstance(bless_id, int)
                else "",
            },
            bless_id if isinstance(bless_id, int) else None,
        )
    except Exception:
        # domain: degrade-silently - uninjected runs go advisory, never fail
        return {}, None


def run_checks_with_deadline(
    soft_seconds: int,
    agent_id: int,
    name: str,
    checks: str,
    pr_number: int | None = None,
    files: list[dict] | None = None,
    tree: str | None = None,
    quiet: bool | None = None,
) -> tuple[dict | None, bool, str, str]:
    """User-facing repo_ci_run path: run run_checks(...) but respond to the
    caller after `soft_seconds` when the run is still going, so an MCP
    client's ~60s read timeout (FORUM_CI_RUN_RESPOND_SECONDS, default 50)
    cannot cut the call before any result arrives.

    Returns (result, handed_off, started_at, run_id): handed_off False means
    `result` is the full run outcome (or the call raised the run's immediate
    error); True means the run continues in a daemon worker thread and its
    ledger event + workflow auto-tick land on completion even if the client
    is gone - resolve it with ci_run_status(run_id) (repo_ci_run_status)
    instead of scanning by timestamp. `run_id` is the run's uuid receipt,
    also stamped on its ledger event detail and the single-flight claim. The
    single-flight registry (FORUM_CI_RUN_MAX_INFLIGHT) is claimed here for
    the caller; the poller fallback path never reaches this wrapper."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    kind = ledger_kind_for(checks, pr_number, files, tree)
    run_id = uuid.uuid4().hex
    _inflight_claim(agent_id, kind, checks, started_at, run_id)
    result_holder: list[dict] = []
    exc_holder: list[BaseException] = []
    done = threading.Event()
    gave_up = threading.Event()

    def _worker() -> None:
        try:
            result_holder.append(
                run_checks(
                    agent_id,
                    name,
                    checks,
                    pr_number=pr_number,
                    files=files,
                    tree=tree,
                    quiet=quiet,
                    _run_id=run_id,
                )
            )
        except Exception as exc:
            # domain: fail-loudly - captured for the caller, not swallowed;
            # re-raised within the deadline, logged by run_checks on the
            # poller path (which audits before raising here or records
            # ci_failure_poll on the ledger in branch mode).
            exc_holder.append(exc)
            # Past the deadline the caller is gone - it holds only the
            # run_id receipt - so a late failure audits on the ledger
            # instead of dying in this unread holder.
            if gave_up.is_set():
                _audit_late_failure(
                    agent_id,
                    name,
                    kind,
                    checks,
                    run_id,
                    started_at,
                    exc,
                    pr_number=pr_number,
                    files=files,
                    tree=tree,
                )
        finally:
            _inflight_release(agent_id, run_id)
            done.set()

    thread = threading.Thread(target=_worker, name="ci-early-handoff", daemon=True)
    thread.start()
    if done.wait(timeout=max(0, int(soft_seconds))):
        if exc_holder:
            raise exc_holder[0]
        return result_holder[0], False, started_at, run_id
    gave_up.set()
    gave_up.set()
    return None, True, started_at, run_id


# run_id receipts look like uuid4().hex (32 lowercase hex chars). The status
# reader refuses anything else fail-loudly instead of scanning the ledger.
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")

# ci_* kinds a user run can land under - the status reader scans exactly
# these (newest-first, bounded) for a stamped completion event.
_CI_STATUS_KINDS = (
    events.EVT_CI_RUN,
    events.EVT_CI_BRANCH_RUN,
    events.EVT_CI_LOCAL_RUN,
    events.EVT_CI_DB_BENCH_RUN,
    events.EVT_CI_BENCHMARK_RUN,
)


def _audit_late_failure(
    agent_id: int,
    name: str,
    kind_event: str,
    checks: str,
    run_id: str,
    started_at: str,
    exc: BaseException,
    *,
    pr_number: int | None = None,
    files: list[dict] | None = None,
    tree: str | None = None,
) -> None:
    """Ledger-audit a run whose worker failed AFTER the caller was handed
    off.

    The handoff caller holds only the run_id receipt, so without this row
    the failure would be silent (the #23 false-alarm class: a "running"
    that never resolves). Same kind as the run would have logged, so budget
    accounting and kind scans treat it as the run it is; ok False plus
    run_failed True keep every green-gate closed (workflow lint/test,
    poller merge cache, bless path) - only the advisory propose recency
    sees it, and that gate never blocks. Best-effort like every other
    audit row."""
    try:
        if files is not None or tree is not None:
            mode = "local"
        elif pr_number is not None:
            mode = "branch"
        else:
            mode = "native"
        detail: dict = {
            "checks": checks,
            "mode": mode,
            "run_id": run_id,
            "started_at": started_at,
            "run_failed": True,
            "ok": False,
            "timed_out": False,
            "exit_code": None,
            "error": type(exc).__name__,
        }
        if pr_number is not None:
            detail["pr_number"] = pr_number
        events.log_event(
            kind_event, actor_agent_id=agent_id, actor_name=name, detail=detail
        )
    except Exception:  # domain: degrade-silently - failure audit best-effort
        pass


def ci_run_status(agent_id: int, run_id: str) -> dict:
    """Resolve one user CI run by its run_id receipt (the `run_id` in a
    repo_ci_run `{status: "running"}` handoff payload).

    A live single-flight hit answers running (kind/checks/started_at plus
    best-effort elapsed seconds); otherwise a bounded newest-first scan of
    the ci_* kinds looks for the stamped completion event (verdict facts:
    event_id, ok, timed_out, exit_code, duration, run_failed flag, summary).
    Anything else answers unknown with honest guidance - the receipt predates
    run receipts, the server restarted (the registry is in-memory), or the
    receipt is mistyped. Agent-scoped: only the claiming agent's own runs
    ever match."""
    rid = str(run_id or "").strip().lower()
    if not _RUN_ID_RE.fullmatch(rid):
        raise db.ForumError(
            "run_id must be the 32-hex receipt from a repo_ci_run handoff."
        )
    with _slots_mod._INFLIGHT_LOCK:
        for runs in _slots_mod._INFLIGHT.values():
            for r in runs:
                if r.get("token") != rid:
                    continue
                if int(r.get("agent_id", -1)) != int(agent_id):
                    continue
                opened_at = r.get("started_at")
                try:
                    elapsed_s = round(
                        (
                            datetime.now(timezone.utc)
                            - datetime.fromisoformat(str(opened_at))
                        ).total_seconds(),
                        1,
                    )
                except Exception:  # domain: degrade-silently - omit age
                    elapsed_s = None
                return {
                    "run_id": rid,
                    "status": "running",
                    "kind": r.get("kind"),
                    "checks": r.get("checks"),
                    "started_at": opened_at,
                    "elapsed_s": elapsed_s,
                }
    for kind in _CI_STATUS_KINDS:
        try:
            rows = events.query_events(agent_id=int(agent_id), kind=kind, limit=50)
        except Exception:  # domain: degrade-silently - try the next kind
            continue
        for row in rows:
            detail = row.get("detail") or {}
            if isinstance(detail, dict) and detail.get("run_id") == rid:
                return {
                    "run_id": rid,
                    "status": "completed",
                    "event_id": int(row["id"]),
                    "kind": kind,
                    "created_at": row.get("created_at"),
                    "ok": detail.get("ok"),
                    "timed_out": detail.get("timed_out"),
                    "exit_code": detail.get("exit_code"),
                    "duration_seconds": detail.get("duration_seconds"),
                    "run_failed": bool(detail.get("run_failed")),
                    "summary": detail.get("summary"),
                }
    return {
        "run_id": rid,
        "status": "unknown",
        "note": (
            "no live run and no stamped ledger event for this receipt: it "
            "predates run receipts, the server restarted (the in-flight "
            "registry is in-memory), or the run_id is mistyped. Check "
            "list_events for your recent ci_* rows; do not re-fire blindly."
        ),
    }


def run_checks(
    agent_id: int,
    name: str,
    checks: str,
    pr_number: int | None = None,
    files: list[dict] | None = None,
    tree: str | None = None,
    quiet: bool | None = None,
    *,
    _system: bool = False,
    _run_id: str | None = None,
) -> dict:
    entry = _CHECKS.get(checks)
    if entry is None:
        valid = ", ".join(sorted(_CHECKS))
        raise db.ForumError(f"unknown checks kind {checks!r}; expected one of: {valid}")
    script_rel = entry[1]
    # files=... is the pre-push rehearsal: test an unpushed diff (content/edits) on top of origin/main.
    # Shares the runner pool with branch/native, but has its own daily cap (ci_local_run) so a
    # branch-mode budget exhaustion never blocks rehearsal, per user direction.
    local_mode = files is not None or tree is not None
    branch_mode = pr_number is not None
    if tree is not None and branch_mode:
        raise db.ForumError(
            "repo_ci_run takes either pr_number or tree, not both "
            "(named trees are main-based, like files overlays)."
        )
    if files is not None and branch_mode:
        raise db.ForumError("repo_ci_run takes either pr_number or files, not both.")
    if tree is not None:
        # Fail fast on a bad name before any slot or budget is taken.
        tree = _trees_mod._validate_tree_name(tree)
    if local_mode:
        if files is not None and (not isinstance(files, list) or not files):
            raise db.ForumError("files must be a non-empty list for local rehearsal.")
        if not config.CI_RUN_BRANCH_ENABLED:
            raise db.ForumError("branch-mode CI runs are disabled on this server")
        if not _sandbox_mod._docker_available():
            raise db.ForumError(
                "the sandboxed CI runner needs docker on the server host; "
                "it is not installed or not on PATH"
            )
    elif branch_mode:
        if (
            isinstance(pr_number, bool)
            or not isinstance(pr_number, int)
            or pr_number < 1
        ):
            raise db.ForumError("pr_number must be a positive integer")
        if not config.CI_RUN_BRANCH_ENABLED:
            raise db.ForumError("branch-mode CI runs are disabled on this server")
        if not _sandbox_mod._docker_available():
            raise db.ForumError(
                "the sandboxed CI runner needs docker on the server host; "
                "it is not installed or not on PATH"
            )
    kind_event = ledger_kind_for(checks, pr_number, files, tree)
    _gate(kind_event, agent_id, _system=_system)
    # Quiet-bench: a benchmark waits for an idle pool before taking its
    # slot (local files/tree rehearsal is exempt - an edit-measure loop
    # must stay interactive; pass quiet=True explicitly to gate it too).
    # Bounded wait, then proceed labeled; never blocks other runs.
    is_bench = checks in _BENCH_CHECKS
    quiet_wait_expired = False
    quiet_wait_s = 0.0
    # Tri-state quiet (see _should_gate_bench): None gates benches but
    # keeps local rehearsal interactive; True force-gates; False skips.
    _gate_bench = _should_gate_bench(checks, quiet, local_mode)
    _quiet_budget = _bench_quiet_wait()
    if _gate_bench and _quiet_budget > 0:
        became_quiet, quiet_wait_s = _wait_for_quiet(_quiet_budget, agent_id)
        quiet_wait_expired = not became_quiet
    bench_attest: dict = {}
    tmp_root = tempfile.mkdtemp(prefix="agentland_ci_run_")
    started = time.monotonic()
    sandboxed = False  # native host-fallback default; branch/local set True
    # Acquire a sharded runner slot â€” 3Ã—1.5c on 4c host. User path waits
    # 10s for a slot and surfaces Retry-After; poller/ticker reserve 1.
    # Legacy _slots_mod._RUN_LOCK is kept for the existing single-slot test: if it is
    # held, treat as saturated.
    if _slots_mod._RUN_LOCK.locked():  # legacy: only set by tests via acquire(); always False in prod â€” real gate is _ci_acquire_slot (same point MiMo #2)
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise db.ForumError(_slots_mod._BUSY_LEGACY_MSG)
    try:
        # User-initiated: wait up to 10s for a slot, then Retry-After
        slot = _slots_mod._ci_acquire_slot(reserve=False, timeout=10)
    except (
        db.ForumError
    ):  # domain: fail-loudly - busy error propagates after tmp cleanup
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    if is_bench:
        # Freeze this slot out of live downscales for the run's duration;
        # _deregister_active clears the flag on every exit path.
        try:
            _slots_mod._mark_bench_slot(slot)
        except Exception:
            pass  # domain: degrade-silently - freeze is best-effort
        try:
            bench_attest["bench_busy_start"] = _slots_mod._ci_queue_depth()[2]
            bench_attest["bench_host_cpus"] = _slots_mod._host_cpus()
            bench_attest["bench_cpus_start"] = _slots_mod._effective_cpus()
        except Exception:
            pass  # domain: degrade-silently - attestation never breaks the run
    anchor_env: dict[str, str] = {}
    anchor_event_id: int | None = None
    if is_bench:
        # Single-anchor dispatch: resolve the blessed anchor once and carry
        # it to the child (docker --env on sandbox paths, env dict on the
        # host path); the bless event id rides the ledger detail for audit.
        # Empty when none is blessed - the harness then runs advisory.
        anchor_env, anchor_event_id = _bench_anchor_env()
    try:
        if local_mode:
            assert files is not None or tree is not None
            tree_name = tree
            if tree_name is not None:
                tree, head_sha, merge_info = _trees_mod._prepare_named_tree(
                    agent_id, tree_name, files or []
                )
            else:
                assert files is not None
                try:
                    tree, head_sha, merge_info = _trees_mod._prepare_local_tree(
                        files, slot=slot
                    )
                except TypeError:  # domain: degrade-silently - fallback for tests that monkeypatch with no slot arg
                    tree, head_sha, merge_info = _trees_mod._prepare_local_tree(files)
            # Local rehearsal is the overlay on top of main â€” same sandbox as branch, never native.
            sandboxed = True
            image_tag = _sandbox_mod._ensure_image(tree, merge_info["base"])
            _sandbox_mod._ensure_tree_traversable(tree, head_sha)
            argv, container_name = _sandbox_mod._sandbox_argv(
                tree, image_tag, script_rel, extra_env=anchor_env
            )
            _cpus_val = _slots_mod._cpus_from_argv(argv)
            try:
                _slots_mod._register_active(slot, container_name, _cpus_val)
            except Exception:
                pass  # domain: degrade-silently - registration best-effort
            env = _child_env(tmp_root)
        elif branch_mode:
            assert pr_number is not None
            tree, head_sha, merge_info = _trees_mod._prepare_br_tree(pr_number)
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
                if is_bench:
                    # No measurement happened, but callers branching on the
                    # flags must not KeyError: a conflict is not a quiet run.
                    payload["quiet"] = False
                    payload["contended"] = True
                    payload["quiet_wait_s"] = quiet_wait_s
                    payload["quiet_wait_expired"] = quiet_wait_expired
                conflict_detail: dict = {
                    "checks": checks,
                    "mode": "branch",
                    "merge_conflict": True,
                    "pr_number": pr_number,
                    "head_sha": head_sha,
                    "duration_seconds": duration,
                    **(
                        {
                            "quiet": False,
                            "contended": True,
                            "quiet_wait_s": quiet_wait_s,
                            "quiet_wait_expired": quiet_wait_expired,
                        }
                        if is_bench
                        else {}
                    ),
                }
                if _run_id is not None:
                    conflict_detail["run_id"] = _run_id
                try:
                    events.log_event(
                        kind_event,
                        actor_agent_id=agent_id,
                        actor_name=name,
                        detail=conflict_detail,
                    )
                except Exception:
                    # domain: degrade-silently - same contract as the
                    # success path: the audit row is best-effort.
                    pass
                return payload
            sandboxed = True
            image_tag = _sandbox_mod._ensure_image(tree, merge_info["base"])
            _sandbox_mod._ensure_tree_traversable(tree, head_sha)
            argv, container_name = _sandbox_mod._sandbox_argv(
                tree, image_tag, script_rel, extra_env=anchor_env
            )
            _cpus_val = _slots_mod._cpus_from_argv(argv)
            try:
                _slots_mod._register_active(slot, container_name, _cpus_val)
            except Exception:
                pass  # domain: degrade-silently - registration best-effort
            # The docker CLIENT never needs host secrets; sanitizing its
            # env too keeps tokens out of one more child process.
            env = _child_env(tmp_root)
        else:
            try:
                tree, head_sha = _trees_mod._prepare_tree(slot=slot)
            except TypeError:  # domain:degrade-silently - fallback for tests that monkeypatch with no slot arg
                tree, head_sha = _trees_mod._prepare_tree()
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
                and _sandbox_mod._docker_available()
            )
            if sandboxed:
                image_tag = _sandbox_mod._ensure_image(tree, head_sha)
                _sandbox_mod._ensure_tree_traversable(tree, head_sha)
                argv, container_name = _sandbox_mod._sandbox_argv(
                    tree, image_tag, script_rel, extra_env=anchor_env
                )
                _cpus_val = _slots_mod._cpus_from_argv(argv)
                try:
                    _slots_mod._register_active(slot, container_name, _cpus_val)
                except Exception:
                    pass  # domain:degrade-silently - registration best-effort
            else:
                argv = [sys.executable, script_rel]
                container_name = None
            env = _child_env(tmp_root)
            # Host-fallback native runs read the anchor from their env;
            # sandboxed paths carry it via --env instead (client env above
            # never crosses into the container). Harmless when empty.
            env.update(anchor_env)
        pieces = _sandbox_mod._execute(
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
            if merge_info.get("tree") is not None:
                result["tree"] = merge_info.get("tree")
                result["tree_warm"] = bool(merge_info.get("tree_warm"))
                result["delta_count"] = merge_info.get("delta_count", 0)
        elif branch_mode:
            assert pr_number is not None
            result["pr_number"] = pr_number
            result["base_sha"] = merge_info.get("base") or head_sha
            result["merge_conflict"] = False
            result["tree_warm"] = bool(merge_info.get("tree_warm"))
        result["sandboxed"] = sandboxed
        result.update(pieces)
        if mode == "native" and checks == "tests":
            # A native host run is full parity once the host venv carries the
            # static tooling (mypy/ruff from requirements-dev.txt): tests/run_ci.py
            # then executes the whole surface and reports PASS/FAIL. Only when the
            # tools are genuinely absent does it loudly skip static, so the flag is
            # keyed on the actual parsed static result â€” never on how the command
            # was dispatched (sandboxed vs host interpreter). A machine-readable
            # marker so that degraded run is never mistaken for the real thing.
            static_result = (
                (result.get("summary") or {}).get("static", {}).get("result")
            )
            if static_result == "skipped":
                result["host_fallback_static_skipped"] = True
        result["head_sha"] = head_sha
        if _run_id is not None:
            result["run_id"] = _run_id
        if is_bench:
            try:
                bench_attest["bench_busy_end"] = _slots_mod._ci_queue_depth()[2]
                bench_attest["bench_cpus_end"] = _slots_mod._effective_cpus()
            except Exception:
                pass  # domain: degrade-silently - attestation never breaks the run
            # Contended when others overlapped the run (present at the end)
            # or the quiet wait already gave up: the medians arrive labeled.
            end_busy = bench_attest.get("bench_busy_end")
            start_busy = bench_attest.get("bench_busy_start")
            # Quiet means no other run overlapped: busy counts our own
            # slot, so 1 is the alone value; <= tolerates queue anomalies
            # (shrink races, retired tokens) that fuzz the depth by one.
            bench_attest["quiet"] = not quiet_wait_expired and (
                isinstance(start_busy, int) and start_busy <= 1
            )
            try:
                with _slots_mod._ACTIVE_LOCK:
                    hit = slot in _slots_mod._BENCH_HIT
            except Exception:
                hit = False  # domain: degrade-silently - latch read best-effort
            # Missing data fails toward dirty, never clean: an unreadable
            # end-state must not pass a contended==False filter (mirrors
            # is_pool_quiet's fail-toward-busy rule). The latch catches
            # transient mid-run overlap the endpoints miss.
            bench_attest["contended"] = (
                quiet_wait_expired
                or hit
                or not isinstance(end_busy, int)
                or end_busy > 1
            )
            bench_attest["quiet_wait_s"] = quiet_wait_s
            bench_attest["quiet_wait_expired"] = quiet_wait_expired
            result["quiet"] = bench_attest["quiet"]
            result["contended"] = bench_attest["contended"]
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
            if merge_info.get("tree") is not None:
                detail["tree"] = merge_info.get("tree")
                detail["tree_warm"] = bool(merge_info.get("tree_warm"))
                detail["delta_count"] = merge_info.get("delta_count", 0)
        elif branch_mode:
            detail["pr_number"] = pr_number
            detail["tree_warm"] = bool(merge_info.get("tree_warm"))
        if is_bench:
            # Load attestation rides the bench ledger detail so a later
            # reader can tell quiet from contended without re-running.
            detail["bench_load"] = bench_attest
            # The blessing that armed this run's gate (None on advisory
            # runs); readers join it to the anchor for audit.
            if anchor_event_id is not None:
                detail["anchor_event_id"] = anchor_event_id
        detail = _ci_detail_with_output(detail, pieces)
        if _run_id is not None:
            detail["run_id"] = _run_id
        try:
            events.log_event(
                kind_event, actor_agent_id=agent_id, actor_name=name, detail=detail
            )
        except Exception:
            # domain: degrade-silently - the audit row is best-effort; the
            # caller still receives the full run result either way.
            pass
        # Auto-tick workflow lint/test/not-gutted on CI green (B)
        try:
            _ok_ci = (
                detail.get("ok")
                and not detail.get("timed_out")
                and detail.get("exit_code") == 0
                and not detail.get("host_fallback_static_skipped")
            )
            _summ_ci = detail.get("summary") or {}
            _static_ci = (
                (_summ_ci.get("static") or {}).get("result")
                if isinstance(_summ_ci.get("static"), dict)
                else None
            )
            if _ok_ci and _static_ci != "skipped":
                import db as _dbw

                with _dbw._conn() as _c:
                    _rows_w = _c.execute(
                        "SELECT id, workflow_path FROM workflow_runs WHERE agent_id = ? AND status = 'open'",
                        (agent_id,),
                    ).fetchall()
                    for _rw in _rows_w:
                        try:
                            _steps_w = _dbw.workflow_steps_for_run(_c, int(_rw["id"]))
                            for _sk in ("not-gutted", "lint", "test"):
                                for _st in _steps_w:
                                    if _st["step_key"] == _sk and not _st["done"]:
                                        try:
                                            _c.execute(
                                                "UPDATE workflow_run_steps SET done = 1, done_at = ?, done_by = ? WHERE run_id = ? AND step_key = ? AND done = 0",
                                                (
                                                    _dbw._now_iso(),
                                                    agent_id,
                                                    int(_rw["id"]),
                                                    _sk,
                                                ),
                                            )
                                        except Exception:  # domain:degrade-silently - per-step auto-tick best-effort
                                            pass
                        except (
                            Exception
                        ):  # domain:degrade-silently - per-run auto-tick best-effort
                            pass
        except Exception:  # domain: degrade-silently - auto-tick best-effort
            pass
        if branch_mode:
            # Blob hygiene: fetched PR heads linger as unreachable objects
            # after the next reset; prune them so the shared tree does not
            # accumulate every citizen's history.  Best-effort in the full
            # sense: _git's timeout raises rather than returning a code,
            # so only an exception guard honors the contract.
            try:
                _trees_mod._git(tree, "gc", "--prune=now", "--quiet")
            except Exception:
                # domain: degrade-silently - retention hygiene is not a run
                # outcome; the audit row already reflects the suite result
                # and nothing serves stale content because of it.
                pass
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        try:
            _slots_mod._deregister_active(slot)
        except Exception:
            pass  # domain: degrade-silently - deregistration best-effort
        try:
            _slots_mod._ci_release_slot(slot)
        except Exception:
            # domain: degrade-silently - releasing a retired slot is best-effort
            pass
        # Legacy lock release for tests that still hold it â€” no-op normally
        if (
            _slots_mod._RUN_LOCK.locked()
        ):  # legacy: release test-held lock if any; always False in prod
            try:
                _slots_mod._RUN_LOCK.release()
            except (
                RuntimeError
            ):  # domain:degrade-silently - legacy test-held lock release; no-op in prod
                pass


def run_heartbeat_bench(
    *, buyer_id: int | None = None, reason: str = "heartbeat"
) -> dict:
    """Dispatch one quiet native benchmark for the anchor heartbeat and
    bless it when it qualifies. System-owned: the run rides _system (no
    per-agent cooldown or daily cap, the run_branch_ci_for_poller
    precedent); the default quiet gate still applies, so a busy pool
    yields a bounded labeled wait with honest quiet/contended attestation.
    Returns {outcome, run_event_id, decision}: outcome is blessed (the
    shared timer resets), held (the quality or drift gate refused — the
    run's numbers stay readable; a store buy auto-refunds via the caller),
    or infra (the harness itself failed — nothing blessed, nothing judged).
    Holds never raise; only infrastructure failures do. Row matching takes
    the newest post-dispatch row logged by agent 0 (unspoofable - citizen
    ids start at 1), native-shaped; anything else holds safely."""
    import db as _db
    import events as _events

    pre = _events.query_events(kind=_events.EVT_CI_DB_BENCH_RUN, limit=1)
    pre_max = int(pre[0]["id"]) if pre else 0
    run_checks(0, "system", "db_benchmark", quiet=None, _system=True)
    rows = _events.query_events(kind=_events.EVT_CI_DB_BENCH_RUN, limit=50)
    ours = None
    for row in rows:
        if int(row["id"]) <= pre_max:
            continue
        # Our own row only: citizen ids start at 1, so agent 0 is
        # unspoofable - a concurrent citizen native must never be blessed
        # with the heartbeat's (or buyer's) reason. Newest-first scan.
        if row.get("actor_agent_id") != 0:
            continue
        detail = row.get("detail") or {}
        if isinstance(detail, dict) and _db._bench_anchor._is_native_detail(detail):
            ours = row
            break
    if ours is None:
        return {
            "outcome": "infra",
            "run_event_id": None,
            "decision": "infra: dispatched bench left no fresh native ledger row",
        }
    run_event_id = int(ours["id"])
    decision = _db.bless_heartbeat_run(run_event_id, reason=reason, blessed_by=buyer_id)
    if decision.startswith("blessed:"):
        return {
            "outcome": "blessed",
            "run_event_id": run_event_id,
            "decision": decision,
        }
    return {"outcome": "held", "run_event_id": run_event_id, "decision": decision}


def run_branch_ci_for_poller(pr_number: int, checks: str = "tests") -> dict:
    """Poller-side branch CI â€” same Docker sandbox as repo_ci_run(branch)
    but without per-agent cooldown/cap. Used when GitHub Actions is
    unreachable and CI_FALLBACK_ENABLED=1 â€” either CI passing is sufficient
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
    if not _sandbox_mod._docker_available():
        raise db.ForumError(
            "the sandboxed CI runner needs docker on the server host; it is not installed or not on PATH"
        )
    kind_event = events.EVT_CI_BRANCH_RUN
    tmp_root = tempfile.mkdtemp(prefix="agentland_ci_poller_")
    started = time.monotonic()
    if _slots_mod._RUN_LOCK.locked():  # legacy: only set by tests; always False in prod â€” real gate is _ci_acquire_slot
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise db.ForumError(_slots_mod._BUSY_LEGACY_MSG)
    try:
        # Poller/ticker: reserve 1 slot for user, non-blocking skip
        slot = _slots_mod._ci_acquire_slot(reserve=True, timeout=None)
    except (
        db.ForumError
    ):  # domain: fail-loudly - busy error propagates after tmp cleanup
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    try:
        try:
            tree, head_sha, merge_info = _trees_mod._prepare_br_tree(pr_number)
        except TypeError:  # domain:degrade-silently - fallback for tests that monkeypatch with no slot arg
            tree, head_sha, merge_info = _trees_mod._prepare_pr_tree(pr_number)
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
            except (
                Exception
            ):  # domain:degrade-silently - conflict-return ledger write best-effort
                pass
            return payload
        image_tag = _sandbox_mod._ensure_image(tree, merge_info["base"])
        _sandbox_mod._ensure_tree_traversable(tree, head_sha)
        p_anchor_env: dict[str, str] = {}
        p_anchor_event_id: int | None = None
        if checks in _BENCH_CHECKS:
            # Poller fallback benches arm the same anchor gate as user runs.
            p_anchor_env, p_anchor_event_id = _bench_anchor_env()
        argv, container_name = _sandbox_mod._sandbox_argv(
            tree, image_tag, script_rel, extra_env=p_anchor_env
        )
        _cpus_val = _slots_mod._cpus_from_argv(argv)
        try:
            _slots_mod._register_active(slot, container_name, _cpus_val)
        except Exception:
            pass  # domain: degrade-silently - registration best-effort
        env = _child_env(tmp_root)
        pieces = _sandbox_mod._execute(
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
            "tree_warm": bool(merge_info.get("tree_warm")),
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
            "tree_warm": bool(merge_info.get("tree_warm")),
            "poller_triggered": True,
        }
        if p_anchor_event_id is not None:
            detail["anchor_event_id"] = p_anchor_event_id
        detail = _ci_detail_with_output(detail, pieces)
        try:
            events.log_event(
                kind_event, actor_agent_id=None, actor_name="poller", detail=detail
            )
        except Exception:  # domain:degrade-silently - ledger write best-effort
            pass
        try:
            _trees_mod._git(tree, "gc", "--prune=now", "--quiet")
        except Exception:  # domain:degrade-silently - blob hygiene best-effort
            pass
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        try:
            _slots_mod._deregister_active(slot)
        except Exception:
            pass  # domain: degrade-silently - deregistration best-effort
        try:
            _slots_mod._ci_release_slot(slot)
        except (
            Exception
        ):  # domain:degrade-silently - releasing a retired slot is best-effort
            pass
        if (
            _slots_mod._RUN_LOCK.locked()
        ):  # legacy: release test-held lock if any; always False in prod
            try:
                _slots_mod._RUN_LOCK.release()
            except (
                RuntimeError
            ):  # domain:degrade-silently - legacy test-held lock release; no-op in prod
                pass
