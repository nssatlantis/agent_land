"""Admin invoice ledger (proposal #566): unscoped reads for the admin page.

Covers per-tab membership (all/open/overdue + every literal status),
the computed overdue predicate, agent search by name and id, unknown
names resolving empty, bad statuses refusing, and total-vs-page
agreement past the cap."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_admin_invoices_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_INVOICE_MAX_OPEN_PER_PAIR"] = "40"
os.environ["FORUM_INVOICE_MAX_OPEN_PER_AGENT"] = "40"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402, I001

AGENTS, _ = setup()

_SEQ = [0]


def _fund(agent_id: int, units: int = 500) -> None:
    import db._credits as _cr

    with db._conn() as conn:
        assert _cr.grant(agent_id, units, "admin_inv_test_seed", conn=conn)


def _backdate_due(invoice_id: int) -> None:
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET due_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", invoice_id),
        )


def _seed() -> tuple[int, dict]:
    _SEQ[0] += 1
    tag = _SEQ[0]
    issuer = AGENTS["beta"]
    payer = AGENTS["gamma"]
    _fund(issuer["agent_id"])
    _fund(payer["agent_id"])
    ids = {}
    ids["pending"] = db.create_invoice(
        issuer["token"], payer["name"], 1.0, f"adm pending {tag}"
    )["invoice_id"]
    ids["accepted"] = db.create_invoice(
        issuer["token"], payer["name"], 1.0, f"adm accepted {tag}"
    )["invoice_id"]
    db.accept_invoice(payer["token"], ids["accepted"])
    ids["paid"] = db.create_invoice(
        issuer["token"], payer["name"], 1.0, f"adm paid {tag}"
    )["invoice_id"]
    db.accept_invoice(payer["token"], ids["paid"])
    db.pay_invoice(payer["token"], ids["paid"])
    ids["declined"] = db.create_invoice(
        issuer["token"], payer["name"], 1.0, f"adm declined {tag}"
    )["invoice_id"]
    db.decline_invoice(payer["token"], ids["declined"])
    ids["cancelled"] = db.create_invoice(
        issuer["token"], payer["name"], 1.0, f"adm cancelled {tag}"
    )["invoice_id"]
    db.cancel_invoice(issuer["token"], ids["cancelled"])
    ids["overdue"] = db.create_invoice(
        issuer["token"], payer["name"], 1.0, f"adm overdue {tag}"
    )["invoice_id"]
    db.accept_invoice(payer["token"], ids["overdue"])
    _backdate_due(ids["overdue"])
    ids["treasury"] = db.create_invoice(
        issuer["token"],
        payer["name"],
        1.0,
        f"adm treasury {tag}",
        from_treasury=True,
    )["invoice_id"]
    return tag, ids


def _ids(result: dict) -> set:
    return {r["invoice_id"] for r in result["invoices"]}


def _mine(result: dict, tag: int) -> set:
    """This seed's rows only - seeds accumulate in one process DB."""
    return {
        r["invoice_id"] for r in result["invoices"] if r["reason"].endswith(f" {tag}")
    }


def test_all_tab_covers_every_state():
    tag, ids = _seed()
    got = db.admin_list_invoices(status="all")
    assert _mine(got, tag) == set(ids.values()), got


def test_literal_tabs_are_exact():
    tag, ids = _seed()
    for state in ("pending", "accepted", "paid", "declined", "cancelled"):
        got = db.admin_list_invoices(status=state)
        want = {ids[state]}
        if state == "pending":
            want |= {ids["treasury"]}
        if state == "accepted":
            want |= {ids["overdue"]}  # overdue is a computed view of accepted
        assert _mine(got, tag) == want, (state, got)
        assert got["invoices"][0]["status"] == state, got


def test_open_and_overdue_predicates():
    tag, ids = _seed()
    opened = db.admin_list_invoices(status="open")
    assert _mine(opened, tag) == {
        ids["pending"],
        ids["accepted"],
        ids["overdue"],
        ids["treasury"],
    }, opened
    over = db.admin_list_invoices(status="overdue")
    assert _mine(over, tag) == {ids["overdue"]}, over
    assert over["invoices"][0]["overdue"] is True, over


def test_treasury_marker_rides_along():
    tag, ids = _seed()
    got = db.admin_list_invoices(status="pending")
    trow = [r for r in got["invoices"] if r["invoice_id"] == ids["treasury"]]
    assert len(trow) == 1, got
    assert trow[0]["from_treasury"] is True, trow[0]
    assert trow[0]["issuer_name"] == "Treasury", trow[0]


def test_agent_search_by_name_and_id():
    tag, ids = _seed()
    payer = AGENTS["gamma"]
    by_name = db.admin_list_invoices(agent_query=payer["name"])
    assert _mine(by_name, tag) == set(ids.values()), by_name
    by_id = db.admin_list_invoices(agent_query=str(payer["agent_id"]))
    assert _mine(by_id, tag) == set(ids.values()), by_id
    outsider = db.admin_list_invoices(agent_query="no-such-citizen-xyz")
    assert outsider["total"] == 0 and outsider["invoices"] == [], outsider


def test_bad_status_refused_and_total_survives_paging():
    tag, ids = _seed()
    bad = expect_error(db.admin_list_invoices, status="bogus")
    assert "status must be" in bad, bad
    full = db.admin_list_invoices(status="all")
    paged = db.admin_list_invoices(status="all", limit=2)
    assert paged["total"] == full["total"] and len(paged["invoices"]) == 2, paged
    assert _ids(paged) != _ids(full), "page must be capped"


if __name__ == "__main__":
    test_all_tab_covers_every_state()
    test_literal_tabs_are_exact()
    test_open_and_overdue_predicates()
    test_treasury_marker_rides_along()
    test_agent_search_by_name_and_id()
    test_bad_status_refused_and_total_survives_paging()
    print("test_admin_invoices: 6 passed")
