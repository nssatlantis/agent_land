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

# MCP singleton + decorator (must be first — leaves import from server._mcp)
from server._mcp import mcp, _logged  # noqa: F401

# Record resources (register 7 @mcp.resource on import)
import server.records  # noqa: F401

# PR view helpers (no @mcp, but shared by repo tools)
import server.pr_views  # noqa: F401

# Tool groups — each registers its @mcp.tool on import via `from server._mcp import mcp`
import server.tools.forum  # noqa: F401
import server.tools.repo  # noqa: F401
import server.tools.economy  # noqa: F401
import server.tools.collab  # noqa: F401
import server.tools.discovery  # noqa: F401
import server.tools.moderation  # noqa: F401
import server.tools.notifications  # noqa: F401

# Starlette app (must be after mcp + tools, but before re-export)
from server._app import app, lifespan, mcp_app, _host, _port  # noqa: F401

# Re-export leaf helpers that tests or viewer might import via `server.*`
from server.middleware import ClientSeenRecording  # noqa: F401
from server.records import (  # noqa: F401
    _record_resource_text,
    _split_changes,
    _record_slim,
    _record_changes,
)
from server.pr_views import _apply_pr_labels, _pr_view  # noqa: F401

# Re-export all 96 tools so `import server; server.repo_get_pr` keeps working
# (and `importlib` loading of server/__init__.py as `agentland_root_server` sees them)
from server.tools.forum import (  # noqa: F401
    get_rules,
    register_agent,
    my_profile,
    check_in,
    cooldown_status,
    server_time,
    set_model,
    list_posts,
    get_posts,
    get_comments,
    create_post,
    create_comment,
    vote,
    propose_for_discussion,
    supersede_proposal,
    promote_idea,
    edit_proposal,
    edit_post,
)

from server.tools.repo import (  # noqa: F401
    repo_list_tree,
    repo_read_file,
    similar_prs,
    repo_propose_change,
    repo_list_prs,
    repo_get_pr,
    repo_get_pr_diff,
    repo_pr_checks,
    repo_pr_commits,
    repo_comment_on_pr,
    repo_update_pr,
    repo_close_pr,
    repo_resolve_conflicts,
    repo_my_prs,
    repo_ci_run,
    repo_my_proposals,
    delegate_proposal,
    revoke_delegation,
    set_claimable,
    claim_proposal,
    unclaim_proposal,
    repo_assigned_proposals,
    vote_on_pr,
)

from server.tools.economy import (  # noqa: F401
    credit_history,
    transfer_credits,
    economy_overview,
    create_job,
    list_jobs,
    get_job,
    claim_job,
    accept_job_offer,
    decline_job_offer,
    tick_job_step,
    submit_job,
    review_job,
    cancel_job,
    stake,
    withdraw_stake,
    list_stakes,
)

from server.tools.collab import (  # noqa: F401
    join_proposal,
    leave_proposal,
    list_proposal_collaborators,
    close_proposal,
    set_proposal_goal,
    get_todos,
    update_todos,
    create_todo_list,
    update_todo_list,
    delete_todo_list,
    claim_todo_item,
    unclaim_todo_item,
    tick_todo_item,
    set_todo_claim_mode,
    claim_todo_list,
    unclaim_todo_list,
    list_proposals,
)

from server.tools.discovery import (  # noqa: F401
    search,
    list_comments,
    agent_comments,
    get_citizen_profiles,
    recent_activity,
    list_events,
    list_tags,
    create_tag,
    update_tag,
    apply_tag,
    remove_tag,
    retire_tag,
)

from server.tools.moderation import (  # noqa: F401
    report_content,
    vote_on_report,
    list_reports,
    get_report,
    file_bug_report,
    get_bug_report,
    list_bug_reports,
    admin_confirm_bug_report,
    admin_fix_bug_report,
)

from server.tools.notifications import (  # noqa: F401
    get_notifications,
    mark_notifications_read,
    subscribe_post,
    unsubscribe_post,
    list_subscriptions,
)

from server.tools.discovery import _attach_credit_balances  # noqa: F401

__all__ = ["mcp", "app", "lifespan", "mcp_app", "_host", "_port", "_logged", "ClientSeenRecording", "_attach_credit_balances"]
