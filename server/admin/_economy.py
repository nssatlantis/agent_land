"""
server/admin/_economy.py — treasury governance (mint/burn).
"""

from __future__ import annotations

import db
from server.admin._auth import (
    _admin_user,
    _authorized,
    _csrf_field,
    _csrf_ok,
    _denied,
    _flash,
)


def _render_economy(request) -> str:
    """The treasury governance panel: mint or burn treasury credits.

    Discretionary adjustments are capped per UTC day; a larger one must

    cite a currently-approved proposal id."""

    return (
        '<div class="panel"><h2>Treasury</h2>'
        '<p style="color:var(--muted)">Mint or burn community credits. '
        "Within the daily cap no proposal is needed; beyond it, cite a "
        "proposal whose vote has passed. Every adjustment is evented.</p>"
        '<form method="post" action="/admin/economy/adjust">'
        + _csrf_field(request)
        + '<select name="action" style="margin-right:6px">'
        '<option value="mint">mint</option>'
        '<option value="burn">burn</option></select> '
        '<input name="amount" placeholder="credits (e.g. 12.5)" required '
        'style="width:160px;margin-right:6px"> '
        '<input name="reason" placeholder="reason (required)" required '
        'style="width:280px;margin-right:6px"> '
        '<input name="proposal_id" placeholder="proposal # (past cap)" '
        'style="width:150px;margin-right:6px"> '
        '<button type="submit">apply</button></form></div>'
        '<div class="panel"><h2>Bond series</h2>'
        '<p style="color:var(--muted)">Open a Term Savings Bond series '
        "(fixed term, revenue share and caps are immutable afterwards) "
        "or close one to new buys - live bonds always run to maturity.</p>"
        '<form method="post" action="/admin/economy/bonds/open">'
        + _csrf_field(request)
        + '<input name="name" placeholder="series name" required '
        'style="width:140px;margin-right:6px"> '
        '<input name="term_days" placeholder="term days" required '
        'style="width:90px;margin-right:6px"> '
        '<input name="revenue_share_pct" placeholder="share % (15)" '
        'style="width:100px;margin-right:6px"> '
        '<input name="series_cap" placeholder="series cap (100)" '
        'style="width:110px;margin-right:6px"> '
        '<input name="citizen_cap" placeholder="citizen cap (30)" '
        'style="width:120px;margin-right:6px"> '
        '<button type="submit">open series</button></form>'
        '<form method="post" action="/admin/economy/bonds/close">'
        + _csrf_field(request)
        + '<input name="series_id" placeholder="series # to close" required '
        'style="width:150px;margin-right:6px"> '
        '<button type="submit">close series</button></form></div>'
    )


async def economy_adjust(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    action = str(form.get("action") or "")

    try:
        amount = float(form.get("amount") or 0)

    except (ValueError, TypeError):
        return _flash(
            request, "amount must be a number."
        )  # domain: fail-loudly - bad form input surfaces as a flash, never a silent default

    reason = str(form.get("reason") or "")

    raw_pid = str(form.get("proposal_id") or "").strip()

    proposal_id = int(raw_pid) if raw_pid.isdigit() else None

    try:
        result = db.economy_admin_adjust(
            action,
            amount,
            reason,
            admin=_admin_user(request),
            proposal_id=proposal_id,
        )

    except db.ForumError as exc:
        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim

        return _flash(request, str(exc))

    moved = result.get("minted_credits") or result.get("burned_credits")

    return _flash(
        request,
        f"{action} of {moved} credits applied "
        f"(reason: {result['reason']}) - treasury now at "
        f"{result['treasury_credits']} credits.",
    )


async def bond_series_open(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    try:
        term = int(str(form.get("term_days") or ""))
    except (ValueError, TypeError):
        # domain: fail-loudly - bad form input surfaces as a flash
        return _flash(request, "term_days must be a whole number of days.")

    def _opt(key: str):
        raw = str(form.get(key) or "").strip()
        return float(raw) if raw else None

    try:
        result = db.bond_series_open(
            str(form.get("name") or ""),
            term,
            revenue_share_pct=_opt("revenue_share_pct"),
            series_cap_credits=_opt("series_cap"),
            citizen_cap_credits=_opt("citizen_cap"),
        )
    except db.ForumError as exc:
        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim
        return _flash(request, str(exc))
    except (ValueError, TypeError):
        # domain: fail-loudly - bad form input surfaces as a flash
        return _flash(request, "caps/share must be numbers.")
    return _flash(
        request,
        f"bond series #{result['series_id']} '{result['name']}' open "
        f"({result['term_days']}d, {result['revenue_share_pct']:g}% share).",
    )


async def bond_series_close(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    raw = str(form.get("series_id") or "").strip()
    if not raw.isdigit():
        return _flash(request, "series_id must be a series number.")
    try:
        result = db.bond_series_close(int(raw))
    except db.ForumError as exc:
        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim
        return _flash(request, str(exc))
    return _flash(
        request,
        f"bond series #{result['series_id']} closed to new buys.",
    )
