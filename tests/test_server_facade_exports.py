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
    "notes",
    # collab tools
    "list_proposals",
    "claim_todo",
    "get_todos_board",
    "get_todos",
    "get_todos_list",
    "search_todos",
    "update_todo_list",
    "move_todo_item",
    "close_proposal",
    "attach_pr_to_proposal",
    "flag_todo_item",
    # discovery tools
    "search",
    "list_events",
    "get_citizen_profiles",
    "rate_skill",
    "get_agent_skills",
    "list_agent_skills",
    "manage_tag",
    # moderation tools
    "report_content",
    "list_reports",
    "admin_bug_decide",
    "attach_pr_to_bug",
    "verify_bug_report",
    "update_bug_report",
    "resolve_bug_report",
    # notifications tools
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
    "guild_chat",
    "guild_cosign",
    "guild_poll",
    "guild_subsidy",
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
    "server.tools.collab": ["list_proposals", "get_todos_board", "search_todos"],
    "server.tools.discovery": ["search"],
    "server.tools.moderation": ["report_content", "verify_bug_report"],
    "server.tools.notifications": ["mailbox"],
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
            # hasattr first: getattr(..., None) is None on BOTH sides when
            # the name exists on NEITHER, so the identity check below passes
            # vacuously (None is None) and can never fail. A name missing
            # from either surface must red here, not hide behind the default.
            assert hasattr(server, attr), f"server facade is missing {attr}"
            assert hasattr(leaf, attr), f"{module_name} is missing {attr}"
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


def test_guild_dispatchers_legacy_tools_removed():
    """Hard-remove pins (proposal #938): the nine guild wrappers must not
    exist as tools on any surface. The db.* functions are
    protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.guilds as _guild_tools
    from tests._setup import expect_error

    for _dead in (
        "request_guild_cosign",
        "confirm_guild_cosign",
        "request_guild_subsidy",
        "decide_guild_subsidy",
        "create_guild_poll",
        "vote_guild_poll",
        "post_guild_chat",
        "list_guild_chat",
        "delete_guild_chat",
    ):
        assert not hasattr(_guild_tools, _dead), f"{_dead} is still defined"
        assert not hasattr(server, _dead), f"{_dead} still on the facade"
    # The cosign selector is step, not action: the request arm already
    # takes an action spend parameter. Both vocabularies derived live.
    _steps = set(
        re.findall(r"step='([a-z_]+)'", _guild_tools.guild_cosign.__doc__ or "")
    )
    assert _steps == {"request", "confirm"}, _steps
    _serr = expect_error(_guild_tools.guild_cosign, "x", "bogus")
    assert "'request'" in _serr and "'confirm'" in _serr, _serr
    _merr = expect_error(_guild_tools.guild_cosign, "x", "request")
    assert "needs guild_id, action and amount_credits" in _merr, _merr
    _xerr = expect_error(_guild_tools.guild_cosign, "x", "confirm", 1, "ops", 5.0, 9)
    assert "pass no guild_id" in _xerr, _xerr
    for _tool, _members in (
        (_guild_tools.guild_subsidy, ("request", "decide")),
        (_guild_tools.guild_poll, ("create", "vote")),
        (_guild_tools.guild_chat, ("post", "list", "delete")),
    ):
        _advertised = set(re.findall(r"action='([a-z_]+)'", _tool.__doc__ or ""))
        assert _advertised == set(_members), (_tool.__name__, _advertised)
        _err = expect_error(_tool, "x", "bogus")
        _missing = sorted(m for m in _members if f"'{m}'" not in _err)
        assert not _missing, f"{_tool.__name__} under-reports {_missing}: {_err}"
    # Matrix arms, one per dispatcher: missing ids refuse, alien ids refuse.
    _uerr = expect_error(_guild_tools.guild_subsidy, "x", "request")
    assert "needs guild_id, amount_credits and payback" in _uerr, _uerr
    _derr = expect_error(
        _guild_tools.guild_subsidy, "x", "decide", 1, 2.0, True, "r", 7, True
    )
    assert "pass no guild_id" in _derr, _derr
    _perr = expect_error(_guild_tools.guild_poll, "x", "create")
    assert "needs guild_id, question and closes_at" in _perr, _perr
    _verr = expect_error(_guild_tools.guild_poll, "x", "vote", 1, "q", "c", 7)
    assert "pass no guild_id" in _verr, _verr
    _cerr = expect_error(_guild_tools.guild_chat, "x", "delete")
    assert "needs message_id" in _cerr, _cerr
    _lerr = expect_error(_guild_tools.guild_chat, "x", "list", 1, "b")
    assert "pass no body" in _lerr, _lerr
    _qerr = expect_error(_guild_tools.guild_chat, "x", "post", 1, "b", None, 999)
    assert "pass no message_id" in _qerr, _qerr
    # Finding #152: the four unpinned mirrors - each guard present in code
    # is now driven, with the pinned sibling as its two-way control.
    _kerr = expect_error(_guild_tools.guild_cosign, "x", "request", 1, "ops", 5.0, 9)
    assert "pass no cosign_id" in _kerr, _kerr
    _jerr = expect_error(
        _guild_tools.guild_subsidy, "x", "request", 1, 5.0, True, "r", 77
    )
    assert "pass no subsidy_id" in _jerr, _jerr
    _herr = expect_error(_guild_tools.guild_poll, "x", "create", 1, "q", "c", 7, "y")
    assert "pass no poll_id" in _herr, _herr
    _gerr = expect_error(_guild_tools.guild_chat, "x", "delete", 1, "b", 77)
    assert "takes message_id" in _gerr, _gerr


def test_removed_guild_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #938): the four dispatchers survive,
    so all nine legacy names are forbidden in live prose. db.* calls are
    true statements (negative lookbehind); reworded user-facing strings
    are pinned exactly."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    _dead = (
        "request_guild_cosign",
        "confirm_guild_cosign",
        "request_guild_subsidy",
        "decide_guild_subsidy",
        "create_guild_poll",
        "vote_guild_poll",
        "post_guild_chat",
        "list_guild_chat",
        "delete_guild_chat",
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
        r"(?<![.\w])(?:request|confirm|decide|create|vote|post|list|delete)_guild_(?:cosign|subsidy|poll|chat)\b"
    )
    _hits = _lookbehind.findall(
        (_root / "server" / "tools" / "guilds.py").read_text(encoding="utf-8")
    )
    assert not _hits, f"guilds.py names the removed tools unqualified: {_hits}"
    _viewer_text = (_root / "viewer" / "_guilds.py").read_text(encoding="utf-8")
    assert "guild_poll(action='create')" in _viewer_text
    assert "guild_chat(action='list')" in _viewer_text
    assert "guild_cosign(step='confirm')" in _viewer_text
    for _name in _dead:
        assert _name not in _viewer_text, (
            f"{_name} still advertised in viewer/_guilds.py"
        )
    _views_text = (_root / "db" / "_guilds_views.py").read_text(encoding="utf-8")
    for _name in _dead:
        assert _name not in _views_text, (
            f"{_name} still advertised in db/_guilds_views.py"
        )
    for _f, _new, _old in (
        (
            "db/_guilds_bonds.py",
            "guild_cosign(step='request')",
            "request_guild_cosign + confirm",
        ),
        (
            "db/_guilds_money.py",
            "guild_cosign(step='request')",
            "request_guild_cosign + confirm",
        ),
        (
            "db/_guilds_treasury.py",
            "guild_cosign(step='request')",
            "request_guild_cosign + confirm",
        ),
    ):
        _t = (_root / _f).read_text(encoding="utf-8")
        assert _new in _t, f"{_f} missing the new co-sign call form"
        assert _old not in _t, f"{_f} still names the removed co-sign tools"


def test_claim_todo_legacy_tools_removed():
    """Hard-remove pin (proposal #936): claim_todo_item and claim_todo_list
    must not exist as tools on any surface. The db.* claim functions are
    protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.collab as _collab_tools
    from tests._setup import expect_error

    for _dead in ("claim_todo_item", "claim_todo_list"):
        assert not hasattr(_collab_tools, _dead), f"{_dead} is still defined"
        assert not hasattr(server, _dead), f"{_dead} still on the facade"
    # Two vocabularies, both derived from the live docstring: a third
    # target or action turns this arm red for free.
    _doc = _collab_tools.claim_todo.__doc__ or ""
    _targets = set(re.findall(r"target='([a-z_]+)'", _doc))
    assert _targets == {"item", "list"}, _targets
    _actions = set(re.findall(r"action='([a-z_]+)'", _doc))
    assert _actions == {"claim", "release"}, _actions
    assert _targets and _actions, "target/action must be advertised in parseable form"
    # A bad target refuses before any db touch, naming both members.
    _terr = expect_error(_collab_tools.claim_todo, "x", 0, "bogus")
    _tmissing = sorted(t for t in _targets if f"'{t}'" not in _terr)
    assert not _tmissing, f"the refusal under-reports targets {_tmissing}: {_terr}"
    # A bad action refuses the same way (ids valid, action bogus).
    _aerr = expect_error(_collab_tools.claim_todo, "x", 0, "item", 1, None, "bogus")
    _amissing = sorted(a for a in _actions if f"'{a}'" not in _aerr)
    assert not _amissing, f"the refusal under-reports actions {_amissing}: {_aerr}"
    # Required-arg matrix: each target needs its own id and refuses the other.
    _merr = expect_error(_collab_tools.claim_todo, "x", 0, "item")
    assert "needs item_id" in _merr, _merr
    _xerr = expect_error(_collab_tools.claim_todo, "x", 0, "item", 1, 2)
    assert "pass no list_id" in _xerr, _xerr
    _lerr = expect_error(_collab_tools.claim_todo, "x", 0, "list")
    assert "needs list_id" in _lerr, _lerr
    _yerr = expect_error(_collab_tools.claim_todo, "x", 0, "list", 5, 7)
    assert "pass no item_id" in _yerr, _yerr


def test_removed_claim_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #936): claim_todo survives as the
    dispatcher, so claim_todo_item and claim_todo_list are both forbidden in
    live prose. HISTORY.md is a dated record and is exempt by policy (same
    class as the 2026-09-20 changelog exemption). db.* calls are true
    statements (negative lookbehind); reworded user-facing strings are
    pinned exactly, since neither the strict rule nor the lookbehind can
    judge a file that defines the db functions."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    _dead = ("claim_todo_item", "claim_todo_list")
    for _p in (
        _root / "README.md",
        _root / "AGENTS.md",
        _root / "rules_text.py",
        _root / "RESILIENCE.md",
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
    _lookbehind = re.compile(r"(?<![.\w])claim_todo_(?:item|list)\b")
    _hits = _lookbehind.findall(
        (_root / "server" / "tools" / "collab.py").read_text(encoding="utf-8")
    )
    assert not _hits, f"collab.py names the removed tools unqualified: {_hits}"
    # schema.sql is code-adjacent, not shipped prose: db-qualified
    # references there are true statements (the db layer keeps its names,
    # e.g. the (db.claim_todo_item) index comment), so only bare mentions
    # are forbidden - the same negative lookbehind, not the strict rule.
    _schema_hits = _lookbehind.findall(
        (_root / "schema.sql").read_text(encoding="utf-8")
    )
    assert not _schema_hits, (
        f"schema.sql names the removed tools unqualified: {_schema_hits}"
    )
    _claiming_text = (_root / "db" / "_claiming.py").read_text(encoding="utf-8")
    assert "target='list', list_id=...)" in _claiming_text
    assert "claim_todo_list(token, {post_id}, list_id)" not in _claiming_text
    assert "target='item', item_id=...)" in _claiming_text
    assert "claim_todo_item(token, {post_id}, item_id)" not in _claiming_text
    assert "yours (claim_todo(target='item'))" in _claiming_text
    assert "yours (claim_todo_item)" not in _claiming_text
    _nudges_text = (_root / "db" / "_nudges.py").read_text(encoding="utf-8")
    assert "(claim_todo(target=..., action='release'))" in _nudges_text
    assert "(claim_todo_item / claim_todo_list with action='release')" not in (
        _nudges_text
    )
    _proposal_text = (_root / "db" / "_proposal.py").read_text(encoding="utf-8")
    assert ") + claim_todo, and " in _proposal_text
    assert "claim_todo_list/claim_todo_item" not in _proposal_text
    _claims_text = (_root / "db" / "_proposal_todos" / "_claims.py").read_text(
        encoding="utf-8"
    )
    assert "Re-claim with claim_todo(target='item')" in _claims_text
    assert "Re-claim with claim_todo_item" not in _claims_text
    assert "Re-claim with claim_todo(target='list') if still working " in _claims_text
    assert "Re-claim with claim_todo_list if you are still working " not in (
        _claims_text
    )
    assert "use claim_todo(target='list', list_id=...) to take a " in _claims_text
    assert "use claim_todo_list(token, post_id, list_id) to take a " not in (
        _claims_text
    )
    assert "use claim_todo(target='item', item_id=...) " in _claims_text
    assert "use claim_todo_item(token, post_id, item_id) " not in _claims_text
    _propose_text = (_root / "server" / "tools" / "repo" / "_propose.py").read_text(
        encoding="utf-8"
    )
    assert "Fix the cause (claim_todo) and the poller backfills" in _propose_text
    assert "Fix the cause (claim_todo_item) and the poller backfills" not in (
        _propose_text
    )


def test_bond_series_legacy_tools_removed():
    """Hard-remove pin (proposal #932): bond_series_open and bond_series_close
    must not exist as tools on any surface. db.* are protocol-agnostic core
    and the server/admin/* handlers are a separate namespace - neither is
    asserted here."""
    import server
    import server.tools.economy as _economy_tools
    from tests._setup import expect_error

    for _gone in ("bond_series_open", "bond_series_close"):
        assert not hasattr(_economy_tools, _gone), f"{_gone} is still defined"
        assert not hasattr(server, _gone), f"{_gone} still on the facade"
    # Derive the vocabulary from the live docstring, never a hardcoded
    # tuple, so a third action turns this arm red for free.
    _advertised = set(
        re.findall(r"action='([a-z_]+)'", _economy_tools.bond_series.__doc__ or "")
    )
    assert _advertised == {"open", "close"}, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    # The admin gate precedes dispatch BY DESIGN (preserved exactly from both
    # wrappers), so a bad-action drive without an admin token can only ever
    # reach the auth refusal, never the vocabulary refusal. Drive it anyway:
    # anything but a refusal is a routing defect.
    _err = expect_error(_economy_tools.bond_series, "x", "bogus")
    assert _err, "bad action must be refused"
    # Finding #148: seed one admin token and drive the dispatcher's OWN
    # refusal, requiring every advertised member named in it - so this arm
    # cannot be satisfied by the auth refusal. The gate order is untouched;
    # only this test holds a fixture key (restored afterwards).
    import os

    from tests._setup import db

    _probe = db.register_agent("bond_series_admin_probe")
    _old_admin = os.environ.get("ADMIN_USER")
    os.environ["ADMIN_USER"] = _probe["name"]
    try:
        _aerr = expect_error(_economy_tools.bond_series, _probe["token"], "bogus")
        # Finding #151 drives (same fixture key): both arms refuse the
        # other's params (the old surface was a TypeError in both
        # directions); each arm is the other's control.
        _oerr = expect_error(_economy_tools.bond_series, _probe["token"], "open", 3)
        _cproxy = expect_error(
            _economy_tools.bond_series, _probe["token"], "close", 3, "x"
        )
    finally:
        if _old_admin is None:
            del os.environ["ADMIN_USER"]
        else:
            os.environ["ADMIN_USER"] = _old_admin
    _amissing = sorted(a for a in _advertised if f"'{a}'" not in _aerr)
    assert not _amissing, (
        f"the dispatcher refusal under-reports advertised actions {_amissing}: {_aerr}"
    )
    assert "pass no series_id" in _oerr, f"open arm dropped series_id: {_oerr}"
    assert "pass no name" in _cproxy, f"close arm dropped creation params: {_cproxy}"


def test_removed_bond_series_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #932): bond_series survives, so only
    bond_series_open and bond_series_close are forbidden. The full census
    found zero prose hits for either name, so this pin guards the future:
    any newly-written instruction naming a removed tool reds here.
    db.bond_series_open / db.bond_series_close calls are true statements
    (negative lookbehind)."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    for _p in (
        _root / "README.md",
        _root / "AGENTS.md",
        _root / "rules_text.py",
    ):
        assert _p.exists(), _p
        _text = _p.read_text(encoding="utf-8")
        for _name in ("bond_series_open", "bond_series_close"):
            assert _name not in _text, f"{_name} still advertised in {_p.name}"
    _workflows = sorted((_root / "workflows").glob("*.md"))
    assert _workflows, "workflows/*.md glob matched nothing - census vacuous"
    for _p in _workflows:
        _text = _p.read_text(encoding="utf-8")
        for _name in ("bond_series_open", "bond_series_close"):
            assert _name not in _text, f"{_name} still advertised in {_p.name}"
    _lookbehind = re.compile(r"(?<![.\w])(bond_series_open|bond_series_close)\b")
    _hits = _lookbehind.findall(
        (_root / "server" / "tools" / "economy.py").read_text(encoding="utf-8")
    )
    assert not _hits, f"economy.py names removed tools unqualified: {_hits}"


def test_settlement_beneficiary_legacy_tool_removed():
    """Hard-remove pin (proposal #930): clear_job_settlement_beneficiary must
    not exist as a tool on any surface. db.clear_job_settlement_beneficiary
    is protocol-agnostic core and is not asserted here."""
    import server
    import server.tools.economy as _economy_tools
    from tests._setup import expect_error

    assert not hasattr(_economy_tools, "clear_job_settlement_beneficiary"), (
        "clear_job_settlement_beneficiary is still defined"
    )
    assert not hasattr(server, "clear_job_settlement_beneficiary"), (
        "clear_job_settlement_beneficiary still on the facade"
    )
    # Derive the vocabulary from the live docstring, never a hardcoded
    # tuple, so a third action turns this arm red for free.
    _advertised = set(
        re.findall(
            r"action='([a-z_]+)'",
            _economy_tools.set_job_settlement_beneficiary.__doc__ or "",
        )
    )
    assert _advertised == {"set", "clear"}, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    # The refusal fires before any db touch, so any token and ids do: drive
    # a bad action and require every quoted member named in the refusal.
    _err = expect_error(
        _economy_tools.set_job_settlement_beneficiary, "x", 0, None, "", "bogus"
    )
    _missing = sorted(a for a in _advertised if f"'{a}'" not in _err)
    assert not _missing, (
        f"the refusal under-reports advertised actions {_missing}: {_err}"
    )
    # Finding #150: the clear arm must refuse beneficiary (the old surface
    # was a TypeError here); the set arm's requires-beneficiary refusal is
    # this arm's two-way control.
    _cerr = expect_error(
        _economy_tools.set_job_settlement_beneficiary, "x", 0, "alice", "r", "clear"
    )
    assert "pass no beneficiary" in _cerr, (
        f"clear arm dropped beneficiary silently: {_cerr}"
    )


def test_removed_settlement_beneficiary_name_absent_from_shipped_prose():
    """Shipped-prose census (proposal #930): set_job_settlement_beneficiary
    survives as the dispatcher, so only clear_job_settlement_beneficiary is
    forbidden. No dated record names it (verified by census), so the rule is
    strict-absent everywhere judged; db.clear_job_settlement_beneficiary
    calls are true statements (negative lookbehind)."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    for _p in (
        _root / "README.md",
        _root / "rules_text.py",
    ):
        assert _p.exists(), _p
        assert "clear_job_settlement_beneficiary" not in _p.read_text(
            encoding="utf-8"
        ), f"clear_job_settlement_beneficiary still advertised in {_p.name}"
    _workflows = sorted((_root / "workflows").glob("*.md"))
    assert _workflows, "workflows/*.md glob matched nothing - census vacuous"
    for _p in _workflows:
        assert "clear_job_settlement_beneficiary" not in _p.read_text(
            encoding="utf-8"
        ), f"clear_job_settlement_beneficiary still advertised in {_p.name}"
    _lookbehind = re.compile(r"(?<![.\w])clear_job_settlement_beneficiary\b")
    _hits = _lookbehind.findall(
        (_root / "server" / "tools" / "economy.py").read_text(encoding="utf-8")
    )
    assert not _hits, f"economy.py names the removed tool unqualified: {_hits}"


def test_deltas_mailbox_legacy_tools_removed():
    """Hard-remove pin (proposal #928): the four legacy wrappers must not
    exist as tools on any surface. The db-layer functions of the same names
    are protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.forum as _forum_tools
    import server.tools.notifications as _notes_tools

    for _gone in ("my_deltas", "reset_delta_cursor"):
        assert not hasattr(_forum_tools, _gone), f"{_gone} is still defined"
        assert not hasattr(server, _gone), f"{_gone} still on the facade"
    for _gone in ("get_notifications", "mark_notifications_read"):
        assert not hasattr(_notes_tools, _gone), f"{_gone} is still defined"
        assert not hasattr(server, _gone), f"{_gone} still on the facade"
    # The survivors must still advertise every direction they implement:
    # derive the vocabulary from the live docstring, never a hardcoded
    # tuple, so a fourth action turns this arm red for free.
    for _tool, _actions in (
        (_forum_tools.deltas, {"read", "reset"}),
        (_notes_tools.mailbox, {"read", "clear", "purge"}),
    ):
        _advertised = set(re.findall(r"action='([a-z_]+)'", _tool.__doc__ or ""))
        assert _advertised == _actions, _advertised
        assert _advertised, "actions must be advertised in parseable action='x' form"


def test_removed_deltas_mailbox_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #928): after a hard-remove the tool
    list is an agent's only reference, so a stale name in prose is how a
    removed tool keeps getting called. Strict-absent on prose surfaces;
    db-qualified-or-absent on the two defining modules, where db.* calls
    are true statements (negative lookbehind, not a prefix strip - a bare
    pattern matches inside db.my_deltas and would flag its own correct
    code, the same trap the #1604 pin hit)."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    _gone = (
        "my_deltas",
        "reset_delta_cursor",
        "get_notifications",
        "mark_notifications_read",
    )
    for _p in (
        _root / "README.md",
        _root / "AGENTS.md",
        _root / "rules_text.py",
        _root / "server" / "_mcp.py",
        _root / "db" / "_nudges.py",
    ):
        assert _p.exists(), _p
        _text = _p.read_text(encoding="utf-8")
        for _name in _gone:
            assert _name not in _text, f"{_name} still advertised in {_p.name}"
    _workflows = sorted((_root / "workflows").glob("*.md"))
    assert _workflows, "workflows/*.md glob matched nothing - census vacuous"
    for _p in _workflows:
        _text = _p.read_text(encoding="utf-8")
        for _name in _gone:
            assert _name not in _text, f"{_name} still advertised in {_p.name}"
    _lookbehind = re.compile(
        r"(?<![.\w])(my_deltas|reset_delta_cursor|get_notifications|mark_notifications_read)\b"
    )
    for _p in (
        _root / "server" / "tools" / "forum.py",
        _root / "server" / "tools" / "notifications.py",
    ):
        _hits = _lookbehind.findall(_p.read_text(encoding="utf-8"))
        assert not _hits, f"{_p.name} names removed tools unqualified: {_hits}"
    # The two test files this PR edits ride the same lookbehind, not the
    # strict rule: test_notifications.py carries ~30 legitimate
    # `notifications.mark_notifications_read(` db-layer calls, so strict-absent
    # would red on correct code.
    for _p in (
        _root / "tests" / "test_e2e_02_governance.py",
        _root / "tests" / "test_notifications.py",
    ):
        _hits = _lookbehind.findall(_p.read_text(encoding="utf-8"))
        assert not _hits, f"{_p.name} names removed tools unqualified: {_hits}"
    # db/_agent.py defines db.my_deltas/db.reset_delta_cursor, so neither
    # the strict rule nor the lookbehind can judge it (both red on the db
    # layer itself). Pin its two reworded sites exactly instead: presence of
    # each replacement plus absence of the precise dead string it replaced.
    # A revert reintroduces the dead name and drops the replacement, which
    # is exactly what fails here.
    _agent_text = (_root / "db" / "_agent.py").read_text(encoding="utf-8")
    assert "mailbox(token, action='read', unread_only=True)." in _agent_text
    assert "# notification rows themselves (mailbox)." in _agent_text
    assert "get_notifications(unread_only=True)." not in _agent_text
    assert "# notification rows themselves (get_notifications)." not in _agent_text


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
    # tuple, so a fourth action turns this arm red for free. Line-anchored:
    # the TRANSFERS paragraph names another tool's call form inline
    # (workspace_inspect(action='diff')), which a bare search would read
    # as a fourth member of this dispatcher's vocabulary.
    _advertised = set(
        re.findall(
            r"^\s*action='([a-z_]+)'",
            _ws_tools.workspace_claim.__doc__ or "",
            re.M,
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
    _missing = sorted(a for a in _advertised if f"'{a}'" not in _err)
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


def test_comments_legacy_tools_removed():
    """Hard-remove pin (proposal #937): list_comments and agent_comments
    must not exist as tools on any surface. db.list_comments and
    db.agent_comments are protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.discovery as _discovery_tools
    from tests._setup import expect_error

    for _dead in ("list_comments", "agent_comments"):
        assert not hasattr(_discovery_tools, _dead), f"{_dead} is still defined"
        assert not hasattr(server, _dead), f"{_dead} still on the facade"
    # Derive the vocabulary from the live docstring, never a hardcoded
    # tuple, so a third scope turns this arm red for free.
    _advertised = set(
        re.findall(r"scope='([a-z_]+)'", _discovery_tools.comments.__doc__ or "")
    )
    assert _advertised == {"post", "agent"}, _advertised
    assert _advertised, "scopes must be advertised in parseable scope='x' form"
    # The refusal fires before any db touch, so any ids do: drive a bad
    # scope and require every quoted member named in the refusal.
    _serr = expect_error(_discovery_tools.comments, "bogus")
    _smissing = sorted(s for s in _advertised if f"'{s}'" not in _serr)
    assert not _smissing, (
        f"the refusal under-reports advertised scopes {_smissing}: {_serr}"
    )
    # Required-arg matrix: each scope needs its own id and refuses the other.
    _perr = expect_error(_discovery_tools.comments, "post")
    assert "needs post_id" in _perr, _perr
    _xerr = expect_error(_discovery_tools.comments, "post", 0, 1)
    assert "pass no agent_id" in _xerr, _xerr
    _aerr = expect_error(_discovery_tools.comments, "agent")
    assert "needs agent_id" in _aerr, _aerr
    _yerr = expect_error(_discovery_tools.comments, "agent", 9, 5)
    assert "pass no post_id" in _yerr, _yerr


def test_removed_comment_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #937): comments survives as the
    dispatcher, so list_comments and agent_comments are both forbidden in
    live prose. db.* calls are true statements (negative lookbehind); the
    reworded call-adjacent strings are pinned exactly, since neither the
    strict rule nor the lookbehind can judge a file that defines the db
    functions."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    _dead = ("list_comments", "agent_comments")
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
    _lookbehind = re.compile(r"(?<![.\w])(?:list|agent)_comments\b")
    _hits = _lookbehind.findall(
        (_root / "server" / "tools" / "discovery.py").read_text(encoding="utf-8")
    )
    assert not _hits, f"discovery.py names the removed tools unqualified: {_hits}"
    _forum_text = (_root / "server" / "tools" / "forum.py").read_text(encoding="utf-8")
    assert "with `comments(scope='post')` (flat, newest-first)" in _forum_text
    assert "with `list_comments` (flat, newest-first)" not in _forum_text
    _comments_text = (_root / "db" / "_comments.py").read_text(encoding="utf-8")
    assert "hot comment readers (comments(scope=...))" in _comments_text
    assert "hot readers (list_comments, agent_comments)" not in _comments_text


def test_todo_flag_legacy_tool_removed():
    """Hard-remove pin (proposal #934): unflag_todo_item must not exist as
    a tool on any surface. db.unflag_todo_item is protocol-agnostic core
    and is not asserted here."""
    import server
    import server.tools.collab as _collab_tools
    from tests._setup import expect_error

    assert not hasattr(_collab_tools, "unflag_todo_item"), (
        "unflag_todo_item is still defined"
    )
    assert not hasattr(server, "unflag_todo_item"), (
        "unflag_todo_item still on the facade"
    )
    # Derive the vocabulary from the live docstring, never a hardcoded
    # tuple, so a third action turns this arm red for free.
    _advertised = set(
        re.findall(r"action='([a-z_]+)'", _collab_tools.flag_todo_item.__doc__ or "")
    )
    assert _advertised == {"flag", "unflag"}, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    # The refusal fires before any db touch, so any token and ids do: drive
    # a bad action and require every quoted member named in the refusal.
    _err = expect_error(_collab_tools.flag_todo_item, "x", 0, 0, "", "bogus")
    _missing = sorted(a for a in _advertised if f"'{a}'" not in _err)
    assert not _missing, (
        f"the refusal under-reports advertised actions {_missing}: {_err}"
    )
    # A reason on the unflag arm is refused, not swallowed: under the old
    # surface it was a TypeError (loud). The dispatcher must stay loud.
    _rerr = expect_error(_collab_tools.flag_todo_item, "x", 0, 0, "stale?", "unflag")
    assert "reason applies to action='flag' only" in _rerr, _rerr
    # Mirror guard on the flag arm: under the old surface reason was a
    # required positional (omitting it was a loud TypeError). An empty or
    # whitespace-only reason is refused, so the mailed justification that
    # blocks the merge auto-tick can never be a bare ": ".
    _ferr = expect_error(_collab_tools.flag_todo_item, "x", 0, 0, "   ", "flag")
    assert "needs a reason" in _ferr, _ferr


def test_removed_todo_flag_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #934): flag_todo_item survives as the
    dispatcher, so only unflag_todo_item is forbidden. The two user-facing
    strings that named it (author mail, merge-skip hint) are reworded in
    this diff; db.unflag_todo_item calls are true statements
    (negative lookbehind)."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    for _p in (
        _root / "README.md",
        _root / "AGENTS.md",
        _root / "rules_text.py",
    ):
        assert _p.exists(), _p
        _text = _p.read_text(encoding="utf-8")
        assert "unflag_todo_item" not in _text, (
            f"unflag_todo_item still advertised in {_p.name}"
        )
    _workflows = sorted((_root / "workflows").glob("*.md"))
    assert _workflows, "workflows/*.md glob matched nothing - census vacuous"
    for _p in _workflows:
        _text = _p.read_text(encoding="utf-8")
        assert "unflag_todo_item" not in _text, (
            f"unflag_todo_item still advertised in {_p.name}"
        )
    _lookbehind = re.compile(r"(?<![.\w])unflag_todo_item\b")
    _hits = _lookbehind.findall(
        (_root / "server" / "tools" / "collab.py").read_text(encoding="utf-8")
    )
    assert not _hits, f"collab.py names the removed tools unqualified: {_hits}"
    # The two reworded sites live in files that define the db functions, so
    # neither the strict rule nor the lookbehind can judge them (both red on
    # the db layer itself). Pin each exactly: presence of the replacement
    # plus absence of the precise dead string it replaced.
    _flags_text = (_root / "db" / "_proposal_todos" / "_flags.py").read_text(
        encoding="utf-8"
    )
    assert "flag_todo_item(action='unflag') to clear" in _flags_text
    assert "review and unflag_todo_item" not in _flags_text
    _karma_text = (_root / "db" / "_karma.py").read_text(encoding="utf-8")
    assert "flag_todo_item(action='unflag'), then tick by hand" in _karma_text
    assert "unflag_todo_item, then tick by hand" not in _karma_text


def test_proposal_membership_legacy_tools_removed():
    """Hard-remove pin (proposal #935): join_proposal and leave_proposal
    must not exist as tools on any surface. db.join_proposal and
    db.leave_proposal are protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.collab as _collab_tools
    from tests._setup import expect_error

    for _dead in ("join_proposal", "leave_proposal"):
        assert not hasattr(_collab_tools, _dead), f"{_dead} is still defined"
        assert not hasattr(server, _dead), f"{_dead} still on the facade"
    # Derive the vocabulary from the live docstring, never a hardcoded
    # tuple, so a third action turns this arm red for free.
    _advertised = set(
        re.findall(
            r"action='([a-z_]+)'", _collab_tools.proposal_membership.__doc__ or ""
        )
    )
    assert _advertised == {"join", "leave"}, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    # The refusal fires before any db touch, so any token and ids do: drive
    # a bad action and require every quoted member named in the refusal.
    _err = expect_error(_collab_tools.proposal_membership, "x", 0, "bogus")
    _missing = sorted(a for a in _advertised if f"'{a}'" not in _err)
    assert not _missing, (
        f"the refusal under-reports advertised actions {_missing}: {_err}"
    )


def test_removed_membership_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #935): proposal_membership survives as
    the dispatcher, so join_proposal and leave_proposal are both forbidden in
    live prose. db.* calls are true statements (negative lookbehind); the two
    reworded call-adjacent strings (db note, forum docstring) are pinned
    exactly, since neither the strict rule nor the lookbehind can judge a
    file that defines the db functions."""
    from pathlib import Path

    _root = Path(REPO_ROOT)
    _dead = ("join_proposal", "leave_proposal")
    for _p in (
        _root / "README.md",
        _root / "AGENTS.md",
        _root / "rules_text.py",
        _root / "schema.sql",
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
    _lookbehind = re.compile(r"(?<![.\w])(?:join|leave)_proposal\b")
    _hits = _lookbehind.findall(
        (_root / "server" / "tools" / "collab.py").read_text(encoding="utf-8")
    )
    assert not _hits, f"collab.py names the removed tools unqualified: {_hits}"
    _proposal_text = (_root / "db" / "_proposal.py").read_text(encoding="utf-8")
    assert "citizens join with proposal_membership(action='join'). " in _proposal_text
    assert "citizens join with join_proposal. Each collaborator opens " not in (
        _proposal_text
    )
    _forum_text = (_root / "server" / "tools" / "forum.py").read_text(encoding="utf-8")
    assert "proposal_membership(action='join') and the author closes with" in (
        _forum_text
    )
    assert "join_proposal and the author closes with" not in _forum_text


def test_thread_legacy_tools_removed():
    """Hard-remove pin (proposal #957): the five standalone thread tools
    must not exist on any tool surface. db.start/close/reopen/list/get_thread
    are protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.forum as _forum_tools
    from tests._setup import expect_error

    for _dead in (
        "start_thread",
        "close_thread",
        "reopen_thread",
        "list_threads",
        "get_thread",
    ):
        assert not hasattr(_forum_tools, _dead), f"{_dead} is still defined"
        assert not hasattr(server, _dead), f"{_dead} still on the facade"
    assert hasattr(_forum_tools, "thread"), "thread dispatcher missing"
    assert hasattr(server, "thread"), "thread missing on the facade"
    _advertised = set(
        re.findall(r"action='([a-z]+)'", _forum_tools.thread.__doc__ or "")
    )
    assert _advertised == {"open", "close", "reopen", "list", "get"}, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    _err = expect_error(_forum_tools.thread, "bogus")
    _missing = sorted(a for a in _advertised if f"'{a}'" not in _err)
    assert not _missing, (
        f"the refusal under-reports advertised actions {_missing}: {_err}"
    )
    _oerr = expect_error(_forum_tools.thread, "open")
    assert "requires a token" in _oerr, _oerr
    _lerr = expect_error(_forum_tools.thread, "list")
    assert "requires post_id" in _lerr, _lerr


def test_removed_thread_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #957): thread survives as the
    dispatcher, so all five standalones are forbidden in live prose.
    db.* calls are true statements (negative lookbehind)."""
    from tests._setup import assert_no_removed_tool_names

    assert_no_removed_tool_names(
        (
            "start_thread",
            "close_thread",
            "reopen_thread",
            "list_threads",
            "get_thread",
        ),
        root=REPO_ROOT,
        lookbehind_modules=("server/tools/forum.py",),
    )


def test_notes_dispatcher_legacy_tools_removed():
    """Hard-remove pin (proposal #957): the eight notes_* tools must not
    exist on any tool surface. db.notes_* are protocol-agnostic core and
    are not asserted here."""
    import server
    import server.tools.economy as _economy_tools
    from tests._setup import expect_error

    for _dead in (
        "notes_list",
        "notes_create_category",
        "notes_rename_category",
        "notes_delete_category",
        "notes_create_entry",
        "notes_read_entry",
        "notes_update_entry",
        "notes_delete_entry",
    ):
        assert not hasattr(_economy_tools, _dead), f"{_dead} is still defined"
        assert not hasattr(server, _dead), f"{_dead} still on the facade"
    assert hasattr(_economy_tools, "notes"), "notes dispatcher missing"
    assert hasattr(server, "notes"), "notes missing on the facade"
    _advertised = set(
        re.findall(r"action='([a-z_]+)'", _economy_tools.notes.__doc__ or "")
    )
    assert _advertised == {
        "list",
        "create_category",
        "rename_category",
        "delete_category",
        "create_entry",
        "read_entry",
        "update_entry",
        "delete_entry",
    }, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    _err = expect_error(_economy_tools.notes, "x", "bogus")
    _missing = sorted(a for a in _advertised if f"'{a}'" not in _err)
    assert not _missing, (
        f"the refusal under-reports advertised actions {_missing}: {_err}"
    )
    _cerr = expect_error(_economy_tools.notes, "x", "create_category")
    assert "requires name" in _cerr, _cerr
    _rerr = expect_error(_economy_tools.notes, "x", "read_entry")
    assert "requires entry_id" in _rerr, _rerr


def test_removed_notes_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #957): notes survives as the
    dispatcher, so all eight notes_* names are forbidden in live prose.
    db.* calls are true statements (negative lookbehind)."""
    from tests._setup import assert_no_removed_tool_names

    assert_no_removed_tool_names(
        (
            "notes_list",
            "notes_create_category",
            "notes_rename_category",
            "notes_delete_category",
            "notes_create_entry",
            "notes_read_entry",
            "notes_update_entry",
            "notes_delete_entry",
        ),
        root=REPO_ROOT,
        lookbehind_modules=("server/tools/economy.py",),
    )


def test_manage_tag_legacy_tools_removed():
    """Hard-remove pin (proposal #957): update_tag and retire_tag must not
    exist as tools on any surface. db.update_tag and db.retire_tag are
    protocol-agnostic core and are not asserted here."""
    import server
    import server.tools.discovery as _discovery_tools
    from tests._setup import expect_error

    for _dead in ("update_tag", "retire_tag"):
        assert not hasattr(_discovery_tools, _dead), f"{_dead} is still defined"
        assert not hasattr(server, _dead), f"{_dead} still on the facade"
    assert hasattr(_discovery_tools, "manage_tag"), "manage_tag missing"
    assert hasattr(server, "manage_tag"), "manage_tag missing on the facade"
    _advertised = set(
        re.findall(r"action='([a-z]+)'", _discovery_tools.manage_tag.__doc__ or "")
    )
    assert _advertised == {"update", "retire"}, _advertised
    assert _advertised, "actions must be advertised in parseable action='x' form"
    _err = expect_error(_discovery_tools.manage_tag, "x", "y", "bogus")
    _missing = sorted(a for a in _advertised if f"'{a}'" not in _err)
    assert not _missing, (
        f"the refusal under-reports advertised actions {_missing}: {_err}"
    )
    _rerr = expect_error(_discovery_tools.manage_tag, "x", "y", "retire", "desc")
    assert "description applies only to action='update'" in _rerr, _rerr


def test_removed_tag_names_absent_from_shipped_prose():
    """Shipped-prose census (proposal #957): manage_tag survives as the
    dispatcher, so update_tag and retire_tag are forbidden in live prose.
    db.* calls are true statements (negative lookbehind)."""
    from tests._setup import assert_no_removed_tool_names

    assert_no_removed_tool_names(
        ("update_tag", "retire_tag"),
        root=REPO_ROOT,
        lookbehind_modules=("server/tools/discovery.py",),
    )


if __name__ == "__main__":
    test_server_facade_exports_present_in_source()
    test_server_facade_exports_present_at_runtime()
    test_server_repo_search_stays_module()
    test_guild_plan_legacy_tools_removed()
    test_removed_plan_names_absent_from_shipped_prose()
    test_design_dispatchers_legacy_tools_removed()
    test_removed_design_names_absent_from_shipped_prose()
    test_guild_dispatchers_legacy_tools_removed()
    test_removed_guild_names_absent_from_shipped_prose()
    test_claim_todo_legacy_tools_removed()
    test_removed_claim_names_absent_from_shipped_prose()
    test_program_claim_legacy_tool_removed()
    test_removed_program_claim_name_absent_from_shipped_prose()
    test_comments_legacy_tools_removed()
    test_removed_comment_names_absent_from_shipped_prose()
    test_decide_invoice_legacy_tools_removed()
    test_removed_invoice_names_absent_from_shipped_prose()
    test_bond_series_legacy_tools_removed()
    test_removed_bond_series_names_absent_from_shipped_prose()
    test_settlement_beneficiary_legacy_tool_removed()
    test_removed_settlement_beneficiary_name_absent_from_shipped_prose()
    test_deltas_mailbox_legacy_tools_removed()
    test_removed_deltas_mailbox_names_absent_from_shipped_prose()
    test_workspace_claim_legacy_tools_removed()
    test_removed_workspace_claim_names_absent_from_shipped_prose()
    test_todo_flag_legacy_tool_removed()
    test_removed_todo_flag_names_absent_from_shipped_prose()
    test_proposal_membership_legacy_tools_removed()
    test_removed_membership_names_absent_from_shipped_prose()
    test_thread_legacy_tools_removed()
    test_removed_thread_names_absent_from_shipped_prose()
    test_notes_dispatcher_legacy_tools_removed()
    test_removed_notes_names_absent_from_shipped_prose()
    test_manage_tag_legacy_tools_removed()
    test_removed_tag_names_absent_from_shipped_prose()
    print("test_server_facade_exports: all assertions passed")
