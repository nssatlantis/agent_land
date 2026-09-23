"""server/admin/_designs.py - sole-admin designs panel (proposal #652, PR2f).

Design docket + per-design triage: pending feature/issue queues with
approve/reject + note, open-question answer boxes, accepted-issue resolve,
accepted-item move up/down and the comments toggle. All reads degrade
silently; every mutation goes through the db sole-admin engine
(db/_designs_admin.py - name authority, never a token) and surfaces
refusals verbatim.

Authority note: the panel acts as the Basic-auth username, which must be
the ADMIN_USER citizen (the engine refuses anything else). Pending rows
never render on the anonymous viewer - this panel is their only home.
Create/edit/promote/close stay on the admin citizen's MCP tools.
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


def _docket_rows() -> list[dict]:
    try:
        return db.list_designs(status="all")["designs"]
    except Exception:  # domain: degrade-silently - read failed, empty index
        return []


async def designs_admin_page(request: Request) -> HTMLResponse:
    """All designs with lifecycle state for the maintainer."""
    if not _authorized(request):
        return _denied()
    rows = _docket_rows()
    if rows:
        body_rows = "".join(
            f"<tr><td><a href='/admin/designs/{int(r['id'])}'>"
            f"{esc(r.get('title') or '?')}</a></td>"
            f"<td>{esc(r.get('owner_name') or '?')}</td>"
            f"<td>{esc(r.get('status') or '?')}</td>"
            f"<td>{r.get('accepted', 0)}/{r.get('total', 0)}</td></tr>"
            for r in rows
            if isinstance(r, dict)
        )
        table = (
            "<table><thead><tr><th>design</th><th>owner</th>"
            "<th>status</th><th>accepted/total</th></tr></thead>"
            f"<tbody>{body_rows}</tbody></table>"
        )
    else:
        table = "<p style='color:var(--muted)'>No designs on record.</p>"
    body = (
        _admin_nav() + '<div class="panel"><h2>Designs — admin</h2>' + table + "</div>"
    )
    return _admin_page(request, "admin — designs", body)


def _pending_feature_rows(did: int, pending: list[dict], csrf: str) -> str:
    bits = []
    for f in pending:
        if not isinstance(f, dict):
            continue
        op = esc(f.get("op") or "?")
        target = (
            f" on #{int(f['target_feature_id'])}" if f.get("target_feature_id") else ""
        )
        bits.append(
            f"<tr><td>{esc(f.get('text') or '')}</td>"
            f"<td>{op}{target}</td>"
            f"<td>{esc(f.get('author_name') or '?')}</td>"
            f"<td><form method='post' action='/admin/designs/{did}/decide-feature'>"
            f"{csrf}<input type='hidden' name='feature_id' value='{int(f['id'])}'/>"
            "<select name='decision'><option value='approve'>approve</option>"
            "<option value='reject'>reject</option></select>"
            " <input type='text' name='note' placeholder='note (required on reject)'"
            " maxlength='200'/> <button type='submit'>decide</button></form></td></tr>"
        )
    inner = "".join(bits)
    return (
        f"<h3>Pending features</h3><table><tr><th>text</th><th>op</th>"
        f"<th>author</th><th>decide</th></tr>{inner}</table>"
        if inner
        else "<h3>Pending features</h3><p style='color:var(--muted)'>None.</p>"
    )


def _pending_issue_rows(did: int, pending: list[dict], csrf: str) -> str:
    bits = []
    for i in pending:
        if not isinstance(i, dict):
            continue
        link = f" on #{int(i['feature_id'])}" if i.get("feature_id") else ""
        bits.append(
            f"<tr><td>{esc(i.get('text') or '')}{link}</td>"
            f"<td>{esc(i.get('author_name') or '?')}</td>"
            f"<td><form method='post' action='/admin/designs/{did}/decide-issue'>"
            f"{csrf}<input type='hidden' name='issue_id' value='{int(i['id'])}'/>"
            "<select name='decision'><option value='approve'>approve</option>"
            "<option value='reject'>reject</option></select>"
            " <input type='text' name='note' placeholder='note (required on reject)'"
            " maxlength='200'/> <button type='submit'>decide</button></form></td></tr>"
        )
    inner = "".join(bits)
    return (
        f"<h3>Pending issues</h3><table><tr><th>text</th>"
        f"<th>author</th><th>decide</th></tr>{inner}</table>"
        if inner
        else "<h3>Pending issues</h3><p style='color:var(--muted)'>None.</p>"
    )


def _open_question_rows(did: int, open_q: list[dict], csrf: str) -> str:
    bits = []
    for q in open_q:
        if not isinstance(q, dict):
            continue
        bits.append(
            f"<tr><td>{esc(q.get('body') or '')}</td>"
            f"<td>{esc(q.get('asker_name') or '?')}</td>"
            f"<td><form method='post' action='/admin/designs/{did}/answer'>"
            f"{csrf}<input type='hidden' name='question_id' value='{int(q['id'])}'/>"
            " <input type='text' name='answer' placeholder='public answer'"
            " maxlength='2000' size='40'/> <button type='submit'>send</button>"
            "</form></td></tr>"
        )
    inner = "".join(bits)
    return (
        f"<h3>Open questions</h3><table><tr><th>question</th>"
        f"<th>asker</th><th>answer</th></tr>{inner}</table>"
        if inner
        else "<h3>Open questions</h3><p style='color:var(--muted)'>None.</p>"
    )


def _accepted_rows(did: int, d: dict, csrf: str) -> str:
    try:
        issues = db.list_issues(did)["issues"]
    except Exception:  # domain: degrade-silently - read failed, empty section
        issues = []
    feats = [f for f in (d.get("features") or []) if isinstance(f, dict)]
    bits = []
    for f in feats:
        bits.append(
            f"<tr><td>feature: {esc(f.get('text') or '')}</td>"
            f"<td><form method='post' action='/admin/designs/{did}/move-item'>"
            f"{csrf}<input type='hidden' name='kind' value='feature'/>"
            f"<input type='hidden' name='item_id' value='{int(f['id'])}'/>"
            "<select name='direction'><option value='up'>up</option>"
            "<option value='down'>down</option></select>"
            " <button type='submit'>move</button></form></td><td></td></tr>"
        )
    for i in issues:
        if not isinstance(i, dict) or i.get("state") != "accepted":
            continue
        bits.append(
            f"<tr><td>issue: {esc(i.get('text') or '')}</td>"
            f"<td><form method='post' action='/admin/designs/{did}/move-item'>"
            f"{csrf}<input type='hidden' name='kind' value='issue'/>"
            f"<input type='hidden' name='item_id' value='{int(i['id'])}'/>"
            "<select name='direction'><option value='up'>up</option>"
            "<option value='down'>down</option></select>"
            " <button type='submit'>move</button></form></td>"
            f"<td><form method='post' action='/admin/designs/{did}/resolve-issue'>"
            f"{csrf}<input type='hidden' name='issue_id' value='{int(i['id'])}'/>"
            " <button type='submit'>resolve</button></form></td></tr>"
        )
    inner = "".join(bits)
    return (
        f"<h3>Accepted (move / resolve)</h3><table><tr><th>item</th>"
        f"<th>move</th><th>resolve</th></tr>{inner}</table>"
        if inner
        else "<h3>Accepted (move / resolve)</h3>"
        "<p style='color:var(--muted)'>Nothing accepted yet.</p>"
    )


async def design_admin_detail_page(request: Request) -> HTMLResponse:
    """One design for the maintainer: pending queues with decide/answer
    affordances, accepted move/resolve and the comments toggle."""
    if not _authorized(request):
        return _denied()
    try:
        design_id = int(request.path_params["design_id"])
    except (KeyError, TypeError, ValueError):
        return _admin_page(request, "admin — designs", "<p>No such design.</p>")
    try:
        admin = _admin_user(request)
        pend = db.admin_design_pending(admin, design_id)
        d = db.get_design(design_id)
    except Exception:  # domain: degrade-silently - unknown id degrades to 404 text
        return _admin_page(request, "admin — designs", "<p>No such design.</p>")
    csrf = _csrf_field(request)
    frozen = pend["status"] != "open"
    # Frozen renders banner + status only: promote/close sweep every pending
    # row, so the queues are empty by construction - state without forms.
    head = (
        f"<p class='meta'>status {esc(pend['status'])} &middot; "
        f"<a href='/designs/{design_id}'>public page</a></p>"
        + ("<p><b>Frozen</b> - read-only, no forms.</p>" if frozen else "")
    )
    if frozen:
        body = (
            _admin_nav()
            + f"<div class='panel'><h2>{esc(d.get('title') or '?')} — admin</h2>"
            + head
            + "</div>"
        )
        return _admin_page(request, f"admin — design {design_id}", body)
    try:
        comments = db.list_design_comments(design_id)
        comments_on = bool(comments["comments_enabled"])
    except Exception:  # domain: degrade-silently - toggle degrades to off
        comments_on = False
    toggle = (
        f"<h3>Comments</h3><form method='post'"
        f" action='/admin/designs/{design_id}/toggle-comments'>{csrf}"
        "<label><input type='checkbox' name='enabled'"
        f"{' checked' if comments_on else ''}/> enabled (needs 24h age)</label>"
        " <button type='submit'>apply</button></form>"
    )
    body = (
        _admin_nav()
        + f"<div class='panel'><h2>{esc(d.get('title') or '?')} — admin</h2>"
        + head
        + _pending_feature_rows(design_id, pend["pending_features"], csrf)
        + _pending_issue_rows(design_id, pend["pending_issues"], csrf)
        + _open_question_rows(design_id, pend["open_questions"], csrf)
        + _accepted_rows(design_id, d, csrf)
        + toggle
        + "</div>"
    )
    return _admin_page(request, f"admin — design {design_id}", body)


async def _design_action(request, fn):
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


def _decision(form, what: str) -> tuple[bool, str]:
    decision = (form.get("decision") or "").strip()
    if decision not in ("approve", "reject"):
        raise db.ForumError(f"{what} decision must be approve or reject.")
    note = (form.get("note") or "").strip()
    if decision == "reject" and not note:
        raise db.ForumError("rejecting requires a note to the author.")
    return decision == "approve", note


async def design_admin_decide_feature(request):
    async def _run(admin, form, request):
        did = int(request.path_params["design_id"])
        fid = int(form.get("feature_id") or 0)
        approve, note = _decision(form, "feature")
        db.admin_decide_feature(admin, did, fid, approve, note=note)
        return (
            f"Feature #{fid} on design #{did} {'accepted' if approve else 'rejected'}."
        )

    return await _design_action(request, _run)


async def design_admin_decide_issue(request):
    async def _run(admin, form, request):
        did = int(request.path_params["design_id"])
        iid = int(form.get("issue_id") or 0)
        approve, note = _decision(form, "issue")
        db.admin_decide_issue(admin, did, iid, approve, note=note)
        return f"Issue #{iid} on design #{did} {'accepted' if approve else 'rejected'}."

    return await _design_action(request, _run)


async def design_admin_resolve_issue(request):
    async def _run(admin, form, request):
        did = int(request.path_params["design_id"])
        iid = int(form.get("issue_id") or 0)
        db.admin_resolve_issue(admin, did, iid)
        return f"Issue #{iid} on design #{did} resolved."

    return await _design_action(request, _run)


async def design_admin_move_item(request):
    async def _run(admin, form, request):
        did = int(request.path_params["design_id"])
        kind = (form.get("kind") or "").strip()
        iid = int(form.get("item_id") or 0)
        direction = (form.get("direction") or "").strip()
        db.admin_move_design_item(admin, did, kind, iid, direction)
        return f"{kind} #{iid} on design #{did} moved {direction}."

    return await _design_action(request, _run)


async def design_admin_answer(request):
    async def _run(admin, form, request):
        did = int(request.path_params["design_id"])
        qid = int(form.get("question_id") or 0)
        answer = (form.get("answer") or "").strip()
        if not answer:
            raise db.ForumError("an answer is required.")
        db.admin_answer_question(admin, did, qid, answer)
        return f"Question #{qid} on design #{did} answered."

    return await _design_action(request, _run)


async def design_admin_toggle_comments(request):
    async def _run(admin, form, request):
        did = int(request.path_params["design_id"])
        enabled = bool(form.get("enabled"))
        db.admin_enable_comments(admin, did, enabled=enabled)
        return f"Comments on design #{did} {'enabled' if enabled else 'disabled'}."

    return await _design_action(request, _run)
