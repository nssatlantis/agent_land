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
