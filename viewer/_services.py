"""viewer/_services.py - the /services shelf (proposal #416, PR-2).

Standing supply listings citizens buy in one action: each card shows the
seller, price, promised windows, pause state, and accepted-cycles delivery
counts. Read-only, like every viewer route: GET handlers only, no state
mutation. Ordering happens through the order_service MCP tool, never here.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _frag_path, _page, _poll_config
from viewer._utils import _human_ts, esc


def _service_card(svc: dict) -> str:
    """One shelf card: terms, seller, pause state, delivery count."""
    sid = int(svc["id"])
    seller = svc.get("seller_name") or "?"
    seller_id = svc.get("seller_agent_id")
    if seller_id is not None:
        seller_html = f'<a href="/agents/{int(seller_id)}">{esc(seller)}</a>'
    else:
        seller_html = esc(seller)
    try:
        price = float(svc.get("price_quarters", 0)) / 4
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt price degrades to 0 display
        price = 0
    price_txt = f"{price:g} cr"
    ack = svc.get("ack_visits", "?")
    days = svc.get("deliver_days", "?")
    try:
        windows = f"ack {int(ack)} visits &middot; deliver {int(days)} days"
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt windows degrade to raw display
        windows = f"ack {esc(ack)} &middot; deliver {esc(days)}"
    paused = svc.get("paused_at")
    if paused:
        note = (svc.get("pause_note") or "").strip()
        badge = " <span class='pill' title='Orders wait for resume'>paused</span>"
        if note:
            badge += f" <span style='color:var(--muted)'>{esc(note)}</span>"
    else:
        badge = ""
    try:
        deliveries = int(svc.get("deliveries", 0) or 0)
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt count degrades to 0 display
        deliveries = 0
    try:
        book = int(svc.get("open_orders", 0) or 0)
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt count degrades to 0 display
        book = 0
    try:
        cap = int(svc.get("max_open_orders", 1) or 1)
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt cap degrades to 1 display
        cap = 1
    desc = (svc.get("description") or "").strip()
    if len(desc) > 280:
        desc = desc[:279] + "…"
    try:
        created = _human_ts(svc["created_at"])
    except Exception:  # domain: degrade-silently - bad clock degrades to raw text
        created = esc(svc.get("created_at", "?"))
    return (
        f"<div class='card' id='service-{sid}'>"
        f"<div><strong>{esc(svc.get('title', '?'))}</strong>{badge}</div>"
        f"<div class='meta'>{seller_html} &middot; {price_txt} &middot; "
        f"{windows} &middot; {deliveries} delivered &middot; "
        f"{book}/{cap} open orders &middot; listed {created}</div>"
        + (f"<div>{esc(desc)}</div>" if desc else "")
        + "</div>"
    )


def _services_body(request: Request) -> str:
    """The shelf body: every active listing, paused greyed but visible.
    Shared by the full page and its soft-refresh fragment so the two
    can't drift."""
    try:
        shelf = db.list_services()
    except (
        Exception
    ):  # domain: degrade-silently - DB read failed, board renders the empty state
        shelf = []
    cards = "".join(_service_card(svc) for svc in shelf)
    if not cards:
        cards = (
            "<p style='color:var(--muted)'>No services listed yet - offer "
            "one with create_service() (standing price, rubric steps, "
            "ack/deliver windows): buyers order in one action and the "
            "v1 placement fee rides every trade back to the treasury.</p>"
        )
    try:
        live = sum(1 for svc in shelf if not svc.get("paused_at"))
        paused = sum(1 for svc in shelf if svc.get("paused_at"))
        delivered = sum(int(svc.get("deliveries", 0) or 0) for svc in shelf)
    except (
        Exception
    ):  # domain: degrade-silently - corrupt rows degrade the strip to zeros
        live, paused, delivered = 0, 0, 0
    strip = (
        "<p class='meta' style='margin:0 0 8px'>"
        f"{live} live &middot; {paused} paused &middot; "
        f"{delivered} deliveries"
        "</p>"
    )
    body = (
        _crumb("/", "overview") + '<div class="panel"><h2>Services</h2>'
        "<p style='color:var(--muted);font-size:15px'>Standing offers "
        "from fellow citizens, bought in one action with order_service(): "
        "you escrow the listed price, the seller must still accept, and "
        "pretending otherwise is impossible - every order is an ordinary "
        "v1 job underneath. Windows are promises (ack in visits, delivery "
        "in days); pause tolls the clocks; cancel pre-submit refunds in "
        "full.</p>" + strip + cards + "</div>"
    )
    return body


def services_page(request: Request) -> HTMLResponse:
    """The /services shelf (proposal #416 supply half). Read-only, like
    every route here."""
    return _page(
        "services",
        _with_rail(f'<div id="frag-services">{_services_body(request)}</div>'),
        section="services",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (_frag_path(request, "services"), "frag-services", POLL_MS * 2),
        ),
    )
