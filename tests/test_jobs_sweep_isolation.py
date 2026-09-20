"""Static pin: the poller jobs-block keeps one guard per sweeper.

Small_fix #579: the jobs housekeeping block in server/poller/_outcome.py
once wrapped four sweepers (expiry, digests, overdue, bounty) in a single
try/except, so one sick sweeper silently starved the other three - the
shape behind the 09-20 bounty starvation (every tick failed before the
bounty line with no trace on the forum). Each sweeper now owns its guard
with a per-phase failure tag; this test pins that shape so a later merge
cannot re-fold them.
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TARGET = _ROOT / "server" / "poller" / "_outcome.py"

# short attribute name -> expected jobs_sweep_failed phase tag
_PHASES = {
    "sweep_expired_jobs": "expiry",
    "send_job_digests": "digests",
    "sweep_overdue_job_cycles": "overdue",
    "sweep_bug_bounties": "bounty",
}


def _enclosing_try_calls():
    """Map each target call to its enclosing Try node (by id)."""
    tree = ast.parse((_TARGET).read_text(encoding="utf-8"), filename=str(_TARGET))
    found: dict[str, int] = {}
    stack: list[ast.AST] = []

    def _visit(node: ast.AST):
        stack.append(node)
        for child in ast.iter_child_nodes(node):
            _visit(child)
        stack.pop()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name in _PHASES and name not in found:
                for parent in reversed(stack):
                    if isinstance(parent, ast.Try):
                        found[name] = id(parent)
                        break

    _visit(tree)
    return tree, found


def _try_node(tree: ast.AST, want_id: int):
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and id(node) == want_id:
            return node
    raise AssertionError("try node vanished mid-test")


def _handler_text(try_node: ast.Try) -> str:
    src_lines = (_TARGET).read_text(encoding="utf-8").splitlines()
    parts = []
    for handler in try_node.handlers:
        start = handler.lineno - 1
        end = handler.end_lineno or start + 1
        parts.append("\n".join(src_lines[start:end]))
    return "\n".join(parts)


def test_jobs_sweep_isolation():
    tree, found = _enclosing_try_calls()
    missing = sorted(set(_PHASES) - set(found))
    assert not missing, f"sweepers outside any try guard: {missing}"
    # One guard per sweeper: no two targets may share a Try node.
    owners = sorted(found.values())
    assert len(set(owners)) == len(_PHASES), (
        f"sweepers share a guard (re-folded): {found}"
    )
    # Each guard names its failure domain and logs its phase tag.
    for name, want_id in found.items():
        text = _handler_text(_try_node(tree, want_id))
        assert "domain:" in text, f"{name}: guard handler lacks a domain marker"
        assert f'"{_PHASES[name]}"' in text, f"{name}: guard never logs its phase tag"
    print("  jobs_sweep_isolation: ok")


if __name__ == "__main__":
    test_jobs_sweep_isolation()
    print("\n== test_jobs_sweep_isolation: all passed ==")
