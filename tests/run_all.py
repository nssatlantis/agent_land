"""Run all test_*.py files in this directory as subprocesses.

Usage: python tests/run_all.py
"""
import glob
import os
import subprocess
import sys


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tests = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "test_*.py")))
    # test_client.py needs a live server (use run_e2e.py instead); skip it here.
    tests = [t for t in tests if os.path.basename(t) != "test_client.py"]
    if not tests:
        print("no test_*.py files found"); sys.exit(1)
    failures = []
    for path in tests:
        name = os.path.basename(path)
        print(f"== {name} ==")
        result = subprocess.run(
            [sys.executable, path],
            cwd=repo,
            timeout=120,
        )
        if result.returncode != 0:
            failures.append(name)
    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        sys.exit(1)
    print(f"\nall {len(tests)} test files passed")


if __name__ == "__main__":
    main()
