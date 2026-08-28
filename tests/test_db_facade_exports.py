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
    # core infrastructure
    "ForumError",
    "_conn",
    "_now_iso",
    "_parse_iso",
    "init_db",
    "now",
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
    # jobs board
    "create_job",
    "admin_review_job_as",
    "list_jobs",
    # treasury
    "economy_overview",
    # proposals / content
    "create_proposal",
    "vote_on_proposal",
    "create_post",
    "get_posts",
    # cross-package re-exports
    "log_event",
    "find_similar_posts",
    # identity
    "register_agent",
]


def test_db_facade_exports_present():
    missing = [name for name in EXPECTED if not hasattr(db, name)]
    assert not missing, f"db facade is missing re-exports: {missing}"
