"""Run all test_*.py files in this directory as subprocesses.

Usage: python tests/run_all.py [--durations]

test_client.py is skipped (needs a live server — use run_e2e.py instead).
test_benchmark.py is skipped (seeds a large dataset for manual benchmarking).

Suites run in parallel (up to CPU-count workers). Output is captured per
suite and printed together to avoid interleaving. With --durations the
5 slowest suites are printed with wall time (M2)."""

import glob
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_SKIP = {"test_client.py", "test_benchmark.py"}


def _run_one(path: str, repo: str) -> tuple[str, bool, str, float]:
    name = os.path.basename(path)
    start = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, path],
            cwd=repo,
            timeout=120,
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
        elapsed = time.perf_counter() - start
        return name, result.returncode == 0, output, elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        return name, False, "TIMEOUT (120s)\n", elapsed


def main():
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

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_one, t, repo): t for t in tests}
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

    # M2: slowest 5 for observability
    if durations:
        slowest = sorted(durations.items(), key=lambda kv: kv[1], reverse=True)[:5]
        print("\nSlowest 5:")
        for name, sec in slowest:
            print(f"  {name}: {sec:.2f}s")
        total = sum(durations.values())
        print(
            f"Total wall (parallel {workers} workers): {total:.2f}s sum, max {max(durations.values()):.2f}s"
        )

    if failures:
        print(f"\nFAILED: {len(failures)} of {len(tests)} test files")
        sys.exit(1)
    print(f"\nall {len(tests)} test files passed")


if __name__ == "__main__":
    main()
