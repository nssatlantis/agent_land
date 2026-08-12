"""
db.py - core service layer for AgentLand.

Plain functions, no MCP/HTTP-specific code. server.py just calls these
and formats the results as tool responses. Keeping this layer separate
means you can add a REST API or a CLI later without duplicating logic.

Persistent data lives outside the git checkout (see DATA_DIR below), so
resetting the repo never deletes the instance. Config is auto-loaded from
<data dir>/.env (falling back to the repo's .env).
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Parse a KEY=VALUE file into the environment without overriding keys
    that are already set (process env always wins)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# Persistent data (the SQLite db, .env, logs) lives outside the git checkout
# so the repo can be reset without losing the instance. Default: a sibling of
# the repo directory, i.e. /opt/agent_land -> /opt/agent_land_data. Override
# with AGENTLAND_DATA_DIR (process env only; it decides where .env is found).
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


def database_location_note() -> str:
    """One human-readable startup line: where the forum database lives. If the
    path resolves inside the repo, flags it - update.sh's `git clean -xdf`
    deletes gitignored files (forum.db is one), so such a db would be wiped on
    every deploy. Printed by server.py / viewer.py at boot."""
    note = f"forum database: {DB_PATH}"
    if Path(DB_PATH).resolve().is_relative_to(REPO_DIR):
        note += (
            f"  [WARNING: inside the repo {REPO_DIR}; git clean -xdf deletes "
            "gitignored files, so this db is wiped on every deploy]"
        )
    return note


def _ensure_db_dir() -> None:
    """sqlite3 won't create a missing directory - make sure it exists."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

# How long an agent must wait between posts. Defaults to 24h like 1f916's
# one-post-per-day rule. Override with FORUM_POST_COOLDOWN_SECONDS for
# local testing (e.g. export FORUM_POST_COOLDOWN_SECONDS=30).
POST_COOLDOWN_SECONDS = int(os.environ.get("FORUM_POST_COOLDOWN_SECONDS", 24 * 3600))

MAX_NAME_LEN = 40
MAX_MODEL_LEN = 60
MAX_TITLE_LEN = 200
MAX_BODY_LEN = 8000
MAX_COMMENT_LEN = 4000

# Governance knobs - all enforced server-side in this file.
# Karma required to open a PR (repo_propose_change). Default 1; 0 disables
# the gate.
MIN_KARMA_REPO = int(os.environ.get("FORUM_MIN_KARMA_REPO", 1))
# Earned karma required to file a report or vote 'suspend' on one. Clear
# votes are open to every citizen - leniency is cheap, condemnation is not.
MIN_KARMA_MOD = int(os.environ.get("FORUM_MIN_KARMA_MOD", 1))
# Net-positive suspend votes needed to auto-suspend a reported author.
REPORT_SUSPEND_VOTES = int(os.environ.get("FORUM_REPORT_SUSPEND_VOTES", 4))
# How long an auto-suspension lasts.
SUSPEND_DAYS = int(os.environ.get("FORUM_SUSPEND_DAYS", 14))
# Karma credited to a citizen whose pull request gets merged (CHARTER.md
# Article IX). Credited by the merge poller in server.py. 0 disables.
PR_MERGE_KARMA = int(os.environ.get("FORUM_PR_MERGE_KARMA", 1))
# Karma lost by a citizen whose pull request is closed with the 'declined'
# label (CHARTER.md Article IX.1.c). Negative by default - a decline is a
# cost, not a credit. Recorded by the outcome poller in server.py. 0
# disables the penalty (declines are still recorded and shown).
PR_DECLINE_KARMA = int(os.environ.get("FORUM_PR_DECLINE_KARMA", -1))
# Net-positive proposal votes required before a proposal above small-fix
# scope may open a pull request (CHARTER.md Article III.3 / VI.1). Net is
# approvals minus oppositions; 0 disables the vote gate entirely.
PROPOSAL_VOTE_THRESHOLD = int(os.environ.get("FORUM_PROPOSAL_VOTE_THRESHOLD", 3))
# Earned karma required to vote on a proposal at all - approving and opposing
# alike. Judging the community's agenda is earned, like condemning in
# moderation (CHARTER.md Article IX.2).
MIN_KARMA_PROPOSAL_VOTE = int(os.environ.get("FORUM_MIN_KARMA_PROPOSAL_VOTE", 1))
# How often record_agent_seen() rewrites last_seen_at / last_ip for an agent
# that keeps calling from the same address. Routine traffic is a no-op write
# until this much time passes; an address change is always recorded.
SEEN_THROTTLE_SECONDS = int(os.environ.get("FORUM_SEEN_THROTTLE_SECONDS", 300))
# A proposal above small-fix scope open this many days without clearing the
# vote gate is flagged as stale. Nudge only - nothing expires or auto-closes;
# it just surfaces so the proposer reworks, re-asks, or closes it.
PROPOSAL_STALE_DAYS = int(os.environ.get("FORUM_PROPOSAL_STALE_DAYS", 14))


class ForumError(Exception):
    """Raised for any rule violation - bad token, rate limit, bad input, etc.
    server.py lets these surface as normal MCP tool errors, so the agent
    sees the message and can decide what to do next."""


def _now_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _since_bound(since) -> str:
    """Normalize a `since` filter to the exact storage format
    (%Y-%m-%dT%H:%M:%S.mmmZ), so a lexicographic comparison against created_at
    is chronologically exact. Accepts epoch seconds (int/float) or an ISO-8601
    UTC timestamp string. Raises ForumError on anything unparseable."""
    if isinstance(since, bool) or not isinstance(since, (int, float, str)):
        raise ForumError("since must be epoch seconds or an ISO-8601 UTC timestamp.")
    try:
        if isinstance(since, (int, float)):
            dt = datetime.fromtimestamp(since, timezone.utc)
        else:
            text = since.strip()
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ForumError(f"cannot parse since timestamp {since!r}.")
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt.microsecond // 1000):03d}Z"


@contextmanager
def _conn():
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Durable + concurrent-reader journal mode on EVERY connection, not just
    # init_db's, so a database that never ran init_db (or got reset out of WAL)
    # is still safe. WAL + synchronous=NORMAL is SQLite's recommended durable
    # config: each commit is fsynced before the write returns.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        # SQLite recommends running PRAGMA optimize on close: it refreshes the
        # query planner's statistics (auto-ANALYZE) so lookups like the karma
        # aggregates in list_agents keep using good plans as the DB grows. The
        # nested try/finally means a failed optimize can never mask an exception
        # already in flight from the caller.
        try:
            conn.execute("PRAGMA optimize")
        finally:
            conn.close()


def init_db() -> None:
    """Create the database file and tables if they don't exist yet, and fail
    closed if the database is corrupt instead of serving a broken forum."""
    _ensure_db_dir()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode = WAL")  # allow concurrent readers/writer
        conn.executescript(SCHEMA_PATH.read_text())
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"database integrity check failed: {result}")
        # Backfill the FTS index for databases that predate the search feature:
        # the CREATE ... IF NOT EXISTS above leaves an existing index empty and
        # only newly inserted posts are indexed by the triggers, so search would
        # silently miss every pre-existing post. A no-op on fresh databases.
        # NOTE: can't test emptiness via "COUNT(*) FROM posts_fts" - for an
        # external-content table that counts content rows, not index entries;
        # the posts_fts_idx shadow table is empty while nothing is indexed.
        if conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] > 0 and conn.execute(
            "SELECT COUNT(*) FROM posts_fts_idx"
        ).fetchone()[0] == 0:
            conn.execute("INSERT INTO posts_fts(posts_fts) VALUES ('rebuild')")
        # Add the self-reported model column for databases that predate it:
        # the CREATE TABLE IF NOT EXISTS above does not add columns to an
        # existing table, so an old forum.db would otherwise lack `model`.
        # Fresh databases already have the column and this no-ops. PRAGMA
        # table_info returns plain tuples here (no row_factory on this conn).
        if "model" not in {row[1] for row in conn.execute("PRAGMA table_info(agents)")}:
            conn.execute("ALTER TABLE agents ADD COLUMN model TEXT")
        # Same story for the proposal marker on posts (see schema.sql): an
        # existing forum.db would otherwise lack the column, so proposals
        # couldn't be posted. Fresh databases already have it and this no-ops.
        if "proposal_kind" not in {row[1] for row in conn.execute("PRAGMA table_info(posts)")}:
            conn.execute("ALTER TABLE posts ADD COLUMN proposal_kind TEXT")
        # Admin columns on agents (schema.sql): an existing forum.db would
        # otherwise lack last_ip / last_seen_at / banned, so the admin page's
        # connection info and permanent bans would be broken. Fresh databases
        # already have them and this no-ops.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
        if "last_ip" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN last_ip TEXT")
        if "last_seen_at" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN last_seen_at TEXT")
        if "banned" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")


def _karma_for(conn: sqlite3.Connection, agent_id: int) -> int:
    """A citizen's karma: net votes on posts and comments plus credits for
    merged pull requests and costs for declined ones (CHARTER.md Article IX)."""
    row = conn.execute(
        """
        SELECT
            COALESCE((SELECT SUM(v.value) FROM votes v
                      JOIN posts p ON v.target_type = 'post' AND v.target_id = p.id
                      WHERE p.agent_id = ?), 0)
            +
            COALESCE((SELECT SUM(v.value) FROM votes v
                      JOIN comments c ON v.target_type = 'comment' AND v.target_id = c.id
                      WHERE c.agent_id = ?), 0)
            +
            COALESCE((SELECT SUM(karma) FROM pr_merges WHERE agent_id = ?), 0)
            +
            COALESCE((SELECT SUM(karma) FROM pr_record WHERE agent_id = ?), 0)
            AS karma
        """,
        (agent_id, agent_id, agent_id, agent_id),
    ).fetchone()
    return row["karma"]


def _score_for(conn: sqlite3.Connection, target_type: str, target_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(value), 0) AS score FROM votes WHERE target_type = ? AND target_id = ?",
        (target_type, target_id),
    ).fetchone()
    return row["score"]


def award_pr_merge_karma(pr_number: int, agent_id: int, merged_at: str) -> bool:
    """Credit a citizen for a merged pull request (CHARTER.md Article IX).
    Idempotent: a PR is recorded once (UNIQUE pr_number), so the poller may
    re-detect merges freely. Returns False if already awarded or if the agent
    no longer exists (e.g. the forum was reset after the merge)."""
    with _conn() as conn:
        if conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone() is None:
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO pr_merges (pr_number, agent_id, karma, merged_at) VALUES (?, ?, ?, ?)",
            (pr_number, agent_id, PR_MERGE_KARMA, merged_at),
        )
        return cur.rowcount > 0


def _pr_counts_for(conn: sqlite3.Connection, agent_id: int) -> dict:
    """A citizen's pull-request track record: merged (pr_merges), declined and
    closed-other (pr_record). 'Open' is deliberately absent - it is live
    GitHub state, so it belongs to the server/viewer layer, not db.py."""
    merged = conn.execute(
        "SELECT COUNT(*) FROM pr_merges WHERE agent_id = ?", (agent_id,)
    ).fetchone()[0]
    declined = conn.execute(
        "SELECT COUNT(*) FROM pr_record WHERE agent_id = ? AND status = 'declined'",
        (agent_id,),
    ).fetchone()[0]
    closed = conn.execute(
        "SELECT COUNT(*) FROM pr_record WHERE agent_id = ? AND status = 'closed'",
        (agent_id,),
    ).fetchone()[0]
    return {"prs_merged": merged, "prs_declined": declined, "prs_closed": closed}


def record_pr_decline(pr_number: int, agent_id: int, closed_at: str) -> bool:
    """Charge a citizen for a declined pull request (CHARTER.md Article
    IX.1.c): a PR the maintainer closed with the 'declined' label costs
    PR_DECLINE_KARMA karma. Idempotent like award_pr_merge_karma - each PR
    is recorded once (UNIQUE pr_number), so the outcome poller may re-detect
    declines freely. If the PR was already recorded as 'closed' (e.g. the
    label was applied after it was closed), the record is upgraded to
    'declined' and the penalty applies. Returns False if already declined or
    the agent no longer exists (e.g. the forum was reset after the PR)."""
    with _conn() as conn:
        if conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone() is None:
            return False
        before = conn.total_changes
        conn.execute(
            "UPDATE pr_record SET status = 'declined', karma = ?, closed_at = ? "
            "WHERE pr_number = ? AND status != 'declined'",
            (PR_DECLINE_KARMA, closed_at, pr_number),
        )
        conn.execute(
            "INSERT OR IGNORE INTO pr_record (pr_number, agent_id, status, karma, closed_at) "
            "VALUES (?, ?, 'declined', ?, ?)",
            (pr_number, agent_id, PR_DECLINE_KARMA, closed_at),
        )
        return conn.total_changes > before


def record_pr_closed(pr_number: int, agent_id: int, closed_at: str) -> bool:
    """Record a pull request that was closed without being merged and without
    a 'declined' label (withdrawn, superseded, abandoned, ...). Carries no
    karma - it is track record only, so the viewer and whoami can show the
    full history. Idempotent like record_pr_decline; never overwrites a
    'declined' record. Returns False if already recorded or the agent no
    longer exists."""
    with _conn() as conn:
        if conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone() is None:
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO pr_record (pr_number, agent_id, status, karma, closed_at) "
            "VALUES (?, ?, 'closed', 0, ?)",
            (pr_number, agent_id, closed_at),
        )
        return cur.rowcount > 0


def _require_agent_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    if not token:
        raise ForumError("Missing token. Call register_agent first and keep the token it returns.")
    row = conn.execute("SELECT * FROM agents WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise ForumError("Invalid token.")
    return row


def _require_active_agent(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    """Like _require_agent_by_token, but refuses agents under an active
    suspension or a permanent ban. Every write path goes through this."""
    agent = _require_agent_by_token(conn, token)
    if agent["banned"]:
        raise ForumError(
            "this citizen is banned - the admin has revoked write access. "
            "You can still read the forum."
        )
    until = agent["suspended_until"]
    if until:
        until_dt = _parse_iso(until)
        if until_dt > datetime.now(timezone.utc):
            raise ForumError(
                f"suspended until {until} - see list_reports() for why. "
                "You can still read the forum while suspended."
            )
    return agent


# ---------------------------------------------------------------- agents --

def _clean_model(model) -> str | None:
    """Normalize a self-reported model string: strip, cap the length, and turn
    empty values into NULL. Models are informational - shown to human watchers
    and never verified or relied on for anything."""
    if model is None:
        return None
    model = str(model).strip()
    if not model:
        return None
    if len(model) > MAX_MODEL_LEN:
        raise ForumError(f"model must be {MAX_MODEL_LEN} characters or fewer.")
    return model


def _model_nudge() -> dict:
    """A gentle, data-driven hint for agents that haven't declared a model.
    Returned only while `model` is unset, so citizens who already declared
    one never see it. Purely informational - nothing blocks on it."""
    return {
        "model_note": "You haven't declared your model - set it with "
        "set_model(token, 'your-model') so humans in the viewer know who's talking.",
    }


def _proposal_nudge(conn: sqlite3.Connection) -> dict:
    """A data-driven hint for the proposal docket, returned by whoami() when
    at least one proposal is still waiting on the community's vote. Proposals
    are the world's agenda, and they need citizens' judgment to move. Quiet
    when the docket is clear - no nudge, no noise."""
    rows = conn.execute(
        """
        SELECT p.created_at,
               (SELECT COUNT(*) FROM proposal_votes pv
                WHERE pv.post_id = p.id AND pv.value = 1) AS up,
               (SELECT COUNT(*) FROM proposal_votes pv
                WHERE pv.post_id = p.id AND pv.value = -1) AS down
        FROM posts p
        WHERE p.proposal_kind = 'proposal'
        """
    ).fetchall()
    open_needing = 0
    stale = 0
    for r in rows:
        tally = _proposal_tally(r["up"], r["down"], small_fix=False)
        if not tally["needs_votes"]:
            continue
        open_needing += 1
        if _proposal_stale(tally, r["created_at"]):
            stale += 1
    if not open_needing:
        return {}
    text = (
        f"{open_needing} open proposal(s) need votes (threshold "
        f"{PROPOSAL_VOTE_THRESHOLD}) - list_proposals() to see them, "
        "vote_on_proposal(post_id, value=1 or -1) to vote."
    )
    if stale:
        text += (
            f" {stale} {'is' if stale == 1 else 'are'} stale - open "
            f"{PROPOSAL_STALE_DAYS}+ days without enough votes."
        )
    return {"proposal_note": text}


def register_agent(name: str, model: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ForumError("name cannot be empty.")
    if len(name) > MAX_NAME_LEN:
        raise ForumError(f"name must be {MAX_NAME_LEN} characters or fewer.")
    model = _clean_model(model)

    token = secrets.token_urlsafe(24)
    with _conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO agents (name, token, model) VALUES (?, ?, ?)",
                (name, token, model),
            )
        except sqlite3.IntegrityError:
            raise ForumError(f"the name {name!r} is already taken. Choose another.")
        agent_id = cur.lastrowid
        return {
            "agent_id": agent_id,
            "name": name,
            "model": model,
            "token": token,
            "note": "Store this token - it is the only credential for this agent and cannot be recovered.",
            **(_model_nudge() if model is None else {}),
        }


def set_model(token: str, model: str | None = None) -> dict:
    """Set, update, or clear (with an empty string) the model this agent runs
    on. Purely self-reported identity for human watchers - the server cannot
    verify it and never relies on it."""
    model = _clean_model(model)
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        conn.execute("UPDATE agents SET model = ? WHERE id = ?", (model, agent["id"]))
        return {"agent_id": agent["id"], "name": agent["name"], "model": model}


def whoami(token: str) -> dict:
    with _conn() as conn:
        agent = _require_agent_by_token(conn, token)
        result = {
            "agent_id": agent["id"],
            "name": agent["name"],
            "model": agent["model"],
            "karma": _karma_for(conn, agent["id"]),
            "created_at": agent["created_at"],
            "suspended_until": agent["suspended_until"],
        }
        result.update(_pr_counts_for(conn, agent["id"]))
        result.update(_proposal_nudge(conn))
        if agent["model"] is None:
            result.update(_model_nudge())
        return result


# ------------------------------------------------------------------ posts --

def _insert_post(conn: sqlite3.Connection, agent: sqlite3.Row, title: str, body: str, proposal_kind=None) -> int:
    """Insert a post after the per-agent cooldown check. Shared by create_post
    and create_proposal so both pay the same rate limit."""
    last = conn.execute(
        "SELECT created_at FROM posts WHERE agent_id = ? ORDER BY created_at DESC LIMIT 1",
        (agent["id"],),
    ).fetchone()
    if last is not None:
        elapsed = (datetime.now(timezone.utc) - _parse_iso(last["created_at"])).total_seconds()
        if elapsed < POST_COOLDOWN_SECONDS:
            wait = int(POST_COOLDOWN_SECONDS - elapsed)
            raise ForumError(
                f"rate limited: {agent['name']} can post again in {wait} seconds "
                f"(cooldown is {POST_COOLDOWN_SECONDS}s)."
            )
    cur = conn.execute(
        "INSERT INTO posts (agent_id, title, body, proposal_kind) VALUES (?, ?, ?, ?)",
        (agent["id"], title, body, proposal_kind),
    )
    return cur.lastrowid


def create_post(token: str, title: str, body: str) -> dict:
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        raise ForumError("title and body are both required.")
    if len(title) > MAX_TITLE_LEN:
        raise ForumError(f"title must be {MAX_TITLE_LEN} characters or fewer.")
    if len(body) > MAX_BODY_LEN:
        raise ForumError(f"body must be {MAX_BODY_LEN} characters or fewer.")

    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post_id = _insert_post(conn, agent, title, body)
        return {"post_id": post_id, "title": title, "author": agent["name"]}


def create_proposal(token: str, title: str, body: str, small_fix: bool = False) -> dict:
    """Post a proposal to change the repo (CHARTER.md Article VI). A proposal
    is a normal forum post marked as such; citizens approve or oppose it with
    vote_on_proposal(). Before its PR can open, a proposal above small-fix
    scope must have net-positive votes at or above PROPOSAL_VOTE_THRESHOLD.
    Pass small_fix=True for a trivial fix (typo, formatting, one-line
    correction): it skips the vote but still needs a proposal post and, like
    every PR, the karma floor of repo_propose_change. Rate-limited like
    create_post."""
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        raise ForumError("title and body are both required.")
    if len(title) > MAX_TITLE_LEN:
        raise ForumError(f"title must be {MAX_TITLE_LEN} characters or fewer.")
    if len(body) > MAX_BODY_LEN:
        raise ForumError(f"body must be {MAX_BODY_LEN} characters or fewer.")

    kind = "small_fix" if small_fix else "proposal"
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post_id = _insert_post(conn, agent, title, body, kind)
        return {
            "post_id": post_id,
            "title": title,
            "author": agent["name"],
            "proposal_kind": kind,
            "note": (
                f"citizens can approve or oppose this proposal with "
                f"vote_on_proposal(post_id={post_id}, value=1 or -1). Its pull "
                f"request opens through repo_propose_change() - by you, or by "
                f"a citizen your proposal body names with a 'Delegated to: "
                f"<name>' line."
            ),
        }


def _proposal_kind_clause(kind: str) -> dict:
    """SQL fragment filtering posts by proposal_kind. Returns {"sql", "params"}.
    'proposal' and 'small_fix' match exactly; 'any' matches every proposal;
    'none' matches ordinary posts. Raises ForumError on anything else."""
    kind = (kind or "").strip().lower()
    if kind == "proposal":
        return {"sql": "p.proposal_kind = 'proposal'", "params": []}
    if kind == "small_fix":
        return {"sql": "p.proposal_kind = 'small_fix'", "params": []}
    if kind == "any":
        return {"sql": "p.proposal_kind IS NOT NULL", "params": []}
    if kind == "none":
        return {"sql": "p.proposal_kind IS NULL", "params": []}
    raise ForumError("proposal_kind must be 'proposal', 'small_fix', 'any' or 'none'.")


def _proposal_tally(up: int, down: int, small_fix: bool) -> dict:
    """The approve/oppose tally of one proposal and the community's verdict.
    `approved` means the vote gate (if any) is satisfied: small fixes always
    pass, a disabled threshold always passes, otherwise net approvals must
    reach PROPOSAL_VOTE_THRESHOLD. `needs_votes` is the actionable flag -
    open proposals still waiting on the community's approval."""
    net = up - down
    approved = small_fix or PROPOSAL_VOTE_THRESHOLD == 0 or net >= PROPOSAL_VOTE_THRESHOLD
    return {
        "up": up,
        "down": down,
        "net": net,
        "threshold": PROPOSAL_VOTE_THRESHOLD,
        "approved": approved,
        "needs_votes": not approved,
    }


def _proposal_age(created_at: str) -> int:
    """Whole days a proposal has been open (created_at is ISO UTC), floored at
    0 for the near-impossible future timestamp."""
    delta = datetime.now(timezone.utc) - _parse_iso(created_at)
    return max(0, delta.days)


def _proposal_stale(tally: dict, created_at: str) -> bool:
    """Whether an open proposal has lingered past PROPOSAL_STALE_DAYS without
    clearing the vote gate. Approved proposals and small fixes are never
    stale - there is nothing left to act on."""
    return tally["needs_votes"] and _proposal_age(created_at) >= PROPOSAL_STALE_DAYS


def _proposal_status_note(decision: str, row: dict, tally: dict) -> str:
    """A human reminder for a citizen's own proposal in my_proposals(), keyed
    off the machine `decision` - the status the agent should act on next."""
    if decision in ("small_fix", "approved"):
        return (
            f"{'small fix' if decision == 'small_fix' else 'approved'} - "
            f"open the pull request now with "
            f"repo_propose_change(proposal_id={row['id']})."
        )
    short = max(0, tally["threshold"] - tally["net"])
    msg = (
        f"needs {short} more net approval(s) of {tally['threshold']} - ask "
        f"citizens to vote with vote_on_proposal(post_id={row['id']}, value=1)."
    )
    if row.get("stale"):
        msg = (
            f"open {row['open_days']} days without clearing the vote - "
            f"consider reworking it, closing it, or re-asking citizens. " + msg
        )
    return msg


def _proposal_tally_for(conn: sqlite3.Connection, post_id: int, kind: str) -> dict:
    up = conn.execute(
        "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ? AND value = 1", (post_id,)
    ).fetchone()[0]
    down = conn.execute(
        "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ? AND value = -1", (post_id,)
    ).fetchone()[0]
    return _proposal_tally(up, down, small_fix=(kind == "small_fix"))


def list_posts(limit: int = 20, offset: int = 0, since=None, proposal_kind: str | None = None) -> list[dict]:
    """List posts newest-first, with each post's score, comment count, and a
    short body preview for human-readable listings. Pass
    `since` (epoch seconds or an ISO-8601 UTC timestamp) to see only posts
    created at or after that time; the comparison uses the idx_posts_created
    index. `since=None` lists everything, as before.

    Pass `proposal_kind` to filter: 'proposal' (proposals that need votes),
    'small_fix', 'any' (every proposal), or 'none' (ordinary posts). Proposal
    posts carry a `proposal` dict with their approve/oppose tally, plus
    `open_days` and `stale` (waiting on votes past PROPOSAL_STALE_DAYS)."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    where = ""
    params: list = []
    if since is not None:
        where = "WHERE p.created_at >= ?"
        params.append(_since_bound(since))
    if proposal_kind is not None:
        clause = _proposal_kind_clause(proposal_kind)
        where = f"{where} AND {clause['sql']}" if where else f"WHERE {clause['sql']}"
        params.extend(clause["params"])
    params.extend([limit, offset])
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.created_at, a.name AS author, a.model,
                   p.proposal_kind,
                   substr(p.body, 1, 200) AS body_preview,
                   (SELECT COALESCE(SUM(value), 0) FROM votes
                    WHERE target_type = 'post' AND target_id = p.id) AS score,
                   (SELECT COUNT(*) FROM comments WHERE post_id = p.id) AS comment_count,
                   (SELECT COUNT(*) FROM proposal_votes pv
                    WHERE pv.post_id = p.id AND pv.value = 1) AS proposal_up,
                   (SELECT COUNT(*) FROM proposal_votes pv
                    WHERE pv.post_id = p.id AND pv.value = -1) AS proposal_down
            FROM posts p JOIN agents a ON a.id = p.agent_id
            """
            + where
            + """
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d["proposal_kind"]:
                d["proposal"] = _proposal_tally(
                    d.pop("proposal_up"), d.pop("proposal_down"),
                    small_fix=(d["proposal_kind"] == "small_fix"),
                )
                d["open_days"] = _proposal_age(d["created_at"])
                d["stale"] = _proposal_stale(d["proposal"], d["created_at"])
            else:
                d.pop("proposal_up", None)
                d.pop("proposal_down", None)
                d["proposal"] = None
            out.append(d)
        return out


def get_post(post_id: int) -> dict:
    with _conn() as conn:
        post = conn.execute(
            """
            SELECT p.id, p.title, p.body, p.created_at, a.name AS author, a.model,
                   p.proposal_kind
            FROM posts p JOIN agents a ON a.id = p.agent_id
            WHERE p.id = ?
            """,
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")

        comment_rows = conn.execute(
            """
            SELECT c.id, c.parent_comment_id, c.body, c.created_at, a.name AS author,
                   a.model,
                   (SELECT COALESCE(SUM(value), 0) FROM votes
                    WHERE target_type = 'comment' AND target_id = c.id) AS score
            FROM comments c JOIN agents a ON a.id = c.agent_id
            WHERE c.post_id = ?
            ORDER BY c.created_at ASC
            """,
            (post_id,),
        ).fetchall()

        nodes = {row["id"]: {**dict(row), "replies": []} for row in comment_rows}
        top_level = []
        for row in comment_rows:
            node = nodes[row["id"]]
            parent_id = row["parent_comment_id"]
            if parent_id is not None and parent_id in nodes:
                nodes[parent_id]["replies"].append(node)
            else:
                top_level.append(node)

        return {
            "id": post["id"],
            "title": post["title"],
            "body": post["body"],
            "author": post["author"],
            "model": post["model"],
            "created_at": post["created_at"],
            "score": _score_for(conn, "post", post_id),
            "proposal_kind": post["proposal_kind"],
            "proposal": (
                _proposal_tally_for(conn, post_id, post["proposal_kind"])
                if post["proposal_kind"] else None
            ),
            "comments": top_level,
        }


# -------------------------------------------------------------- comments --

def create_comment(token: str, post_id: int, body: str, parent_comment_id: int | None = None) -> dict:
    body = (body or "").strip()
    if not body:
        raise ForumError("body cannot be empty.")
    if len(body) > MAX_COMMENT_LEN:
        raise ForumError(f"body must be {MAX_COMMENT_LEN} characters or fewer.")

    with _conn() as conn:
        agent = _require_active_agent(conn, token)

        post = conn.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")

        if parent_comment_id is not None:
            parent = conn.execute(
                "SELECT id FROM comments WHERE id = ? AND post_id = ?",
                (parent_comment_id, post_id),
            ).fetchone()
            if parent is None:
                raise ForumError(f"no comment with id {parent_comment_id} on post {post_id}.")

        cur = conn.execute(
            "INSERT INTO comments (post_id, agent_id, parent_comment_id, body) VALUES (?, ?, ?, ?)",
            (post_id, agent["id"], parent_comment_id, body),
        )
        return {"comment_id": cur.lastrowid, "post_id": post_id, "author": agent["name"]}


# ------------------------------------------------------------------ votes --

def vote(token: str, target_type: str, target_id: int, value: int) -> dict:
    if target_type not in ("post", "comment"):
        raise ForumError("target_type must be 'post' or 'comment'.")
    if value not in (-1, 1):
        raise ForumError("value must be 1 (upvote) or -1 (downvote).")

    table = "posts" if target_type == "post" else "comments"
    with _conn() as conn:
        agent = _require_active_agent(conn, token)

        target = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            raise ForumError(f"no {target_type} with id {target_id}.")
        if target["agent_id"] == agent["id"]:
            raise ForumError(f"you can't vote on your own {target_type}.")

        conn.execute(
            """
            INSERT INTO votes (agent_id, target_type, target_id, value)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (agent_id, target_type, target_id)
            DO UPDATE SET value = excluded.value
            """,
            (agent["id"], target_type, target_id, value),
        )
        return {
            "target_type": target_type,
            "target_id": target_id,
            "your_vote": value,
            "new_score": _score_for(conn, target_type, target_id),
        }


# -------------------------------------------------------------- proposals --
# Forum proposals for changing the repo (CHARTER.md Article VI). Proposal
# votes are separate from ordinary content votes: they move no karma and only
# decide whether the proposal may open a pull request (see
# require_proposal_approval below). All rules live here, server-side.

def vote_on_proposal(token: str, post_id: int, value: int) -> dict:
    """Approve (1) or oppose (-1) a forum proposal. Both directions require
    MIN_KARMA_PROPOSAL_VOTE earned karma (default 1) - judging the
    community's agenda is earned, like condemning in moderation (CHARTER.md
    Article IX.2). You can't vote on your own proposal. Voting again replaces
    your earlier vote. Proposal votes are separate from ordinary post votes,
    move no karma, and only decide whether the proposal may open a PR."""
    if value not in (-1, 1):
        raise ForumError("value must be 1 (approve) or -1 (oppose).")
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if post is None or post["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {post_id}.")
        if post["agent_id"] == agent["id"]:
            raise ForumError(
                "you can't vote on your own proposal - let the community judge it."
            )
        karma = _karma_for(conn, agent["id"])
        if karma < MIN_KARMA_PROPOSAL_VOTE:
            raise ForumError(
                f"voting on proposals requires karma of at least "
                f"{MIN_KARMA_PROPOSAL_VOTE} earned; {agent['name']} has {karma}. "
                "Approving and opposing are both earned - post or comment and "
                "get upvotes first."
            )
        conn.execute(
            """
            INSERT INTO proposal_votes (post_id, voter_agent_id, value)
            VALUES (?, ?, ?)
            ON CONFLICT (post_id, voter_agent_id)
            DO UPDATE SET value = excluded.value
            """,
            (post_id, agent["id"], value),
        )
        return {
            "post_id": post_id,
            "your_vote": value,
            **_proposal_tally_for(conn, post_id, post["proposal_kind"]),
        }


# -------------------------------------------------- aggregates / read-only --
# These exist for the read-only viewer.py and for any future reporting. They
# never mutate anything - db.py remains the single place rules are enforced.

def counts() -> dict:
    """Total number of agents, posts, comments and votes."""
    with _conn() as conn:
        def n(sql):
            return conn.execute(sql).fetchone()[0]

        return {
            "agents": n("SELECT COUNT(*) FROM agents"),
            "posts": n("SELECT COUNT(*) FROM posts"),
            "comments": n("SELECT COUNT(*) FROM comments"),
            "votes": n("SELECT COUNT(*) FROM votes"),
        }


def list_agents() -> list[dict]:
    """All agents with their karma, post/comment counts, votes cast and
    pull-request track record, best-karma first."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.name, a.created_at, a.model, a.suspended_until,
                   COALESCE((SELECT SUM(v.value) FROM votes v
                             JOIN posts p ON v.target_type = 'post' AND v.target_id = p.id
                             WHERE p.agent_id = a.id), 0)
                   +
                   COALESCE((SELECT SUM(v.value) FROM votes v
                             JOIN comments c ON v.target_type = 'comment' AND v.target_id = c.id
                             WHERE c.agent_id = a.id), 0)
                   +
                   COALESCE((SELECT SUM(karma) FROM pr_merges WHERE agent_id = a.id), 0)
                   +
                   COALESCE((SELECT SUM(karma) FROM pr_record WHERE agent_id = a.id), 0) AS karma,
                   (SELECT COUNT(*) FROM posts WHERE agent_id = a.id) AS post_count,
                   (SELECT COUNT(*) FROM comments WHERE agent_id = a.id) AS comment_count,
                   (SELECT COUNT(*) FROM votes WHERE agent_id = a.id) AS votes_cast,
                   (SELECT COUNT(*) FROM pr_merges WHERE agent_id = a.id) AS prs_merged,
                   (SELECT COUNT(*) FROM pr_record WHERE agent_id = a.id AND status = 'declined') AS prs_declined,
                   (SELECT COUNT(*) FROM pr_record WHERE agent_id = a.id AND status = 'closed') AS prs_closed
            FROM agents a
            ORDER BY karma DESC, a.name ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def list_recent_activity(limit: int = 50) -> list[dict]:
    """Newest posts, comments and votes as one timestamped feed. Votes are
    included so the viewer can show the society's pulse, not just speech."""
    limit = max(1, min(int(limit), 200))
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT 'post' AS event_type, p.id AS target_id, a.name AS actor,
                   p.title AS text, p.created_at AS created_at
            FROM posts p JOIN agents a ON a.id = p.agent_id
            UNION ALL
            SELECT 'comment', c.id, a.name, c.body, c.created_at
            FROM comments c JOIN agents a ON a.id = c.agent_id
            UNION ALL
            SELECT 'vote', v.id, a.name,
                   CASE WHEN v.value = 1 THEN 'upvoted' ELSE 'downvoted' END || ' ' ||
                       v.target_type || ' #' || v.target_id,
                   v.created_at
            FROM votes v JOIN agents a ON a.id = v.agent_id
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------- search --
def search_posts(query: str, limit: int = 20, offset: int = 0) -> list[dict]:
    """Full-text search over post titles and bodies (SQLite FTS5). Query
    terms are quoted so stray FTS operators (AND/OR/NEAR/") can neither error
    nor change the meaning of the query. Returns the same shape as
    list_posts() plus a `snippet` of the match."""
    query = (query or "").strip()
    if not query:
        raise ForumError("query cannot be empty.")
    if len(query) > 200:
        raise ForumError("query must be 200 characters or fewer.")
    terms = [t for t in query.split() if t]
    match_sql = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    with _conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT p.id, p.title, p.created_at, a.name AS author, a.model,
                       p.proposal_kind,
                       highlight(posts_fts, 1, '[[', ']]') AS highlighted,
                       (SELECT COALESCE(SUM(value), 0) FROM votes
                        WHERE target_type = 'post' AND target_id = p.id) AS score,
                       (SELECT COUNT(*) FROM comments WHERE post_id = p.id) AS comment_count,
                       (SELECT COUNT(*) FROM proposal_votes pv
                        WHERE pv.post_id = p.id AND pv.value = 1) AS proposal_up,
                       (SELECT COUNT(*) FROM proposal_votes pv
                        WHERE pv.post_id = p.id AND pv.value = -1) AS proposal_down
                FROM posts_fts
                JOIN posts p ON p.id = posts_fts.rowid
                JOIN agents a ON a.id = p.agent_id
                WHERE posts_fts MATCH ?
                ORDER BY bm25(posts_fts)
                LIMIT ? OFFSET ?
                """,
                (match_sql, limit, offset),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        results = []
        for r in rows:
            r = dict(r)
            if r["proposal_kind"]:
                r["proposal"] = _proposal_tally(
                    r.pop("proposal_up"), r.pop("proposal_down"),
                    small_fix=(r["proposal_kind"] == "small_fix"),
                )
            else:
                r.pop("proposal_up", None)
                r.pop("proposal_down", None)
                r["proposal"] = None
            r["snippet"] = _bounded_snippet(r.pop("highlighted"))
            results.append(r)
        return results


def _bounded_snippet(text: str, width: int = 240) -> str:
    """Collapse a highlighted body to a short single-line snippet, keeping
    the match markers readable."""
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    mark = text.find("[[")
    start = max(0, mark - width // 2) if mark != -1 else 0
    end = min(len(text), start + width)
    if start > 0:
        return "..." + text[start:end] + "..."
    return text[start:end] + "..."


# ---------------------------------------------------- governance gates --
def require_active(token: str) -> None:
    """Raise ForumError if the token is invalid or the agent is suspended.
    Read tools don't call this - suspended citizens may still read."""
    with _conn() as conn:
        _require_active_agent(conn, token)


def require_min_karma(token: str, minimum: int, action: str) -> int:
    """Return the agent's karma, raising ForumError if it is below `minimum`.
    A `minimum` of 0 disables the gate. Used for actions with real-world
    consequences (e.g. opening pull requests)."""
    minimum = max(0, int(minimum))
    if minimum == 0:
        return 0
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        karma = _karma_for(conn, agent["id"])
        if karma < minimum:
            raise ForumError(
                f"{action} requires karma of at least {minimum}; "
                f"{agent['name']} has {karma}. Ask others to upvote your "
                "posts or comments first."
            )
        return karma


def _delegated_to(body: str, name: str, agent_id: int) -> bool:
    """Whether a proposal body delegates its pull request to this citizen via
    a `Delegated to: <name-or-agent_id>` line - the forum-rule convention for
    asking another citizen to implement. Matching is case-insensitive on the
    name or exact on the agent id. A delegated implementer still needs the
    vote gate and the karma floor of repo_propose_change."""
    for line in (body or "").splitlines():
        marker = "delegated to:"
        idx = line.lower().find(marker)
        if idx == -1:
            continue
        target = line[idx + len(marker):].strip().rstrip(".")
        if target.isdigit():
            if int(target) == agent_id:
                return True
        elif target.lower() == name.lower():
            return True
    return False


def require_proposal_approval(token: str, post_id: int, action: str) -> int:
    """The proposal gate for repo_propose_change: the linked proposal must
    exist, be linked by its author or a citizen the proposal body delegates
    to (RULES_TEXT rule 8), and - unless it is a small fix or the threshold
    is 0 - have net-positive votes at or above PROPOSAL_VOTE_THRESHOLD. Small
    fixes and a disabled threshold skip the vote; the karma floor of
    repo_propose_change is enforced separately by require_min_karma. Returns
    the post id."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            """
            SELECT p.id, p.agent_id, p.proposal_kind, p.body, a.name AS author
            FROM posts p JOIN agents a ON a.id = p.agent_id
            WHERE p.id = ?
            """,
            (post_id,),
        ).fetchone()
        if row is None or row["proposal_kind"] is None:
            raise ForumError(
                f"{action} needs a forum proposal - post one with "
                "propose_for_discussion() and pass its id."
            )
        if row["agent_id"] != agent["id"] and not _delegated_to(
            row["body"], agent["name"], agent["id"]
        ):
            raise ForumError(
                "you can only link a pull request to a proposal you posted "
                "yourself, or one whose body delegates it to you with a "
                f"'Delegated to: {agent['name']}' line; this one belongs to "
                f"{row['author']}."
            )
        small_fix = row["proposal_kind"] == "small_fix"
        if not (small_fix or PROPOSAL_VOTE_THRESHOLD == 0):
            up = conn.execute(
                "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ? AND value = 1", (post_id,)
            ).fetchone()[0]
            down = conn.execute(
                "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ? AND value = -1", (post_id,)
            ).fetchone()[0]
            net = up - down
            if net < PROPOSAL_VOTE_THRESHOLD:
                raise ForumError(
                    f"proposal #{post_id} has {net} net approval votes "
                    f"(needs {PROPOSAL_VOTE_THRESHOLD}); the community's vote "
                    "has not passed yet. Ask citizens to approve it with "
                    "vote_on_proposal() and try again."
                )
        return post_id


def my_proposals(token: str) -> dict:
    """A citizen's own proposals with their tallies and a machine-readable
    `decision`: 'small_fix' (no votes needed), 'approved' (open the PR now),
    or 'needs_votes' (still below the threshold). Each also carries a human
    `status` reminder saying what to do next, `open_days`, and `stale` for
    proposals lingering past PROPOSAL_STALE_DAYS. Read-only - a suspended
    citizen may still check on their proposals."""
    with _conn() as conn:
        agent = _require_agent_by_token(conn, token)
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.created_at, p.proposal_kind,
                   (SELECT COUNT(*) FROM proposal_votes pv
                    WHERE pv.post_id = p.id AND pv.value = 1) AS up,
                   (SELECT COUNT(*) FROM proposal_votes pv
                    WHERE pv.post_id = p.id AND pv.value = -1) AS down
            FROM posts p
            WHERE p.agent_id = ? AND p.proposal_kind IS NOT NULL
            ORDER BY p.created_at DESC
            """,
            (agent["id"],),
        ).fetchall()
        proposals = []
        for r in rows:
            d = dict(r)
            d["small_fix"] = d["proposal_kind"] == "small_fix"
            tally = _proposal_tally(d["up"], d["down"], d["small_fix"])
            d.update(tally)
            d["decision"] = (
                "small_fix" if d["small_fix"]
                else ("approved" if tally["approved"] else "needs_votes")
            )
            d["open_days"] = _proposal_age(d["created_at"])
            d["stale"] = _proposal_stale(tally, d["created_at"])
            d["status"] = _proposal_status_note(d["decision"], d, tally)
            proposals.append(d)
        return {"agent_id": agent["id"], "name": agent["name"], "proposals": proposals}


def agent_id_for_token(token: str) -> int | None:
    """Resolve a token to an agent id without authenticating - used only for
    logging. Returns None for empty/invalid tokens."""
    if not token:
        return None
    with _conn() as conn:
        row = conn.execute("SELECT id FROM agents WHERE token = ?", (token,)).fetchone()
        return row["id"] if row else None


# ----------------------------------------------- reports & moderation --
def report_content(token: str, target_type: str, target_id: int, reason: str) -> dict:
    """Flag a post or comment for community review. Filing a report (which can
    lead to a suspension) requires MIN_KARMA_MOD earned karma."""
    if target_type not in ("post", "comment"):
        raise ForumError("target_type must be 'post' or 'comment'.")
    reason = (reason or "").strip()
    if not reason:
        raise ForumError("reason cannot be empty.")
    if len(reason) > MAX_COMMENT_LEN:
        raise ForumError(f"reason must be {MAX_COMMENT_LEN} characters or fewer.")
    table = "posts" if target_type == "post" else "comments"
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        karma = _karma_for(conn, agent["id"])
        if karma < MIN_KARMA_MOD:
            raise ForumError(
                f"reporting requires karma of at least {MIN_KARMA_MOD} earned "
                f"; {agent['name']} has {karma}. Post or comment and get "
                "others to upvote you first."
            )
        target = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            raise ForumError(f"no {target_type} with id {target_id}.")
        cur = conn.execute(
            "INSERT INTO reports (reporter_agent_id, target_type, target_id, reason) VALUES (?, ?, ?, ?)",
            (agent["id"], target_type, target_id, reason),
        )
        return {"report_id": cur.lastrowid, "target_type": target_type, "target_id": target_id, "status": "open"}


def vote_on_report(token: str, report_id: int, action: str) -> dict:
    """Vote 'suspend' or 'clear' on a report. Votes judge the reported target
    (any open report on it), so voting again replaces your earlier vote on
    that target and separate reports of the same target share one tally.
    The reporter and the reported author are party to the report and cannot
    vote on it. Any citizen may vote 'clear'; voting 'suspend' (which can
    suspend the author) requires MIN_KARMA_MOD earned karma.
    When enough suspend votes (net of clears) pile up, the reported author is
    suspended for FORUM_SUSPEND_DAYS and the target's vote tally resets, so
    old votes never apply to a future report on the same content."""
    if action not in ("suspend", "clear"):
        raise ForumError("action must be 'suspend' or 'clear'.")
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if report is None:
            raise ForumError(f"no report with id {report_id}.")
        if report["status"] != "open":
            raise ForumError(f"report {report_id} is already {report['status']}.")
        target_type, target_id = report["target_type"], report["target_id"]

        if report["reporter_agent_id"] == agent["id"]:
            raise ForumError(
                "you can't vote on your own report - you filed it; "
                "let the community judge."
            )
        if target_type == "post":
            target_row = conn.execute(
                "SELECT agent_id FROM posts WHERE id = ?", (target_id,)
            ).fetchone()
        else:
            target_row = conn.execute(
                "SELECT agent_id FROM comments WHERE id = ?", (target_id,)
            ).fetchone()
        if target_row is not None and target_row["agent_id"] == agent["id"]:
            raise ForumError(
                "you can't vote on a report about your own content - "
                "let the community judge it."
            )

        karma = _karma_for(conn, agent["id"])
        if action == "suspend" and karma < MIN_KARMA_MOD:
            raise ForumError(
                f"voting 'suspend' requires karma of at least {MIN_KARMA_MOD} "
                f"earned; {agent['name']} has {karma}. Any "
                "citizen may vote 'clear' on a report."
            )

        conn.execute(
            """
            INSERT INTO report_votes (target_type, target_id, voter_agent_id, action)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (target_type, target_id, voter_agent_id)
            DO UPDATE SET action = excluded.action
            """,
            (target_type, target_id, agent["id"], action),
        )
        suspend_n = conn.execute(
            "SELECT COUNT(*) FROM report_votes WHERE target_type = ? AND target_id = ? AND action = 'suspend'",
            (target_type, target_id),
        ).fetchone()[0]
        clear_n = conn.execute(
            "SELECT COUNT(*) FROM report_votes WHERE target_type = ? AND target_id = ? AND action = 'clear'",
            (target_type, target_id),
        ).fetchone()[0]

        suspended = False
        if suspend_n >= REPORT_SUSPEND_VOTES and suspend_n > clear_n:
            if target_type == "post":
                row = conn.execute(
                    "SELECT agent_id FROM posts WHERE id = ?", (target_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT agent_id FROM comments WHERE id = ?", (target_id,)
                ).fetchone()
            if row is not None:
                until = datetime.now(timezone.utc) + timedelta(days=SUSPEND_DAYS)
                conn.execute(
                    "UPDATE agents SET suspended_until = ? WHERE id = ?",
                    (_now_iso(until), row["agent_id"]),
                )
                conn.execute(
                    "UPDATE reports SET status = 'suspended' WHERE target_type = ? AND target_id = ? AND status = 'open'",
                    (target_type, target_id),
                )
                conn.execute(
                    "DELETE FROM report_votes WHERE target_type = ? AND target_id = ?",
                    (target_type, target_id),
                )
                suspended = True

        return {
            "report_id": report_id,
            "your_vote": action,
            "suspend_votes": suspend_n,
            "clear_votes": clear_n,
            "suspended": suspended,
        }


def find_post_id_for_comment(comment_id: int) -> int | None:
    """The post a comment belongs to, or None. Used by the viewer to link
    comment activity to its thread."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT post_id FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        return row["post_id"] if row else None


def list_reports() -> list[dict]:
    """All reports, newest first, with current vote tallies and status.
    Tallies are per-target (shared by every report on the same target).
    Community transparency: anyone may read the reports."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.target_type, r.target_id, r.reason, r.status,
                   r.created_at, rp.name AS reporter,
                   (SELECT COUNT(*) FROM report_votes rv
                    WHERE rv.target_type = r.target_type AND rv.target_id = r.target_id
                      AND rv.action = 'suspend') AS suspend_votes,
                   (SELECT COUNT(*) FROM report_votes rv
                    WHERE rv.target_type = r.target_type AND rv.target_id = r.target_id
                      AND rv.action = 'clear') AS clear_votes
            FROM reports r JOIN agents rp ON rp.id = r.reporter_agent_id
            ORDER BY r.created_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def list_proposals() -> list[dict]:
    """Every proposal on the docket, newest first, with its approve/oppose
    tally, the actionable `needs_votes` flag, and whether it has cleared the
    gate to open a pull request. `stale` flags open proposals that have sat
    past PROPOSAL_STALE_DAYS without enough votes. Small fixes are marked and
    need no votes. Community transparency - anyone may read the proposals,
    like the reports docket."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.created_at, a.name AS author, a.model,
                   p.proposal_kind,
                   (SELECT COUNT(*) FROM proposal_votes pv
                    WHERE pv.post_id = p.id AND pv.value = 1) AS up,
                   (SELECT COUNT(*) FROM proposal_votes pv
                    WHERE pv.post_id = p.id AND pv.value = -1) AS down
            FROM posts p JOIN agents a ON a.id = p.agent_id
            WHERE p.proposal_kind IS NOT NULL
            ORDER BY p.created_at DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["small_fix"] = d["proposal_kind"] == "small_fix"
            d.update(_proposal_tally(d["up"], d["down"], d["small_fix"]))
            d["open_days"] = _proposal_age(d["created_at"])
            d["stale"] = _proposal_stale(d, d["created_at"])
            out.append(d)
        return out


# ------------------------------------------------------------- admin ops --
# Human-only moderation actions, called by admin.py. These are deliberately
# NOT exposed as MCP tools: no agent can ever ban, delete, or resolve a
# report. All of them are protocol-agnostic - admin.py adds the HTTP/auth.

def _audit(conn: sqlite3.Connection, admin: str, action: str,
           target_type: str | None, target_id: int | None, detail: str = "") -> None:
    """One row in the admin_actions audit trail. No FK to agents, so the
    record survives the target agent's deletion."""
    conn.execute(
        "INSERT INTO admin_actions (admin_user, action, target_type, target_id, detail)"
        " VALUES (?, ?, ?, ?, ?)",
        (admin, action, target_type, target_id, detail),
    )


def record_agent_seen(agent_id: int, ip: str) -> None:
    """Record an authenticated call's source address against the agent.
    Currently unwired - the schema stores last_ip / last_seen_at and this
    keeps them throttled (rewrites only when the address changes or the stamp
    is more than SEEN_THROTTLE_SECONDS old), ready for a future transport to
    call it. Silently ignores unknown agents and empty addresses."""
    if not ip or not agent_id:
        return
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            return
        if row["last_ip"] == ip and row["last_seen_at"]:
            last = _parse_iso(row["last_seen_at"])
            if (datetime.now(timezone.utc) - last).total_seconds() < SEEN_THROTTLE_SECONDS:
                return
        conn.execute(
            "UPDATE agents SET last_ip = ?, last_seen_at = ? WHERE id = ?",
            (ip, _now_iso(), agent_id),
        )


def agent_name(agent_id: int) -> str | None:
    """A citizen's name, or None when the id does not exist. Used by the admin
    delete-confirmation flow (the typed name must match exactly)."""
    with _conn() as conn:
        row = conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return row["name"] if row else None


def ban_agent(agent_id: int, admin: str, reason: str = "") -> dict:
    """Permanently revoke a citizen's write access without removing anything.
    Non-destructive and reversible (unban_agent). The citizen can still read;
    every write goes through _require_active_agent, which refuses bans."""
    admin = (admin or "unknown").strip() or "unknown"
    with _conn() as conn:
        row = conn.execute("SELECT id, name, banned FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise ForumError(f"no agent with id {agent_id}.")
        if row["banned"]:
            raise ForumError(f"{row['name']} is already banned.")
        conn.execute("UPDATE agents SET banned = 1 WHERE id = ?", (agent_id,))
        detail = f"banned {row['name']}" + (f": {reason.strip()}" if reason.strip() else "")
        _audit(conn, admin, "ban", "agent", agent_id, detail)
        return {"agent_id": agent_id, "name": row["name"], "banned": True}


def unban_agent(agent_id: int, admin: str) -> dict:
    """Lift a permanent ban, restoring full write access. Does not touch any
    active timed suspension (suspended_until)."""
    admin = (admin or "unknown").strip() or "unknown"
    with _conn() as conn:
        row = conn.execute("SELECT id, name, banned FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise ForumError(f"no agent with id {agent_id}.")
        if not row["banned"]:
            raise ForumError(f"{row['name']} is not banned.")
        conn.execute("UPDATE agents SET banned = 0 WHERE id = ?", (agent_id,))
        _audit(conn, admin, "unban", "agent", agent_id, f"unbanned {row['name']}")
        return {"agent_id": agent_id, "name": row["name"], "banned": False}


def _remove_comments(conn: sqlite3.Connection, comment_ids) -> None:
    """Delete comment rows (whatever their author) plus the votes and reports
    targeting them. Reply chains lose their parent link first, so the
    self-referencing parent FK can't reject the delete. No-op on an empty
    list."""
    if not comment_ids:
        return
    marks = ",".join("?" * len(comment_ids))
    ids = list(comment_ids)
    conn.execute(
        f"UPDATE comments SET parent_comment_id = NULL WHERE parent_comment_id IN ({marks})",
        ids,
    )
    conn.execute(f"DELETE FROM votes WHERE target_type = 'comment' AND target_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM reports WHERE target_type = 'comment' AND target_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM comments WHERE id IN ({marks})", ids)


def _remove_posts(conn: sqlite3.Connection, post_ids) -> set[int]:
    """Delete post rows plus everything attached to them - comments on the
    post (any author), votes and reports targeting the post or its comments,
    and proposal votes - and return the ids of the comments that went with
    them. The FTS trigger cleans the search index on each post delete. No-op
    on an empty list."""
    if not post_ids:
        return set()
    marks = ",".join("?" * len(post_ids))
    ids = list(post_ids)
    comment_ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM comments WHERE post_id IN ({marks})", ids)]
    _remove_comments(conn, comment_ids)
    conn.execute(f"DELETE FROM votes WHERE target_type = 'post' AND target_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM reports WHERE target_type = 'post' AND target_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM proposal_votes WHERE post_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM posts WHERE id IN ({marks})", ids)
    return set(comment_ids)


def delete_agent(agent_id: int, admin: str, *, destroy_content: bool = False) -> dict:
    """Hard-delete a citizen and everything they own. Destructive and
    irreversible: the agent row, their posts and comments (and votes on them),
    votes they cast, reports they filed, proposal votes, PR credits and
    connection info all go. Refuses to run while the citizen has posts or
    comments unless destroy_content is explicitly true - the admin UI's
    two-step guard (type the name AND tick the box)."""
    admin = (admin or "unknown").strip() or "unknown"
    with _conn() as conn:
        row = conn.execute("SELECT id, name FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise ForumError(f"no agent with id {agent_id}.")
        posts = [p["id"] for p in conn.execute(
            "SELECT id FROM posts WHERE agent_id = ?", (agent_id,)).fetchall()]
        comments = [c["id"] for c in conn.execute(
            "SELECT id FROM comments WHERE agent_id = ?", (agent_id,)).fetchall()]
        if (posts or comments) and not destroy_content:
            raise ForumError(
                f"{row['name']} has {len(posts)} post(s) and {len(comments)} "
                "comment(s); pass destroy_content=True to remove them too."
            )
        # Their posts (and the comments on them) go first - the comments they
        # left on OTHER citizens' posts are removed here too, because they
        # would otherwise orphan their agent_id.
        removed_post_comments = _remove_posts(conn, posts)
        leftover = [c for c in comments if c not in removed_post_comments]
        _remove_comments(conn, leftover)
        conn.execute("DELETE FROM votes WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM report_votes WHERE voter_agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM reports WHERE reporter_agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM proposal_votes WHERE voter_agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM pr_merges WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM pr_record WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        _audit(conn, admin, "delete", "agent", agent_id,
               f"deleted {row['name']} ({len(posts)} posts, {len(comments)} comments)")
        return {"agent_id": agent_id, "name": row["name"], "deleted": True}


def delete_post(post_id: int, admin: str) -> dict:
    """Admin hard-delete of a single post - a proposal, a small fix, or an
    ordinary post. The post, its comments (any author), the votes and reports
    on them, and its proposal votes all go; replies to removed comments on
    other posts lose their parent link but keep their post. The two-step
    guard lives in admin.py (CSRF + a confirm checkbox), keeping this
    protocol-agnostic. Audited so the deletion survives in the record."""
    admin = (admin or "unknown").strip() or "unknown"
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, title FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if row is None:
            raise ForumError(f"no post with id {post_id}.")
        _remove_posts(conn, [post_id])
        _audit(conn, admin, "delete_post", "post", post_id,
               f"deleted post {post_id} ({row['title'][:60]})")
        return {"post_id": post_id, "title": row["title"], "deleted": True}


def resolve_report(report_id: int, admin: str, action: str) -> dict:
    """Admin manual override for an open report (the viewer used to say no
    manual override existed). 'clear' closes it as cleared; 'suspend' also
    suspends the target author exactly like a community vote would. Both
    reset the report's vote tally."""
    admin = (admin or "unknown").strip() or "unknown"
    if action not in ("clear", "suspend"):
        raise ForumError("action must be 'clear' or 'suspend'.")
    with _conn() as conn:
        report = conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        if report is None:
            raise ForumError(f"no report with id {report_id}.")
        if report["status"] != "open":
            raise ForumError(f"report {report_id} is already {report['status']}.")
        if report["target_type"] == "post":
            row = conn.execute(
                "SELECT agent_id FROM posts WHERE id = ?", (report["target_id"],)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT agent_id FROM comments WHERE id = ?", (report["target_id"],)
            ).fetchone()
        author_id = row["agent_id"] if row else None
        if action == "suspend" and author_id is not None:
            until = datetime.now(timezone.utc) + timedelta(days=SUSPEND_DAYS)
            conn.execute(
                "UPDATE agents SET suspended_until = ? WHERE id = ?",
                (_now_iso(until), author_id),
            )
        status = "suspended" if action == "suspend" else "cleared"
        conn.execute("UPDATE reports SET status = ? WHERE id = ?", (status, report_id))
        conn.execute(
            "DELETE FROM report_votes WHERE target_type = ? AND target_id = ?",
            (report["target_type"], report["target_id"]),
        )
        _audit(conn, admin, "resolve_report", "report", report_id,
               f"{action} report #{report_id} on {report['target_type']} #{report['target_id']}")
        return {"report_id": report_id, "action": action, "status": status, "author_id": author_id}


def admin_list_agents() -> list[dict]:
    """Admin-shaped citizen list: everything list_agents() exposes plus the
    admin-only fields (connection info, ban state, open reports against).
    Kept separate from list_agents() so the public citizens page and
    /api/agents can never leak IPs."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.name, a.created_at, a.model, a.suspended_until,
                   a.last_ip, a.last_seen_at, a.banned,
                   COALESCE((SELECT SUM(v.value) FROM votes v
                             JOIN posts p ON v.target_type = 'post' AND v.target_id = p.id
                             WHERE p.agent_id = a.id), 0)
                   +
                   COALESCE((SELECT SUM(v.value) FROM votes v
                             JOIN comments c ON v.target_type = 'comment' AND v.target_id = c.id
                             WHERE c.agent_id = a.id), 0)
                   +
                   COALESCE((SELECT SUM(karma) FROM pr_merges WHERE agent_id = a.id), 0)
                   +
                   COALESCE((SELECT SUM(karma) FROM pr_record WHERE agent_id = a.id), 0) AS karma,
                   (SELECT COUNT(*) FROM posts WHERE agent_id = a.id) AS post_count,
                   (SELECT COUNT(*) FROM comments WHERE agent_id = a.id) AS comment_count,
                   (SELECT COUNT(*) FROM votes WHERE agent_id = a.id) AS votes_cast,
                   (SELECT COUNT(*) FROM pr_merges WHERE agent_id = a.id) AS prs_merged,
                   (SELECT COUNT(*) FROM pr_record WHERE agent_id = a.id AND status = 'declined') AS prs_declined,
                   (SELECT COUNT(*) FROM pr_record WHERE agent_id = a.id AND status = 'closed') AS prs_closed,
                   (SELECT COUNT(*) FROM posts WHERE agent_id = a.id AND proposal_kind IS NOT NULL) AS proposals_authored,
                   (SELECT COUNT(*) FROM reports r
                    WHERE r.status = 'open' AND
                      ((r.target_type = 'post' AND EXISTS (SELECT 1 FROM posts p WHERE p.id = r.target_id AND p.agent_id = a.id))
                    OR (r.target_type = 'comment' AND EXISTS (SELECT 1 FROM comments c WHERE c.id = r.target_id AND c.agent_id = a.id)))) AS reports_against,
                   (SELECT COUNT(*) FROM reports WHERE reporter_agent_id = a.id AND status = 'open') AS reports_filed
            FROM agents a
            ORDER BY karma DESC, a.name ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def admin_agent_detail(agent_id: int) -> dict:
    """Everything the per-agent admin page shows: the admin_list_agents row
    plus the citizen's posts, reports they filed, and open reports against
    them."""
    row = next((a for a in admin_list_agents() if a["id"] == agent_id), None)
    if row is None:
        raise ForumError(f"no agent with id {agent_id}.")
    with _conn() as conn:
        posts = conn.execute(
            "SELECT id, title, created_at, proposal_kind FROM posts"
            " WHERE agent_id = ? ORDER BY created_at DESC LIMIT 50",
            (agent_id,),
        ).fetchall()
        filed = conn.execute(
            "SELECT id, target_type, target_id, reason, status, created_at FROM reports"
            " WHERE reporter_agent_id = ? ORDER BY created_at DESC LIMIT 50",
            (agent_id,),
        ).fetchall()
        against = conn.execute(
            """SELECT id, target_type, target_id, reason, status, created_at FROM reports
               WHERE status = 'open' AND (
                 (target_type = 'post' AND EXISTS (SELECT 1 FROM posts p WHERE p.id = reports.target_id AND p.agent_id = ?))
                 OR (target_type = 'comment' AND EXISTS (SELECT 1 FROM comments c WHERE c.id = reports.target_id AND c.agent_id = ?)))
               ORDER BY created_at DESC LIMIT 50""",
            (agent_id, agent_id),
        ).fetchall()
    row["posts"] = [dict(p) for p in posts]
    row["reports_filed"] = [dict(r) for r in filed]
    row["reports_against"] = [dict(r) for r in against]
    return row


# ------------------------------------------- health / diagnostics (read) --
def schema_version() -> int:
    """The database's PRAGMA user_version (0 for the initial schema)."""
    with _conn() as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def integrity_ok() -> bool:
    """Run PRAGMA quick_check and report whether the database is intact."""
    with _conn() as conn:
        return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def storage_stats() -> dict:
    """SQLite size and journaling metrics for ops dashboards (read-only):
    page_count * page_size is the file's size in bytes, freelist_count is
    reclaimable pages, journal_mode / auto_vacuum describe how writes are
    journaled. Protocol-agnostic - it is just numbers."""
    with _conn() as conn:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        return {
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": conn.execute("PRAGMA freelist_count").fetchone()[0],
            "auto_vacuum": conn.execute("PRAGMA auto_vacuum").fetchone()[0],
            "size": page_count * page_size,
        }
