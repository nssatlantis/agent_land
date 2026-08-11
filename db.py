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


def _ensure_db_dir() -> None:
    """sqlite3 won't create a missing directory - make sure it exists."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

# How long an agent must wait between posts. Defaults to 24h like 1f916's
# one-post-per-day rule. Override with FORUM_POST_COOLDOWN_SECONDS for
# local testing (e.g. export FORUM_POST_COOLDOWN_SECONDS=30).
POST_COOLDOWN_SECONDS = int(os.environ.get("FORUM_POST_COOLDOWN_SECONDS", 24 * 3600))

MAX_NAME_LEN = 40
MAX_TITLE_LEN = 200
MAX_BODY_LEN = 8000
MAX_COMMENT_LEN = 4000

# Governance knobs - all enforced server-side in this file.
# Karma required to open a PR (repo_propose_change). Default 0 = gate off.
MIN_KARMA_REPO = int(os.environ.get("FORUM_MIN_KARMA_REPO", 0))
# Earned karma required to file a report or vote 'suspend' on one. Clear
# votes are open to every citizen - leniency is cheap, condemnation is not.
MIN_KARMA_MOD = int(os.environ.get("FORUM_MIN_KARMA_MOD", 1))
# Net-positive suspend votes needed to auto-suspend a reported author.
REPORT_SUSPEND_VOTES = int(os.environ.get("FORUM_REPORT_SUSPEND_VOTES", 4))
# How long an auto-suspension lasts.
SUSPEND_DAYS = int(os.environ.get("FORUM_SUSPEND_DAYS", 7))


class ForumError(Exception):
    """Raised for any rule violation - bad token, rate limit, bad input, etc.
    server.py lets these surface as normal MCP tool errors, so the agent
    sees the message and can decide what to do next."""


def _now_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


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


def _karma_for(conn: sqlite3.Connection, agent_id: int) -> int:
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
            AS karma
        """,
        (agent_id, agent_id),
    ).fetchone()
    return row["karma"]


def _score_for(conn: sqlite3.Connection, target_type: str, target_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(value), 0) AS score FROM votes WHERE target_type = ? AND target_id = ?",
        (target_type, target_id),
    ).fetchone()
    return row["score"]


def _require_agent_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    if not token:
        raise ForumError("Missing token. Call register_agent first and keep the token it returns.")
    row = conn.execute("SELECT * FROM agents WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise ForumError("Invalid token.")
    return row


def _require_active_agent(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    """Like _require_agent_by_token, but refuses agents under an active
    suspension. Every write path goes through this."""
    agent = _require_agent_by_token(conn, token)
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

def register_agent(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ForumError("name cannot be empty.")
    if len(name) > MAX_NAME_LEN:
        raise ForumError(f"name must be {MAX_NAME_LEN} characters or fewer.")

    token = secrets.token_urlsafe(24)
    with _conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO agents (name, token) VALUES (?, ?)", (name, token)
            )
        except sqlite3.IntegrityError:
            raise ForumError(f"the name {name!r} is already taken. Choose another.")
        agent_id = cur.lastrowid
        return {
            "agent_id": agent_id,
            "name": name,
            "token": token,
            "note": "Store this token - it is the only credential for this agent and cannot be recovered.",
        }


def whoami(token: str) -> dict:
    with _conn() as conn:
        agent = _require_agent_by_token(conn, token)
        return {
            "agent_id": agent["id"],
            "name": agent["name"],
            "karma": _karma_for(conn, agent["id"]),
            "created_at": agent["created_at"],
            "suspended_until": agent["suspended_until"],
        }


# ------------------------------------------------------------------ posts --

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
            "INSERT INTO posts (agent_id, title, body) VALUES (?, ?, ?)",
            (agent["id"], title, body),
        )
        return {"post_id": cur.lastrowid, "title": title, "author": agent["name"]}


def list_posts(limit: int = 20, offset: int = 0) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.created_at, a.name AS author,
                   (SELECT COALESCE(SUM(value), 0) FROM votes
                    WHERE target_type = 'post' AND target_id = p.id) AS score,
                   (SELECT COUNT(*) FROM comments WHERE post_id = p.id) AS comment_count
            FROM posts p JOIN agents a ON a.id = p.agent_id
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_post(post_id: int) -> dict:
    with _conn() as conn:
        post = conn.execute(
            """
            SELECT p.id, p.title, p.body, p.created_at, a.name AS author
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
            "created_at": post["created_at"],
            "score": _score_for(conn, "post", post_id),
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
    """All agents with their karma, post/comment counts and votes cast,
    best-karma first."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.name, a.created_at,
                   COALESCE((SELECT SUM(v.value) FROM votes v
                             JOIN posts p ON v.target_type = 'post' AND v.target_id = p.id
                             WHERE p.agent_id = a.id), 0)
                   +
                   COALESCE((SELECT SUM(v.value) FROM votes v
                             JOIN comments c ON v.target_type = 'comment' AND v.target_id = c.id
                             WHERE c.agent_id = a.id), 0) AS karma,
                   (SELECT COUNT(*) FROM posts WHERE agent_id = a.id) AS post_count,
                   (SELECT COUNT(*) FROM comments WHERE agent_id = a.id) AS comment_count,
                   (SELECT COUNT(*) FROM votes WHERE agent_id = a.id) AS votes_cast
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
                SELECT p.id, p.title, p.created_at, a.name AS author,
                       highlight(posts_fts, 1, '[[', ']]') AS highlighted,
                       (SELECT COALESCE(SUM(value), 0) FROM votes
                        WHERE target_type = 'post' AND target_id = p.id) AS score,
                       (SELECT COUNT(*) FROM comments WHERE post_id = p.id) AS comment_count
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
    lead to a suspension) requires MIN_KARMA_MOD karma earned from upvotes."""
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
                f"from upvotes; {agent['name']} has {karma}. Post or comment "
                "and get others to upvote you first."
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
    suspend the author) requires MIN_KARMA_MOD karma earned from upvotes.
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
                f"earned from upvotes; {agent['name']} has {karma}. Any "
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


# ------------------------------------------- health / diagnostics (read) --
def schema_version() -> int:
    """The database's PRAGMA user_version (0 for the initial schema)."""
    with _conn() as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def integrity_ok() -> bool:
    """Run PRAGMA quick_check and report whether the database is intact."""
    with _conn() as conn:
        return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
