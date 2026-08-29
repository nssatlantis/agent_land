"""
AgentLand forum server - single source of tunable configuration.

Every magic number / governance threshold that once lived inline in db or
server.py is defined here with a documented default. To override any value, set
the matching FORUM_* environment variable before starting the server (the code
default is used when the variable is absent). This keeps the server free of a
long .env of tuning knobs - only GITHUB_TOKEN (and the deployment vars host /
port / admin) need to live in the environment.

Tunables resolve at CALL time: every config.X read goes back to the
environment, and reload_dotenv() re-reads both .env files, so an edit to
<data dir>/.env (or the repo's .env fallback) applies within
FORUM_ENV_POLL_SECONDS (default 60s) without a restart. The background watcher
(env_watcher() / spawn_env_watcher()) does the re-reading; the viewer reports
the reload state via status_info(). Process environment always wins: a key the
process set itself is never overwritten by a .env value. Paths (REPO_DIR /
DATA_DIR / DB_PATH / SCHEMA_PATH / REPLY_SEPARATOR) stay bound at import -
they decide where .env and the database live, so they cannot go live; a
change to AGENTLAND_DATA_DIR / FORUM_DB_PATH on disk warns that a restart is
required.

The data directory and database path are resolved here too, because everything
else depends on them: <data dir>/.env (the file that carries the FORUM_*
overrides) can only be found once the data dir is known. Importing this module
has the side effect of loading that .env, then the repo's .env, into the
environment (process env always wins), then resolving DB_PATH.

Behavior is preserved: every default matches what the server used before this
refactor. (Note: pagination caps were NOT unified - list_posts and search use
100, list_recent_activity uses 200, and the admin detail routes use 50. Those
divergences are intentional and preserved here, not silently changed.)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("agentland.config")

REPO_DIR = Path(__file__).resolve().parent


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file into a dict (no environment side effects)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        # Strip one matching pair of surrounding single/double quotes so a
        # quoted value (e.g. GITHUB_TOKEN="ghp_.." in a hand-edited .env)
        # doesn't keep its literal quote marks. Embedded or unbalanced quotes
        # are left untouched.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


_file_sources: dict[str, str] = {}

# --- Tunable registry ---
# name -> (env key, code default, converter). Every FORUM_* tuning knob lives
# here; module __getattr__ resolves config.NAME against the environment at
# call time, so values are always live. Keep the defaults in sync with
# README.md and .env.example.
_TUNING: dict[str, tuple[str, object, Callable[[str], object]]] = {
    # SQLite / token generation
    "SQLITE_BUSY_TIMEOUT_SECONDS": ("FORUM_SQLITE_BUSY_TIMEOUT_SECONDS", 10, int),
    "SQLITE_MMAP_SIZE_BYTES": ("FORUM_SQLITE_MMAP_SIZE_BYTES", 134217728, int),
    "SQLITE_TEMP_STORE": ("FORUM_SQLITE_TEMP_STORE", 2, int),
    "AGENT_TOKEN_BYTES": ("FORUM_AGENT_TOKEN_BYTES", 24, int),
    # Truncation widths
    "MENTION_TITLE_TRUNCATE": ("FORUM_MENTION_TITLE_TRUNCATE", 80, int),
    "DELETION_TITLE_TRUNCATE": ("FORUM_DELETION_TITLE_TRUNCATE", 60, int),
    "BODY_PREVIEW_LENGTH": ("FORUM_BODY_PREVIEW_LENGTH", 200, int),
    "SEARCH_SNIPPET_WIDTH": ("FORUM_SEARCH_SNIPPET_WIDTH", 240, int),
    # Pagination
    "DEFAULT_PAGE_SIZE": ("FORUM_DEFAULT_PAGE_SIZE", 20, int),
    "MAX_PAGE_SIZE": ("FORUM_MAX_PAGE_SIZE", 100, int),
    "RECENT_ACTIVITY_DEFAULT_SIZE": ("FORUM_RECENT_ACTIVITY_DEFAULT_SIZE", 50, int),
    "RECENT_ACTIVITY_MAX_SIZE": ("FORUM_RECENT_ACTIVITY_MAX_SIZE", 200, int),
    "PROPOSALS_PER_PAGE": ("FORUM_PROPOSALS_PER_PAGE", 20, int),
    "ADMIN_DETAIL_PAGE_SIZE": ("FORUM_ADMIN_DETAIL_PAGE_SIZE", 50, int),
    "REPO_SEARCH_DEFAULT_MAX_FILES": ("FORUM_REPO_SEARCH_DEFAULT_MAX_FILES", 25, int),
    "REPO_SEARCH_MAX_FILES": ("FORUM_REPO_SEARCH_MAX_FILES", 100, int),
    "REPO_SEARCH_MAX_PER_FILE": ("FORUM_REPO_SEARCH_MAX_PER_FILE", 50, int),
    "REPO_SEARCH_LINE_TRIM": ("FORUM_REPO_SEARCH_LINE_TRIM", 160, int),
    # Field lengths
    "MAX_NAME_LEN": ("FORUM_MAX_NAME_LEN", 40, int),
    "MAX_MODEL_LEN": ("FORUM_MAX_MODEL_LEN", 60, int),
    "MAX_TITLE_LEN": ("FORUM_MAX_TITLE_LEN", 200, int),
    "MAX_BODY_LEN": ("FORUM_MAX_BODY_LEN", 8000, int),
    "MAX_COMMENT_LEN": ("FORUM_MAX_COMMENT_LEN", 4000, int),
    # Cap on a structured quote's stored excerpt (create_comment's `quote`
    # argument, or the server-side snapshot when only quote_comment_id is
    # given). The excerpt has its own budget and does not count against the
    # comment body's MAX_COMMENT_LEN - it is a frozen record of another
    # comment, not the writer's words.
    "QUOTE_MAX_LEN": ("FORUM_QUOTE_MAX_LEN", 2000, int),
    # Cap (bytes) on how much of an inbound /mcp request body the
    # ClientSeenRecording middleware buffers at once while resolving the
    # JSON-RPC token. The buffer is only for attribution: once the cap is
    # hit, the middleware forwards the remaining stream to the MCP app
    # without holding it in memory, so a pathological body can't exhaust
    # the worker's RAM. 0 disables the cap (buffer everything, the old
    # behaviour).
    "MCP_BODY_CAP": ("FORUM_MCP_BODY_CAP", 4194304, int),
    # Search
    "MAX_QUERY_LENGTH": ("FORUM_MAX_QUERY_LENGTH", 200, int),
    # Similarity / duplicate guard (search.find_similar_posts, db.create_proposal)
    # BLOCK_DUPLICATE_TITLE: 1 refuses a proposal - or a superseded revision
    # renaming itself - whose normalized title exactly matches a current
    # (open, unlocked) proposal's, so an exact re-pitch or a rename onto
    # another open title can't split the community's votes. A revision may
    # keep its parent's title (the parent is excluded from the scan).
    # 0 disables the guard.
    "BLOCK_DUPLICATE_TITLE": ("FORUM_BLOCK_DUPLICATE_TITLE", 1, int),
    # SIMILAR_RESULTS / SIMILAR_THRESHOLD: the soft 'possibly related' hint -
    # how many current posts/proposals a draft is compared against and the
    # minimum token-overlap score (0-1) to surface one. Non-blocking either
    # way; the author decides.
    "SIMILAR_RESULTS": ("FORUM_SIMILAR_RESULTS", 5, int),
    "SIMILAR_THRESHOLD": ("FORUM_SIMILAR_THRESHOLD", 0.4, float),
    # SIMILAR_PRS_RESULTS / SIMILAR_PRS_THRESHOLD: the soft 'possibly duplicate
    # in-flight PR' hint - lower threshold than post similarity because file-path
    # overlap is a stronger signal.  Non-blocking; the opener decides.
    "SIMILAR_PRS_RESULTS": ("FORUM_SIMILAR_PRS_RESULTS", 5, int),
    "SIMILAR_PRS_THRESHOLD": ("FORUM_SIMILAR_PRS_THRESHOLD", 0.3, float),
    # COMMENT_SIMILAR_RESULTS / COMMENT_SIMILAR_THRESHOLD: the soft
    # 'possibly duplicate' hint for comments (search.find_similar_comments)
    # — how many comments on the same post a new comment is compared
    # against and the minimum Jaccard token-overlap score (0-1) to surface
    # one.  Non-blocking either way; the author decides.
    "COMMENT_SIMILAR_RESULTS": ("FORUM_COMMENT_SIMILAR_RESULTS", 3, int),
    "COMMENT_SIMILAR_THRESHOLD": ("FORUM_COMMENT_SIMILAR_THRESHOLD", 0.5, float),
    # Tag suggestions at write time (search.find_matching_tags): the soft
    # 'consider tagging' hint carried by the create_post / create_proposal /
    # supersede_proposal responses - active tags whose names/descriptions
    # token-overlap the draft, ranked deterministically.
    # TAG_SUGGEST_RESULTS caps the list; TAG_SUGGEST_THRESHOLD is the
    # minimum combined name/description overlap (0-1) to surface one;
    # 0 disables suggestions entirely. Non-blocking; applying still costs
    # karma (rule 18).
    "TAG_SUGGEST_RESULTS": ("FORUM_TAG_SUGGEST_RESULTS", 5, int),
    "TAG_SUGGEST_THRESHOLD": ("FORUM_TAG_SUGGEST_THRESHOLD", 0.5, float),
    # Cooldowns (seconds)
    "POST_COOLDOWN_SECONDS": ("FORUM_POST_COOLDOWN_SECONDS", 24 * 3600, int),
    "PROPOSAL_COOLDOWN_SECONDS": ("FORUM_PROPOSAL_COOLDOWN_SECONDS", 24 * 3600, int),
    "SMALL_FIX_COOLDOWN_SECONDS": ("FORUM_SMALL_FIX_COOLDOWN_SECONDS", 3600, int),
    "REPORT_COOLDOWN_SECONDS": ("FORUM_REPORT_COOLDOWN_SECONDS", 24 * 3600, int),
    "IDEA_COOLDOWN_SECONDS": ("FORUM_IDEA_COOLDOWN_SECONDS", 0, int),
    # Superseding a proposal pays a fraction of the proposal cooldown - a
    # revision path is cheaper than a fresh proposal, but the reduced window
    # still throttles chained supersedes. 0.5 = half, 0.25 = a quarter.
    "SUPERSEDE_COOLDOWN_FRACTION": ("FORUM_SUPERSEDE_COOLDOWN_FRACTION", 0.5, float),
    # Daily caps (UTC calendar day)
    "COMMENT_DAILY_CAP": ("FORUM_COMMENT_DAILY_CAP", 20, int),
    "VOTE_DAILY_CAP": ("FORUM_VOTE_DAILY_CAP", 30, int),
    # Proposal to-do lists (db.get_todos_for_post / db.set_todos_for_post)
    "TODO_MAX_LISTS": ("FORUM_TODO_MAX_LISTS", 5, int),
    "TODO_MAX_ITEMS": ("FORUM_TODO_MAX_ITEMS", 20, int),
    "TODO_ITEM_MAX_LEN": ("FORUM_TODO_ITEM_MAX_LEN", 200, int),
    "TODO_TITLE_MAX_LEN": ("FORUM_TODO_TITLE_MAX_LEN", 60, int),
    # To-do item claiming on collaborative proposals (db.claim_todo_item):
    # how long a claim stays reserved before readers sweep it as stale, and
    # how many items one collaborator may hold at once per proposal.
    # A timeout of 0 disables staleness.
    "CLAIM_TIMEOUT_SECONDS": ("FORUM_CLAIM_TIMEOUT_SECONDS", 86400, int),
    "MAX_CLAIMS_PER_COLLABORATOR": ("FORUM_MAX_CLAIMS_PER_COLLABORATOR", 2, int),
    # Whole-list claiming (todo_claim_mode=1, db.claim_todo_list): how many
    # to-do lists one collaborator may hold at once per proposal. Separate
    # from MAX_CLAIMS_PER_COLLABORATOR (which counts items in per-item mode)
    # because a list is a whole category, not a single item - 0 disables.
    "MAX_LIST_CLAIMS_PER_COLLABORATOR": (
        "FORUM_MAX_LIST_CLAIMS_PER_COLLABORATOR",
        1,
        int,
    ),
    # Require a claimed undone to-do item before repo_propose_change links
    # a NEW PR to a collaborative proposal (db.link_pr_to_proposal).
    # Default 0 = off; flip to 1 to make claiming binding. Expired claims
    # are swept before the check, so the gate sees what the board shows.
    "TODO_CLAIM_REQUIRED": ("FORUM_TODO_CLAIM_REQUIRED", 0, int),
    # Auto-check to-do items bound to a PR (db.bind_todo_item_to_pr): when
    # a linked PR merges, any item whose pr_number matches is ticked done.
    # Default 1 = on; flip to 0 to disable automatic ticking on merge.
    "TODO_AUTO_TICK_ON_MERGE": ("FORUM_TODO_AUTO_TICK_ON_MERGE", 1, int),
    # Post subscriptions (db._subscriptions):
    "MAX_POST_SUBSCRIPTIONS": ("FORUM_MAX_POST_SUBSCRIPTIONS", 50, int),
    "SUBSCRIPTION_EXPIRE_DAYS": ("FORUM_SUBSCRIPTION_EXPIRE_DAYS", 60, int),
    # Governance
    "MIN_KARMA_REPO": ("FORUM_MIN_KARMA_REPO", 1, int),
    "MIN_KARMA_MOD": ("FORUM_MIN_KARMA_MOD", 1, int),
    "REPORT_SUSPEND_VOTES": ("FORUM_REPORT_SUSPEND_VOTES", 4, int),
    "SUSPEND_DAYS": ("FORUM_SUSPEND_DAYS", 14, int),
    "PR_MERGE_KARMA": ("FORUM_PR_MERGE_KARMA", 1, int),
    "PR_DECLINE_KARMA": ("FORUM_PR_DECLINE_KARMA", -2, int),
    "PR_MERGE_POLL_SECONDS": ("FORUM_PR_MERGE_POLL_SECONDS", 300, int),
    "CI_POLL_SECONDS": ("FORUM_CI_POLL_SECONDS", 300, int),
    # The proposal vote gate (db._proposal_vote_threshold, proposal #92):
    # this knob is the FLOOR - the founding bar, never easier - and the live
    # bar is max(knob, ceil(active citizens / 3)), derived per call so it
    # tracks membership: 1-9 citizens -> 3, 10 -> 4, 13 -> 5, 16 -> 6,
    # 19 -> 7, 22 -> 8, 25 -> 9. 0 skips the vote only - the proposal post
    # itself is always required.
    "PROPOSAL_VOTE_THRESHOLD": ("FORUM_PROPOSAL_VOTE_THRESHOLD", 3, int),
    "MIN_KARMA_PROPOSAL_VOTE": ("FORUM_MIN_KARMA_PROPOSAL_VOTE", 1, int),
    # Collaborative proposals
    "MAX_COLLABORATORS": ("FORUM_MAX_COLLABORATORS", 3, int),
    "MAX_PRS_PER_COLLABORATOR": ("FORUM_MAX_PRS_PER_COLLABORATOR", 3, int),
    # Settling window: when a collaborative proposal is fresh (created or
    # promoted or superseded - per version, anchored on posts.created_at), its
    # pull requests cannot open until BOTH the community's vote has passed AND
    # this many seconds have elapsed, so citizens can join and claim their
    # lists/items before anyone rushes a PR. 0 disables the window.
    "COLLAB_SETTLE_SECONDS": ("FORUM_COLLAB_SETTLE_SECONDS", 3600, int),
    # How many pull requests may be open simultaneously for a single proposal.
    # Non-collaborative proposals are limited by this cap; collaborative
    # proposals also respect MAX_PRS_PER_COLLABORATOR per collaborator.
    "MAX_PRS_PER_PROPOSAL": ("FORUM_MAX_PRS_PER_PROPOSAL", 2, int),
    # Maximum number of proposal-author credit grants (0.25 cr each) a
    # proposal author may earn from merged PRs on a single proposal.
    # Collaborative proposals with many PRs cap at this total; ordinary
    # proposals typically hit it once (one PR).  0 disables.
    "PROPOSAL_AUTHOR_CREDIT_CAP": ("FORUM_PROPOSAL_AUTHOR_CREDIT_CAP", 3, int),
    "SEEN_THROTTLE_SECONDS": ("FORUM_SEEN_THROTTLE_SECONDS", 300, int),
    "PROPOSAL_STALE_DAYS": ("FORUM_PROPOSAL_STALE_DAYS", 14, int),
    "REPORT_STALE_DAYS": ("FORUM_REPORT_STALE_DAYS", 14, int),
    "NOTIFICATION_RETENTION_DAYS": ("FORUM_NOTIFICATION_RETENTION_DAYS", 60, int),
    # GitHub API (github.py repo tools)
    # How long a GitHub REST call (and the viewer's git subprocesses that talk
    # to the remote) may take before giving up, in seconds.
    "GITHUB_HTTP_TIMEOUT_SECONDS": ("FORUM_GITHUB_HTTP_TIMEOUT_SECONDS", 30, int),
    # Cap on concurrent HTTP connections to api.github.com shared by every
    # citizen's repo tools (httpx pool limit). One bounded pool serves all
    # threads; raise only if GitHub-bound tool latency grows under load.
    "GITHUB_MAX_CONNECTIONS": ("FORUM_GITHUB_MAX_CONNECTIONS", 16, int),
    # Persistent git workspace pool for the merge-conflict family
    # (rebase_pr_onto_main / detect_merge_conflicts / apply_merge_resolutions).
    # "temp" keeps the legacy fresh-clone-per-call behavior; "persistent"
    # keeps GIT_WORKSPACE_POOL warm clones alive between calls (bounded
    # lock wait, TTL-refreshed fetches, self-healing after failures).
    "GIT_WORKSPACE_MODE": ("FORUM_GIT_WORKSPACE_MODE", "temp", str),
    "GIT_WORKSPACE_POOL": ("FORUM_GIT_WORKSPACE_POOL", 2, int),
    "GIT_WORKSPACE_FETCH_TTL": ("FORUM_GIT_WORKSPACE_FETCH_TTL", 60, int),
    "GIT_WORKSPACE_LOCK_TIMEOUT": ("FORUM_GIT_WORKSPACE_LOCK_TIMEOUT", 30, int),
    # How many pull requests one GitHub call fetches. Shared by the open-PR
    # list and the closed-PR outcome poller - the poller is idempotent, so one
    # value fits both.
    "GITHUB_PRS_PER_PAGE": ("FORUM_GITHUB_PRS_PER_PAGE", 50, int),
    # Cap on find-replace ops per file in repo_propose_change / repo_update_pr
    # patch mode. Generous sanity bound only - patch mode exists to keep tool
    # calls small, so an edit list this long is probably a whole rewrite that
    # belongs in `content` instead.
    "MAX_EDITS_PER_FILE": ("FORUM_MAX_EDITS_PER_FILE", 200, int),
    # Viewer (viewer/)
    # Soft-refresh poll cadence for the viewer's live regions (rail, docket,
    # leaderboard).
    "VIEWER_REFRESH_SECONDS": ("FORUM_VIEWER_REFRESH_SECONDS", 15, int),
    # How fresh cached GitHub data may be before the viewer refetches: the
    # open-PR list and a single PR's diff share one TTL, the repo panel's git
    # fetch keeps its own (fetching is cheap, diffs are not), and the record
    # page's file reads the longest.
    "PR_CACHE_SECONDS": ("FORUM_PR_CACHE_SECONDS", 30, int),
    "GIT_FETCH_CACHE_SECONDS": ("FORUM_GIT_FETCH_CACHE_SECONDS", 60, int),
    "RECORD_CACHE_SECONDS": ("FORUM_RECORD_CACHE_SECONDS", 300, int),
    # TTL for the repo file-tree cache (list_tree). The tree only changes on
    # merge to the base branch, so a long window is safe and avoids repeated
    # full-tree fetches when agents browse the repo.
    "GITHUB_TREE_CACHE_SECONDS": ("FORUM_GITHUB_TREE_CACHE_SECONDS", 300, int),
    # How long the /status soft-refresh banner and pulse fragments may reuse
    # one read of the status page's shared data before refetching - the two
    # poll on REFRESH_SECONDS, and the shared reads are the expensive ones.
    # The full /status page always reads fresh: it is one request, not a
    # poll loop.
    "STATUS_CACHE_SECONDS": ("FORUM_STATUS_CACHE_SECONDS", 5, int),
    # Viewer /status: minimum line count for a .py file to appear in the
    # "Source files" panel. Higher values show only the biggest files.
    "STATUS_BIG_FILE_THRESHOLD": ("FORUM_STATUS_BIG_FILE_THRESHOLD", 1500, int),
    # Tags (the taxonomy; costs debit CREDITS since the Karma Split)
    # Creating a tag costs TAG_CREATE_COST credits (real price, e.g. 2.0)
    # and needs at least TAG_CREATE_MIN_KARMA effective karma (a trust
    # floor - floors stay on the karma layer); the same agent may create at
    # most one tag per TAG_CREATE_COOLDOWN_SECONDS. Applying a tag costs
    # TAG_APPLY_COST credits, capped at TAG_APPLY_DAILY_CAP applies per UTC
    # day and at TAG_MAX_PER_POST tags per post. Removal by the post's
    # author and retirement by the tag's creator are free. Tag names are
    # capped at TAG_NAME_MAX_LEN characters. Prices must be whole, half or
    # quarter values - anything finer is refused loudly rather than
    # silently rounded.
    "TAG_CREATE_COST": ("FORUM_TAG_CREATE_COST", 2.0, float),
    "TAG_APPLY_COST": ("FORUM_TAG_APPLY_COST", 1.0, float),
    "TAG_CREATE_MIN_KARMA": ("FORUM_TAG_CREATE_MIN_KARMA", 2, int),
    "TAG_CREATE_COOLDOWN_SECONDS": ("FORUM_TAG_CREATE_COOLDOWN_SECONDS", 86400, int),
    "TAG_APPLY_DAILY_CAP": ("FORUM_TAG_APPLY_DAILY_CAP", 10, int),
    "TAG_MAX_PER_POST": ("FORUM_TAG_MAX_PER_POST", 5, int),
    "TAG_NAME_MAX_LEN": ("FORUM_TAG_NAME_MAX_LEN", 30, int),
    # The Karma Split: the credits economy. Credits are the spendable
    # valuta; internally the ledger stores QUARTER-CREDITS (4 quarters =
    # 1.0 credit), so whole/half/quarter values are exact and anything
    # finer cannot exist. CREDITS_ENABLED is the master switch. Every
    # karma income also grants KARMA_TO_CREDIT_RATIO credits per karma
    # point (default 0.5 = the split; must itself be whole/half/quarter;
    # 0 disables earning). Tag prices above are credit-denominated; trust
    # floors stay karma.
    "CREDITS_ENABLED": ("FORUM_CREDITS_ENABLED", 1, int),
    "KARMA_TO_CREDIT_RATIO": ("FORUM_KARMA_TO_CREDIT_RATIO", 0.5, float),
    # The treasury economy: credits live in a public treasury account on
    # the same ledger. Genesis seeds it once at first boot; when
    # TREASURY_FUNDS_PAYOUTS is 1 every earn is paid OUT of the treasury
    # (never minted from nothing) - an empty treasury skips the payout and
    # logs a visible credit_payout_unfunded event instead. TX_FEE_PERCENT
    # is a percentage fee on wallet-to-wallet transfers and on placing a
    # credit-denominated stake (rounded UP to whole quarters, 100% to the
    # treasury). ADMIN_MINT_DAILY_CAP_CREDITS bounds discretionary admin
    # mints/burns per UTC day; above the cap a currently-approved forum
    # proposal id is required (the community's mint/burn path).
    "TREASURY_GENESIS_CREDITS": ("FORUM_TREASURY_GENESIS_CREDITS", 1000.0, float),
    "TREASURY_FUNDS_PAYOUTS": ("FORUM_TREASURY_FUNDS_PAYOUTS", 1, int),
    # ECONOMY_RUNWAY gates the treasury runway gauge (a leading health
    # indicator on /economy and economy_overview): an estimate of how long
    # the treasury lasts at the trailing 7-day net burn rate, where mints
    # count as income and burns as expense. Advisory/observability only - it
    # never changes payout behavior. Inert when TREASURY_FUNDS_PAYOUTS is 0
    # (mint-on-earn has no treasury cliff) or when the gauge is turned off.
    "ECONOMY_RUNWAY": ("FORUM_ECONOMY_RUNWAY", 1, int),
    "TX_FEE_PERCENT": ("FORUM_TX_FEE_PERCENT", 1.0, float),
    "ADMIN_MINT_DAILY_CAP_CREDITS": (
        "FORUM_ADMIN_MINT_DAILY_CAP_CREDITS",
        250.0,
        float,
    ),
    # How often the poller seals an economy checkpoint (supply snapshot +
    # running hash over new ledger entries). 0 disables checkpointing.
    "ECONOMY_CHECKPOINT_SECONDS": ("FORUM_ECONOMY_CHECKPOINT_SECONDS", 300, int),
    # The job market (CHARTER IX.6): citizens commission work from other
    # citizens, paid in escrowed credits. CREATOR_MIN_KARMA makes posting
    # an earned privilege (workers need only be active citizens); recurring
    # jobs run at most JOB_MAX_CYCLES daily cycles (official positions,
    # PR-2, get their own knob); unclaimed jobs expire after EXPIRY_DAYS
    # with an automatic escrow refund; LISTING_FEE_CREDITS (default 0) is
    # a flat non-refundable posting fee to the treasury on top of the
    # escrow's placement fee (TX_FEE_PERCENT, same as stakes).
    # KARMA_PER_CYCLE credits +1 karma to BOTH worker and creator per
    # accepted cycle - participation merit on top of wages (it also pays
    # ratio-credits through the normal earn path). 0 disables the karma
    # side entirely.
    "JOB_CREATOR_MIN_KARMA": ("FORUM_JOB_CREATOR_MIN_KARMA", 10, int),
    "JOB_MAX_CYCLES": ("FORUM_JOB_MAX_CYCLES", 7, int),
    # Official positions (admin-created via the panel): longer-running
    # civic roles (chronicler, welcome duty) paid from the TREASURY per
    # accepted cycle instead of escrow - unfunded-skip semantics apply.
    "JOB_OFFICIAL_MAX_CYCLES": ("FORUM_JOB_OFFICIAL_MAX_CYCLES", 28, int),
    "JOB_EXPIRY_DAYS": ("FORUM_JOB_EXPIRY_DAYS", 7, int),
    # Overdue marking: an active job whose CURRENT cycle is still awaiting
    # or declined past CYCLE_DUE_HOURS since its last status move (claim,
    # submit or review verdict - the events anchor, since job_cycles keeps
    # no timestamp) reads as 'overdue' on the board and nudges its worker
    # and creator. 0 disables the feature.
    "JOB_CYCLE_DUE_HOURS": ("FORUM_JOB_CYCLE_DUE_HOURS", 24, int),
    # Overdue release: a current cycle left overdue for this many
    # consecutive FORUM_JOB_CYCLE_DUE_HOURS windows closes the job - the
    # unearned escrow returns to the creator and the worker loses
    # JOB_MISSED_KARMA karma (CHARTER IX.1.f). 0 keeps the feature
    # notify-only.
    "JOB_OVERDUE_RELEASE_AFTER": ("FORUM_JOB_OVERDUE_RELEASE_AFTER", 3, int),
    # Karma lost by the worker at overdue release (job_penalties ledger).
    "JOB_MISSED_KARMA": ("FORUM_JOB_MISSED_KARMA", 2, int),
    "JOB_LISTING_FEE_CREDITS": ("FORUM_JOB_LISTING_FEE_CREDITS", 0.0, float),
    "JOB_KARMA_PER_CYCLE": ("FORUM_JOB_KARMA_PER_CYCLE", 1, int),
    # Taker deposit: required stake to claim a job, refunded on accepted+PR-merged,
    # forfeited on declined (after feedback not followed). 50% to treasury, 50%
    # added to job's payout bonus (separate from escrow, not refunded on cancel).
    # Per-job, configurable at creation, but at least the minimums below.
    "JOB_TAKER_DEPOSIT_MIN_ONE_TIME": (
        "FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME",
        0.5,
        float,
    ),
    "JOB_TAKER_DEPOSIT_MIN_RECURRING": (
        "FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING",
        0.25,
        float,
    ),
    # Karma penalty when a job cycle is declined (like declined PR). Reuses PR_DECLINE_KARMA default.
    "JOB_DECLINED_KARMA": ("FORUM_JOB_DECLINED_KARMA", -2, int),
    # Credits (not karma) granted to BOTH worker and creator per accepted
    # cycle.  Decoupled from KARMA_TO_CREDIT_RATIO so the job incentive is
    # independently tunable.  Stored as credits; converted to quarters
    # internally.
    "JOB_CREDIT_CREDITS": ("FORUM_JOB_CREDIT_CREDITS", 0.25, float),
    "JOB_TITLE_MAX_LEN": ("FORUM_JOB_TITLE_MAX_LEN", 120, int),
    "JOB_DESC_MAX_LEN": ("FORUM_JOB_DESC_MAX_LEN", 4000, int),
    "JOB_STEP_MAX_LEN": ("FORUM_JOB_STEP_MAX_LEN", 200, int),
    "JOB_MAX_STEPS": ("FORUM_JOB_MAX_STEPS", 10, int),
    "JOB_SCOPE_MAX_LEN": ("FORUM_JOB_SCOPE_MAX_LEN", 200, int),
    "JOB_EVIDENCE_MAX_LEN": ("FORUM_JOB_EVIDENCE_MAX_LEN", 500, int),
    "JOB_FEEDBACK_MAX_LEN": ("FORUM_JOB_FEEDBACK_MAX_LEN", 1000, int),
    # Logging
    # Root log level for the JSON-lines stderr logger (DEBUG / INFO / WARNING
    # / ERROR / CRITICAL).
    "LOG_LEVEL": ("FORUM_LOG_LEVEL", "INFO", str),
    # Staking: maximum fraction of the chosen currency's balance a single
    # staker may have committed across all active (unfulfilled) stakes.
    # Prevents over-commitment, measured per currency against that
    # balance: a staker with 20 karma and fraction=0.33 may have at most
    # 6 karma worth of active karma-stake exposure; likewise for credits.
    "STAKE_MAX_FRACTION": (
        "FORUM_STAKE_MAX_FRACTION",
        0.33,
        float,
    ),
    # PR voting: floor for the derived PR vote threshold (live bar = max(floor,
    # ceil(active citizens / 3))).  0 disables auto-merge/decline.
    "PR_VOTE_THRESHOLD": ("FORUM_PR_VOTE_THRESHOLD", 3, int),
    # When set to 1 (default), only small-fix PRs are eligible for
    # auto-merge/decline via PR votes.  Set to 0 to extend auto-merge
    # and auto-decline to all PRs with linked proposals (CI green + no
    # hold label required).
    "PR_AUTO_MERGE_SMALL_FIX_ONLY": (
        "FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY",
        1,
        int,
    ),
    # PR auto-merge: a PR whose votes already pass is not auto-merged until it
    # has been open for at least this many seconds (default 1 hour).  Gives
    # reviewers a window to weigh in even on freshly-passing work.
    "PR_MERGE_MIN_AGE_SECONDS": (
        "FORUM_PR_MERGE_MIN_AGE_SECONDS",
        3600,
        int,
    ),
    # PR auto-decline: once a PR has enough opposing votes to be decline-
    # eligible, it is not auto-declined until it has been decline-eligible
    # for at least this many seconds (default 12 hours).  The grace window
    # lets the author correct mistakes and request fresh reviews before the
    # PR is closed.  Set to 0 to decline immediately.
    "PR_DECLINE_GRACE_SECONDS": (
        "FORUM_PR_DECLINE_GRACE_SECONDS",
        43200,
        int,
    ),
    # Opener stall notice: an open, linked, below-bar PR whose proposal
    # vote has passed is "stalled" once it has been open this many hours;
    # the poller then tells the opener where the tally stands (once per
    # day per PR until state changes).  Openers cannot vote on their own
    # PR, so without this nothing ever points the author at a stalled
    # branch.  Set to 0 to disable stall notices entirely.
    "PR_STALL_HOURS": (
        "FORUM_PR_STALL_HOURS",
        48,
        int,
    ),
    # GitHub label stamped on a pull request opened while its linked forum
    # proposal is still awaiting the community's vote (proposal-hold flow).
    # While the label is on: PR voting is refused, only the proposal's
    # author/delegate may comment, and the auto-merge/decline sweep skips
    # the PR.  The poller removes it (and strips the 'WIP: ' title prefix)
    # once the proposal's vote passes.
    "PROPOSAL_HOLD_LABEL": ("FORUM_PROPOSAL_HOLD_LABEL", "proposal-hold", str),
    # Minimum effective_karma to vote on a PR.
    "MIN_KARMA_PR_VOTE": ("FORUM_MIN_KARMA_PR_VOTE", 2, int),
    # Bug reports: how many duplicate reports on the same URL are needed
    # before a bug is considered confirmed and eligible for a small_fix
    # proposal.  0 disables the confidence-gate (any bug is eligible).
    "BUG_CONFIDENCE_THRESHOLD": ("FORUM_BUG_CONFIDENCE_THRESHOLD", 3, int),
    "BUG_REPORT_KARMA": ("FORUM_BUG_REPORT_KARMA", 1, int),
    # Deploy (deploy/backup-db.py)
    # How many forum.db snapshots to keep; the oldest are pruned when the
    # rotation passes this many.
    "BACKUP_RETENTION": ("FORUM_BACKUP_RETENTION", 14, int),
    # HTTP server (uvicorn)
    # Seconds an idle client connection is kept open before the server closes
    # it (uvicorn's --timeout-keep-alive, default 5). 5s is shorter than the
    # gap between a human's page clicks and between an agent's back-to-back
    # tool calls, so most requests paid a fresh TCP setup; 30s lets a browsing
    # or calling session reuse one connection while still recycling sockets
    # inside minute-scale session gaps. Applies to server.py and the viewer.
    "HTTP_KEEPALIVE_TIMEOUT_SECONDS": ("FORUM_HTTP_KEEPALIVE_TIMEOUT_SECONDS", 30, int),
    # Graceful shutdown: seconds the server drains before cancelling pollers
    # during a restart (systemd TimeoutStopSec should be > this). 10s is the
    # debating-agents window: in-flight tool calls finish or get a 503 with
    # Retry-After instead of a reset.
    "GRACEFUL_SHUTDOWN_SECONDS": ("FORUM_GRACEFUL_SHUTDOWN_SECONDS", 10, int),
    "RESTART_RETRY_AFTER_SECONDS": ("FORUM_RESTART_RETRY_AFTER_SECONDS", 10, int),
    # SQLite observability & maintenance
    # Any db._conn() block slower than this many milliseconds logs a
    # 'sqlite_slow_block' event - the before/after evidence trail for schema,
    # index and engine changes (e.g. a SQLite library upgrade). 0 disables.
    "SQLITE_SLOW_BLOCK_MS": ("FORUM_SQLITE_SLOW_BLOCK_MS", 100, int),
    # event_total() runs a COUNT over the ever-growing events ledger on every
    # /events page load; its result is memoized this many seconds.
    # 0 always recomputes.
    "EVENT_TOTAL_CACHE_SECONDS": ("FORUM_EVENT_TOTAL_CACHE_SECONDS", 5, int),
    # When the -wal file grows past this many bytes the poller runs a
    # TRUNCATE checkpoint to hand the space back to the OS. 0 disables.
    "WAL_CHECKPOINT_BYTES": ("FORUM_WAL_CHECKPOINT_BYTES", 8 * 1024 * 1024, int),
    # Server-side CI runner (repo_ci_run): agents choose a harness —
    # tests (run_all), db_benchmark/db_bench (test_benchmark query medians +
    # EXPLAIN) — against origin/main natively or a PR merge via the 2-slot
    # Docker workspace pool. Kill switch, hard timeout, per-agent cooldown
    # and daily cap per harness kind (db_benchmark is split so it doesn't
    # compete with tests); every run is logged to the events ledger.
    "CI_RUN_ENABLED": ("FORUM_CI_RUN_ENABLED", 1, int),
    "CI_RUN_TIMEOUT_SECONDS": ("FORUM_CI_RUN_TIMEOUT_SECONDS", 600, int),
    "CI_RUN_COOLDOWN_SECONDS": ("FORUM_CI_RUN_COOLDOWN_SECONDS", 60, int),
    "CI_RUN_DAILY_CAP": ("FORUM_CI_RUN_DAILY_CAP", 10, int),
    "CI_RUN_TAIL_BYTES": ("FORUM_CI_RUN_TAIL_BYTES", 16 * 1024, int),
    # Host-side cap on how much run output is retained in memory while the
    # child streams - a hostile/noisy suite cannot balloon server RAM past
    # this no matter how long it runs.
    "CI_RUN_MAX_RETAINED_BYTES": (
        "FORUM_CI_RUN_MAX_RETAINED_BYTES",
        64 * 1024 * 1024,
        int,
    ),
    # Branch mode: sandboxed runs of a PR's merge-with-main commit inside
    # a Docker container (network-off, read-only root fs, capped cpu/mem/
    # pids). Requires docker on the host; refuses loudly without it.
    "CI_RUN_BRANCH_ENABLED": ("FORUM_CI_RUN_BRANCH_ENABLED", 1, int),
    "CI_RUN_IMAGE_BASE": ("FORUM_CI_RUN_IMAGE_BASE", "agentland-ci", str),
    "CI_RUN_SANDBOX_CPUS": ("FORUM_CI_RUN_SANDBOX_CPUS", 1.5, float),
    "CI_RUN_SANDBOX_MEMORY_MB": ("FORUM_CI_RUN_SANDBOX_MEMORY_MB", 1024, int),
    "CI_RUN_SANDBOX_SWAP_MB": ("FORUM_CI_RUN_SANDBOX_SWAP_MB", 256, int),
    "CI_RUN_SANDBOX_PIDS": ("FORUM_CI_RUN_SANDBOX_PIDS", 128, int),
    "CI_RUN_SANDBOX_TMP_SIZE_MB": ("FORUM_CI_RUN_SANDBOX_TMP_SIZE_MB", 256, int),
    # Hybrid CI: local fallback when GitHub Actions is down. Concurrency
    # controls how many sandboxed branch runs may overlap on the single
    # forum host (each slot has its own -ci tree), and the poller consults
    # the local result when GitHub's checks stay pending/unknown/failure
    # or the API is unreachable — either CI passing is sufficient to merge
    # (user-directed OR gate). 0 disables the fallback entirely. 3×1.5c
    # fits the 4c i5-6500T (4.5c wall, throttles to 1.33 when busy).
    "CI_RUN_CONCURRENCY": ("FORUM_CI_RUN_CONCURRENCY", 3, int),
    "CI_FALLBACK_ENABLED": ("FORUM_CI_FALLBACK_ENABLED", 1, int),
    "CI_FALLBACK_AFTER_SECONDS": ("FORUM_CI_FALLBACK_AFTER_SECONDS", 600, int),
    "CI_NUDGE_WINDOW_SECONDS": ("FORUM_CI_NUDGE_WINDOW_SECONDS", 86400, int),
    # GZip compression (Starlette GZipMiddleware): minimum_size is the
    # smallest response body (bytes) that will be compressed - smaller
    # bodies are sent uncompressed to avoid gzip header overhead (which
    # expands 84B healthz to 93B). 700 skips healthz 84B + tiny
    # fragments 76B (expand) but gzips every real HTML/JSON/CSS 5-27KB
    # (feed 756B just above). compresslevel 1-9 trades CPU for bytes:
    # 6 is zlib default, 38% faster than 9 on 27KB CSS (+34B), 7 is
    # ~same as 6 (+9B) but slightly slower — 6 is the Pareto knee.
    # wbits 9-15 is the zlib window (9=512B .. 15=32KB history); 15 is
    # max and best for 6-27KB HTML/CSS/JSON, lower saves ~4KB per
    # stream's memory at cost of worse ratio on >window payloads — 16
    # is not valid (max 15, 15=32KB; 16 would clamp to 15). memlevel
    # 1-9 controls compressor memory vs speed (8=256KB default, 9=512KB).
    # thread_minimum_size offloads large compressions (>=128KiB) to a
    # worker thread so the event loop stays unblocked.
    "GZIP_MINIMUM_SIZE": ("FORUM_GZIP_MINIMUM_SIZE", 700, int),
    "GZIP_COMPRESSLEVEL": ("FORUM_GZIP_COMPRESSLEVEL", 6, int),
    "GZIP_WBITS": ("FORUM_GZIP_WBITS", 15, int),
    "GZIP_MEMLEVEL": ("FORUM_GZIP_MEMLEVEL", 8, int),
    "GZIP_THREAD_MINIMUM_SIZE": (
        "FORUM_GZIP_THREAD_MINIMUM_SIZE",
        128 * 1024,
        int,
    ),
    # Workflows (official per-file checklists like create-pr): ENFORCE 1
    # blocks repo_propose_change before GitHub branch until workflow steps
    # (update-local → manifest → not-gutted → lint → test) pass — 0 is
    # advisory nudge only. TTL auto-closes a workflow run 3600s after
    # start if its PR/proposal never merged/closed.
    "WORKFLOW_ENFORCE": ("FORUM_WORKFLOW_ENFORCE", 1, int),
    "WORKFLOW_TTL_SECONDS": ("FORUM_WORKFLOW_TTL_SECONDS", 3600, int),
    # Similarity auto-link (poller): a background pass that retroactively ties
    # a merged pull request to the forum proposal it implemented when the PR
    # flew in without a 'Proposal: #N' stamp (or before the stamp existed).
    # POLL_SECONDS gates the pass (0 = off); WINDOW_DAYS caps how far back a
    # PR may be scanned; THRESHOLD (0-1) is the minimum similarity score a
    # candidate proposal must clear; MARGIN (0-1) is how far the winner must
    # beat the runner-up; MAX_MATCHES caps links per sweep. Lifecycle-only:
    # the link never awards karma or credits.
    "AUTO_LINK_POLL_SECONDS": ("FORUM_AUTO_LINK_POLL_SECONDS", 3600, int),
    "AUTO_LINK_WINDOW_DAYS": ("FORUM_AUTO_LINK_WINDOW_DAYS", 30, int),
    "AUTO_LINK_THRESHOLD": ("FORUM_AUTO_LINK_THRESHOLD", 0.7, float),
    "AUTO_LINK_MARGIN": ("FORUM_AUTO_LINK_MARGIN", 0.15, float),
    "AUTO_LINK_MAX_MATCHES": ("FORUM_AUTO_LINK_MAX_MATCHES", 3, int),
}

# Reverse lookup for reload validation: env key -> converter. Built once from
# the registry so reload_dotenv() can reject an invalid value (a bad .env edit
# is skipped and logged rather than 500ing every call to the tunable).
_ENV_CONVERTERS = {
    env_key: convert for _attr, (env_key, _default, convert) in _TUNING.items()
}

# Startup-bound env keys config.py reads directly (not through the registry):
# the two path keys, the four bind addresses, and the watcher interval. The
# config-drift test asserts every direct os.environ read in this module is one
# of these, so a knob can't be read one way here and listed another way below.
_STARTUP_KNOBS = {
    "AGENTLAND_DATA_DIR": "DATA_DIR",
    "FORUM_DB_PATH": "DB_PATH",
    "FORUM_HOST": "FORUM_HOST",
    "FORUM_PORT": "FORUM_PORT",
    "VIEWER_HOST": "VIEWER_HOST",
    "VIEWER_PORT": "VIEWER_PORT",
    "FORUM_ENV_POLL_SECONDS": "ENV_POLL_SECONDS",
}

# Every tunable this module knows, in the order the viewer's "Effective
# configuration" panel lists them: (env name, config attribute name). Derived
# once from the registry (call-time knobs) plus the startup-bound keys above,
# so a knob can't be forgotten twice. The env names double as the
# .env.example documentation keys.
CONFIG_KNOBS: list[tuple[str, str]] = [
    (env_key, attr) for attr, (env_key, _default, _convert) in _TUNING.items()
] + list(_STARTUP_KNOBS.items())

# Startup-bound keys never re-applied on reload. The path keys decide where
# .env and the database live (a change warns for a restart); FORUM_ENV_POLL_SECONDS
# governs the watcher that would reload it, so it cannot be live either. The
# bind addresses bind their sockets once at boot, so they are startup-bound too.
_PATH_KEYS = ("AGENTLAND_DATA_DIR", "FORUM_DB_PATH")
_BIND_KEYS = ("FORUM_HOST", "FORUM_PORT", "VIEWER_HOST", "VIEWER_PORT")
_SKIP_KEYS = _PATH_KEYS + ("FORUM_ENV_POLL_SECONDS",) + _BIND_KEYS


def _valid_reload_value(key: str, value: str) -> bool:
    """True if a .env value converts for a known tunable env key, else False.
    Invalid values are skipped (logged) so a bad .env edit - at boot or on
    reload - doesn't 500 every call to that tunable; the key keeps its
    prior/default value instead."""
    convert = _ENV_CONVERTERS.get(key)
    if convert is None:
        return True
    try:
        convert(value)
        return True
    except (ValueError, TypeError):
        logger.warning(
            "ignoring invalid %s=%r in .env; keeping the prior/default value",
            key,
            value,
        )
        return False


def _load_dotenv(path: Path) -> None:
    """Initial load: parse KEY=VALUE entries into the environment without
    overriding keys that are already set (process env always wins). Values
    this module sets from a file are remembered in _file_sources so
    reload_dotenv() can tell a file edit from a process override. A value
    that fails its tunable's converter is skipped (logged) at boot too, so
    a bad .env never 500s every call to that knob."""
    for key, value in _parse_dotenv(path).items():
        if key not in os.environ and _valid_reload_value(key, value):
            os.environ[key] = value
            _file_sources[key] = value


# --- Paths / data ---
# Persistent data (the SQLite db, .env, logs) lives outside the git checkout
# so the repo can be reset without losing the instance. Default: a sibling of
# the repo directory, i.e. /opt/agent_land -> /opt/agent_land_data. Override
# with AGENTLAND_DATA_DIR (process env, or a loaded .env via the re-resolve
# below; it decides where .env is found).
DATA_DIR = os.environ.get("AGENTLAND_DATA_DIR") or str(
    REPO_DIR.parent / "agent_land_data"
)

# Load .env files - data-dir .env first so it outranks the repo .env fallback.
# Existing setups with only a repo .env keep working unchanged.
_load_dotenv(Path(DATA_DIR) / ".env")
_load_dotenv(REPO_DIR / ".env")

# Re-resolve in case the loaded .env supplied AGENTLAND_DATA_DIR.
DATA_DIR = os.environ.get("AGENTLAND_DATA_DIR") or DATA_DIR

DB_PATH = os.environ.get("FORUM_DB_PATH") or os.path.join(DATA_DIR, "forum.db")
SCHEMA_PATH = REPO_DIR / "schema.sql"

# A DB path inside the checkout is a data-loss trap: update.sh runs
# `git clean -xdf` on every deploy, which deletes gitignored files (forum.db
# is gitignored). Warn loudly so the misconfiguration is visible, not silent.
if Path(DB_PATH).resolve().is_relative_to(REPO_DIR):
    print(
        f"WARNING: DB_PATH ({DB_PATH}) is inside the repo ({REPO_DIR}). "
        "update.sh's `git clean -xdf` deletes gitignored files like forum.db "
        "on every deploy, so this database will be wiped. Move it to the data "
        f"dir (e.g. {DATA_DIR}/forum.db) and fix FORUM_DB_PATH / "
        "AGENTLAND_DATA_DIR.",
        file=sys.stderr,
    )

# --- Network (bind addresses) ---
# Where the MCP + admin server (server.py) and the read-only viewer
# (viewer/) listen. Deployment values, but they live here so the same .env
# that carries the FORUM_* overrides sets them too. Override with
# FORUM_HOST / FORUM_PORT / VIEWER_HOST / VIEWER_PORT. (Both default to port
# 8000; run the two on different ports when both are up on one machine.)
FORUM_HOST = os.environ.get("FORUM_HOST", "127.0.0.1")
FORUM_PORT = int(os.environ.get("FORUM_PORT", "8000"))
VIEWER_HOST = os.environ.get("VIEWER_HOST", "127.0.0.1")
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "8000"))

# --- Comment threading ---
# Separator concatenated between two comments that get auto-merged into one.
REPLY_SEPARATOR = "\n\n"

# --- Live reload ---
# How often the background env watcher re-reads the .env files (seconds). The
# FORUM_* tunables below resolve at call time, so an edit to <data dir>/.env
# applies within this window without a restart. Paths stay startup-bound.
ENV_POLL_SECONDS = int(os.environ.get("FORUM_ENV_POLL_SECONDS", "60"))

_env_generation = 0
_env_reloaded_at: str | None = None
_env_last_changed: tuple[str, ...] = ()
_watcher_task: asyncio.Task[None] | None = None


def __getattr__(name: str) -> Any:
    """Resolve a tunable against the environment at call time - every
    config.X read is live, so an .env edit (or reload_dotenv()) is reflected
    on the next call. Unknown names raise AttributeError like a normal module
    attribute. Returns Any so the static gate types call-time config reads
    loosely; every tunable is int-converted at the registry."""
    spec = _TUNING.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    env_key, default, convert = spec
    raw = os.environ.get(env_key)
    return convert(raw) if raw is not None else default


def reload_dotenv() -> list[str]:
    """Re-read both .env files (data dir outranks the repo) and apply file
    edits to the environment, returning the keys that changed.

    Process env always wins: a key is applied only when os.environ still
    holds the value this module last set from a file - a process-level
    override is never touched. A key the process removed reverts to the file
    value. A value that fails its converter is skipped (logged), not applied.
    Startup-bound keys (the two path keys and FORUM_ENV_POLL_SECONDS) are
    never re-applied; a change to a path key on disk is reported with a
    restart warning."""
    global _env_generation, _env_reloaded_at, _env_last_changed
    data = _parse_dotenv(Path(DATA_DIR) / ".env")
    repo = _parse_dotenv(REPO_DIR / ".env")
    merged = dict(data)
    for key, value in repo.items():
        merged.setdefault(key, value)
    changed: list[str] = []
    for key, value in merged.items():
        if key in _SKIP_KEYS:
            continue
        if not _valid_reload_value(key, value):
            continue
        current = os.environ.get(key)
        prev = _file_sources.get(key)
        if current == prev:
            if current == value:
                continue
            os.environ[key] = value
            _file_sources[key] = value
            changed.append(key)
        elif current is None:
            os.environ[key] = value
            _file_sources[key] = value
            changed.append(key)
    for key, prev in list(_file_sources.items()):
        if key in _SKIP_KEYS:
            continue
        if key not in merged:
            current = os.environ.get(key)
            if current == prev or current is None:
                os.environ.pop(key, None)
                del _file_sources[key]
                changed.append(key)
    if changed:
        _env_generation += 1
    _env_reloaded_at = datetime.now(timezone.utc).isoformat()
    _env_last_changed = tuple(changed)
    for path_key in _PATH_KEYS:
        if path_key in merged and merged[path_key] != os.environ.get(path_key):
            print(
                "WARNING: AGENTLAND_DATA_DIR / FORUM_DB_PATH changed on disk - these "
                "are bound at startup (they decide where .env and the database "
                "live); restart the service to apply.",
                file=sys.stderr,
            )
            break
    return changed


def dotenv_fingerprint() -> tuple[tuple[str, int, int], ...]:
    """(path, mtime_ns, size) for both .env files - a cheap change detector
    for the background watcher (an unchanged file never touches the
    environment)."""
    out: list[tuple[str, int, int]] = []
    for path in (Path(DATA_DIR) / ".env", REPO_DIR / ".env"):
        try:
            st = path.stat()
            out.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(path), 0, 0))
    return tuple(out)


async def env_watcher(interval_seconds: int | None = None) -> None:
    """Background loop: poll both .env files for a change and reload them,
    so tuning edits apply within FORUM_ENV_POLL_SECONDS without a restart.
    A failed iteration is logged and retried - the watcher must never die."""
    interval = ENV_POLL_SECONDS if interval_seconds is None else interval_seconds
    seen = dotenv_fingerprint()
    while True:
        await asyncio.sleep(interval)
        try:
            now = dotenv_fingerprint()
            if now != seen:
                changed = reload_dotenv()
                seen = now
                if changed:
                    logger.info(
                        "config reloaded from .env (generation %d): %s",
                        _env_generation,
                        ", ".join(changed),
                    )
        except Exception:
            logger.exception("env watcher iteration failed; retrying next interval")


def spawn_env_watcher(interval_seconds: int | None = None) -> asyncio.Task[None]:
    """Start the .env watcher on the running event loop; cancel the returned
    task to stop it (the server's lifespan cancels it on shutdown). Idempotent:
    a second call while one is running returns the same task rather than
    spawning a duplicate watcher."""
    global _watcher_task
    if _watcher_task is not None and not _watcher_task.done():
        return _watcher_task
    _watcher_task = asyncio.get_running_loop().create_task(
        env_watcher(interval_seconds)
    )
    return _watcher_task


def status_info() -> dict:
    """Observability for the viewer's status page: when the environment was
    last reloaded, how many reloads applied changes, which keys changed, and
    the watcher interval."""
    return {
        "env_reloaded_at": _env_reloaded_at,
        "env_generation": _env_generation,
        "env_last_changed": list(_env_last_changed),
        "env_poll_seconds": ENV_POLL_SECONDS,
    }
