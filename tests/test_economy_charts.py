"""Tests for the /economy charts build (proposal #603): seal-series and
daily-flow db helpers (shape, growth, EXPLAIN pins) plus the four viewer
chart helpers (markers, empty states, corrupt-row skips, escaping).
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_economy_charts_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402,I001

db.init_db()

AGENTS, BASE_POST = setup()

from db._credits import mint as _mint  # noqa: E402
from db._economy import (  # noqa: E402
    treasury_daily_flows,
    treasury_supply_series,
    write_checkpoint,
)


class _Req:
    """Minimal Request stand-in - mirrors the house _Req fake (real
    QueryParams, like the _Req fakes in test_viewer.py)."""

    def __init__(self, params=None):
        from starlette.datastructures import QueryParams

        self.query_params = QueryParams(params or {})


def _no_full_scan(plan: str, table: str) -> bool:
    """Bare-SCAN detector (mirrors test_benchmark._no_full_scan): only an
    exact per-line 'SCAN <table>' / 'SCAN TABLE <table>' fails, so a
    covering-index scan never false-fires."""
    return not any(
        line.strip() in (f"SCAN {table}", f"SCAN TABLE {table}")
        for line in plan.splitlines()
    )


def _explain(sql: str, params=()) -> str:
    with db._conn() as c:
        rows = c.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return "\n".join(str(r[-1]) for r in rows)


def test_daily_flows_buckets():
    """Backdated treasury legs land in their day's buckets; quiet days
    read all-zero buckets; the window is exactly 14 days, oldest-first."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yest = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    with db._conn(immediate=True) as c:
        c.execute(
            "INSERT INTO credit_entries"
            " (agent_id, delta_units, reason, account, created_at)"
            " VALUES (NULL, 4000, 'admin_mint', 'treasury', ?)",
            (f"{yest}T12:00:00.000Z",),
        )
    rows = treasury_daily_flows(14)
    assert len(rows) == 14, rows
    assert rows[-1]["day"] == today, rows
    assert rows[0]["day"] != today, rows
    by_day = {r["day"]: r["flows"] for r in rows}
    assert by_day[yest]["minted_units"] >= 4000, by_day[yest]
    assert set(rows[0]["flows"]) >= {
        "minted_units",
        "burned_units",
        "payouts_out_units",
    }, rows[0]


def test_daily_flows_explain_covering():
    """The daily GROUP BY rides a treasury partial index - no full table
    scan. The name is deliberately the shared prefix: GitHub-hosted
    SQLite serves this shape from idx_credit_entries_treasury while
    other builds prefer the covering idx_credit_entries_treasury_flows
    (same pin shape as test_benchmark's treasury probe) - either is a
    valid index path, only a bare SCAN fails."""
    plan = _explain(
        "SELECT substr(created_at, 1, 10) AS day, reason,"
        " SUM(delta_units) AS total FROM credit_entries"
        " WHERE account = 'treasury' AND created_at >= ?"
        " GROUP BY day, reason ORDER BY day",
        ("2000-01-01T00:00:00.000Z",),
    )
    assert "idx_credit_entries_treasury" in plan, plan
    assert _no_full_scan(plan, "credit_entries"), plan


def test_economy_body_renders_charts():
    """The full /economy body carries all four chart markers once seals
    exist (trend line, daily bars, split bar caption, svg)."""
    write_checkpoint()
    with db._conn(immediate=True) as _c:
        _mint(2000, "charts_body_topup", admin="test-suite", conn=_c)
    write_checkpoint()
    from viewer._money import _economy_body

    html = _economy_body(_Req())
    for marker in (
        "Treasury over time",
        "Daily flows, trailing 14 days",
        "stakes, guild pools and bonds overlap",
        "<svg",
        "<polyline",
    ):
        assert marker in html, marker


def test_helpers_degrade():
    """Empty inputs read empty strings; a raising series reads ''; a
    corrupt point is skipped while good points still render."""
    from viewer import _money as money_mod
    from viewer._cache import _reset_for_tests

    assert money_mod._supply_split_html({}) == ""
    assert money_mod._store_donut_html({}) == ""
    assert money_mod._store_donut_html({"items": []}) == ""
    real = money_mod.db.treasury_supply_series
    _reset_for_tests()
    money_mod.db.treasury_supply_series = lambda *a: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    try:
        assert money_mod._treasury_trend_html() == ""
    finally:
        money_mod.db.treasury_supply_series = real
        _reset_for_tests()
    money_mod.db.treasury_supply_series = lambda *a: [
        {
            "created_at": "2026-09-21T00:00:00.000Z",
            "supply_units": 1000,
            "treasury_units": 600,
        },
        {
            "created_at": "2026-09-21T01:00:00.000Z",
            "supply_units": "bogus",
            "treasury_units": 600,
        },
        {
            "created_at": "2026-09-21T02:00:00.000Z",
            "supply_units": 1100,
            "treasury_units": 650,
        },
    ]
    try:
        html = money_mod._treasury_trend_html()
        assert "<polyline" in html, html
        assert "2 seals" in html, html
    finally:
        money_mod.db.treasury_supply_series = real
        _reset_for_tests()


def test_store_donut_fabricated():
    """Donut renders conic-gradient + legend off a fabricated store dict
    and escapes hostile labels."""
    from viewer._money import _store_donut_html

    html = _store_donut_html(
        {
            "items": [
                {
                    "label": "vote_boost",
                    "units": 10,
                    "revenue_credits": "5",
                    "units_7d": 3,
                },
                {
                    "label": "<script>x</script>",
                    "units": 5,
                    "revenue_credits": "2.5",
                    "units_7d": 0,
                },
            ]
        }
    )
    assert "conic-gradient" in html, html
    assert "vote_boost" in html, html
    assert "<script>" not in html, html
    assert "3 in 7d" in html, html


def test_supply_series_downsamples():
    """70 seals read back as exactly 60 points, newest kept."""
    with db._conn(immediate=True) as c:
        for i in range(70):
            c.execute(
                "INSERT INTO economy_checkpoints"
                " (created_at, last_entry_id, entry_count,"
                " total_supply_u, treasury_u, running_hash)"
                " VALUES (?, 0, 0, ?, ?, 'test')",
                ("2026-09-21T03:00:00.000Z", 1000 + i, 600),
            )
    s = treasury_supply_series()
    assert len(s) == 60, len(s)
    assert s[-1]["supply_units"] == 1000 + 69, s[-1]


def test_supply_series_limit_bounded():
    """The series honors its row cap deterministically: 130 seals read
    back as 60 points with the newest kept (behavior pin replacing the
    planner-text pin, which flips on table size/SQLite version)."""
    with db._conn(immediate=True) as c:
        for i in range(130):
            c.execute(
                "INSERT INTO economy_checkpoints"
                " (created_at, last_entry_id, entry_count,"
                " total_supply_u, treasury_u, running_hash)"
                " VALUES (?, 0, 0, ?, ?, 'test')",
                ("2026-09-21T04:00:00.000Z", 2000 + i, 700),
            )
    s = treasury_supply_series()
    assert len(s) == 60, len(s)
    assert s[-1]["supply_units"] == 2000 + 129, s[-1]


def test_supply_series_seals():
    """write_checkpoint seals feed the series: +2 seals, exact mint
    delta on both supply and treasury, oldest-first."""
    write_checkpoint()
    pre = treasury_supply_series()[-1]
    with db._conn(immediate=True) as _c:
        _mint(20000, "charts_suite_topup", admin="test-suite", conn=_c)
    write_checkpoint()
    s = treasury_supply_series()
    assert s[-1]["treasury_units"] - pre["treasury_units"] == 20000, (pre, s[-1])
    assert s[-1]["supply_units"] - pre["supply_units"] == 20000, (pre, s[-1])
    assert [p["created_at"] for p in s] == sorted(p["created_at"] for p in s), s


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} economy-charts tests passed")
