"""Ratchet: every tests/test_*.py file with module-level test defs must execute them.

tests/run_all.py spawns each file as a bare subprocess and scores only the
exit code (#672 item 5342, the vacuous-green class of #1038/#1041/#1069):
a file whose test defs are never invoked passes while asserting nothing.
This test AST-scans tests/ and fails naming any file with test_ defs but
no `if __name__ == "__main__"` tail reaching them. A tail reaches its
tests by calling them (or `main()`) directly, or through one level of
module-level driver (e.g. a `_run_all_tests()` list driver). Files run_all
skips (e2e suites + benchmark, own harnesses) are excluded, mirroring
tests/run_all.py:_SKIP by name.
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


def _calls_something(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Call) for child in ast.walk(node))


def _tail_invokes_tests(tree: ast.Module, test_names: list) -> bool:
    targets = set(test_names) | {"main"}
    defs = _module_defs(tree)
    for node in tree.body:
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            refs = _loaded_names(node)
            for name in list(refs):
                func = defs.get(name)
                if func is not None:
                    refs |= _loaded_names(func)
            if refs & targets:
                return True
            if refs & _DYNAMIC_DISCOVERY and _calls_something(node):
                return True
    return False


def test_all_test_files_execute_their_tests():
    offenders = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name in SKIPPED_BY_RUN_ALL:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = _module_test_defs(tree)
        if names and not _tail_invokes_tests(tree, names):
            offenders.append(f"{path.name}: {', '.join(names)}")
    assert not offenders, (
        "test files with un-invoked test defs (vacuous green under run_all.py):\n"
        + "\n".join(offenders)
    )


def main():
    test_all_test_files_execute_their_tests()
    print("test_entry_point_wiring: all assertions passed")


if __name__ == "__main__":
    main()
