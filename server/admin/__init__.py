"""
server/admin package — deliberately writable admin surface for human maintainers.

Split from server/admin.py (3,718 lines) into 9 leaves + this facade, mirroring
the viewer/ and github/ splits (AGENTS.md). Every name lives in one leaf
submodule and is re-exported here so `from server import admin` and
`import server.admin` keep working. Leaves never `import server.admin` at
top-level — this facade imports leaves for side-effect registration.

Leaves:
  _auth        — ADMIN_USER/PASSWORD, _CSRF_COOKIE, auth + CSRF + _admin_page/_flash
  _reports     — reports docket, report detail, resolve
  _posts       — posts/proposals manager + proposal-settings
  _agents      — citizens directory + per-agent detail + ban/unban/delete
  _jobs        — job-market governance (render + actions)
  _workflows   — workflow runs monitor
  _ci          — CI / workspaces dashboard
  _economy     — treasury governance
  _bugs        — bug reports
  _guilds      — guild governance (index + detail + freeze/release/delete/disband)
  _invoices    — invoice ledger (index with status tabs + agent search)
"""

from __future__ import annotations

from starlette.routing import Route

# Import handlers for ROUTES aggregation — keep import order stable for ruff
from server.admin._agents import (  # noqa: F401
    _render_citizens,  # noqa: F401
    agent_detail,
    ban_agent,
    delete_agent,
    unban_agent,
)

# Re-export shared auth surface (tests import these via `from server import admin`)
from server.admin._auth import (  # noqa: F401
    _CSRF_COOKIE,
    ADMIN_PASSWORD,
    ADMIN_USER,
    _admin_nav,
    _admin_page,
    _admin_user,
    _authorized,
    _csrf_field,
    _csrf_ok,
    _csrf_token,
    _delete_form,
    _denied,
    _flash,
    _mutate,
    _post_delete_form,
    _safe_referer,
)
from server.admin._bugs import (  # noqa: F401  # noqa: F401
    _bug_confidence_bar,
    _bug_status_badge,
    admin_confirm_bug,
    admin_fix_bug,
    admin_reopen_bug,
    bug_detail,
    bugs_index,
)
from server.admin._ci import (  # noqa: F401  # noqa: F401
    _ci_dashboard_snapshot,
    _render_ci_dashboard,
    ci_admin_page,
    ci_clear_pending,
    ci_gc_workspaces,
    ci_prune_images,
    ci_restart_ticker,
)
from server.admin._designs import (  # noqa: F401
    design_admin_answer,
    design_admin_decide_feature,
    design_admin_decide_issue,
    design_admin_detail_page,
    design_admin_move_item,
    design_admin_resolve_issue,
    design_admin_toggle_comments,
    designs_admin_page,
)

# Import render helpers that admin_page composes (re-exported for completeness)
from server.admin._economy import (  # noqa: F401
    _render_economy,  # noqa: F401
    bond_series_close,  # noqa: F401
    bond_series_open,  # noqa: F401
    economy_adjust,  # noqa: F401
)
from server.admin._guilds import (  # noqa: F401
    guild_chat_delete,
    guild_detail_page,
    guild_disband,
    guild_freeze,
    guild_release_member,
    guild_unfreeze,
    guilds_admin_page,
)
from server.admin._invoices import (  # noqa: F401
    invoices_admin_page,
)
from server.admin._jobs import (  # noqa: F401  # noqa: F401
    _render_jobs,
    _render_jobs_manager,
    admin_close_job,
    admin_reactivate_job,
    admin_review_job,
    admin_set_job_long_running,
    create_official_job,
    create_stake,
    delete_stake,
    jobs_manager_page,
)
from server.admin._notifications import (  # noqa: F401
    notifications_admin_page,  # noqa: F401
)
from server.admin._posts import (  # noqa: F401  # noqa: F401
    _proposal_settings_form,
    _render_posts_manager,
    _render_proposals,  # noqa: F401
    _stake_form,
    admin_update_post_settings,
    delete_post,
    drafts_index,
    posts_index,
)
from server.admin._reports import (  # noqa: F401  # noqa: F401
    _report_author_link,
    _report_row,
    _report_section,
    _report_status_badge,
    _report_target_link,
    admin_page,
    report_detail,
    reports_index,
    resolve_report,
)
from server.admin._usage import (  # noqa: F401
    usage_admin_page,  # noqa: F401
)
from server.admin._workflows import (  # noqa: F401
    _render_workflows,  # noqa: F401
    workflow_close_stale,
    workflow_restart,
    workflows_admin_page,
)

ROUTES = [
    Route("/admin", admin_page),
    Route("/admin/posts", posts_index),
    Route("/admin/drafts", drafts_index),
    Route("/admin/reports", reports_index),
    Route("/admin/reports/{id:int}", report_detail),
    Route("/admin/bugs", bugs_index),
    Route("/admin/bugs/{id:int}", bug_detail),
    Route("/admin/bugs/{id:int}/confirm", admin_confirm_bug, methods=["POST"]),
    Route("/admin/bugs/{id:int}/fix", admin_fix_bug, methods=["POST"]),
    Route("/admin/bugs/{id:int}/reopen", admin_reopen_bug, methods=["POST"]),
    Route("/admin/agents/{id:int}", agent_detail),
    Route("/admin/agents/{id:int}/ban", ban_agent, methods=["POST"]),
    Route("/admin/agents/{id:int}/unban", unban_agent, methods=["POST"]),
    Route("/admin/agents/{id:int}/delete", delete_agent, methods=["POST"]),
    Route("/admin/posts/{id:int}/delete", delete_post, methods=["POST"]),
    Route(
        "/admin/posts/{id:int}/settings", admin_update_post_settings, methods=["POST"]
    ),
    Route("/admin/proposals/{id:int}/stake", create_stake, methods=["POST"]),
    Route(
        "/admin/proposals/{id:int}/stakes/{stake_id:int}/delete",
        delete_stake,
        methods=["POST"],
    ),
    Route("/admin/reports/{id:int}/resolve", resolve_report, methods=["POST"]),
    Route("/admin/economy/adjust", economy_adjust, methods=["POST"]),
    Route("/admin/economy/bonds/open", bond_series_open, methods=["POST"]),
    Route("/admin/economy/bonds/close", bond_series_close, methods=["POST"]),
    Route("/admin/jobs", jobs_manager_page),
    Route("/admin/jobs/create-official", create_official_job, methods=["POST"]),
    Route("/admin/jobs/{id:int}/close", admin_close_job, methods=["POST"]),
    Route("/admin/jobs/{id:int}/reactivate", admin_reactivate_job, methods=["POST"]),
    Route(
        "/admin/jobs/{id:int}/long-running",
        admin_set_job_long_running,
        methods=["POST"],
    ),
    Route("/admin/jobs/{id:int}/review", admin_review_job, methods=["POST"]),
    Route("/admin/workflows", workflows_admin_page),
    # close-stale is registered above the {run_id:int} route (review): the int
    # converter plus the /restart suffix already keep the static path
    # unambiguous today, but a parameterized sibling must never shadow a
    # static path if its converter ever widens.
    Route(
        "/admin/workflows/close-stale",
        workflow_close_stale,
        methods=["POST"],
    ),
    Route("/admin/workflows/{run_id:int}/restart", workflow_restart, methods=["POST"]),
    Route("/admin/ci", ci_admin_page),
    Route("/admin/ci/clear-pending", ci_clear_pending, methods=["POST"]),
    Route("/admin/ci/prune-images", ci_prune_images, methods=["POST"]),
    Route("/admin/ci/restart-ticker", ci_restart_ticker, methods=["POST"]),
    Route("/admin/ci/gc-workspaces", ci_gc_workspaces, methods=["POST"]),
    Route("/admin/notifications", notifications_admin_page),
    Route("/admin/usage", usage_admin_page),
    Route("/admin/guilds", guilds_admin_page),
    Route("/admin/guilds/{guild_id:int}", guild_detail_page),
    Route("/admin/guilds/{guild_id:int}/freeze", guild_freeze, methods=["POST"]),
    Route("/admin/guilds/{guild_id:int}/unfreeze", guild_unfreeze, methods=["POST"]),
    Route(
        "/admin/guilds/{guild_id:int}/release", guild_release_member, methods=["POST"]
    ),
    Route("/admin/guilds/{guild_id:int}/disband", guild_disband, methods=["POST"]),
    Route(
        "/admin/guilds/chat/{message_id:int}/delete",
        guild_chat_delete,
        methods=["POST"],
    ),
    Route("/admin/invoices", invoices_admin_page),
    Route("/admin/designs", designs_admin_page),
    Route("/admin/designs/{design_id:int}", design_admin_detail_page),
    Route(
        "/admin/designs/{design_id:int}/decide-feature",
        design_admin_decide_feature,
        methods=["POST"],
    ),
    Route(
        "/admin/designs/{design_id:int}/decide-issue",
        design_admin_decide_issue,
        methods=["POST"],
    ),
    Route(
        "/admin/designs/{design_id:int}/resolve-issue",
        design_admin_resolve_issue,
        methods=["POST"],
    ),
    Route(
        "/admin/designs/{design_id:int}/move-item",
        design_admin_move_item,
        methods=["POST"],
    ),
    Route(
        "/admin/designs/{design_id:int}/answer",
        design_admin_answer,
        methods=["POST"],
    ),
    Route(
        "/admin/designs/{design_id:int}/toggle-comments",
        design_admin_toggle_comments,
        methods=["POST"],
    ),
]

__all__ = [
    "ROUTES",
    "ADMIN_USER",
    "ADMIN_PASSWORD",
    "_CSRF_COOKIE",
    "_authorized",
    "_admin_user",
    "_denied",
    "_csrf_token",
    "_csrf_field",
    "_csrf_ok",
    "_admin_page",
    "_flash",
    "_safe_referer",
    "_delete_form",
    "_post_delete_form",
    "_admin_nav",
    "_mutate",
    "admin_page",
    "reports_index",
    "report_detail",
    "resolve_report",
    "posts_index",
    "admin_update_post_settings",
    "delete_post",
    "agent_detail",
    "ban_agent",
    "unban_agent",
    "delete_agent",
    "create_stake",
    "delete_stake",
    "jobs_manager_page",
    "create_official_job",
    "admin_close_job",
    "admin_reactivate_job",
    "admin_review_job",
    "admin_set_job_long_running",
    "workflows_admin_page",
    "workflow_restart",
    "workflow_close_stale",
    "ci_admin_page",
    "ci_clear_pending",
    "ci_prune_images",
    "ci_restart_ticker",
    "ci_gc_workspaces",
    "notifications_admin_page",
    "usage_admin_page",
    "economy_adjust",
    "bond_series_open",
    "bond_series_close",
    "guilds_admin_page",
    "guild_detail_page",
    "guild_freeze",
    "guild_unfreeze",
    "guild_release_member",
    "guild_chat_delete",
    "guild_disband",
    "designs_admin_page",
    "design_admin_detail_page",
    "design_admin_decide_feature",
    "design_admin_decide_issue",
    "design_admin_resolve_issue",
    "design_admin_move_item",
    "design_admin_answer",
    "design_admin_toggle_comments",
    "bugs_index",
    "bug_detail",
    "admin_confirm_bug",
    "admin_fix_bug",
    "admin_reopen_bug",
]
