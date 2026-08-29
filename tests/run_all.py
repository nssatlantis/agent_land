"""Run all test_*.py files in this directory as subprocesses.

Usage: python tests/run_all.py [--durations] [--session]

test_client.py is skipped (needs a live server — use run_e2e.py instead).
test_benchmark.py is skipped (seeds a large dataset for manual benchmarking).

Suites run in parallel (up to CPU-count workers). Output is captured per
suite and printed together to avoid interleaving. With --durations the
5 slowest suites are printed. With --session each worker shares one DB
file (D1, N workers = N files) instead of 60 mkdtemp DBs — each file
still truncates via _setup so isolation is preserved but mkdtemp/init_db
overhead is cut. Without --session each file gets its own mkdtemp (default).
"""

import glob
import os
import queue
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_SKIP = {"test_client.py", "test_benchmark.py"}


_SESSION_BLOCKLIST = {
    "test_pure.py",
    "test_migrations.py",
    "test_config.py",
    "test_bug_reports.py",
    "test_misc.py",
}


def _run_one(
    path: str, repo: str, session_q: queue.Queue | None = None
) -> tuple[str, bool, str, float]:
    name = os.path.basename(path)
    start = time.perf_counter()
    extra = None
    sess_tmp = None
    # Session mode: share per-worker DB, but blocklisted tests need
    # per-file isolation (they assert config.DB_PATH == per-file tmp)
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
    use_session = "--session" in sys.argv
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tests = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "test_*.py")))
    tests = [t for t in tests if os.path.basename(t) not in _SKIP]
    if not tests:
        print("no test_*.py files found")
        sys.exit(1)

    failures: list[tuple[str, str]] = []
    successes: list[str] = []
    durations: dict[str, float] = {}
    workers = min(len(tests), os.cpu_count() or 4)

    # D1 session DBs: one per worker when --session
    session_q: queue.Queue | None = None
    session_tmps: list[Path] = []
    if use_session:
        session_q = queue.Queue()
        for i in range(workers):
            tmp = Path(tempfile.mkdtemp(prefix=f"agentland_session_w{i}_"))
            db_path = str(tmp / "forum.db")
            # Pre-create schema so _truncate path works
            sys.path.insert(0, repo)
            try:
                # Force init for this worker's DB
                prev = os.environ.get("FORUM_DB_PATH")
                prev_data = os.environ.get("AGENTLAND_DATA_DIR")
                os.environ["FORUM_DB_PATH"] = db_path
                os.environ["AGENTLAND_DATA_DIR"] = str(tmp)
                import db as _db

                _db.init_db()
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
        total = sum(durations.values())
        print(
            f"Total wall (parallel {workers} workers): {total:.2f}s sum, max {max(durations.values()):.2f}s"
        )
        if use_session:
            print(f"Session DBs: {len(session_tmps)} workers, each truncated per file")

    # Cleanup session dirs
    for tmp in session_tmps:
        try:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    if failures:
        print(f"\nFAILED: {len(failures)} of {len(tests)} test files")
        sys.exit(1)
    print(f"\nall {len(tests)} test files passed")


if __name__ == "__main__":
    main()
