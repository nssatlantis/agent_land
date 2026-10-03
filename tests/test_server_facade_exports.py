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
    "decide_invoice",
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
    # workspace claim/release dispatch (proposal #919)
    "workspace_claim",
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


def test_workspace_claim_legacy_tools_removed():
    """Hard-remove pin (proposal #919): claim_workspace,
    release_workspace, workspace_fetch_ticket and workspace_upload_ticket
    must not exist as tools on any surface. The db.* claim functions are
    protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.repo._transfer as _transfer_tools
    import server.tools.repo._workspace as _ws_tools
    from tests._setup import expect_error

    for _dead, _mod in (
        ("claim_workspace", _ws_tools),
        ("release_workspace", _ws_tools),
        ("workspace_fetch_ticket", _transfer_tools),
        ("workspace_upload_ticket", _transfer_tools),
    ):
        assert not hasattr(_mod, _dead), f"{_dead} is still defined"
        assert not hasattr(server, _dead), f"{_dead} still on the facade"
    # Derive the vocabulary from the live docstring, never a hardcoded
    # tuple, so a fourth action turns this arm red for free.
    _advertised = set(
        re.findall(
            r"action='([a-z_]+)'", _ws_tools.workspace_claim.__doc__ or ""
        )
    )
    assert _advertised == {"claim", "renew", "release"}, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    # The unknown-action refusal fires before any db touch, so any token
    # and ids do: drive it and require every advertised member named.
    _err = expect_error(_ws_tools.workspace_claim, "x", "bogus", 0, "n")
    _missing = sorted(a for a in _advertised if f"'{a}'" not in _err)
    assert not _missing, (
        f"the refusal under-reports advertised actions {_missing}: {_err}"
    )
    # The hoisted expect_shas guard (Lyra-Quill's audit): pins belong to
    # renew and are refused - never silently dropped - on claim/release.
    _cerr = expect_error(
        _ws_tools.workspace_claim, "x", "claim", 0, "n", expect_shas={"a": "b"}
    )
    assert "action='renew'" in _cerr, f"claim arm dropped expect_shas: {_cerr}"
    _rerr = expect_error(
        _ws_tools.workspace_claim, "x", "release", 0, "n", expect_shas={"a": "b"}
    )
    assert "action='renew'" in _rerr, f"release arm dropped expect_shas: {_rerr}"


def test_removed_workspace_claim_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #919): workspace_claim survives, so
    all four legacy names are forbidden in live prose. db.* calls are true
    statements (negative lookbehind)."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    _dead = (
        "claim_workspace",
        "release_workspace",
        "workspace_fetch_ticket",
        "workspace_upload_ticket",
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
    _ws_path = _root / "server" / "tools" / "repo" / "_workspace.py"
    _ws_hits = re.compile(r"(?<![.\w])(?:claim|release)_workspace\b").findall(
        _ws_path.read_text(encoding="utf-8")
    )
    assert not _ws_hits, f"_workspace.py names the removed tools: {_ws_hits}"
    _tr_path = _root / "server" / "tools" / "repo" / "_transfer.py"
    _tr_hits = re.compile(r"(?<![.\w])workspace_(?:fetch|upload)_ticket\b").findall(
        _tr_path.read_text(encoding="utf-8")
    )
    assert not _tr_hits, f"_transfer.py names the removed tools: {_tr_hits}"


def test_program_claim_legacy_tool_removed():
    """Hard-remove pin (proposal #929): release_program_item must not exist
    as a tool on any surface. db.release_program_item is protocol-agnostic
    core and is not asserted here."""
    import server
    import server.tools.programs as _programs_tools
    from tests._setup import expect_error

    assert not hasattr(_programs_tools, "release_program_item"), (
        "release_program_item is still defined"
    )
    assert not hasattr(server, "release_program_item"), (
        "release_program_item still on the facade"
    )
    # Derive the vocabulary from the live docstring, never a hardcoded
    # tuple, so a third action turns this arm red for free.
    _advertised = set(
        re.findall(
            r"action='([a-z_]+)'", _programs_tools.claim_program_item.__doc__ or ""
        )
    )
    assert _advertised == {"claim", "release"}, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    # The refusal fires before any db touch, so any token and ids do: drive
    # a bad action and require every quoted member named in the refusal.
    _err = expect_error(_programs_tools.claim_program_item, "x", 0, 0, "bogus")
    _missing = sorted(a for a in _advertised if f"'{a}'" not in _err)
    assert not _missing, (
        f"the refusal under-reports advertised actions {_missing}: {_err}"
    )


def test_removed_program_claim_name_absent_from_shipped_prose():
    """Shipped-prose census (proposal #929): claim_program_item survives as
    the dispatcher, so only release_program_item is forbidden - and only
    outside one dated record. The 2026-09-20 full-visit changelog line names
    both tools as of that date; like HISTORY.md it stays byte-identical,
    so the pin exempts exactly that line and nothing else."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    _HISTORICAL = (
        "`claim_program_item`/`release_program_item`, "
        "`add_program_item`/`update_program`/`create_program` (proposal #529)."
    )
    for _p in (
        _root / "README.md",
        _root / "AGENTS.md",
        _root / "rules_text.py",
    ):
        assert _p.exists(), _p
        assert "release_program_item" not in _p.read_text(encoding="utf-8"), (
            f"release_program_item still advertised in {_p.name}"
        )
    _workflows = sorted((_root / "workflows").glob("*.md"))
    assert _workflows, "workflows/*.md glob matched nothing - census vacuous"
    for _p in _workflows:
        _text = _p.read_text(encoding="utf-8").replace(_HISTORICAL, "")
        assert "release_program_item" not in _text, (
            f"release_program_item still advertised in {_p.name}"
        )
    # Defining module: db.release_program_item is a true statement, so the
    # rule is db-qualified-or-absent (negative lookbehind, not a prefix
    # strip - a bare pattern matches inside db.release_program_item).
    _lookbehind = re.compile(r"(?<![.\w])release_program_item\b")
    _hits = _lookbehind.findall(
        (_root / "server" / "tools" / "programs.py").read_text(encoding="utf-8")
    )
    assert not _hits, f"programs.py names the removed tool unqualified: {_hits}"


def test_decide_invoice_legacy_tools_removed():
    """Hard-remove pin (proposal #931): accept_invoice and decline_invoice
    must not exist as tools on any surface. db.accept_invoice and
    db.decline_invoice are protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.economy as _economy_tools
    from tests._setup import expect_error

    for _gone in ("accept_invoice", "decline_invoice"):
        assert not hasattr(_economy_tools, _gone), f"{_gone} is still defined"
        assert not hasattr(server, _gone), f"{_gone} still on the facade"
    # Derive the vocabulary from the live docstring, never a hardcoded
    # tuple, so a third action turns this arm red for free.
    _advertised = set(
        re.findall(r"action='([a-z_]+)'", _economy_tools.decide_invoice.__doc__ or "")
    )
    assert _advertised == {"accept", "decline"}, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    # The refusal fires before any db touch, so any token and id do: drive
    # a bad action and require every quoted member named in the refusal.
    _err = expect_error(_economy_tools.decide_invoice, "x", 0, "bogus")
    assert not _missing, (
        f"the refusal under-reports advertised actions {_missing}: {_err}"
    )


def test_removed_invoice_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #931): decide_invoice survives, so
    only accept_invoice and decline_invoice are forbidden. The canonical
    sweep lives in the shared helper (finding #142) - this arm passes its
    names plus its defining-module lookbehind and keeps only the
    db-adjacent exact pins inline. No dated record names them (verified by
    census)."""
    from pathlib import Path

    from tests._setup import assert_no_removed_tool_names

    _root = Path(REPO_ROOT)
    assert_no_removed_tool_names(
        ("accept_invoice", "decline_invoice"),
        root=_root,
        lookbehind_modules=("server/tools/economy.py",),
    )
    # db/_invoices.py defines db.accept_invoice/db.decline_invoice, so neither
    # the strict rule nor the lookbehind can judge it (both red on the db
    # layer itself - CI proved exactly that on the first push: the two defs
    # plus one internal comment). Pin its three reworded nudge strings
    # exactly instead: presence of each replacement plus absence of the
    # precise dead string it replaced.
    _inv_text = (_root / "db" / "_invoices.py").read_text(encoding="utf-8")
    assert "decide_invoice({iid}, action='accept')" in _inv_text
    assert "decide_invoice({iid}, action='decline')" in _inv_text
    assert "accept_invoice({iid})" not in _inv_text
    assert "decline_invoice({iid})" not in _inv_text
    # Fourth site, same file: the create_invoice db docstring carried a bare
    # parenthesized reference neither ({iid}) shape above can see.
    assert "(accept_invoice) before anything nudges" not in _inv_text


if __name__ == "__main__":
    test_server_facade_exports_present_in_source()
    test_server_facade_exports_present_at_runtime()
    test_server_repo_search_stays_module()
    test_workspace_claim_legacy_tools_removed()
    test_removed_workspace_claim_names_absent_from_shipped_prose()
    test_program_claim_legacy_tool_removed()
    test_removed_program_claim_name_absent_from_shipped_prose()
    test_decide_invoice_legacy_tools_removed()
    test_removed_invoice_names_absent_from_shipped_prose()
    print("test_server_facade_exports: all assertions passed")
