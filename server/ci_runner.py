"""Server-side CI runner: execute the repository's test suite or benchmark
harness against ``origin/main`` inside a dedicated workspace tree, and --
behind a mandatory container sandbox -- a pull request's merge-with-main
commit.

Security posture (deliberate - do not loosen casually):

- Native mode executes ONLY ``origin/main``: maintainer-blessed code that
  the server already runs itself.  No ref parameter exists.
- Branch mode (``pr_number=``) tests the MERGE of origin/main into the PR
  head - what GitHub CI actually tests - but only inside a Docker sandbox:
  network-off, read-only root filesystem, dropped capabilities, no-new-
  privileges, non-root uid, capped cpu/memory/pids, tmpfs scratch.  The
  repository tree is mounted read-only; all test writes go to tmpfs.
  Running unmerged PR code outside that boundary would hand any citizen
  arbitrary code execution on the production host, so the mode refuses
  loudly whenever docker is unavailable.  The dependency image bakes only
  requirements.txt from trusted main (content-hash-tagged); repository
  code never enters an image.
- Child processes in native mode receive an allowlisted environment:
  GITHUB_TOKEN and forum secrets are physically absent, and
  AGENTLAND_DATA_DIR points at a throwaway temp dir so even a stray
  default-path write lands in /tmp and vanishes afterwards.  With no
  token in the env the benchmark harness's live mode cannot activate
  either - runs are mocked-only by construction.
- Guardrails: one run at a time server-wide (single-flight lock), a hard
  timeout with process-group kill, and per-agent cooldown + daily cap
  enforced from the events ledger.  Every run is logged as a ``ci_run`` /
  ``ci_benchmark_run`` / ``ci_branch_run`` event, so abuse is auditable
  for free and the caps need no new tables.  Branch runs draw on their
  own ledger kind, giving them an independent budget.

The native suite runs under this process's interpreter (sys.executable),
which is the deployment venv that already carries the dependencies; the
sandboxed suite runs under the image's own python3.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import config
import db
import events
import github

# One CI run at a time across the whole server - the runner tree is shared
# state and the point of the cap set is bounded load, not throughput.
_RUN_LOCK = threading.Lock()

# checks value -> (native event kind, suite script path relative to the tree)
_CHECKS: dict[str, tuple[str, str]] = {
    "tests": ("ci_run", os.path.join("tests", "run_all.py")),
    "benchmarks": ("ci_benchmark_run", os.path.join("tests", "benchmark_github.py")),
}

# Only these variables (matched case-insensitively) pass into native child
# test processes.  Everything else - tokens above all - stays sealed out.
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


def _ensure_clone(tree: str) -> None:
    base = github.base_branch()
    if os.path.isdir(os.path.join(tree, ".git")):
        return
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
    # Merge-preview creates merge commits, which need a committer identity;
    # production hosts may have no global git config (see #382).
    github._seed_identity(tree)


def _refresh_main(tree: str) -> str:
    """Fetch and hard-reset onto origin/<base>; returns the main sha."""
    base = github.base_branch()
    fetch = _git(tree, "fetch", "--force", "origin", base)
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
    clean = _git(tree, "clean", "-xdf")
    if clean.returncode != 0:
        # domain: degrade-silently - leftover untracked files slow runs but
        # reset --hard already pinned tracked content to origin/main.
        pass
    head = _git(tree, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise db.ForumError("CI runner tree has no resolvable HEAD after refresh")
    return head.stdout.strip()


def _prepare_tree() -> tuple[str, str]:
    """Return (tree_dir, head_sha) for a fresh origin/main checkout."""
    tree = _runner_dir()
    _ensure_clone(tree)
    return tree, _refresh_main(tree)


def _prepare_pr_tree(pr_number: int) -> tuple[str, str, dict]:
    """Merge origin/main into the PR head inside the runner tree and return
    ``(tree, merge_commit_sha, merge_info)``.  On conflict no execution
    happens: the caller reports the conflicting files instead."""
    tree = _runner_dir()
    _ensure_clone(tree)
    pr_fetch = _git(tree, "fetch", "--force", "origin", f"pull/{pr_number}/head")
    if pr_fetch.returncode != 0:
        raise db.ForumError(
            f"could not fetch the head of pull request #{pr_number} "
            "(unknown PR, or its branch was deleted?): "
            f"{(pr_fetch.stderr or pr_fetch.stdout).strip()[-300:]}"
        )
    pr_sha = _git(tree, "rev-parse", "FETCH_HEAD").stdout.strip()
    main_sha = _refresh_main(tree)
    checkout = _git(tree, "checkout", "--detach", main_sha)
    if checkout.returncode != 0:
        raise db.ForumError(
            f"CI runner could not check out main for the merge preview: "
            f"{checkout.stderr.strip()[-300:]}"
        )
    merge = _git(tree, "merge", "--no-edit", pr_sha)
    if merge.returncode != 0:
        conflicted = [
            line.strip() for line in
            _git(tree, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            if line.strip()
        ]
        abort = _git(tree, "merge", "--abort")
        if abort.returncode != 0:
            # domain: degrade-silently - the next run's reset --hard heals
            # any half-merged state; nothing serves stale content meanwhile.
            pass
        return tree, main_sha, {"conflict": True, "files": conflicted}
    head = _git(tree, "rev-parse", "HEAD")
    return tree, head.stdout.strip(), {"conflict": False, "base": main_sha}


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
            # These exist on every posix host; getattr-with-defaults
            # keeps non-posix type stubs (and linters) honest.
            getpgid = getattr(os, "getpgid", None)
            killpg = getattr(os, "killpg", None)
            if getpgid is not None and killpg is not None:
                killpg(getpgid(proc.pid), getattr(signal, "SIGKILL", 9))
            else:
                proc.kill()
        except OSError:
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


def _requirements_digest(tree: str) -> str:
    with open(os.path.join(tree, "requirements.txt"), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:16]


def _image_tag(tree: str) -> str:
    return f"{config.CI_RUN_IMAGE_BASE}:{_requirements_digest(tree)}"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_tree_traversable(tree: str) -> None:
    """The sandbox reads the mounted tree as uid 1000 while the host-side
    owner may be anyone (e.g. a 1001 service account with a restrictive
    umask, which denies traversal outright). Best-effort a+rX keeps the
    mount readable regardless of who owns the tree."""
    if os.name != "posix":
        return
    try:
        subprocess.run(
            ["chmod", "-R", "a+rX", tree],
            capture_output=True, timeout=120,
        )
    except Exception:
        # domain: degrade-silently - trees already world-readable (the
        # common root-owned deployment) need nothing here anyway.
        pass


def _ensure_image(tree: str) -> str:
    """Return a tag whose image contains exactly main's pinned dependencies.
    Builds from a minimal context (requirements.txt + Dockerfile) so the
    repository tree is never sent to the daemon."""
    tag = _image_tag(tree)
    probe = subprocess.run(
        ["docker", "image", "inspect", tag], capture_output=True, timeout=60,
    )
    if probe.returncode == 0:
        return tag
    context = tempfile.mkdtemp(prefix="agentland_ci_img_")
    try:
        shutil.copyfile(
            os.path.join(tree, "requirements.txt"),
            os.path.join(context, "requirements.txt"),
        )
        dockerfile = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), os.pardir, "Dockerfile"
        )
        shutil.copyfile(dockerfile, os.path.join(context, "Dockerfile"))
        build = subprocess.run(
            ["docker", "build", "-t", tag, context],
            capture_output=True, text=True, timeout=900,
        )
        if build.returncode != 0:
            raise db.ForumError(
                f"sandbox image build failed: "
                f"{(build.stderr or build.stdout).strip()[-300:]}"
            )
        return tag
    finally:
        shutil.rmtree(context, ignore_errors=True)


def _sandbox_argv(tree: str, image_tag: str, script_rel: str) -> tuple[list[str], str]:
    """Build the docker run argv for one sandboxed suite execution.
    Returns (argv, container_name) - the name lets the timeout path stop
    the container even though the killed client detaches from it."""
    name = f"agentland-ci-{uuid.uuid4().hex[:12]}"
    argv = [
        "docker", "run", "--rm",
        "--name", name,
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "1000:1000",
        "--cpus", str(config.CI_RUN_SANDBOX_CPUS),
        "--memory", f"{config.CI_RUN_SANDBOX_MEMORY_MB}m",
        "--pids-limit", str(config.CI_RUN_SANDBOX_PIDS),
        "--tmpfs", f"/tmp:rw,size={config.CI_RUN_SANDBOX_TMP_SIZE_MB * 1024 * 1024}",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "HOME=/tmp",
        "--volume", f"{tree}:/repo:ro",
        "--workdir", "/repo",
        image_tag,
        "python3", script_rel,
    ]
    return argv, name


def _stop_sandbox(name: str) -> None:
    """Best-effort container stop when the client is killed on timeout -
    a detached --rm container would otherwise keep burning its cgroup."""
    subprocess.run(
        ["docker", "kill", name], capture_output=True, text=True, timeout=30,
    )


def _execute(
    argv: list[str], tree: str, timeout: int, tail_cap: int,
    env: dict | None = None, container_name: str | None = None,
) -> dict:
    started = time.monotonic()
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        argv, cwd=tree, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", **popen_kwargs,
    )
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # domain: degrade-silently - an over-long run becomes a structured
        # timed-out failure, not a server error.
        timed_out = True
        if container_name is not None:
            try:
                _stop_sandbox(container_name)
            except Exception:
                # domain: degrade-silently - the daemon still reaps the
                # container when its workload exits; the run is reported
                # as timed out either way.
                pass
        _kill_tree(proc)
        out, _ = proc.communicate()
    duration = round(time.monotonic() - started, 2)
    truncated = len(out.encode("utf-8", errors="replace")) > tail_cap
    tail = out[-tail_cap:] if truncated else out
    summary, failed_files = _parse_summary(out)
    result: dict = {
        "ok": proc.returncode == 0 and not timed_out,
        "timed_out": timed_out,
        "exit_code": None if timed_out else proc.returncode,
        "duration_seconds": duration,
        "output_tail": tail,
        "output_truncated": truncated,
    }
    if summary is not None:
        result["summary"] = summary
    if failed_files:
        result["failed_files"] = failed_files
    return result


def run_checks(agent_id: int, name: str, checks: str, pr_number: int | None = None) -> dict:
    entry = _CHECKS.get(checks)
    if entry is None:
        valid = ", ".join(sorted(_CHECKS))
        raise db.ForumError(f"unknown checks kind {checks!r}; expected one of: {valid}")
    script_rel = entry[1]
    branch_mode = pr_number is not None
    if branch_mode:
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
            raise db.ForumError("pr_number must be a positive integer")
        if not config.CI_RUN_BRANCH_ENABLED:
            raise db.ForumError("branch-mode CI runs are disabled on this server")
        if not _docker_available():
            raise db.ForumError(
                "the sandboxed CI runner needs docker on the server host; "
                "it is not installed or not on PATH"
            )
        kind_event = events.EVT_CI_BRANCH_RUN
    else:
        kind_event = entry[0]
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
        if branch_mode:
            assert pr_number is not None
            tree, head_sha, merge_info = _prepare_pr_tree(pr_number)
            if merge_info["conflict"]:
                duration = round(time.monotonic() - started, 2)
                payload = {
                    "checks": checks, "mode": "branch", "pr_number": pr_number,
                    "ok": False, "merge_conflict": True,
                    "conflict_files": merge_info["files"],
                    "base_sha": head_sha, "head_sha": head_sha,
                    "timed_out": False, "exit_code": None,
                    "duration_seconds": duration,
                    "output_tail": "", "output_truncated": False,
                }
                events.log_event(
                    kind_event, actor_agent_id=agent_id, actor_name=name,
                    detail={"checks": checks, "mode": "branch",
                            "merge_conflict": True, "pr_number": pr_number,
                            "duration_seconds": duration},
                )
                return payload
            image_tag = _ensure_image(tree)
            _ensure_tree_traversable(tree)
            argv, container_name = _sandbox_argv(tree, image_tag, script_rel)
            env = None
        else:
            tree, head_sha = _prepare_tree()
            argv = [sys.executable, script_rel]
            container_name = None
            env = _child_env(tmp_root)
        pieces = _execute(
            argv, tree, config.CI_RUN_TIMEOUT_SECONDS,
            config.CI_RUN_TAIL_BYTES, env=env, container_name=container_name,
        )
        result: dict = {"checks": checks, "mode": "branch" if branch_mode else "native"}
        if branch_mode:
            assert pr_number is not None
            result["pr_number"] = pr_number
            result["base_sha"] = (merge_info.get("base") or head_sha)
            result["merge_conflict"] = False
        result.update(pieces)
        result["head_sha"] = head_sha
        detail = {"checks": checks, "mode": result["mode"], "ok": pieces["ok"],
                  "timed_out": pieces["timed_out"],
                  "exit_code": pieces["exit_code"],
                  "duration_seconds": pieces["duration_seconds"]}
        if branch_mode:
            detail["pr_number"] = pr_number
        try:
            events.log_event(kind_event, actor_agent_id=agent_id, actor_name=name,
                             detail=detail)
        except Exception:
            # domain: degrade-silently - the audit row is best-effort; the
            # caller still receives the full run result either way.
            pass
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        if held:
            _RUN_LOCK.release()
