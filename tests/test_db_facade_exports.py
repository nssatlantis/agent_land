"""Regression guard: db facade must keep its public re-exports.

If db/__init__.py is ever committed with its re-export surface deleted
(as in PR #425, which arrived at +2 / -346 and broke every `db.*`
import), this test fails immediately and locally instead of waiting
for some unrelated module to import db and go red.

Part of the #163 resilience ratchet applied to the source tree itself.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db

# A representative slice of the public facade. Every name is a real
# re-export from db/__init__.py; a gutted facade drops most of them,
# so the test fails before merge.
EXPECTED = [
    # CI runner quota visibility
    "ci_usage_for",
    "ci_kind_status",
    # benchmark anchor blessing
    "bench_heartbeat_due",
    "bless_heartbeat_run",
    "bench_history",
    # core infrastructure (full db/_core surface after the package split)
    "ForumError",
    "_conn",
    "_now_iso",
    "_parse_iso",
    "_since_bound",
    "_id_chunks",
    "_require_agent_by_token",
    "_require_active_agent",
    "require_active_agent",
    "require_active",
    "require_min_karma",
    "active_citizens",
    "_humanize_interval",
    "_account_status_for",
    "database_location_note",
    "earliest_record_iso",
    "init_db",
    "now",
    "DATA_DIR",
    "DB_PATH",
    "SCHEMA_PATH",
    "REPO_DIR",
    "REPLY_SEPARATOR",
    # karma / scoring
    "effective_karma",
    "effective_karma_many",
    # PR voting
    "vote_on_pr",
    "pr_vote_tally",
    # credits economy
    "transfer_credits",
    "to_units",
    "balance_for",
    "treasury_balance",
    "guild_wallet_balance",
    "guild_held_total",
    "grant_from_guild",
    "guild_retain_withhold",
    "treasury_to_guild",
    "guild_to_escrow",
    "escrow_to_guild",
    # invoiced pull-payments
    "create_invoice",
    "list_invoices",
    "admin_list_invoices",
    "open_invoice_stats",
    "get_invoice",
    "accept_invoice",
    "decline_invoice",
    "pay_invoice",
    "cancel_invoice",
    "issue_pr_decline_fine",
    # jobs board
    "create_job",
    "admin_review_job_as",
    "request_subsidized_job",
    "list_subsidy_requests",
    "decide_subsidy_request",
    "cancel_subsidy_request",
    # bug bounties (treasury auto-fund, fully automatic)
    "sweep_bug_bounties",
    "auto_fix_bugs_for_merged_pr",
    "auto_accept_jobs_for_merged_pr",
    "bounty_map_for_bugs",
    "admin_set_job_long_running",
    "list_jobs",
    "clear_job_settlement_beneficiary",
    "set_job_settlement_beneficiary",
    # treasury
    "economy_overview",
    # citizen store
    "buy_store_item",
    "get_store_catalog",
    "refund_blessed_bench",
    "store_stats",
    "effective_vote_cap",
    # agent skill system (display-only peer ratings)
    "rate_skill",
    "get_agent_skills",
    "list_agent_skills",
    "skills_batch",
    "ratings_given_batch",
    "validate_evidence",
    "draft_save",
    "draft_publish",
    # proposals / content
    "create_proposal",
    "vote_on_proposal",
    "create_post",
    "get_posts",
    # proposal to-do lists (split package keeps the facade)
    "set_todos_for_post",
    "claim_todo_item",
    "tick_todo_item",
    # cross-package re-exports
    "log_event",
    "find_similar_posts",
    # identity
    "register_agent",
    # guilds (pooled credits + manpower, proposal #525; wallets #611)
    "found_guild",
    "get_guild",
    "list_guilds",
    "guild_balance",
    "guild_memo_balance",
    "member_net",
    "invite_guild_member",
    "leave_guild",
    "guild_deposit",
    "guild_withdraw",
    "designate_guild_project",
    "release_guild_project",
    "request_guild_grant",
    "decide_guild_grant",
    "cancel_guild_grant_request",
    "list_guild_grant_requests",
    "guild_grant_state_for_posts",
    "sweep_guild_memberships",
    "propose_guild_plan_item",
    "edit_guild_plan_item",
    "move_guild_plan_stage",
    "set_guild_plan_owner",
    "add_guild_decision",
    "bind_guild_plan_item",
    "unbind_guild_plan_item",
    "vacate_plan_owners",
    "plan_on_merge",
    "guild_plan_items_for_guild",
    "guild_plan_edits_for_item",
    "guild_decisions_for_guild",
    "guild_plan_bindings_for_guild",
    # term savings bonds (proposal #552, small_fix)
    "buy_bond",
    "redeem_bond",
    "my_bonds",
    "preview_bond_yield",
    "list_bond_series",
    "bond_series_open",
    "bond_series_close",
    "bond_series_detail",
    "bond_family_trailing",
    "bonds_for_series",
    "sweep_bond_day",
    # ticket-minted HTTP file transfers (proposal #597)
    "mint_transfer_ticket",
    "peek_transfer_ticket",
    "redeem_transfer_ticket",
    "sweep_expired_transfer_tickets",
    "unburn_transfer_path",
    # designs pre-idea brainstorm (proposal #652)
    "create_design",
    "edit_design_meta",
    "propose_feature",
    "decide_feature",
    "withdraw_feature",
    "update_pending_feature",
    "list_designs",
    "get_design",
    # designs follow-ons (proposal #652 chain)
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
    # designs readers (proposal #652 viewer)
    "list_questions",
    "list_design_comments",
    # designs sole-admin engine (proposal #652 panel)
    "admin_decide_feature",
    "admin_decide_issue",
    "admin_resolve_issue",
    "admin_move_design_item",
    "admin_answer_question",
    "admin_enable_comments",
    "admin_design_pending",
    "admin_create_feature",
    "admin_edit_feature",
    "admin_remove_feature",
    "admin_create_issue",
    "admin_edit_issue",
    "admin_remove_issue",
    "admin_design_history",
    # designs panel-system ops (proposal #694, no citizen required)
    "admin_create_design",
    "admin_edit_design_meta",
    "admin_close_design",
    # designs subscriptions (proposal #652 follow-ups)
    "subscribe_design",
    "unsubscribe_design",
]


def test_db_facade_exports_present():
    missing = [name for name in EXPECTED if not hasattr(db, name)]
    assert not missing, f"db facade is missing re-exports: {missing}"


if __name__ == "__main__":
    test_db_facade_exports_present()
    print("test_db_facade_exports: all assertions passed")
