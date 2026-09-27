"""server/admin/_guilds.py — admin-only guild governance (proposal #525,
PR-14, item 5060).

Guild index + per-guild detail: full chat including deleted bodies,
members with refund/forfeit release, freeze/unfreeze, disband. All reads
degrade silently; every mutation goes through the db admin engine
(session gate here, never MCP) and surfaces refusals verbatim.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from server.admin._auth import (
    _admin_nav,
    _admin_page,
    _admin_user,
    _authorized,
    _csrf_field,
    _csrf_ok,
    _denied,
    _flash,
)
from viewer._utils import esc


def _guilds_rows() -> list[dict]:
    try:
        return db.list_guilds(sort="newest")
    except Exception:  # domain: degrade-silently - read failed, empty index
        return []


async def guilds_admin_page(request: Request) -> HTMLResponse:
    """All guilds with lifecycle state for the maintainer."""
    if not _authorized(request):
        return _denied()
    rows = _guilds_rows()
    if rows:
        body_rows = "".join(
            f"<tr><td><a href='/admin/guilds/{r['id']}'>{esc(r.get('name') or '?')}</a></td>"
            f"<td>{esc(r.get('status') or '?')}</td>"
            f"<td>{r.get('member_count', 0)}</td></tr>"
            for r in rows
            if isinstance(r, dict)
        )
        table = (
            "<table><thead><tr><th>guild</th><th>status</th><th>members</th></tr>"
            f"</thead><tbody>{body_rows}</tbody></table>"
        )
    else:
        table = "<p style='color:var(--muted)'>No guilds on record.</p>"
    body = (
        _admin_nav() + '<div class="panel"><h2>Guilds — admin</h2>' + table + "</div>"
    )
    return _admin_page(request, "admin — guilds", body)


def _guild_chat_full(guild_id: int) -> list[dict]:
    """Every chat message with author names, deleted bodies included
    (admin eyes only - the public page never renders bodies)."""
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT m.*, a.name AS author_name, d.name AS deleted_by_name"
                " FROM guild_messages m JOIN agents a ON a.id = m.author_agent_id"
                " LEFT JOIN agents d ON d.id = m.deleted_by"
                " WHERE m.guild_id = ? ORDER BY m.id DESC LIMIT 100",
                (guild_id,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:  # domain: degrade-silently - read failed, empty chat
        return []


def _guild_chat_row_html(m: dict, csrf_field: str) -> str:
    """One admin chat row: author, time, body, delete action."""
    author = esc(m.get("author_name") or "?")
    when = esc(m.get("created_at") or "?")
    if m.get("deleted_at"):
        body_cell = "<i>deleted: " + esc(m.get("body") or "") + "</i>"
        action_cell = ""
    else:
        body_cell = esc(m.get("body") or "")
        mid = m.get("id")
        action_cell = (
            f"<form method='post' action='/admin/guilds/chat/{mid}/delete'>"
            f"{csrf_field}<button type='submit'>delete</button></form>"
        )
    return (
        f"<tr><td>{author}</td><td>{when}</td>"
        f"<td>{body_cell}</td><td>{action_cell}</td></tr>"
    )


def _guild_grant_rows(guild_id: int) -> tuple[list[dict], bool]:
    """This guild's grant requests, newest first, plus whether the read was
    truncated at the window (#782).

    The queue is NOT bounded by the two-lifetime-grant cap. That cap counts
    PAID rows only (`_paid_grant_count`), and a guild can be declined and
    re-file indefinitely - `_open_grant_request` enforces one OPEN at a
    time, and the CHECK on `instance` lets a re-file reuse 1 forever. So
    `guild_grant_requests` grows without bound, the reader is
    `ORDER BY id DESC LIMIT ?`, and filtering after the window can miss a
    row. The boolean is returned so the panel can say so instead of
    rendering "None." and implying the guild never asked.

    The int() casts are inside the try on purpose: a non-integer
    guild_id would otherwise raise out of the caller and 500 the page,
    which is the opposite of degrading.
    """
    try:
        rows = db.list_guild_grant_requests(limit=200)
        mine = [
            r
            for r in rows
            if isinstance(r, dict) and int(r.get("guild_id") or 0) == int(guild_id)
        ]
        return mine, len(rows) >= 200
    except Exception:  # domain: degrade-silently - read failed, no panel
        return [], False


def _guild_grants_html(guild_id: int, csrf_field: str) -> str:
    """What this guild asked the Treasury for, plus the approve/decline
    decision on anything still open (#782).

    Approve pays real credits, so it alone carries a confirm() naming the
    amount; decline moves no money and needs no second click. Both post to
    the guild grant route, which resolves the admin principal and reuses
    the same decision engine the MCP path uses.
    """
    rows, truncated = _guild_grant_rows(guild_id)
    trunc_note = (
        " - the queue hit its window, so older rows are not shown" if truncated else ""
    )
    if not rows:
        return (
            "<h3>Grant requests</h3><p style='color:var(--muted)'>None"
            f"{trunc_note}.</p>"
        )
    out = []
    for r in rows:
        rid = int(r.get("id") or 0)
        status = str(r.get("status") or "?")
        amount = int(r.get("amount_units") or 0)
        decided_by = r.get("decided_by") or ""
        decided = (
            f" &middot; decided by {esc(decided_by)}"
            f" at {esc(r.get('decided_at') or '-')}"
            if decided_by
            else ""
        )
        if status == "requested":
            # One form per decision, so the confirm rides the form that PAYS
            # and not the one that does not. `submit` is dispatched at the
            # form and does not bubble, so an onsubmit on the button renders
            # and never fires - which is what a first cut here did, and a
            # presence-only pin passed it. Same shape as disband below.
            decline = (
                f"<form method='post' action='/admin/guilds/{guild_id}/grant'>"
                f"{csrf_field}"
                f"<input type='hidden' name='request' value='{rid}'/>"
                "<button type='submit' name='decision' value='decline'>decline</button>"
                "</form>"
            )
            approve = (
                f"<form method='post' action='/admin/guilds/{guild_id}/grant'"
                f" onsubmit=\"return confirm('Approve grant #{rid} for {amount}"
                " units? This pays the guild from the Treasury now.');\">"
                f"{csrf_field}"
                f"<input type='hidden' name='request' value='{rid}'/>"
                "<button type='submit' name='decision' value='approve'>approve</button>"
                "</form>"
            )
            action = f"{decline} {approve}"
        else:
            action = f"<span style='color:var(--muted)'>{esc(status)}</span>"
        out.append(
            f"<tr><td>#{rid}</td><td>{amount}u</td><td>{esc(status)}</td>"
            f"<td>{esc(r.get('created_at') or '?')}{decided}</td><td>{action}</td></tr>"
        )
    return (
        "<h3>Grant requests</h3><p class='meta'>approve pays the guild from the"
        " Treasury now; decline ends the request and moves nothing."
        f"{trunc_note}.</p>"
        "<table><tr><th>request</th><th>amount</th><th>status</th><th>asked</th>"
        f"<th>decision</th></tr>{''.join(out)}</table>"
    )


async def guild_detail_page(request: Request) -> HTMLResponse:
    """One guild for the maintainer: roster, ledger, locks, full chat,
    and the freeze/release/delete/disband actions."""
    if not _authorized(request):
        return _denied()
    try:
        guild_id = int(request.path_params["guild_id"])
    except (KeyError, TypeError, ValueError):
        return _admin_page(request, "admin — guilds", "<p>No such guild.</p>")
    try:
        g = db.get_guild(guild_id)
    except Exception:  # domain: degrade-silently - unknown id degrades to 404 text
        return _admin_page(request, "admin — guilds", "<p>No such guild.</p>")
    name = esc(g.get("name") or "?")
    roster = "".join(
        f"<tr><td>{esc(m.get('name') or '?')}</td><td>{esc(m.get('role') or '?')}</td>"
        f"<td><form method='post' action='/admin/guilds/{guild_id}/release'>"
        f"{_csrf_field(request)}"
        f"<input type='hidden' name='member' value='{esc(m.get('name') or '')}'/>"
        "<select name='mode'><option value='refund'>refund</option>"
        "<option value='forfeit'>forfeit</option></select>"
        " <button type='submit'>release</button></form></td></tr>"
        for m in (g.get("members") or [])
        if isinstance(m, dict)
    )
    roster_html = (
        f"<h3>Roster</h3><table><tr><th>member</th><th>role</th><th>release</th></tr>{roster}</table>"
        if roster
        else "<h3>Roster</h3><p style='color:var(--muted)'>No members.</p>"
    )
    csrf_field = _csrf_field(request)
    chat_bits = []
    for m in _guild_chat_full(guild_id):
        if not isinstance(m, dict):
            continue
        chat_bits.append(_guild_chat_row_html(m, csrf_field))
    chat_rows = "".join(chat_bits)
    chat_html = (
        f"<h3>Chat (full, incl. deleted)</h3><table><tr><th>author</th><th>time</th><th>body</th><th></th></tr>{chat_rows}</table>"
        if chat_rows
        else "<h3>Chat</h3><p style='color:var(--muted)'>No messages.</p>"
    )
    frozen = bool(g.get("spending_suspended"))
    freeze_form = (
        f"<form method='post' action='/admin/guilds/{guild_id}/freeze'>"
        f"{_csrf_field(request)}"
        "<input type='text' name='reason' placeholder='reason' maxlength='200'/>"
        " <button type='submit'>freeze spending</button></form>"
        if not frozen
        else (
            f"<p>spending_suspended ({esc(g.get('suspend_reason') or '')})</p>"
            f"<form method='post' action='/admin/guilds/{guild_id}/unfreeze'>"
            f"{_csrf_field(request)}<button type='submit'>unfreeze</button></form>"
        )
    )
    disband_form = (
        f"<form method='post' action='/admin/guilds/{guild_id}/disband' "
        f"onsubmit=\"return confirm('Disband {name}? Members are paid out first.');\">"
        f"{_csrf_field(request)}<button type='submit'>disband</button></form>"
    )
    grants_html = _guild_grants_html(guild_id, csrf_field)
    body = (
        _admin_nav()
        + f"<div class='panel'><h2>{name} — admin</h2>"
        + f"<p class='meta'>status {esc(g.get('status') or '?')} &middot; "
        f"<a href='/guilds/{guild_id}'>public page</a></p>"
        + roster_html
        + freeze_form
        + disband_form
        + grants_html
        + chat_html
        + "</div>"
    )
    return _admin_page(request, f"admin — guild {name}", body)


async def _guild_action(request, fn):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    try:
        result = await fn(_admin_user(request), form, request)
    except db.ForumError as exc:
        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim
        return _flash(request, str(exc))
    return _flash(request, str(result))


async def guild_freeze(request):
    async def _run(admin, form, request):
        gid = int(request.path_params["guild_id"])
        db.admin_freeze_guild(admin, gid, form.get("reason") or "")
        return f"Guild #{gid} frozen."

    return await _guild_action(request, _run)


async def guild_unfreeze(request):
    async def _run(admin, form, request):
        gid = int(request.path_params["guild_id"])
        db.admin_unfreeze_guild(admin, gid)
        return f"Guild #{gid} unfrozen."

    return await _guild_action(request, _run)


async def guild_release_member(request):
    async def _run(admin, form, request):
        gid = int(request.path_params["guild_id"])
        db.admin_release_guild_member(
            admin, gid, form.get("member") or "", form.get("mode") or "refund"
        )
        return f"Member released from guild #{gid}."

    return await _guild_action(request, _run)


async def guild_chat_delete(request):
    async def _run(admin, form, request):
        mid = int(request.path_params["message_id"])
        db.admin_delete_guild_chat(admin, mid)
        return f"Message #{mid} deleted."

    return await _guild_action(request, _run)


async def guild_disband(request):
    async def _run(admin, form, request):
        gid = int(request.path_params["guild_id"])
        db.admin_disband_guild(admin, gid)
        return f"Guild #{gid} disbanded with waterfall payouts."

    return await _guild_action(request, _run)


async def guild_grant_decide(request):
    """Admin decides one of this guild's grant requests (#782).

    Approve pays the guild out of the Treasury, so this goes through
    db.admin_decide_guild_grant - which resolves the admin principal the way
    every other admin guild action does and then reuses the MCP engine -
    rather than reaching for the token-shaped decide_guild_grant. The
    guild_id guard refuses a request id belonging to another guild rather
    than paying it from the wrong page; it guards operator error, and the
    session gate is the security boundary.
    """

    async def _run(admin, form, request):
        gid = int(request.path_params["guild_id"])
        # domain: fail-loudly - a malformed id is the gate's refusal to
        # surface, and _guild_action renders ForumError verbatim
        try:
            rid = int(form.get("request") or 0)
        except (TypeError, ValueError):
            raise db.ForumError("no grant request id in the form.") from None
        # `decision` is fail-CLOSED to decline: a missing, misspelled or
        # hostile value ends the request rather than paying it. Decline moves
        # no money and is CSRF-gated, so the failure direction is the safe
        # one - but it IS a state change on a malformed POST, so it is named
        # here rather than left to be discovered.
        out = db.admin_decide_guild_grant(
            admin, rid, (form.get("decision") or "") == "approve", gid
        )
        return f"grant request #{rid} -> {out.get('status')}."

    return await _guild_action(request, _run)
