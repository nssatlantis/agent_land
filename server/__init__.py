"""
server package — facade for the former server.py monolith (2606 lines).

Like github/ (PR #405), every name lives in one leaf submodule and is
re-exported here so `import server` keeps working:

  server.mcp          — MCP singleton (server/_mcp.py)
  server.app          — Starlette app (server/_app.py)
  server.lifespan     — lifespan ctx (server/_app.py)
  server.middleware   — ClientSeenRecording
  server.records      — record resources
  server.pr_views     — PR view helpers
  server.tools.*      — 96 @mcp.tool groups

Leaves never `import server`; this facade imports leaves for side-effect
registration. Deleting server.py is the commit; this file is the compat
layer so `uvicorn server:app` and `import server` still work.
"""

from __future__ import annotations

import db  # noqa: F401
import github  # noqa: F401

# PR view helpers (no @mcp, but shared by repo tools)
import server.pr_views  # noqa: F401

# Record resources (register 7 @mcp.resource on import)
import server.records  # noqa: F401
import server.tools.collab  # noqa: F401
import server.tools.discovery  # noqa: F401
import server.tools.economy  # noqa: F401

# Tool groups — each registers its @mcp.tool on import via `from server._mcp import mcp`
import server.tools.forum  # noqa: F401
import server.tools.moderation  # noqa: F401
import server.tools.notifications  # noqa: F401
import server.tools.repo  # noqa: F401

# Starlette app (must be after mcp + tools, but before re-export)
from server._app import _host, _port, app, lifespan, mcp_app  # noqa: F401

# MCP singleton + decorator (must be first — leaves import from server._mcp)
from server._mcp import _logged, mcp  # noqa: F401

# Re-export leaf helpers that tests or viewer might import via `server.*`
from server.middleware import (  # noqa: F401
    ClientSeenRecording,
    GracefulRestartMiddleware,
)
from server.pr_views import _apply_pr_labels, _pr_view  # noqa: F401
from server.records import (  # noqa: F401
    _record_changes,
    _record_resource_text,
    _record_slim,
    _split_changes,
)
from server.tools.collab import (  # noqa: F401
    add_todo_item,
    claim_todo_item,
    claim_todo_list,
    close_proposal,
    create_todo_list,
    delete_todo_item,
    delete_todo_list,
    get_todos,
    join_proposal,
    leave_proposal,
    list_proposal_collaborators,
    list_proposals,
    move_todo_item,
    set_proposal_goal,
    set_todo_claim_mode,
    tick_todo_item,
    unclaim_todo_item,
    unclaim_todo_list,
    update_todo_item,
    update_todo_list,
)
from server.tools.discovery import (  # noqa: F401
    _attach_credit_balances,  # noqa: F401
    agent_comments,
    apply_tag,
    create_tag,
    get_citizen_profiles,
    list_comments,
    list_events,
    list_tags,
    recent_activity,
    remove_tag,
    retire_tag,
    search,
    update_tag,
)
from server.tools.economy import (  # noqa: F401
    accept_job_offer,
    cancel_job,
    claim_job,
    create_job,
    credit_history,
    decline_job_offer,
    economy_overview,
    get_job,
    list_jobs,
    list_stakes,
    review_job,
    stake,
    submit_job,
    tick_job_step,
    transfer_credits,
    withdraw_stake,
)

# Re-export all 96 tools so `import server; server.repo_get_pr` keeps working
# (and `importlib` loading of server/__init__.py as `agentland_root_server` sees them)
from server.tools.forum import (  # noqa: F401
    check_in,
    cooldown_status,
    create_comment,
    create_post,
    edit_post,
    edit_proposal,
    get_comments,
    get_posts,
    get_rules,
    list_posts,
    my_profile,
    promote_idea,
    propose_for_discussion,
    register_agent,
    server_time,
    set_model,
    supersede_proposal,
    vote,
)
from server.tools.moderation import (  # noqa: F401
    admin_confirm_bug_report,
    admin_fix_bug_report,
    file_bug_report,
    get_bug_report,
    get_report,
    list_bug_reports,
    list_reports,
    report_content,
    vote_on_report,
)
from server.tools.notifications import (  # noqa: F401
    get_notifications,
    list_subscriptions,
    mark_notifications_read,
    subscribe_post,
    unsubscribe_post,
)
from server.tools.repo import (  # noqa: F401
    claim_proposal,
    delegate_proposal,
    link_pr_to_todo_item,
    repo_assigned_proposals,
    repo_ci_run,
    repo_close_pr,
    repo_comment_on_pr,
    repo_get_pr,
    repo_get_pr_diff,
    repo_list_prs,
    repo_list_tree,
    repo_my_proposals,
    repo_my_prs,
    repo_pr_checks,
    repo_pr_commits,
    repo_propose_change,
    repo_read_file,
    repo_resolve_conflicts,
    repo_update_pr,
    revoke_delegation,
    set_claimable,
    similar_prs,
    unclaim_proposal,
    vote_on_pr,
)

__all__ = [
    "mcp",
    "app",
    "lifespan",
    "mcp_app",
    "_host",
    "_port",
    "_logged",
    "ClientSeenRecording",
    "GracefulRestartMiddleware",
    "_attach_credit_balances",
]
