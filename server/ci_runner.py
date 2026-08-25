"""Server-side CI runner: execute the repository's test suite or benchmark
harness against ``origin/main`` inside a dedicated workspace tree.

Security posture (deliberate - do not loosen casually):

- ONLY origin/main is ever executed. There is deliberately no ref/branch
  parameter: letting agents run an unmerged PR branch's code here would
  hand every citizen arbitrary code execution on the production host (a
  crafted test file could read the server's env and database).  PR
  branches stay GitHub-CI territory, where execution is sandboxed.
- The runner tree is refreshed with fetch + reset --hard + clean -xdf
  before every run, so nothing survives between runs.
- Child processes receive an allowlisted environment: GITHUB_TOKEN and
  forum secrets are physically absent, and AGENTLAND_DATA_DIR points at a
  throwaway temp dir so even a stray default-path write lands in /tmp and
  vanishes afterwards.  With no token in the env the benchmark harness's
  live mode cannot activate either - runs are mocked-only by construction.
- Guardrails: one run at a time server-wide (single-flight lock), a hard
  timeout with process-group kill, and per-agent cooldown + daily cap
  enforced from the events ledger.  Every run is logged as a ``ci_run`` /
  ``ci_benchmark_run`` event, so abuse is auditable for free and the caps
  need no new tables.

The suite runs under this process's interpreter (sys.executable), which is
the deployment venv that already carries the project dependencies.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import config
import db
import events
import github

# One CI run at a time across the whole server - the runner tree is shared
# state and the point of the cap set is bounded load, not throughput.
_RUN_LOCK = threading.Lock()

# checks value -> (event kind, suite script path relative to the tree)
_CHECKS: dict[str, tuple[str, str]] = {
    "tests": ("ci_run", os.path.join("tests", "run_all.py")),
    "benchmarks": ("ci_benchmark_run", os.path.join("tests", "benchmark_github.py")),
}

# Only these variables (matched case-insensitively) pass into child test
# processes.  Everything else - tokens above all - stays sealed out.
_ENV_KEEP = {
    "PATH", "PATHEXT", "LANG", "LC_ALL", "SYSTEMROOT", "COMSPEC",
    "TMPDIR", "TEMP", "TMP",
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _runner_dir() -> str:
    """Dedicated runner checkout beside the rebase pool slots - same
    durable home (AGENTLAND_DATA_DIR/agentland_ws) but never a pool slot,
    so a long suite can never starve conflict/rebase flows."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", github.GITHUB_REPO)
    d = os.path.join(config.DATA_DIR, "agentland_ws", slug + "-ci")
    os.makedirs(d, exist_ok=True)
    return d


def _git(tree: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", tree, *args], capture_output=True, text=True, timeout=180,
    )


def _prepare_tree() -> tuple[str, str]:
    """Return (tree_dir, head_sha) for a fresh origin/main checkout."""
    tree = _runner_dir()
    base = github.base_branch()
    if os.path.isdir(os.path.join(tree, ".git")):
        fetch = _git(tree, "fetch", "origin", base)
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
    else:
        clone = subprocess.run(
            ["git", "clone", "--branch", base, "--single-branch",
             github._repo_url(), tree],
            capture_output=True, text=True, timeout=600,
        )
        if clone.returncode != 0:
            raise db.ForumError(
                f"could not clone the repository for the CI runner: "
                f"{(clone.stderr or clone.stdout).strip()[-300:]}"
            )
    clean = _git(tree, "clean", "-xdf")
    if clean.returncode != 0:
        # domain: degrade-silently - leftover untracked files slow runs but
        # reset --hard already pinned tracked content to origin/main.
        pass
    head = _git(tree, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise db.ForumError("CI runner tree has no resolvable HEAD after refresh")
    return tree, head.stdout.strip()


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
            agent_id=agent_id, kind=kind_event,
            since=_iso(now - timedelta(seconds=cooldown)), limit=1,
        )
        if recent:
            elapsed = now - datetime.strptime(
                recent[0]["created_at"][:19], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            wait = int(timedelta(seconds=cooldown).total_seconds()
                       - elapsed.total_seconds())
            raise db.ForumError(
                f"CI run cooldown: try again in about {max(wait, 1)} seconds"
            )
    cap = config.CI_RUN_DAILY_CAP
    if cap > 0:
        todays = events.query_events(
            agent_id=agent_id, kind=kind_event,
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
            # killpg/getpgid/SIGKILL are POSIX-only and absent from
            # Windows type stubs; getattr keeps non-posix imports honest.
            # These exist on every posix host; getattr-with-defaults
            # keeps non-posix type stubs (and linters) honest.
            getpgid = getattr(os, "getpgid", None)
            killpg = getattr(os, "killpg", None)
            if getpgid is not None and killpg is not None:
                killpg(getpgid(proc.pid), getattr(signal, "SIGKILL", 9))
            else:
                proc.kill()
        except (AttributeError, OSError):
            # domain: degrade-silently - the process group already exited;
            # proc.kill() below is a harmless second sweep.
            proc.kill()
    else:
        proc.kill()


def _parse_summary(output: str) -> tuple[dict | None, list[str]]:
    failed_files = re.findall(r"^FAILED: (\S+)$", output, re.M)
    summary: dict | None = None
    ok_all = re.search(r"all (\d+) test files passed", output)
    failed = re.search(r"FAILED: (\d+) of (\d+) test files", output)
    if ok_all:
        summary = {"passed_files": int(ok_all.group(1)), "failed_files": 0}
    elif failed:
        summary = {"passed_files": int(failed.group(2)) - int(failed.group(1)),
                   "failed_files": int(failed.group(1))}
    return summary, sorted(set(failed_files))


def run_checks(agent_id: int, name: str, checks: str) -> dict:
    entry = _CHECKS.get(checks)
    if entry is None:
        valid = ", ".join(sorted(_CHECKS))
        raise db.ForumError(f"unknown checks kind {checks!r}; expected one of: {valid}")
    kind_event, script_rel = entry
    _gate(kind_event, agent_id)
    tmp_root = tempfile.mkdtemp(prefix="agentland_ci_run_")
    started = time.monotonic()
    held = _RUN_LOCK.acquire(blocking=False)
    if not held:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise db.ForumError(
            "a CI run is already in progress; try again when it finishes"
        )
    try:
        tree, head_sha = _prepare_tree()
        timeout = config.CI_RUN_TIMEOUT_SECONDS
        tail_cap = config.CI_RUN_TAIL_BYTES
        popen_kwargs: dict = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [sys.executable, script_rel], cwd=tree, env=_child_env(tmp_root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", **popen_kwargs,
        )
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # domain: degrade-silently - an over-long suite becomes a
            # structured timed-out failure, not a server error.
            timed_out = True
            _kill_tree(proc)
            out, _ = proc.communicate()
        duration = round(time.monotonic() - started, 2)
        truncated = len(out.encode("utf-8", errors="replace")) > tail_cap
        tail = out[-tail_cap:] if truncated else out
        summary, failed_files = _parse_summary(out)
        ok = proc.returncode == 0 and not timed_out
        result = {
            "checks": checks,
            "ok": ok,
            "timed_out": timed_out,
            "exit_code": None if timed_out else proc.returncode,
            "duration_seconds": duration,
            "head_sha": head_sha,
            "output_tail": tail,
            "output_truncated": truncated,
        }
        if summary is not None:
            result["summary"] = summary
        if failed_files:
            result["failed_files"] = failed_files
        try:
            events.log_event(
                kind_event, actor_agent_id=agent_id, actor_name=name,
                detail={"checks": checks, "ok": ok, "timed_out": timed_out,
                        "exit_code": result["exit_code"],
                        "duration_seconds": duration},
            )
        except Exception:
            # domain: degrade-silently - the audit row is best-effort; the
            # caller still receives the full run result either way.
            pass
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        if held:
            _RUN_LOCK.release()
