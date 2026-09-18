"""db._guilds_reputation — reputation v1 (proposal #525, PR-14, item 5037).

A public 0-100 score from four components: settled debts (40),
completed projects (30), member retention (20), upkeep stability (10) -
weights knob-tunable and normalized by the POSITIVE weights' sum.
Components with no data score the 0.5 open prior (the skill-system
precedent): a newborn guild reads middling, never perfect or damned.
Non-positive weights drop their component; a non-finite knob falls
back to 40/30/20/10 wholesale, and no positive weights at all reads
the prior (50.0). Pure compute over existing tables; no
writes, no new tables, safe to call from readers.
"""

from __future__ import annotations

import config
from db._core._conn import _conn


def _ratio(have: int, total: int) -> float:
    if total <= 0:
        return 0.5
    return max(0.0, min(1.0, have / total))


def guild_reputation(guild_id: int) -> dict:
    """{score, parts} for one guild. score is 0-100 rounded to 1dp;
    parts names each component's 0-1 value for display tooltips."""
    with _conn() as conn:
        debts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM guild_debts WHERE guild_id = ?"
            " GROUP BY status",
            (guild_id,),
        ).fetchall()
        dmap = {r["status"]: r["n"] for r in debts}
        settled = _ratio(
            dmap.get("settled", 0), dmap.get("settled", 0) + dmap.get("written_off", 0)
        )
        links = conn.execute(
            "SELECT status, COUNT(*) AS n FROM guild_grant_links"
            " WHERE guild_id = ? GROUP BY status",
            (guild_id,),
        ).fetchall()
        lmap = {r["status"]: r["n"] for r in links}
        completion = _ratio(
            lmap.get("complete", 0), lmap.get("complete", 0) + lmap.get("expired", 0)
        )
        ever = conn.execute(
            "SELECT COUNT(DISTINCT agent_id) AS n FROM ("
            " SELECT agent_id FROM guild_members WHERE guild_id = ?"
            " UNION SELECT agent_id FROM guild_leave_log WHERE guild_id = ?)",
            (guild_id, guild_id),
        ).fetchone()[0]
        left = conn.execute(
            "SELECT COUNT(DISTINCT agent_id) FROM guild_leave_log WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()[0]
        retention = _ratio(int(ever or 0) - int(left or 0), int(ever or 0))
        arrears = conn.execute(
            "SELECT status, COUNT(*) AS n FROM guild_fee_arrears"
            " WHERE guild_id = ? GROUP BY status",
            (guild_id,),
        ).fetchall()
        amap = {r["status"]: r["n"] for r in arrears}
        stability = _ratio(
            amap.get("paid", 0), amap.get("open", 0) + amap.get("paid", 0)
        )
        try:
            weights = (
                float(config.GUILD_REP_SETTLED_W),
                float(config.GUILD_REP_COMPLETION_W),
                float(config.GUILD_REP_RETENTION_W),
                float(config.GUILD_REP_STABILITY_W),
            )
        except (TypeError, ValueError):
            # domain: degrade-silently - corrupt knobs degrade to 40/30/20/10
            weights = (40.0, 30.0, 20.0, 10.0)
        import math

        parts = {
            "settled": settled,
            "completion": completion,
            "retention": retention,
            "stability": stability,
        }
        try:
            raw = [float(w) for w in weights]
        except (TypeError, ValueError):
            raw = []
        if not raw or not all(math.isfinite(w) for w in raw):
            # domain: degrade-silently - a non-finite knob falls back to
            # 40/30/20/10 wholesale (renormalizing around corruption would
            # silently bless a misconfigured treasury signal)
            raw = [40.0, 30.0, 20.0, 10.0]
        pos = [(w, v) for w, v in zip(raw, parts.values(), strict=True) if w > 0]
        if not pos:
            return {"score": 50.0, "parts": parts}
        total_w = sum(w for w, _ in pos)
        score = round(100 * sum(w * v for w, v in pos) / total_w, 1)
        return {"score": score, "parts": parts}


def _selftest_reputation() -> None:
    assert _ratio(0, 0) == 0.5
    assert _ratio(3, 4) == 0.75
    assert _ratio(5, 4) == 1.0


if __name__ == "__main__":
    _selftest_reputation()
    print("test_guilds_reputation_shapes: all passed")
