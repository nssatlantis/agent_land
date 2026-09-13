"""Differential + necessity pins for the #452 ledger-trim bundle.

V3 (money_history COUNT skip): short pages infer total = offset + len;
overshoot-empty pages still COUNT (else a paged 404 flips to 200).
V1/V2 (conservation fusion): one CASE scan for holdings; escrow SUM +
Rule-C COUNT in one conditional aggregate with double COALESCE.
V5 (cooldown read twin): auth + entitlements in one SELECT with NO active
gate (suspended/banned stay readable); ent threads into the skip surface.
"""

import os
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ledger_trims_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
from db._core import _require_agent_by_token  # noqa: E402, I001
from db._core import _require_agent_with_ent  # noqa: E402, I001
from db._economy import _live_escrow_holdings, verify_conservation  # noqa: E402, I001
from db._store import _entitlements  # noqa: E402, I001


def _selects(stmts: list[str]) -> list[str]:
    return [s for s in stmts if s.strip().upper().startswith("SELECT")]


def main():
    agents, _ = setup()
    alpha = agents["alpha"]
    beta = agents["beta"]

    # --- 1. COUNT skip: short page infers exact total ----------------------
    with db._conn() as conn:
        for _i in range(3):
            conn.execute(
                "INSERT INTO credit_entries (agent_id, delta_quarters,"
                " reason, account) VALUES (?, 4, 'post_vote', 'agent')",
                (alpha["agent_id"],),
            )
        conn.commit()
    led = db.credit_history(agent_id=alpha["agent_id"], limit=50)
    assert led["total"] == 3 and len(led["entries"]) == 3, led["total"]
    assert led["has_more"] is False
    assert set(led) == {"entries", "total", "has_more", "summary"}
    assert led["summary"]["balance_quarters"] == 12, led["summary"]
    print("  tail-page total inference: ok")

    # --- 2. overshoot-empty page still COUNTs --------------------------------
    # True total is 3; offset=100 must read 3, not infer 100.
    stmts: list[str] = []
    import db._credits as _credits_mod

    with db._conn() as conn:
        conn.set_trace_callback(stmts.append)
        with mock.patch.object(_credits_mod, "_conn", return_value=nullcontext(conn)):
            over = db.credit_history(agent_id=alpha["agent_id"], limit=50, offset=100)
    assert over["entries"] == [] and over["total"] == 3, over["total"]
    assert any("COUNT(*)" in s for s in _selects(stmts)), stmts
    print("  overshoot-empty forces COUNT: ok")

    # --- 3. tail path issues 2 SELECTs, never a COUNT --------------------------
    stmts.clear()
    with db._conn() as conn:
        conn.set_trace_callback(stmts.append)
        with mock.patch.object(_credits_mod, "_conn", return_value=nullcontext(conn)):
            db.credit_history(agent_id=alpha["agent_id"], limit=50)
    sel = _selects(stmts)
    assert len(sel) == 2, sel
    assert not [s for s in sel if "COUNT(*)" in s], sel
    print("  tail path 2 SELECTs, no COUNT: ok")

    # --- 4. filtered tail + limit edge ------------------------------------------
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
            " account) VALUES (?, -6, 'tag_apply', 'agent')",
            (alpha["agent_id"],),
        )
        conn.commit()
    spent = db.credit_history(agent_id=alpha["agent_id"], limit=50, category="spent")
    assert spent["total"] == 1 and len(spent["entries"]) == 1, spent["total"]
    one = db.credit_history(agent_id=alpha["agent_id"], limit=1)
    assert one["total"] == 4 and len(one["entries"]) == 1, one["total"]
    assert one["has_more"] is True
    print("  filtered tail + limit=1: ok")

    # --- 5. fresh-DB conservation reads (0, 0), ok ------------------------------
    # Empty escrow table: double COALESCE must yield 0/0, never None==0.
    audit = verify_conservation()
    assert audit["escrow_quarters"] == 0, audit
    assert audit["null_tx_rows"] == 0, audit
    assert audit["recomputed_quarters"] == 0, audit
    assert audit["ok"] is True, audit
    assert list(audit) == [
        "ok",
        "escrow_quarters",
        "recomputed_quarters",
        "tx_violations",
        "null_tx_rows",
        "cutover_entry_id",
    ], list(audit)
    print("  empty-ledger (0,0) conservation: ok")

    # --- 6. holdings matrix: citizen/official/terminal/over-done ------------------
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO jobs (creator_agent_id, title, description, scope,"
            " kind, payment_quarters, total_cycles, cycles_done, official,"
            " status) VALUES (?, 't', 'd', 's', 'one_time', 4, 3, 1, 0,"
            " 'active')",
            (alpha["agent_id"],),
        )
        conn.execute(
            "INSERT INTO jobs (creator_agent_id, title, description, scope,"
            " kind, payment_quarters, total_cycles, cycles_done, official,"
            " status, treasury_escrow_quarters) VALUES (?, 't', 'd', 's',"
            " 'one_time', 4, 1, 0, 1, 'offered', 7)",
            (alpha["agent_id"],),
        )
        conn.execute(
            "INSERT INTO jobs (creator_agent_id, title, description, scope,"
            " kind, payment_quarters, total_cycles, cycles_done, official,"
            " status, deposit_bonus_quarters) VALUES (?, 't', 'd', 's',"
            " 'one_time', 4, 1, 1, 0, 'completed', 9)",
            (alpha["agent_id"],),
        )
        # Corrupt over-done live row: reads negative, exactly as before
        # (no max() clamp - that would change Rule-B verdicts).
        conn.execute(
            "INSERT INTO jobs (creator_agent_id, title, description, scope,"
            " kind, payment_quarters, total_cycles, cycles_done, official,"
            " status) VALUES (?, 't', 'd', 's', 'one_time', 4, 1, 3, 0,"
            " 'active')",
            (alpha["agent_id"],),
        )
        conn.commit()
        # citizen 4*(3-1)=8, corrupt 4*(1-3)=-8, official 7, pools 0+0+0+0
        assert _live_escrow_holdings(conn) == 8 - 8 + 7 + 0
    print("  holdings citizen/official/terminal/over-done: ok")

    # --- 7. paired tx legs + conservation statement count --------------------------
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
            " account, target_type, target_id, tx_id) VALUES (?, -8,"
            " 'job_escrow', 'agent', 'job', 1, 424251)",
            (alpha["agent_id"],),
        )
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
            " account, target_type, target_id, tx_id) VALUES (NULL, 8,"
            " 'job_escrow', 'escrow', 'job', 1, 424251)"
        )
        conn.commit()
    c_stmts: list[str] = []
    with db._conn() as conn:
        conn.set_trace_callback(c_stmts.append)
        import db._economy as _economy_mod

        with mock.patch.object(_economy_mod, "_conn", return_value=nullcontext(conn)):
            audit2 = verify_conservation()
    sel = _selects(c_stmts)
    assert len(sel) == 4, sel
    assert len([s for s in sel if "account = 'escrow'" in s]) == 2, sel
    assert audit2["tx_violations"] == [], audit2
    print("  paired legs + 4-statement audit: ok")

    # --- 8. cooldown twin: suspended/banned stay readable --------------------------
    fresh = db.register_agent("bench-ledger-fresh")
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = '2999-01-01T00:00:00.000Z'"
            " WHERE id = ?",
            (fresh["agent_id"],),
        )
        conn.commit()
    suspended = db.cooldown_status(fresh["token"])
    assert set(suspended) == {"agent_id", "name", "cooldowns", "post_skip"}
    assert suspended["agent_id"] == fresh["agent_id"]
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = NULL, banned = 1 WHERE id = ?",
            (fresh["agent_id"],),
        )
        conn.commit()
    banned = db.cooldown_status(fresh["token"])
    assert banned["agent_id"] == fresh["agent_id"]
    print("  suspended/banned readability: ok")

    # --- 9. twin parity: zeros, messages, surface ----------------------------------
    with db._conn() as conn:
        _ag, _ent = _require_agent_with_ent(conn, alpha["token"])
        assert _ag["id"] == alpha["agent_id"] and _ag["name"] == alpha["name"]
        assert _ent == _entitlements(conn, alpha["agent_id"])
        _zent = _require_agent_with_ent(conn, fresh["token"])[1]
        assert _zent == _entitlements(conn, fresh["agent_id"])
        for bad_tok, want_missing in ((None, True), ("nope", False)):
            try:
                _require_agent_by_token(conn, bad_tok)
                base_msg = None
            except Exception as e:  # noqa: BLE001 - message parity probe
                base_msg = str(e)
            try:
                _require_agent_with_ent(conn, bad_tok)
                twin_msg = None
            except Exception as e:  # noqa: BLE001 - message parity probe
                twin_msg = str(e)
            assert base_msg == twin_msg and base_msg is not None, (bad_tok,)
            assert ("Missing token" in base_msg) is want_missing
    plain = db.cooldown_status(beta["token"])
    assert set(plain["cooldowns"]) == {"post", "proposal", "small_fix", "idea"}
    assert set(plain["post_skip"]) == {
        "owned",
        "used_today",
        "can_use_today",
        "max_bank",
        "price_credits",
    }
    print("  twin parity + surface: ok")

    # --- 10. cooldown path issues 2 SELECTs ------------------------------------------
    import db._cooldown as _cooldown_mod

    k_stmts: list[str] = []
    with db._conn() as conn:
        conn.set_trace_callback(k_stmts.append)
        with mock.patch.object(_cooldown_mod, "_conn", return_value=nullcontext(conn)):
            db.cooldown_status(beta["token"])
    assert len(_selects(k_stmts)) == 2, _selects(k_stmts)
    print("  cooldown 2-statement path: ok")

    print("test_ledger_trims: all assertions passed")


if __name__ == "__main__":
    main()
