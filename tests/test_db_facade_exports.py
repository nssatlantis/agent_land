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
    "to_quarters",
    "balance_for",
    # invoiced pull-payments
    "create_invoice",
    "list_invoices",
    "get_invoice",
    "accept_invoice",
    "decline_invoice",
    "pay_invoice",
    "cancel_invoice",
    "issue_pr_decline_fine",
    # jobs board
    "create_job",
    "admin_review_job_as",
    "list_jobs",
    # treasury
    "economy_overview",
    # citizen store
    "buy_store_item",
    "get_store_catalog",
    "refund_blessed_bench",
    "store_stats",
    "effective_vote_cap",
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
]


def test_db_facade_exports_present():
    missing = [name for name in EXPECTED if not hasattr(db, name)]
    assert not missing, f"db facade is missing re-exports: {missing}"
