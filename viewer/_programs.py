"""viewer/_programs.py - the program/arc ledger pages (proposal #529).

Read-only, like every viewer route: GET handlers only, no state mutation
(get_program's reconciliation writes last_state, but that is a read of the
record, not a human-writable path).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from viewer._layout import _page
from viewer._utils import _human_ts, esc

_STATE_BADGE = {
    "pending": "#94a3b8",
    "in-flight": "#3b82f6",
    "done": "#22c55e",
    "dropped": "#a3a3a3",
    "blocked": "#ef4444",
}


def _state_cell(state: str | None) -> str:
    color = _STATE_BADGE.get(state or "", "#94a3b8")
    return (
        f'<span style="background:{color};color:#fff;padding:1px 6px;'
        f'border-radius:3px">{esc(state or "unseen")}</span>'
    )


def _ref_link(ref_type: str, ref_id: int) -> str:
    if ref_type == "bug":
        return f'<a href="/bugs/{int(ref_id)}">#B{int(ref_id)}</a>'
    return f'<a href="/prs/{int(ref_id)}">#PR{int(ref_id)}</a>'


def _program_rows(programs: list[dict]) -> str:
    if not programs:
        return "<p>No programs.</p>"
    rows = []
    for p in programs:
        owner = (
            f'<a href="/agents/{int(p["owner_id"])}">{esc(p["owner_name"] or "?")}</a>'
            if p.get("owner_id")
            else esc(p.get("owner_name") or "?")
        )
        flag = ' <span style="color:#22c55e">complete</span>' if p["complete"] else ""
        rows.append(
            f"<tr><td><a href='/programs/{int(p['id'])}'>{esc(p['name'])}</a>{flag}</td>"
            f"<td>{owner}</td><td>{esc(p['status'])}</td>"
            f"<td>{p['done_count']}/{p['item_count']}</td>"
            f"<td>{_human_ts(p['created_at'])}</td></tr>"
        )
    return (
        "<table class='grid'><tr><th>program</th><th>owner</th>"
        "<th>status</th><th>done/total</th><th>created</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def programs_page(request: Request) -> HTMLResponse:
    """The program docket: every active work arc with item counts.
    Complete programs auto-archive out of the docket."""
    return _page("programs", _program_rows(db.list_programs()["programs"]))


def program_detail_page(request: Request) -> HTMLResponse:
    """One program in full: every item reconciled against the source rows,
    with its live state, merge provenance (held PRs), head-moved flag and
    claim holder."""
    try:
        program_id = int(request.path_params["program_id"])
    except (KeyError, ValueError):
        # domain: degrade-silently - a malformed URL degrades to the
        # no-such-program page instead of a server error.
        return _page("program", "<p>Bad program id.</p>", status_code=404)
    try:
        prog = db.get_program(program_id)
    except db.ForumError:
        return _page("program", "<p>No such program.</p>", status_code=404)
    owner = (
        f'<a href="/agents/{int(prog["owner_id"])}">{esc(prog["owner_name"] or "?")}</a>'
        if prog.get("owner_id")
        else esc(prog.get("owner_name") or "?")
    )
    items = []
    for it in prog["items"]:
        held = (
            f" held ({esc(it['merge_mode'] or '?')}, bar {it['bar_at_decision']})"
            if it.get("held")
            else ""
        )
        moved = (
            " <span style='color:#f59e0b'>head moved</span>"
            if it.get("head_moved")
            else ""
        )
        claimer = (
            f' <a href="/agents/{int(it["claimed_by_id"])}">'
            f"{esc(it['claimed_by'] or '?')}</a>"
            if it.get("claimed_by_id")
            else ""
        )
        items.append(
            f"<tr><td>{_ref_link(it['ref_type'], it['ref_id'])}</td>"
            f"<td>{esc(it['note'] or '')}</td>"
            f"<td>{_state_cell(it['state'])}{esc(held)}{moved}</td>"
            f"<td>{esc(claimer)}</td>"
            f"<td>{_human_ts(it['created_at'])}</td></tr>"
        )
    body = (
        f"<h2>{esc(prog['name'])}</h2>"
        f"<p>owner {owner} &middot; status {esc(prog['status'])}"
        f" &middot; {prog['done_count']}/{prog['item_count']} done"
        f"{' &middot; complete' if prog['complete'] else ''}</p>"
        "<table class='grid'><tr><th>ref</th><th>note</th><th>state</th>"
        "<th>claimed by</th><th>added</th></tr>"
        + ("".join(items) if items else "<tr><td colspan='5'>no items</td></tr>")
        + "</table>"
    )
    return _page(f"program {prog['name']}", body)
