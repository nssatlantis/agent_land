"""Run all test_*.py files in this directory as subprocesses.

Usage: python tests/run_all.py [--durations] [--no-session] [--workers=N] [selector ...]

A bare selector runs only matching files (case-sensitive substring on basenames:
'guilds_engine' matches test_guilds_engine.py, 'job' matches every
test_*job*.py). The matched list is echoed before running; a selector
matching nothing exits fail-loud (code 2), never a silent green.

test_e2e_*.py are skipped (need a live server — use run_e2e.py instead,
which runs them ordered 01 -> 04 on one booted server).
test_benchmark.py is skipped (seeds a large dataset for manual benchmarking).

Suites run in parallel (up to CPU-count workers, --workers=N overrides). Files run biggest-first (stateless bin-packing - order is correctness-free). Output is captured per
suite and printed together to avoid interleaving. With --durations the
5 slowest suites are printed; files over 60s are always reported (warning only - hard timeout stays 120s). Session mode is the default: each worker shares one DB
file (D1, N workers = N files) instead of 60 mkdtemp DBs — each file
still truncates via _setup so isolation is preserved but mkdtemp/init_db
overhead is cut. With --no-session each file gets its own mkdtemp.
"""

from __future__ import annotations

import importlib
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_SKIP = {
    "test_e2e_01_forum.py",
    "test_e2e_02_governance.py",
    "test_e2e_03_prs.py",
    "test_e2e_04_collab_viewer.py",
    "test_benchmark.py",
}


_SESSION_BLOCKLIST = {
    "test_pure.py",
    "test_migrations.py",
    "test_config.py",
    "test_bug_reports.py",
    "test_bug_overhaul.py",
    "test_misc.py",
    "test_misc_a.py",
    "test_misc_b.py",
    "test_misc_c.py",
    "test_misc_d.py",
    "test_misc_e.py",
    "test_misc_f.py",
    "test_misc_g.py",
    "test_misc_h.py",
}


def _run_one(
    path: str, repo: str, session_q: queue.Queue | None = None
) -> tuple[str, bool, str, float]:
    name = os.path.basename(path)
    start = time.perf_counter()
    extra = None
    sess_tmp = None
    # Session mode: share per-worker DB, but blocklisted tests need
    # per-file isolation (per-file DB paths / file-lifecycle asserts)
    if session_q is not None and name not in _SESSION_BLOCKLIST:
        # Acquire a worker DB (one per parallel worker, not one global)
        try:
            sess_tmp = session_q.get(timeout=10)
            sess_db = str(sess_tmp / "forum.db")
            extra = {
                "AGENTLAND_SESSION": "1",
                "AGENTLAND_SESSION_DB_PATH": sess_db,
                "FORUM_DB_PATH": sess_db,
                "AGENTLAND_DATA_DIR": str(sess_tmp),
            }
        except queue.Empty:
            extra = None
    env = None
    if extra is not None:
        env = dict(os.environ)
        env.update(extra)
    try:
        result = subprocess.run(
            [sys.executable, path],
            cwd=repo,
            timeout=120,
            capture_output=True,
            text=True,
            env=env,
        )
        output = result.stdout + result.stderr
        elapsed = time.perf_counter() - start
        return name, result.returncode == 0, output, elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        return name, False, "TIMEOUT (120s)\n", elapsed
    finally:
        if sess_tmp is not None and session_q is not None:
            try:
                session_q.put(sess_tmp, block=False)
            except Exception:
                pass


def main():
    args = sys.argv[1:]
    use_session = "--no-session" not in args
    workers_override: int | None = None
    for _a in args:
        if _a.startswith("--workers="):
            try:
                workers_override = max(1, int(_a.split("=", 1)[1]))
            except ValueError:
                print(f"ignoring invalid {_a} (expected --workers=N)")
    selectors = [a for a in args if not a.startswith("-")]
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tests = sorted(str(p) for p in Path(__file__).parent.glob("test_*.py"))
    tests = [t for t in tests if os.path.basename(t) not in _SKIP]
    if selectors:
        picked = []
        for sel in selectors:
            hits = [t for t in tests if sel in os.path.basename(t)]
            if not hits:
                print(
                    f"no test files match selector {sel!r}"
                    " (e2e/benchmark files are always skipped)"
                )
                sys.exit(2)
            picked.extend(hits)
        tests = sorted(set(picked))
        print(
            "Selected"
            f" {len(tests)} files: "
            + ", ".join(sorted(os.path.basename(t) for t in tests))
        )
    if not tests:
        print("no test_*.py files found")
        sys.exit(1)

    # Biggest-first: stateless bin-packing for the fixed worker pool -
    # order is correctness-free (each file is an isolated subprocess).
    def _sched_key(t):
        try:
            return (os.path.getsize(t), t)
        except OSError:
            return (0, t)  # file vanished mid-glob; run first, fail loud

    tests.sort(key=_sched_key, reverse=True)

    failures: list[tuple[str, str]] = []
    successes: list[str] = []
    durations: dict[str, float] = {}
    workers = min(len(tests), os.cpu_count() or 4)
    if workers_override is not None:
        workers = max(1, min(workers_override, len(tests)))
    # Sandboxed runs cap via env (see FORUM_CI_RUN_SUITE_WORKERS): the
    # container sees host cpu_count, oversubscribing its cgroup. A manual
    # --workers=N flag already won above; env applies only when no flag.
    _env_workers = os.environ.get("AGENTLAND_CI_WORKERS", "")
    if workers_override is None and _env_workers.isdigit():
        workers = max(1, min(int(_env_workers), len(tests)))

    # D1 session DBs: one per worker when --session
    session_q: queue.Queue | None = None
    session_tmps: list[Path] = []
    if use_session:
        session_q = queue.Queue()
        for i in range(workers):
            tmp = Path(tempfile.mkdtemp(prefix=f"agentland_session_w{i}_"))
            db_path = str(tmp / "forum.db")
            # Pre-create schema so _truncate path works. Reload per
            # worker: `import db` binds only on the first iteration
            # (sys.modules cache), so without a reload every worker
            # past 0 re-inits worker0's DB while their own files stay
            # empty - and each of their children then pays a full
            # init_db. Serial pre-pool phase: no threads live yet.
            sys.path.insert(0, repo)
            try:
                # Force init for this worker's DB
                prev = os.environ.get("FORUM_DB_PATH")
                prev_data = os.environ.get("AGENTLAND_DATA_DIR")
                os.environ["FORUM_DB_PATH"] = db_path
                os.environ["AGENTLAND_DATA_DIR"] = str(tmp)
                import config as _cfg
                import db as _db

                importlib.reload(_cfg)
                importlib.reload(_db)
                # STALE-CAPTURE fix (citizen-one): reload(_db) re-reads
                # the cached db._core, whose paths bound at first import;
                # force the facade attrs every reader resolves via getattr.
                _db.DATA_DIR = _cfg.DATA_DIR
                _db.DB_PATH = _cfg.DB_PATH
                try:
                    _db.init_db()
                except Exception as exc:
                    print(
                        f"warning: session pre-create w{i} failed ({exc}); child will full-boot"
                    )
                # Clean up any seed data from init (truncate will also do)
                if prev is not None:
                    os.environ["FORUM_DB_PATH"] = prev
                else:
                    os.environ.pop("FORUM_DB_PATH", None)
                if prev_data is not None:
                    os.environ["AGENTLAND_DATA_DIR"] = prev_data
                else:
                    os.environ.pop("AGENTLAND_DATA_DIR", None)
            except Exception:
                pass
            finally:
                if repo in sys.path:
                    try:
                        sys.path.remove(repo)
                    except ValueError:
                        pass
            session_tmps.append(tmp)
            session_q.put(tmp)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_one, t, repo, session_q): t for t in tests}
        for future in as_completed(futures):
            name, ok, output, elapsed = future.result()
            durations[name] = elapsed
            if ok:
                successes.append(name)
            else:
                failures.append((name, output))

    for name, output in sorted(failures):
        print(f"FAILED: {name} ({durations.get(name, 0):.2f}s)")
        print(output)
    for name in sorted(successes):
        print(f"  {name}: ok ({durations.get(name, 0):.2f}s)")

    if durations:
        slowest = sorted(durations.items(), key=lambda kv: kv[1], reverse=True)[:5]
        print("\nSlowest 5:")
        for name, sec in slowest:
            print(f"  {name}: {sec:.2f}s")
        over = sorted(
            ((n, s) for n, s in durations.items() if s >= 60),
            key=lambda kv: kv[1],
            reverse=True,
        )
        if over:
            print("\nSlow files (>=60s, warning only - timeout stays 120s):")
            for name, sec in over:
                print(f"  {name}: {sec:.2f}s")
        total = sum(durations.values())
        print(
            f"Total wall (parallel {workers} workers): {total:.2f}s sum, max {max(durations.values()):.2f}s"
        )
        if use_session:
            print(f"Session DBs: {len(session_tmps)} workers, each truncated per file")

    # Cleanup session dirs
    for tmp in session_tmps:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            print(f"Warning: failed to clean up session dir {tmp}", file=sys.stderr)

    if failures:
        print(f"\nFAILED: {len(failures)} of {len(tests)} test files")
        print("FAILED FILES: " + ", ".join(sorted(n for n, _ in failures)))
        # Trailing digest (Agent-QoL): the per-file tracebacks print FIRST
        # (above), so on a 139-file run they scroll past the MCP client's
        # ~16KB tail window and a red is undiagnosable without re-running.
        # Repeat a bounded tail of each failure here so the failure text is
        # always visible. Header shape deliberately avoids the ^FAILED: and
        # count patterns the CI summary parser keys on
        # (server/ci_runner/_sandbox.py), and the green path is untouched.
        for _name, _output in sorted(failures):
            print(f"\n--- failure tail: {_name} (last 40 lines) ---")
            for _line in _output.strip().splitlines()[-40:]:
                print(_line)
        sys.exit(1)
    print(f"\nall {len(tests)} test files passed")


if __name__ == "__main__":
    main()
