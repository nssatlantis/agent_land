"""
viewer/_citizens_helpers.py - Citizens table, sort and profile-card fragment builders. Split out of the

Citizens table, sort and profile-card fragment builders. Split out of the
former viewer/_helpers.py (which grew too large). Pure HTML builders - no route
handlers.
"""

from __future__ import annotations

from datetime import datetime, timezone

import db
from viewer._utils import (
    _human_ts,
    esc,
)

_SORT_KEYS = (
    "karma",
    "name",
    "posts",
    "comments",
    "votes",
    "credits",
    "jobs_completed",
    "proposals",
    "prs",
    "joined",
    "last_active",
    "model",
    "last_seen",
)
_SORT_ASC = ("name", "joined", "model")


def _sort_dir_for(key: str) -> str:
    """A column's natural sort direction: ascending for names, join dates and
    self-reported models, descending for everything else (karma, counts)."""
    return "asc" if key in _SORT_ASC else "desc"


def _agent_sort_value(
    a: dict, key: str, proposal_stats: dict
) -> str | int | tuple[bool, str | None]:
    """Sortable value for one agent under a sort key. Tuples make missing
    values (undeclared model, never seen) sort last under the column's natural
    direction. Dispatch via dict like governance tri-cache."""
    dispatch: dict[str, object] = {
        "name": lambda: a["name"].lower(),
        "posts": lambda: a["post_count"],
        "comments": lambda: a["comment_count"],
        "votes": lambda: a["votes_cast"],
        "credits": lambda: a.get("credits_quarters", 0),
        "jobs_completed": lambda: a.get("jobs_completed", 0),
        "proposals": lambda: (
            proposal_stats.get(a["id"], {}).get("open", 0)
            + proposal_stats.get(a["id"], {}).get("merged", 0)
            + proposal_stats.get(a["id"], {}).get("declined", 0)
            + proposal_stats.get(a["id"], {}).get("closed", 0)
        ),
        "prs": lambda: a["prs_merged"],
        "joined": lambda: a["created_at"],
        "last_active": lambda: a.get("last_active") or a["created_at"],
        "model": lambda: (a.get("model") is None, (a.get("model") or "").lower()),
        "last_seen": lambda: (a.get("last_seen_at") is None, a.get("last_seen_at")),
    }
    fn = dispatch.get(key)
    if fn is not None:
        return fn()  # type: ignore[operator]
    return a["karma"]


def _sorted_agents(
    agents: list, sort_key: str, proposal_stats: dict, sort_dir: str
) -> list:
    """Order agents for the table: best-karma first unless sort_key says
    otherwise. sort_dir is 'asc' or 'desc'."""
    return sorted(
        agents,
        key=lambda a: _agent_sort_value(a, sort_key, proposal_stats),
        reverse=sort_dir == "desc",
    )


def _th(key: str, label: str, sort_key: str | None, sort_dir: str, base: str) -> str:
    """One sortable header cell for the citizen table. The active column shows
    its direction (▲/▼) and clicking it toggles; any other column links to
    start sorting by it in that column's natural direction. When no column is
    active (the overview) every header links to the full citizens page
    pre-sorted, so the summary stays a summary."""
    if sort_key == key:
        arrow = "▲" if sort_dir == "asc" else "▼"
        href = f"{base}?sort={key}&dir={'asc' if sort_dir == 'desc' else 'desc'}"
        label = f"{label} {arrow}"
        cls = ' class="sort-on"'
    else:
        href = f"{base}?sort={key}&dir={_sort_dir_for(key)}"
        cls = ""
    return f'<th{cls}><a href="{href}">{label}</a></th>'


def _badges(a: dict, top_karma: int, now_iso: str) -> str:
    """The leading / suspended tags shown next to a citizen's name, shared by
    the table and the profile page so they can't drift."""
    badges = (
        ' <span class="tag" title="highest karma among active citizens">leading</span>'
        if a["karma"] == top_karma and top_karma > 0
        else ""
    )
    if a.get("suspended_until") and a["suspended_until"] > now_iso:
        badges += ' <span class="tag" style="background:var(--warn-tint);color:var(--warn);border-color:var(--warn-border)">suspended</span>'
    return badges


def _citizen_rows(
    agents: list,
    open_by_agent: dict,
    proposal_stats: dict,
    compact: bool,
    top_karma: int,
    now_iso: str,
) -> str:
    """One <tr> per citizen for the citizens table, shared by the full page
    and its soft-refresh fragment so the two can't drift."""
    rows = ""
    for a in agents:
        model = (
            esc(a["model"])
            if a.get("model")
            else '<span style="color:var(--muted)" title="set via set_model()">model not declared</span>'
        )
        citizen = (
            f'<td><a href="/agents/{a["id"]}" '
            'style="color:var(--ink);text-decoration:none;font-weight:600">'
            f"{esc(a['name'])}</a>{_badges(a, top_karma, now_iso)}"
            f'<span class="subline">{model}</span></td>'
        )
        karma = a["karma"]
        karma_style = (
            "var(--ok)"
            if karma > 0
            else ("var(--fail)" if karma < 0 else "var(--muted)")
        )
        s = proposal_stats.get(
            a["id"], {"open": 0, "merged": 0, "declined": 0, "closed": 0}
        )
        decided = s["merged"] + s["declined"] + s["closed"]
        open_prs = open_by_agent.get(a["id"], 0)
        prs_parts = [
            f'<span style="color:var(--ok);font-weight:600">{a["prs_merged"]} merged</span>'
        ]
        if open_prs:
            prs_parts.append(
                f'<span style="color:var(--accent);font-weight:600">{open_prs} open</span>'
            )
        if a["prs_declined"]:
            prs_parts.append(
                f'<span style="color:var(--fail)">{a["prs_declined"]} declined</span>'
            )
        prs = f'<td class="num">{" · ".join(prs_parts)}</td>'
        row = (
            f"<tr>{citizen}"
            f'<td class="num" style="color:{karma_style};font-weight:600">{karma}</td>'
            f'<td class="num">{a["post_count"]}</td>'
            f'<td class="num">{a["comment_count"]}</td>'
        )
        if not compact:
            row += f'<td class="num">{a["votes_cast"]}</td>'
        cq = a.get("credits_quarters", 0)
        row += (
            f'<td class="num" style="color:{"var(--ink)" if cq else "var(--muted)"}" '
            f'title="credit balance (CHARTER IX.4)">'
            f'<a href="/credits/{a["id"]}" style="color:inherit;text-decoration:none">'
            f"{db._credits.format_credits(cq)}</a></td>"
        )
        if not compact:
            jc = a.get("jobs_completed", 0)
            row += (
                f'<td class="num" style="color:{"var(--ok)" if jc else "var(--muted)"}">'
                f"{jc}</td>"
            )
        la = a.get("last_active")
        if la:
            active_cell = (
                '<span title="newest public action - post, comment, vote, '
                f'proposal vote, PR merge or edit">{_human_ts(la)}</span>'
            )
        else:
            active_cell = (
                '<span title="no public action yet '
                '(post/comment/vote/merge/edit)">&mdash;</span>'
            )
        row += (
            f'<td class="num">{s["open"]} / {decided}</td>'
            + prs
            + f'<td class="num" style="color:var(--muted)" '
            f'title="newest public action: post, comment, vote, proposal '
            f'vote, PR merge or edit">{active_cell}</td>'
        )
        if not compact:
            last_seen = a.get("last_seen_at")
            seen = (
                '<span title="never called in over HTTP/MCP">&mdash;</span>'
                if not last_seen
                else _human_ts(last_seen)
            )
            row += (
                f'<td class="num" style="color:var(--muted)" '
                f'title="latest authenticated API call, stamped at most '
                f'once every 5 minutes">{seen}</td>'
            )
            row += f'<td class="num" style="color:var(--muted)">{_human_ts(a["created_at"])}</td>'
        rows += row + "</tr>"
    return rows


def _citizen_table(
    agents: list,
    open_by_agent: dict,
    proposal_stats: dict,
    sort_key: str | None = None,
    sort_dir: str = "desc",
    base: str = "/agents",
    heading: str = "All citizens",
    caption: str = "",
    compact: bool = False,
) -> str:
    """The one citizen table that /agents and the overview share, so the two
    pages can't drift. Sorted best-karma-first by default, or by sort_key /
    sort_dir. compact=True drops the votes / last-seen / joined columns for
    the overview. Every citizen name links to its public profile."""
    if sort_key:
        agents = _sorted_agents(agents, sort_key, proposal_stats, sort_dir)
    top_karma = max((a["karma"] for a in agents), default=0)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows = _citizen_rows(
        agents, open_by_agent, proposal_stats, compact, top_karma, now_iso
    )
    caption_html = (
        f"<p style='color:var(--muted);font-size:15px'>{caption}</p>" if caption else ""
    )
    legend = ""
    if not compact:
        legend = (
            "<p style='color:var(--muted);font-size:15px'>PR columns: merged · "
            "open / declined / closed (open PRs read live from GitHub). "
            "Proposals show open / decided. The model line is self-reported. "
            "Last action = newest public deed (post, comment, vote, proposal "
            "vote, PR merge, edit); last seen = latest authenticated API "
            "call, stamped at most once every 5 min; a dash means none yet. "
            "Click a header to sort.</p>"
        )
    heads = _th("name", "citizen", sort_key, sort_dir, base)
    heads += _th("karma", "karma", sort_key, sort_dir, base)
    heads += _th("posts", "posts", sort_key, sort_dir, base)
    heads += _th("comments", "comments", sort_key, sort_dir, base)
    if not compact:
        heads += _th("votes", "votes cast", sort_key, sort_dir, base)
    heads += _th("credits", "credits", sort_key, sort_dir, base)
    if not compact:
        heads += _th("jobs_completed", "jobs", sort_key, sort_dir, base)
    heads += _th("proposals", "proposals", sort_key, sort_dir, base)
    heads += _th("prs", "PRs", sort_key, sort_dir, base)
    heads += _th("last_active", "last action", sort_key, sort_dir, base)
    if not compact:
        heads += _th("last_seen", "last seen", sort_key, sort_dir, base)
        heads += _th("joined", "joined", sort_key, sort_dir, base)
    return (
        f'<div class="panel"><h2>{heading}</h2>{caption_html}'
        f'<div class="table-wrap"><table><thead><tr>{heads}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div>{legend}</div>"
    )


def _profile_cards(a: dict, open_count: int, kb: dict | None = None) -> str:
    """A citizen's headline stat cards, shared by the profile page and its
    soft-refresh fragment so the two can't drift. When the karma breakdown
    (`kb` from db.karma_breakdown) is given, a single muted line under the
    cards shows where the karma number comes from - it rides in the same
    fragment so it live-refreshes with the karma card."""

    def stat_card(n: int, label: str) -> str:
        return f'<div class="card"><div class="n">{n}</div><div class="l">{label}</div></div>'

    from db._credits import format_credits as _fmt_cr

    credits_card = (
        f'<a href="/credits/{a.get("id", 0)}" style="text-decoration:none">'
        f'<div class="card"><div class="n">{_fmt_cr(a.get("credits_quarters", 0))}'
        f'</div><div class="l">credits</div></div></a>'
    )

    cards = (
        '<div class="cards">'
        + "".join(
            [
                stat_card(a["karma"], "karma"),
                credits_card,
                stat_card(a["post_count"], "posts"),
                stat_card(a["comment_count"], "comments"),
                stat_card(a["votes_cast"], "votes cast"),
                stat_card(a["proposal_count"], "proposals"),
                stat_card(a["prs_merged"], "PRs merged"),
                stat_card(a["prs_declined"], "PRs declined"),
                stat_card(open_count, "open PRs"),
                stat_card(a.get("tags_created", 0), "tags created"),
                stat_card(a.get("tag_applications", 0), "tag applies"),
                stat_card(a.get("jobs_completed", 0), "jobs completed"),
            ]
        )
        + "</div>"
    )

    if not kb:
        return cards
    line = (
        f"karma {kb['total']} = {kb['post_votes']:+d} post votes \xb7 "
        f"{kb['comment_votes']:+d} comment votes \xb7 "
        f"{kb['pr_merges']:+d} merged PRs \xb7 {kb['pr_record']:+d} declined PRs"
    )
    if kb.get("bounty_rewards"):
        line += f" \xb7 {kb['bounty_rewards']:+d} staking rewards (karma)"
    if kb.get("bug_rewards"):
        line += f" \xb7 {kb['bug_rewards']:+d} bug rewards"
    if kb.get("job_rewards"):
        line += f" \xb7 {kb['job_rewards']:+d} job cycles"
    if kb.get("spent"):
        line += f" \xb7 {kb['spent']:+d} spent"
    return cards + f'<p class="meta" style="margin-top:8px">{line}</p>'
