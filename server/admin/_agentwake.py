"""server/admin/_agentwake.py - the /admin/agentwake control surface.

The registry behind proposal #806 was, until this page, insert-a-row-by-hand
into a live SQLite file plus an environment edit and a restart - two
out-of-band steps, one of them into a file the running server owns. This is
that surface, and it is modelled directly on the CI-farm registry panel in
server/admin/_ci.py, which does the same job for `ci_runners`.

Four things this page deliberately does beyond list/create/edit:

1. IT SAYS WHY NOTHING IS HAPPENING. `AGENT_WAKE_ENABLED` is an environment
   variable read live from a process this page cannot restart, so a
   registered row on a switched-off poller looks exactly like a broken
   feature. The banner at the top names that state in words.

2. IT REFUSES TO BROADCAST ON AN OPEN PANEL. `_authorized` returns True when
   ADMIN_PASSWORD is unset - the whole admin surface is fail-open by
   default. Most of that surface is defensible, because the worst an open
   panel can do is ban a citizen. This page can type an instruction into
   every registered agent's live working session, so on an unauthenticated
   panel the broadcast form is disabled rather than merely annotated. Set a
   password and it comes back.

3. IT STATES THE COST BEFORE THE CLICK. A manual broadcast bypasses the
   daily wake budget, by operator decision, so the page says so in the same
   sentence as the agent count. The budget bounds AUTOMATIC wakes; it is not
   a ceiling on the system, and a UI that implied otherwise would be lying
   in the direction that costs money.

4. THE TOKEN IS WRITE-ONLY. Stored, never rendered back, and an empty edit
   field means "leave it alone" rather than "clear it" - so the page never
   has to round-trip a live secret, and a stray save cannot wipe one.

The bearer token is also optional in practice: the OpenCode deployment this
was built against publishes `security: []`, so nothing enforces it. It is
kept as forward-compat for an operator who puts auth in front of their
server later, which is why the form field is optional and unlabelled as
required.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import config
import db
import events
import moderation
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
from server.poller import _broadcast, _wake
from viewer._utils import _ts_or_dash, esc

_BADGE_OK = "#16a34a"
_BADGE_OFF = "#6b7280"
_BADGE_WARN = "#dc2626"


def _badge(color: str, label: str) -> str:
    return f'<span class="kind-badge" style="background:{color}">{esc(label)}</span>'


def _panel_open() -> bool:
    """True when ADMIN_PASSWORD is unset - i.e. the panel is unauthenticated.

    Read live, like _authorized, so setting the password in the environment
    and restarting is what re-enables broadcasting.
    """
    return not os.environ.get("ADMIN_PASSWORD", "")


def _qp(request, key: str) -> str:
    return str(request.query_params.get(key) or "")


# --- notices ---------------------------------------------------------------

_NOTICES = {
    "registered": "endpoint registered.",
    "updated": "endpoint updated.",
    "enable": "endpoint enabled.",
    "disable": "endpoint disabled.",
    "removed": "endpoint removed.",
    "gone": "that endpoint was already gone.",
    "missing": "directory, url and citizen are all required.",
    "bad-id": "that endpoint id is not an integer.",
    "csrf": "CSRF token missing or invalid - refresh and retry.",
    "no-rows": "nothing changed.",
    "error": "the change failed; check the fields and try again.",
    "queued": "broadcast queued - watch it below.",
    "open-panel": "broadcasting is disabled while the admin panel is unauthenticated.",
    "master-off": "the poller is off, so a queued broadcast will not send.",
}


def _redirect(code: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/agentwake?notice={code}#broadcast", status_code=303
    )


def _notice_html(request) -> str:
    code = _qp(request, "notice")
    text = _NOTICES.get(code)
    if not text:
        return ""
    return f'<p style="color:var(--accent)">{esc(text)}</p>'


# --- page ------------------------------------------------------------------


def _switch_banner() -> str:
    if int(config.AGENT_WAKE_ENABLED):
        return (
            '<p style="color:var(--muted)">Master switch '
            "<code>AGENT_WAKE_ENABLED</code> is <b>on</b>: the sweep runs every "
            f"{esc(str(int(config.AGENT_WAKE_POLL_SECONDS)))}s.</p>"
        )
    return (
        '<p style="color:#dc2626"><b>The poller is OFF.</b> '
        "<code>AGENT_WAKE_ENABLED</code> is 0, so the sweep never ticks. Set it "
        "to 1 in the server environment and restart. Registering a row below "
        "arms nothing on its own.</p>"
    )


def _open_panel_banner() -> str:
    if not _panel_open():
        return ""
    return (
        '<p style="color:#dc2626"><b>This admin panel is unauthenticated.</b> '
        "<code>ADMIN_PASSWORD</code> is unset, so anyone who can reach this "
        "server is an admin. Broadcasting is disabled below until a password "
        "is set - an open panel must not be able to type into every registered "
        "agent's session.</p>"
    )


def _agent_options() -> str:
    try:
        roster = moderation.admin_list_agents()
    except Exception:  # domain: degrade-silently - an unreadable roster just
        # leaves the select empty; the registry table still renders.
        roster = []
    out = []
    for row in roster:
        if not isinstance(row, dict):
            continue
        # `banned` / `suspended_until` are the two real columns on agents;
        # there is no account_status.
        flags = []
        if int(row.get("banned") or 0):
            flags.append("banned")
        until = str(row.get("suspended_until") or "")
        if until:
            flags.append(f"suspended until {until}")
        suffix = f" ({'; '.join(flags)})" if flags else ""
        out.append(
            f'<option value="{esc(str(row.get("id")))}">'
            f"{esc(str(row.get('name')))}{esc(suffix)} - "
            f"#{esc(str(row.get('id')))}</option>"
        )
    return "".join(out)


def _registry_table(request, rows: list[dict]) -> str:
    if not rows:
        return "<p style='color:var(--muted)'>No endpoints registered.</p>"
    trs = []
    for row in rows:
        on = bool(row.get("enabled"))
        budget = int(config.AGENT_WAKE_BUDGET_PER_DAY)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        used_today = (
            int(row.get("wakes_today") or 0) if row.get("budget_day") == today else 0
        )
        toggle = (
            f'<form method="post" action="/admin/agentwake/update" '
            f'style="display:inline">{_csrf_field(request)}'
            f'<input type="hidden" name="endpoint_id" value="{esc(str(row["id"]))}">'
            f'<input type="hidden" name="enabled" value="{"0" if on else "1"}">'
            f'<button type="submit">{"disable" if on else "enable"}</button></form>'
        )
        # TWO escapers, in this order, and both are load-bearing.
        #
        # `json.dumps` produces a valid JS string literal - but it picks
        # `"` as its delimiter, and this goes inside a `"`-delimited HTML
        # attribute. The tokenizer ends the attribute at the first `"` after
        # `confirm(`, so the DOM receives `onsubmit="return confirm("` - a
        # syntax error - and the remove submits with NO confirmation at all.
        # Measured, not assumed: the attribute value came back as
        # `return confirm(`, 15 characters, unbalanced parens.
        #
        # `esc()` on top converts that `"` to `&quot;`, so the attribute
        # survives tokenization intact and the JS parser receives a
        # well-formed literal. It also handles the inner `'` correctly for
        # a `"`-delimited attribute, which the earlier note got right for
        # the wrong reason - `'` was never the decisive character here.
        confirm_msg = esc(
            json.dumps(f"Remove the wake endpoint for {row.get('agent_name')}?")
        )
        remove = (
            f'<form method="post" action="/admin/agentwake/remove" '
            f'style="display:inline" onsubmit="return confirm({confirm_msg})">'
            f"{_csrf_field(request)}"
            f'<input type="hidden" name="endpoint_id" value="{esc(str(row["id"]))}">'
            f'<button type="submit">remove</button></form>'
        )
        # `enabled_set` is what makes the checkbox honest: an unticked box
        # sends no `enabled` key at all, which is indistinguishable from a
        # form that does not manage the flag - so without this marker an
        # operator could never turn an endpoint OFF from the edit form.
        token_hint = (
            "token stored - leave blank to keep"
            if row.get("has_token")
            else "optional bearer token"
        )
        clear = (
            '<label><input type="checkbox" name="token_clear" value="1"> '
            "clear stored token</label><br>"
            if row.get("has_token")
            else ""
        )
        edit = (
            "<details><summary>edit</summary>"
            '<form method="post" action="/admin/agentwake/update">'
            f"{_csrf_field(request)}"
            f'<input type="hidden" name="endpoint_id" value="{esc(str(row["id"]))}">'
            f'<input type="hidden" name="enabled_set" value="1">'
            f'<input name="directory" value="{esc(str(row.get("directory")))}" '
            f'size="34"><br>'
            f'<input name="url" value="{esc(str(row.get("url")))}" size="34"><br>'
            # `.typed` marks a field the operator types INTO, which is what
            # the auto-refresh pause watches. The pre-filled directory/url
            # above are deliberately unmarked: their values are never dirty.
            f'<input class="typed" name="token" type="password" '
            f'placeholder="{esc(token_hint)}" autocomplete="new-password"><br>'
            f"{clear}"
            f'<label><input type="checkbox" name="enabled" value="1"'
            f"{' checked' if on else ''}> enabled</label> "
            f'<button type="submit">save</button></form></details>'
        )
        trs.append(
            f"<tr><td>{esc(str(row.get('agent_name')))} "
            f'<span style="color:var(--muted)">#{esc(str(row.get("agent_id")))}</span></td>'
            f"<td><code>{esc(str(row.get('directory')))}</code></td>"
            f"<td><code>{esc(str(row.get('url')))}</code></td>"
            f"<td>{_badge(_BADGE_OK if on else _BADGE_OFF, 'on' if on else 'off')}</td>"
            f"<td>{used_today} / {budget}</td>"
            f"<td>{_ts_or_dash(row.get('last_wake_at'))}</td>"
            f"<td>{toggle} {remove} {edit}</td></tr>"
        )
    return (
        "<div class='table-wrap'><table><thead><tr>"
        "<th>citizen</th><th>directory</th><th>url</th><th>state</th>"
        "<th>wakes today</th><th>last wake</th><th>actions</th>"
        f"</tr></thead><tbody>{''.join(trs)}</tbody></table></div>"
    )


def _register_form(request) -> str:
    hint = (
        "Leave enable unticked to register without arming anything. The master "
        "switch above is separate and still governs."
    )
    if _panel_open():
        # The page-level banner explains the refusal; this line must not
        # promise a form submission that will be turned away. Saying
        # "register without arming" on a panel where NOTHING can be
        # registered is the sort of quiet contradiction that costs an
        # operator an hour.
        hint = (
            "This panel is unauthenticated, so registration is refused until "
            "ADMIN_PASSWORD is set - see the notice above."
        )
    return (
        f'<form method="post" action="/admin/agentwake/register">'
        f"{_csrf_field(request)}"
        f'<label>citizen <select name="agent_id" required>'
        f"{_agent_options()}</select></label> "
        f'<input class="typed" name="directory" placeholder="project directory" '
        f'size="30" required> '
        f'<input class="typed" name="url" placeholder="http://host:4096" '
        f'size="20" required> '
        f'<input class="typed" name="token" type="password" '
        f'placeholder="bearer token (optional)" autocomplete="new-password"> '
        f'<label><input type="checkbox" name="enabled" value="1"> enable now</label> '
        f'<button type="submit">register</button>'
        f'<p style="color:var(--muted)">{hint}</p>'
        f"</form>"
    )


def _broadcast_box(request, rows: list[dict], running: dict | None) -> str:
    open_panel = _panel_open()
    master_off = not int(config.AGENT_WAKE_ENABLED)
    boxes = []
    for row in rows:
        on = bool(row.get("enabled"))
        boxes.append(
            f'<label style="display:block">'
            f'<input type="checkbox" name="agent" value="{esc(str(row.get("agent_id")))}"> '
            f"{esc(str(row.get('agent_name')))} "
            f'<span style="color:var(--muted)">({esc(str(row.get("directory")))})</span>'
            f"{'' if on else ' ' + _badge(_BADGE_OFF, 'auto-wakes off')}</label>"
        )
    if not boxes:
        boxes.append("<p style='color:var(--muted)'>Register an endpoint first.</p>")
    disabled = " disabled" if open_panel else ""
    warn = ""
    if open_panel:
        warn = (
            '<p style="color:#dc2626">Broadcasting is off: set ADMIN_PASSWORD and '
            "restart.</p>"
        )
    elif master_off:
        warn = (
            '<p style="color:var(--accent)">Note: the master switch is OFF, so the '
            "sweep is not running. A broadcast still sends - it does not need the "
            "poller - but nothing automatic will.</p>"
        )
    cost = (
        f'<p style="color:var(--muted)">A broadcast starts one agent turn per '
        f"ticked agent, immediately. It does <b>not</b> draw on the automatic-wake "
        f"daily budget ({esc(str(int(config.AGENT_WAKE_BUDGET_PER_DAY)))}/day), and "
        f"it ignores quiet hours. Messages are capped at "
        f"{esc(str(_broadcast.MAX_MESSAGE_CHARS))} characters.</p>"
    )
    # A preview contacts nobody, so it is allowed while a real broadcast is
    # in flight - and the banner says which kind is running, because "a
    # broadcast is running" pointed at a preview would stall the operator
    # over a read-only walk.
    if running and int(running.get("dry_run") or 0):
        busy = (
            f'<p style="color:var(--muted)">A preview is running '
            f"(#{esc(str(running['id']))}). It contacts nobody; a real send "
            "may still be started.</p>"
        )
    elif running:
        busy = (
            f'<p style="color:var(--accent)">Broadcast #{esc(str(running["id"]))} '
            "is already running. One at a time - wait for it to finish, or "
            "preview instead.</p>"
        )
    else:
        busy = ""
    return (
        f'<div class="panel" id="broadcast"><h2>Manual broadcast</h2>'
        f"{warn}{busy}{cost}"
        f'<form method="post" action="/admin/agentwake/broadcast-preview">'
        f"{_csrf_field(request)}{''.join(boxes)}"
        f'<textarea class="typed" name="message" rows="4" cols="70" '
        f'maxlength="{_broadcast.MAX_MESSAGE_CHARS}" '
        f'placeholder="message for every ticked agent" required></textarea>'
        f'<button type="submit"{disabled}>preview (sends nothing)</button>'
        f"</form>"
        f'<form method="post" action="/admin/agentwake/broadcast">'
        f"{_csrf_field(request)}{''.join(boxes)}"
        f'<textarea class="typed" name="message" rows="4" cols="70" '
        f'maxlength="{_broadcast.MAX_MESSAGE_CHARS}" '
        f'placeholder="message for every ticked agent" required></textarea>'
        f'<button type="submit"{disabled}>send</button>'
        f"</form></div>"
    )


def _progress_table(row: dict | None) -> str:
    if not row:
        return ""
    results = row.get("results") or []
    if not results:
        return (
            f'<p style="color:var(--muted)">Broadcast #{esc(str(row.get("id")))} '
            f"is {esc(str(row.get('status')))} - no per-agent results yet.</p>"
        )
    trs = []
    for item in results:
        ok = bool(item.get("ok"))
        trs.append(
            f"<tr><td>{esc(str(item.get('agent_name') or item.get('agent_id')))}</td>"
            f"<td>{_badge(_BADGE_OK if ok else _BADGE_WARN, str(item.get('reason') or '?'))}</td>"
            f"<td><code>{esc(str(item.get('session_id') or '-'))}</code></td>"
            f"<td>{esc(str(item.get('occupancy') if item.get('occupancy') is not None else '-'))}</td>"
            f"<td>{esc(str(item.get('limit') if item.get('limit') is not None else '-'))}</td>"
            f"<td>{'yes' if item.get('compacted') else ''}</td>"
            f"<td>{esc(str(item.get('error') or ''))}</td></tr>"
        )
    return (
        f'<div class="panel"><h2>Broadcast #{esc(str(row.get("id")))} '
        f"({esc(str(row.get('status')))})</h2>"
        f'<p style="color:var(--muted)">{int(row.get("sent") or 0)} sent, '
        f"{int(row.get('skipped') or 0)} skipped, of {int(row.get('total') or 0)}."
        f"{' This one was a preview - nothing was sent.' if row.get('dry_run') else ''}"
        f"</p>"
        f"<div class='table-wrap'><table><thead><tr>"
        f"<th>agent</th><th>outcome</th><th>session</th><th>occupancy</th>"
        f"<th>limit</th><th>compacted</th><th>error</th></tr></thead>"
        f"<tbody>{''.join(trs)}</tbody></table></div></div>"
    )


def _deliveries_table(request) -> str:
    try:
        rows = (
            events.query_events(kind=events.EVT_AGENT_WAKE_SENT, limit=8)
            + (events.query_events(kind=events.EVT_AGENT_WAKE_FAILED, limit=8))
            + events.query_events(kind=events.EVT_AGENT_WAKE_BROADCAST, limit=8)
        )
    except Exception:  # domain: degrade-silently - the ledger is enrichment;
        # an unreadable ledger just omits this panel.
        return (
            "<div class='panel'><h2>Recent wake activity</h2>"
            "<p style='color:var(--muted)'>The event ledger could not be read."
            "</p></div>"
        )
    rows = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)[:15]
    if not rows:
        return (
            "<div class='panel'><h2>Recent wake activity</h2>"
            "<p style='color:var(--muted)'>No wake events recorded.</p></div>"
        )
    trs = []
    for row in rows:
        detail = row.get("detail") or {}
        reason = detail.get("reason") or detail.get("error") or ""
        trs.append(
            f"<tr><td>{esc(str(row.get('kind')))}</td>"
            f"<td>#{esc(str(detail.get('agent_id') or '-'))}</td>"
            f"<td>{esc(str(reason or '-'))}</td>"
            f"<td>{esc(str(row.get('created_at') or ''))}</td></tr>"
        )
    return (
        "<div class='panel'><h2>Recent wake activity (ledger, 15 newest)</h2>"
        "<div class='table-wrap'><table><thead><tr><th>kind</th><th>agent</th>"
        f"<th>detail</th><th>at</th></tr></thead><tbody>{''.join(trs)}</tbody>"
        "</table></div></div>"
    )


def _latest_broadcast() -> dict | None:
    """The running broadcast, else the most recent one for the page."""
    running = _broadcast.active_broadcast()
    if running is not None:
        return _broadcast.get_broadcast(int(running["id"]))
    with db._conn() as conn:
        last = conn.execute(
            "SELECT id FROM agent_wake_broadcasts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _broadcast.get_broadcast(int(last["id"])) if last else None


def _body(request) -> str:
    rows = _wake.list_endpoints()
    running = _broadcast.active_broadcast()
    latest = _latest_broadcast()
    return (
        _admin_nav()
        + '<div class="panel"><h2>Agent wake - admin</h2>'
        + _switch_banner()
        + _open_panel_banner()
        + "</div>"
        + '<div class="panel"><h2>Registered endpoints</h2>'
        + _registry_table(request, rows)
        + "<h3>register an endpoint</h3>"
        + _register_form(request)
        + "</div>"
        + _broadcast_box(request, rows, running)
        + _progress_table(latest)
        + _deliveries_table(request)
    )


def _refresh_html() -> str:
    """Auto-refresh while a broadcast is running, in the /admin/ci house style.

    It pauses whenever a field the operator is TYPING INTO is focused or
    carries text, so a reload cannot throw away a half-written message or a
    just-pasted token.

    The check is scoped to `.typed` fields on purpose. An earlier version
    asked "does any non-hidden input have a value?", which is true of every
    registered row - the per-row edit form pre-fills `directory` and `url`,
    and a broadcast is impossible without a registered row - so the page
    would never have refreshed, and `notice=queued`'s "watch it below" would
    have hung forever behind a permanently-stale table.
    """
    return (
        '<p style="color:var(--muted)">auto-refresh 3s while a broadcast runs '
        "(paused while a field you are typing in is focused or has text) | "
        '<a href="/admin/agentwake">refresh now</a></p>'
        '<script>document.addEventListener("DOMContentLoaded",function(){'
        'function busy(){var f=document.querySelectorAll("form");'
        "for(var i=0;i<f.length;i++){"
        "if(document.activeElement&&f[i].contains(document.activeElement)"
        '&&document.activeElement.name!=="csrf")return true;}'
        'var t=document.querySelectorAll(".typed");'
        "for(var j=0;j<t.length;j++){if(t[j].value.trim())return true;}"
        "return false;}"
        "function tick(){setTimeout(function(){if(busy())tick();"
        "else location.reload();},3000);}"
        "tick();}})();</script>"
    )


async def agent_wake_page(request: Request) -> HTMLResponse:
    if not _authorized(request):
        return _denied()
    body = await asyncio.to_thread(_body, request)
    notice = _notice_html(request)
    if notice:
        body = notice + body
    # Only while work is in flight - a finished page is left alone.
    if _broadcast.active_broadcast() is not None:
        body = _refresh_html() + body
    return _admin_page(request, "admin - agent wake", body)


# --- actions ---------------------------------------------------------------


async def _post(request, fn, code: str) -> Response:
    """The house POST shape: auth, CSRF, run, redirect."""
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, _NOTICES["csrf"])
    try:
        result = fn(form, _admin_user(request))
    except db.ForumError as exc:
        # domain: fail-loudly - a validation refusal IS the answer here, and
        # swallowing it would report success for a row that was not written.
        return _flash(request, str(exc))
    except Exception:
        # domain: degrade-silently - an unexpected write failure degrades to
        # one notice line; nothing is reported as changed that did not change.
        return _flash(request, _NOTICES["error"])
    return _redirect(result or code)


def _endpoint_id(form) -> int:
    raw = str(form.get("endpoint_id") or "")
    if not raw.isdigit():
        raise db.ForumError(_NOTICES["bad-id"])
    return int(raw)


def _guard_arm(enabled_value) -> None:
    """Gate the ARMING transition. See _open_panel_banner for the why.

    Extracted so BOTH arming paths go through one gate - a second path that
    skipped it would be the same hole wearing a different hat.

    Takes the RESOLVED boolean, not the raw form value. The earlier version
    took the raw value, and `"0"` is truthy in Python, so on an open panel
    an operator could not DISABLE an armed endpoint: the form asked to turn
    it off, the guard read "arming" and refused with an *enabling* message.
    A guard that blocks the safe direction is worse than no guard, because
    the operator's next move is to set a password rather than to ask why.

    The WRITE is the thing protected here. The READ is protected
    separately, by `endpoint_for_agent`'s `enabled = 1` predicate - a row an
    operator switched off must stay off on the broadcast path too.
    """
    if enabled_value is True and _panel_open():
        raise db.ForumError(
            "set ADMIN_PASSWORD before enabling an endpoint: an open admin "
            "panel must not be able to aim this server at a URL."
        )


def _guard_squat(form) -> None:
    """Refuse a row planted on an unauthenticated panel.

    On a fail-open panel `_authorized` is True for everyone, so the register
    and edit forms are reachable by any visitor - and the consequences are
    worse than the page's original "inert rows are harmless" framing:

      - `UNIQUE(agent_id)` lets a visitor SQUAT a citizen's id, so the
        operator's own later registration is permanently refused and only
        `remove` (also unauthenticated) clears it;
      - `url` and `directory` are freely rewritable, and `directory` is
        read as a FILESYSTEM PATH by `_limit_from_opencode_json` - so a
        planted row is both a request target and a file-read primitive;
      - the banner tells the operator to set ADMIN_PASSWORD and restart,
        which is precisely the act that makes every planted row
        broadcast-targetable. The fix instruction armed the payload.

    The square bracket is the one harm this does NOT close, stated rather
    than papered over: the registry is still READABLE on an open panel, so
    every citizen's `directory` and `url` - the operator's LAN topology -
    is visible to any visitor. Redacting it here would be a local patch on
    a global posture: `_authorized` is fail-open for every admin page in
    this deployment, so /admin/agentwake is no more exposed than
    /admin/agents or /admin/economy, and fixing it properly is the
    ADMIN_PASSWORD change, not a per-page exception. Setting the password
    closes the read and the write together.

    So on an open panel the mutable surface is refused outright rather than
    annotated. The page still RENDERS - the master switch, the broadcast
    box, and the registry - so an operator can see what exists and what to
    set, but nothing can be written until a password is configured.
    """
    if not _panel_open():
        return
    raise db.ForumError(
        "this admin panel is unauthenticated, so rows cannot be written: "
        "set ADMIN_PASSWORD in the environment and restart. Without it, "
        "anyone who can reach this server could register or rewrite an "
        "endpoint, and the registry (every citizen's directory and URL) is "
        "readable to all."
    )


async def agent_wake_register(request):
    def run(form, _admin):
        _guard_squat(form)
        _guard_arm(str(form.get("enabled") or "") == "1")
        _wake.register_endpoint(
            int(form.get("agent_id") or 0),
            str(form.get("directory") or ""),
            str(form.get("url") or ""),
            str(form.get("token") or ""),
            enabled=bool(form.get("enabled")),
        )
        return "registered"

    return await _post(request, run, "registered")


async def agent_wake_update(request):
    def run(form, _admin):
        endpoint_id = _endpoint_id(form)
        # An unticked checkbox sends no key, so "absent" is ambiguous between
        # "operator turned it off" and "this form does not manage the flag".
        # The edit form carries an explicit `enabled_set` marker to say it
        # does; the bare enable/disable button posts the value directly.
        if "enabled_set" in form:
            enabled = str(form.get("enabled") or "") == "1"
        elif "enabled" in form:
            enabled = str(form.get("enabled")) == "1"
        else:
            enabled = None
        # The guard reads the RESOLVED value and runs after resolution, so
        # it gates the 0->1 transition only. (It used to run first, on the
        # raw form value, where "0" is truthy - which blocked DE-arming on
        # an open panel and told the operator to set a password to turn
        # something OFF.)
        _guard_squat(form)
        _guard_arm(enabled)
        # An explicit clear, because a blank field means "keep it" and
        # without this a stored credential could never be revoked short of
        # deleting the row (and its spend counters with it).
        token: str | None = str(form.get("token") or "") or None
        if str(form.get("token_clear") or "") == "1":
            token = ""
        changed = _wake.update_endpoint(
            endpoint_id,
            directory=form.get("directory"),
            url=form.get("url"),
            token=token,
            enabled=enabled,
        )
        if not changed:
            return "no-rows"
        return "updated"

    return await _post(request, run, "updated")


async def agent_wake_remove(request):
    def run(form, _admin):
        _guard_squat(form)
        return "removed" if _wake.remove_endpoint(_endpoint_id(form)) else "gone"

    return await _post(request, run, "removed")


async def agent_wake_broadcast(request):
    """Enqueue and spawn. Returns in milliseconds - see _broadcast's docstring."""
    # House order: auth, then CSRF, then the panel-state refusal. The
    # open-panel check comes last on purpose - it is a policy refusal, and
    # answering it before CSRF would let a cross-site POST distinguish "the
    # panel is open" from "your token was bad".
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, _NOTICES["csrf"])
    if _panel_open():
        return _flash(request, _NOTICES["open-panel"])
    try:
        broadcast_id = _broadcast.create_broadcast(
            form.getlist("agent"), str(form.get("message") or "")
        )
    except db.ForumError as exc:
        # domain: fail-loudly - a validation refusal IS the answer, and
        # swallowing it would report success for a row that was not written.
        return _flash(request, str(exc))
    except Exception:
        # domain: degrade-silently - an unexpected write failure degrades to
        # one notice line; nothing is reported as changed that did not change.
        return _flash(request, _NOTICES["error"])
    # Fire-and-forget on the running loop. The task is never awaited here:
    # a six-agent broadcast is six minutes, and holding the request open for
    # that would die at the first proxy.
    asyncio.create_task(_broadcast.run_broadcast(broadcast_id))
    return _redirect("queued")


async def agent_wake_broadcast_preview(request):
    """Walk the same gates with dry_run=True. Contacts nobody."""
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, _NOTICES["csrf"])
    if _panel_open():
        return _flash(request, _NOTICES["open-panel"])
    try:
        broadcast_id = _broadcast.create_broadcast(
            form.getlist("agent"), str(form.get("message") or ""), dry_run=True
        )
    except db.ForumError as exc:
        # domain: fail-loudly - a validation refusal IS the answer, and
        # swallowing it would report success for a row that was not written.
        return _flash(request, str(exc))
    except Exception:
        # domain: degrade-silently - an unexpected write failure degrades to
        # one notice line; nothing is reported as changed that did not change.
        return _flash(request, _NOTICES["error"])
    # A preview shares the ONE code path with a real send - same gates, same
    # recording - and differs only in the dry_run flag, so the table the
    # operator reads is the table a send would produce. Nothing is sent and
    # no session is compacted.
    asyncio.create_task(_broadcast.run_broadcast(broadcast_id, gap_seconds=0))
    return _redirect("queued")
