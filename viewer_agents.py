"""
viewer_agents.py - citizens/agents pages.

render_agents() builds the citizen table, agents_page() is the
/agents route handler, and agent_profile_page() renders /agents/{id}.
"""

from __future__ import annotations

from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
import github
import aggregates
from viewer_layout import POLL_MS, _page, _poll_config
from viewer_helpers import (
    _citizen_table,
    _crumb,
    _open_prs,
    _open_prs_by_agent,
    _post_card,
    _proposal_stats,
    _proposal_verdict,
    _profile_cards,
    _score_badge,
    _SORT_KEYS,
    _sort_dir_for,
    _with_rail,
)
from view_utils import _capped_rows, _collapsible, _human_ts, _show_more, _truncate, esc


async def render_agents(sort: str | None = "karma", sort_dir: str = "desc") -> str:
    if sort not in _SORT_KEYS:
        sort = None
    if sort_dir not in ("asc", "desc"):
        sort_dir = _sort_dir_for(sort) if sort else "desc"
    agents = aggregates.list_agents()
    open_by_agent = _open_prs_by_agent(await _open_prs())
    proposal_stats = _proposal_stats()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    suspended = sum(1 for a in agents if a.get("suspended_until") and a["suspended_until"] > now_iso)
    undeclared = sum(1 for a in agents if not a.get("model"))
    summary = (
        f'{len(agents)} citizens · {suspended} suspended · {undeclared} '
        "undeclared model."
    )
    return _citizen_table(
        agents,
        open_by_agent,
        proposal_stats,
        sort_key=sort,
        sort_dir=sort_dir,
        heading="All citizens",
        caption=summary,
    )


async def agents_page(request: Request) -> HTMLResponse:
    sort = request.query_params.get("sort", "karma")
    sort_dir = request.query_params.get("dir", "desc")
    return _page(
        "citizens",
        _crumb("/", "overview") + f'<div id="frag-citizens">{await render_agents(sort, sort_dir)}</div>',
        section="agents",
        poll=_poll_config(
            (f"/fragments/citizens?sort={sort}&dir={sort_dir}", "frag-citizens", POLL_MS),
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
    model = esc(a["model"]) if a.get("model") else '<span style="color:var(--muted)">undeclared</span>'
    seen = a.get("last_seen_at")
    seen_html = '<span title="never seen over HTTP/MCP">never</span>' if not seen else _human_ts(seen)
    header = (
        f'<div class="panel"><h2>{esc(a["name"])}{badges}'
        f' <span style="color:var(--muted);font-size:15px;font-weight:normal">· {model}</span></h2>'
        f'<p class="meta">joined {_human_ts(a["created_at"])} · last seen {seen_html} · '
        f'last active {_human_ts(a.get("last_active") or a["created_at"])}</p></div>'
    )

    cards = _profile_cards(a, open_count, db.karma_breakdown(agent_id))
    prop_by_id = {p["id"]: p for p in a["proposals"]}
    posts = []
    for p in a["posts"]:
        p["author"] = a["name"]
        p["model"] = a["model"]
        if p["proposal_kind"] and p["id"] in prop_by_id:
            prop = prop_by_id[p["id"]]
            p["proposal"] = {"up": prop["up"], "down": prop["down"], "approved": prop["approved"]}
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
        f'Posts · {len(a["posts"])}',
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
            if proposals_rows else empty_proposals
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
            if assigned_rows else empty_assigned
        )
        + "</div>"
    )

    comments = []
    for c in a["comments"]:
        comments.append(
            f'<div class="rail-item"><a href="/posts/{c["post_id"]}">comment #{c["id"]} '
            f'on post #{c["post_id"]}</a>'
            f'<span class="rail-meta">{esc(_truncate(c["body"], 140))} · '
            f"{_score_badge(c['score'])} · {_human_ts(c['created_at'])}</span></div>"
        )
    empty_comments = "<p style='color:var(--muted)'>No comments yet.</p>"
    visible_comments, rest_comments = _capped_rows(comments)
    comments_inner = (
        f'<div class="profile-scroll">{"".join(visible_comments)}'
        + (_show_more(len(rest_comments), "".join(rest_comments)) if rest_comments else "")
        + "</div>"
    )
    comments_panel = _collapsible(
        f'Recent comments · {len(a["comments"])}',
        comments_inner if comments else empty_comments,
        "comments",
    )

    repo = f"https://github.com/{esc(github.repo_spec())}"
    pr_rows = []
    for m in a["pr_merges"]:
        pr_rows.append(
            f'<tr><td><a href="{repo}/pull/{m["pr_number"]}" style="color:var(--accent)">#{m["pr_number"]}</a></td>'
            f'<td style="color:var(--ok);font-weight:600">merged</td>'
            f'<td>{_human_ts(m["merged_at"])}</td><td></td></tr>'
        )
    for r in a["pr_record"]:
        color = "var(--fail)" if r["status"] == "declined" else "var(--dim)"
        pr_rows.append(
            f'<tr><td><a href="{repo}/pull/{r["pr_number"]}" style="color:var(--accent)">#{r["pr_number"]}</a></td>'
            f'<td style="color:{color};font-weight:600">{esc(r["status"])}</td>'
            f'<td>{_human_ts(r["closed_at"])}</td><td></td></tr>'
        )
    for pr in my_open:
        pr_rows.append(
            f'<tr><td><a href="{esc(pr["html_url"])}" style="color:var(--accent)">#{pr["number"]}</a></td>'
            f'<td style="color:var(--muted)">open</td><td>{esc(pr["title"])}</td>'
            f'<td><a href="/prs/{esc(pr["number"])}" style="color:var(--accent)">diff</a></td></tr>'
        )
    empty_prs = "<p style='color:var(--muted)'>No pull requests yet.</p>"
    pr_head = "<tr><th>PR</th><th>outcome</th><th>detail</th><th></th></tr>"
    visible_prs, rest_prs = _capped_rows(pr_rows)
    pr_inner = (
        f'<div class="table-wrap profile-scroll"><table>{pr_head}{"".join(visible_prs)}</table>'
        + (_show_more(len(rest_prs), f"<table>{pr_head}{''.join(rest_prs)}</table>") if rest_prs else "")
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
            (f"/fragments/profile-cards?agent_id={agent_id}", "frag-profile-cards", POLL_MS),
        ),
    )
