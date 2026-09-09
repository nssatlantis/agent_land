"""Tests for the declined-PR Treasury fine (maintainer-supervised):
issue_pr_decline_fine bills the PR opener a Treasury invoice on the
first decline record; handled refusals skip without raising."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_pr_fine_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

AGENTS, _ = setup()


def _fine(pr_number: int, agent_id: int, **kw):
    with db._conn() as conn:
        return db.issue_pr_decline_fine(conn, pr_number, agent_id, **kw)


def _mail(token, **kw):
    from tests._setup import notifications

    return notifications.notifications(token, **kw)


def _open_treasury_bills(agent_id: int, n: int):
    """Create n open Treasury invoices to *agent_id* via the token path
    (db-level, no ADMIN_USER gate), so the fine's pair-cap check has
    something to trip on."""
    creator = AGENTS["zeta"]
    bills = []
    for i in range(n):
        bills.append(
            db.create_invoice(
                creator["token"],
                AGENTS_by_id(agent_id)["name"],
                1.0,
                f"t-bill {i}",
                from_treasury=True,
            )
        )
    return bills


def AGENTS_by_id(agent_id: int):
    for _a in AGENTS.values():
        if _a["agent_id"] == agent_id:
            return _a
    raise AssertionError(f"no setup agent with id {agent_id}")


def _set_knob(value: str | None):
    if value is None:
        os.environ.pop("FORUM_PR_DECLINE_FINE_CREDITS", None)
    else:
        os.environ["FORUM_PR_DECLINE_FINE_CREDITS"] = value


def _set_admin(name: str | None):
    if name is None:
        os.environ.pop("ADMIN_USER", None)
    else:
        os.environ["ADMIN_USER"] = name


def test_fine_off_when_zero():
    opener, creator = AGENTS["gamma"], AGENTS["beta"]
    _set_knob("0")
    _set_admin(creator["name"])
    try:
        out = _fine(900001, opener["agent_id"])
        assert out == {"issued": False, "skip": "off"}, out
        with db._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        assert n == 0
    finally:
        _set_knob(None)
        _set_admin(None)


def test_fine_skips_amount():
    opener, creator = AGENTS["gamma"], AGENTS["beta"]
    _set_knob("0.3")  # not whole/half/quarter -> exact_from_credits refuses
    _set_admin(creator["name"])
    try:
        assert _fine(900002, opener["agent_id"])["skip"] == "amount"
    finally:
        _set_knob(None)
        _set_admin(None)


def test_fine_skips_without_creator():
    opener = AGENTS["gamma"]
    _set_knob("0.5")
    _set_admin(None)
    try:
        assert _fine(900003, opener["agent_id"])["skip"] == "no_creator"
    finally:
        _set_knob(None)


def test_fine_skips_creator_is_payer():
    payer = AGENTS["gamma"]
    _set_knob("0.5")
    _set_admin(payer["name"])
    try:
        assert _fine(900004, payer["agent_id"])["skip"] == "creator_is_payer"
    finally:
        _set_knob(None)
        _set_admin(None)


def test_fine_skips_payer_unavailable():
    opener = AGENTS["gamma"]
    _set_knob("0.5")
    _set_admin(AGENTS["beta"]["name"])
    try:
        with db._conn() as conn:
            conn.execute(
                "UPDATE agents SET status = 'suspended' WHERE id = ?",
                (opener["agent_id"],),
            )
        try:
            assert _fine(900005, opener["agent_id"])["skip"] == "payer_unavailable"
        finally:
            with db._conn() as conn:
                conn.execute(
                    "UPDATE agents SET status = 'active' WHERE id = ?",
                    (opener["agent_id"],),
                )
    finally:
        _set_knob(None)
        _set_admin(None)


def test_fine_skips_pair_cap():
    opener = AGENTS["gamma"]
    _set_knob("0.5")
    _set_admin(AGENTS["beta"]["name"])
    try:
        bills = _open_treasury_bills(opener["agent_id"], 2)
        try:
            out = _fine(900006, opener["agent_id"])
            assert out["skip"] == "pair_cap", out
        finally:
            for b in bills:
                db.cancel_invoice(AGENTS["zeta"]["token"], b["invoice_id"])
    finally:
        _set_knob(None)
        _set_admin(None)


def test_fine_issues_treasury_bill():
    opener, creator = AGENTS["gamma"], AGENTS["beta"]
    _set_knob("0.5")
    _set_admin(creator["name"])
    import events

    try:
        import db._credits as _cr

        with db._conn() as conn:
            t0 = _cr.treasury_balance(conn)
            b0 = _cr.balance_for(conn, opener["agent_id"])

        out = _fine(900007, opener["agent_id"])
        assert out["issued"] is True and out["skip"] is None, out
        inv = out["invoice"]
        assert inv["from_treasury"] is True, inv
        assert inv["issuer_agent_id"] is None and inv["issuer_name"] == "Treasury", inv
        assert inv["created_by_name"] == creator["name"], inv
        assert inv["payer_agent_id"] == opener["agent_id"], inv
        assert inv["amount_quarters"] == 2 and inv["remaining_quarters"] == 2, inv
        assert inv["status"] == "pending", inv
        assert inv["fee_quarters"] == 0, inv
        assert f"#{900007}" in inv["reason"] and "Treasury" in inv["reason"], inv
        # Issuance moves nothing (no fee, no auto-debit).
        with db._conn() as conn:
            assert _cr.treasury_balance(conn) == t0
            assert _cr.balance_for(conn, opener["agent_id"]) == b0
        # The payer is notified and sees it owed; the creator sees it issued.
        mails = [
            n
            for n in _mail(opener["token"], kind="economy")["notifications"]
            if n["ref_id"] == inv["invoice_id"]
        ]
        assert len(mails) == 1 and "accept" in mails[0]["body"], mails
        assert any(
            i["invoice_id"] == inv["invoice_id"]
            for i in db.list_invoices(opener["token"], view="owed")["invoices"]
        )
        assert any(
            i["invoice_id"] == inv["invoice_id"]
            for i in db.list_invoices(creator["token"], view="issued")["invoices"]
        )
        # The event ledger names the PR and the Treasury provenance.
        evs = events.query_events(
            kind="invoice_created", target_type="invoice", target_id=inv["invoice_id"]
        )
        assert len(evs) == 1, evs
        det = evs[0]["detail"]
        assert det["pr_number"] == 900007 and det["from_treasury"] is True, det
        assert det["created_by"] == creator["name"], det
        assert det["to_agent_id"] == opener["agent_id"], det
        assert det["credits"] == "0.5" and det["delta_quarters"] == 2, det
        # The payer can decline it (bills nothing) and the creator is pinged.
        db.decline_invoice(opener["token"], inv["invoice_id"])
        creator_mail = [
            n
            for n in _mail(creator["token"], kind="economy")["notifications"]
            if n["ref_id"] == inv["invoice_id"]
        ]
        assert any("declined" in m["body"] for m in creator_mail), creator_mail
    finally:
        _set_knob(None)
        _set_admin(None)


def test_poller_fires_fine_once_on_first_decline():
    """The full closed-PR path: a declined PR bills the opener exactly
    once (the record_pr_decline once-guard halts replays before the fine
    can double-issue)."""
    opener, creator = AGENTS["gamma"], AGENTS["beta"]
    _set_knob("0.5")
    _set_admin(creator["name"])
    import events
    from server.poller import _process_closed_pr

    pr_num = 900010
    pr_dict = {
        "number": pr_num,
        "declined": True,
        "closed_at": "2026-09-09T00:00:00.000Z",
        "decline_reason": "lacks the proposal stamp",
        "citizen": {"name": opener["name"], "agent_id": opener["agent_id"]},
    }
    with db._conn() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE payer_agent_id = ?",
            (opener["agent_id"],),
        ).fetchone()[0]
    try:
        _process_closed_pr(dict(pr_dict))
        # Replay the same closed-PR row on the next poll tick: the decline
        # is already recorded, so no second fine is issued.
        _process_closed_pr(dict(pr_dict))
    finally:
        _set_knob(None)
        _set_admin(None)

    with db._conn() as conn:
        rows = conn.execute(
            "SELECT id FROM invoices WHERE payer_agent_id = ?", (opener["agent_id"],)
        ).fetchall()
        assert len(rows) == before + 1, rows
        rec = conn.execute(
            "SELECT status FROM pr_record WHERE pr_number = ?", (pr_num,)
        ).fetchone()
        assert rec is not None and rec["status"] == "declined", rec
        declined = conn.execute(
            "SELECT 1 FROM events WHERE kind = ? AND target_type = 'pr'"
            " AND target_id = ?",
            ("pr_declined", pr_num),
        ).fetchone()
        assert declined is not None
        invoice_created = conn.execute(
            "SELECT detail FROM events WHERE kind = 'invoice_created'"
            " AND target_type = 'invoice' AND target_id = ?",
            (rows[0]["id"],),
        ).fetchone()
        import json as _json

        det = _json.loads(invoice_created["detail"])
        assert det["pr_number"] == pr_num and det["from_treasury"] is True, det

    # The decline event carried the reason; the fine's reason names the PR.
    evs = events.query_events(kind="pr_declined", target_type="pr", target_id=pr_num)
    assert evs and evs[0]["detail"].get("decline_reason") == "lacks the proposal stamp"
