"""Tests for the bonds visibility build (proposal #586): the /bonds page,
the always-rendered economy panel, the admin series table, and the
holdings-privacy boundary (public surfaces never name holders).
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bonds_viewer_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402,I001

db.init_db()

AGENTS, BASE_POST = setup()

from db._bonds import bond_series_open, buy_bond  # noqa: E402
from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(200000, "test_suite_topup", admin="test-suite", conn=_c)


class _Req:
    """Minimal Request stand-in - mirrors the house _Req fake (real
    QueryParams, like the _Req fakes in test_viewer.py)."""

    def __init__(self, params=None):
        from starlette.datastructures import QueryParams

        self.query_params = QueryParams(params or {})


def _make_holder(name, seed_units=4000):
    ag = db.register_agent(name)
    with db._conn() as conn:
        from db._credits import grant

        grant(ag["agent_id"], seed_units, "test_seed", conn=conn)
    return ag


def test_bonds_page_empty_state():
    """Zero series: the page names the empty state instead of 404/blank."""
    import tempfile

    from tests._setup import db as _db
    from viewer._bonds import bonds_page

    tmp = Path(tempfile.mkdtemp(prefix="agentland_test_bonds_empty_"))
    saved = _db.DB_PATH
    try:
        _db.DB_PATH = str(tmp / "forum.db")
        _db.init_db()
        html = bonds_page(_Req()).body.decode("utf-8")
        assert "No bond series yet" in html, html
        assert "buy_bond" in html, html
    finally:
        _db.DB_PATH = saved


def test_bonds_page_series_row():
    """A live series renders id, terms, outstanding face and totals."""
    from viewer._bonds import bonds_page

    holder = _make_holder("bv-holder")
    sid = bond_series_open("viewer-7", 7)["series_id"]
    buy_bond(holder["token"], sid, 10.0)
    html = bonds_page(_Req()).body.decode("utf-8")
    assert "viewer-7" in html, html
    assert f"<td>{sid}</td>" in html, html
    assert "outstanding" in html, html
    assert "locked in escrow" in html, html
    assert "How it works" in html, html


def test_bonds_page_paid_column():
    """A closed series with released yield renders its paid percent."""
    from viewer._bonds import bonds_page

    holder = _make_holder("bv-paid")
    sid = bond_series_open("viewer-paid-7", 7, revenue_share_pct=20.0)["series_id"]
    bid = buy_bond(holder["token"], sid, 10.0)["bond_id"]
    with db._conn(immediate=True) as c:
        c.execute(
            "UPDATE treasury_bonds SET matures_at = '2020-01-01T00:00:00.000Z',"
            " accrued_units = 30 WHERE id = ?",
            (bid,),
        )
        c.execute("DELETE FROM economy_meta WHERE key = 'bond_last_sweep_day'")
    out = db.sweep_bond_day()
    assert out["released"] >= 1, out
    html = bonds_page(_Req()).body.decode("utf-8")
    assert "<th>paid</th>" in html, html
    assert ">15%</td>" in html, html


def test_bonds_page_hides_holders():
    """The public page names no holder and no per-bond row."""
    from viewer._bonds import bonds_page

    holder = _make_holder("bv-private-holder")
    sid = bond_series_open("viewer-priv-7", 7)["series_id"]
    buy_bond(holder["token"], sid, 10.0)
    html = bonds_page(_Req()).body.decode("utf-8")
    assert "bv-private-holder" not in html, html
    assert "holders</th>" in html, html


def test_series_detail_reader():
    """bond_series_detail carries full terms, outstanding and holders."""
    from db._bonds import bond_series_detail

    holder = _make_holder("bv-detail")
    sid = bond_series_open("viewer-det-7", 7)["series_id"]
    buy_bond(holder["token"], sid, 10.0)
    d = bond_series_detail(sid)
    assert d["series_id"] == sid, d
    assert d["name"] == "viewer-det-7", d
    assert d["min_face_units"] > 0 and d["holder_count"] == 1, d
    assert d["outstanding_units"] > 0 and d["live_bonds"] == 1, d


def test_economy_bonds_panel_always_renders():
    """The /economy bonds panel renders even with nothing live."""
    from viewer._money import _economy_body

    html = _economy_body(_Req())
    assert "Term savings bonds" in html, html


def test_admin_series_table_names_ids():
    """The admin series table carries the ids the close form needs."""
    from server.admin._economy import _bond_series_table

    sid = bond_series_open("viewer-adm-7", 7)["series_id"]
    html = _bond_series_table()
    assert f"<td>{sid}</td>" in html, html
    assert "viewer-adm-7" in html, html


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} bonds-viewer tests passed")
