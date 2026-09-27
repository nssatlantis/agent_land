"""Ratchet: every tests/test_*.py file with module-level test defs must execute them.

tests/run_all.py spawns each file as a bare subprocess and scores only the
exit code (#672 item 5342, the vacuous-green class of #1038/#1041/#1069):
a file whose test defs are never invoked passes while asserting nothing.
This test AST-scans tests/ and fails naming every module-level test def the
file's `if __name__ == "__main__"` tail cannot reach. Reachability expands
the tail's call graph recursively through module-level defs with a visited
set - `main` itself is not a target, so a tail that merely calls `main()`
proves nothing until the expansion lands on real test defs (a bare
`def main(): pass` driver reaches none). Dynamic discovery (`globals()` /
`locals()` / `dir()` / `vars()` called anywhere in the tail's closure,
plus a call) still passes a file: thirty-one suites drive every test
through a globals() list driver, and that form genuinely executes them.
Files run_all skips (e2e suites + benchmark, own harnesses) are excluded,
mirroring tests/run_all.py:_SKIP by name.

Known debt lives in EXPECTED_UNWIRED_COUNTS (dated 2026-09-25): per-file
counts of unreached defs, enforced exactly. The map can only shrink -
wiring a test without shrinking it fails CI, and so does any growth.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

# Mirrors tests/run_all.py:_SKIP (files with their own harnesses, never the
# bare-subprocess path this ratchet guards). Keep in sync by hand.
SKIPPED_BY_RUN_ALL = frozenset(
    {
        "test_e2e_01_forum.py",
        "test_e2e_02_governance.py",
        "test_e2e_03_prs.py",
        "test_e2e_04_collab_viewer.py",
        "test_benchmark.py",
    }
)

_DYNAMIC_DISCOVERY = frozenset({"globals", "locals", "dir", "vars"})

# Pinned record of known entry-point debt at the per-test ratchet landing
# (2026-09-25, #672 item 5342, PR #1462): filename -> unreached test-def
# count. Enforced EXACTLY - growth fails CI, fixes must shrink this map in
# the same PR. Never extend it for new code; wire the tests instead.
EXPECTED_UNWIRED_COUNTS = {
    "test_viewer.py": 28,
    "test_credits.py": 3,
    "test_farm.py": 2,
    "test_bench_gate.py": 1,
    "test_economy.py": 1,
    "test_pr_vote.py": 1,
}


def _module_test_defs(tree: ast.Module) -> list:
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


def _module_defs(tree: ast.Module) -> dict:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _loaded_names(node: ast.AST) -> set:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _tail_closure(tree: ast.Module) -> list:
    """Tail node plus every module-level def it reaches by name.

    The `__main__` tail's loaded names expand transitively through
    module-level defs (visited set, order-free: defs may sit after the
    tail's `main()`). Names matching no module def are leaves.
    """
    defs = _module_defs(tree)
    nodes: list = []
    visited: set = set()
    frontier = [
        node
        for node in tree.body
        if isinstance(node, ast.If) and _is_main_guard(node.test)
    ]
    while frontier:
        cur = frontier.pop()
        nodes.append(cur)
        for name in _loaded_names(cur) - visited:
            visited.add(name)
            func = defs.get(name)
            if func is not None:
                frontier.append(func)
    return nodes


def _tail_reached_tests(tree: ast.Module, test_names: list) -> set:
    """Module-level test defs the `__main__` tail can reach.

    Dynamic discovery (`globals()` / `locals()` / `dir()` / `vars()`
    called anywhere in the tail's closure, plus a call) runs every test
    by construction. Otherwise a test is reached when its name loads
    anywhere in the closure. `main` is deliberately NOT a target: a tail
    that merely calls `main()` reaches nothing until the expansion lands
    on real test defs.
    """
    targets = set(test_names)
    closure = _tail_closure(tree)
    calls = False
    discovered = False
    for node in closure:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                calls = True
                func = child.func
                if isinstance(func, ast.Name) and func.id in _DYNAMIC_DISCOVERY:
                    discovered = True
    if discovered and calls:
        return set(test_names)
    reached: set = set()
    for node in closure:
        reached |= _loaded_names(node) & targets
    return reached


def test_all_test_files_execute_their_tests():
    actual: dict = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name in SKIPPED_BY_RUN_ALL:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = _module_test_defs(tree)
        unreached = sorted(set(names) - _tail_reached_tests(tree, names))
        if unreached:
            actual[path.name] = unreached
    detail = "; ".join(
        fname + ": " + ", ".join(actual[fname]) for fname in sorted(actual)
    )
    assert set(actual) == set(EXPECTED_UNWIRED_COUNTS), (
        "entry-point debt membership changed (#672 item 5342): actual=["
        + detail
        + "] allowlisted="
        + str(sorted(EXPECTED_UNWIRED_COUNTS))
        + ". Wire the tests (and shrink the allowlist in the same PR) "
        "instead of extending it."
    )
    for fname, expected in EXPECTED_UNWIRED_COUNTS.items():
        assert len(actual[fname]) == expected, (
            fname
            + ": "
            + str(len(actual[fname]))
            + " unreached, allowlist pins "
            + str(expected)
            + " (#672 item 5342): "
            + ", ".join(actual[fname])
            + ". Shrink the wiring AND the allowlist in the same PR."
        )


def main():
    test_all_test_files_execute_their_tests()
    print("test_entry_point_wiring: all assertions passed")


if __name__ == "__main__":
    main()
