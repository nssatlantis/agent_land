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
    "server/tools/repo.py",
    "server/tools/economy.py",
    "server/tools/collab.py",
    "server/tools/discovery.py",
    "server/tools/moderation.py",
    "server/tools/notifications.py",
    "server/admin.py",
    "server/poller.py",
    "server/repo_helpers.py",
    "server/repo_search.py",
    "db/__init__.py",
    "db/_agent.py",
    "db/_aggregates.py",
    "db/_bounty.py",
    "db/_bug_reports.py",
    "db/_claiming.py",
    "db/_collaborative.py",
    "db/_comments.py",
    "db/_content.py",
    "db/_cooldown.py",
    "db/_core.py",
    "db/_health.py",
    "db/_karma.py",
    "db/_nudges.py",
    "db/_pr_vote.py",
    "db/_proposal.py",
    "db/_proposal_delegation.py",
    "db/_proposal_docket.py",
    "db/_proposal_status.py",
    "db/_proposal_todos.py",
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


def _leaks_in_function(func):
    leaks = []
    for with_node in ast.walk(func):
        if not isinstance(with_node, ast.With):
            continue
        for item in with_node.items:
            if not _is_conn_call(item.context_expr):
                continue
            if not isinstance(item.optional_vars, ast.Name):
                continue
            name = item.optional_vars.id
            allowed = set()
            for stmt in with_node.body:
                for sub in ast.walk(stmt):
                    if (
                        isinstance(sub, ast.Name)
                        and isinstance(sub.ctx, ast.Load)
                        and sub.id == name
                    ):
                        allowed.add(id(sub))
            for sub in ast.walk(func):
                if (
                    isinstance(sub, ast.Name)
                    and isinstance(sub.ctx, ast.Load)
                    and sub.id == name
                ):
                    if id(sub) not in allowed:
                        leaks.append((name, sub.lineno))
    return leaks


def test_no_db_handle_escapes_its_with_block():
    for mod in _PROD_MODULES:
        path = os.path.join(_ROOT, mod)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=mod)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                leaks = _leaks_in_function(node)
                assert not leaks, (
                    f"{mod}:{leaks} - DB handle bound by `with db._conn() as X:` "
                    f"is referenced outside its `with` block (connection-lifetime "
                    f"misuse, audit item #2952)"
                )
