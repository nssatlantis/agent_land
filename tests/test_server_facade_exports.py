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
    "propose_guild_plan_item",
    "edit_guild_plan_item",
    "move_guild_plan_stage",
    "set_guild_plan_owner",
    "add_guild_decision",
    "bind_guild_plan_item",
    "unbind_guild_plan_item",
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
    "design_question",
    "enable_comments",
    "add_comment",
    "design_promote",
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


def test_design_dispatchers_legacy_tools_removed():
    """Hard-remove pins (proposal #939): the four design wrappers must not
    exist as tools on any surface. The db.* functions are
    protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.designs as _designs_tools
    from tests._setup import expect_error

    for _dead in (
        "ask_question",
        "answer_question",
        "promote_preview",
        "promote_to_idea",
    ):
        assert not hasattr(_designs_tools, _dead), f"{_dead} is still defined"
        assert not hasattr(server, _dead), f"{_dead} still on the facade"
    _qdoc = _designs_tools.design_question.__doc__ or ""
    _qactions = set(re.findall(r"action='([a-z_]+)'", _qdoc))
    assert _qactions == {"ask", "answer"}, _qactions
    _pdoc = _designs_tools.design_promote.__doc__ or ""
    _pactions = set(re.findall(r"action='([a-z_]+)'", _pdoc))
    assert _pactions == {"preview", "promote"}, _pactions
    assert _qactions and _pactions, "actions must be advertised in parseable form"
    for _tool, _members in (
        (_designs_tools.design_question, ("ask", "answer")),
        (_designs_tools.design_promote, ("preview", "promote")),
    ):
        _advertised = set(re.findall(r"action='([a-z_]+)'", _tool.__doc__ or ""))
        assert _advertised == set(_members), (_tool.__name__, _advertised)
        _args = ("x", 0) if _tool.__name__ == "design_question" else (0,)
        _err = expect_error(_tool, *_args, "bogus")
        _missing = sorted(m for m in _members if f"'{m}'" not in _err)
        assert not _missing, f"{_tool.__name__} under-reports {_missing}: {_err}"
    # Matrix arms: ask needs body; answer needs question_id + answer;
    # preview takes nothing else; promote needs token + title + body.
    _aerr = expect_error(_designs_tools.design_question, "x", 0, "ask")
    assert "needs body" in _aerr, _aerr
    # Finding #153: the ask-arm mirror - the guard is present in code, now
    # driven, with the answer-arm refusal as its two-way control.
    _kerr = expect_error(_designs_tools.design_question, "x", 0, "ask", None, 7, "a")
    assert "pass no question_id or answer" in _kerr, _kerr
    _nerr = expect_error(_designs_tools.design_question, "x", 0, "answer", "b")
    assert "pass no body" in _nerr, _nerr
    _merr = expect_error(_designs_tools.design_question, "x", 0, "answer")
    assert "needs question_id and answer" in _merr, _merr
    _verr = expect_error(_designs_tools.design_promote, 0, "preview", "tok")
    assert "previews only" in _verr, _verr
    _perr = expect_error(_designs_tools.design_promote, 0, "promote")
    assert "needs token, title and body" in _perr, _perr


def test_removed_design_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #939): the two dispatchers survive,
    so all four legacy names are forbidden in live prose. db.* and
    admin_* seats are true statements (negative lookbehind); the viewer
    and workflow callouts are pinned exactly."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    _dead = ("ask_question", "answer_question", "promote_preview", "promote_to_idea")
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
    _lookbehind = re.compile(r"(?<![.\w])(?:ask|answer)_question\b")
    _hits = _lookbehind.findall(
        (_root / "server" / "tools" / "designs.py").read_text(encoding="utf-8")
    )
    assert not _hits, f"designs.py names the removed Q&A tools unqualified: {_hits}"
    _plookbehind = re.compile(r"(?<![.\w])promote_(?:preview|to_idea)\b")
    _phits = _plookbehind.findall(
        (_root / "server" / "tools" / "designs.py").read_text(encoding="utf-8")
    )
    assert not _phits, (
        f"designs.py names the removed promote tools unqualified: {_phits}"
    )
    _viewer_text = (_root / "viewer" / "_designs.py").read_text(encoding="utf-8")
    assert "design_question(action='ask')" in _viewer_text
    assert "ask_question." not in _viewer_text
    assert "design_promote(action='preview')" in _viewer_text
    assert "promote_preview shows" not in _viewer_text


if __name__ == "__main__":
    test_server_facade_exports_present_in_source()
    test_server_facade_exports_present_at_runtime()
    test_server_repo_search_stays_module()
    test_design_dispatchers_legacy_tools_removed()
    test_removed_design_names_absent_from_shipped_prose()
    print("test_server_facade_exports: all assertions passed")
