"""Pin the README tests/ tree row to the runner's real skip set.

PR #1427 introduced a false claim ("all `test_*.py` modules") in the README
tree. run_all.py actually skips the four test_e2e_0*.py end-to-end suites and
test_benchmark.py. This prose-consistency pin keeps the README row honest: it
refuses the over-claim and re-verifies the runner still carries the exact
names the row calls out.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_NAMES = {
    "test_e2e_01_forum.py",
    "test_e2e_02_governance.py",
    "test_e2e_03_prs.py",
    "test_e2e_04_collab_viewer.py",
    "test_benchmark.py",
}


def main():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    run_all = (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8")

    for name in sorted(SKIP_NAMES):
        assert name in run_all, f"run_all.py no longer skips {name}"

    lines = readme.splitlines()
    idx = None
    for i, line in enumerate(lines):
        if line.startswith("tests/ "):
            idx = i
            break
    assert idx is not None, "README tree has no tests/ package row"

    row_lines = [lines[idx]]
    j = idx + 1
    while j < len(lines) and lines[j].startswith(" "):
        row_lines.append(lines[j])
        j += 1
    row = "\n".join(row_lines)

    assert "all `test_*.py`" not in row, (
        "README tests/ row over-claims: the runner skips the e2e suites "
        "and test_benchmark.py"
    )
    assert "test_e2e_0*.py" in row, (
        "README tests/ row no longer names the test_e2e_0* skip family"
    )
    assert "test_benchmark.py" in row, (
        "README tests/ row no longer names test_benchmark.py"
    )

    print("README tests/ row prose is consistent with run_all.py _SKIP")


if __name__ == "__main__":
    main()
