"""server.ci_runner._runs — gating, single-flight, and run orchestration."""

from __future__ import annotations

import os
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


def _ci_detail_with_output(detail: dict, pieces: dict) -> dict:
    """Fold a finished run's output into its ci_* ledger detail so a red
    run is diagnosable from the events ledger even when the caller's MCP
    transport dropped the response. The tool response's tail was already
    capped upstream by CI_RUN_TAIL_BYTES; the LEDGER copy keeps only the
    last CI_RUN_EVENT_TAIL_BYTES bytes of that tail (0 keeps the whole
    caller tail), byte-exact like the caller-facing capper, so one ci_*
    event detail stays on a few SQLite pages instead of spilling across
    dozens of overflow pages."""
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


def _gate(kind_event: str, agent_id: int) -> None:
    if not config.CI_RUN_ENABLED:
        raise db.ForumError("the server-side CI runner is disabled")
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
                f"{max_inflight}); wait for its ci_* ledger event "
                "(list_events) or the /ci page - a -32001 timeout means "
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


def run_checks_with_deadline(
    soft_seconds: int,
    agent_id: int,
    name: str,
    checks: str,
    pr_number: int | None = None,
    files: list[dict] | None = None,
    tree: str | None = None,
) -> tuple[dict | None, bool, str]:
    """User-facing repo_ci_run path: run run_checks(...) but respond to the
    caller after `soft_seconds` when the run is still going, so an MCP
    client's ~60s read timeout (FORUM_CI_RUN_RESPOND_SECONDS, default 50)
    cannot cut the call before any result arrives.

    Returns (result, handed_off, started_at): handed_off False means `result`
    is the full run outcome (or the call raised the run's immediate error);
    True means the run continues in a daemon worker thread and its ledger
    event + workflow auto-tick land on completion even if the client is gone -
    correlate with (ledger_kind, agent, created_at >= started_at). The
    single-flight registry (FORUM_CI_RUN_MAX_INFLIGHT) is claimed here for
    the caller; the poller fallback path never reaches this wrapper."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    kind = ledger_kind_for(checks, pr_number, files, tree)
    token = uuid.uuid4().hex
    _inflight_claim(agent_id, kind, checks, started_at, token)
    result_holder: list[dict] = []
    exc_holder: list[BaseException] = []
    done = threading.Event()

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
                )
            )
        except Exception as exc:
            # domain: fail-loudly - captured for the caller, not swallowed;
            # re-raised within the deadline, logged by run_checks on the
            # poller path (which audits before raising here or records
            # ci_failure_poll on the ledger in branch mode).
            exc_holder.append(exc)
        finally:
            _inflight_release(agent_id, token)
            done.set()

    thread = threading.Thread(target=_worker, name="ci-early-handoff", daemon=True)
    thread.start()
    if done.wait(timeout=max(0, int(soft_seconds))):
        if exc_holder:
            raise exc_holder[0]
        return result_holder[0], False, started_at
    return None, True, started_at


def run_checks(
    agent_id: int,
    name: str,
    checks: str,
    pr_number: int | None = None,
    files: list[dict] | None = None,
    tree: str | None = None,
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
    _gate(kind_event, agent_id)
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
                tree, image_tag, script_rel
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
            image_tag = _sandbox_mod._ensure_image(tree, merge_info["base"])
            _sandbox_mod._ensure_tree_traversable(tree, head_sha)
            argv, container_name = _sandbox_mod._sandbox_argv(
                tree, image_tag, script_rel
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
                    tree, image_tag, script_rel
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
        detail = _ci_detail_with_output(detail, pieces)
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
        argv, container_name = _sandbox_mod._sandbox_argv(tree, image_tag, script_rel)
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
