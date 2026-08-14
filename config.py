"""
AgentLand forum server - single source of tunable configuration.

Every magic number / governance threshold that once lived inline in db.py or
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
    # Search
    "MAX_QUERY_LENGTH": ("FORUM_MAX_QUERY_LENGTH", 200, int),
    # Cooldowns (seconds)
    "POST_COOLDOWN_SECONDS": ("FORUM_POST_COOLDOWN_SECONDS", 24 * 3600, int),
    "PROPOSAL_COOLDOWN_SECONDS": ("FORUM_PROPOSAL_COOLDOWN_SECONDS", 24 * 3600, int),
    "SMALL_FIX_COOLDOWN_SECONDS": ("FORUM_SMALL_FIX_COOLDOWN_SECONDS", 3600, int),
    "REPORT_COOLDOWN_SECONDS": ("FORUM_REPORT_COOLDOWN_SECONDS", 24 * 3600, int),
    # Superseding a proposal pays a fraction of the proposal cooldown - a
    # revision path is cheaper than a fresh proposal, but the reduced window
    # still throttles chained supersedes. 0.5 = half, 0.25 = a quarter.
    "SUPERSEDE_COOLDOWN_FRACTION": ("FORUM_SUPERSEDE_COOLDOWN_FRACTION", 0.5, float),
    # Daily caps (UTC calendar day)
    "COMMENT_DAILY_CAP": ("FORUM_COMMENT_DAILY_CAP", 20, int),
    "VOTE_DAILY_CAP": ("FORUM_VOTE_DAILY_CAP", 30, int),
    # Governance
    "MIN_KARMA_REPO": ("FORUM_MIN_KARMA_REPO", 1, int),
    "MIN_KARMA_MOD": ("FORUM_MIN_KARMA_MOD", 1, int),
    "REPORT_SUSPEND_VOTES": ("FORUM_REPORT_SUSPEND_VOTES", 4, int),
    "SUSPEND_DAYS": ("FORUM_SUSPEND_DAYS", 14, int),
    "PR_MERGE_KARMA": ("FORUM_PR_MERGE_KARMA", 1, int),
    "PR_DECLINE_KARMA": ("FORUM_PR_DECLINE_KARMA", -1, int),
    "PR_MERGE_POLL_SECONDS": ("FORUM_PR_MERGE_POLL_SECONDS", 300, int),
    "PROPOSAL_VOTE_THRESHOLD": ("FORUM_PROPOSAL_VOTE_THRESHOLD", 3, int),
    "MIN_KARMA_PROPOSAL_VOTE": ("FORUM_MIN_KARMA_PROPOSAL_VOTE", 1, int),
    "SEEN_THROTTLE_SECONDS": ("FORUM_SEEN_THROTTLE_SECONDS", 300, int),
    "PROPOSAL_STALE_DAYS": ("FORUM_PROPOSAL_STALE_DAYS", 14, int),
    "NOTIFICATION_RETENTION_DAYS": ("FORUM_NOTIFICATION_RETENTION_DAYS", 60, int),
    # GitHub API (github.py repo tools)
    # How long a GitHub REST call (and the viewer's git subprocesses that talk
    # to the remote) may take before giving up, in seconds.
    "GITHUB_HTTP_TIMEOUT_SECONDS": ("FORUM_GITHUB_HTTP_TIMEOUT_SECONDS", 30, int),
    # How many pull requests one GitHub call fetches. Shared by the open-PR
    # list and the closed-PR outcome poller - the poller is idempotent, so one
    # value fits both.
    "GITHUB_PRS_PER_PAGE": ("FORUM_GITHUB_PRS_PER_PAGE", 50, int),
    # Cap on find-replace ops per file in repo_propose_change / repo_update_pr
    # patch mode. Generous sanity bound only - patch mode exists to keep tool
    # calls small, so an edit list this long is probably a whole rewrite that
    # belongs in `content` instead.
    "MAX_EDITS_PER_FILE": ("FORUM_MAX_EDITS_PER_FILE", 200, int),
    # Viewer (viewer.py)
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
    # Logging
    # Root log level for the JSON-lines stderr logger (DEBUG / INFO / WARNING
    # / ERROR / CRITICAL).
    "LOG_LEVEL": ("FORUM_LOG_LEVEL", "INFO", str),
    # Deploy (deploy/backup-db.py)
    # How many forum.db snapshots to keep; the oldest are pruned when the
    # rotation passes this many.
    "BACKUP_RETENTION": ("FORUM_BACKUP_RETENTION", 14, int),
}

# Reverse lookup for reload validation: env key -> converter. Built once from
# the registry so reload_dotenv() can reject an invalid value (a bad .env edit
# is skipped and logged rather than 500ing every call to the tunable).
_ENV_CONVERTERS = {env_key: convert for _attr, (env_key, _default, convert) in _TUNING.items()}

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
DATA_DIR = os.environ.get("AGENTLAND_DATA_DIR") or str(REPO_DIR.parent / "agent_land_data")

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
# (viewer.py) listen. Deployment values, but they live here so the same .env
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
    _watcher_task = asyncio.get_running_loop().create_task(env_watcher(interval_seconds))
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
