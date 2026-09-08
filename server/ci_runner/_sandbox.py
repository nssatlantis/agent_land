"""server.ci_runner._sandbox — image build, argv, process execution."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

import config
import db
import server.ci_runner._slots as _slots_mod
import server.ci_runner._trees as _trees_mod


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


_STATIC_SUMMARY_RE = re.compile(
    r"^STATIC SUMMARY: compileall=(\w+) mypy=(-?\d+) ruff_check=(-?\d+) "
    r"ruff_format=(-?\d+) bash_n=(\w+)$",
    re.M,
)


def _parse_static_summary(output: str) -> dict | None:
    """Parse the combined harness's static-checks marker (tests/run_ci.py).
    Returns None when the marker is absent (not a combined run).  Best-effort
    enrichment - the harness's exit code is the authoritative pass/fail."""
    m = _STATIC_SUMMARY_RE.search(output)
    if m is None:
        return None
    if "STATIC RESULT: PASS" in output:
        result = "pass"
    elif "STATIC RESULT: FAIL" in output:
        result = "fail"
    elif "STATIC RESULT: SKIPPED" in output:
        result = "skipped"
    else:
        result = "unknown"
    return {
        "result": result,
        "compileall": m.group(1),
        "mypy_errors": int(m.group(2)),
        "ruff_check_errors": int(m.group(3)),
        "ruff_format_files": int(m.group(4)),
        "bash_n": m.group(5),
    }


def _parse_summary(output: str) -> tuple[dict | None, list[str]]:
    # run_all.py prints bare basenames ("FAILED: test_x.py"); prefix them
    # so failed_files entries are copy-pasteable paths from the repo root.
    raw = re.findall(r"^FAILED: (\S+)$", output, re.M)
    failed_files = [
        name if "/" in name or not name.endswith(".py") else "tests/" + name
        for name in raw
    ]
    summary: dict | None = None
    ok_all = re.search(r"all (\d+) test files passed", output)
    failed = re.search(r"FAILED: (\d+) of (\d+) test files", output)
    if ok_all:
        summary = {"passed_files": int(ok_all.group(1)), "failed_files": 0}
    elif failed:
        summary = {
            "passed_files": int(failed.group(2)) - int(failed.group(1)),
            "failed_files": int(failed.group(1)),
        }
    # db_benchmark (tests/test_benchmark.py) â€” compact high-signal summary
    # Most info / least text: parse the timing table medians + regression
    # marker, so callers get a one-object summary without scanning the tail.
    if summary is None and "[Timing -" in output:
        try:
            timings: dict[str, float] = {}
            for m in re.finditer(
                r"^\s{2}(\w+)\s+[\d.]+ / +([\d.]+) / +[\d.]+", output, re.M
            ):
                label = m.group(1)
                try:
                    timings[label] = float(m.group(2))
                except ValueError:
                    pass  # domain:degrade-silently - malformed timing line, skip
            reg_m = re.search(r"REGRESSIONS DETECTED:\s*(\d+)", output)
            regressions = int(reg_m.group(1)) if reg_m else 0
            ok_bench = (
                "All checks passed." in output
                and regressions == 0
                and "FAIL" not in output.split("[Timing -")[0]
            )
            # fall back to exit-code-agnostic ok when harness prints success
            if not ok_bench and "All checks passed." in output and regressions == 0:
                ok_bench = True
            summary = {
                "bench": "db_benchmark",
                "regressions": regressions,
                "timings_median_ms": timings,
            }
            # preserve failed_files shape for db_bench structural failures
            if not ok_bench and not failed_files:
                # surface structural FAIL lines as pseudo failed_files for visibility
                struct_fails = re.findall(r"^\s{2}(.+?)\s+FAIL", output, re.M)
                failed_files = sorted(set(s.strip() for s in struct_fails))[:5]
        except Exception:
            # domain:degrade-silently - bench summary parse is advisory; tail still carries raw
            pass
    static_summary = _parse_static_summary(output)
    if static_summary is not None:
        if summary is None:
            summary = {}
        summary["static"] = static_summary
    return summary, sorted(set(failed_files))


def _requirements_at(tree: str, rev: str) -> bytes:
    """Read requirements.txt AS OF a specific commit.  Branch mode always
    passes origin/main's sha here: the dependency image must be derived
    from trusted main's pinned set, never from the merge result - a PR
    that edits requirements.txt must not control what a host-side
    ``docker build`` pip-installs."""
    res = _trees_mod._git(tree, "show", f"{rev}:requirements.txt")
    if res.returncode != 0:
        raise db.ForumError(
            f"could not read requirements.txt at {rev[:12]}: "
            f"{(res.stderr or res.stdout).strip()[-200:]}"
        )
    return res.stdout.encode("utf-8", errors="replace")


def _requirements_dev_at(tree: str, rev: str) -> bytes:
    """Read requirements-dev.txt AS OF a specific commit.  Same trust rule as
    _requirements_at: the static tooling (mypy/ruff/...) pinned by main's dev
    requirements is what gets baked into the sandbox image, never the merge
    result - a PR must not choose what a host-side build installs.  Absent at
    a commit too old to carry it => empty bytes (no static tooling baked)."""
    res = _trees_mod._git(tree, "show", f"{rev}:requirements-dev.txt")
    if res.returncode != 0:
        return b""
    return res.stdout.encode("utf-8", errors="replace")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _image_tag(digest_hex: str) -> str:
    return f"{config.CI_RUN_IMAGE_BASE}:{digest_hex}"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


# Traversability memo: (tree, content-sha) pairs already chmodded.
# Bounded best-effort cache — a miss only costs the find walks.
_TRAVERSABLE_CACHE: dict[tuple[str, str], bool] = {}
_TRAVERSABLE_LOCK = threading.Lock()


def _ensure_tree_traversable(tree: str, marker: str | None = None) -> None:
    """The sandbox reads the mounted tree as uid 1000 while the host-side
    owner may be anyone (e.g. a 1001 service account with a restrictive
    umask, which denies traversal outright).  Best-effort readability for
    the tracked content only: ``.git`` is pruned from the pass on purpose,
    so fetched PR blobs are not widened on the host.  Repo files are
    public content; their world-readability persisting afterwards is
    intentional and harmless.

    `marker` (the tree's content sha at the call site) skips repeat walks:
    runner trees are reused per slot, so the same (tree, sha) pair means
    byte-identical content that was already chmodded — the two find walks
    (dirs+files over ~3k files, 100-400ms) drop to a dict hit.  A new sha
    always re-runs.  Best-effort cache (bounded, lock-guarded); a miss or
    a cleared entry only costs the walks, never correctness."""
    if os.name != "posix":
        return
    if marker is not None:
        with _TRAVERSABLE_LOCK:
            if _TRAVERSABLE_CACHE.get((tree, marker)):
                return
    dirs = [
        "find",
        tree,
        "-name",
        ".git",
        "-prune",
        "-o",
        "-type",
        "d",
        "-exec",
        "chmod",
        "a+rx",
        "{}",
        "+",
    ]
    files = [
        "find",
        tree,
        "-name",
        ".git",
        "-prune",
        "-o",
        "-type",
        "f",
        "-exec",
        "chmod",
        "a+r",
        "{}",
        "+",
    ]
    for cmd in (dirs, files):
        try:
            subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception:
            # domain: degrade-silently - trees already world-readable (the
            # common root-owned deployment) need nothing here anyway.
            pass
    if marker is not None:
        with _TRAVERSABLE_LOCK:
            if len(_TRAVERSABLE_CACHE) > 64:
                _TRAVERSABLE_CACHE.clear()
            _TRAVERSABLE_CACHE[(tree, marker)] = True


def _prune_stale_images(keep_tag: str) -> None:
    """Housekeeping: the dependency set changes rarely, but every change
    leaves a slim image behind; drop our prefix's other tags so they do
    not accumulate on the host."""
    prefix = config.CI_RUN_IMAGE_BASE + ":"
    try:
        ls = subprocess.run(
            [
                "docker",
                "image",
                "ls",
                "--format",
                "{{.Repository}}:{{.Tag}}",
                "--filter",
                f"reference={config.CI_RUN_IMAGE_BASE}:*",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if ls.returncode != 0:
            # domain: degrade-silently - listing is housekeeping; stale
            # tags simply survive until a later build prunes them.
            return
        tags = [
            line.strip()
            for line in ls.stdout.splitlines()
            if line.strip()
            and line.strip() != keep_tag
            and line.strip().startswith(prefix)
        ]
        if tags:
            subprocess.run(
                ["docker", "rmi", "-f", *tags],
                capture_output=True,
                text=True,
                timeout=120,
            )
    except Exception:
        # domain: degrade-silently - image GC must never fail a run.
        pass


def _ensure_image(tree: str, rev: str) -> str:
    """Return a tag whose image contains exactly the pinned dependencies of
    *rev* - branch mode always passes origin/main's sha, never the merge
    result, so an untrusted PR cannot choose what this host-side build
    installs.  Builds from a minimal context (the two requirements files +
    the deployment's own Dockerfile) so repository code is never sent to
    the daemon.  The tag hashes BOTH requirements.txt and requirements-dev.txt,
    so a change to either invalidates the image (a dev-tools bump must not
    hide behind an unchanged runtime tag)."""
    data = _requirements_at(tree, rev)
    dev = _requirements_dev_at(tree, rev)
    tag = _image_tag(_digest(data + b"\x00" + dev))
    probe = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        timeout=60,
    )
    if probe.returncode == 0:
        return tag
    context = tempfile.mkdtemp(prefix="agentland_ci_img_")
    try:
        with open(os.path.join(context, "requirements.txt"), "wb") as fh:
            fh.write(data)
        with open(os.path.join(context, "requirements-dev.txt"), "wb") as fh:
            fh.write(dev)
        dockerfile = os.path.join(
            # Two levels up: this module lives in server/ci_runner/, while
            # the Dockerfile sits at the repo root (the old flat module
            # needed only one pardir from server/).
            os.path.dirname(os.path.abspath(__file__)),
            os.pardir,
            os.pardir,
            "Dockerfile",
        )
        shutil.copyfile(dockerfile, os.path.join(context, "Dockerfile"))
        build = subprocess.run(
            ["docker", "build", "-t", tag, context],
            capture_output=True,
            text=True,
            timeout=config.CI_RUN_BUILD_TIMEOUT,
        )
        if build.returncode != 0:
            raise db.ForumError(
                f"sandbox image build failed: "
                f"{(build.stderr or build.stdout).strip()[-300:]}"
            )
        _prune_stale_images(tag)
        return tag
    finally:
        shutil.rmtree(context, ignore_errors=True)


def _sandbox_argv(tree: str, image_tag: str, script_rel: str) -> tuple[list[str], str]:
    """Build the docker run argv for one sandboxed suite execution.
    Returns (argv, container_name) - the name lets the timeout path stop
    the container even though the killed client detaches from it."""
    name = f"agentland-ci-{uuid.uuid4().hex[:12]}"
    # Busy-aware: ceil (2.5) alone, host/busy when contended â€” live-throttled via docker update
    try:
        cpus = _slots_mod._effective_cpus()
    except Exception:
        cpus = float(config.CI_RUN_SANDBOX_CPUS)  # domain: degrade-silently
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "1000:1000",
        "--cpus",
        str(cpus),
        "--memory",
        f"{config.CI_RUN_SANDBOX_MEMORY_MB}m",
        # memory-swap = memory + swap extra; 256M swap lets a brief peak spill to swap
        # instead of OOM-killing, while still bounding total host pressure (2 slots Ã— 1G).
        "--memory-swap",
        f"{config.CI_RUN_SANDBOX_MEMORY_MB + config.CI_RUN_SANDBOX_SWAP_MB}m",
        "--pids-limit",
        str(config.CI_RUN_SANDBOX_PIDS),
        "--tmpfs",
        f"/tmp:rw,size={config.CI_RUN_SANDBOX_TMP_SIZE_MB * 1024 * 1024}",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "HOME=/tmp",
        # git >=2.35 guards repos owned by a different uid; the mounted tree
        # is host-owned while the container runs as 1000:1000, so trust /repo
        # explicitly or git-derived record enrichment degrades to nothing.
        "--env",
        "GIT_CONFIG_COUNT=1",
        "--env",
        "GIT_CONFIG_KEY_0=safe.directory",
        "--env",
        "GIT_CONFIG_VALUE_0=/repo",
        "--volume",
        f"{tree}:/repo:ro",
        "--workdir",
        "/repo",
        image_tag,
        "python3",
        script_rel,
    ]
    return argv, name


def _stop_sandbox(name: str) -> None:
    """Best-effort container stop when the client is killed on timeout -
    a detached --rm container would otherwise keep burning its cgroup."""
    subprocess.run(
        ["docker", "kill", name],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _drain(pipe, chunks: list, start_holder: dict, retain: int, state: dict) -> None:
    """Read the child's merged stdout/stderr in chunks so a hostile suite
    cannot balloon host memory through the pipe buffer.  At most *retain*
    bytes are retained (the contiguous tail), while state['total'] counts
    everything that ever flowed.

    Storage is a list of byte chunks plus a front offset - appending is
    O(chunk) and eviction moves list pointers only, never payload bytes.
    A bytearray with prefix deletion would memmove the whole retained
    window on every chunk (for a 1GB stream at 64KB reads that is ~1TB of
    memory copying); this shape does not."""
    total = 0
    kept = 0
    start = 0  # bytes already logically dropped from chunks[0]
    while True:
        try:
            chunk = pipe.read(65536)
        except (
            OSError,
            ValueError,
        ):  # domain: degrade-silently - output pipe died; keep what was captured
            break
        if not chunk:
            break
        total += len(chunk)
        chunks.append(chunk)
        kept += len(chunk)
        # Trim from the front once over budget; the partial cut lands
        # inside chunks[0], so the tail stays contiguous.
        while kept > retain and len(chunks) > 1:
            avail = len(chunks[0]) - start
            cut = min(avail, kept - retain)
            start += cut
            kept -= cut
            if start == len(chunks[0]):
                chunks.pop(0)
                start = 0
    state["total"] = total
    state["start"] = start


def _execute(
    argv: list[str],
    tree: str,
    timeout: int,
    tail_cap: int,
    max_retained: int,
    env: dict | None = None,
    container_name: str | None = None,
) -> dict:
    started = time.monotonic()
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        argv,
        cwd=tree,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **popen_kwargs,
    )
    chunks: list = []
    state: dict = {"total": 0, "start": 0}
    reader = threading.Thread(
        target=_drain,
        args=(proc.stdout, chunks, state, max_retained, state),
        daemon=True,
    )
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=max(timeout, 1))
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
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # domain: degrade-silently - an unreapable pid is left to init;
            # the pipe reader below still terminates at EOF or stays a
            # daemon thread that cannot block shutdown.
            pass
    reader.join(timeout=30)
    if reader.is_alive():
        # domain: degrade-silently - the drain thread dies with the process
        # rather than blocking the caller; partial output is still served.
        pass
    try:
        proc.stdout.close()  # type: ignore[union-attr]
    except Exception:
        # domain: degrade-silently - closing an already-dead pipe is
        # bookkeeping; nothing downstream depends on it succeeding.
        pass
    duration = round(time.monotonic() - started, 2)
    total = state.get("total", 0)
    truncated = total > tail_cap
    # Summary patterns are parsed over everything retained (a huge failing
    # run can scroll its "FAILED:" headers past a 16KB window); the tail
    # handed back to the caller is byte-exact against tail_cap.  Newlines
    # are normalized so CRLF-streaming children parse identically to LF.
    start = state.get("start", 0)
    parts = []
    for i, c in enumerate(chunks):
        parts.append(c[start:] if i == 0 else c)
        start = 0
    retained_bytes = b"".join(parts)
    retained_text = retained_bytes.decode("utf-8", errors="replace")
    retained_text = retained_text.replace("\r\n", "\n").replace("\r", "\n")
    tail = (
        retained_bytes[-tail_cap:].decode("utf-8", errors="replace")
        if truncated
        else retained_text
    )
    summary, failed_files = _parse_summary(retained_text)
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
