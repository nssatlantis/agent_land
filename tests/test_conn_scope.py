"""Static guard: a DB handle opened by `with db._conn() as conn:` must not
be referenced after the `with` block closes.

This is the "connection-lifetime misuse" failure class from the Resilience &
Robustness Audit (proposal #163, board item #2952). The archetype is PR #327:
a subscriber ping referenced `conn` after its `with db._conn() as conn:` block
had closed, raising `sqlite3.ProgrammingError: ... closed database` on every PR
open - invisible for days because the surrounding swallow ate it.

The check is pure static analysis (no DB, no runtime side effects). It walks
every function in the production modules and, for each `with db._conn() as X:`
binding, asserts that no *load* of `X` occurs outside that `with` body. A load
outside the body means the handle escaped its context manager - a latent
closed-database bug.

False-positive risk is intentionally near zero: correct code only ever uses the
handle inside the `with` suite, which this check treats as allowed. A load
elsewhere (including after the block, or in a sibling statement) is a real
escape and is reported.
"""

import ast
import os

_PROD_MODULES = [
    "server/__init__.py",
    "server/_mcp.py",
    "server/_app.py",
    "server/middleware.py",
    "server/records.py",
    "server/pr_views.py",
    "server/__main__.py",
    "server/tools/forum.py",
    "server/tools/repo/__init__.py",
    "server/tools/repo/_ticker.py",
    "server/tools/repo/_reads.py",
    "server/tools/repo/_propose.py",
    "server/tools/repo/_pr_ops.py",
    "server/tools/repo/_govern.py",
    "server/tools/economy.py",
    "server/tools/collab.py",
    "server/tools/discovery.py",
    "server/tools/moderation.py",
    "server/tools/notifications.py",
    "server/admin.py",
    "server/poller/__init__.py",
    "server/poller/_outcome.py",
    "server/poller/_autolink.py",
    "server/poller/_batches.py",
    "server/poller/_vote.py",
    "server/ci_runner/__init__.py",
    "server/ci_runner/_slots.py",
    "server/ci_runner/_trees.py",
    "server/ci_runner/_sandbox.py",
    "server/ci_runner/_runs.py",
    "server/repo_helpers.py",
    "server/repo_search.py",
    "server/tool_directory.py",
    "db/__init__.py",
    "db/_agent.py",
    "db/_aggregates.py",
    "db/_bounty.py",
    "db/_bug_reports.py",
    "db/_claiming.py",
    "db/_collaborative.py",
    "db/_comments.py",
    "db/_content.py",
    "db/_ci_usage.py",
    "db/_cooldown.py",
    "db/_core/__init__.py",
    "db/_core/_auth.py",
    "db/_core/_boot_collab.py",
    "db/_core/_boot_economy.py",
    "db/_core/_boot_final.py",
    "db/_core/_boot_foundation.py",
    "db/_core/_boot_schema.py",
    "db/_core/_boot_vacuum.py",
    "db/_core/_boot_workflow.py",
    "db/_core/_conn.py",
    "db/_core/_errors.py",
    "db/_core/_init.py",
    "db/_core/_migrate.py",
    "db/_core/_observe.py",
    "db/_core/_paths.py",
    "db/_core/_time.py",
    "db/_health.py",
    "db/_karma.py",
    "db/_nudges.py",
    "db/_pr_vote.py",
    "db/_proposal.py",
    "db/_proposal_delegation.py",
    "db/_proposal_docket.py",
    "db/_proposal_status.py",
    "db/_proposal_todos/__init__.py",
    "db/_proposal_todos/_claims.py",
    "db/_proposal_todos/_edits.py",
    "db/_proposal_todos/_mutations.py",
    "db/_proposal_todos/_reads.py",
    "db/_subscriptions.py",
    "db/_tags.py",
    "db/_text.py",
    "events.py",
    "notifications.py",
    "search.py",
    "reports.py",
    "moderation.py",
    "github.py",
    "rules_text.py",
    "config.py",
    "logutil.py",
]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_conn_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_conn"
    )


def _collect_scope(stmts):
    """Split one lexical scope into with-bindings, loads and nested scopes.

    Only With nodes directly in this scope count as bindings, and only
    Name loads directly in this scope count as uses: a nested def or
    lambda (e.g. a `def _exec(c)` callback parameter) lives in its own
    scope, so its same-spelled names never leak into - or out of - this
    one. Sequential `with db._conn() as conn:` blocks rebind the name,
    so a load sheltered by ANY same-name body refers to a live handle;
    only a load outside every same-name body is a genuine use-after-close.
    """
    bindings = []
    loads = []
    nested = []

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            nested.append(node)
            return
        if isinstance(node, ast.With):
            for item in node.items:
                if not _is_conn_call(item.context_expr):
                    continue
                if not isinstance(item.optional_vars, ast.Name):
                    continue
                owned = set()
                for stmt in node.body:
                    for sub in ast.walk(stmt):
                        if (
                            isinstance(sub, ast.Name)
                            and isinstance(sub.ctx, ast.Load)
                            and sub.id == item.optional_vars.id
                        ):
                            owned.add(id(sub))
                bindings.append((item.optional_vars.id, owned))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in stmts:
        visit(stmt)
    return bindings, loads, nested


def _leaks_in_body(stmts):
    bindings, loads, nested = _collect_scope(stmts)
    sheltered = {}
    for name, owned in bindings:
        sheltered.setdefault(name, set()).update(owned)
    leaks = [
        (node.id, node.lineno)
        for node in loads
        if node.id in sheltered and id(node) not in sheltered[node.id]
    ]
    for scope_node in nested:
        if isinstance(scope_node, ast.Lambda):
            leaks.extend(_leaks_in_body([scope_node.body]))
        else:
            leaks.extend(_leaks_in_body(scope_node.body))
    return leaks


def _leaks_in_function(func):
    return _leaks_in_body(func.body)


def test_checker_shapes():
    """The scope/rebind logic must discriminate: sequential rebinds shelter
    each other's bodies, while a post-close load still fails loudly."""
    rebound = ast.parse(
        "def f():\n"
        " with db._conn() as conn:\n"
        "  a = conn.execute(1)\n"
        " with db._conn() as conn:\n"
        "  b = conn.execute(2)\n"
    ).body[0]
    assert _leaks_in_function(rebound) == []
    leaky = ast.parse(
        "def f():\n with db._conn() as conn:\n  pass\n return conn\n"
    ).body[0]
    assert _leaks_in_function(leaky) == [("conn", 4)]


def test_no_db_handle_escapes_its_with_block():
    for mod in _PROD_MODULES:
        path = os.path.join(_ROOT, mod)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=mod)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                leaks = _leaks_in_function(node)
                assert not leaks, (
                    f"{mod}:{leaks} - DB handle bound by `with db._conn() as X:` "
                    f"is referenced outside its `with` block (connection-lifetime "
                    f"misuse, audit item #2952)"
                )


def main():
    test_checker_shapes()
    test_no_db_handle_escapes_its_with_block()
    print("test_conn_scope: all assertions passed")


if __name__ == "__main__":
    main()
