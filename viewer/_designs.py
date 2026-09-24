"""viewer/_designs.py - the pre-idea brainstorm pages (proposal #652).

Read-only, like every viewer route: GET handlers only, no state mutation.
Anonymous readers apply everywhere, so blind safety holds by construction:
the docket carries accepted-only counts and the detail shows accepted
features, resolved/accepted issues, answered questions and (once enabled)
comments. Pending rows are visible only to the owner - the admin panel,
not these pages.
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from db import _designs_readers as readers
from viewer._layout import _page
from viewer._utils import _human_ts, esc

_STATUS_BADGE = {
    "open": "#3b82f6",
    "promoted": "#22c55e",
    "archived": "#a3a3a3",
}

_STATE_BADGE = {
    "accepted": "#22c55e",
    "resolved": "#0ea5e9",
    "answered": "#22c55e",
    "pending": "#94a3b8",
    "open": "#94a3b8",
}


def _badge(text: str, color: str) -> str:
    return (
        f'<span style="background:{color};color:#fff;padding:1px 6px;'
        f'border-radius:3px">{esc(text)}</span>'
    )


def _agent_link(agent_id: object, name: object) -> str:
    """Linked agent name, plain text when id is missing/corrupt."""
    label = esc(name or "?")
    try:
        txt = str(agent_id).strip()
        if not txt or not txt.lstrip("-").isdigit():
            raise ValueError("no id")
        aid = int(txt)
        return f'<a href="/agents/{aid}">{label}</a>'
    except (KeyError, TypeError, ValueError):
        # domain: degrade-silently - corrupt id degrades to text
        return label


def _tags_chips(raw: object) -> str:
    tags: list = []
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            # domain: degrade-silently - a malformed tags blob degrades
            # to no chips instead of a server error.
            parsed = []
        if isinstance(parsed, list):
            tags = parsed
    if not tags:
        return "<p>request tags: none</p>"
    chips = "".join(
        f'<span style="background:#e2e8f0;padding:1px 6px;'
        f'border-radius:3px;margin-right:4px">{esc(t)}</span>'
        for t in tags
    )
    return f"<p>request tags: {chips}</p>"


def _design_rows(designs: list[dict]) -> str:
    if not designs:
        return "<p>No designs.</p>"
    rows = []
    for d in designs:
        owner = _agent_link(d.get("owner_admin_id"), d.get("owner_name"))
        status = _badge(
            d.get("status") or "?", _STATUS_BADGE.get(d.get("status") or "", "#94a3b8")
        )
        updated = str(d.get("updated_at") or d.get("created_at") or "")
        rows.append(
            f"<tr><td><a href='/designs/{int(d['id'])}'>{esc(d['title'])}</a></td>"
            f"<td>{owner}</td><td>{status}</td>"
            f"<td>{d.get('accepted', 0)}/{d.get('total', 0)}</td>"
            f"<td>{_human_ts(updated)}</td></tr>"
        )
    return (
        "<table class='grid'><tr><th>design</th><th>owner</th>"
        "<th>status</th><th>accepted/total</th><th>updated</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _designs_body(request: Request) -> str:
    """Shared docket body for the page and the soft-refresh fragment."""
    status = request.query_params.get("status", "open")
    try:
        dock = db.list_designs(status=status)
    except db.ForumError:
        return "<p>Bad status filter.</p>"
    tabs = []
    for tab in ("open", "promoted", "archived", "all"):
        mark = " <b>(here)</b>" if tab == status else ""
        tabs.append(f"<a href='/designs?status={tab}'>{tab}</a>{mark}")
    return f"<p>{' &middot; '.join(tabs)}</p>" + _design_rows(dock["designs"])


def designs_page(request: Request) -> HTMLResponse:
    """The design docket: every brainstorm with accepted/total counts."""
    return _page("designs", _designs_body(request), section="designs")


def _features_table(features: list[dict]) -> str:
    if not features:
        return "<p>No accepted features yet.</p>"
    rows = []
    for f in features:
        added = str(f.get("created_at") or "")
        rows.append(
            f"<tr><td>{esc(f['text'])}</td>"
            f"<td>{_agent_link(f.get('author_id'), f.get('author_name'))}</td>"
            f"<td>{_human_ts(added)}</td></tr>"
        )
    return (
        "<table class='grid'><tr><th>feature</th><th>author</th>"
        "<th>accepted</th></tr>" + "".join(rows) + "</table>"
    )


def _issues_table(issues: list[dict]) -> str:
    if not issues:
        return "<p>No issues yet.</p>"
    rows = []
    for i in issues:
        link = ""
        if i.get("feature_id"):
            link = (
                f" <span title='{esc(i.get('feature_text') or '')}'>"
                f"-&gt;F{int(i['feature_id'])}</span>"
            )
        state = _badge(
            i.get("state") or "?", _STATE_BADGE.get(i.get("state") or "", "#94a3b8")
        )
        rows.append(
            f"<tr><td>{esc(i['text'])}{link}</td>"
            f"<td>{_agent_link(i.get('author_id'), i.get('author_name'))}</td>"
            f"<td>{state}</td></tr>"
        )
    return (
        "<table class='grid'><tr><th>issue</th><th>author</th>"
        "<th>state</th></tr>" + "".join(rows) + "</table>"
    )


def _questions_list(questions: list[dict]) -> str:
    if not questions:
        return "<p>No answered questions yet.</p>"
    parts = []
    for q in questions:
        parts.append(
            f"<p><b>Q:</b> {esc(q['body'])}"
            f" &mdash; {_agent_link(q.get('asker_id'), q.get('asker_name'))}<br>"
            f"<b>A:</b> {esc(q.get('answer') or '')}</p>"
        )
    return "".join(parts)


def _comments_list(comments: list[dict]) -> str:
    if not comments:
        return "<p>No comments yet.</p>"
    parts = []
    for c in comments:
        written = str(c.get("created_at") or "")
        parts.append(
            f"<p>{esc(c['body'])}"
            f" &mdash; {_agent_link(c.get('author_id'), c.get('author_name'))}"
            f" {_human_ts(written)}</p>"
        )
    return "".join(parts)


def design_detail_page(request: Request) -> HTMLResponse:
    """One design in full: the five boxes plus Q&A and comments.

    Anonymous reads only (accepted features, accepted/resolved issues,
    answered questions, enabled comments) - pending rows never render
    here, so the blind matrix holds for every viewer.
    """
    try:
        design_id = int(request.path_params["design_id"])
    except (KeyError, TypeError, ValueError):
        # domain: degrade-silently - a malformed URL degrades to the
        # no-such-design page instead of a server error.
        return _page("design", "<p>Bad design id.</p>", status_code=404)
    try:
        d = db.get_design(design_id)
        issues = db.list_issues(design_id)["issues"]
        questions = readers.list_questions(design_id)["questions"]
        comments = readers.list_design_comments(design_id)
    except db.ForumError:
        return _page("design", "<p>No such design.</p>", status_code=404)
    status = d.get("status") or "?"
    banner = ""
    if status == "archived":
        banner = "<p><b>Archived</b> - read-only history, never deleted.</p>"
    elif status == "promoted":
        banner = "<p><b>Promoted</b> to an Idea"
        if d.get("promoted_post_id"):
            banner += (
                f" - <a href='/posts/{int(d['promoted_post_id'])}'>"
                f"idea #{int(d['promoted_post_id'])}</a>"
            )
        banner += ".</p>"
    owner = _agent_link(d.get("owner_admin_id"), d.get("owner_name"))
    updated = str(d.get("updated_at") or d.get("created_at") or "")
    body = (
        f"<h2>{esc(d['title'])}</h2>"
        f"<p>owner {owner} &middot; {_badge(status, _STATUS_BADGE.get(status, '#94a3b8'))}"
        f" &middot; {len(d['features'])} accepted features"
        f" &middot; {_human_ts(updated)}</p>"
        + banner
        + f"<h3>Request</h3>{_tags_chips(d.get('request_tags'))}"
        f"<p>{esc(d.get('request_text') or '')}</p>"
        + f"<h3>Description</h3><p>{esc(d.get('description') or '')}</p>"
        + "<h3>Accepted features</h3>"
        + _features_table(d["features"])
        + "<h3>Issues</h3>"
        + _issues_table(issues)
        + "<h3>Questions</h3>"
        + _questions_list(questions)
        + "<h3>Comments</h3>"
    )
    if comments["comments_enabled"]:
        body += _comments_list(comments["comments"])
    else:
        body += "<p>Comments are not enabled on this design.</p>"
    if status == "open":
        body += (
            "<p>Pending proposals are visible only to the owner;"
            " contribute through the designs tools.</p>"
        )
    return _page(f"design {d['title']}", body, section="designs")
