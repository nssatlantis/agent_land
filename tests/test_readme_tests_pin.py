"""Pin the README tests/ row to the runner's real skip set.

PR #1427 introduced a false claim ("all `test_*.py` modules") in the README
tree. run_all.py actually skips the four test_e2e_0*.py end-to-end suites and
test_benchmark.py. This pin keeps the README row honest: it refuses the
over-claim and re-verifies the runner still carries the names the row cites.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_NAMES = {
    "test_e2e_01_forum.py",
    "test_e2e_02_governance.py",
    "test_e2e_03_prs.py",
    "test_e2e_04_collab_viewer.py",
    "test_benchmark.py",
}


def _tests_row(readme):
    lines = readme.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("tests/ "):
            continue
        row = [line]
        j = i + 1
        while j < len(lines) and lines[j].startswith(" "):
            row.append(lines[j])
            j += 1
        return "\n".join(row)
    return ""


def main():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    run_all = (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8")

    for name in sorted(SKIP_NAMES):
        assert name in run_all, f"run_all.py no longer skips {name}"
    row = _tests_row(readme)
    assert row, "README tree has no tests/ package row"
    assert "all `test_*.py`" not in row, "README row over-claims all tests"
    assert "test_e2e_0*.py" in row, "README row must cite the e2e skip family"
    assert "test_benchmark.py" in row, "README row must cite the bench skip"

    print("README tests/ row is consistent with the run_all.py skip set")


if __name__ == "__main__":
    main()
