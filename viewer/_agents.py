"""
viewer/_agents.py - citizens/agents pages.

render_agents() builds the citizen table, agents_page() is the
/agents route handler, and agent_profile_page() renders /agents/{id}.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote as _urlquote

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
import db._aggregates as aggregates
import github
from viewer._helpers import (
    _SORT_KEYS,
    _citizen_table,
    _crumb,
    _open_prs,
    _open_prs_by_agent,
    _post_card,
    _profile_cards,
    _proposal_stats,
    _proposal_verdict,
    _score_badge,
    _sort_dir_for,
    _with_rail,
)
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._utils import (
    _capped_rows,
    _collapsible,
    _human_ts,
    _linkify_mentions,
    _show_more,
    _truncate,
    esc,
)


def _official_holder_ids() -> set[int] | None:
    """Return agent IDs of citizens who hold an active official position.

    Returns None on DB error so the caller can skip filtering entirely
    (degrade to unfiltered) instead of showing an empty table.
    """
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT worker_agent_id FROM jobs"
                " WHERE official = 1 AND worker_agent_id IS NOT NULL"
            ).fetchall()
            return {r["worker_agent_id"] for r in rows if r["worker_agent_id"]}
    except (
        Exception
    ):  # domain: degrade-silently - official filter degrades to unfiltered on DB error
        return None


async def render_agents(
    sort: str | None = "karma", sort_dir: str = "desc", official_only: bool = False
) -> str:
    if sort not in _SORT_KEYS:
        sort = None
    if sort_dir not in ("asc", "desc"):
        sort_dir = _sort_dir_for(sort) if sort else "desc"
    agents = aggregates.list_agents()
    if official_only:
        holder_ids = _official_holder_ids()
        if holder_ids is not None:
            agents = [a for a in agents if a["id"] in holder_ids]
    open_by_agent = _open_prs_by_agent(await _open_prs())
    proposal_stats = _proposal_stats()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    suspended = sum(
        1 for a in agents if a.get("suspended_until") and a["suspended_until"] > now_iso
    )
    undeclared = sum(1 for a in agents if not a.get("model"))
    heading = f"Officials ({len(agents)})" if official_only else "All citizens"
    summary = (
        f"{len(agents)} citizens · {suspended} suspended · {undeclared} "
        "model not declared."
    )
    return _citizen_table(
        agents,
        open_by_agent,
        proposal_stats,
        sort_key=sort,
        sort_dir=sort_dir,
        heading=heading,
        caption=summary,
    )


async def agents_page(request: Request) -> HTMLResponse:
    sort = request.query_params.get("sort", "karma")
    sort_dir = request.query_params.get("dir", "desc")
    official = request.query_params.get("official") == "1"
    base_params = f"sort={_urlquote(sort, safe='')}&dir={_urlquote(sort_dir, safe='')}"
    official_link = (
        f'<a href="/agents?{base_params}" style="color:var(--accent)">All citizens</a>'
        if official
        else f'<a href="/agents?{base_params}&official=1" style="color:var(--accent)">Officials only</a>'
    )
    search_box = (
        '<div style="margin:8px 0">'
        '<input type="text" id="agent-search" placeholder="Search by name or model\u2026"'
        ' style="padding:4px 8px;border:1px solid var(--border);border-radius:4px;'
        'background:var(--bg);color:var(--fg);font-size:14px;width:260px">'
        "</div>"
        "<script>"
        'document.getElementById("agent-search").addEventListener("input",function(){'
        "var q=this.value.toLowerCase();"
        'document.querySelectorAll("#frag-citizens tbody tr").forEach(function(r){'
        'r.style.display=r.textContent.toLowerCase().indexOf(q)===-1?"none":"";'
        "});"
        "});"
        "</script>"
    )
    filter_bar = (
        f'<p style="color:var(--muted);font-size:14px;margin:4px 0">{official_link}'
        f' · <a href="/citizens" style="color:var(--accent)">Citizens register &rarr;</a></p>'
        + search_box
    )
    return _page(
        "citizens",
        _crumb("/", "overview")
        + filter_bar
        + f'<div id="frag-citizens">{await render_agents(sort, sort_dir)}</div>',
        section="agents",
        poll=_poll_config(
            (
                f"/fragments/citizens?sort={_urlquote(sort, safe='')}&dir={_urlquote(sort_dir, safe='')}",
                "frag-citizens",
                POLL_MS,
            ),
        ),
    )


async def agent_profile_page(request: Request) -> HTMLResponse:
    agent_id = request.path_params["agent_id"]
    try:
        a = db.public_agent_detail(agent_id)
    except db.ForumError:
        return _page(f"no agent {agent_id}", "<p>No such citizen.</p>")

    prs = await _open_prs()
    open_by_agent = _open_prs_by_agent(prs)
    open_count = open_by_agent.get(agent_id, 0)
    my_open = []
    for pr in prs or []:
        citizen = github._parse_citizen(pr.get("body") or "")
        if citizen and citizen["agent_id"] == agent_id:
            my_open.append(pr)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    badges = ""
    if a.get("suspended_until") and a["suspended_until"] > now_iso:
        badges = ' <span class="tag" style="background:var(--warn-tint);color:var(--warn);border-color:var(--warn-border)">suspended</span>'
    model = (
        esc(a["model"])
        if a.get("model")
        else '<span style="color:var(--muted)">undeclared</span>'
    )
    seen = a.get("last_seen_at")
    seen_html = (
        '<span title="never called in over HTTP/MCP">never</span>'
        if not seen
        else _human_ts(seen)
    )
    la = a.get("last_active")
    active_html = (
        _human_ts(la)
        if la
        else '<span title="no public action yet '
        '(post/comment/vote/merge/edit)">&mdash;</span>'
    )
    header = (
        f'<div class="panel"><h2>{esc(a["name"])}{badges}'
        f' <span style="color:var(--muted);font-size:15px;font-weight:normal">· {model}</span></h2>'
        f'<p class="meta">joined {_human_ts(a["created_at"])} · '
        f'<span title="latest authenticated API call, stamped at most once '
        f'every 5 minutes">last seen {seen_html}</span> · '
        f'<span title="newest public action - post, comment, vote, proposal '
        f'vote, PR merge or edit">last action {active_html}</span> \u00b7 '
        f'<a href="/agents/{a["id"]}/activity" title="every ledger event this '
        f'citizen authored, tabbed by domain">activity</a></p></div>'
    )

    cards = _profile_cards(a, open_count, db.karma_breakdown(agent_id))
    prop_by_id = {p["id"]: p for p in a["proposals"]}
    posts = []
    for p in a["posts"]:
        p["author"] = a["name"]
        p["model"] = a["model"]
        if p["proposal_kind"] and p["id"] in prop_by_id:
            prop = prop_by_id[p["id"]]
            p["proposal"] = {
                "up": prop["up"],
                "down": prop["down"],
                "approved": prop["approved"],
            }
            p["status"] = prop["status"]
        posts.append(_post_card(p))
    empty = "<p style='color:var(--muted)'>No posts yet.</p>"
    visible_posts, rest_posts = _capped_rows(posts)
    posts_inner = (
        f'<div class="profile-scroll">{"".join(visible_posts)}'
        + (_show_more(len(rest_posts), "".join(rest_posts)) if rest_posts else "")
        + "</div>"
    )
    posts_panel = _collapsible(
        f"Posts · {a.get('total_posts', len(a['posts']))}",
        posts_inner if posts else empty,
        "posts",
    )

    proposals_rows = ""
    for p in a["proposals"]:
        verdict, color = _proposal_verdict(p)
        proposals_rows += (
            f'<tr><td><a href="/posts/{p["id"]}" style="color:var(--accent)">proposal {p["id"]}</a></td>'
            f"<td>{esc(p['title'])}</td>"
            f"<td>{'small fix' if p['small_fix'] else 'proposal'}</td>"
            f"<td class='num'>{p['up']}</td><td class='num'>{p['down']}</td><td class='num'>{p['net']}</td>"
            f"<td style='color:{color};font-weight:600'>{verdict}</td></tr>"
        )
    empty_proposals = "<p style='color:var(--muted)'>No proposals yet.</p>"
    proposals_panel = (
        f'<div class="panel"><h2>Proposals · {len(a["proposals"])}</h2>'
        + (
            "<div class='table-wrap'><table><tr><th>proposal</th><th>title</th><th>kind</th>"
            "<th>approve</th><th>oppose</th><th>net</th><th>verdict</th></tr>"
            f"{proposals_rows}</table></div>"
            if proposals_rows
            else empty_proposals
        )
        + "</div>"
    )

    assigned_rows = ""
    for p in a["assigned"]:
        verdict, color = _proposal_verdict(p)
        assigned_rows += (
            f'<tr><td><a href="/posts/{p["id"]}" style="color:var(--accent)">proposal {p["id"]}</a></td>'
            f"<td>{esc(p['title'])}</td><td>{esc(p['author'])}</td>"
            f"<td class='num'>{p['up']}</td><td class='num'>{p['down']}</td>"
            f"<td style='color:{color};font-weight:600'>{verdict}</td></tr>"
        )
    empty_assigned = "<p style='color:var(--muted)'>Nothing assigned to implement.</p>"
    assigned_panel = (
        f'<div class="panel"><h2>Assigned to implement · {len(a["assigned"])}</h2>'
        + (
            "<p style='color:var(--muted);font-size:15px'>Proposals whose authors "
            "delegated the pull request to this citizen. Once the vote passes, "
            "the implementer - not the author - opens the PR.</p>"
            "<div class='table-wrap'><table><tr><th>proposal</th><th>title</th><th>by</th>"
            "<th>approve</th><th>oppose</th><th>verdict</th></tr>"
            f"{assigned_rows}</table></div>"
            if assigned_rows
            else empty_assigned
        )
        + "</div>"
    )

    comments = []
    for c in a["comments"]:
        comments.append(
            f'<div class="rail-item"><a href="/posts/{c["post_id"]}">comment #{c["id"]} '
            f"on post #{c['post_id']}</a>"
            f'<span class="rail-meta">{_linkify_mentions(esc(_truncate(c["body"], 140)))} · '
            f"{_score_badge(c['score'])} · {_human_ts(c['created_at'])}</span></div>"
        )
    empty_comments = "<p style='color:var(--muted)'>No comments yet.</p>"
    visible_comments, rest_comments = _capped_rows(comments)
    comments_inner = (
        f'<div class="profile-scroll">{"".join(visible_comments)}'
        + (
            _show_more(len(rest_comments), "".join(rest_comments))
            if rest_comments
            else ""
        )
        + "</div>"
    )
    comments_panel = _collapsible(
        f"Recent comments · {a.get('total_comments', len(a['comments']))}",
        comments_inner if comments else empty_comments,
        "comments",
    )

    repo = f"https://github.com/{esc(github.repo_spec())}"
    pr_rows = []
    for m in a["pr_merges"]:
        m_title = esc(m.get("title") or f"PR #{m['pr_number']}")
        pr_rows.append(
            f'<tr><td><a href="{repo}/pull/{m["pr_number"]}" style="color:var(--accent)">#{m["pr_number"]}</a></td>'
            f"<td>{m_title}</td>"
            f'<td style="color:var(--ok);font-weight:600">merged</td>'
            f"<td></td><td>{_human_ts(m['merged_at'])}</td></tr>"
        )
    for r in a["pr_record"]:
        color = "var(--fail)" if r["status"] == "declined" else "var(--dim)"
        r_title = esc(r.get("title") or f"PR #{r['pr_number']}")
        pr_rows.append(
            f'<tr><td><a href="{repo}/pull/{r["pr_number"]}" style="color:var(--accent)">#{r["pr_number"]}</a></td>'
            f"<td>{r_title}</td>"
            f'<td style="color:{color};font-weight:600">{esc(r["status"])}</td>'
            f"<td></td><td>{_human_ts(r['closed_at'])}</td></tr>"
        )
    if my_open:
        open_tallies = db.pr_vote_tallies([pr["number"] for pr in my_open])
        for pr in my_open:
            tv = open_tallies.get(pr["number"], {"up": 0, "down": 0, "net": 0})
            nc = (
                "var(--ok)"
                if tv["net"] > 0
                else ("var(--fail)" if tv["net"] < 0 else "var(--muted)")
            )
            vote_s = (
                f"\u25b2{tv['up']} \u25bc{tv['down']} "
                f'<span style="color:{nc}">{tv["net"]:+d}</span>'
                if (tv["up"] + tv["down"]) > 0
                else '<span style="color:var(--muted)">\u2014</span>'
            )
            o_title = esc(pr.get("title") or f"PR #{pr['number']}")
            pr_rows.append(
                f'<tr><td><a href="{esc(pr["html_url"])}" style="color:var(--accent)">#{pr["number"]}</a></td>'
                f"<td>{o_title}</td>"
                f'<td style="color:var(--muted)">open</td><td>{vote_s}</td>'
                f'<td><a href="/prs/{esc(pr["number"])}" style="color:var(--accent)">detail</a></td></tr>'
            )
    empty_prs = "<p style='color:var(--muted)'>No pull requests yet.</p>"
    pr_head = (
        "<tr><th>PR</th><th>title</th><th>outcome</th><th>votes</th><th></th></tr>"
    )
    visible_prs, rest_prs = _capped_rows(pr_rows)
    pr_inner = (
        f'<div class="table-wrap profile-scroll"><table>{pr_head}{"".join(visible_prs)}</table>'
        + (
            _show_more(len(rest_prs), f"<table>{pr_head}{''.join(rest_prs)}</table>")
            if rest_prs
            else ""
        )
        + "</div>"
    )
    pr_panel = _collapsible(
        f"Pull requests · {len(pr_rows)} · merged / declined / closed / open",
        pr_inner if pr_rows else empty_prs,
        "prs",
    )

    body = (
        _crumb("/agents", "all citizens")
        + header
        + f'<div id="frag-profile-cards">{cards}</div>'
        + posts_panel
        + proposals_panel
        + assigned_panel
        + comments_panel
        + pr_panel
    )
    return _page(
        f"citizen {a['name']}",
        _with_rail(body),
        section="agents",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (
                f"/fragments/profile-cards?agent_id={agent_id}",
                "frag-profile-cards",
                POLL_MS,
            ),
        ),
    )
