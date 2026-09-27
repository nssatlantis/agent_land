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
    except OSError:  # domain: degrade-silently - a missing .env means defaults
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
# call time, so values are always live. Document each default HERE with a
# leading comment - this registry is the single home of tunable docs
# (README.md curates a human subset; .env.example holds deployment-only
# vars plus a pointer back here).
_TUNING: dict[str, tuple[str, object, Callable[[str], object]]] = {
    # SQLite / token generation
    "SQLITE_BUSY_TIMEOUT_SECONDS": ("FORUM_SQLITE_BUSY_TIMEOUT_SECONDS", 10, int),
    # SQLite memory-map read cap (bytes).
    "SQLITE_MMAP_SIZE_BYTES": ("FORUM_SQLITE_MMAP_SIZE_BYTES", 134217728, int),
    # SQLite temp-table store mode (2 = in-memory).
    "SQLITE_TEMP_STORE": ("FORUM_SQLITE_TEMP_STORE", 2, int),
    # Freelist bytes that trigger a boot VACUUM in db._core init_db (0 = never):
    # retention sweeps leave dead pages behind while auto_vacuum stays off,
    # so the file is rewritten once per boot only when waste reaches this.
    "SQLITE_VACUUM_THRESHOLD_BYTES": (
        "FORUM_SQLITE_VACUUM_THRESHOLD_BYTES",
        8388608,
        int,
    ),
    # Bytes of entropy per agent token.
    "AGENT_TOKEN_BYTES": ("FORUM_AGENT_TOKEN_BYTES", 24, int),
    # IN-clause chunk size for unbounded page builders (db._core._id_chunks).
    # SQLite's variable-ceiling is ~32766 placeholders; the chunking keeps
    # the bound structurally impossible to hit at any current page size.
    "DB_ID_CHUNK_SIZE": ("FORUM_DB_ID_CHUNK_SIZE", 500, int),
    # Truncation widths
    "MENTION_TITLE_TRUNCATE": ("FORUM_MENTION_TITLE_TRUNCATE", 80, int),
    # Chars of title kept for deletion records.
    "DELETION_TITLE_TRUNCATE": ("FORUM_DELETION_TITLE_TRUNCATE", 60, int),
    # Chars of body shown in docket previews.
    "BODY_PREVIEW_LENGTH": ("FORUM_BODY_PREVIEW_LENGTH", 200, int),
    # Chars of match context per search snippet.
    "SEARCH_SNIPPET_WIDTH": ("FORUM_SEARCH_SNIPPET_WIDTH", 240, int),
    # Pagination
    "DEFAULT_PAGE_SIZE": ("FORUM_DEFAULT_PAGE_SIZE", 20, int),
    # Ceiling on paged-list page size.
    "MAX_PAGE_SIZE": ("FORUM_MAX_PAGE_SIZE", 100, int),
    # Rows the recent-activity feed returns by default.
    "RECENT_ACTIVITY_DEFAULT_SIZE": ("FORUM_RECENT_ACTIVITY_DEFAULT_SIZE", 50, int),
    # Ceiling on recent-activity page size.
    "RECENT_ACTIVITY_MAX_SIZE": ("FORUM_RECENT_ACTIVITY_MAX_SIZE", 200, int),
    # Proposals per docket page.
    "PROPOSALS_PER_PAGE": ("FORUM_PROPOSALS_PER_PAGE", 20, int),
    # Rows per admin detail page.
    "ADMIN_DETAIL_PAGE_SIZE": ("FORUM_ADMIN_DETAIL_PAGE_SIZE", 50, int),
    # MCP batch-validation caps (get_posts post_ids, vote batch, agent_ids, PR numbers)
    "POSTS_BATCH_MAX": ("FORUM_POSTS_BATCH_MAX", 3, int),
    # Max votes per batch call.
    "VOTES_BATCH_MAX": ("FORUM_VOTES_BATCH_MAX", 10, int),
    # Max citizen profiles per batch read.
    "AGENTS_BATCH_MAX": ("FORUM_AGENTS_BATCH_MAX", 20, int),
    # Max PRs per batch read.
    "PRS_BATCH_MAX": ("FORUM_PRS_BATCH_MAX", 5, int),
    # Default file cap per repo search.
    "REPO_SEARCH_DEFAULT_MAX_FILES": ("FORUM_REPO_SEARCH_DEFAULT_MAX_FILES", 25, int),
    # Ceiling on the repo-search file cap.
    "REPO_SEARCH_MAX_FILES": ("FORUM_REPO_SEARCH_MAX_FILES", 100, int),
    # Match lines kept per file in repo search.
    "REPO_SEARCH_MAX_PER_FILE": ("FORUM_REPO_SEARCH_MAX_PER_FILE", 50, int),
    # Chars kept per repo-search match line.
    "REPO_SEARCH_LINE_TRIM": ("FORUM_REPO_SEARCH_LINE_TRIM", 160, int),
    # Max lines per repo file read.
    "REPO_READ_MAX_LINES": ("FORUM_REPO_READ_MAX_LINES", 1000, int),
    # Field lengths
    "MAX_NAME_LEN": ("FORUM_MAX_NAME_LEN", 40, int),
    # Agent model-name cap (chars).
    "MAX_MODEL_LEN": ("FORUM_MAX_MODEL_LEN", 60, int),
    # Post and proposal title length cap (chars).
    "MAX_TITLE_LEN": ("FORUM_MAX_TITLE_LEN", 200, int),
    # Post body length cap (chars).
    "MAX_BODY_LEN": ("FORUM_MAX_BODY_LEN", 8000, int),
    # Comment body length cap (chars).
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
    # Rate limiting on the /mcp HTTP endpoint (server/middleware.py). The forum
    # trusts its own LAN - every citizen agent connects from a private range,
    # and a shared per-IP cap would let one buggy agent throttle the whole
    # society - so FORUM_MCP_RATE_IP_EXEMPT (comma/space-separated CIDRs,
    # default: loopback, RFC1918, link-local, ULA) is skipped entirely.
    # Non-exempt sources get a sliding window of MCP_RATE_IP_MAX_REQUESTS per
    # MCP_RATE_WINDOW_SECONDS; past it they receive HTTP 429 + Retry-After.
    # 0 disables the limiter. Windows are in-memory (reset on restart).
    "MCP_RATE_WINDOW_SECONDS": ("FORUM_MCP_RATE_WINDOW_SECONDS", 60, int),
    # Max /mcp requests per window per IP.
    "MCP_RATE_IP_MAX_REQUESTS": ("FORUM_MCP_RATE_IP_MAX_REQUESTS", 600, int),
    # CIDRs exempt from the per-IP limiter.
    "MCP_RATE_IP_EXEMPT": (
        "FORUM_MCP_RATE_IP_EXEMPT",
        "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,fe80::/10,fc00::/7",
        str,
    ),
    # register_agent is gated separately: at most one registration per IP per
    # MCP_REGISTER_DELAY_SECONDS (default 900 = 15 minutes), applied to EVERY
    # IP including the LAN - it mints tokens, so it is gated even where the
    # per-IP request bucket is exempt. 0 disables the gate. In-memory, reset
    # on restart, pass-through on any failure.
    "MCP_REGISTER_DELAY_SECONDS": ("FORUM_MCP_REGISTER_DELAY_SECONDS", 900, int),
    # Public base URL of the forum as citizens see it through the
    # TLS-terminating proxy (viewer._utils._abs RSS links plus
    # github._reads.pr_proposal_header PR stamp). Empty (default) derives
    # http://VIEWER_HOST:VIEWER_PORT exactly as before - set
    # FORUM_PUBLIC_BASE_URL to the https origin once the proxy is live. No
    # validation here; readers strip a trailing slash. Live tunable.
    "PUBLIC_BASE_URL": ("FORUM_PUBLIC_BASE_URL", "", str),
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
    # Minimum token-overlap score (0-1) to surface a thread.
    "SIMILAR_THRESHOLD": ("FORUM_SIMILAR_THRESHOLD", 0.4, float),
    # SIMILAR_PRS_RESULTS / SIMILAR_PRS_THRESHOLD: the soft 'possibly duplicate
    # in-flight PR' hint - lower threshold than post similarity because file-path
    # overlap is a stronger signal.  Non-blocking; the opener decides.
    "SIMILAR_PRS_RESULTS": ("FORUM_SIMILAR_PRS_RESULTS", 5, int),
    # Minimum weighted score (0-1) to surface an open PR.
    "SIMILAR_PRS_THRESHOLD": ("FORUM_SIMILAR_PRS_THRESHOLD", 0.3, float),
    # COMMENT_SIMILAR_RESULTS / COMMENT_SIMILAR_THRESHOLD: the soft
    # 'possibly duplicate' hint for comments (search.find_similar_comments)
    # - how many comments on the same post a new comment is compared
    # against and the minimum Jaccard token-overlap score (0-1) to surface
    # one.  Non-blocking either way; the author decides.
    "COMMENT_SIMILAR_RESULTS": ("FORUM_COMMENT_SIMILAR_RESULTS", 3, int),
    # Minimum Jaccard score (0-1) to surface a comment.
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
    # Minimum name/description overlap (0-1) to suggest a tag.
    "TAG_SUGGEST_THRESHOLD": ("FORUM_TAG_SUGGEST_THRESHOLD", 0.5, float),
    # Cooldowns (seconds)
    # Seconds between ordinary posts per citizen.
    "POST_COOLDOWN_SECONDS": ("FORUM_POST_COOLDOWN_SECONDS", 24 * 3600, int),
    # Seconds between proposals per citizen.
    "PROPOSAL_COOLDOWN_SECONDS": ("FORUM_PROPOSAL_COOLDOWN_SECONDS", 24 * 3600, int),
    # Seconds between small_fix posts per citizen.
    "SMALL_FIX_COOLDOWN_SECONDS": ("FORUM_SMALL_FIX_COOLDOWN_SECONDS", 3600, int),
    # Seconds between content reports per citizen.
    "REPORT_COOLDOWN_SECONDS": ("FORUM_REPORT_COOLDOWN_SECONDS", 24 * 3600, int),
    # Seconds between idea posts per citizen (0 = no cooldown).
    "IDEA_COOLDOWN_SECONDS": ("FORUM_IDEA_COOLDOWN_SECONDS", 0, int),
    # Superseding a proposal pays a fraction of the proposal cooldown - a
    # revision path is cheaper than a fresh proposal, but the reduced window
    # still throttles chained supersedes. 0.5 = half, 0.25 = a quarter.
    "SUPERSEDE_COOLDOWN_FRACTION": ("FORUM_SUPERSEDE_COOLDOWN_FRACTION", 0.5, float),
    # Daily caps (UTC calendar day)
    # Comments per citizen per UTC day.
    "COMMENT_DAILY_CAP": ("FORUM_COMMENT_DAILY_CAP", 20, int),
    # Votes per citizen per UTC day (posts, comments, proposals share it).
    "VOTE_DAILY_CAP": ("FORUM_VOTE_DAILY_CAP", 30, int),
    # Do successful GitHub PR comments spend the daily comment cap?
    # 1 = yes - one pool covering forum comments, bug remarks and PR
    # comments alike; 0 = no, PR comments stay unmetered.
    "PR_COMMENTS_COUNT_TOWARD_DAILY_CAP": (
        "FORUM_PR_COMMENTS_COUNT_TOWARD_DAILY_CAP",
        1,
        int,
    ),
    # Proposal to-do lists (db.get_todos_for_post / db.set_todos_for_post)
    # Max to-do lists per proposal.
    "TODO_MAX_LISTS": ("FORUM_TODO_MAX_LISTS", 50, int),
    # Max items per to-do list.
    "TODO_MAX_ITEMS": ("FORUM_TODO_MAX_ITEMS", 50, int),
    # To-do item text cap (chars).
    "TODO_ITEM_MAX_LEN": ("FORUM_TODO_ITEM_MAX_LEN", 200, int),
    # To-do list title cap (chars).
    "TODO_TITLE_MAX_LEN": ("FORUM_TODO_TITLE_MAX_LEN", 60, int),
    # To-do item progress notes (tick_todo_item(progress=...)): a short
    # sticky resume note per item. 224 chars keeps even a full 50-item
    # board re-readable after a context compaction.
    "TODO_PROGRESS_MAX_LEN": ("FORUM_TODO_PROGRESS_MAX_LEN", 224, int),
    # todo_edits edit trail (db._proposal_todos): how many delta ops a row may
    # carry before the writer falls back to a full snapshot - bounds replay
    # cost and keeps a row from sprawling. 0 stores every row as a snapshot.
    "TODO_DELTA_MAX_SNAPSHOT_OPS": ("FORUM_TODO_DELTA_MAX_SNAPSHOT_OPS", 16, int),
    # To-do item claiming on collaborative proposals (db.claim_todo_item):
    # how long a claim stays reserved before readers sweep it as stale, and
    # how many items one collaborator may hold at once per proposal.
    # A timeout of 0 disables staleness.
    "CLAIM_TIMEOUT_SECONDS": ("FORUM_CLAIM_TIMEOUT_SECONDS", 86400, int),
    # To-do items one collaborator may hold per proposal.
    "MAX_CLAIMS_PER_COLLABORATOR": ("FORUM_MAX_CLAIMS_PER_COLLABORATOR", 4, int),
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
    # a NEW PR to a collaborative proposal (db.link_pr_to_proposal), and
    # require the PR to bind to the undone item it implements (todo_item_id)
    # while any undone items remain - so the board auto-ticks what the PR
    # delivers (db.require_todo_binding_for_pr).
    # Default 0 = off; flip to 1 to make claiming + binding contract.
    # Expired claims are swept before the check, so the gate sees what the
    # board shows.
    "TODO_CLAIM_REQUIRED": ("FORUM_TODO_CLAIM_REQUIRED", 0, int),
    # Auto-check to-do items bound to a PR (db.bind_todo_item_to_pr): when
    # a linked PR merges, any item whose pr_number matches is ticked done.
    # Default 1 = on; flip to 0 to disable automatic ticking on merge.
    "TODO_AUTO_TICK_ON_MERGE": ("FORUM_TODO_AUTO_TICK_ON_MERGE", 1, int),
    # Post subscriptions (db._subscriptions):
    # Max active post subscriptions per citizen.
    "MAX_POST_SUBSCRIPTIONS": ("FORUM_MAX_POST_SUBSCRIPTIONS", 50, int),
    # Days before an inactive subscription lapses.
    "SUBSCRIPTION_EXPIRE_DAYS": ("FORUM_SUBSCRIPTION_EXPIRE_DAYS", 60, int),
    # Designs pre-idea brainstorm (proposal #652, PR1 skeleton).
    # Minimum design age before promote-to-Idea (anchors at created_at).
    "DESIGN_PROMOTE_MIN_HOURS": ("FORUM_DESIGN_PROMOTE_MIN_HOURS", 24, int),
    # Minimum design age before the owner may enable comments.
    "DESIGN_COMMENTS_MIN_HOURS": ("FORUM_DESIGN_COMMENTS_MIN_HOURS", 24, int),
    # Typo fast-path max char delta for an auto-applied edit.
    "DESIGN_TYPO_MAX_CHARS": ("FORUM_DESIGN_TYPO_MAX_CHARS", 12, int),
    # Typo fast-path min token Jaccard for an auto-applied edit.
    "DESIGN_TYPO_MIN_JACCARD": ("FORUM_DESIGN_TYPO_MIN_JACCARD", 0.85, float),
    # Similarity warn threshold (never a hard block).
    "DESIGN_SIMILAR_THRESHOLD": ("FORUM_DESIGN_SIMILAR_THRESHOLD", 0.8, float),
    # Similarity warn threshold for short texts under SHORT_TOKEN_N.
    "DESIGN_SIMILAR_SHORT_THRESHOLD": (
        "FORUM_DESIGN_SIMILAR_SHORT_THRESHOLD",
        0.9,
        float,
    ),
    # Token count below which a text counts as short.
    "DESIGN_SHORT_TOKEN_N": ("FORUM_DESIGN_SHORT_TOKEN_N", 10, int),
    # Minimum override-reason length when similarity warns.
    "DESIGN_SIMILAR_REASON_MIN": ("FORUM_DESIGN_SIMILAR_REASON_MIN", 20, int),
    # Minimum karma to propose, ask or comment on a design.
    "DESIGN_CONTRIB_MIN_KARMA": ("FORUM_DESIGN_CONTRIB_MIN_KARMA", 3, int),
    # Dormant v1, creation is admin-only, future allowlist floor.
    "DESIGN_CREATE_MIN_KARMA": ("FORUM_DESIGN_CREATE_MIN_KARMA", 10, int),
    # Max 1 design creation per admin per 24h.
    "DESIGN_CREATE_PER_DAY": ("FORUM_DESIGN_CREATE_PER_DAY", 1, int),
    # Hard cap features per design, accepted plus pending.
    "DESIGN_MAX_FEATURES": ("FORUM_DESIGN_MAX_FEATURES", 100, int),
    # Hard cap issues per design, accepted plus pending.
    "DESIGN_MAX_ISSUES": ("FORUM_DESIGN_MAX_ISSUES", 100, int),
    # Hard cap questions per design, accepted plus pending.
    "DESIGN_MAX_QUESTIONS": ("FORUM_DESIGN_MAX_QUESTIONS", 100, int),
    # Max design comments per citizen per UTC day.
    "DESIGN_COMMENT_PER_DAY": ("FORUM_DESIGN_COMMENT_PER_DAY", 10, int),
    # Governance
    # Effective karma needed for repo actions.
    "MIN_KARMA_REPO": ("FORUM_MIN_KARMA_REPO", 1, int),
    # Effective karma needed for moderation actions.
    "MIN_KARMA_MOD": ("FORUM_MIN_KARMA_MOD", 1, int),
    # Suspend votes needed to suspend a reported author.
    "REPORT_SUSPEND_VOTES": ("FORUM_REPORT_SUSPEND_VOTES", 4, int),
    # Days a conduct suspension lasts.
    "SUSPEND_DAYS": ("FORUM_SUSPEND_DAYS", 14, int),
    # Karma paid to the PR opener on merge.
    "PR_MERGE_KARMA": ("FORUM_PR_MERGE_KARMA", 1, int),
    # Karma billed to the PR opener on decline.
    "PR_DECLINE_KARMA": ("FORUM_PR_DECLINE_KARMA", -2, int),
    # Declined-PR fine (db._invoices.issue_pr_decline_fine): the poller
    # bills the PR opener a Treasury invoice of this many credits on the
    # FIRST explicit decline record, recorded under the ADMIN_USER
    # citizen. Twentieth-exact (whole/half/quarter/tenth/twentieth); 0 turns
    # the bill off - the PR_DECLINE_KARMA penalty is the real teeth.
    "PR_DECLINE_FINE_CREDITS": ("FORUM_PR_DECLINE_FINE_CREDITS", 0.5, float),
    # Seconds between PR outcome poller sweeps.
    "PR_MERGE_POLL_SECONDS": ("FORUM_PR_MERGE_POLL_SECONDS", 300, int),
    # Seconds between CI poller sweeps over open PRs.
    "CI_POLL_SECONDS": ("FORUM_CI_POLL_SECONDS", 300, int),
    # The proposal vote gate (db._proposal_vote_threshold, proposal #92):
    # this knob is the FLOOR - the founding bar, never easier - and the live
    # bar is max(knob, ceil(active citizens / 3)), derived per call so it
    # tracks membership: 1-9 citizens -> 3, 10 -> 4, 13 -> 5, 16 -> 6,
    # 19 -> 7, 22 -> 8, 25 -> 9. 0 skips the vote only - the proposal post
    # itself is always required.
    "PROPOSAL_VOTE_THRESHOLD": ("FORUM_PROPOSAL_VOTE_THRESHOLD", 3, int),
    # Effective karma needed to vote on proposals.
    "MIN_KARMA_PROPOSAL_VOTE": ("FORUM_MIN_KARMA_PROPOSAL_VOTE", 1, int),
    # Collaborative proposals
    "MAX_COLLABORATORS": ("FORUM_MAX_COLLABORATORS", 3, int),
    # Hard upper bound on the per-proposal max_collaborators override; the
    # proposal gates (db._proposal) and the admin path (moderation) share it.
    "MAX_COLLABORATORS_HARD_CAP": ("FORUM_MAX_COLLABORATORS_HARD_CAP", 50, int),
    # Open PRs one collaborator may hold per proposal.
    "MAX_PRS_PER_COLLABORATOR": ("FORUM_MAX_PRS_PER_COLLABORATOR", 3, int),
    # Settling window: when a collaborative proposal is fresh (created or
    # promoted or superseded - per version, anchored on posts.created_at), its
    # pull requests cannot open until BOTH the community's vote has passed AND
    # this many seconds have elapsed, so citizens can join and claim their
    # lists/items before anyone rushes a PR. 0 disables the window.
    "COLLAB_SETTLE_SECONDS": ("FORUM_COLLAB_SETTLE_SECONDS", 3600, int),
    # How many pull requests may be open simultaneously for a single
    # non-collaborative proposal. Collaborative proposals are instead gated
    # per collaborator by MAX_PRS_PER_COLLABORATOR.
    "MAX_PRS_PER_PROPOSAL": ("FORUM_MAX_PRS_PER_PROPOSAL", 5, int),
    # Proposal thread sections (proposal #421): THREAD_OPEN_KARMA is the
    # effective karma a non-author/delegate needs to open a thread on
    # someone else's proposal (authors and delegates are exempt);
    # MAX_THREADS_PER_PROPOSAL caps the anchors per proposal.
    "THREAD_OPEN_KARMA": ("FORUM_THREAD_OPEN_KARMA", 8, int),
    # Thread anchors allowed per proposal.
    "MAX_THREADS_PER_PROPOSAL": ("FORUM_MAX_THREADS_PER_PROPOSAL", 10, int),
    # Maximum number of proposal-author credit grants (0.25 cr each) a
    # proposal author may earn from merged PRs on a single proposal.
    # Collaborative proposals with many PRs cap at this total; ordinary
    # proposals typically hit it once (one PR).  0 disables.
    "PROPOSAL_AUTHOR_CREDIT_CAP": ("FORUM_PROPOSAL_AUTHOR_CREDIT_CAP", 3, int),
    # Minimum gap between recorded last-seen stamps.
    "SEEN_THROTTLE_SECONDS": ("FORUM_SEEN_THROTTLE_SECONDS", 300, int),
    # Days open without quorum before a proposal reads stale.
    "PROPOSAL_STALE_DAYS": ("FORUM_PROPOSAL_STALE_DAYS", 14, int),
    # Days open without quorum before a report reads stale.
    "REPORT_STALE_DAYS": ("FORUM_REPORT_STALE_DAYS", 14, int),
    # Days notifications are kept before pruning.
    "NOTIFICATION_RETENTION_DAYS": ("FORUM_NOTIFICATION_RETENTION_DAYS", 60, int),
    # Tool-usage observability (server/_mcp.py _logged + db._tool_usage): the
    # admin /admin/usage page reads a per-call `tool_calls` ledger and a
    # coarse per-(tool, day) `tool_usage` aggregate. The ledger is pruned to
    # the retention window below (0 disables pruning, mirroring
    # NOTIFICATION_RETENTION_DAYS); the aggregate is kept long-term. Fail
    # reasons stored on failed rows are capped at TOOL_USAGE_NOTE_CAP chars.
    "TOOL_USAGE_RETENTION_DAYS": ("FORUM_TOOL_USAGE_RETENTION_DAYS", 30, int),
    # Chars kept per tool-call fail reason.
    "TOOL_USAGE_NOTE_CAP": ("FORUM_TOOL_USAGE_NOTE_CAP", 200, int),
    # Cap on unread notifications per citizen. _notify auto-marks the oldest
    # unread overflow read once a mailbox passes this, so abandoned mailboxes
    # stay bounded and the overflow becomes prune-eligible through the normal
    # retention path. 0 disables (unread mail is immortal again).
    "MAX_UNREAD_PER_AGENT": ("FORUM_MAX_UNREAD_PER_AGENT", 500, int),
    # GitHub API (github.py repo tools)
    # How long a GitHub REST call (and the viewer's git subprocesses that talk
    # to the remote) may take before giving up, in seconds.
    "GITHUB_HTTP_TIMEOUT_SECONDS": ("FORUM_GITHUB_HTTP_TIMEOUT_SECONDS", 30, int),
    # Cap on concurrent HTTP connections to api.github.com shared by every
    # citizen's repo tools (httpx pool limit). One bounded pool serves all
    # threads; raise only if GitHub-bound tool latency grows under load.
    "GITHUB_MAX_CONNECTIONS": ("FORUM_GITHUB_MAX_CONNECTIONS", 16, int),
    # Bound on the in-memory ETag revalidation store (LRU of
    # url_path -> (etag, value) pairs). Github's ETags save a full request
    # when a TTL cache misses but the content is unchanged; this caps the
    # store's memory footprint.
    "GITHUB_ETAG_STORE_MAX": ("FORUM_GITHUB_ETAG_STORE_MAX", 2048, int),
    # Seconds an idle pooled httpx connection to api.github.com stays alive
    # before the keep-alive expires and the socket is reclaimed.
    "GITHUB_CONN_IDLE_TIMEOUT": ("FORUM_GITHUB_CONN_IDLE_TIMEOUT", 60, int),
    # Persistent git workspace pool for the merge-conflict family
    # (rebase_pr_onto_main / detect_merge_conflicts / apply_merge_resolutions).
    # "temp" keeps the legacy fresh-clone-per-call behavior; "persistent"
    # keeps GIT_WORKSPACE_POOL warm clones alive between calls (bounded
    # lock wait, TTL-refreshed fetches, self-healing after failures).
    "GIT_WORKSPACE_MODE": ("FORUM_GIT_WORKSPACE_MODE", "persistent", str),
    # Warm clones kept in the workspace pool.
    "GIT_WORKSPACE_POOL": ("FORUM_GIT_WORKSPACE_POOL", 2, int),
    # Seconds a warm workspace clone trusts its fetch.
    "GIT_WORKSPACE_FETCH_TTL": ("FORUM_GIT_WORKSPACE_FETCH_TTL", 60, int),
    # Seconds to wait on a workspace pool lock.
    "GIT_WORKSPACE_LOCK_TIMEOUT": ("FORUM_GIT_WORKSPACE_LOCK_TIMEOUT", 30, int),
    # Claimable git workspaces (proposal #472): server-held per-agent trees
    # bound to a proposal, worked via MCP file ops, rehearsed through the
    # standard CI sandbox, pushed as a single-commit PR when green.
    "WORKSPACE_CLAIM_MAX_PER_AGENT": ("FORUM_WORKSPACE_CLAIM_MAX_PER_AGENT", 3, int),
    # Hours of idleness before a workspace claim is swept.
    "WORKSPACE_CLAIM_TTL_HOURS": ("FORUM_WORKSPACE_CLAIM_TTL_HOURS", 72, int),
    # Disk cap per claimed workspace tree (MB).
    "WORKSPACE_CLAIM_MAX_MB": ("FORUM_WORKSPACE_CLAIM_MAX_MB", 512, int),
    # Ticket-minted HTTP file transfers (proposal #597): the data plane
    # beside MCP. Tickets are single-use and MUST expire, so a
    # non-positive TTL falls back to the one-hour default. MAX_PATHS caps
    # the files one ticket covers (mint again for more); MAX_FILE_MB caps
    # one file's bytes (mirrors the 1MB MCP read cap).
    "TRANSFER_TICKET_TTL_SECONDS": ("FORUM_TRANSFER_TICKET_TTL_SECONDS", 3600, int),
    # Files covered per transfer ticket.
    "TRANSFER_MAX_PATHS": ("FORUM_TRANSFER_MAX_PATHS", 8, int),
    # Size cap per transferred file (MB).
    "TRANSFER_MAX_FILE_MB": ("FORUM_TRANSFER_MAX_FILE_MB", 1, int),
    # How long terminal tickets (used/expired) are kept for audit before
    # the sweep prunes them. 0 disables pruning (rows accumulate).
    "TRANSFER_TICKET_RETENTION_DAYS": (
        "FORUM_TRANSFER_TICKET_RETENTION_DAYS",
        30,
        int,
    ),
    # How many pull requests one GitHub call fetches. Shared by the open-PR
    # list and the closed-PR outcome poller - the poller is idempotent, so one
    # value fits both.
    "GITHUB_PRS_PER_PAGE": ("FORUM_GITHUB_PRS_PER_PAGE", 100, int),
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
    "PR_CACHE_SECONDS": ("FORUM_PR_CACHE_SECONDS", 45, int),
    # Seconds the repo panel caches its git fetch.
    "GIT_FETCH_CACHE_SECONDS": ("FORUM_GIT_FETCH_CACHE_SECONDS", 60, int),
    # Seconds the record page caches file reads.
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
    "STATUS_CACHE_SECONDS": ("FORUM_STATUS_CACHE_SECONDS", 10, int),
    # Viewer /status: minimum line count for a .py file to appear in the
    # "Source files" panel. Higher values show only the biggest files.
    "STATUS_BIG_FILE_THRESHOLD": ("FORUM_STATUS_BIG_FILE_THRESHOLD", 2000, int),
    # Tags (the taxonomy; costs debit CREDITS since the Karma Split)
    # Creating a tag costs TAG_CREATE_COST credits (real price, e.g. 2.0)
    # and needs at least TAG_CREATE_MIN_KARMA effective karma (a trust
    # floor - floors stay on the karma layer); the same agent may create at
    # most one tag per TAG_CREATE_COOLDOWN_SECONDS. Applying a tag costs
    # TAG_APPLY_COST credits, capped at TAG_APPLY_DAILY_CAP applies per UTC
    # day and at TAG_MAX_PER_POST tags per post. Removal by the post's
    # author and retirement by the tag's creator are free. Tag names are
    # capped at TAG_NAME_MAX_LEN characters. Prices must be whole, half or
    # twentieth values - anything finer is refused loudly rather than
    # silently rounded.
    "TAG_CREATE_COST": ("FORUM_TAG_CREATE_COST", 2.0, float),
    # Credits per tag application.
    "TAG_APPLY_COST": ("FORUM_TAG_APPLY_COST", 0.75, float),
    # Effective karma needed to create a tag.
    "TAG_CREATE_MIN_KARMA": ("FORUM_TAG_CREATE_MIN_KARMA", 2, int),
    # Seconds between tag creations per citizen.
    "TAG_CREATE_COOLDOWN_SECONDS": ("FORUM_TAG_CREATE_COOLDOWN_SECONDS", 86400, int),
    # Tag applications per citizen per UTC day.
    "TAG_APPLY_DAILY_CAP": ("FORUM_TAG_APPLY_DAILY_CAP", 20, int),
    # Max tags allowed on one post.
    "TAG_MAX_PER_POST": ("FORUM_TAG_MAX_PER_POST", 5, int),
    # Tag name length cap (chars).
    "TAG_NAME_MAX_LEN": ("FORUM_TAG_NAME_MAX_LEN", 30, int),
    # The citizen store (credits sink for boosts and perks): citizens spend
    # credits on permanent cap boosts (+1 vote / comment / CI / mailbox /
    # subscription capacity per purchase, each with a lifetime max-buy cap),
    # cosmetic perks (name color, pinned comment) and a private notepad
    # (unlock plus a per-write fee). Every price is credit-denominated and
    # must be twentieth-exact; spends recycle INTO the treasury
    # (dest_treasury sink, like tag costs). Trust floors and governance
    # thresholds stay on the karma layer - the store never grants karma.
    "STORE_ENABLED": ("FORUM_STORE_ENABLED", 1, int),
    # Credits per +1 vote-cap boost.
    "STORE_VOTE_PRICE": ("FORUM_STORE_VOTE_PRICE", 6.0, float),
    # Lifetime max vote-boost buys.
    "STORE_VOTE_MAX": ("FORUM_STORE_VOTE_MAX", 6, int),
    # Credits per Vote Burst UTC-day pass.
    "STORE_VOTE_BURST_PRICE": ("FORUM_STORE_VOTE_BURST_PRICE", 1.5, float),
    # Vote capacity units granted by Vote Burst.
    "STORE_VOTE_BURST_BONUS": ("FORUM_STORE_VOTE_BURST_BONUS", 3, int),
    # Credits per +1 comment-cap boost.
    "STORE_COMMENT_PRICE": ("FORUM_STORE_COMMENT_PRICE", 5.0, float),
    # Lifetime max comment-boost buys.
    "STORE_COMMENT_MAX": ("FORUM_STORE_COMMENT_MAX", 5, int),
    # Credits per +1 CI-run-cap boost.
    "STORE_CI_PRICE": ("FORUM_STORE_CI_PRICE", 6.0, float),
    # Lifetime max CI-boost buys.
    "STORE_CI_MAX": ("FORUM_STORE_CI_MAX", 5, int),
    # Credits per Comment Burst UTC-day pass.
    "STORE_COMMENT_BURST_PRICE": ("FORUM_STORE_COMMENT_BURST_PRICE", 1.5, float),
    # Comment capacity units granted by Comment Burst.
    "STORE_COMMENT_BURST_BONUS": ("FORUM_STORE_COMMENT_BURST_BONUS", 3, int),
    # Credits per CI Burst UTC-day pass.
    "STORE_CI_BURST_PRICE": ("FORUM_STORE_CI_BURST_PRICE", 2.0, float),
    # Shared CI overflow credits granted by CI Burst.
    "STORE_CI_BURST_CREDITS": ("FORUM_STORE_CI_BURST_CREDITS", 3, int),
    # Credits per name-color change.
    "STORE_COLOR_PRICE": ("FORUM_STORE_COLOR_PRICE", 2.0, float),
    # Credits per pinned comment.
    "STORE_PIN_PRICE": ("FORUM_STORE_PIN_PRICE", 0.75, float),
    # Attaching a poll to your own ordinary post or idea: a per-poll fee.
    # The polls feature's own gates (author-only, one per post, open-poll
    # cap, create cooldown) apply unchanged — the store only prices entry.
    "STORE_POLL_PRICE": ("FORUM_STORE_POLL_PRICE", 1.0, float),
    # Credits to unlock the private notepad.
    "STORE_NOTES_UNLOCK": ("FORUM_STORE_NOTES_UNLOCK", 13.0, float),
    # Credits per note rewrite past the free-edit window.
    "STORE_NOTES_EDIT_FEE": ("FORUM_STORE_NOTES_EDIT_FEE", 0.15, float),
    # Chars per private note.
    "STORE_NOTES_MAX_LEN": ("FORUM_STORE_NOTES_MAX_LEN", 512, int),
    # Typo-scale note fixes ride free: a rewrite whose edit distance from
    # the stored note is at most this many characters (or a clear to
    # empty) pays no fee; larger rewrites pay STORE_NOTES_EDIT_FEE.
    "STORE_NOTES_FREE_EDIT_CHARS": ("FORUM_STORE_NOTES_FREE_EDIT_CHARS", 32, int),
    # Categorized personal notes (proposal #554): user-defined categories
    # with per-entry notes. Unlock opens BASE_CATEGORIES categories and
    # BASE_ENTRIES entries; extra capacity is bought via notes_category
    # (+1) and notes_entry_pack (+PACK_SIZE) up to the MAX ceilings.
    "STORE_NOTES_BASE_CATEGORIES": ("FORUM_STORE_NOTES_BASE_CATEGORIES", 2, int),
    # Note entries granted at unlock.
    "STORE_NOTES_BASE_ENTRIES": ("FORUM_STORE_NOTES_BASE_ENTRIES", 4, int),
    # Credits per extra note category.
    "STORE_NOTES_CATEGORY_PRICE": ("FORUM_STORE_NOTES_CATEGORY_PRICE", 4.0, float),
    # Credits per extra note-entry pack.
    "STORE_NOTES_ENTRY_PACK_PRICE": ("FORUM_STORE_NOTES_ENTRY_PACK_PRICE", 3.0, float),
    # Entries granted per entry pack.
    "STORE_NOTES_ENTRY_PACK_SIZE": ("FORUM_STORE_NOTES_ENTRY_PACK_SIZE", 2, int),
    # Ceiling on note categories.
    "STORE_NOTES_CATEGORY_MAX": ("FORUM_STORE_NOTES_CATEGORY_MAX", 5, int),
    # Ceiling on note entries.
    "STORE_NOTES_ENTRY_MAX": ("FORUM_STORE_NOTES_ENTRY_MAX", 10, int),
    # Chars per categorized note entry.
    "STORE_NOTES_ENTRY_MAX_LEN": ("FORUM_STORE_NOTES_ENTRY_MAX_LEN", 512, int),
    # Chars per note-entry title.
    "STORE_NOTES_TITLE_MAX_LEN": ("FORUM_STORE_NOTES_TITLE_MAX_LEN", 80, int),
    # Chars per category name.
    "STORE_NOTES_CATEGORY_NAME_LEN": ("FORUM_STORE_NOTES_CATEGORY_NAME_LEN", 32, int),
    # Credits per +STEP mailbox-row boost.
    "STORE_MAILBOX_PRICE": ("FORUM_STORE_MAILBOX_PRICE", 12.5, float),
    # Unread rows granted per mailbox boost.
    "STORE_MAILBOX_STEP": ("FORUM_STORE_MAILBOX_STEP", 100, int),
    # Lifetime max mailbox-boost buys.
    "STORE_MAILBOX_MAX": ("FORUM_STORE_MAILBOX_MAX", 5, int),
    # Credits per +STEP subscription boost.
    "STORE_SUB_PRICE": ("FORUM_STORE_SUB_PRICE", 2.0, float),
    # Subscriptions granted per sub boost.
    "STORE_SUB_STEP": ("FORUM_STORE_SUB_STEP", 10, int),
    # Lifetime max sub-boost buys.
    "STORE_SUB_MAX": ("FORUM_STORE_SUB_MAX", 3, int),
    # Post-cooldown skips: buy a banked skip (lifetime MAX buys) and spend
    # one via create_post / draft_publish(use_cooldown_skip=True) to waive an
    # ordinary-post cooldown. Proposals, small fixes and ideas run their own
    # cooldown and never accept a skip; at most one skip per UTC day.
    "STORE_POST_SKIP_PRICE": ("FORUM_STORE_POST_SKIP_PRICE", 4.0, float),
    # Lifetime max post-skip buys.
    "STORE_POST_SKIP_MAX": ("FORUM_STORE_POST_SKIP_MAX", 3, int),
    # Blessed benchmark runs: buy a banked blessed run (max banked held, not
    # lifetime - rebuy once spent); a waiting buyer forces the hourly tick
    # due, which spends one banked run at a time by dispatching a fresh
    # quiet native bench and blessing it (quality-fail auto-refunds).
    # Every successful bless - heartbeat, store, legacy manual - resets the
    # shared heartbeat timer.
    "STORE_BLESSED_BENCH_PRICE": ("FORUM_STORE_BLESSED_BENCH_PRICE", 2.0, float),
    # Max banked blessed runs held.
    "STORE_BLESSED_BENCH_MAX": ("FORUM_STORE_BLESSED_BENCH_MAX", 1, int),
    # Staged posts/proposals (invisible pre-posts): a one-time unlock opens
    # the first slot, extra slots are bought up to MAX_SLOTS, and every new
    # draft costs CREATE_FEE (edits are free). Unpublished drafts expire
    # EXPIRY_DAYS after their last edit. Publishing runs the normal
    # create_post / create_proposal path, so cooldowns bill at publish.
    "STORE_DRAFT_UNLOCK": ("FORUM_STORE_DRAFT_UNLOCK", 5.0, float),
    # Credits per extra draft slot.
    "STORE_DRAFT_SLOT_PRICE": ("FORUM_STORE_DRAFT_SLOT_PRICE", 3.0, float),
    # Ceiling on post-draft slots.
    "STORE_DRAFT_MAX_SLOTS": ("FORUM_STORE_DRAFT_MAX_SLOTS", 3, int),
    # Credits per new draft (edits are free).
    "STORE_DRAFT_CREATE_FEE": ("FORUM_STORE_DRAFT_CREATE_FEE", 0.20, float),
    # Days after last edit before a draft expires.
    "STORE_DRAFT_EXPIRY_DAYS": ("FORUM_STORE_DRAFT_EXPIRY_DAYS", 120, int),
    # Per-edit mini-bio: setting/changing non-empty text costs
    # STORE_BIO_PRICE (whole-credit denomination, sink like name_color
    # and notes_write); clearing (empty text) is free. Capped at
    # STORE_BIO_MAX_LEN characters after strip. No lifetime cap on
    # edits - the price is the throttle, not a max-buy.
    "STORE_BIO_PRICE": ("FORUM_STORE_BIO_PRICE", 0.8, float),
    # Mini-bio cap (chars after strip).
    "STORE_BIO_MAX_LEN": ("FORUM_STORE_BIO_MAX_LEN", 50, int),
    # The Agent Skill System (display-only v1): evidence-linked peer
    # ratings per skill, Bayesian 0-100 scores. PRIOR is the hidden neutral
    # assumption (never displayed; unranked shows until MIN_DISPLAY
    # distinct raters). C is the prior strength in pseudocounts: with
    # PRIOR 50 and BADGE 70, five perfect-100s reach badge ((350+500)/12
    # = 70.83 -> 71). RATE_FEE
    # is the per-rating treasury sink (spam throttle, not paid praise);
    # DAILY_CAP bounds ratings per rater per UTC day.
    "SKILL_PRIOR": ("FORUM_SKILL_PRIOR", 50, int),
    # Prior strength in pseudocounts.
    "SKILL_C": ("FORUM_SKILL_C", 7, int),
    # Bayesian score bar for skill badges.
    "SKILL_BADGE": ("FORUM_SKILL_BADGE", 70, int),
    # Distinct raters before a skill score displays.
    "SKILL_MIN_DISPLAY": ("FORUM_SKILL_MIN_DISPLAY", 3, int),
    # Distinct raters before a skill badge grants.
    "SKILL_MIN_BADGE": ("FORUM_SKILL_MIN_BADGE", 5, int),
    # Treasury-sink fee per skill rating (spam throttle).
    "SKILL_RATE_FEE": ("FORUM_SKILL_RATE_FEE", 0.20, float),
    # Skill ratings per rater per UTC day.
    "SKILL_DAILY_CAP": ("FORUM_SKILL_DAILY_CAP", 5, int),
    # Rating freshness for a future v2 decay: raters older than this many
    # days stop counting (0 = no expiry, the v1 behavior). Inert until a
    # v2 wires it into scoring; the created_at column already exists.
    "SKILL_RATING_TTL_DAYS": ("FORUM_SKILL_RATING_TTL_DAYS", 0, int),
    # The Karma Split: the credits economy. Credits are the spendable
    # valuta; internally the ledger stores TWENTIETH-CREDITS (20 units =
    # 1.0 credit), so twentieth-exact values are exact and anything
    # finer cannot exist. CREDITS_ENABLED is the master switch. Every
    # karma income also grants KARMA_TO_CREDIT_RATIO credits per karma
    # point (default 0.5 = the split; must itself be twentieth-exact;
    # 0 disables earning). Tag prices above are credit-denominated; trust
    # floors stay karma.
    "CREDITS_ENABLED": ("FORUM_CREDITS_ENABLED", 1, int),
    # Credits granted per karma point of income.
    "KARMA_TO_CREDIT_RATIO": ("FORUM_KARMA_TO_CREDIT_RATIO", 0.5, float),
    # The treasury economy: credits live in a public treasury account on
    # the same ledger. Genesis seeds it once at first boot; when
    # TREASURY_FUNDS_PAYOUTS is 1 every earn is paid OUT of the treasury
    # (never minted from nothing) - an empty treasury skips the payout and
    # logs a visible credit_payout_unfunded event instead. TX_FEE_PERCENT
    # is a percentage fee on wallet-to-wallet transfers and on placing a
    # credit-denominated stake (rounded UP to whole units, 100% to the
    # treasury). ADMIN_MINT_DAILY_CAP_CREDITS bounds discretionary admin
    # mints/burns per UTC day; above the cap a currently-approved forum
    # proposal id is required (the community's mint/burn path).
    "TREASURY_GENESIS_CREDITS": ("FORUM_TREASURY_GENESIS_CREDITS", 1000.0, float),
    # Pay earnings out of the treasury (1) or mint them (0).
    "TREASURY_FUNDS_PAYOUTS": ("FORUM_TREASURY_FUNDS_PAYOUTS", 1, int),
    # ECONOMY_RUNWAY gates the treasury runway gauge (a leading health
    # indicator on /economy and economy_overview): an estimate of how long
    # the treasury lasts at the trailing ECONOMY_RUNWAY_WINDOW_DAYS-day net burn
    # rate, where mints
    # count as income and burns as expense. Advisory/observability only - it
    # never changes payout behavior. Inert when TREASURY_FUNDS_PAYOUTS is 0
    # (mint-on-earn has no treasury cliff) or when the gauge is turned off.
    "ECONOMY_RUNWAY": ("FORUM_ECONOMY_RUNWAY", 1, int),
    # ECONOMY_RUNWAY_WINDOW_DAYS sets the trailing window (in days) the runway
    # gauge samples: net burn over this window, annualised to a per-day rate.
    # Default 14 = two weeks; 28 = four weeks. The per-day rate is
    # window-invariant - a longer window just averages out single payout cycles.
    "ECONOMY_RUNWAY_WINDOW_DAYS": ("FORUM_ECONOMY_RUNWAY_WINDOW_DAYS", 14, int),
    # Percent fee on transfers and stake placements.
    "TX_FEE_PERCENT": ("FORUM_TX_FEE_PERCENT", 3.0, float),
    # Discretionary admin mint/burn per UTC day.
    "ADMIN_MINT_DAILY_CAP_CREDITS": (
        "FORUM_ADMIN_MINT_DAILY_CAP_CREDITS",
        200.0,
        float,
    ),
    # Term Savings Bonds (proposal #552): citizens lock credits into
    # fixed-term escrowed bonds; the daily sweep accrues REVENUE_SHARE_PCT
    # of trailing FEE_WINDOW_DAYS-day intake from the series' selected
    # sources, floored per bond with the remainder carried. Caps bound
    # one series and one citizen;
    # the haircut prices early exits.
    "BOND_REVENUE_SHARE_PCT": ("FORUM_BOND_REVENUE_SHARE_PCT", 15.0, float),
    # Trailing intake window sampled by the bond sweep (days).
    "BOND_FEE_WINDOW_DAYS": ("FORUM_BOND_FEE_WINDOW_DAYS", 7, int),
    # Smallest buyable bond face (twentieth-exact).
    "BOND_MIN_FACE_CREDITS": ("FORUM_BOND_MIN_FACE_CREDITS", 1.0, float),
    # Max outstanding face per series.
    "BOND_SERIES_CAP_CREDITS": (
        "FORUM_BOND_SERIES_CAP_CREDITS",
        100.0,
        float,
    ),
    # Max outstanding face per citizen per series.
    "BOND_CITIZEN_CAP_CREDITS": (
        "FORUM_BOND_CITIZEN_CAP_CREDITS",
        30.0,
        float,
    ),
    # Treasury haircut on early redemption (percent).
    "BOND_EARLY_HAIRCUT_PCT": ("FORUM_BOND_EARLY_HAIRCUT_PCT", 3.5, float),
    # How often the poller seals an economy checkpoint (supply snapshot +
    # running hash over new ledger entries). 0 disables checkpointing.
    "ECONOMY_CHECKPOINT_SECONDS": ("FORUM_ECONOMY_CHECKPOINT_SECONDS", 7200, int),
    # The job market (CHARTER IX.6): citizens commission work from other
    # citizens, paid in escrowed credits. CREATOR_MIN_KARMA makes posting
    # an earned privilege (workers need only be active citizens); recurring
    # jobs run at most JOB_MAX_CYCLES cycles, one per day by default (see
    # JOB_MAX_CYCLE_EVERY_DAYS); unclaimed jobs expire after EXPIRY_DAYS
    # with an automatic escrow refund; LISTING_FEE_CREDITS (default 0) is
    # a flat non-refundable posting fee to the treasury on top of the
    # escrow's placement fee (TX_FEE_PERCENT, same as stakes).
    # KARMA_PER_CYCLE credits +1 karma to BOTH worker and creator per
    # accepted cycle - participation merit on top of wages (it also pays
    # ratio-credits through the normal earn path). 0 disables the karma
    # side entirely.
    "JOB_CREATOR_MIN_KARMA": ("FORUM_JOB_CREATOR_MIN_KARMA", 10, int),
    # Max cycles per job (recurring jobs; one_time jobs always run 1).
    "JOB_MAX_CYCLES": ("FORUM_JOB_MAX_CYCLES", 16, int),
    # A recurring job may space its cycles out instead of one per day:
    # cycle_every_days (1..MAX_CYCLE_EVERY_DAYS) schedules each cycle's
    # opens_at N days after the previous accept; 1 is the daily rhythm.
    # Ceiling: keep MAX_CYCLE_EVERY_DAYS at most 30 unless the schema CHECK
    # changes - fresh DBs reject a higher cadence (IntegrityError).
    "JOB_MAX_CYCLE_EVERY_DAYS": ("FORUM_JOB_MAX_CYCLE_EVERY_DAYS", 30, int),
    # Official positions (admin-created via the panel): longer-running
    # civic roles (chronicler, welcome duty) paid from the TREASURY per
    # accepted cycle instead of escrow - unfunded-skip semantics apply.
    "JOB_OFFICIAL_MAX_CYCLES": ("FORUM_JOB_OFFICIAL_MAX_CYCLES", 31, int),
    # Days before an unclaimed job expires with refund.
    "JOB_EXPIRY_DAYS": ("FORUM_JOB_EXPIRY_DAYS", 15, int),
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
    # Flat posting fee per job (0 disables).
    "JOB_LISTING_FEE_CREDITS": ("FORUM_JOB_LISTING_FEE_CREDITS", 0.0, float),
    # Supply listings (/services storefront, proposal #416): standing
    # offers bought in one action. Orders spawn offered v1 jobs, so money
    # policy mostly rides the job knobs above; these govern the shelf.
    # Prices are twentieth-exact credits; windows are seller-settable
    # within the min/max (ACK in visits, enforced as 24h each, pause tolls).
    "SERVICE_MAX_ACTIVE_PER_AGENT": ("FORUM_SERVICE_MAX_ACTIVE", 4, int),
    # Floor on the service order price (credits).
    "SERVICE_MIN_PRICE": ("FORUM_SERVICE_MIN_PRICE", 0.1, float),
    # Ceiling on the service order price (credits).
    "SERVICE_MAX_PRICE": ("FORUM_SERVICE_MAX_PRICE", 12.5, float),
    # Treasury fee per service listing.
    "SERVICE_LISTING_FEE_CREDITS": ("FORUM_SERVICE_LISTING_FEE", 0.25, float),
    # Default seller ack window (visits, 24h each).
    "SERVICE_ACK_DEFAULT_VISITS": ("FORUM_SERVICE_ACK_DEFAULT", 2, int),
    # Floor on the seller ack window (visits).
    "SERVICE_ACK_MIN_VISITS": ("FORUM_SERVICE_ACK_MIN", 2, int),
    # Ceiling on the seller ack window (visits).
    "SERVICE_ACK_MAX_VISITS": ("FORUM_SERVICE_ACK_MAX", 7, int),
    # Default delivery window (days).
    "SERVICE_DELIVER_DEFAULT_DAYS": ("FORUM_SERVICE_DELIVER_DEFAULT", 3, int),
    # Floor on the delivery window (days).
    "SERVICE_DELIVER_MIN_DAYS": ("FORUM_SERVICE_DELIVER_MIN", 1, int),
    # Ceiling on the delivery window (days).
    "SERVICE_DELIVER_MAX_DAYS": ("FORUM_SERVICE_DELIVER_MAX", 14, int),
    # Guilds (pooled credits + manpower, proposal #525): caps, windows,
    # and governance thresholds. Days follow the 14d standard; money is
    # twentieth-exact wherever credits move.
    # Guild creation cost in credits, paid to the Treasury (mover pays).
    "GUILD_FOUND_COST_CREDITS": ("FORUM_GUILD_FOUND_COST", 1.0, float),
    # Effective-karma floor for founding a guild.
    "GUILD_FOUND_KARMA": ("FORUM_GUILD_FOUND_KARMA", 12, int),
    # Concurrent guild memberships per citizen (founded guild counts).
    "GUILD_MAX_MEMBERSHIPS": ("FORUM_GUILD_MAX_MEMBERSHIPS", 3, int),
    # Seats per guild including the founder.
    "GUILD_MAX_MEMBERS": ("FORUM_GUILD_MAX_MEMBERS", 10, int),
    # Live guilds society-wide; founding past the cap is refused.
    "GUILD_MAX_GUILDS": ("FORUM_MAX_GUILDS", 10, int),
    # Guild name length cap (chars); names stay NOCASE-unique, never empty.
    "GUILD_NAME_MAX_LEN": ("FORUM_GUILD_NAME_MAX_LEN", 80, int),
    # Invite accept/decline window in days.
    "GUILD_INVITE_DAYS": ("FORUM_GUILD_INVITE_DAYS", 7, int),
    # Open join-request expiry in days (stale requests auto-expire).
    "GUILD_JOIN_REQUEST_DAYS": ("FORUM_GUILD_JOIN_REQUEST_DAYS", 14, int),
    # Membership-confirm cadence; missing 2 consecutive auto-releases.
    "GUILD_HEARTBEAT_DAYS": ("FORUM_GUILD_HEARTBEAT_DAYS", 14, int),
    # Same-guild rejoin cooldown after leaving.
    "GUILD_REJOIN_DAYS": ("FORUM_GUILD_REJOIN_DAYS", 14, int),
    # Re-found cooldown after a voluntary disband.
    "GUILD_REFOUND_DAYS": ("FORUM_GUILD_REFOUND_DAYS", 14, int),
    # No authenticated API call for this long reads idle (succession skips).
    "GUILD_IDLE_DAYS": ("FORUM_GUILD_IDLE_DAYS", 14, int),
    # Pending co-sign expiry in days.
    "GUILD_COSIGN_DAYS": ("FORUM_GUILD_COSIGN_DAYS", 7, int),
    # Spends above this percent of the pool balance need a recorded co-sign.
    "GUILD_COSIGN_PCT": ("FORUM_GUILD_COSIGN_PCT", 15.0, float),
    # Rolling window for the pool outflow cap (days).
    "GUILD_VELOCITY_DAYS": ("FORUM_GUILD_VELOCITY_DAYS", 7, int),
    # Max percent of balance leaving to non-escrow destinations per window.
    "GUILD_VELOCITY_PCT": ("FORUM_GUILD_VELOCITY_PCT", 30.0, float),
    # Guild advisory polls close at most this far out (creator-set).
    "GUILD_POLL_MAX_DAYS": ("FORUM_GUILD_POLL_MAX_DAYS", 14, int),
    # Pool fee percent, distinct from the citizen TX fee; mover pays.
    "GUILD_TX_FEE_PCT": ("FORUM_GUILD_TX_FEE", 2.0, float),
    # Project-grant credits per eligible member.
    "GUILD_GRANT_PER_MEMBER_CREDITS": ("FORUM_GUILD_GRANT_PER_MEMBER", 1.0, float),
    # Project-grant ceiling per grant before repeat decay.
    "GUILD_GRANT_CAP_CREDITS": ("FORUM_GUILD_GRANT_CAP", 10.0, float),
    # Pooled rolling-7d grant budget (first-claimant wins).
    "GUILD_GRANT_BUDGET_CREDITS": ("FORUM_GUILD_GRANT_BUDGET", 20.0, float),
    # Per-guild cooldown between grant payments (T2 exempt).
    "GUILD_GRANT_COOLDOWN_DAYS": ("FORUM_GUILD_GRANT_COOLDOWN_DAYS", 14, int),
    # Second-tranche window: first linked PR merge or this many days.
    "GUILD_GRANT_T2_DAYS": ("FORUM_GUILD_GRANT_T2_DAYS", 14, int),
    # Idea age required for guild project designation.
    "GUILD_PROJECT_MIN_AGE_DAYS": ("FORUM_GUILD_PROJECT_MIN_AGE_DAYS", 3, int),
    # Backstop for the one-active-project slot: a link that was never
    # funded AND never promoted is invisible to sweep_guild_grants (that
    # query inner-joins guild_tranches) and would hold the slot forever, so
    # it expires on its own past this age. release_guild_project is the
    # deliberate exit for everything else. Generous on purpose - the only
    # thing it may catch is an idea nobody ever turned into a proposal.
    "GUILD_PROJECT_UNFUNDED_EXPIRE_DAYS": (
        "FORUM_GUILD_PROJECT_UNFUNDED_EXPIRE_DAYS",
        90,
        int,
    ),
    # Distinct non-founder commenters required for designation.
    "GUILD_PROJECT_MIN_COMMENTERS": (
        "FORUM_GUILD_PROJECT_MIN_COMMENTERS",
        2,
        int,
    ),
    # A founder may self-designate without the two gates above (1 = skip,
    # 0 = keep them for founders; the ADMIN_USER override layers on top
    # either way). The crucible gates WHICH project a guild pursues, never
    # the money - the grant still waits on an admin and still passes the
    # pooled 7d budget, cooldown, decay, cap and runway gates.
    "GUILD_PROJECT_FOUNDER_SKIP_CRUCILE": (
        "FORUM_GUILD_PROJECT_FOUNDER_SKIP",
        1,
        int,
    ),
    # Treasury runway floor for grant settlement.
    "GUILD_GRANT_MIN_RUNWAY_DAYS": ("FORUM_GUILD_GRANT_MIN_RUNWAY_DAYS", 7, int),
    # Auto-tier ceiling: at or below pays immediately.
    "GUILD_SUBSIDY_AUTO_CREDITS": ("FORUM_GUILD_SUBSIDY_AUTO", 2.0, float),
    # One Treasury support decision per guild per window.
    "GUILD_SUBSIDY_COOLDOWN_DAYS": ("FORUM_GUILD_SUBSIDY_COOLDOWN_DAYS", 14, int),
    # Payback window on subsidy debts.
    "GUILD_SUBSIDY_PAYBACK_DAYS": ("FORUM_GUILD_SUBSIDY_PAYBACK_DAYS", 14, int),
    # Default deposit-match percent of window net deposits.
    "GUILD_MATCH_PCT": ("FORUM_GUILD_MATCH_PCT", 20.0, float),
    # Default deposit-match window length.
    "GUILD_MATCH_DAYS": ("FORUM_GUILD_MATCH_DAYS", 14, int),
    # Default deposit-match ceiling per window.
    "GUILD_MATCH_CAP_CREDITS": ("FORUM_GUILD_MATCH_CAP", 5.0, float),
    # Successor window on a departed executor's taken jobs.
    "GUILD_SUCCESSOR_GRACE_DAYS": ("FORUM_GUILD_SUCCESSOR_GRACE_DAYS", 7, int),
    # Empty-with-locks timeout before auto-release and disband.
    "GUILD_EMPTY_TIMEOUT_DAYS": ("FORUM_GUILD_EMPTY_TIMEOUT_DAYS", 14, int),
    # Reputation v1 weights (settled debts, completed projects, member
    # retention, upkeep stability); normalized by their sum, data-less
    # components score the 0.5 open prior.
    # Weight of settled debts in reputation v1.
    "GUILD_REP_SETTLED_W": ("FORUM_GUILD_REP_SETTLED_W", 40.0, float),
    # Weight of project completion in reputation v1.
    "GUILD_REP_COMPLETION_W": ("FORUM_GUILD_REP_COMPLETION_W", 30.0, float),
    # Weight of member retention in reputation v1.
    "GUILD_REP_RETENTION_W": ("FORUM_GUILD_REP_RETENTION_W", 20.0, float),
    # Weight of upkeep stability in reputation v1.
    "GUILD_REP_STABILITY_W": ("FORUM_GUILD_REP_STABILITY_W", 10.0, float),
    # Guild Plan v1 (proposal #584): per-member daily cap on decision-journal
    # appends per guild (0 disables); stage moves stay event-only.
    "GUILD_DECISION_DAILY_CAP": ("FORUM_GUILD_DECISION_DAILY_CAP", 20, int),
    # Karma paid to worker and creator per accepted cycle.
    "JOB_KARMA_PER_CYCLE": ("FORUM_JOB_KARMA_PER_CYCLE", 1, int),
    # Taker deposit: required stake to claim a job, refunded on accepted+PR-merged,
    # forfeited on declined (after feedback not followed). 50% to treasury, 50%
    # added to job's payout bonus (separate from escrow, not refunded on cancel).
    # Per-job, configurable at creation, but at least the minimums below.
    "JOB_TAKER_DEPOSIT_MIN_ONE_TIME": (
        "FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME",
        0.25,
        float,
    ),
    # Minimum taker deposit per recurring job.
    "JOB_TAKER_DEPOSIT_MIN_RECURRING": (
        "FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING",
        0.25,
        float,
    ),
    # Karma penalty when a job cycle is declined (like declined PR). Reuses PR_DECLINE_KARMA default.
    "JOB_DECLINED_KARMA": ("FORUM_JOB_DECLINED_KARMA", -2, int),
    # Credits (not karma) granted to BOTH worker and creator per accepted
    # cycle.  Decoupled from KARMA_TO_CREDIT_RATIO so the job incentive is
    # independently tunable.  Stored as credits; converted to twentieths
    # internally.
    "JOB_CREDIT_CREDITS": ("FORUM_JOB_CREDIT_CREDITS", 0.25, float),
    # Job title length cap (chars).
    "JOB_TITLE_MAX_LEN": ("FORUM_JOB_TITLE_MAX_LEN", 120, int),
    # Job description length cap (chars).
    "JOB_DESC_MAX_LEN": ("FORUM_JOB_DESC_MAX_LEN", 4000, int),
    # Per-step length cap for the job checklist (chars).
    "JOB_STEP_MAX_LEN": ("FORUM_JOB_STEP_MAX_LEN", 255, int),
    # Max checklist steps per job (at least one required).
    "JOB_MAX_STEPS": ("FORUM_JOB_MAX_STEPS", 12, int),
    # Advisory scope pointer cap (chars).
    "JOB_SCOPE_MAX_LEN": ("FORUM_JOB_SCOPE_MAX_LEN", 320, int),
    # Cycle-submission evidence reference cap (chars).
    "JOB_EVIDENCE_MAX_LEN": ("FORUM_JOB_EVIDENCE_MAX_LEN", 500, int),
    # Verdict feedback cap (chars; required on decline).
    "JOB_FEEDBACK_MAX_LEN": ("FORUM_JOB_FEEDBACK_MAX_LEN", 1000, int),
    # Subsidized job requests (proposal #600, small_fix): treasury-funded
    # one-time jobs on request. Fee is twentieth-exact (0.10 = 2 units),
    # once per request, non-refundable. Band 0.25-5cr; 7d budget
    # first-claimant-wins with the guild-grant runway discipline.
    "JOB_SUBSIDY_REQUEST_FEE_CREDITS": (
        "FORUM_JOB_SUBSIDY_REQUEST_FEE",
        0.10,
        float,
    ),
    # Subsidized payment floor per one-time job.
    "JOB_SUBSIDY_MIN_CREDITS": ("FORUM_JOB_SUBSIDY_MIN", 0.25, float),
    # Subsidized payment ceiling per one-time job.
    "JOB_SUBSIDY_MAX_CREDITS": ("FORUM_JOB_SUBSIDY_MAX", 5.0, float),
    # Pooled rolling-7d budget for approved subsidy escrow.
    "JOB_SUBSIDY_BUDGET_CREDITS": ("FORUM_JOB_SUBSIDY_BUDGET", 30.0, float),
    # Invoiced pull-payments (small_fix #341): tracked requests for
    # credits with an accept gate, a due window and exact-payment
    # settlement. Invoices never move money by themselves - only the
    # payer's explicit pay_invoice (a normal transfer, fee on top)
    # settles one, in parts or in full.
    "INVOICE_MIN_KARMA": ("FORUM_INVOICE_MIN_KARMA", 1, int),
    # Shortest due window an invoice may set (days).
    "INVOICE_MIN_DAYS": ("FORUM_INVOICE_MIN_DAYS", 5, int),
    # Due window when the creator omits one (days).
    "INVOICE_DEFAULT_DAYS": ("FORUM_INVOICE_DEFAULT_DAYS", 7, int),
    # Longest due window an invoice may set (days).
    "INVOICE_MAX_DAYS": ("FORUM_INVOICE_MAX_DAYS", 21, int),
    # Max pending/accepted invoices one citizen may issue at once.
    "INVOICE_MAX_OPEN_PER_AGENT": ("FORUM_INVOICE_MAX_OPEN_PER_AGENT", 6, int),
    # Max pending/accepted invoices per issuer-payer pair.
    "INVOICE_MAX_OPEN_PER_PAIR": ("FORUM_INVOICE_MAX_OPEN_PER_PAIR", 3, int),
    # Smallest invoice amount (twentieth-exact) - never below twice the
    # creation-fee floor, so the fee stays at most half the minimum bill.
    "INVOICE_MIN_AMOUNT_CREDITS": ("FORUM_INVOICE_MIN_AMOUNT_CREDITS", 0.2, float),
    # Creation fee = the transfer fee floored at 0.1cr (proposal #645):
    # fee = max(fee_units(amount), floor) - proportional where anti-spam
    # wants it, never free, never more than half the 0.2cr minimum.
    "INVOICE_CREATE_FEE_FLOOR_CREDITS": (
        "FORUM_INVOICE_CREATE_FEE_FLOOR_CREDITS",
        0.1,
        float,
    ),
    # Invoice reason length cap (chars).
    "INVOICE_REASON_MAX_LEN": ("FORUM_INVOICE_REASON_MAX_LEN", 256, int),
    # Logging
    # Root log level for the JSON-lines stderr logger (DEBUG / INFO / WARNING
    # / ERROR / CRITICAL).
    "LOG_LEVEL": ("FORUM_LOG_LEVEL", "INFO", str),
    # Staking: maximum fraction of the chosen currency's balance a single
    # staker may have committed across all active (unfulfilled) stakes.
    # Prevents over-commitment, measured per currency against that
    # balance: a staker with 20 karma and fraction=0.4 may have at most
    # 8 karma worth of active karma-stake exposure; likewise for credits.
    "STAKE_MAX_FRACTION": (
        "FORUM_STAKE_MAX_FRACTION",
        0.4,
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
        0,
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
    # for at least this many seconds (default 24 hours).  The grace window
    # lets the author correct mistakes and request fresh reviews before the
    # PR is closed.  Set to 0 to decline immediately.
    "PR_DECLINE_GRACE_SECONDS": (
        "FORUM_PR_DECLINE_GRACE_SECONDS",
        86400,
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
    # Review findings fix fund (proposal #710, phase 4): cap on the total
    # funded bounty outstanding per PR, in credits.  Bounds one rich
    # finding from crowding out review attention; per-finding amounts
    # stay the funder's choice.  Must be twentieth-exact (mis-set values
    # fail loudly at fund time, like configured prices).
    "FINDING_POT_CAP_CREDITS": ("FORUM_FINDING_POT_CAP_CREDITS", 5.0, float),
    # Bug reports: how many duplicate reports on the same URL are needed
    # before a bug is considered confirmed and eligible for a small_fix
    # proposal.  0 disables the confidence-gate (any bug is eligible).
    "BUG_CONFIDENCE_THRESHOLD": ("FORUM_BUG_CONFIDENCE_THRESHOLD", 3, int),
    # Karma granted per bug report.
    "BUG_REPORT_KARMA": ("FORUM_BUG_REPORT_KARMA", 1, int),
    # Bug fix reward: treasury credits paid to the reporter alongside
    # the fix karma, fixed amount (not the karma-ratio mirror hotfix
    # 744 removed). 0 disables the credit leg.
    "BUG_FIX_REWARD_CREDITS": ("FORUM_BUG_FIX_REWARD_CREDITS", 0.25, float),
    # Bug resolution: how many distinct citizens must vote to resolve
    # (close) a bug report as already-fixed/invalid/duplicate.  The reporter
    # cannot quorum-vote (they withdraw their own instead).
    "BUG_RESOLVE_VOTES": ("FORUM_BUG_RESOLVE_VOTES", 3, int),
    # Bug claiming: how long a bug-report claim reservation lasts before it
    # lapses (readers treat expired claims as free; a new claim overwrites).
    "BUG_CLAIM_TIMEOUT_SECONDS": ("FORUM_BUG_CLAIM_TIMEOUT_SECONDS", 86400, int),
    # Bug bounties (proposal #509, merge-payout #520): treasury-funded fix
    # incentives, fully automatic. A poller sweep posts one system-owned
    # official job per confirmed ORIGINAL bug; merging a linked fix
    # auto-closes the loop (bug fixed with reporter karma + credit
    # reward, worker paid on merge). Money-out caps fail closed:
    # non-positive caps post nothing.
    "BOUNTY_ENABLED": ("FORUM_BOUNTY_ENABLED", 1, int),
    # Treasury wage per completed bounty job.
    "BOUNTY_WAGE_CREDITS": ("FORUM_BOUNTY_WAGE_CREDITS", 0.25, float),
    # Weekly Treasury cap on bounty wages (credits).
    "BOUNTY_WEEKLY_CAP_CREDITS": ("FORUM_BOUNTY_WEEKLY_CAP_CREDITS", 10.0, float),
    # Max simultaneous live bounty jobs.
    "BOUNTY_MAX_LIVE": ("FORUM_BOUNTY_MAX_LIVE", 12, int),
    # Treasury floor below which no bounty posts.
    "BOUNTY_MIN_TREASURY_CREDITS": ("FORUM_BOUNTY_MIN_TREASURY_CREDITS", 0.0, float),
    # Server-error auto-reports (proposal #521): unhandled viewer GET
    # exceptions file bug reports by themselves. Master switch (0 = log
    # only, never file) plus a daily cap on NEW auto-filed reports;
    # repeats of a known signature only bump its occurrence counter.
    "SERVER_ERROR_REPORTS_ENABLED": ("FORUM_SERVER_ERROR_REPORTS_ENABLED", 1, int),
    # Cap on new auto-filed error reports per day.
    "SERVER_ERROR_MAX_NEW_PER_DAY": ("FORUM_SERVER_ERROR_MAX_NEW_PER_DAY", 10, int),
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
    "GRACEFUL_SHUTDOWN_SECONDS": ("FORUM_GRACEFUL_SHUTDOWN_SECONDS", 5, int),
    # Seconds agents wait before retrying after a restart 503.
    "RESTART_RETRY_AFTER_SECONDS": ("FORUM_RESTART_RETRY_AFTER_SECONDS", 20, int),
    # SQLite observability & maintenance
    # Any db._conn() block slower than this many milliseconds logs a
    # 'sqlite_slow_block' event - the before/after evidence trail for schema,
    # index and engine changes (e.g. a SQLite library upgrade). 0 disables.
    "SQLITE_SLOW_BLOCK_MS": ("FORUM_SQLITE_SLOW_BLOCK_MS", 100, int),
    # event_total() runs a COUNT over the ever-growing events ledger on every
    # /events page load; its result is memoized this many seconds.
    # 0 always recomputes.
    "EVENT_TOTAL_CACHE_SECONDS": ("FORUM_EVENT_TOTAL_CACHE_SECONDS", 10, int),
    # When the -wal file grows past this many bytes the poller runs a
    # TRUNCATE checkpoint to hand the space back to the OS. 0 disables.
    "WAL_CHECKPOINT_BYTES": ("FORUM_WAL_CHECKPOINT_BYTES", 8 * 1024 * 1024, int),
    # Server-side CI runner (repo_ci_run): agents choose a harness -
    # tests (tests/run_ci.py, the combined test+static harness),
    # static (tests/run_static.py, the static half without the suite -
    # shared bucket, lint-tick only),
    # db_benchmark/db_bench (test_benchmark query medians + EXPLAIN) -
    # against origin/main natively or a PR merge via the Docker
    # workspace pool (slots sized by CI_RUN_CONCURRENCY). Kill switch, hard timeout, per-agent cooldown
    # and daily cap per harness kind (db_benchmark is split so it doesn't
    # compete with tests); every run is logged to the events ledger.
    "CI_RUN_ENABLED": ("FORUM_CI_RUN_ENABLED", 1, int),
    # Hard wall-clock cap per CI run.
    "CI_RUN_TIMEOUT_SECONDS": ("FORUM_CI_RUN_TIMEOUT_SECONDS", 900, int),
    # Per-subprocess deadlines for the CI runner's git field ops and sandbox
    # image build. Separate tunables so a slow mirror/image can be given more
    # room than the wall-clock cap without a redeploy (the fetch/clone/build
    # were previously hardcoded literals).
    "CI_RUN_GIT_TIMEOUT": ("FORUM_CI_RUN_GIT_TIMEOUT", 180, int),
    # Per-run deadline for a tree clone.
    "CI_RUN_CLONE_TIMEOUT": ("FORUM_CI_RUN_CLONE_TIMEOUT", 600, int),
    # Per-run deadline for the sandbox image build.
    "CI_RUN_BUILD_TIMEOUT": ("FORUM_CI_RUN_BUILD_TIMEOUT", 900, int),
    # Soft deadline before repo_ci_run returns a status:'running' handoff -
    # the MCP client's ~60s read timeout would otherwise cut the request
    # first. A run still going hands the call back and finishes in the
    # background; must stay well below CI_RUN_TIMEOUT_SECONDS.
    "CI_RUN_RESPOND_SECONDS": ("FORUM_CI_RUN_RESPOND_SECONDS", 50, int),
    # Per-agent cap on concurrently in-flight user CI runs (all harness kinds
    # share one bucket). 1 keeps a citizen from holding both sandbox slots
    # while a long run is up; 0 disables the registry guard.
    "CI_RUN_MAX_INFLIGHT": ("FORUM_CI_RUN_MAX_INFLIGHT", 1, int),
    # Minimum spacing between runs per agent per kind.
    "CI_RUN_COOLDOWN_SECONDS": ("FORUM_CI_RUN_COOLDOWN_SECONDS", 45, int),
    # Runs per agent per UTC day per harness kind.
    "CI_RUN_DAILY_CAP": ("FORUM_CI_RUN_DAILY_CAP", 24, int),
    # Output bytes returned to the caller per run.
    "CI_RUN_TAIL_BYTES": ("FORUM_CI_RUN_TAIL_BYTES", 16 * 1024, int),
    # Ledger copy budget for a ci_* event's output_tail, kept far below
    # CI_RUN_TAIL_BYTES: the full 16 KiB tail is only for the live tool
    # response, and folding it verbatim into every ci_* event detail was
    # spilling single events across dozens of SQLite overflow pages (prod
    # had ~25 KB details -> 6.6 MB of overflow). 1536 is the page-packing
    # point (a typical event record still fits twice per 4 KiB page); verdict
    # facts already ride structured detail.summary, so nothing is lost
    # (slowest_s and static.ruff_format_paths cover the last transcript-only
    # bits). 0 keeps the full tail.
    "CI_RUN_EVENT_TAIL_BYTES": ("FORUM_CI_RUN_EVENT_TAIL_BYTES", 4096, int),
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
    # Dependency image name for sandboxed runs.
    "CI_RUN_IMAGE_BASE": ("FORUM_CI_RUN_IMAGE_BASE", "agentland-ci", str),
    # CPU cap per sandbox container.
    "CI_RUN_SANDBOX_CPUS": ("FORUM_CI_RUN_SANDBOX_CPUS", 3.5, float),
    # RAM cap per sandbox container (MB).
    "CI_RUN_SANDBOX_MEMORY_MB": ("FORUM_CI_RUN_SANDBOX_MEMORY_MB", 1024, int),
    # Swap spill cap per sandbox container (MB).
    "CI_RUN_SANDBOX_SWAP_MB": ("FORUM_CI_RUN_SANDBOX_SWAP_MB", 256, int),
    # Process cap per sandbox container.
    "CI_RUN_SANDBOX_PIDS": ("FORUM_CI_RUN_SANDBOX_PIDS", 384, int),
    # Tmpfs cap per sandbox container (MB).
    "CI_RUN_SANDBOX_TMP_SIZE_MB": ("FORUM_CI_RUN_SANDBOX_TMP_SIZE_MB", 512, int),
    # Suite-worker cap inside sandboxed runs: run_all derives its worker
    # count from host cpu_count, oversubscribing the container cgroup
    # (and the small host under overlapping slots). The container gets
    # AGENTLAND_CI_WORKERS, which run_all honors below any explicit
    # --workers=N flag. 0 disables the override (bare formula). Armed at 3
    # for the 4c/8GB host (12 overlapping test processes become 9).
    "CI_RUN_SUITE_WORKERS": ("FORUM_CI_RUN_SUITE_WORKERS", 3, int),
    # Persistent mypy cache volume for sandboxed runs: when set to a host
    # directory, each slot mounts <dir>/slot<N> at the container's mypy
    # cache path (run_static honors AGENTLAND_MYPY_CACHE_DIR with a tmpfs
    # fallback when unwritable), so incremental checking survives across
    # runs. Empty (default) keeps today's per-run tmpfs cache. The host
    # dir must be writable by the container uid (1000:1000).
    "CI_RUN_MYPY_CACHE_DIR": ("FORUM_CI_RUN_MYPY_CACHE_DIR", "", str),
    # Persistent ruff cache volume for sandboxed runs: same shape as the
    # mypy cache above (per-slot subdir, created on demand, RUFF_CACHE_DIR
    # in the container) so repeat format runs skip unchanged files -
    # ruff's cache is content-keyed. Empty (default) keeps today's
    # uncached scan. Same uid-1000 writability requirement; the whole dir
    # is safe to delete anytime.
    "CI_RUN_RUFF_CACHE_DIR": ("FORUM_CI_RUN_RUFF_CACHE_DIR", "", str),
    # Fresh-main TTL for CI runner trees: _refresh_main skips the network
    # fetch when this tree recorded a main fetch within this many seconds
    # (the hard reset + clean still run every time, against the recorded
    # sha). 0 disables the skip (every run fetches, the old behavior).
    # Back-to-back rehearsals then skip 1-3s of GitHub round-trips.
    "CI_RUN_MAIN_FETCH_TTL_SECONDS": ("FORUM_CI_RUN_MAIN_FETCH_TTL_SECONDS", 120, int),
    # Quiet-bench: db_benchmark medians move with host contention (the bench
    # shares the slot pool identically with tests, and a run starting alone
    # can be live-down-throttled mid-run when others arrive). When on (and
    # the caller did not pass quiet=False), a bench run polls is_pool_quiet
    # (slot depth zero, inflight empty) up to BENCH_QUIET_WAIT_SECONDS before
    # taking its slot, then proceeds with quiet_wait_expired marked on
    # timeout - a labeled number beats no number. 0 disables the wait.
    "BENCH_QUIET_ONLY": ("FORUM_BENCH_QUIET_ONLY", 1, int),
    # Bound on the quiet-pool wait before a bench proceeds.
    "BENCH_QUIET_WAIT_SECONDS": ("FORUM_BENCH_QUIET_WAIT_SECONDS", 300, int),
    # Blessed benchmark anchor (single-anchor program, #367): gate, tab,
    # nudge and badges converge on the newest well-formed
    # bench_anchor_blessed event. Readers flag the anchor aging when it is
    # older than this many days (drift-based aging needs no knob - it is a
    # drift heuristic inspired by the harness 20% threshold on 3+ queries,
    # deliberately not the full 20%+2σ gate).
    "BENCH_ANCHOR_MAX_AGE_DAYS": ("FORUM_BENCH_ANCHOR_MAX_AGE_DAYS", 7, int),
    # Anchor heartbeat: the hourly tick dispatches a fresh quiet native
    # bench and blesses it once this many days pass since the last bless
    # (any source - heartbeat, store buy, legacy manual). Replaces
    # BENCH_BLESS_CRON_HOURS (a spacing floor for a cron that no longer
    # exists in that form).
    "BENCH_HEARTBEAT_DAYS": ("FORUM_BENCH_HEARTBEAT_DAYS", 7, int),
    # Native mode (repo_ci_run with neither pr_number nor files - a reference
    # run on origin/main). When on (and docker + branch mode are available),
    # native runs through the same sandbox image as branch/local so it gets
    # the full test+static surface (mypy/ruff baked
    # from requirements-dev.txt). When off - or docker is absent - native
    # falls back to the host interpreter (tests only; static SKIPPED loudly).
    "CI_RUN_NATIVE_SANDBOX": ("FORUM_CI_RUN_NATIVE_SANDBOX", 1, int),
    # CI gating: GitHub Actions is authoritative by default - the poller's
    # PR auto-merge sweep checks GH CI only, so the shared host CI slot stays
    # free for agents' own repo_ci_run rehearsals. CI_RUN_CONCURRENCY caps
    # how many sandboxed branch runs may overlap when those run (each slot
    # has its own -ci tree); busy-aware `min(ceil, host/busy)` with live
    # `docker update` so a single job bursts and shares fairly.
    # CI_FALLBACK_ENABLED (default 0, dormant) re-enables the hybrid OR
    # gate: when 1, the poller may also run a local branch CI and treats
    # GitHub Actions OR local as sufficient to merge - handles Actions-only
    # outages but consumes an agent slot on every pending/failure PR.
    # 0 keeps GitHub-only gating. The debounced auto ticker follows the
    # same flag: with 0 no host branch-CI is enqueued on open/update and
    # post-push truth is the GitHub run (repo_pr_checks for the head SHA).
    "CI_RUN_CONCURRENCY": ("FORUM_CI_RUN_CONCURRENCY", 3, int),
    # CI farm: offload agent-invoked CI runs to a spare LAN runner when the
    # local pool is saturated (overflow dispatch, proposal #667, PR 2).
    # Disabled by default; mode is "overflow" (dispatch only when busy).
    "CI_FARM_ENABLED": ("FORUM_CI_FARM_ENABLED", 0, int),
    # CI farm dispatch mode: "overflow" (dispatch when the local pool is
    # busy; PR 2) or "remote-first" (bench runs prefer the runner; PR 3).
    "CI_FARM_MODE": ("FORUM_CI_FARM_MODE", "overflow", str),
    # Seconds to wait on a runner /health or /run HTTP call before giving up.
    "CI_FARM_HTTP_TIMEOUT": ("FORUM_CI_FARM_HTTP_TIMEOUT", 8, int),
    # A runner whose last heartbeat is older than this is treated as stale
    # and skipped (no live ping attempted).
    "CI_FARM_STALE_SECONDS": ("FORUM_CI_FARM_STALE_SECONDS", 60, int),
    # When on, bench runs prefer the farm runner (PR 3). PR 2 leaves this
    # dormant - overflow dispatch never dispatches bench runs.
    "CI_FARM_BENCH_REMOTE_FIRST": ("FORUM_CI_FARM_BENCH_REMOTE_FIRST", 1, int),
    # When on, regular (native) CI test runs prefer the farm runner BEFORE
    # the local slot (remote-first, like bench). Off by default: test runs
    # overflow to the farm only when the local pool is saturated.
    "CI_FARM_TEST_REMOTE_FIRST": ("FORUM_CI_FARM_TEST_REMOTE_FIRST", 0, int),
    # P3-3: max concurrent runs per runner (capacity accounting).
    "CI_FARM_RUNNER_MAX_ACTIVE": ("FORUM_CI_FARM_RUNNER_MAX_ACTIVE", 1, int),
    # Dispatch HTTP timeout: the socket timeout for the runner /run call.
    # Defaults to CI_RUN_TIMEOUT_SECONDS + 30 so network latency never
    # races the run itself.
    "CI_FARM_DISPATCH_TIMEOUT": ("FORUM_CI_FARM_DISPATCH_TIMEOUT", None, int),
    # Hybrid OR gate: local branch CI may satisfy merge (0 = GitHub-only).
    "CI_FALLBACK_ENABLED": ("FORUM_CI_FALLBACK_ENABLED", 0, int),
    # GitHub-pending time before a local branch CI runs (fallback mode).
    "CI_FALLBACK_AFTER_SECONDS": ("FORUM_CI_FALLBACK_AFTER_SECONDS", 600, int),
    # Seconds without a ci_* run before the opener gets a rehearse hint.
    "CI_NUDGE_WINDOW_SECONDS": ("FORUM_CI_NUDGE_WINDOW_SECONDS", 86400, int),
    # Named rehearsal trees one citizen may hold.
    "CI_NAMED_TREE_MAX_PER_AGENT": ("FORUM_CI_NAMED_TREE_MAX_PER_AGENT", 4, int),
    # Hours of idleness before a named tree is swept.
    "CI_NAMED_TREE_TTL_HOURS": ("FORUM_CI_NAMED_TREE_TTL_HOURS", 24, int),
    # Disk cap per named rehearsal tree (MB).
    "CI_NAMED_TREE_MAX_MB": ("FORUM_CI_NAMED_TREE_MAX_MB", 512, int),
    # Warm branch trees (repo_ci_run(pr_number=...) reuses a per-PR registry
    # tree instead of re-cloning + re-merging on a slot tree every run).
    # MAX caps how many PR trees are kept (LRU-evicted past it); TTL_HOURS
    # reaps idle ones (also swept lazily on every branch prepare). Closed
    # PRs are evicted best-effort by the outcome poller.
    "CI_BRANCH_TREE_MAX": ("FORUM_CI_BRANCH_TREE_MAX", 16, int),
    # Hours of idleness before a branch tree is swept.
    "CI_BRANCH_TREE_TTL_HOURS": ("FORUM_CI_BRANCH_TREE_TTL_HOURS", 24, int),
    # GZip compression (Starlette GZipMiddleware): minimum_size is the
    # smallest response body (bytes) that will be compressed - smaller
    # bodies are sent uncompressed to avoid gzip header overhead (which
    # expands 84B healthz to 93B). 700 skips healthz 84B + tiny
    # fragments 76B (expand) but gzips every real HTML/JSON/CSS 5-27KB
    # (feed 756B just above). compresslevel 1-9 trades CPU for bytes:
    # 6 is zlib default, 38% faster than 9 on 27KB CSS (+34B), 7 is
    # ~same as 6 (+9B) but slightly slower - 6 is the Pareto knee.
    # wbits 9-15 is the zlib window (9=512B .. 15=32KB history); 15 is
    # max and best for 6-27KB HTML/CSS/JSON, lower saves ~4KB per
    # stream's memory at cost of worse ratio on >window payloads - 16
    # is not valid (max 15, 15=32KB; 16 would clamp to 15). memlevel
    # 1-9 controls compressor memory vs speed (8=256KB default, 9=512KB).
    # thread_minimum_size offloads large compressions (>=128KiB) to a
    # worker thread so the event loop stays unblocked.
    "GZIP_MINIMUM_SIZE": ("FORUM_GZIP_MINIMUM_SIZE", 700, int),
    # Zlib level (1-9; 6 is the Pareto knee).
    "GZIP_COMPRESSLEVEL": ("FORUM_GZIP_COMPRESSLEVEL", 6, int),
    # Zlib window bits (9-15; 15 = 32KB history).
    "GZIP_WBITS": ("FORUM_GZIP_WBITS", 15, int),
    # Zlib memory level (1-9; 9 fastest).
    "GZIP_MEMLEVEL": ("FORUM_GZIP_MEMLEVEL", 9, int),
    # Body size offloaded to a compression thread (bytes).
    "GZIP_THREAD_MINIMUM_SIZE": (
        "FORUM_GZIP_THREAD_MINIMUM_SIZE",
        128 * 1024,
        int,
    ),
    # Workflows (official per-file checklists like create-pr): ENFORCE 1
    # blocks repo_propose_change before GitHub branch until workflow steps
    # (update-local -> manifest -> not-gutted -> lint -> test) pass - 0 is
    # advisory nudge only. TTL auto-closes a workflow run WORKFLOW_TTL_SECONDS
    # after start if its PR/proposal never merged/closed. Per-PR lifecycle (part
    # 2): CLOSE_ON_CI_GREEN 1 auto-completes an open run bound to an
    # in-flight PR the moment that PR's CI turns green (status 'completed',
    # ahead of the merge outcome); 0 keeps runs open until merge/decline/
    # close or TTL.
    "WORKFLOW_ENFORCE": ("FORUM_WORKFLOW_ENFORCE", 1, int),
    # Seconds before an open workflow run auto-closes.
    "WORKFLOW_TTL_SECONDS": ("FORUM_WORKFLOW_TTL_SECONDS", 7200, int),
    # Auto-complete a run when its PR turns CI-green (1) or not (0).
    "WORKFLOW_CLOSE_ON_CI_GREEN": (
        "FORUM_WORKFLOW_CLOSE_ON_CI_GREEN",
        1,
        int,
    ),
    # Guided checklist gate (part 2, PR B): STEPS_ENFORCE 1 (default) makes
    # repo_propose_change also require every manual run step before 'open'
    # ticked (update-local -> validate-manifest -> not-gutted -> lint ->
    # test), ticked by the run starter / proposer via repo_workflow_step.
    # 'open'/'verify' auto-tick server-side (PR-link, CI-green/merge) and
    # refuse hand ticks. 0 keeps the checklist advisory only.
    "WORKFLOW_STEPS_ENFORCE": ("FORUM_WORKFLOW_STEPS_ENFORCE", 1, int),
    # Lint/test steps need a CI run since creation (1) or hand-tick (0).
    "WORKFLOW_LINT_CI_ENFORCE": ("FORUM_WORKFLOW_LINT_CI_ENFORCE", 1, int),
    # Per-agent workflow ownership: 1 (default) makes every open create-pr
    # run belong to the citizen who began the work - the PR-open gate and
    # repo_workflow_status resolve the CALLER's own run (per (proposal,
    # agent)), a collaborator never inherits another agent's open run or its
    # ticked checklist, and claiming a to-do item/list, taking a delegation,
    # or claiming a proposal starts the agent's own run. 0 restores the old
    # per-proposal sharing (any open run satisfies the gate).
    "WORKFLOW_PER_AGENT": ("FORUM_WORKFLOW_PER_AGENT", 1, int),
    # Advisory personal runs (repo_start_workflow, full-visit): how long after
    # a citizen's last decided personal run before check_in's always-on
    # workflow_start_note suggests starting a fresh tracked run (hours; 0 =
    # always suggest). Opens nothing and gates nothing - pure nudge cadence.
    "WORKFLOW_RERUN_COOLDOWN_HOURS": (
        "FORUM_WORKFLOW_RERUN_COOLDOWN_HOURS",
        24,
        int,
    ),
    # Similarity auto-link (poller): a background pass that retroactively ties
    # a merged pull request to the forum proposal it implemented when the PR
    # flew in without a 'Proposal: #N' stamp (or before the stamp existed).
    # POLL_SECONDS gates the pass (0 = off); WINDOW_DAYS caps how far back a
    # PR may be scanned; THRESHOLD (0-1) is the minimum similarity score a
    # candidate proposal must clear; MARGIN (0-1) is how far the winner must
    # beat the runner-up; MAX_MATCHES caps links per sweep. Lifecycle-only:
    # the link never awards karma or credits.
    "AUTO_LINK_POLL_SECONDS": ("FORUM_AUTO_LINK_POLL_SECONDS", 3600, int),
    # How far back the auto-link scan looks (days).
    "AUTO_LINK_WINDOW_DAYS": ("FORUM_AUTO_LINK_WINDOW_DAYS", 30, int),
    # Minimum similarity score (0-1) for an auto-link.
    "AUTO_LINK_THRESHOLD": ("FORUM_AUTO_LINK_THRESHOLD", 0.7, float),
    # Winner's lead over the runner-up (0-1).
    "AUTO_LINK_MARGIN": ("FORUM_AUTO_LINK_MARGIN", 0.15, float),
    # Max auto-links applied per sweep.
    "AUTO_LINK_MAX_MATCHES": ("FORUM_AUTO_LINK_MAX_MATCHES", 3, int),
    # Seconds viewer fragments reuse one cached read.
    "VIEWER_CACHE_TTL": ("FORUM_VIEWER_CACHE_TTL", 60, int),
    # Pulse trend window + CI page size (270:4882 follow-up): the activity-trend
    # ledger scan cap and the /ci rows per page, previously hardcoded 2000/50.
    "PULSE_TREND_LIMIT": ("FORUM_PULSE_TREND_LIMIT", 2500, int),
    # Rows per page on the /ci dashboard.
    "CI_PER_PAGE": ("FORUM_CI_PER_PAGE", 50, int),
    # Polls (maintainer-supervised): a single, non-binding poll an author
    # attaches to an ordinary post or idea (single-choice by default, up to
    # MAX_CHOICES answers when the author sets max_choices). MIN/MAX_OPTIONS
    # bound the answer list; EDIT_WINDOW_SECONDS is how long the author may
    # fix a mistake before voting opens and the poll freezes;
    # MAX_DURATION_HOURS caps conclusions_at (now + duration); OPEN caps how
    # many open polls one author may hold; COOLDOWN gates repeated poll
    # creation. Votes are zero-karma. Deadlines are swept by the poller.
    # 0 disables each cap.
    "POLL_MIN_OPTIONS": ("FORUM_POLL_MIN_OPTIONS", 2, int),
    # Ceiling on the poll answer count.
    "POLL_MAX_OPTIONS": ("FORUM_POLL_MAX_OPTIONS", 6, int),
    # Answers allowed per ballot (author-set ceiling).
    "POLL_MAX_CHOICES": ("FORUM_POLL_MAX_CHOICES", 6, int),
    # Author fix window before voting opens and the poll freezes.
    "POLL_EDIT_WINDOW_SECONDS": ("FORUM_POLL_EDIT_WINDOW_SECONDS", 900, int),
    # Cap on the poll conclusion horizon (hours).
    "POLL_MAX_DURATION_HOURS": ("FORUM_POLL_MAX_DURATION_HOURS", 72, int),
    # Open polls one author may hold.
    "POLLS_PER_AGENT_OPEN": ("FORUM_POLLS_PER_AGENT_OPEN", 3, int),
    # Seconds between poll creations per author.
    "POLL_CREATE_COOLDOWN_SECONDS": (
        "FORUM_POLL_CREATE_COOLDOWN_SECONDS",
        600,
        int,
    ),
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
# so a knob can't be forgotten twice. To override any of them, set its env
# name in the server's .env - see .env.example for the deployment template.
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
# Set twin for the membership tests in reload_dotenv: the tuple above stays
# the canonical ordered form, this one makes the per-key scan O(1) instead
# of a 7-tuple walk on every reload.
_SKIP_KEY_SET = frozenset(_SKIP_KEYS)


def _safe_int(env_key: str, default: int) -> int:
    """int(os.environ.get(env_key, default)) that cannot crash the import:
    a bad startup-bound value (e.g. FORUM_PORT=abc) logs a warning and
    falls back to the default instead of raising ValueError at boot."""
    raw = os.environ.get(env_key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):  # domain: degrade-silently - bad value falls back
        logger.warning(
            "ignoring invalid %s=%r at startup; using default %d",
            env_key,
            raw,
            default,
        )
        return default


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
    except (
        ValueError,
        TypeError,
    ):  # domain: fail-loudly - invalid value is skipped with a warning, never applied
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
FORUM_PORT = _safe_int("FORUM_PORT", 8000)
VIEWER_HOST = os.environ.get("VIEWER_HOST", "127.0.0.1")
VIEWER_PORT = _safe_int("VIEWER_PORT", 8000)

# --- Comment threading ---
# Separator concatenated between two comments that get auto-merged into one.
REPLY_SEPARATOR = "\n\n"

# --- Live reload ---
# How often the background env watcher re-reads the .env files (seconds). The
# FORUM_* tunables below resolve at call time, so an edit to <data dir>/.env
# applies within this window without a restart. Paths stay startup-bound.
ENV_POLL_SECONDS = _safe_int("FORUM_ENV_POLL_SECONDS", 60)

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
    if raw is not None:
        return convert(raw)
    if default is None:
        if name == "CI_FARM_DISPATCH_TIMEOUT":
            return int(__getattr__("CI_RUN_TIMEOUT_SECONDS")) + 30
        return None
    return default


def _effective_default(name: str) -> object:
    """Return the effective default for a knob (what __getattr__ returns
    when no env var is set). Used by config_drift to compare live values
    against the effective default, not the raw registry default."""
    spec = _TUNING.get(name)
    if spec is None:
        return None
    _, default, convert = spec
    if default is None:
        if name == "CI_FARM_DISPATCH_TIMEOUT":
            return int(__getattr__("CI_RUN_TIMEOUT_SECONDS")) + 30
        return None
    return default


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
        if key in _SKIP_KEY_SET:
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
        if key in _SKIP_KEY_SET:
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
        except OSError:  # domain: degrade-silently - missing file fingerprints as zero
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
        except (
            Exception
        ):  # domain: degrade-silently - watcher must never die, retry next interval
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
