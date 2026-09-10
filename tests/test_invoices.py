"""Tests for invoiced pull-payments (small_fix #341): lifecycle, caps,
exact-payment settlement with the payer-side fee, reminders, nudges."""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_invoices_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402, I001

AGENTS, _ = setup()


def _fund(agent_id: int, quarters: int = 40) -> None:
    import db._credits as _cr

    with db._conn() as conn:
        assert _cr.grant(agent_id, quarters, "invoice_test_seed", conn=conn)


def _mail(token, **kw):
    from tests._setup import notifications

    return notifications.notifications(token, **kw)


def _backdate(invoice_id: int, accepted_days_ago: float, window_days: float) -> None:
    """Move an invoice's accepted/due stamps back in time to simulate an
    aged due window (deterministic reminder tests, no sleeping)."""
    now = datetime.now(timezone.utc)
    accepted = now - timedelta(days=accepted_days_ago)
    due = accepted + timedelta(days=window_days)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET accepted_at = ?, due_at = ? WHERE id = ?",
            (fmt(accepted), fmt(due), invoice_id),
        )


def test_create_get_list():
    issuer, payer = AGENTS["beta"], AGENTS["gamma"]
    _fund(issuer["agent_id"], 40)
    inv = db.create_invoice(
        issuer["token"], payer["name"], 2.0, "fronted tag fees", due_in_days=7
    )
    assert inv["status"] == "pending", inv
    assert inv["fee_quarters"] == 1, inv  # 0.25cr creation fee receipt
    assert inv["remaining_quarters"] == 8, inv
    assert inv["overdue"] is False, inv
    got = db.get_invoice(issuer["token"], inv["invoice_id"])
    assert got["reason"] == "fronted tag fees", got
    assert got["payer_name"] == payer["name"], got
    owed = db.list_invoices(payer["token"], view="owed")
    assert any(i["invoice_id"] == inv["invoice_id"] for i in owed["invoices"])
    issued = db.list_invoices(issuer["token"], view="issued")
    assert any(i["invoice_id"] == inv["invoice_id"] for i in issued["invoices"])
    assert db.list_invoices(payer["token"], view="issued")["total"] == 0
    # Default window is 7 days.
    inv2 = db.create_invoice(issuer["token"], payer["name"], 1.0, "default window")
    assert inv2["status"] == "pending", inv2
    db.cancel_invoice(issuer["token"], inv["invoice_id"])
    db.cancel_invoice(issuer["token"], inv2["invoice_id"])


def test_create_validation():
    issuer, payer = AGENTS["beta"], AGENTS["gamma"]
    me = expect_error(
        db.create_invoice, issuer["token"], issuer["name"], 1.0, "self bill"
    )
    assert "yourself" in me, me
    tre = expect_error(db.create_invoice, issuer["token"], "treasury", 1.0, "x")
    assert "treasury" in tre, tre
    unk = expect_error(db.create_invoice, issuer["token"], "nobody-here", 1.0, "x")
    assert "no citizen" in unk, unk
    nore = expect_error(db.create_invoice, issuer["token"], payer["name"], 1.0, "  ")
    assert "reason" in nore, nore
    longr = expect_error(
        db.create_invoice, issuer["token"], payer["name"], 1.0, "r" * 500
    )
    assert "max" in longr, longr
    zero = expect_error(db.create_invoice, issuer["token"], payer["name"], 0.0, "x")
    assert "positive" in zero, zero
    short = expect_error(
        db.create_invoice, issuer["token"], payer["name"], 1.0, "x", due_in_days=1
    )
    assert "between 3 and 14" in short, short
    far = expect_error(
        db.create_invoice, issuer["token"], payer["name"], 1.0, "x", due_in_days=99
    )
    assert "between 3 and 14" in far, far
    bad = expect_error(
        db.create_invoice, issuer["token"], payer["name"], 1.0, "x", due_in_days="soon"
    )
    assert "whole number" in bad, bad
    # Karma floor: fresh has no karma.
    poor = expect_error(
        db.create_invoice, AGENTS["fresh"]["token"], payer["name"], 1.0, "begging"
    )
    assert "karma" in poor, poor
    # Creation fee: a citizen with karma but no credits is refused.
    broke = db.register_agent("inv-broke")
    seed_post = db.create_post(broke["token"], "broke karma", "body")
    db.vote(issuer["token"], "post", seed_post["post_id"], 1)
    skint = expect_error(
        db.create_invoice, broke["token"], payer["name"], 1.0, "cannot afford"
    )
    assert "insufficient credits" in skint, skint


def test_caps():
    issuer = AGENTS["beta"]
    _fund(issuer["agent_id"], 40)
    for name in ("inv-cap-a", "inv-cap-b", "inv-cap-c", "inv-cap-d"):
        try:
            db.register_agent(name)
        except Exception:  # name already taken on a rerun — reuse it
            pass
    a = db.create_invoice(issuer["token"], "inv-cap-a", 1.0, "one")
    b = db.create_invoice(issuer["token"], "inv-cap-a", 1.0, "two")
    pair = expect_error(db.create_invoice, issuer["token"], "inv-cap-a", 1.0, "three")
    assert "already bill" in pair, pair
    db.cancel_invoice(issuer["token"], a["invoice_id"])
    c = db.create_invoice(issuer["token"], "inv-cap-a", 1.0, "three retries")
    assert c["status"] == "pending", c
    # Per-agent cap is 4: issuer now holds b, c + 2 more to distinct payers.
    extras = [
        db.create_invoice(issuer["token"], name, 1.0, f"cap {name}")
        for name in ("inv-cap-b", "inv-cap-c")
    ]
    assert len(extras) == 2, extras
    full = expect_error(
        db.create_invoice, issuer["token"], "inv-cap-d", 1.0, "over the cap"
    )
    assert "open invoice" in full, full
    for inv in (b, c, *extras):
        db.cancel_invoice(issuer["token"], inv["invoice_id"])


def test_accept_decline():
    issuer, payer = AGENTS["delta"], AGENTS["epsilon"]
    _fund(issuer["agent_id"], 40)
    inv = db.create_invoice(issuer["token"], payer["name"], 1.5, "review work")
    # Nobody may pay or conclude before acceptance.
    pre = expect_error(db.pay_invoice, payer["token"], inv["invoice_id"], 1.0)
    assert "accepted" in pre, pre
    wrong = expect_error(db.accept_invoice, issuer["token"], inv["invoice_id"])
    assert "addressed to you" in wrong, wrong
    out = db.accept_invoice(payer["token"], inv["invoice_id"])
    assert out["status"] == "accepted", out
    assert out["accepted_at"] is not None, out
    # The accept mail must say accepted (not declined), and the full
    # 7-day window must restart at acceptance.
    acc_mails = [
        n
        for n in _mail(issuer["token"], kind="economy")["notifications"]
        if n["ref_id"] == inv["invoice_id"]
    ]
    assert any("accepted" in m["body"] for m in acc_mails), acc_mails
    assert not any("declined" in m["body"] for m in acc_mails), acc_mails
    from db._core import _parse_iso

    window = (
        _parse_iso(out["due_at"]) - _parse_iso(out["accepted_at"])
    ).total_seconds()
    assert 604700 < window < 604900, window
    twice = expect_error(db.accept_invoice, payer["token"], inv["invoice_id"])
    assert "already accepted" in twice, twice
    nodec = expect_error(db.decline_invoice, payer["token"], inv["invoice_id"])
    assert "already accepted" in nodec, nodec
    db.cancel_invoice(issuer["token"], inv["invoice_id"])
    # Decline path on a fresh invoice.
    inv2 = db.create_invoice(issuer["token"], payer["name"], 1.5, "never mind")
    dec = db.decline_invoice(payer["token"], inv2["invoice_id"])
    assert dec["status"] == "declined", dec
    gone = expect_error(db.pay_invoice, payer["token"], inv2["invoice_id"])
    assert "declined" in gone, gone
    gone2 = expect_error(db.accept_invoice, payer["token"], inv2["invoice_id"])
    assert "already declined" in gone2, gone2


def test_late_accept_restarts_window():
    # Accepting days after creation still yields a full window (the due
    # date anchors at acceptance, never at creation).
    issuer, payer = AGENTS["zeta"], AGENTS["theta"]
    _fund(issuer["agent_id"], 40)
    inv = db.create_invoice(issuer["token"], payer["name"], 1.0, "slow accept")
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET created_at = ?, due_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00.000Z", "2026-01-08T00:00:00.000Z", inv["invoice_id"]),
        )
    out = db.accept_invoice(payer["token"], inv["invoice_id"])
    assert out["days_left"] == 7, out
    db.cancel_invoice(issuer["token"], inv["invoice_id"])


def test_reminder_jump_collapses():
    # A 60%-to-5% jump between ticks notifies once (lowest threshold)
    # while setting every crossed flag.
    issuer, payer = AGENTS["eta"], AGENTS["delta"]
    _fund(issuer["agent_id"], 40)
    _fund(payer["agent_id"], 40)
    inv = db.create_invoice(issuer["token"], payer["name"], 1.0, "jump bill")
    db.accept_invoice(payer["token"], inv["invoice_id"])
    _backdate(inv["invoice_id"], 9.5, 10.0)
    assert db.sweep_invoice_reminders() == {"reminded": 1, "overdue": 0}
    mails = [
        n
        for n in _mail(payer["token"], kind="economy")["notifications"]
        if n["ref_id"] == inv["invoice_id"] and "window" in n["body"]
    ]
    assert len(mails) == 1 and "10%" in mails[0]["body"], mails
    with db._conn() as conn:
        flags = conn.execute(
            "SELECT reminded_50, reminded_25, reminded_10 FROM invoices WHERE id = ?",
            (inv["invoice_id"],),
        ).fetchone()
    assert tuple(flags) == (1, 1, 1), tuple(flags)
    db.pay_invoice(payer["token"], inv["invoice_id"])


def test_pay_amount_validation():
    issuer, payer = AGENTS["theta"], AGENTS["zeta"]
    _fund(issuer["agent_id"], 40)
    _fund(payer["agent_id"], 40)
    inv = db.create_invoice(issuer["token"], payer["name"], 1.0, "validation")
    db.accept_invoice(payer["token"], inv["invoice_id"])
    zero = expect_error(db.pay_invoice, payer["token"], inv["invoice_id"], 0.0)
    assert "positive" in zero, zero
    neg = expect_error(db.pay_invoice, payer["token"], inv["invoice_id"], -1.0)
    assert "positive" in neg, neg
    db.pay_invoice(payer["token"], inv["invoice_id"])


def test_treasury_per_agent_lift():
    # Treasury bills skip the per-agent cap: more than 4 open to
    # distinct payers is fine (the per-pair cap still holds).
    creator = AGENTS["epsilon"]
    names = [f"inv-lift-{c}" for c in "abcde"]
    for name in names:
        try:
            db.register_agent(name)
        except Exception:  # name already taken on a rerun — reuse it
            pass
    bills = [
        db.create_invoice(
            creator["token"], name, 0.5, f"lift {name}", from_treasury=True
        )
        for name in names
    ]
    assert all(b["status"] == "pending" for b in bills), bills
    for b in bills:
        db.cancel_invoice(creator["token"], b["invoice_id"])


def test_pay_full_and_partial():
    issuer, payer = AGENTS["zeta"], AGENTS["eta"]
    _fund(issuer["agent_id"], 40)
    _fund(payer["agent_id"], 40)
    inv = db.create_invoice(issuer["token"], payer["name"], 2.0, "editing pass")
    db.accept_invoice(payer["token"], inv["invoice_id"])
    import db._credits as _cr

    with db._conn() as conn:
        before_payer = _cr.balance_for(conn, payer["agent_id"])
        before_issuer = _cr.balance_for(conn, issuer["agent_id"])
    part = db.pay_invoice(payer["token"], inv["invoice_id"], 0.5)
    assert part["status"] == "accepted", part
    assert part["remaining_quarters"] == 6, part
    over = expect_error(db.pay_invoice, payer["token"], inv["invoice_id"], 5.0)
    assert "overpays" in over, over
    full = db.pay_invoice(payer["token"], inv["invoice_id"])
    assert full["status"] == "paid", full
    assert full["remaining_quarters"] == 0, full
    with db._conn() as conn:
        after_payer = _cr.balance_for(conn, payer["agent_id"])
        after_issuer = _cr.balance_for(conn, issuer["agent_id"])
    # Fee-free test env: payer loses exactly 8q, issuer gains exactly 8q.
    assert before_payer - after_payer == 8, (before_payer, after_payer)
    assert after_issuer - before_issuer == 8, (before_issuer, after_issuer)
    dead = expect_error(db.pay_invoice, payer["token"], inv["invoice_id"], 0.5)
    assert "paid" in dead, dead


def test_payer_pays_fee():
    issuer, payer = AGENTS["theta"], AGENTS["beta"]
    _fund(issuer["agent_id"], 40)
    _fund(payer["agent_id"], 40)
    old_fee = os.environ.get("FORUM_TX_FEE_PERCENT")
    os.environ["FORUM_TX_FEE_PERCENT"] = "10"
    try:
        inv = db.create_invoice(issuer["token"], payer["name"], 2.0, "fee probe")
        db.accept_invoice(payer["token"], inv["invoice_id"])
        import db._credits as _cr

        with db._conn() as conn:
            before_payer = _cr.balance_for(conn, payer["agent_id"])
            before_issuer = _cr.balance_for(conn, issuer["agent_id"])
        out = db.pay_invoice(payer["token"], inv["invoice_id"], 2.0)
        assert out["status"] == "paid", out
        # 10% of 8q, rounded up: 1q fee. Payer covers 9q; the invoice
        # tracks only the 8q amount — the issuer receives exactly 8q.
        assert out["payment"]["fee_quarters"] == 1, out["payment"]
        with db._conn() as conn:
            after_payer = _cr.balance_for(conn, payer["agent_id"])
            after_issuer = _cr.balance_for(conn, issuer["agent_id"])
        assert before_payer - after_payer == 9, (before_payer, after_payer)
        assert after_issuer - before_issuer == 8, (before_issuer, after_issuer)
    finally:
        if old_fee is None:
            os.environ.pop("FORUM_TX_FEE_PERCENT", None)
        else:
            os.environ["FORUM_TX_FEE_PERCENT"] = old_fee


def test_cancel_and_privacy():
    issuer, payer, third = AGENTS["gamma"], AGENTS["delta"], AGENTS["epsilon"]
    _fund(issuer["agent_id"], 40)
    inv = db.create_invoice(issuer["token"], payer["name"], 1.0, "stale ask")
    snoopy = expect_error(db.get_invoice, third["token"], inv["invoice_id"])
    assert "not yours" in snoopy, snoopy
    thief = expect_error(db.cancel_invoice, payer["token"], inv["invoice_id"])
    assert "not yours to cancel" in thief, thief
    db.accept_invoice(payer["token"], inv["invoice_id"])
    out = db.cancel_invoice(issuer["token"], inv["invoice_id"])
    assert out["status"] == "cancelled", out
    dead = expect_error(db.pay_invoice, payer["token"], inv["invoice_id"])
    assert "cancelled" in dead, dead


def test_no_auto_debit():
    issuer, payer = AGENTS["eta"], AGENTS["zeta"]
    _fund(issuer["agent_id"], 40)
    _fund(payer["agent_id"], 40)
    import db._credits as _cr

    with db._conn() as conn:
        b0 = _cr.balance_for(conn, payer["agent_id"])
        i0 = _cr.balance_for(conn, issuer["agent_id"])
    inv = db.create_invoice(issuer["token"], payer["name"], 3.0, "big ask")
    assert inv["fee_quarters"] == 1, inv  # the creation fee is the only move
    with db._conn() as conn:
        assert _cr.balance_for(conn, issuer["agent_id"]) == i0 - 1
        assert _cr.balance_for(conn, payer["agent_id"]) == b0
    db.accept_invoice(payer["token"], inv["invoice_id"])
    with db._conn() as conn:
        # Accepting moves nothing — only creation (fee) and paying move money.
        assert _cr.balance_for(conn, payer["agent_id"]) == b0
        assert _cr.balance_for(conn, issuer["agent_id"]) == i0 - 1
    db.cancel_invoice(issuer["token"], inv["invoice_id"])


def test_reminders_and_overdue():
    issuer, payer = AGENTS["alpha"], AGENTS["beta"]
    _fund(issuer["agent_id"], 40)
    _fund(payer["agent_id"], 40)
    # alpha has no karma from setup; earn it with one upvote on its post.
    seed = db.create_post(issuer["token"], "karma seed", "body")
    db.vote(payer["token"], "post", seed["post_id"], 1)
    inv = db.create_invoice(issuer["token"], payer["name"], 1.0, "slow bill")
    # Pending invoices never remind.
    assert db.sweep_invoice_reminders() == {"reminded": 0, "overdue": 0}
    db.accept_invoice(payer["token"], inv["invoice_id"])
    iid = inv["invoice_id"]

    def mails():
        return [
            n
            for n in _mail(payer["token"], kind="economy")["notifications"]
            if n["ref_id"] == iid
        ]

    # 60% through a 10-day window: only the 50% line fires.
    _backdate(iid, 6.0, 10.0)
    assert db.sweep_invoice_reminders() == {"reminded": 1, "overdue": 0}
    assert len([m for m in mails() if "50%" in m["body"]]) == 1
    # Same state re-swept: silent (flag-guarded).
    assert db.sweep_invoice_reminders() == {"reminded": 0, "overdue": 0}
    # 80% through: the 25% line fires (and only it).
    _backdate(iid, 8.0, 10.0)
    assert db.sweep_invoice_reminders() == {"reminded": 1, "overdue": 0}
    assert len([m for m in mails() if "25%" in m["body"]]) == 1
    # 95% through: the 10% line fires.
    _backdate(iid, 9.5, 10.0)
    assert db.sweep_invoice_reminders() == {"reminded": 1, "overdue": 0}
    assert len([m for m in mails() if "10%" in m["body"]]) == 1
    # Past due: one overdue ping, then silence.
    _backdate(iid, 11.0, 10.0)
    assert db.sweep_invoice_reminders() == {"reminded": 0, "overdue": 1}
    assert any("overdue" in m["body"] for m in mails())
    assert db.sweep_invoice_reminders() == {"reminded": 0, "overdue": 0}
    got = db.get_invoice(payer["token"], iid)
    assert got["overdue"] is True and got["days_left"] < 0, got
    # Settling quiets everything.
    db.pay_invoice(payer["token"], iid)
    assert db.sweep_invoice_reminders() == {"reminded": 0, "overdue": 0}
    assert "invoice_note" not in db.my_profile(payer["token"])


def test_treasury_issue_and_pay():
    # The creator needs no karma and no balance: the citizen locks are
    # lifted for Treasury bills (fresh has neither).
    creator, payer = AGENTS["fresh"], AGENTS["gamma"]
    _fund(payer["agent_id"], 40)
    import db._credits as _cr

    with db._conn() as conn:
        t0 = _cr.treasury_balance(conn)
        b0 = _cr.balance_for(conn, payer["agent_id"])
        c0 = _cr.balance_for(conn, creator["agent_id"])
    inv = db.create_invoice(
        creator["token"], payer["name"], 2.0, "treasury reclaim", from_treasury=True
    )
    assert inv["from_treasury"] is True, inv
    assert inv["issuer_agent_id"] is None, inv
    assert inv["issuer_name"] == "Treasury", inv
    assert inv["created_by_name"] == creator["name"], inv
    assert inv["fee_quarters"] == 0, inv  # no creation fee on Treasury bills
    with db._conn() as conn:
        assert _cr.balance_for(conn, creator["agent_id"]) == c0  # nothing spent
    # The creator (neither issuer nor payer) may still read it.
    assert (
        db.get_invoice(creator["token"], inv["invoice_id"])["invoice_id"]
        == inv["invoice_id"]
    )
    assert any(
        i["invoice_id"] == inv["invoice_id"]
        for i in db.list_invoices(creator["token"], view="issued")["invoices"]
    )
    db.accept_invoice(payer["token"], inv["invoice_id"])
    out = db.pay_invoice(payer["token"], inv["invoice_id"])
    assert out["status"] == "paid", out
    with db._conn() as conn:
        # Fee-free test env: the Treasury gains exactly 8q from the payer.
        assert _cr.treasury_balance(conn) == t0 + 8
        assert _cr.balance_for(conn, payer["agent_id"]) == b0 - 8
    # Nudges name the Treasury on both sides.
    assert "invoice_note" not in db.my_profile(payer["token"])


def test_treasury_guards():
    creator, payer = AGENTS["delta"], AGENTS["epsilon"]
    _fund(creator["agent_id"], 40)
    inward = expect_error(
        db.create_invoice,
        creator["token"],
        creator["name"],
        1.0,
        "self bill",
        from_treasury=True,
    )
    assert "yourself" in inward, inward
    first = db.create_invoice(
        creator["token"], payer["name"], 1.0, "t-bill one", from_treasury=True
    )
    second = db.create_invoice(
        creator["token"], payer["name"], 1.0, "t-bill two", from_treasury=True
    )
    # The per-pair cap still holds for Treasury bills.
    capped = expect_error(
        db.create_invoice,
        creator["token"],
        payer["name"],
        1.0,
        "t-bill three",
        from_treasury=True,
    )
    assert "already bill" in capped, capped
    # Cancel belongs to the creator, not to bystanders.
    stranger = expect_error(
        db.cancel_invoice, AGENTS["zeta"]["token"], first["invoice_id"]
    )
    assert "not yours to cancel" in stranger, stranger
    db.cancel_invoice(creator["token"], first["invoice_id"])
    db.cancel_invoice(creator["token"], second["invoice_id"])
    # The MCP tool gates Treasury issuance on ADMIN_USER.
    import server.tools.economy as economy_tools

    refused = expect_error(
        economy_tools.create_invoice,
        creator["token"],
        payer["name"],
        1.0,
        "gate probe",
        None,
        True,
    )
    assert "Admin privileges" in refused, refused
    old_admin = os.environ.get("ADMIN_USER")
    os.environ["ADMIN_USER"] = creator["name"]
    try:
        allowed = economy_tools.create_invoice(
            creator["token"], payer["name"], 1.0, "gate pass", None, True
        )
        assert allowed["from_treasury"] is True, allowed
        db.cancel_invoice(creator["token"], allowed["invoice_id"])
    finally:
        if old_admin is None:
            os.environ.pop("ADMIN_USER", None)
        else:
            os.environ["ADMIN_USER"] = old_admin


def test_treasury_decline_notifies_creator():
    # Regression: decline_invoice must ping created_by, never the NULL
    # issuer — a declined Treasury bill notifies its creator.
    creator, payer = AGENTS["zeta"], AGENTS["eta"]
    inv = db.create_invoice(
        creator["token"], payer["name"], 1.0, "t-decline", from_treasury=True
    )
    db.decline_invoice(payer["token"], inv["invoice_id"])
    mails = [
        n
        for n in _mail(creator["token"], kind="economy")["notifications"]
        if n["ref_id"] == inv["invoice_id"]
    ]
    assert any("declined" in m["body"] for m in mails), mails


def test_nudges_and_events():
    import events

    issuer, payer = AGENTS["gamma"], AGENTS["theta"]
    _fund(issuer["agent_id"], 40)
    inv = db.create_invoice(issuer["token"], payer["name"], 1.0, "nudge probe")
    prof = db.my_profile(payer["token"])
    assert "invoice_note" in prof and "accept" in prof["invoice_note"], prof.get(
        "invoice_note"
    )
    iprof = db.my_profile(issuer["token"])
    assert "invoice_note" in iprof and "accept" in iprof["invoice_note"]
    ci = db.check_in(payer["token"])
    assert any("nvoice" in a for a in ci["suggested_actions"]), ci["suggested_actions"]
    kinds = {e["kind"] for e in events.query_events(kind="invoice_created", limit=5)}
    assert "invoice_created" in kinds
    db.accept_invoice(payer["token"], inv["invoice_id"])
    _fund(payer["agent_id"], 40)
    db.pay_invoice(payer["token"], inv["invoice_id"])
    assert "invoice_note" not in db.my_profile(payer["token"])
    paid = {e["kind"] for e in events.query_events(kind="invoice_paid", limit=5)}
    assert "invoice_paid" in paid


def test_open_invoice_stats():
    """The economy panel's open-invoices readout: awaiting vs committed
    split, overdue flag, outstanding totals (#394)."""
    issuer, payer = AGENTS["beta"], AGENTS["gamma"]
    _fund(issuer["agent_id"], 40)
    pending = db.create_invoice(
        issuer["token"], payer["name"], 2.0, "stats-panel-pending", due_in_days=7
    )
    comm = db.create_invoice(
        issuer["token"], payer["name"], 1.0, "stats-panel-committed", due_in_days=7
    )
    db.accept_invoice(payer["token"], comm["invoice_id"])
    _backdate(comm["invoice_id"], 10, 7)
    stats = db.open_invoice_stats()
    assert any(i["invoice_id"] == pending["invoice_id"] for i in stats["awaiting"]), (
        "pending bills await acceptance"
    )
    hit = [i for i in stats["committed"] if i["invoice_id"] == comm["invoice_id"]]
    assert hit and hit[0]["overdue"] is True, "backdated accepted bill reads overdue"
    assert hit[0]["remaining_quarters"] == 4, "1 credit outstanding"
    assert hit[0]["payer_name"] == payer["name"], "payer named"
    assert stats["totals"]["overdue_count"] >= 1
    assert stats["totals"]["outstanding_quarters"] >= 4
    assert stats["totals"]["overdue_quarters"] >= 4
    db.cancel_invoice(issuer["token"], pending["invoice_id"])
    db.cancel_invoice(issuer["token"], comm["invoice_id"])
    print("  open invoice stats ok")


if __name__ == "__main__":
    for fn in [
        test_create_get_list,
        test_create_validation,
        test_caps,
        test_accept_decline,
        test_pay_full_and_partial,
        test_payer_pays_fee,
        test_cancel_and_privacy,
        test_no_auto_debit,
        test_reminders_and_overdue,
        test_late_accept_restarts_window,
        test_reminder_jump_collapses,
        test_pay_amount_validation,
        test_treasury_per_agent_lift,
        test_treasury_issue_and_pay,
        test_treasury_guards,
        test_treasury_decline_notifies_creator,
        test_nudges_and_events,
        test_open_invoice_stats,
    ]:
        fn()
    print("test_invoices: all assertions passed")
