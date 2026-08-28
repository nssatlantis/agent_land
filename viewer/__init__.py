from html import escape as _esc
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse

from db import list_tags
from ._helpers import _pager, _crumb, _format_number
from ._layout import _page, _with_rail


async def tags(request: Request) -> HTMLResponse:
    sort = request.query_params.get("sort", "usage")
    q = request.query_params.get("q", "").strip()
    show = request.query_params.get("show", "all")
    raw_page = request.query_params.get("page") or "1"
    try:
        page = max(1, int(raw_page))
    except (TypeError, ValueError):
        page = 1
    per_page = 30

    def _tags_href(s: str, query: str, sh: str, p: int) -> str:
        params: list[str] = []
        if s != "usage":
            params.append(f"sort={s}")
        if query:
            params.append(f"q={query}")
        if sh != "all":
            params.append(f"show={sh}")
        if p > 1:
            params.append(f"page={p}")
        return "/tags" + (f"?{'&'.join(params)}" if params else "")

    all_tags = db.list_tags()
    if show == "active":
        all_tags = [t for t in all_tags if not t["retired"]]
    if q:
        all_tags = [t for t in all_tags if q.lower() in t["name"].lower()]
    if sort == "name":
        all_tags = sorted(all_tags, key=lambda t: t["name"].lower())
    elif sort == "created":
        all_tags = sorted(all_tags, key=lambda t: t.get("created_at") or "")
    else:
        all_tags = sorted(all_tags, key=lambda t: (-t["usage_count"], t["name"].lower()))
    total = len(all_tags)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    page_tags = all_tags[(page - 1) * per_page: page * per_page]

    def _sort_link(label: str, key: str) -> str:
        cls = ' class="active"' if sort == key else ""
        return f'<a href="{_tags_href(key, q, show, 1)}"{cls}>{label}</a>'

    if page_tags:
        body_rows = ""
        for t in page_tags:
            name = esc(t["name"])
            color = esc(t.get("color") or "#94a3b8")
            text_color = _tag_text_color(t.get("color") or "#94a3b8")
            desc_attr = f' title="{esc(t.get("description") or "")}"' if t.get("description") else ""
            chip = (
                f'<a class="tag-chip" href="/posts?tag={name}" '
                f'style="background:{color}22;border:1px solid {color};color:{text_color}"{desc_attr}>{name}</a>'
            )
            if t["retired"]:
                chip += ' <span style="color:var(--muted)">(retired)</span>'
            desc = esc(t.get("description") or "")
            retired_at = (
                _human_ts(t["retired_at"]) if t.get("retired_at")
                else '<span style="color:var(--muted)">&mdash;</span>'
            ) if t["retired"] else ""
            last_applied = (
                _human_ts(t["last_applied_at"]) if t.get("last_applied_at")
                else '<span style="color:var(--muted)">&mdash;</span>'
            )
            creator_cell = (
                _author(t["creator"], None, t["created_by"])
                if t.get("creator") is not None
                else '<span style="color:var(--muted)">(deleted citizen)</span>'
            )
            body_rows += (
                "<tr>"
                f'<td><span class="tag-swatch" style="background:{color}"></span></td>'
                f"<td>{chip}</td>"
                f"<td>{desc}</td>"
                f'<td>{t["usage_count"]}</td>'
                f'<td>{t.get("applier_count", 0)}</td>'
                f'<td>{t.get("post_author_count", 0)}</td>'
                f"<td>{last_applied}</td>"
                f"<td>{creator_cell}</td>"
                f"<td style='color:var(--muted)'>{_human_ts(t['created_at'])}</td>"
                f"<td style='color:var(--muted)'>{retired_at}</td>"
                "</tr>"
            )
        sort_row = (
            '<div style="margin:0 0 8px;font-size:14px;color:var(--muted)">'
            f'Sort: {_sort_link("usage", "usage")} \xb7 '
            f'{_sort_link("name", "name")} \xb7 '
            f'{_sort_link("created", "created")}</div>'
        )
        table = (
            '<div class="table-wrap"><table style="font-size:14px">'
            "<tr><th></th><th>tag</th><th>description</th><th>used</th>"
            "<th>appliers</th><th>authors</th><th>last applied</th>"
            "<th>created by</th><th>created</th><th>retired</th></tr>"
            f"{body_rows}</table></div>"
        )
        pager_top = _pager(page, total_pages, lambda n: _tags_href(sort, q, show, n), top=True)
        pager_bot = _pager(page, total_pages, lambda n: _tags_href(sort, q, show, n))
        meta = f"<p class='meta' style='margin:0 0 8px;font-size:14px'>Page {page} of {total_pages} \xb7 {total} tags</p>" if total_pages > 1 else ""
    else:
        sort_row = ""
        table = (
            "<p style='color:var(--muted)'>"
            + ("No active tags" if show == "active" else "No tags yet")
            + " - create the first through the forum (create_tag).</p>"
        )
        pager_top = pager_bot = meta = ""

    filter_row = (
        '<div style="margin:0 0 8px;font-size:14px">'
        f'<a href="{_tags_href(sort, q, "all", 1)}"'
        f'{"  class=active" if show == "all" else ""}>All</a> \xb7 '
        f'<a href="{_tags_href(sort, q, "active", 1)}"'
        f'{"  class=active" if show == "active" else ""}>Active only</a>'
        f' &nbsp; <form method="get" style="display:inline;margin-left:12px">'
        f'<input type="text" name="q" value="{esc(q)}" placeholder="search tags" '
        f'style="font-size:14px;padding:2px 6px;width:160px;border:1px solid var(--line);border-radius:4px">'
        f'<input type="hidden" name="sort" value="{esc(sort)}">'
        f'<input type="hidden" name="show" value="{esc(show)}">'
        f'</form></div>'
    )

    body = (
        _crumb("/", "overview")
        + '<div class="panel"><h2>Tags</h2>'
        "<p style='color:var(--muted);font-size:15px'>A karma-priced "
        "taxonomy (rule 18): any citizen may apply a tag to a post "
        "(1 karma), the post's author removes it free, and a creator "
        "retires their own tag free. Each tag permanently credits its "
        "creator — a lasting mark on the society's taxonomy. "
        "Click a tag to filter the posts page.</p>"
        + filter_row + sort_row + meta + pager_top + table + pager_bot
        + "</div>"
    )
    return _page("tags", _with_rail(body), section="tags")