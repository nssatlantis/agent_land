"""db._pr_rows - the DB-persisted PR cache.

Closed-PR rows (the /prs closed tab, repo_list_prs state='closed'/'all') and
per-PR header ETags (the repo_get_pr revalidation path) are served from
SQLite instead of re-reading GitHub's closed-pulls listing on every hit.
The cache is enrichment, never a source of truth: readers fall back to live
GitHub when it is unpopulated (no rows AND no completed backfill), and the
outcome poller keeps it warm from the rows it already ingests.

Protocol-agnostic by design (no MCP types, no HTTP status codes): the
github/ package owns HTTP concerns and drives these helpers.
"""

from __future__ import annotations

import json
import sqlite3

from db._core import _conn, _now_iso

_BACKFILL_KEY = "pr_rows_backfill_at"

_PR_COLS = (
    "pr_number, title, body, head, head_sha, base, author, state, created_at,"
    " updated_at, merged_at, closed_at, html_url, labels_json,"
    " citizen_agent_id, citizen_name, etag, verified_at"
)


def _outcome(state: str | None, merged_at: str | None, labels: list) -> str:
    """Mirror of github._reads._pr_outcome - state order is load-bearing:
    open wins over anything, then merged_at, then the declined label."""
    if state != "closed":
        return "open"
    if merged_at:
        return "merged"
    if any(str(label or "").startswith("declined") for label in labels):
        return "declined"
    return "closed"


def _row_to_dict(row: sqlite3.Row) -> dict:
    labels = json.loads(row["labels_json"] or "[]")
    citizen_agent_id = row["citizen_agent_id"]
    return {
        "number": int(row["pr_number"]),
        "title": row["title"],
        "body": row["body"],
        "head": row["head"],
        "head_sha": row["head_sha"],
        "base": row["base"],
        "author": row["author"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "state": row["state"],
        "merged_at": row["merged_at"],
        "closed_at": row["closed_at"],
        "html_url": row["html_url"],
        "labels": labels,
        "citizen": (
            {"agent_id": int(citizen_agent_id), "name": row["citizen_name"]}
            if citizen_agent_id is not None
            else None
        ),
        "etag": row["etag"],
        "verified_at": row["verified_at"],
        "outcome": _outcome(row["state"], row["merged_at"], labels),
    }


def pr_row(pr_number: int, conn: sqlite3.Connection | None = None) -> dict | None:
    """The cached row for one PR (labels/citizen/outcome derived), or None
    when the cache has not stored it yet. The revalidation seam reads it for
    its ETag and to build the 'not modified' synthetic header."""

    def _read(c: sqlite3.Connection) -> dict | None:
        row = c.execute(
            "SELECT " + _PR_COLS + " FROM pr_rows WHERE pr_number = ?",
            (int(pr_number),),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    if conn is not None:
        return _read(conn)
    with _conn() as c:
        return _read(c)


def list_pr_rows(state: str = "closed", since: str | None = None) -> list[dict] | None:
    """Closed-PR cache rows, newest first (updated_at desc, number tiebreak).

    Returns None - the live-GitHub fallback signal - when the cache is
    unpopulated (zero rows AND the backfill watermark was never set), so a
    fresh database never reads 'no PRs'. `state` is accepted for symmetry;
    only closed rows are stored and open composition stays live. `since`
    (ISO-8601 UTC) filters by updated_at, mirroring the live closed path."""
    with _conn() as c:
        count = c.execute("SELECT COUNT(*) FROM pr_rows").fetchone()[0]
        watermark = c.execute(
            "SELECT value FROM pr_cache_meta WHERE key = ?", (_BACKFILL_KEY,)
        ).fetchone()
        if count == 0 and watermark is None:
            return None
        sql = "SELECT " + _PR_COLS + " FROM pr_rows"
        params: list = []
        if since is not None:
            sql += " WHERE updated_at IS NOT NULL AND updated_at >= ?"
            params.append(since)
        sql += " ORDER BY COALESCE(updated_at, created_at, '') DESC, pr_number DESC"
        return [_row_to_dict(r) for r in c.execute(sql, params)]


def pr_rows_upsert(conn: sqlite3.Connection, row: dict) -> None:
    """Store/refresh one feed-shaped closed-PR row (the output of
    github._closed_row_from_raw / the outcome feed). Citizen columns are
    overwritten here - the feed knows the trailer - but etag only when the
    incoming row carries one (the poller never sees headers). Defensive
    .get(): test mocks feed stripped rows in.

    Not idempotency-critical in the poller sense - INSERT OR IGNORE would
    silently keep a stale copy after a PR is reopened and reclosed - so
    every content field wins on conflict and verified_at refreshes.
    """
    now = _now_iso()
    labels = [str(label or "") for label in (row.get("labels") or [])]
    citizen = row.get("citizen") or {}
    conn.execute(
        "INSERT INTO pr_rows ("
        + _PR_COLS
        + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(pr_number) DO UPDATE SET"
        " title=excluded.title, body=excluded.body, head=excluded.head,"
        " head_sha=excluded.head_sha, base=excluded.base, author=excluded.author,"
        " state=excluded.state,"
        " created_at=excluded.created_at, updated_at=excluded.updated_at,"
        " merged_at=excluded.merged_at, closed_at=excluded.closed_at,"
        " html_url=excluded.html_url, labels_json=excluded.labels_json,"
        " citizen_agent_id=excluded.citizen_agent_id,"
        " citizen_name=excluded.citizen_name,"
        " etag=COALESCE(excluded.etag, pr_rows.etag),"
        " verified_at=excluded.verified_at",
        (
            int(row["number"]),
            row.get("title") or "",
            row.get("body") or "",
            row.get("head") or "",
            row.get("head_sha") or "",
            row.get("base") or "",
            row.get("author"),
            row.get("state") or "closed",
            row.get("created_at"),
            row.get("updated_at"),
            row.get("merged_at"),
            row.get("closed_at"),
            row.get("html_url") or "",
            json.dumps(labels, separators=(",", ":")),
            citizen.get("agent_id"),
            citizen.get("name"),
            row.get("etag"),
            now,
        ),
    )


def pr_rows_upsert_from_raw(
    conn: sqlite3.Connection,
    payload: dict,
    etag: str | None,
) -> None:
    """Store/refresh one PR from a raw GitHub pull header (the revalidation
    path). Content + etag are overwritten; the citizen columns are LEFT
    UNTOUCHED - only the outcome poller knows the trailer, and overwriting
    them here with None would erase hard-won attribution."""
    now = _now_iso()
    head = payload.get("head") or {}
    base = payload.get("base") or {}
    user = payload.get("user") or {}
    labels = [label.get("name", "") for label in (payload.get("labels") or [])]
    state = payload.get("state") or "closed"
    if state != "closed":
        # A reopened PR is no longer a closed-PR cache row - drop it so the
        # closed listing and revalidation seam never serve an open PR from
        # the cache (the caller reads the fresh payload live regardless).
        conn.execute(
            "DELETE FROM pr_rows WHERE pr_number = ?", (int(payload["number"]),)
        )
        return
    conn.execute(
        "INSERT INTO pr_rows ("
        + _PR_COLS
        + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(pr_number) DO UPDATE SET"
        " title=excluded.title, body=excluded.body, head=excluded.head,"
        " head_sha=excluded.head_sha, base=excluded.base, author=excluded.author,"
        " state=excluded.state,"
        " created_at=excluded.created_at, updated_at=excluded.updated_at,"
        " merged_at=excluded.merged_at, closed_at=excluded.closed_at,"
        " html_url=excluded.html_url, labels_json=excluded.labels_json,"
        " etag=excluded.etag, verified_at=excluded.verified_at",
        (
            int(payload["number"]),
            payload.get("title") or "",
            payload.get("body") or "",
            head.get("ref") or head.get("label") or "",
            head.get("sha") or "",
            base.get("ref") or base.get("label") or "",
            user.get("login"),
            state,
            payload.get("created_at"),
            payload.get("updated_at"),
            payload.get("merged_at"),
            payload.get("closed_at"),
            payload.get("html_url") or "",
            json.dumps(labels, separators=(",", ":")),
            None,
            None,
            etag,
            now,
        ),
    )


def pr_rows_watermark(conn: sqlite3.Connection | None = None) -> str | None:
    """The completed backfill's timestamp, or None when it never completed."""

    def _read(c: sqlite3.Connection) -> str | None:
        row = c.execute(
            "SELECT value FROM pr_cache_meta WHERE key = ?", (_BACKFILL_KEY,)
        ).fetchone()
        return row[0] if row is not None else None

    if conn is not None:
        return _read(conn)
    with _conn() as c:
        return _read(c)


def pr_rows_set_watermark(conn: sqlite3.Connection, value: str | None = None) -> None:
    """Stamp the completed-backfill watermark (defaults to now). Absent +
    zero rows is the 'unpopulated' signal that forces readers back to
    live GitHub; the watermark disambiguates 'never backfilled' from a
    genuinely empty repo. Stamping is a one-way upsert - there is no
    clear path, and none is needed: a failed backfill simply never
    stamps."""
    now = _now_iso() if value is None else value
    conn.execute(
        "INSERT INTO pr_cache_meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_BACKFILL_KEY, now),
    )
