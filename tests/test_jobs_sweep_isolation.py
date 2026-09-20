"""Static pin: the poller jobs-block keeps one guard per sweeper.

Small_fix #579: the jobs housekeeping block in server/poller/_outcome.py
once wrapped four sweepers (expiry, digests, overdue, bounty) in a single
try/except, so one sick sweeper silently starved the other three - the
shape behind the 09-20 bounty starvation (every tick failed before the
bounty line with no trace on the forum). Each sweeper now owns its guard
with a per-phase failure tag; this test pins that shape on AST nodes (not
source text) so a later merge cannot re-fold them and no comment can
spoof the pin.
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


def _target_calls():
    """Every Call node in the file invoking one of the four sweepers:
    [(func-name, enclosing-Try-or-None, enclosing-Try-or-None...)] - the
    full chain of enclosing Try nodes, innermost first."""
    tree = ast.parse((_TARGET).read_text(encoding="utf-8"), filename=str(_TARGET))
    hits: dict[str, list[list[ast.Try]]] = {name: [] for name in _PHASES}

    def _visit(node: ast.AST, trys: list[ast.Try]):
        if isinstance(node, ast.Try):
            trys = trys + [node]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _PHASES
        ):
            hits[node.func.attr].append(list(trys))
        for child in ast.iter_child_nodes(node):
            _visit(child, trys)

    _visit(tree, [])
    return hits


def _phase_tagged(t: ast.Try, phase: str) -> bool:
    """Some handler of this Try logs jobs_sweep_failed with phase=<phase>
    as a real keyword literal (quote-style agnostic)."""
    for handler in t.handlers:
        for node in ast.walk(handler):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "log"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "jobs_sweep_failed"
                and any(
                    kw.arg == "phase"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == phase
                    for kw in node.keywords
                )
            ):
                return True
    return False


def _domain_marked(t: ast.Try) -> bool:
    """Some handler of this Try carries a domain marker comment."""
    src = ast.get_source_segment((_TARGET).read_text(encoding="utf-8"), t)
    return src is not None and "domain:" in src


def test_jobs_sweep_isolation():
    hits = _target_calls()
    for name, chains in hits.items():
        # Exactly one call site per sweeper: a second, unguarded call
        # anywhere in the file fails the pin.
        assert len(chains) == 1, f"{name}: {len(chains)} call sites, want 1"
        (chain,) = chains
        # Exactly one guard, and it holds nothing but this sweeper.
        assert len(chain) == 1, f"{name}: not guarded by exactly one try"
        (guard,) = chain
        solos = [
            other
            for other, other_chains in hits.items()
            for c in other_chains
            if c and c[0] is guard and other != name
        ]
        assert not solos, f"{name} shares its guard with {solos} (re-folded)"
        assert _domain_marked(guard), f"{name}: guard lacks a domain marker"
        assert _phase_tagged(guard, _PHASES[name]), (
            f"{name}: guard never logs its phase tag"
        )
    print("  jobs_sweep_isolation: ok")


if __name__ == "__main__":
    test_jobs_sweep_isolation()
    print("\n== test_jobs_sweep_isolation: all passed ==")
