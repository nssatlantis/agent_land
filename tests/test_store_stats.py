"""Tests for the citizen-store sales reader (db.store_stats, #391).

Self-contained throwaway DB: two buyers, known prices, one backdated
purchase (windows), one blessed-bench take+refund (netting), plus an
empty-DB shape pin via a second throwaway file. No harness, no mocks.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_store_stats_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402, I001
import db._credits as _cr  # noqa: E402
import db._store as _store  # noqa: E402


def _fund(agent_id: int, quarters: int):
    with db._conn() as conn:
        assert _cr.grant(agent_id, quarters, "admin_adjust", conn=conn)


def _price(knob: str) -> int:
    return _cr.exact_from_credits(getattr(config, knob), what=knob)


def _row(stats: dict, reason: str) -> dict:
    for item in stats["items"]:
        if item["reason"] == reason:
            return item
    raise AssertionError(f"no sales row for {reason}")


def main():
    agents, _ = setup()
    buyer_a = db.register_agent("stats-alpha")
    buyer_b = db.register_agent("stats-beta")
    _fund(buyer_a["agent_id"], 400)
    _fund(buyer_b["agent_id"], 400)

    vote_q = _price("STORE_VOTE_PRICE")
    ci_q = _price("STORE_CI_PRICE")
    color_q = _price("STORE_COLOR_PRICE")
    bio_q = _price("STORE_BIO_PRICE")
    notes_q = _price("STORE_NOTES_UNLOCK")

    db.buy_store_item(buyer_a["token"], "vote_boost")
    db.buy_store_item(buyer_b["token"], "vote_boost")
    db.buy_store_item(buyer_a["token"], "ci_boost")
    db.buy_store_item(buyer_b["token"], "name_color", color="#112233")
    db.buy_store_item(buyer_a["token"], "drafts_unlock")  # bio rides behind
    # the drafts gate in the buy cascade, so unlock first (covers it too)
    db.buy_store_item(buyer_a["token"], "bio", text="stats bio")
    db.buy_store_item(buyer_a["token"], "notes_unlock")
    db.buy_store_item(buyer_b["token"], "blessed_bench")
    with db._conn(immediate=True) as conn:
        _store._take_blessed_bench(conn, buyer_b["agent_id"])
    db.refund_blessed_bench(buyer_b["agent_id"])

    # Backdate b's vote purchase 8 days (both legs share the tx).
    old_at = (
        (datetime.now(timezone.utc) - timedelta(days=8))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    with db._conn() as conn:
        txs = [
            r["tx_id"]
            for r in conn.execute(
                "SELECT DISTINCT tx_id FROM credit_entries"
                " WHERE agent_id = ? AND reason = 'store_vote'",
                (buyer_b["agent_id"],),
            ).fetchall()
        ]
        assert len(txs) == 1, "one vote-buy tx to backdate"
        conn.execute(
            f"UPDATE credit_entries SET created_at = ? WHERE tx_id IN ({','.join('?' * len(txs))})",
            (old_at, *txs),
        )

    stats = db.store_stats()
    assert stats["window_days"] == 7, "window labeled"
    assert len(stats["items"]) == 15, (
        f"14 catalog reasons + notes_write ({len(stats['items'])})"
    )
    for item in stats["items"]:
        for key in (
            "key",
            "label",
            "reason",
            "units",
            "units_7d",
            "revenue_quarters",
            "revenue_credits",
            "buyers",
            "buyers_7d",
            "held",
            "price_credits",
        ):
            assert key in item, f"row misses {key}"

    vote = _row(stats, "store_vote")
    assert (vote["units"], vote["units_7d"]) == (2, 1), (
        "backdated buy leaves the window"
    )
    assert (vote["buyers"], vote["buyers_7d"]) == (2, 1), "buyers windowed too"
    assert vote["revenue_quarters"] == 2 * vote_q, "revenue exact in quarters"
    assert vote["held"] == 2, "two vote boosts installed"
    ci = _row(stats, "store_ci")
    assert (ci["units"], ci["buyers"], ci["revenue_quarters"]) == (1, 1, ci_q)
    assert _row(stats, "store_color")["revenue_quarters"] == color_q
    assert _row(stats, "store_bio")["revenue_quarters"] == bio_q
    assert _row(stats, "store_notes_unlock")["revenue_quarters"] == notes_q
    assert _row(stats, "store_blessed_bench")["price_credits"] == (
        config.STORE_BLESSED_BENCH_PRICE
    )
    with db._conn() as conn:
        hold = conn.execute(
            "SELECT bio, draft_slots FROM store_entitlements WHERE agent_id = ?",
            (buyer_a["agent_id"],),
        ).fetchone()
    # Regression pin (live bug, fixed with this reader): an unguarded
    # draft_slot block used to swallow every bio buy (charged a slot,
    # never set the bio). Bio sets the bio; slots stay at the unlock.
    assert hold["bio"] == "stats bio", "bio buy sets the bio"
    assert hold["draft_slots"] == 1, "bio buy grants no phantom slot"
    assert _row(stats, "store_notes_unlock")["units"] == 1

    # Refund netting: the buy counts as a unit, the price nets to zero.
    bench = _row(stats, "store_blessed_bench")
    assert bench["units"] == 1, "take+refund keeps the unit"
    assert bench["revenue_quarters"] == 0, f"refund nets revenue ({bench})"
    assert bench["held"] == 0, "taken bank stays spent"

    # Predicate-shape pins (sargable rewrite): unknown future reasons still
    # bucket under Other, uppercase reasons stay excluded (BINARY range -
    # all ledger writers emit lowercase literals only).
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
            " account) VALUES (?, ?, 'store_future_gadget', 'agent')",
            (buyer_a["agent_id"], -vote_q),
        )
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
            " account) VALUES (NULL, ?, 'store_future_gadget_intake', 'treasury')",
            (vote_q,),
        )
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
            " account) VALUES (?, ?, 'STORE_vote', 'agent')",
            (buyer_a["agent_id"], -vote_q),
        )
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
            " account) VALUES (NULL, ?, 'STORE_vote_intake', 'treasury')",
            (vote_q,),
        )
    stats2 = db.store_stats()
    future = _row(stats2, "store_future_gadget")
    assert future["label"].startswith("Other ("), "unknown reason buckets Other"
    assert (future["units"], future["buyers"]) == (1, 1), "future reason counted"
    assert future["revenue_quarters"] == vote_q, "future revenue exact"
    assert not any(i["reason"] == "STORE_vote" for i in stats2["items"]), (
        "uppercase reason excluded from buyers"
    )
    assert _row(stats2, "store_vote")["units"] == 2, "uppercase intake excluded"
    assert stats2["totals"]["units"] == stats["totals"]["units"] + 1, (
        "only the future intake adds a unit"
    )
    assert stats2["totals"]["buyers"] == stats["totals"]["buyers"], (
        "uppercase buyer leg excluded"
    )

    totals = stats["totals"]
    assert totals["units"] == sum(i["units"] for i in stats["items"]), "units add up"
    assert totals["revenue_quarters"] == sum(
        i["revenue_quarters"] for i in stats["items"]
    ), "revenue adds up"
    assert totals["revenue_credits"] == _cr.format_credits(totals["revenue_quarters"])
    assert totals["revenue_7d_quarters"] == totals["revenue_quarters"] - vote_q, (
        "backdated sale leaves the 7d revenue"
    )
    assert totals["buyers"] == 2 and totals["buyers_7d"] == 2, "both buyers counted"
    assert stats["installed"]["citizens_served"] == 2, "two entitlement rows"

    # Empty DB renders zeros, never None.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "store_stats_empty.db")
        db.init_db()
        empty = db.store_stats()
        assert empty["totals"]["units"] == 0
        assert empty["totals"]["revenue_quarters"] == 0
        assert empty["totals"]["buyers"] == 0
        assert empty["installed"]["citizens_served"] == 0
        assert all(i["units"] == 0 and i["held"] == 0 for i in empty["items"])
        with db._conn() as conn:
            fresh_idx = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        assert "idx_credit_entries_store_buyers" in fresh_idx, (
            "fresh boot creates the store-buyers covering index"
        )
    finally:
        db.DB_PATH = saved_db_path

    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_store_stats: all assertions passed")


if __name__ == "__main__":
    main()
