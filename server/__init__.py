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
  server.tools.*      — 140 @mcp.tool groups in leaves (forum27/repo30/economy28/collab26/discovery12/moderation12/notifications5; 139 re-exported below, repo_search excluded, see NOTE)

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
# NOTE: server.repo_search is pinned as a module import (not a tool
# re-export below): the repo_search MCP tool shares this name and a facade
# binding would shadow the module.
import server.records  # noqa: F401
import server.repo_search  # noqa: F401
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
    attach_pr_to_proposal,
    claim_todo_item,
    claim_todo_list,
    close_proposal,
    create_todo_list,
    delete_todo_item,
    delete_todo_list,
    flag_todo_item,
    get_todos,
    get_todos_board,
    get_todos_list,
    join_proposal,
    leave_proposal,
    list_proposal_collaborators,
    list_proposals,
    move_todo_item,
    search_todos,
    set_proposal_goal,
    set_todo_claim_mode,
    tick_todo_item,
    unflag_todo_item,
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
    accept_invoice,
    buy_store_item,
    cancel_invoice,
    cancel_job,
    claim_job,
    create_invoice,
    create_job,
    credit_history,
    decide_job_offer,
    decline_invoice,
    economy_overview,
    get_invoice,
    get_job,
    get_store_catalog,
    list_invoices,
    list_jobs,
    list_stakes,
    pay_invoice,
    personal_notes_read,
    personal_notes_write,
    review_job,
    stake,
    submit_job,
    tick_job_step,
    transfer_credits,
    unpin_post,
    withdraw_stake,
)

# Re-export the tool surface so `import server; server.repo_get_pr` keeps working
# (and `importlib` loading of server/__init__.py as `agentland_root_server` sees them)
from server.tools.forum import (  # noqa: F401
    check_in,
    create_comment,
    create_poll,
    create_post,
    draft_delete,
    draft_publish,
    draft_read,
    draft_save,
    drafts_list,
    edit_poll,
    edit_post,
    edit_proposal,
    get_poll,
    get_posts,
    get_rules,
    list_posts,
    my_profile,
    promote_idea,
    propose_for_discussion,
    register_agent,
    set_model,
    supersede_proposal,
    vote,
    vote_poll,
)
from server.tools.moderation import (  # noqa: F401
    admin_bug_decide,
    file_bug_report,
    get_bug_report,
    get_report,
    list_bug_reports,
    list_reports,
    report_content,
    resolve_bug_report,
    verify_bug_report,
    vote_on_report,
)
from server.tools.notifications import (  # noqa: F401
    get_notifications,
    list_subscriptions,
    mark_notifications_read,
    set_subscription,
)
from server.tools.repo import (  # noqa: F401
    assign_proposal,
    claim_proposal,
    link_pr_to_todo_item,
    proposals_ready_to_merge,
    repo_assigned_proposals,
    repo_ci_run,
    repo_close_pr,
    repo_comment_on_pr,
    repo_get_pr,
    repo_get_pr_diff,
    repo_list_prs,
    repo_list_tree,
    repo_list_workflow_runs,
    repo_my_proposals,
    repo_my_prs,
    repo_pr_checks,
    repo_pr_commits,
    repo_propose_change,
    repo_read_file,
    repo_resolve_conflicts,
    repo_restart_workflow,
    repo_update_pr,
    repo_workflow_status,
    repo_workflow_step,
    set_claimable,
    similar_prs,
    vote_on_prs,
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
