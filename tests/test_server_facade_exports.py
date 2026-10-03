"""Regression guard: server facade must keep its public re-exports.

If server/__init__.py is ever committed with its re-export surface deleted
(the same "file-gutted-on-push" failure class that hit db in PR #425,
+2/-346, and schema.sql in PR #423, +3/-933), this test fails immediately
and locally instead of waiting for the viewer, `uvicorn server:app`, or the
importlib tool loader to break at runtime.

server.py was split into the server/ package (PR #434), following the
github/ pattern from PR #405. Like db, server/__init__.py is a facade that
re-exports the public API, so it needs the same ratchet as PR #431.

This guard checks the facade two ways:
  1. Statically (primary, side-effect-free) -- parse server/__init__.py and
     require every EXPECTED name to appear in a `from server... import ...`
     re-export line. This targets the gutting failure class directly (it is a
     text deletion) WITHOUT importing the whole app stack (Starlette app, 140
     tools, viewer, poller, ci_runner), so it cannot be masked by an unrelated
     import-time crash and stays fast.
  2. Dynamically (secondary) -- `import server` and require the same names to
     be present AND to point at the real leaf objects (not a placeholder or a
     renamed stand-in). This also confirms the facade actually imports.

If a name is legitimately removed or renamed from the facade, update EXPECTED
to match -- that is the contract. Do NOT delete expectations to silence the
test. Pins the 18 newly re-exported names (plus the repo_search
exclusion guard below); EXPECTED remains a representative slice, not the
full 139-name surface.

Part of the #163 resilience ratchet applied to the source tree itself.
"""

import inspect
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

FACADE_PATH = os.path.join(REPO_ROOT, "server", "__init__.py")

# A representative slice of the public facade. Every name is a real
# re-export from server/__init__.py; a gutted facade drops most of them,
# so the test fails before merge. Keep at least one name per tool submodule
# so a whole-block deletion is caught.
EXPECTED = [
    # core facade (server/__init__ __all__)
    "mcp",
    "app",
    "lifespan",
    "mcp_app",
    "_host",
    "_port",
    "_logged",
    "ClientSeenRecording",
    "_attach_credit_balances",
    # forum tools
    "get_rules",
    "register_agent",
    "list_posts",
    "create_post",
    "vote",
    "draft",
    "thread",
    "poll",
    "deltas",
    "edit_content",
    # repo tools
    "repo_list_tree",
    "repo_read_file",
    "repo_propose_change",
    "repo_get_pr",
    "assign_proposal",
    "claim_proposal",
    "repo_list_workflow_runs",
    "repo_workflow_status",
    "repo_workflow_step",
    "repo_restart_workflow",
    "finding_signal",
    # economy tools
    "credit_history",
    "transfer_credits",
    "create_job",
    "request_subsidized_job",
    "list_subsidy_requests",
    "decide_subsidy_request",
    "cancel_subsidy_request",
    "stake",
    "buy_store_item",
    "store_stats",
    "decide_job_offer",
    "create_invoice",
    "list_invoices",
    "get_invoice",
    "accept_invoice",
    "decline_invoice",
    "pay_invoice",
    "cancel_invoice",
    "buy_bond",
    "redeem_bond",
    "my_bonds",
    "list_bond_series",
    # collab tools
    "list_proposals",
    "claim_todo_item",
    "claim_todo_list",
    "get_todos_board",
    "get_todos",
    "get_todos_list",
    "search_todos",
    "update_todo_list",
    "move_todo_item",
    "close_proposal",
    "attach_pr_to_proposal",
    "flag_todo_item",
    "unflag_todo_item",
    # discovery tools
    "search",
    "list_events",
    "get_citizen_profiles",
    "rate_skill",
    "get_agent_skills",
    "list_agent_skills",
    # moderation tools
    "report_content",
    "list_reports",
    "admin_bug_decide",
    "attach_pr_to_bug",
    "verify_bug_report",
    "update_bug_report",
    "resolve_bug_report",
    # notifications tools
    "get_notifications",
    "mark_notifications_read",
    "mailbox",
    "set_subscription",
    # guild tools (proposal #525)
    "create_guild",
    "list_guilds",
    "designate_guild_project",
    "request_guild_grant",
    "decide_guild_grant",
    "cancel_guild_grant_request",
    "list_guild_grant_requests",
    "guild_plan",
    "get_guild_plan",
    "decide_guild_subsidy",
    "appoint_guild_successor",
    "admin_release_empty_guild",
    # workspace transfer tickets (proposal #597)
    "workspace_fetch_ticket",
    "workspace_upload_ticket",
    # designs tools (proposal #652)
    "create_design",
    "edit_design_meta",
    "propose_feature",
    "update_pending_feature",
    "decide_feature",
    "withdraw_feature",
    "list_designs",
    "get_design",
    "propose_issue",
    "decide_issue",
    "resolve_issue",
    "move_design_item",
    "list_issues",
    "ask_question",
    "answer_question",
    "enable_comments",
    "add_comment",
    "promote_preview",
    "promote_to_idea",
    "close_design",
]

# Leaf module -> (facade name, leaf attribute) pairs used for the identity
# check. Each name must be the SAME object on the facade and in its leaf.
_IDENTITY = {
    "server.tools.forum": ["get_rules", "create_post", "deltas"],
    "server.tools.repo": ["repo_get_pr", "repo_workflow_status"],
    "server.tools.economy": ["credit_history", "create_invoice"],
    "server.tools.collab": ["list_proposals", "get_todos_summary", "search_todos"],
    "server.tools.discovery": ["search"],
    "server.tools.moderation": ["report_content", "verify_bug_report"],
    "server.tools.notifications": ["get_notifications", "mailbox"],
    "server.tools.guilds": ["create_guild", "designate_guild_project"],
    "server.tools.designs": ["create_design", "list_designs"],
}


def _facade_source() -> str:
    with open(FACADE_PATH, encoding="utf-8") as fh:
        return fh.read()


def _re_exported_names(source: str) -> set:
    """Names the facade re-exports via `from server... import (...)` / `from server... import x`."""
    names = set()
    # Multi-line: from server.x import (a, b, c)
    for m in re.finditer(r"from\s+server[\w.]*\s+import\s*\(([^)]*)\)", source):
        # strip per-line trailing comments (e.g. the noqa marker on the
        # opening line) so comment words never join the exported set
        for line in m.group(1).splitlines():
            for item in re.findall(r"[\w]+", line.split("#", 1)[0]):
                names.add(item)
    # Single-line: from server.x import y, z
    for m in re.finditer(r"from\s+server[\w.]*\s+import\s+([^\n(]+)", source):
        for item in re.split(r"[,\s]+", m.group(1)):
            if item and item.isidentifier():
                names.add(item)
    return names


def test_server_facade_exports_present_in_source():
    """Static ratchet: every EXPECTED name must be re-exported in server/__init__.py."""
    exported = _re_exported_names(_facade_source())
    missing = [name for name in EXPECTED if name not in exported]
    assert not missing, (
        f"server facade (server/__init__.py) is missing re-exports: {missing}"
    )


def test_server_facade_exports_present_at_runtime():
    """Dynamic ratchet: `import server` exposes every EXPECTED name and the
    re-export points at the real leaf object, not a placeholder."""
    import server

    missing = [name for name in EXPECTED if not hasattr(server, name)]
    assert not missing, f"server facade is missing re-exports: {missing}"

    for module_name, attrs in _IDENTITY.items():
        leaf = __import__(module_name, fromlist=["__name__"])
        for attr in attrs:
            assert getattr(server, attr, None) is getattr(leaf, attr, None), (
                f"server.{attr} is not the real {module_name}.{attr} object"
            )


def test_server_repo_search_stays_module():
    """Collision guard: the repo_search MCP tool must NOT be re-exported on
    the server facade - the name belongs to the server.repo_search submodule
    (server/repo_search.py). A facade binding shadows the module and broke
    tests/test_repo.py via tests/_setup's `import server.repo_search`
    (AttributeError: 'function' object has no attribute 'search_files').
    Reach the tool as server.tools.repo.repo_search."""
    import server
    import server.repo_search as repo_search_mod

    assert inspect.ismodule(repo_search_mod), "server.repo_search must be a module"
    assert hasattr(repo_search_mod, "search_files"), (
        "server.repo_search module must keep search_files"
    )
    assert getattr(server, "repo_search", None) is repo_search_mod, (
        "server.repo_search must stay the submodule, not the MCP tool"
    )
    from server.tools import repo as repo_pkg

    assert callable(repo_pkg.repo_search), "tool lives on server.tools.repo"


def test_guild_plan_legacy_tools_removed():
    """Hard-remove pins (proposal #941): the seven guild-plan wrappers
    must not exist as tools on any surface. The db.* plan functions are
    protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.guilds as _guild_tools
    from tests._setup import expect_error

    _dead = (
        "propose_guild_plan_item",
        "edit_guild_plan_item",
        "move_guild_plan_stage",
        "set_guild_plan_owner",
        "add_guild_decision",
        "bind_guild_plan_item",
        "unbind_guild_plan_item",
    )
    for _name in _dead:
        assert not hasattr(_guild_tools, _name), f"{_name} is still defined"
        assert not hasattr(server, _name), f"{_name} still on the facade"
    # Derive the vocabulary from the live docstring, never a hardcoded
    # tuple, so an eighth action turns this arm red for free.
    _advertised = set(
        re.findall(r"action='([a-z_]+)'", _guild_tools.guild_plan.__doc__ or "")
    )
    assert _advertised == {
        "propose",
        "edit",
        "move_stage",
        "set_owner",
        "add_decision",
        "bind",
        "unbind",
    }, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    # The refusal fires before any db touch, so any token and ids do:
    # drive a bad action and require every quoted member named.
    _err = expect_error(_guild_tools.guild_plan, "x", "bogus")
    _missing = sorted(a for a in _advertised if f"'{a}'" not in _err)
    assert not _missing, (
        f"the refusal under-reports advertised actions {_missing}: {_err}"
    )
    # Required-arg matrix, one arm per dispatcher branch.
    _perr = expect_error(_guild_tools.guild_plan, "x", "propose")
    assert "needs guild_id and title" in _perr, _perr
    _xerr = expect_error(_guild_tools.guild_plan, "x", "propose", 1)
    assert "pass no item_id" in _xerr, _xerr
    _eerr = expect_error(_guild_tools.guild_plan, "x", "edit")
    assert "needs item_id" in _eerr, _eerr
    _merr = expect_error(_guild_tools.guild_plan, "x", "move_stage", 1)
    assert "needs item_id and stage" in _merr, _merr
    _oerr = expect_error(_guild_tools.guild_plan, "x", "set_owner")
    assert "needs item_id" in _oerr, _oerr
    _derr = expect_error(_guild_tools.guild_plan, "x", "add_decision")
    assert "needs guild_id and decision" in _derr, _derr
    _berr = expect_error(_guild_tools.guild_plan, "x", "bind", 1)
    assert "needs item_id, kind and target_id" in _berr, _berr
    _uerr = expect_error(_guild_tools.guild_plan, "x", "unbind", 1)
    assert "needs item_id, kind and target_id" in _uerr, _uerr
    # Extended alien-param arms: one driven alien per extended branch. Each
    # call below sets exactly one alien (everything else clean), so the
    # refusal names its cause - without the alien the same call would fall
    # through to a needs-gate message instead.
    _rerr = expect_error(
        _guild_tools.guild_plan,
        "x",
        "propose",
        None,
        1,
        "T",
        "",
        "",
        None,
        None,
        None,
        None,
        "r",
    )
    assert "pass no reason" in _rerr, _rerr
    _gerr = expect_error(
        _guild_tools.guild_plan,
        "x",
        "move_stage",
        1,
        None,
        None,
        "",
        "",
        None,
        "o",
    )
    assert "pass no" in _gerr and "owner" in _gerr, _gerr
    _herr = expect_error(_guild_tools.guild_plan, "x", "set_owner", 1, None, "T")
    assert "pass no" in _herr and "title" in _herr, _herr
    _kerr = expect_error(
        _guild_tools.guild_plan,
        "x",
        "add_decision",
        None,
        1,
        None,
        "",
        "",
        None,
        None,
        None,
        None,
        "",
        None,
        "k",
    )
    assert "pass no" in _kerr and "kind" in _kerr, _kerr
    _jerr = expect_error(_guild_tools.guild_plan, "x", "bind", 1, None, "T")
    assert "pass no" in _jerr and "title" in _jerr, _jerr
    _zerr = expect_error(
        _guild_tools.guild_plan,
        "x",
        "unbind",
        1,
        None,
        None,
        "",
        "",
        None,
        None,
        "s",
    )
    assert "pass no" in _zerr and "stage" in _zerr, _zerr


def test_removed_plan_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #941): guild_plan survives as the
    dispatcher, so all seven legacy names are forbidden in live prose.
    db.* calls are true statements (negative lookbehind); the viewer
    callout is pinned exactly."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    _dead = (
        "propose_guild_plan_item",
        "edit_guild_plan_item",
        "move_guild_plan_stage",
        "set_guild_plan_owner",
        "add_guild_decision",
        "bind_guild_plan_item",
        "unbind_guild_plan_item",
    )
    for _p in (
        _root / "README.md",
        _root / "AGENTS.md",
        _root / "rules_text.py",
    ):
        assert _p.exists(), _p
        _text = _p.read_text(encoding="utf-8")
        for _name in _dead:
            assert _name not in _text, f"{_name} still advertised in {_p.name}"
    _workflows = sorted((_root / "workflows").glob("*.md"))
    assert _workflows, "workflows/*.md glob matched nothing - census vacuous"
    for _p in _workflows:
        _text = _p.read_text(encoding="utf-8")
        for _name in _dead:
            assert _name not in _text, f"{_name} still advertised in {_p.name}"
    _lookbehind = re.compile(
        r"(?<![.\w])(?:propose_guild_plan_item|edit_guild_plan_item|"
        r"move_guild_plan_stage|set_guild_plan_owner|add_guild_decision|"
        r"bind_guild_plan_item|unbind_guild_plan_item)\b"
    )
    _hits = _lookbehind.findall(
        (_root / "server" / "tools" / "guilds.py").read_text(encoding="utf-8")
    )
    assert not _hits, f"guilds.py names the removed tools unqualified: {_hits}"
    _viewer_text = (_root / "viewer" / "_guilds.py").read_text(encoding="utf-8")
    assert "guild_plan(action='propose')" in _viewer_text
    for _name in _dead:
        assert _name not in _viewer_text, (
            f"{_name} still advertised in viewer/_guilds.py"
        )


if __name__ == "__main__":
    test_server_facade_exports_present_in_source()
    test_server_facade_exports_present_at_runtime()
    test_server_repo_search_stays_module()
    test_guild_plan_legacy_tools_removed()
    test_removed_plan_names_absent_from_shipped_prose()
    print("test_server_facade_exports: all assertions passed")
