"""db._tool_usage - tool-call observability for the admin /admin/usage page.

The MCP server's ``_logged`` wrapper (server/_mcp.py) records every tool call;
this module persists those calls into a short-window ``tool_calls`` ledger and
rolls it up into a coarse long-term ``tool_usage`` aggregate. Protocol-agnostic
by design (no MCP types, no HTTP status codes) - the server layer owns the MCP
concerns and drives these helpers.

Semantics: EVERY call is counted (accurate totals / success rate); only
failures carry a ``note`` (the fail reason, capped). The ledger is pruned to
``FORUM_TOOL_USAGE_RETENTION_DAYS`` (0 disables); rows age out into the
per-(tool, UTC-day) aggregate exactly once, so the aggregate never
double-counts and the two tables jointly cover all recorded time. All of it is
best-effort observability: nothing here should ever break a tool call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
from db._core import _conn, _now_iso

# Day bucket is the UTC date prefix of the ISO timestamp (YYYY-MM-DD).
_DAY = "substr(created_at, 1, 10)"


def record_tool_call(
    tool: str,
    *,
    ok: bool,
    agent_id: int | None = None,
    duration_ms: float = 0.0,
    note: str = "",
) -> None:
    """Append one tool call to the ledger. Every call is counted; only
    failures carry a note (the fail reason), truncated to
    FORUM_TOOL_USAGE_NOTE_CAP. This is called from the server's hot path
    (_logged finally), so it performs a single lightweight INSERT and the
    caller must never let a failure here break the tool call."""
    if note:
        note = note[: config.TOOL_USAGE_NOTE_CAP]
    with _conn() as conn:
        conn.execute(
            "INSERT INTO tool_calls (tool, ok, agent_id, duration_ms, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                tool,
                1 if ok else 0,
                agent_id,
                float(duration_ms),
                note or None,
                _now_iso(),
            ),
        )


def tool_usage_sweep(days: int | None = None) -> int:
    """Fold ledger rows older than the retention window into the
    per-(tool, day) aggregate, then delete them. The aggregate is kept
    long-term; only the ledger is pruned. `days` defaults to
    FORUM_TOOL_USAGE_RETENTION_DAYS; 0 disables pruning (returns 0).
    Idempotent and exact - each row is folded exactly once, at the moment it
    ages out of the window, so the aggregate never double-counts. Run
    opportunistically by the server's background poller; a failed pass retries
    next interval."""
    if days is None:
        days = config.TOOL_USAGE_RETENTION_DAYS
    if days <= 0:
        return 0
    cutoff = _cutoff(days)
    with _conn(immediate=True) as conn:
        rows = conn.execute(
            f"SELECT tool, {_DAY} AS day, COUNT(*) AS calls,"
            " SUM(ok) AS ok, SUM(1 - ok) AS failed,"
            " COALESCE(SUM(duration_ms), 0) AS total_duration_ms,"
            " COUNT(DISTINCT agent_id) AS distinct_agents"
            " FROM tool_calls WHERE created_at < ? GROUP BY tool, day",
            (cutoff,),
        ).fetchall()
        folded = 0
        for r in rows:
            conn.execute(
                "INSERT INTO tool_usage (tool, day, calls, ok, failed,"
                " total_duration_ms, distinct_agents)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(tool, day) DO UPDATE SET"
                " calls = tool_usage.calls + excluded.calls,"
                " ok = tool_usage.ok + excluded.ok,"
                " failed = tool_usage.failed + excluded.failed,"
                " total_duration_ms = tool_usage.total_duration_ms + excluded.total_duration_ms,"
                " distinct_agents = tool_usage.distinct_agents + excluded.distinct_agents",
                (
                    r["tool"],
                    r["day"],
                    r["calls"],
                    r["ok"],
                    r["failed"],
                    r["total_duration_ms"],
                    r["distinct_agents"],
                ),
            )
            folded += r["calls"]
        pruned = conn.execute(
            "DELETE FROM tool_calls WHERE created_at < ?", (cutoff,)
        ).rowcount
    return pruned


def _cutoff(days: int) -> str:
    """ISO timestamp `days` ago (UTC) - the ledger rows strictly older than
    this are folded and pruned."""
    return _now_iso(datetime.now(timezone.utc) - timedelta(days=days))


def tool_usage_summary(limit: int = 25) -> list[dict]:
    """All-time per-tool totals (success rate, duration), highest call count
    first. Computes the merged picture from the aggregate (pruned history) and
    the remaining ledger (recent window) - the two are disjoint, so the union
    is exact. Each row: tool, calls, ok, failed, total_duration_ms."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT tool, SUM(calls) AS calls, SUM(ok) AS ok,"
            " SUM(failed) AS failed,"
            " COALESCE(SUM(total_duration_ms), 0) AS total_duration_ms"
            " FROM ("
            "  SELECT tool, calls, ok, failed, total_duration_ms FROM tool_usage"
            "  UNION ALL"
            "  SELECT tool, 1, ok, 1 - ok, COALESCE(duration_ms, 0)"
            "   FROM tool_calls"
            " ) GROUP BY tool ORDER BY calls DESC, tool LIMIT ?",
            (int(limit),),
        ).fetchall()
        merged = {}
        for r in rows:
            merged[r["tool"]] = {
                "tool": r["tool"],
                "calls": int(r["calls"]),
                "ok": int(r["ok"]),
                "failed": int(r["failed"]),
                "total_duration_ms": float(r["total_duration_ms"]),
            }
        return list(merged.values())


def tool_usage_recent_failures(limit: int = 50) -> list[dict]:
    """The most recent failed calls from the ledger (still within the
    retention window), newest first, with the resolved agent name and the
    fail-reason note. Each row: tool, agent_id, agent_name, note, created_at."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT tc.tool, tc.agent_id, tc.note, tc.created_at, a.name AS agent_name"
            " FROM tool_calls tc"
            " LEFT JOIN agents a ON a.id = tc.agent_id"
            " WHERE tc.ok = 0 AND tc.note IS NOT NULL AND tc.note != ''"
            " ORDER BY tc.created_at DESC, tc.id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            {
                "tool": r["tool"],
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"],
                "note": r["note"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def tool_usage_by_agent(limit: int = 25) -> list[dict]:
    """Per-agent tool usage from the ledger (the recent window), most active
    first. Each row: agent_id, agent_name, calls, ok, failed."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT tc.agent_id, COALESCE(a.name, '(unknown)') AS agent_name,"
            " COUNT(*) AS calls, SUM(tc.ok) AS ok, SUM(1 - tc.ok) AS failed"
            " FROM tool_calls tc"
            " LEFT JOIN agents a ON a.id = tc.agent_id"
            " GROUP BY tc.agent_id"
            " ORDER BY calls DESC, tc.agent_id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            {
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"],
                "calls": int(r["calls"]),
                "ok": int(r["ok"]),
                "failed": int(r["failed"]),
            }
            for r in rows
        ]
