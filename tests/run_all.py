"""Run all test_*.py files in this directory as subprocesses.

Usage: python tests/run_all.py

test_client.py is skipped (needs a live server — use run_e2e.py instead).
test_benchmark.py is skipped (seeds a large dataset for manual benchmarking).

Suites run in parallel (up to CPU-count workers). Output is captured per
suite and printed together to avoid interleaving."""
import glob
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

_SKIP = {"test_client.py", "test_benchmark.py"}


def _run_one(path: str, repo: str) -> tuple[str, bool, str]:
    name = os.path.basename(path)
    try:
        result = subprocess.run(
            [sys.executable, path],
            cwd=repo,
            timeout=120,
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
        return name, result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return name, False, "TIMEOUT (120s)\n"


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tests = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "test_*.py")))
    tests = [t for t in tests if os.path.basename(t) not in _SKIP]
    if not tests:
        print("no test_*.py files found")
        sys.exit(1)

    failures: list[tuple[str, str]] = []
    successes: list[str] = []
    workers = min(len(tests), os.cpu_count() or 4)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_one, t, repo): t for t in tests}
        for future in as_completed(futures):
            name, ok, output = future.result()
            if ok:
                successes.append(name)
            else:
                failures.append((name, output))

    for name, output in sorted(failures):
        print(f"FAILED: {name}")
        print(output)
    for name in sorted(successes):
        print(f"  {name}: ok")

    if failures:
        print(f"\nFAILED: {len(failures)} of {len(tests)} test files")
        sys.exit(1)
    print(f"\nall {len(tests)} test files passed")


if __name__ == "__main__":
    main()
