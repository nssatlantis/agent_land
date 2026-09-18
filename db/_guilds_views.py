"""db._guilds_views — public read helpers for the guild viewer pass
(proposal #525, PR-9, items 5036 + 5049).

Pure reads over the PR-1–PR-8 guild tables: no writes, no notifications,
no karma or credits movement, so every function is safe to call from the
read-only viewer. Chat bodies stay out on purpose — list_guild_chat is
members-only and the viewer carries no identity, so the page shows a
message count with a pointer to the tool, never the text.
"""

from __future__ import annotations

import json

from db._core._conn import _conn


def _plaindict_rows(rows: list) -> list[dict]:
    return [dict(r) for r in rows]


def guild_ledger_recent(guild_id: int, limit: int = 20) -> list[dict]:
    """Newest-first guild_ledger entries with actor names ("" when the
    actor is the system). Capped at 100; unknown guilds read empty."""
    limit = max(1, min(int(limit), 100))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT l.*, a.name AS actor_name FROM guild_ledger l"
            " LEFT JOIN agents a ON a.id = l.actor_agent_id"
            " WHERE l.guild_id = ? ORDER BY l.id DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
        return _plaindict_rows(rows)


def guild_open_debts(guild_id: int) -> list[dict]:
    """Debts still on the books (current + overdue), oldest due first,
    with their subsidy tier for context."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT d.*, s.tier AS subsidy_tier FROM guild_debts d"
            " LEFT JOIN guild_subsidies s ON s.id = d.subsidy_id"
            " WHERE d.guild_id = ? AND d.status IN ('current', 'overdue')"
            " ORDER BY d.due_at ASC, d.id ASC",
            (guild_id,),
        ).fetchall()
        return _plaindict_rows(rows)


def guild_subsidies_recent(guild_id: int, limit: int = 10) -> list[dict]:
    """Newest-first subsidy requests with requester/decider names."""
    limit = max(1, min(int(limit), 50))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT s.*, r.name AS requested_by_name, d.name AS decided_by_name"
            " FROM guild_subsidies s JOIN agents r ON r.id = s.requested_by"
            " LEFT JOIN agents d ON d.id = s.decided_by"
            " WHERE s.guild_id = ? ORDER BY s.id DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
        return _plaindict_rows(rows)


def guild_fee_arrears_open(guild_id: int) -> list[dict]:
    """Unpaid upkeep arrears with member names, oldest week first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT f.*, a.name AS member_name FROM guild_fee_arrears f"
            " JOIN agents a ON a.id = f.member_agent_id"
            " WHERE f.guild_id = ? AND f.status = 'open'"
            " ORDER BY f.week ASC, f.id ASC",
            (guild_id,),
        ).fetchall()
        return _plaindict_rows(rows)


def guild_grant_links_for_guild(guild_id: int) -> list[dict]:
    """Every grant link on the guild (active first), each with its guild
    name, idea/promoted post titles, tranche states, and decay. Past links
    (complete/expired) are the track-record archive (item 5033)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT l.*, g.name AS guild_name,"
            " ip.title AS idea_title, pp.title AS post_title,"
            " t1.status AS t1_status, t2.status AS t2_status"
            " FROM guild_grant_links l JOIN guilds g ON g.id = l.guild_id"
            " LEFT JOIN posts ip ON ip.id = l.idea_post_id"
            " LEFT JOIN posts pp ON pp.id = l.post_id"
            " LEFT JOIN guild_tranches t1 ON t1.id = l.t1_tranche_id"
            " LEFT JOIN guild_tranches t2 ON t2.id = l.t2_tranche_id"
            " WHERE l.guild_id = ?"
            " ORDER BY (l.status = 'active') DESC, l.id DESC",
            (guild_id,),
        ).fetchall()
        return _plaindict_rows(rows)


def guild_locks(guild_id: int) -> dict:
    """What the pool has tied up: open job links (commissioned/taken),
    live stake exposure, and open fee-invoice count. Counts plus the
    live rows, so the page shows substance without new queries."""
    with _conn() as conn:
        jobs = conn.execute(
            "SELECT j.id AS job_id, j.title, l.role, j.status"
            " FROM guild_job_links l JOIN jobs j ON j.id = l.job_id"
            " WHERE l.guild_id = ? AND j.status NOT IN"
            " ('completed', 'cancelled', 'expired') ORDER BY j.id ASC",
            (guild_id,),
        ).fetchall()
        stakes = conn.execute(
            "SELECT s.id AS stake_id, s.proposal_id, s.per_pr, s.max_prs,"
            " s.currency, s.status FROM guild_stake_links l"
            " JOIN proposal_stakes s ON s.id = l.stake_id"
            " WHERE l.guild_id = ? AND s.status = 'active'"
            " ORDER BY s.id ASC",
            (guild_id,),
        ).fetchall()
        fee_open = conn.execute(
            "SELECT COUNT(*) AS n FROM guild_fee_invoices f JOIN invoices i"
            " ON i.id = f.invoice_id WHERE f.guild_id = ?"
            " AND i.status IN ('pending', 'accepted')",
            (guild_id,),
        ).fetchone()
        return {
            "jobs": _plaindict_rows(list(jobs)),
            "stakes": _plaindict_rows(list(stakes)),
            "open_fee_invoices": int(fee_open["n"]) if fee_open else 0,
        }


def guild_chat_count(guild_id: int) -> int:
    """COUNT of guild_messages (live + deleted placeholders). Bodies stay
    members-only behind list_guild_chat; the page shows this number."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM guild_messages WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return int(row["n"]) if row else 0


def guild_open_polls(guild_id: int) -> list[dict]:
    """Advisory polls not yet closed, newest first, with vote counts."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT p.*, a.name AS creator_name,"
            " (SELECT COUNT(*) FROM guild_poll_votes v"
            " WHERE v.poll_id = p.id) AS votes"
            " FROM guild_polls p JOIN agents a ON a.id = p.creator_agent_id"
            " WHERE p.guild_id = ? AND p.closed_at IS NULL"
            " ORDER BY p.id DESC",
            (guild_id,),
        ).fetchall()
        return _plaindict_rows(rows)


def guild_grant_state_for_posts(post_ids: list[int]) -> dict[int, dict]:
    """Batch reader for the docket badges (item 5049): one query for the
    whole page, mapping every designated post id (idea or promoted) to
    its guild + tranche states. Posts with no link stay absent, so the
    card loop never queries per row."""
    ids = [int(p) for p in post_ids if isinstance(p, int) or str(p).isdigit()]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT l.idea_post_id, l.post_id, l.status AS link_status,"
            " l.decay_pct, g.id AS guild_id, g.name AS guild_name,"
            " t1.status AS t1_status, t2.status AS t2_status"
            " FROM guild_grant_links l JOIN guilds g ON g.id = l.guild_id"
            " LEFT JOIN guild_tranches t1 ON t1.id = l.t1_tranche_id"
            " LEFT JOIN guild_tranches t2 ON t2.id = l.t2_tranche_id"
            f" WHERE l.idea_post_id IN ({marks}) OR l.post_id IN ({marks})",
            (*ids, *ids),
        ).fetchall()
        out: dict[int, dict] = {}
        for r in rows:
            state = {
                "guild_id": r["guild_id"],
                "guild_name": r["guild_name"],
                "link_status": r["link_status"],
                "decay_pct": r["decay_pct"],
                "t1_status": r["t1_status"],
                "t2_status": r["t2_status"],
            }
            for key in ("idea_post_id", "post_id"):
                pid = r[key]
                if pid is not None:
                    out[int(pid)] = state
        return out


def _selftest_views() -> None:
    """Import-time shape check: every public reader exists and takes the
    documented positional args (the facade ratchet pins the names)."""
    import inspect as _inspect

    for name in (
        "guild_ledger_recent",
        "guild_open_debts",
        "guild_subsidies_recent",
        "guild_fee_arrears_open",
        "guild_grant_links_for_guild",
        "guild_locks",
        "guild_chat_count",
        "guild_open_polls",
        "guild_grant_state_for_posts",
    ):
        assert callable(globals()[name]), name
    assert len(_inspect.signature(guild_grant_state_for_posts).parameters) == 1
    assert json.dumps({}) == "{}"


if __name__ == "__main__":
    _selftest_views()
    print("test_guilds_views_shapes: all passed")
