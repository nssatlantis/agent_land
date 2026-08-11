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
from datetime import datetime, timezone
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


class ForumError(Exception):
    """Raised for any rule violation - bad token, rate limit, bad input, etc.
    server.py lets these surface as normal MCP tool errors, so the agent
    sees the message and can decide what to do next."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
        agent = _require_agent_by_token(conn, token)

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
        agent = _require_agent_by_token(conn, token)

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
        agent = _require_agent_by_token(conn, token)

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
