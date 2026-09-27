"""Admin guild grant decision surface (proposal #782).

The panel is the second surface onto a decision engine that already
existed. These pins cover the parts that are NEW - the route, the panel
render and the operator-error guard - and are honest about which parts
they do not reach (see the note at the bottom).
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from server.admin import _guilds as ag  # noqa: E402

# A grant request row as the panel sees it. `requested` is the only status
# that carries decision forms; everything else is already decided.
OPEN_REQ = {
    "id": 51,
    "guild_id": 3,
    "amount_units": 4000,
    "status": "requested",
    "created_at": "2026-09-27T00:00:00.000Z",
    "decided_by": None,
    "decided_at": None,
}
PAID_REQ = {
    "id": 52,
    "guild_id": 3,
    "amount_units": 2500,
    "status": "paid",
    "created_at": "2026-09-26T00:00:00.000Z",
    "decided_by": "Pickle",
    "decided_at": "2026-09-27T01:00:00.000Z",
}
OTHER_GUILD_REQ = {
    "id": 53,
    "guild_id": 9,
    "amount_units": 1000,
    "status": "requested",
    "created_at": "2026-09-27T00:00:00.000Z",
    "decided_by": None,
    "decided_at": None,
}


def _with_rows(rows, fn):
    """Run fn() against a panel that reads exactly `rows`."""
    real = db.list_guild_grant_requests

    def _fake(*a, **kw):
        return list(rows)

    db.list_guild_grant_requests = _fake
    try:
        return fn()
    finally:
        db.list_guild_grant_requests = real


def test_grant_route_registered():
    """The route exists. This is the wiring pin: remove the Route entry and
    the panel renders buttons that post nowhere, so the suite goes red."""
    from server import admin

    paths = [getattr(r, "path", None) for r in admin.ROUTES]
    assert "/admin/guilds/{guild_id:int}/grant" in paths, paths


def test_panel_renders_both_forms_for_an_open_request():
    html = _with_rows([OPEN_REQ], lambda: ag._guild_grants_html(3, ""))
    assert "Grant requests" in html
    assert "#51" in html and "4000u" in html
    assert "decline</button>" in html and "approve</button>" in html
    # The request id travels as a hidden field, so the handler knows which.
    assert "<input type='hidden' name='request' value='51'/>" in html
    assert "action='/admin/guilds/3/grant'" in html


def test_confirm_rides_the_paying_form_not_the_button():
    """The confirm must sit on the FORM's opening tag.

    `submit` is dispatched at the form and does not bubble, so an onsubmit
    on the BUTTON renders correctly and never fires - a first cut of this
    PR did exactly that, and a presence-only pin (`count("onsubmit=") == 1`)
    passed it. Matching the opening tag is the whole point: it is what makes
    this pin discriminating rather than decorative. This is the presence-is-
    not-discrimination axis applied to my own change.
    """
    html = _with_rows([OPEN_REQ], lambda: ag._guild_grants_html(3, ""))
    forms = re.findall(r"<form[^>]*>", html)
    assert len(forms) == 2, forms
    paying = [f for f in forms if "onsubmit=" in f]
    assert len(paying) == 1, forms
    assert "confirm(" in paying[0], paying[0]
    # It names the amount and says what the click costs.
    assert "Approve grant #51 for 4000 units?" in paying[0], paying[0]
    assert "This pays the guild from the Treasury now." in paying[0], paying[0]
    # The form that moves nothing carries no confirm at all.
    quiet = [f for f in forms if f is not paying[0]][0]
    assert "onsubmit" not in quiet, quiet
    # And the paying form is the one whose button approves.
    assert "value='approve'" in html


def test_panel_discloses_a_truncated_queue():
    """The read is a window over an UNBOUNDED table - the two-lifetime-grant
    cap counts paid rows only, and a declined request can be re-filed
    forever - so the panel must never claim a guild never asked when the
    window may simply have cut its row off. `None.` reads as a fact, which
    is the silent-wrong-answer class."""
    # 200 rows, none of them this guild's: empty AND truncated.
    foreign = [dict(OPEN_REQ, guild_id=99) for _ in range(200)]
    html = _with_rows(foreign, lambda: ag._guild_grants_html(3, ""))
    assert "None" in html
    assert "hit its window" in html, html[-400:]


def test_panel_says_nothing_about_truncation_on_a_short_queue():
    """The converse, so the note cannot just always be on: a queue well
    inside the window must render the plain sentence."""
    html = _with_rows([OPEN_REQ], lambda: ag._guild_grants_html(3, ""))
    assert "hit its window" not in html


def test_panel_hides_forms_once_the_request_is_decided():
    html = _with_rows([PAID_REQ], lambda: ag._guild_grants_html(3, ""))
    assert "approve</button>" not in html and "decline</button>" not in html
    # A decided request still shows what happened and who did it.
    assert "paid" in html
    assert "decided by Pickle" in html and "2026-09-27T01:00:00.000Z" in html


def test_panel_shows_only_this_guilds_requests():
    """The panel is scoped to the page it is on: another guild's open request
    must not render here, or its id would post against this guild."""
    html = _with_rows([OPEN_REQ, OTHER_GUILD_REQ], lambda: ag._guild_grants_html(3, ""))
    assert "#51" in html
    assert "#53" not in html


def test_panel_renders_nothing_decidable_without_requests():
    html = _with_rows([], lambda: ag._guild_grants_html(3, ""))
    assert "Grant requests" in html
    assert "approve</button>" not in html and "None." in html


def test_grant_decide_refuses_an_unknown_admin():
    """The principal gate. admin_decide_guild_grant resolves the name through
    _admin_agent BEFORE it reads or pays anything, so an unknown name is
    refused with no scaffolding and no money path reached."""
    try:
        db.admin_decide_guild_grant("definitely-not-an-admin", 1, True, 1)
    except db.ForumError as exc:
        assert "unknown admin" in str(exc), exc
    else:
        raise AssertionError("an unknown admin reached the grant decision")

    # The twin is the admin panel's own entry point, exported from the
    # facade, and it is NOT the token-shaped MCP function.
    assert callable(db.admin_decide_guild_grant)
    assert hasattr(db, "decide_guild_grant")


# ── What these pins do NOT cover, stated rather than implied ─────────────
# The approve/decline money legs are the existing engine's, and
# tests/test_guilds_grants.py already covers them through
# db.decide_guild_grant. What is NOT covered here, and should be covered
# before this is trusted with a live grant:
#   - a real end-to-end approval from the panel (needs the full
#     collaborative-proposal + designated-project scaffolding, which is
#     what test_guilds_grants.py's harness builds).
#   - the cross-guild guard in admin_decide_guild_grant, which needs a real
#     guild_grant_requests row to name the wrong guild.
#   - the CSRF refusal and the session gate, which live in _auth and are
#     shared with every other admin action on this page.
# Those are the honest edges; the panel-render and route pins above are
# what discriminate the new surface itself.


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} admin-guild-grants tests passed")
