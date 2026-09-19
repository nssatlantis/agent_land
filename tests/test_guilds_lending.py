"""Guild soft-lending + delinquency (proposal #525, PR-7): subsidy
requests (auto-tier immediate pay, over-tier Idea venue + admin
decide), deposit-match (lump now, window net-basis at maturity),
payback debts (Treasury invoices, part-pay, freeze refresh),
delinquency freeze, seize-and-dissolve waterfall with write-offs,
suspension forfeits (member + founder-succession), disband stake
release, shared pooled budget, and conservation (memo-only support
pays: supply and treasury fixed; forfeit burns destroy supply by
design).
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_lending_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "0"
os.environ["FORUM_INVOICE_MIN_KARMA"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, units: int):
    import db._credits as _cr

    with db._conn() as _c:
        ok = _cr.grant(
            agent_id,
            units,
            "guild_lending_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )
    assert ok, "treasury could not fund the test seed"


def _treasury() -> int:
    import db._credits as _cr

    with db._conn() as conn:
        return _cr.treasury_balance(conn)


def _supply() -> int:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE account IN ('agent', 'treasury', 'escrow')"
        ).fetchone()
    return int(row[0] or 0)


def _arm(env_key: str, value: str):
    from tests._setup import config as _cfg

    old = os.environ.get(env_key)
    os.environ[env_key] = value
    importlib.reload(_cfg)
    return old


def _unarm(old, env_key: str):
    from tests._setup import config as _cfg

    if old is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = old
    importlib.reload(_cfg)


def _found(name: str | None = None) -> tuple[dict, dict]:
    ag = _new_agent("gl-founder")
    _fund(ag["agent_id"], 600)
    return ag, db.found_guild(ag["token"], name or f"Lending-{_SEQ[0]}")


def _mate(
    founder: dict,
    guild: dict,
    prefix: str = "gl-mate",
    deposit_cr: float = 10.0,
) -> dict:
    mate = _new_agent(prefix)
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.guild_deposit(mate["token"], guild["id"], deposit_cr)
    return mate


def _pool(guild_id: int) -> int:
    with db._conn() as conn:
        return db.guild_balance(conn, guild_id)


def _debt(guild_id: int) -> dict | None:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_debts WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _backdate_debt_due(debt_id: int, due_iso: str):
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_debts SET due_at = ? WHERE id = ?", (due_iso, debt_id)
        )
        conn.execute(
            "UPDATE invoices SET due_at = ? WHERE id IN"
            " (SELECT invoice_id FROM guild_debt_invoices WHERE debt_id = ?)",
            (due_iso, debt_id),
        )


def _accept_invoice(agent: dict, invoice_id: int):
    # Invoices start pending; payback bills must be accepted before pay.
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET status = 'accepted' WHERE id = ?",
            (invoice_id,),
        )


def test_tables_upgrade_and_kinds():
    with db._conn() as conn:
        for table in (
            "guild_subsidies",
            "guild_debts",
            "guild_debt_invoices",
            "guild_match_windows",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    db.init_db()
    with db._conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    for table in (
        "guild_subsidies",
        "guild_debts",
        "guild_debt_invoices",
        "guild_match_windows",
    ):
        assert table in tables, f"{table} missing after init_db"
    for idx in (
        "idx_guild_subsidies_guild",
        "idx_guild_debts_guild",
        "idx_guild_debt_invoices_guild",
        "idx_guild_match_windows_guild",
    ):
        assert idx in indexes, f"{idx} missing after init_db"
    # Widened ledger kinds land in the balance math as inflows.
    founder, guild = _found()
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units, note)"
            " VALUES (?, 'subsidy', 7, 'kind pin')",
            (guild["id"],),
        )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units, note)"
            " VALUES (?, 'match', 5, 'kind pin')",
            (guild["id"],),
        )
        assert db.guild_balance(conn, guild["id"]) == 12


def test_request_auto_pays_and_conservation():
    founder, guild = _found()
    _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    supply_before, treasury_before, pool_before = (
        _supply(),
        _treasury(),
        _pool(guild["id"]),
    )
    out = db.request_guild_subsidy(
        founder["token"], guild["id"], 1.0, False, "seed money"
    )
    assert out["status"] == "paid" and out["amount_units"] == 20, out
    assert out["debt_id"] is None and out["invoice_id"] is None
    assert _pool(guild["id"]) == pool_before + 20
    assert _supply() == supply_before, "subsidy must be memo-only"
    assert _treasury() == treasury_before, "subsidy must not move the treasury"


def test_request_payback_mints_debt_and_part_pay():
    founder, guild = _found()
    _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    out = db.request_guild_subsidy(
        founder["token"], guild["id"], 1.0, True, "bridge loan"
    )
    assert out["status"] == "paid" and out["debt_id"] is not None, out
    debt = _debt(guild["id"])
    assert debt is not None and debt["status"] == "current", debt
    assert debt["remaining_units"] == 20, debt
    _accept_invoice(founder, out["invoice_id"])
    _fund(founder["agent_id"], 200)
    db.pay_invoice(founder["token"], out["invoice_id"], 0.5)
    debt = _debt(guild["id"])
    assert debt is not None and debt["remaining_units"] == 10, debt
    assert debt["status"] == "current", debt
    db.pay_invoice(founder["token"], out["invoice_id"])
    debt = _debt(guild["id"])
    assert debt is not None and debt["status"] == "settled", debt
    assert debt["remaining_units"] == 0, debt
    # Terminal-transition guards: declined/cancelled payback bills would
    # brick their debts, so both doors refuse on debt-linked invoices.
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_subsidies SET created_at = ? WHERE guild_id = ?",
            ("2026-08-01T00:00:00.000Z", guild["id"]),
        )
    out2 = db.request_guild_subsidy(
        founder["token"], guild["id"], 1.0, True, "second bridge"
    )
    try:
        db.decline_invoice(founder["token"], out2["invoice_id"])
        raise AssertionError("debt bill declined")
    except Exception as exc:
        assert "payback" in str(exc), exc
    try:
        db.cancel_invoice(founder["token"], out2["invoice_id"])
        raise AssertionError("debt bill cancelled")
    except Exception as exc:
        assert "payback" in str(exc) or "forgive" in str(exc), exc


def test_over_tier_venue_and_admin_decide():
    founder, guild = _found()
    _mate(founder, guild)
    out = db.request_guild_subsidy(founder["token"], guild["id"], 5.0, True, "big push")
    assert out["status"] == "requested" and out["tier"] == "admin", out
    assert out["idea_post_id"] is not None
    # The admin queue is serial: a second request waits for the decision.
    try:
        db.request_guild_subsidy(
            founder["token"], guild["id"], 1.0, True, "jumping queue"
        )
        raise AssertionError("concurrent request filed")
    except Exception as exc:
        assert "undecided" in str(exc), exc
    # Oversized reasons refuse before any row exists (venue posts cap).
    try:
        db.request_guild_subsidy(founder["token"], guild["id"], 1.0, True, "x" * 9000)
        raise AssertionError("oversized reason filed")
    except Exception as exc:
        assert "8000" in str(exc), exc
    with db._conn() as conn:
        idea = conn.execute(
            "SELECT proposal_kind FROM posts WHERE id = ?",
            (out["idea_post_id"],),
        ).fetchone()
    assert idea is not None and idea["proposal_kind"] == "idea", dict(idea or {})
    assert _pool(guild["id"]) == 200, "requested pays nothing"
    try:
        db.decide_guild_subsidy(founder["token"], out["subsidy_id"], True)
        raise AssertionError("non-admin decided")
    except Exception as exc:
        assert "admin" in str(exc), exc
    decided = db.decide_guild_subsidy(
        founder["token"], out["subsidy_id"], False, admin=True
    )
    assert decided["status"] == "declined", decided
    # A declined request paid nothing, so it is not "a subsidy taken":
    # the free second subsidy survives without payback.
    out2 = db.request_guild_subsidy(
        founder["token"], guild["id"], 1.0, False, "retry small"
    )
    assert out2["status"] == "paid", out2


def test_second_needs_payback_and_overdue_blocks():
    founder, guild = _found()
    _mate(founder, guild)
    first = db.request_guild_subsidy(founder["token"], guild["id"], 1.0, False, "first")
    assert first["status"] == "paid"
    try:
        db.request_guild_subsidy(founder["token"], guild["id"], 1.0, False, "again")
        raise AssertionError("second no-payback subsidy filed")
    except Exception as exc:
        assert "payback" in str(exc), exc
    # Stand down the 14d auto clock so the payback-second can file.
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_subsidies SET created_at = ? WHERE id = ?",
            ("2026-08-01T00:00:00.000Z", first["subsidy_id"]),
        )
    owed = db.request_guild_subsidy(founder["token"], guild["id"], 1.0, True, "second")
    assert owed["status"] == "paid" and owed["debt_id"] is not None
    _backdate_debt_due(owed["debt_id"], "2026-09-01T00:00:00.000Z")
    db.sweep_guild_lending()
    with db._conn() as conn:
        row = conn.execute(
            "SELECT status FROM guild_debts WHERE id = ?", (owed["debt_id"],)
        ).fetchone()
    assert row is not None and row["status"] == "overdue", dict(row or {})
    try:
        db.request_guild_subsidy(founder["token"], guild["id"], 1.0, True, "third")
        raise AssertionError("subsidy filed while overdue")
    except Exception as exc:
        assert "overdue" in str(exc), exc


def test_match_lump_and_window_wash():
    founder, guild = _found()
    mate = _mate(founder, guild)
    lump = db.open_guild_match_window(
        founder["token"], guild["id"], "lump", amount_credits=2.0
    )
    assert lump["status"] == "paid" and lump["amount_units"] == 40, lump
    window = db.open_guild_match_window(founder["token"], guild["id"], "window")
    assert window["status"] == "open", window
    try:
        db.open_guild_match_window(founder["token"], guild["id"], "window")
        raise AssertionError("second open window accepted")
    except Exception as exc:
        assert "one at a time" in str(exc), exc
    # Wash: deposits land after the window opens, then most is withdrawn;
    # the match pays on the net remainder (500 - 40 = 460u -> 92u at 20%),
    # not the gross deposits.
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    db.guild_withdraw(founder["token"], guild["id"], 2.0)
    # Upkeep dues ride kind 'deposit' for the shares math but are dues,
    # not deposits: five 5u arrears weeks paid mid-window must not move
    # the match net (gross would read 485u -> 97u).
    import db._guilds_lending as _gl

    with db._conn() as conn:
        for week in ("2026-W30", "2026-W31", "2026-W32", "2026-W33", "2026-W34"):
            conn.execute(
                "INSERT INTO guild_fee_arrears (guild_id, member_agent_id,"
                " week, units, status) VALUES (?, ?, ?, 5, 'open')",
                (guild["id"], mate["agent_id"], week),
            )
        cur = conn.execute(
            "INSERT INTO invoices (payer_agent_id, created_by_agent_id,"
            " amount_units, remaining_units, reason, status, due_at)"
            " VALUES (?, ?, 25, 25, 'upkeep catch-up', 'accepted', ?)",
            (mate["agent_id"], founder["agent_id"], "2026-09-24T00:00:00.000Z"),
        )
        fee_inv = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO guild_fee_invoices (invoice_id, guild_id,"
            " member_agent_id, week) VALUES (?, ?, ?, '2026-W34')",
            (fee_inv, guild["id"], mate["agent_id"]),
        )
    db.pay_invoice(mate["token"], fee_inv)
    with db._conn() as conn:
        wrow = conn.execute(
            "SELECT created_at FROM guild_match_windows WHERE id = ?",
            (window["window_id"],),
        ).fetchone()
        assert _gl._window_net(conn, guild["id"], wrow["created_at"]) == 460
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_match_windows SET ends_at = ? WHERE id = ?",
            ("2026-09-01T00:00:00.000Z", window["window_id"]),
        )
    report = db.sweep_guild_lending()
    paid = [m for m in report["matches"] if m["window_id"] == window["window_id"]]
    assert paid and paid[0]["status"] == "paid", report
    assert paid[0]["amount_units"] == 92, paid
    with db._conn() as conn:
        row = conn.execute(
            "SELECT amount_units FROM guild_match_windows WHERE id = ?",
            (window["window_id"],),
        ).fetchone()
    assert row is not None and row["amount_units"] == 92, dict(row or {})


def test_delinquency_freeze_and_repay_and_upkeep_guard():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    out = db.request_guild_subsidy(founder["token"], guild["id"], 1.0, True, "bridge")
    _backdate_debt_due(out["debt_id"], "2026-09-01T00:00:00.000Z")
    db.sweep_guild_lending()
    with db._conn() as conn:
        grow = conn.execute(
            "SELECT spending_suspended, suspend_reason FROM guilds WHERE id = ?",
            (guild["id"],),
        ).fetchone()
    assert grow is not None and grow["spending_suspended"] == 1, dict(grow or {})
    assert grow["suspend_reason"] == "delinquent", dict(grow)
    # Frozen: spends refuse, deposits still land.
    try:
        db.guild_withdraw(founder["token"], guild["id"], 1.0)
        raise AssertionError("delinquent spend passed")
    except Exception as exc:
        assert "suspend" in str(exc) or "frozen" in str(exc) or "lock" in str(exc), exc
    db.guild_deposit(mate["token"], guild["id"], 1.0)
    # Upkeep recovery must not clear a delinquent freeze.
    with db._conn() as conn:
        conn.execute(
            "UPDATE guilds SET last_upkeep_week = '2020-W01' WHERE id = ?",
            (guild["id"],),
        )
    db.sweep_guild_upkeep()
    with db._conn() as conn:
        grow = conn.execute(
            "SELECT spending_suspended, suspend_reason FROM guilds WHERE id = ?",
            (guild["id"],),
        ).fetchone()
    assert grow is not None and grow["spending_suspended"] == 1, dict(grow or {})
    assert grow["suspend_reason"] == "delinquent", dict(grow)
    # Repay in full: settled debt refreshes the freeze.
    _accept_invoice(founder, out["invoice_id"])
    _fund(founder["agent_id"], 200)
    db.pay_invoice(founder["token"], out["invoice_id"])
    with db._conn() as conn:
        grow = conn.execute(
            "SELECT spending_suspended FROM guilds WHERE id = ?",
            (guild["id"],),
        ).fetchone()
    assert grow is not None and grow["spending_suspended"] == 0, dict(grow or {})


def test_seize_waterfall_full_and_partial():
    # Full cover: pool 700u vs 20u debt - debt settles, rest disbands away.
    founder, guild = _found()
    _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    out = db.request_guild_subsidy(founder["token"], guild["id"], 1.0, True, "doomed")
    _backdate_debt_due(out["debt_id"], "2020-01-01T00:00:00.000Z")
    db.sweep_guild_lending()  # overdue + freeze
    db.sweep_guild_lending()  # past due+window: seize + disband
    with db._conn() as conn:
        grow = conn.execute(
            "SELECT status FROM guilds WHERE id = ?", (guild["id"],)
        ).fetchone()
        debt = conn.execute(
            "SELECT status, remaining_units FROM guild_debts WHERE id = ?",
            (out["debt_id"],),
        ).fetchone()
        members = conn.execute(
            "SELECT COUNT(*) FROM guild_members WHERE guild_id = ?",
            (guild["id"],),
        ).fetchone()[0]
    assert grow is not None and grow["status"] == "disbanded", dict(grow or {})
    assert debt is not None and debt["status"] == "settled", dict(debt or {})
    assert members == 0
    assert _pool(guild["id"]) == 0
    # Partial: pool spent below the debts via an escrow-exempt
    # commission (velocity caps plain withdrawals ~30%, and the subsidy
    # itself funds the pool it draws against). Deposits 5+5u, two 20u
    # subsidies (cooldown stood down), 15u escrowed job completes: pool
    # 50-15 = 35u vs 40u debts - first settles 20u, second seizes 15u and
    # writes off 5u.
    founder2, guild2 = _found()
    _mate(founder2, guild2, prefix="gl-m2", deposit_cr=0.25)
    db.guild_deposit(founder2["token"], guild2["id"], 0.25)
    out_a = db.request_guild_subsidy(
        founder2["token"], guild2["id"], 1.0, True, "first"
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_subsidies SET created_at = ? WHERE id = ?",
            ("2026-08-01T00:00:00.000Z", out_a["subsidy_id"]),
        )
    out2 = db.request_guild_subsidy(
        founder2["token"], guild2["id"], 1.0, True, "doomed too"
    )
    assert out_a["status"] == "paid" and out2["status"] == "paid"
    cos = db.request_guild_cosign(founder2["token"], guild2["id"], "big-job", 15)
    db.confirm_guild_cosign(founder2["token"], cos["cosign_id"])
    job = db.create_job(
        founder2["token"],
        "Big job",
        "spend it",
        0.75,
        ["x"],
        guild_id=guild2["id"],
    )
    worker = _new_agent("gl-worker")
    _fund(worker["agent_id"], 200)
    db.claim_job(worker["token"], job["job_id"])
    live = db.get_job(job["job_id"])
    for step in live["steps"]:
        db.tick_job_step(worker["token"], job["job_id"], step["id"], True)
    db.submit_job(worker["token"], job["job_id"], "done")
    db.review_job(founder2["token"], job["job_id"], "accept", "")
    assert _pool(guild2["id"]) == 35, _pool(guild2["id"])
    _backdate_debt_due(out2["debt_id"], "2020-01-01T00:00:00.000Z")
    db.sweep_guild_lending()
    db.sweep_guild_lending()
    with db._conn() as conn:
        debt2 = conn.execute(
            "SELECT status, remaining_units FROM guild_debts WHERE id = ?",
            (out2["debt_id"],),
        ).fetchone()
        evts = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'guild_debt_written_off'"
        ).fetchone()[0]
        gone = conn.execute(
            "SELECT status FROM guilds WHERE id = ?", (guild2["id"],)
        ).fetchone()
    assert debt2 is not None and debt2["status"] == "written_off", dict(debt2 or {})
    assert debt2["remaining_units"] == 5, dict(debt2)
    assert gone is not None and gone["status"] == "disbanded", dict(gone or {})
    assert evts >= 1


def test_voluntary_disband_refuses_open_debts():
    founder, guild = _found()
    _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    out = db.request_guild_subsidy(founder["token"], guild["id"], 1.0, True, "bridge")
    try:
        db.disband_guild(founder["token"], guild["id"], "dissolve")
        raise AssertionError("debt-laden dissolve passed")
    except Exception as exc:
        assert "debt" in str(exc), exc
    _accept_invoice(founder, out["invoice_id"])
    _fund(founder["agent_id"], 200)
    db.pay_invoice(founder["token"], out["invoice_id"])
    done = db.disband_guild(founder["token"], guild["id"], "dissolve")
    assert done["mode"] == "dissolve", done


def test_forfeit_split_and_founder_succession():
    founder, guild = _found()
    gid = guild["id"]
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], gid, 25.0)
    pool_before = _pool(gid)
    supply_before = _supply()
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2099-01-01T00:00:00.000Z", mate["agent_id"]),
        )
    report = db.sweep_guild_lending()
    assert mate["agent_id"] in report["forfeited"], report
    # Mate net 200u on a 700u pool: pro-rata min(200, 700*200//700)=200u;
    # memo extinguishes 200, burn takes 100, pool keeps the parked 100.
    assert _pool(gid) == pool_before - 200, (_pool(gid), pool_before)
    assert _supply() == supply_before - 100, "burn must destroy supply"
    with db._conn() as conn:
        gone = conn.execute(
            "SELECT 1 FROM guild_members WHERE guild_id = ? AND agent_id = ?",
            (gid, mate["agent_id"]),
        ).fetchone()
        burn = conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE reason = 'forfeit_burned'"
        ).fetchone()[0]
    assert gone is None, "forfeited member must be released"
    assert int(burn or 0) <= -20, burn
    # Founder suspended: heir inherits first, ex-founder forfeits after.
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2099-01-01T00:00:00.000Z", founder["agent_id"]),
        )
    report = db.sweep_guild_lending()
    assert founder["agent_id"] in report["forfeited"], report
    with db._conn() as conn:
        grow = conn.execute(
            "SELECT founder_agent_id, status FROM guilds WHERE id = ?", (gid,)
        ).fetchone()
    # Mate is suspended, so no heir qualifies (both suspended) ->
    # disbanded without paying the ex-founder.
    assert grow is not None and grow["status"] == "disbanded", dict(grow or {})


def test_disband_releases_guild_stakes():
    founder, guild = _found()
    gid = guild["id"]
    _mate(founder, guild)
    db.guild_deposit(founder["token"], gid, 25.0)
    sponsor = _new_agent("gl-sponsor")
    post = db.create_post(sponsor["token"], "Stake prop", "Body text here.")
    for name in ("beta", "gamma", "delta", "epsilon", "zeta"):
        db.vote(AGENTS[name]["token"], "post", post["post_id"], 1)
    prop = db.create_proposal(sponsor["token"], "Stake Prop L", "Body")
    pid = prop["post_id"]
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
    # A stranger's personal stake locks the same shared PR number:
    # disband must not touch it. Created outside the seed transaction
    # below (registering opens its own write txn - never nest writes).
    outsider = _new_agent("gl-outsider")
    _fund(outsider["agent_id"], 500)
    with db._conn() as conn:
        cur = conn.execute(
            "INSERT INTO proposal_stakes (proposal_id, staker_agent_id,"
            " per_pr, max_prs, currency, status) VALUES (?, ?, 20, 1,"
            " 'credits', 'active')",
            (pid, founder["agent_id"]),
        )
        stake_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO stake_locks (stake_id, pr_number, agent_id, amount,"
            " status) VALUES (?, 99991, ?, 20, 'locked')",
            (stake_id, founder["agent_id"]),
        )
        conn.execute(
            "INSERT INTO guild_stake_links (stake_id, guild_id,"
            " opener_bonus_pct) VALUES (?, ?, 0)",
            (stake_id, gid),
        )
        cur = conn.execute(
            "INSERT INTO proposal_stakes (proposal_id, staker_agent_id,"
            " per_pr, max_prs, currency, status) VALUES (?, ?, 10, 1,"
            " 'credits', 'active')",
            (pid, outsider["agent_id"]),
        )
        ostr = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO stake_locks (stake_id, pr_number, agent_id, amount,"
            " status) VALUES (?, 99991, ?, 10, 'locked')",
            (ostr, outsider["agent_id"]),
        )
    done = db.disband_guild(founder["token"], gid, "dissolve")
    assert done["mode"] == "dissolve", done
    with db._conn() as conn:
        link = conn.execute(
            "SELECT 1 FROM guild_stake_links WHERE stake_id = ?", (stake_id,)
        ).fetchone()
        lock = conn.execute(
            "SELECT status FROM stake_locks WHERE stake_id = ?", (stake_id,)
        ).fetchone()
        stranger = conn.execute(
            "SELECT status FROM stake_locks WHERE stake_id = ?", (ostr,)
        ).fetchone()
    assert link is None, "stake link must dissolve with the guild"
    assert lock is not None and lock["status"] == "refunded", dict(lock or {})
    assert stranger is not None and stranger["status"] == "locked", dict(stranger or {})


def test_shared_budget_and_sweep_quiet():
    founder, guild = _found()
    _mate(founder, guild)
    # Drain the file-cumulative window so the armed small budget below
    # measures exactly this test's two subsidies plus the third refusal.
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_subsidies SET decided_at = ?"
            " WHERE status IN ('paid', 'settled')",
            ("2026-08-01T00:00:00.000Z",),
        )
        conn.execute(
            "UPDATE guild_tranches SET released_at = ? WHERE status = 'released'",
            ("2026-08-01T00:00:00.000Z",),
        )
        conn.execute(
            "UPDATE guild_match_windows SET settled_at = ? WHERE status = 'paid'",
            ("2026-08-01T00:00:00.000Z",),
        )
    old = _arm("FORUM_GUILD_GRANT_BUDGET", "4.0")
    try:
        first = db.request_guild_subsidy(
            founder["token"], guild["id"], 1.0, False, "one"
        )
        assert first["status"] == "paid", first
        # The 14d auto clock: a backdated first row frees the second.
        with db._conn() as conn:
            conn.execute(
                "UPDATE guild_subsidies SET created_at = ? WHERE id = ?",
                ("2026-08-01T00:00:00.000Z", first["subsidy_id"]),
            )
        second = db.request_guild_subsidy(
            founder["token"], guild["id"], 1.0, True, "two"
        )
        assert second["status"] == "paid", second
        # The 14d auto clock still bites an immediate third request.
        try:
            db.request_guild_subsidy(
                founder["token"], guild["id"], 1.0, True, "three-early"
            )
            raise AssertionError("clock-busted subsidy paid")
        except Exception as exc:
            assert "14d" in str(exc), exc
        # 8q of 16q spent: a 3cr (12q) third breaches the shared window.
        try:
            db.request_guild_subsidy(founder["token"], guild["id"], 3.0, True, "three")
            raise AssertionError("budget-busted subsidy paid")
        except Exception as exc:
            assert "budget" in str(exc), exc
    finally:
        _unarm(old, "FORUM_GUILD_GRANT_BUDGET")
    report = db.sweep_guild_lending()
    assert set(report) == {
        "matches",
        "overdue",
        "seized",
        "forfeited",
        "skipped",
    }, report


# -- run all --
if __name__ == "__main__":
    test_tables_upgrade_and_kinds()
    test_request_auto_pays_and_conservation()
    test_request_payback_mints_debt_and_part_pay()
    test_over_tier_venue_and_admin_decide()
    test_second_needs_payback_and_overdue_blocks()
    test_match_lump_and_window_wash()
    test_delinquency_freeze_and_repay_and_upkeep_guard()
    test_seize_waterfall_full_and_partial()
    test_voluntary_disband_refuses_open_debts()
    test_forfeit_split_and_founder_succession()
    test_disband_releases_guild_stakes()
    test_shared_budget_and_sweep_quiet()
    print("\n== test_guilds_lending: all passed ==")
