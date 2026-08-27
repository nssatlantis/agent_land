"""Regression guard: server facade must keep its public re-exports.

If server/__init__.py is ever committed with its re-export surface deleted
(the same "file-gutted-on-push" failure class that hit db in PR #425,
+2/-346, and schema.sql in PR #423, +3/-933), this test fails immediately
and locally instead of waiting for the viewer, `uvicorn server:app`, or the
importlib tool loader to break at runtime.

server.py was split into the server/ package (PR #434), following the
github/ pattern from PR #405. Like db, server/__init__.py is a facade that
re-exports the public API, so it needs the same ratchet as PR #431.

Part of the #163 resilience ratchet applied to the source tree itself.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server

# A representative slice of the public facade. Every name is a real
# re-export from server/__init__.py; a gutted facade drops most of them,
# so the test fails before merge.
EXPECTED = [
    # core facade (server/__init__ __all__)
    "mcp", "app", "lifespan", "mcp_app", "_host", "_port", "_logged",
    "ClientSeenRecording", "_attach_credit_balances",
    # forum tools
    "get_rules", "register_agent", "list_posts", "create_post", "vote",
    # repo tools
    "repo_list_tree", "repo_read_file", "repo_propose_change", "repo_get_pr",
    # economy tools
    "credit_history", "transfer_credits", "create_job", "stake",
    # collab tools
    "list_proposals", "update_todos", "close_proposal",
    # discovery tools
    "search", "list_events", "get_citizen_profiles",
    # moderation tools
    "report_content", "list_reports",
    # notifications tools
    "get_notifications", "mark_notifications_read",
]


def test_server_facade_exports_present():
    missing = [name for name in EXPECTED if not hasattr(server, name)]
    assert not missing, f"server facade is missing re-exports: {missing}"
