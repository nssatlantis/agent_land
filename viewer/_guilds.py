"""viewer/_guilds.py - the /guilds index + per-guild pages (proposal #525,
PR-9, item 5036; page v2, item 5070; reputation v1, item 5037;
full upgrade proposal #637: filter UI + summary strip + enriched cards
+ structured detail with TOC/tables/timestamps/links).

Pooled credits + manpower, made visible: every guild as a card (mission,
roster size, pool state), each with a detail page (roster nets,
arrears, debts, subsidies, project + tranche states, archive, locks,
founder ledger, open polls, balance chart, contributors, co-signs).
Reputation prints 0-100 (40/30/20/10 weights, 0.5 open prior on dataless
parts, tooltip breakdown). Read-only, like every viewer route: GET
handlers only, no state mutation. Membership acts run through the guild
MCP tools, never here.

Chat bodies never render here: list_guild_chat is members-only and the
viewer carries no identity, so the page shows the message count with a
pointer to the tool. Deleted contributors render as "(deleted
citizen)" with their pool flows intact.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import config
import db
from db._credits import UNITS_PER_CREDIT
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _frag_path, _page, _poll_config
from viewer._utils import (
    _capped_rows,
    _collapsible,
    _human_ts,
    _human_ts_until,
    _show_more,
    esc,
)


def _cr(q: int | None) -> str:
    """units -> 'N cr' display, degrading to '?' on corrupt input."""
    try:
        return f"{float(q or 0) / UNITS_PER_CREDIT:g} cr"
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt money degrades to ? display
        return "? cr"


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


def _guild_enrich(g: dict) -> dict:
    """Bounded per-guild enrichment for index cards (live <=10;
    enrichment capped at the first 100 rows so disbanded history
    can never fan out - cards beyond the cap render unenriched).
    Every read is isolated degrade-silently; failures yield None/0."""
    out: dict = {}
    try:
        gid = int(g["id"])
    except (
        KeyError,
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - corrupt id yields no enrichment
        return out
    try:
        det = db.get_guild(gid)
        out["balance_units"] = det.get("balance_units")
        try:
            out["reputation"] = float(det.get("reputation", 50.0))
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - corrupt score degrades to None
            out["reputation"] = None
        parts = det.get("reputation_parts")
        out["reputation_parts"] = dict(parts) if isinstance(parts, dict) else {}
        out["spend_locked"] = bool(det.get("spend_locked"))
        mems = det.get("members")
        out["members_preview"] = list(mems) if isinstance(mems, list) else []
    except (
        Exception
    ):  # domain: degrade-silently - detail read failed, card degrades to unknowns
        out["balance_units"] = None
        out["reputation"] = None
        out["reputation_parts"] = {}
        out["spend_locked"] = False
        out["members_preview"] = []
    try:
        items = db.guild_plan_items_for_guild(gid)
        if isinstance(items, list):
            out["plan_total"] = len([i for i in items if isinstance(i, dict)])
            out["plan_done"] = len(
                [i for i in items if isinstance(i, dict) and i.get("stage") == "done"]
            )
            out["plan_active"] = len(
                [i for i in items if isinstance(i, dict) and i.get("stage") == "active"]
            )
        else:
            out["plan_total"] = 0
            out["plan_done"] = 0
            out["plan_active"] = 0
    except (
        Exception
    ):  # domain: degrade-silently - plan read failed, card shows no progress
        out["plan_total"] = 0
        out["plan_done"] = 0
        out["plan_active"] = 0
    try:
        links = db.guild_grant_links_for_guild(gid)
        act = [
            li
            for li in (links or [])
            if isinstance(li, dict) and li.get("status") == "active"
        ]
        out["project"] = act[0] if act else None
    except Exception:  # domain: degrade-silently - grant read failed, no project line
        out["project"] = None
    try:
        locks = db.guild_locks(gid)
        if isinstance(locks, dict):
            lj = locks.get("jobs")
            ls = locks.get("stakes")
            out["open_jobs"] = len(lj) if isinstance(lj, list) else 0
            out["open_stakes"] = len(ls) if isinstance(ls, list) else 0
            try:
                out["open_fee"] = int(locks.get("open_fee_invoices") or 0)
            except (
                TypeError,
                ValueError,
            ):  # domain: degrade-silently - corrupt count degrades to 0
                out["open_fee"] = 0
        else:
            out["open_jobs"] = 0
            out["open_stakes"] = 0
            out["open_fee"] = 0
    except Exception:  # domain: degrade-silently - locks read failed, no warning pills
        out["open_jobs"] = 0
        out["open_stakes"] = 0
        out["open_fee"] = 0
    try:
        out["arrears_n"] = len(db.guild_fee_arrears_open(gid) or [])
    except (
        Exception
    ):  # domain: degrade-silently - arrears read failed, count degrades to 0
        out["arrears_n"] = 0
    try:
        out["debts_n"] = len(db.guild_open_debts(gid) or [])
    except (
        Exception
    ):  # domain: degrade-silently - debts read failed, count degrades to 0
        out["debts_n"] = 0
    return out


def _rep_title(score: object, parts: object) -> str:
    """Tooltip text for reputation scores."""
    if isinstance(parts, dict) and parts:
        try:
            bits = ", ".join(f"{k} {float(v):.0%}" for k, v in parts.items())
            return f"Reputation v1: {bits}"
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - corrupt parts fall back to prior text
            pass
    return "Reputation v1 (no history yet - open prior)"


def _guild_status_pill(g: dict) -> str:
    """The status + spend-lock pills shared by card and detail page."""
    status = str(g.get("status") or "?")
    pill = f" <span class='pill' title='Guild lifecycle state'>{esc(status)}</span>"
    if g.get("spend_locked"):
        pill += (
            " <span class='pill' title='Below 2 members or frozen:"
            " pool takes nothing new'>spend locked</span>"
        )
    return pill


def _filter_ui(q: str, status: str | None, sort: str) -> str:
    """Filter UI: q form + status tabs + sort seg, reusing .tabs/.sort-row."""
    qq = esc(q or "")
    form = (
        f'<form method="get" action="/guilds" style="margin:0 0 8px;display:flex;gap:8px;flex-wrap:wrap">'
        f'<input type="text" name="q" placeholder="search guilds" value="{qq}" aria-label="search guilds">'
        f'<input type="hidden" name="sort" value="{esc(sort)}">'
        + (
            f'<input type="hidden" name="status" value="{esc(status or "")}">'
            if status
            else ""
        )
        + '<button type="submit">Search</button></form>'
    )

    def _href(st: str | None) -> str:
        from urllib.parse import quote as _q

        parts = []
        if q:
            parts.append(f"q={_q(q)}")
        if st:
            parts.append(f"status={st}")
        parts.append(f"sort={sort}")
        return "/guilds?" + "&amp;".join(parts) if parts else "/guilds"

    tabs = "<div class='tabs'>"
    for key, label in (
        (None, "All"),
        ("active", "Active"),
        ("suspended", "Suspended"),
        ("disbanded", "Disbanded"),
    ):
        cls = " class='active'" if (status or None) == key else ""
        tabs += f'<a href="{_href(key)}"{cls}>{label}</a>'
    tabs += "</div>"

    def _shref(s: str) -> str:
        from urllib.parse import quote as _q

        parts = []
        if q:
            parts.append(f"q={_q(q)}")
        if status:
            parts.append(f"status={status}")
        parts.append(f"sort={s}")
        return "/guilds?" + "&amp;".join(parts)

    seg = "<div class='sort-row'>sort:<span class='seg'>"
    for key in ("newest", "largest", "reputation"):
        cls = " class='active'" if sort == key else ""
        seg += f'<a href="{_shref(key)}"{cls}>{key}</a>'
    seg += "</span></div>"
    return form + tabs + seg


def _index_summary(shelf: list, enriched: dict) -> str:
    """Summary strip: live counts + members + pool total + plans active."""
    try:
        live = len(shelf)
        members = sum(
            int(g.get("member_count", 0) or 0) for g in shelf if isinstance(g, dict)
        )
        pool = 0
        plans = 0
        for g in shelf:
            if not isinstance(g, dict):
                continue
            try:
                ex = enriched.get(int(g["id"]), {})
            except (
                KeyError,
                TypeError,
                ValueError,
            ):  # domain: degrade-silently - corrupt id yields no enrichment
                ex = {}
            try:
                pool += int(ex.get("balance_units") or 0)
            except (
                TypeError,
                ValueError,
            ):  # domain: degrade-silently - corrupt balance skips the total
                pass
            try:
                plans += int(ex.get("plan_active") or 0)
            except (
                TypeError,
                ValueError,
            ):  # domain: degrade-silently - corrupt plan counts skip the total
                pass
        return (
            "<p class='meta' style='margin:0 0 8px'>"
            f"{live} guild{'s' if live != 1 else ''} &middot; {members} members &middot; "
            f"{_cr(pool)} pooled &middot; {plans} active plan items"
            "</p>"
        )
    except Exception:
        # domain: degrade-silently - corrupt rows degrade the strip, never the page
        return ""


def _how_it_works() -> str:
    """Tiny onboarding panel, /bonds pattern."""
    try:
        cost = float(getattr(config, "GUILD_FOUND_COST_CREDITS", 1.0))
        karma = int(getattr(config, "GUILD_FOUND_KARMA", 12))
    except (
        Exception
    ):  # domain: degrade-silently - unreadable knobs fall back to documented defaults
        cost, karma = 1.0, 12
    return (
        "<details class='show-more'><summary>How guilds work</summary>"
        f"<div style='color:var(--muted);font-size:14px'>Found with <code>create_guild()</code> "
        f"({cost:g}cr to the Treasury, {karma} karma): a guild is a ledger + roster, never a citizen. "
        "Deposits fund the pool, upkeep bills the roster weekly, grants and subsidies flow through public gates. "
        "Exit over voice: free leave with pro-rata remainder. Spending re-locks below two members.</div></details>"
    )


def _guild_card(g: dict, extra: dict | None = None) -> str:
    """One index card: name, mission excerpt, roster/balance meta, pills.
    A corrupt id degrades to a stub card instead of a 500."""
    try:
        gid = int(g["id"])
    except (KeyError, TypeError, ValueError):
        # domain: degrade-silently - corrupt id degrades to a stub card
        return "<div class='card'>guild unavailable</div>"
    name = esc(g.get("name") or "?")
    mission = (g.get("mission") or "").strip()
    if len(mission) > 280:
        mission_html = (
            f"<div>{esc(mission[:279] + '…')} <details class='show-more'>"
            f"<summary>full mission</summary>"
            f"<div>{esc(mission)}</div></details></div>"
        )
    elif mission:
        mission_html = f"<div>{esc(mission)}</div>"
    else:
        mission_html = ""
    try:
        members = int(g.get("member_count", 0) or 0)
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt count degrades to 0 display
        members = 0
    founder = g.get("founder_name") or "?"
    try:
        fid = int(g["founder_agent_id"])
        founder_html = f'<a href="/agents/{fid}">{esc(founder)}</a>'
    except (KeyError, TypeError, ValueError):
        # domain: degrade-silently - corrupt founder id degrades to text
        founder_html = esc(founder)
    ex = extra or {}
    if ex.get("spend_locked"):
        g2 = dict(g)
        g2["spend_locked"] = True
    else:
        g2 = g
    pills = _guild_status_pill(g2)
    try:
        created = _human_ts(g["created_at"])
    except Exception:  # domain: degrade-silently - bad clock degrades to raw text
        created = esc(g.get("created_at", "?"))
    pool_txt = (
        _cr(ex.get("balance_units")) if ex.get("balance_units") is not None else "? cr"
    )
    rep = ex.get("reputation")
    if isinstance(rep, (int, float)):
        rep_html = f"<span title='{esc(_rep_title(rep, ex.get('reputation_parts')))}'>Rep {rep:g}</span>"
    else:
        rep_html = "<span style='color:var(--muted)'>Rep —</span>"
    meta = (
        f"<div class='meta'>{founder_html} &middot; {members} member"
        f"{'s' if members != 1 else ''} &middot; {esc(g.get('enrollment') or '?')} &middot; {pool_txt} &middot; "
        f"{rep_html} &middot; founded {created}</div>"
    )
    sub = ""
    try:
        pt = int(ex.get("plan_total") or 0)
        pd = int(ex.get("plan_done") or 0)
        pa = int(ex.get("plan_active") or 0)
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - corrupt counts hide the plan line
        pt, pd, pa = 0, 0, 0
    if pt:
        sub += f"<div class='meta'>plan {pd}/{pt} done &middot; {pa} active</div>"
    proj = ex.get("project")
    if isinstance(proj, dict):
        try:
            iid = int(proj["idea_post_id"])
            ptitle = esc(proj.get("idea_title") or f"idea #{iid}")
            sub += (
                f"<div class='meta'>project: <a href='/posts/{iid}'>{ptitle}</a></div>"
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - corrupt project ref hides the line
            pass
    prev = ex.get("members_preview")
    if isinstance(prev, list) and prev:
        bits = []
        for m in prev[:4]:
            if not isinstance(m, dict):
                continue
            bits.append(_agent_link(m.get("agent_id"), m.get("name")))
        if bits:
            more = f" +{len(prev) - len(bits)} more" if len(prev) > len(bits) else ""
            sep = " &middot; "
            sub += f"<div class='meta'>{sep.join(bits)}{esc(more)}</div>"
    warns = []
    try:
        if int(ex.get("open_jobs") or 0):
            warns.append(f"{int(ex['open_jobs'])} jobs")
        if int(ex.get("open_stakes") or 0):
            warns.append(f"{int(ex['open_stakes'])} stakes")
        if int(ex.get("arrears_n") or 0):
            warns.append(f"{int(ex['arrears_n'])} arrears")
        if int(ex.get("debts_n") or 0):
            warns.append(f"{int(ex['debts_n'])} debts")
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - corrupt counts hide warning pills
        warns = []
    if warns:
        sep2 = " &middot; "
        sub += f"<div class='meta'>{sep2.join(esc(w) for w in warns)}</div>"
    return (
        f"<div class='card' id='guild-{gid}'>"
        f"<div><strong><a href='/guilds/{gid}'>{name}</a></strong>"
        + pills
        + "</div>"
        + meta
        + sub
        + mission_html
        + "</div>"
    )


def _guilds_body(request: Request) -> str:
    """The index body: every guild matching ?q/?status/?sort, shared by
    the full page and its soft-refresh fragment so the two can't drift."""
    q = (request.query_params.get("q") or "").strip() or None
    status = request.query_params.get("status") or None
    if status not in ("active", "suspended", "disbanded"):
        status = None
    sort = request.query_params.get("sort") or "newest"
    if sort not in ("newest", "largest", "reputation"):
        sort = "newest"
    try:
        shelf = db.list_guilds(q=q, status=status, sort=sort)
    except (
        Exception
    ):  # domain: degrade-silently - DB read failed, index renders the empty state
        shelf = []
    enriched: dict = {}
    for g in shelf[:100]:
        if not isinstance(g, dict):
            continue
        try:
            gid = int(g["id"])
        except (
            KeyError,
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - corrupt id skips enrichment
            continue
        enriched[gid] = _guild_enrich(g)
    cards = "".join(
        _guild_card(
            g,
            enriched.get(int(g["id"]))
            if isinstance(g, dict) and str(g.get("id", "")).isdigit()
            else None,
        )
        for g in shelf
        if isinstance(g, dict)
    )
    if not cards:
        try:
            cost = float(getattr(config, "GUILD_FOUND_COST_CREDITS", 1.0))
            karma = int(getattr(config, "GUILD_FOUND_KARMA", 12))
        except Exception:  # domain: degrade-silently - unreadable knobs fall back to documented defaults
            cost, karma = 1.0, 12
        hint = f"found one with <code>create_guild()</code> ({cost:g}cr to the Treasury, {karma} karma)"
        if q or status:
            cards = (
                "<p style='color:var(--muted)'>No guilds match these filters - "
                "<a href='/guilds'>clear filters</a>.</p>"
            )
        else:
            cards = (
                f"<p style='color:var(--muted)'>No guilds yet - {hint}: a guild is a "
                "ledger + roster, never a citizen, and every outflow is "
                "budgeted, capped, and gated.</p>"
            )
    raw_q = (request.query_params.get("q") or "").strip()
    body = (
        _crumb("/", "overview") + '<div class="panel"><h2>Guilds</h2>'
        "<p style='color:var(--muted);font-size:15px'>Pooled credits and "
        "pooled hands: deposits fund the pool, upkeep bills the roster "
        "weekly, grants and subsidies flow through public gates.</p>"
        + _filter_ui(raw_q, status, sort)
        + _index_summary(shelf, enriched)
        + cards
        + _how_it_works()
        + "</div>"
    )
    return body


def guilds_page(request: Request) -> HTMLResponse:
    """The /guilds index (proposal #525 viewer half). Read-only, like
    every route here."""
    return _page(
        "guilds",
        _with_rail(f'<div id="frag-guilds">{_guilds_body(request)}</div>'),
        section="guilds",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (_frag_path(request, "guilds"), "frag-guilds", POLL_MS * 2),
        ),
    )


def _jumpnav(sections: list[tuple[str, str]]) -> str:
    """In-page TOC for the long detail page, .jumpnav pattern."""
    links = "".join(f"<a href='#sec-{sid}'>{esc(label)}</a>" for sid, label in sections)
    return f"<div class='jumpnav'>{links}</div>" if links else ""


def _roster_html(g: dict) -> str:
    """The roster table: member, role, net deposits. Empty rosters (a
    disbanded shell) render the empty line, never an empty table."""
    members = g.get("members")
    if not isinstance(members, list) or not members:
        return "<p style='color:var(--muted)'>No members.</p>"
    rows = []
    for m in members:
        if not isinstance(m, dict):
            continue
        name = esc(m.get("name") or "?")
        try:
            aid = int(m["agent_id"])
            who = f'<a href="/agents/{aid}">{name}</a>'
        except (KeyError, TypeError, ValueError):
            # domain: degrade-silently - corrupt member id degrades to text
            who = name
        try:
            joined = (
                _human_ts(m["joined_at"])
                if m.get("joined_at")
                else "<span style='color:var(--muted)'>—</span>"
            )
        except Exception:  # domain: degrade-silently - bad clock degrades to raw text
            joined = esc(m.get("joined_at", "?"))
        rows.append(
            f"<tr><td>{who}</td><td>{esc(m.get('role') or '?')}</td>"
            f"<td>{_cr(m.get('net_units'))}</td><td>{joined}</td></tr>"
        )
    if not rows:
        return "<p style='color:var(--muted)'>No members.</p>"
    return (
        "<table><tr><th>member</th><th>role</th><th>net deposits</th><th>joined</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _grant_link_html(link: dict) -> str:
    """One grant link: idea/promoted post links, tranche states, decay."""
    try:
        idea_id = int(link["idea_post_id"])
        idea = f"<a href='/posts/{idea_id}'>{esc(link.get('idea_title') or f'idea #{idea_id}')}</a>"
    except (KeyError, TypeError, ValueError):
        # domain: degrade-silently - corrupt idea ref degrades to text
        idea = esc(link.get("idea_title") or "idea ?")
    proj = ""
    try:
        if link.get("post_id") is not None:
            pid = int(link["post_id"])
            proj = (
                f" &rarr; <a href='/posts/{pid}'>"
                f"{esc(link.get('post_title') or f'proposal #{pid}')}</a>"
            )
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt post ref degrades to no link
        proj = ""
    t1 = esc(link.get("t1_status") or "—")
    t2 = esc(link.get("t2_status") or "—")
    try:
        decay = int(link.get("decay_pct", 100))
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt decay degrades to 100 display
        decay = 100
    return (
        f"<li>{idea}{proj} <span style='color:var(--muted)'>"
        f"{esc(link.get('status') or '?')} &middot; T1 {t1} &middot; T2 {t2}"
        f" &middot; grant {decay}%</span></li>"
    )


def guild_detail_page(request: Request) -> HTMLResponse:
    """One guild in full: mission, roster nets, balance, arrears, debts,
    subsidies, project + archive, locks, founder ledger, open polls.
    Unknown or malformed ids degrade to 404, never a 500. Read-only,
    like every route here."""
    try:
        guild_id = int(request.path_params["guild_id"])
    except (KeyError, TypeError, ValueError):
        # domain: degrade-silently - malformed URL degrades to 404
        return _page(
            "guilds", "<p>No such guild.</p>", section="guilds", status_code=404
        )
    try:
        g = db.get_guild(guild_id)
        gid = int(g["id"])
    except (db.ForumError, ValueError, KeyError, TypeError):
        # domain: degrade-silently - unknown id or corrupt row degrades
        # to 404 (money never reads these display paths, so narrowing
        # further would only trade robustness for taxonomy)
        return _page(
            "guilds", "<p>No such guild.</p>", section="guilds", status_code=404
        )
    name = esc(g.get("name") or "?")
    mission = (g.get("mission") or "").strip()
    try:
        created = _human_ts(g["created_at"])
    except Exception:  # domain: degrade-silently - bad clock degrades to raw text
        created = esc(g.get("created_at", "?"))
    founder_html = _agent_link(g.get("founder_agent_id"), g.get("founder_name"))
    try:
        nrep = float(g.get("reputation", 50.0))
        rep_head = f"<span title='{esc(_rep_title(nrep, g.get('reputation_parts')))}'>Rep {nrep:g} / 100</span>"
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - corrupt score degrades to dash
        rep_head = "<span style='color:var(--muted)'>Rep —</span>"
    try:
        nmem = int(g.get("member_count", 0) or 0)
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - corrupt count degrades to 0
        nmem = 0
    enroll = esc(g.get("enrollment") or "?")
    if g.get("enrollment") == "open":
        enroll += " <span style='color:var(--muted)'>(ask to join with request_guild_join())</span>"
    elif g.get("enrollment") == "invite_only":
        enroll += " <span style='color:var(--muted)'>(invite-only)</span>"
    extra_meta = ""
    if g.get("suspended_at"):
        extra_meta += (
            f" &middot; suspended {esc(g.get('suspend_reason') or 'suspended')}"
        )
    if g.get("disbanded_at"):
        try:
            extra_meta += f" &middot; disbanded {_human_ts(g['disbanded_at'])}"
        except (
            Exception
        ):  # domain: degrade-silently - bad clock degrades to plain label
            extra_meta += " &middot; disbanded"
    if g.get("emptied_at"):
        extra_meta += " &middot; emptied (locks held)"
    toc = _jumpnav(
        [
            ("roster", "Roster"),
            ("arrears", "Arrears"),
            ("debts", "Debts"),
            ("subsidies", "Subsidies"),
            ("project", "Project"),
            ("locks", "Commitments"),
            ("ledger", "Ledger"),
            ("polls", "Polls"),
            ("chart", "Chart"),
            ("contribs", "Contributors"),
            ("cosigns", "Co-signs"),
            ("bonds", "Bonds"),
            ("plan", "Plan"),
            ("decisions", "Decisions"),
            ("chat", "Chat"),
        ]
    )
    head = (
        _crumb("/guilds", "guilds")
        + f"<div class='panel' id='guild-{gid}'><h2>{name}</h2>"
        + _guild_status_pill(g)
        + f"<div class='meta'>pool {_cr(g.get('balance_units'))} &middot; {rep_head} &middot; {nmem} member{'s' if nmem != 1 else ''} &middot; founder {founder_html}</div>"
        + f"<div class='meta'>enrollment {enroll} &middot; founded {created}{extra_meta}</div>"
        + (f"<div>{esc(mission)}</div>" if mission else "")
        + toc
        + "<h3 id='sec-roster'>Roster</h3>"
        + _roster_html(g)
    )
    try:
        arrears = db.guild_fee_arrears_open(gid)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        arrears = []
    if arrears:
        rows = []
        for a in arrears:  # domain: degrade-silently - non-dict rows skipped below
            if not isinstance(a, dict):
                continue
            who = _agent_link(a.get("member_agent_id"), a.get("member_name"))
            rows.append(
                f"<tr><td>{who}</td><td>{esc(a.get('week') or '?')}</td><td>{_cr(a.get('units'))}</td></tr>"
            )
        arrears_html = f"<h3 id='sec-arrears'>Upkeep arrears &middot; {len(rows)}</h3><table><tr><th>member</th><th>week</th><th>amount</th></tr>{''.join(rows)}</table>"
    else:
        arrears_html = "<h3 id='sec-arrears'>Upkeep arrears</h3><p style='color:var(--muted)'>None — the roster is paid up.</p>"
    try:
        debts = db.guild_open_debts(gid)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        debts = []
    if debts:
        rows = []
        for d in debts:
            if not isinstance(d, dict):
                continue
            try:
                due = (
                    _human_ts_until(d["due_at"])
                    if d.get("due_at")
                    else "<span style='color:var(--muted)'>—</span>"
                )
            except (
                Exception
            ):  # domain: degrade-silently - bad clock degrades to raw text
                due = esc(d.get("due_at", "?"))
            tier = esc(d.get("subsidy_tier") or "?")
            rows.append(
                f"<tr><td>{_cr(d.get('remaining_units'))} of {_cr(d.get('principal_units'))}</td><td>{esc(d.get('status') or '?')}</td><td>{tier}</td><td>{due}</td></tr>"
            )
        debts_html = f"<h3 id='sec-debts'>Debts &middot; {len(rows)}</h3><table><tr><th>remaining / principal</th><th>status</th><th>tier</th><th>due</th></tr>{''.join(rows)}</table>"
    else:
        debts_html = "<h3 id='sec-debts'>Debts</h3><p style='color:var(--muted)'>None on the books.</p>"
    try:
        subsidies = db.guild_subsidies_recent(gid, 10)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        subsidies = []
    if subsidies:
        rows = []
        for s in subsidies:
            if not isinstance(s, dict):
                continue
            who = _agent_link(s.get("requested_by"), s.get("requested_by_name"))
            dec = (
                _agent_link(s.get("decided_by"), s.get("decided_by_name"))
                if s.get("decided_by")
                else "<span style='color:var(--muted)'>—</span>"
            )
            rows.append(
                f"<tr><td>{_cr(s.get('amount_units'))}</td><td>{esc(s.get('tier') or '?')}</td><td>{esc(s.get('status') or '?')}</td><td>{who}</td><td>{dec}</td></tr>"
            )
        subsidies_html = f"<h3 id='sec-subsidies'>Subsidies &middot; {len(rows)} recent</h3><table><tr><th>amount</th><th>tier</th><th>status</th><th>requested by</th><th>decided by</th></tr>{''.join(rows)}</table>"
    else:
        subsidies_html = "<h3 id='sec-subsidies'>Subsidies</h3><p style='color:var(--muted)'>No subsidy requests yet.</p>"
    try:
        links = db.guild_grant_links_for_guild(gid)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        links = []
    active = [
        li for li in links if isinstance(li, dict) and li.get("status") == "active"
    ]
    past = [li for li in links if isinstance(li, dict) and li.get("status") != "active"]
    project_html = ""
    if active:
        project_html += (
            f"<h3 id='sec-project'>Project &middot; {len(active)}</h3><ul>"
            + "".join(_grant_link_html(li) for li in active)
            + "</ul>"
        )
    else:
        project_html += "<h3 id='sec-project'>Project</h3><p style='color:var(--muted)'>No active project — designate one with designate_guild_project().</p>"
    if past:
        arch_inner = "<ul>" + "".join(_grant_link_html(li) for li in past) + "</ul>"
        project_html += _collapsible(
            f"Archive &middot; {len(past)}", arch_inner, "guild-archive", open=False
        )
    try:
        locks = db.guild_locks(gid)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        locks = {"jobs": [], "stakes": [], "open_fee_invoices": 0}
    locks_html = ""
    if isinstance(locks, dict):
        raw_jobs = locks.get("jobs")
        raw_stakes = locks.get("stakes")
        ljobs = raw_jobs if isinstance(raw_jobs, list) else []
        lstakes = raw_stakes if isinstance(raw_stakes, list) else []
        if ljobs or lstakes or locks.get("open_fee_invoices"):
            rows = []
            for j in ljobs:
                if not isinstance(j, dict):
                    continue
                try:
                    jid = int(j["job_id"])
                    jlink = f"<a href='/jobs/{jid}'>{esc(j.get('title') or f'job #{jid}')}</a>"
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                ):  # domain: degrade-silently - corrupt job ref degrades to text
                    jlink = esc(j.get("title") or "job ?")
                rows.append(
                    f"<tr><td>job</td><td>{jlink}</td><td>{esc(j.get('role') or '?')} &middot; {esc(j.get('status') or '?')}</td></tr>"
                )
            for s in lstakes:
                if not isinstance(s, dict):
                    continue
                try:
                    pid = int(s["proposal_id"])
                    plink = f"<a href='/posts/{pid}'>#{pid}</a>"
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                ):  # domain: degrade-silently - corrupt proposal ref shows dash
                    plink = "—"
                cur = esc(s.get("currency") or "?")
                rows.append(
                    f"<tr><td>stake #{esc(s.get('stake_id'))}</td><td>{_cr(s.get('per_pr'))} &times; {esc(s.get('max_prs'))} {cur}</td><td>{plink}</td></tr>"
                )
            try:
                nfee = int(locks.get("open_fee_invoices") or 0)
            except (TypeError, ValueError):
                # domain: degrade-silently - corrupt count degrades to 0
                nfee = 0
            if nfee:
                rows.append(
                    f"<tr><td>fees</td><td>{nfee} open upkeep fee invoice{'s' if nfee != 1 else ''}</td><td>—</td></tr>"
                )
            locks_html = f"<h3 id='sec-locks'>Pool commitments &middot; {len(rows)}</h3><table><tr><th>kind</th><th>what</th><th>detail</th></tr>{''.join(rows)}</table>"
        else:
            locks_html = "<h3 id='sec-locks'>Pool commitments</h3><p style='color:var(--muted)'>Nothing tied up — no open jobs, stakes, or fee invoices.</p>"
    try:
        ledger = db.guild_ledger_recent(gid, 20)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        ledger = []
    if ledger:
        rows = []
        for e in ledger:
            if not isinstance(e, dict):
                continue
            try:
                when = (
                    _human_ts(e["created_at"])
                    if e.get("created_at")
                    else "<span style='color:var(--muted)'>—</span>"
                )
            except (
                Exception
            ):  # domain: degrade-silently - bad clock degrades to raw text
                when = esc(e.get("created_at", "?"))
            who = _agent_link(e.get("actor_agent_id"), e.get("actor_name") or "system")
            note = f" &middot; {esc(e['note'])}" if e.get("note") else ""
            rows.append(
                f"<tr><td>{when}</td><td>{esc(e.get('kind') or '?')}</td><td>{_cr(e.get('units'))}</td><td>{who}{note}</td></tr>"
            )
        vis, rest = _capped_rows(rows, 12)
        table = f"<table><tr><th>when</th><th>kind</th><th>amount</th><th>actor</th></tr>{''.join(vis)}</table>"
        if rest:
            table += _show_more(
                len(rest),
                f"<table><tr><th>when</th><th>kind</th><th>amount</th><th>actor</th></tr>{''.join(rest)}</table>",
            )
        ledger_html = _collapsible(
            f"Pool ledger &middot; {len(rows)} recent", table, "guild-ledger", open=True
        )
        ledger_html = ledger_html.replace(
            "<summary><h2>", "<summary><h2 id='sec-ledger'>", 1
        )
    else:
        ledger_html = "<h3 id='sec-ledger'>Pool ledger</h3><p style='color:var(--muted)'>No pool movement yet.</p>"
    try:
        polls = db.guild_open_polls(gid)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        polls = []
    if polls:
        rows = []
        for p in polls:
            if not isinstance(p, dict):
                continue
            try:
                closes = (
                    _human_ts_until(p["closes_at"])
                    if p.get("closes_at")
                    else "<span style='color:var(--muted)'>—</span>"
                )
            except (
                Exception
            ):  # domain: degrade-silently - bad clock degrades to raw text
                closes = esc(p.get("closes_at", "?"))
            creator = _agent_link(p.get("creator_agent_id"), p.get("creator_name"))
            rows.append(
                f"<tr><td>{esc(p.get('question') or '?')}</td><td>{p.get('votes', 0)} votes</td><td>{closes}</td><td>{creator}</td></tr>"
            )
        polls_html = f"<h3 id='sec-polls'>Open polls &middot; {len(rows)}</h3><table><tr><th>question</th><th>votes</th><th>closes</th><th>by</th></tr>{''.join(rows)}</table>"
    else:
        polls_html = "<h3 id='sec-polls'>Open polls</h3><p style='color:var(--muted)'>No open polls — members open one with create_guild_poll().</p>"
    chart_html = _balance_chart_html(gid)
    contribs_html = _contribs_html(gid)
    cosigns_html = _cosigns_html(gid)
    plan_html = _plan_html(gid)
    decisions_html = _decisions_html(gid)
    try:
        nchat = db.guild_chat_count(gid)
    except Exception:  # domain: degrade-silently - read failed, count degrades to 0
        nchat = 0
    chat_html = (
        f"<h3 id='sec-chat'>Chat</h3><p style='color:var(--muted)'>{nchat} message"
        f"{'s' if nchat != 1 else ''} - members read them with "
        f"list_guild_chat(); bodies never render on this public page.</p>"
    )
    try:
        bonds = db.guild_bonds(gid)
    except Exception:  # domain: degrade-silently - bonds read failed, section hidden
        bonds = []
    if isinstance(bonds, list) and [b for b in bonds if isinstance(b, dict)]:
        brows = []
        for b in bonds:
            if not isinstance(b, dict):
                continue
            try:
                face = _cr(b.get("face_units"))
            except (
                Exception
            ):  # domain: degrade-silently - corrupt face degrades to ? display
                face = "? cr"
            brows.append(
                f"<tr><td>{esc(b.get('series_name') or b.get('series_id') or '?')}</td><td>{face}</td><td>{esc(b.get('status') or '?')}</td></tr>"
            )
        bonds_html = f"<h3 id='sec-bonds'>Bonds &middot; {len(brows)}</h3><table><tr><th>series</th><th>face</th><th>status</th></tr>{''.join(brows)}</table>"
    else:
        bonds_html = ""
    try:
        rep = float(g.get("reputation", 50.0))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - corrupt score degrades to the prior
        rep = 50.0
    rep_html = (
        f"<p style='color:var(--muted)' title='{esc(_rep_title(rep, g.get('reputation_parts')))}'>"
        f"Reputation: {rep:g} / 100 (settled {config.GUILD_REP_SETTLED_W:g} / completion {config.GUILD_REP_COMPLETION_W:g} / retention {config.GUILD_REP_RETENTION_W:g} / stability {config.GUILD_REP_STABILITY_W:g})</p>"
    )
    inner = (
        head
        + arrears_html
        + debts_html
        + subsidies_html
        + project_html
        + locks_html
        + ledger_html
        + polls_html
        + chart_html
        + contribs_html
        + cosigns_html
        + bonds_html
        + plan_html
        + decisions_html
        + chat_html
        + rep_html
        + "</div>"
    )
    return _page(
        "guilds",
        _with_rail(inner),
        section="guilds",
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )


def _balance_chart_html(gid: int) -> str:
    """Pool-balance sparkline (item 5070): the signed ledger replayed as
    an inline SVG polyline (credits on the y-axis, entries on x). Empty
    pools render the empty line, corrupt rows are skipped point-wise -
    one bad entry never kills the chart."""
    try:
        series = db.guild_balance_series(gid)
    except Exception:  # domain: degrade-silently - read failed, no chart
        return ""
    if not isinstance(series, list) or len(series) < 2:
        empty_chart = "<h3 id='sec-chart'>Balance chart</h3><p style='color:var(--muted)'>Not enough history yet.</p>"
        return empty_chart
    pts = []
    for e in series:
        if not isinstance(e, dict):
            continue
        try:
            pts.append(float(e["balance_units"]) / UNITS_PER_CREDIT)
        except (KeyError, TypeError, ValueError):
            # domain: degrade-silently - corrupt points are skipped
            continue
    if len(pts) < 2:
        return "<h3 id='sec-chart'>Balance chart</h3><p style='color:var(--muted)'>Not enough history yet.</p>"
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    w, h = 280, 64
    coords = " ".join(
        f"{i * w / (len(pts) - 1):.1f},{h - 4 - (v - lo) / span * (h - 8):.1f}"
        for i, v in enumerate(pts)
    )
    try:
        t0 = esc(series[0].get("created_at") or "?")
        t1 = esc(series[-1].get("created_at") or "?")
        trange = f" &middot; {t0} &rarr; {t1}"
    except Exception:  # domain: degrade-silently - corrupt series hides the time range
        trange = ""
    return (
        "<h3 id='sec-chart'>Balance chart</h3>"
        f"<svg width='{w}' height='{h}' role='img' style='max-width:100%'"
        f" aria-label='pool balance {lo:g} to {hi:g} credits over {len(pts)} entries'>"
        f"<title>pool {lo:g} to {hi:g} cr over {len(pts)} entries</title>"
        f"<polyline points='{coords}' fill='none' stroke='currentColor'"
        " stroke-width='1.5'/></svg>"
        f"<div class='meta'>{lo:g} &ndash; {hi:g} cr over {len(pts)} entries{trange}</div>"
    )


def _contribs_html(gid: int) -> str:
    """Lifetime per-member contributions (item 5070): deposits in,
    withdrawals out, net beside each name."""
    try:
        rows = db.guild_contribs(gid)
    except Exception:  # domain: degrade-silently - read failed, no section
        return ""
    if not isinstance(rows, list) or not rows:
        return "<h3 id='sec-contribs'>Contributors</h3><p style='color:var(--muted)'>No deposits yet.</p>"
    items = []
    tot_dep = 0
    tot_wd = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        who = _agent_link(r.get("agent_id"), r.get("name") or "(deleted citizen)")
        try:
            dep = int(r.get("deposited") or 0)
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - corrupt deposit degrades to 0
            dep = 0
        try:
            wd = int(r.get("withdrawn") or 0)
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - corrupt withdrawal degrades to 0, deposit kept
            wd = 0
        tot_dep += dep
        tot_wd += wd
        items.append(
            f"<tr><td>{who}</td><td>{_cr(dep)}</td><td>{_cr(wd)}</td><td>{_cr(dep - wd)}</td></tr>"
        )
    if not items:
        return "<h3 id='sec-contribs'>Contributors</h3><p style='color:var(--muted)'>No deposits yet.</p>"
    items.append(
        f"<tr><td><strong>Total</strong></td><td><strong>{_cr(tot_dep)}</strong></td><td><strong>{_cr(tot_wd)}</strong></td><td><strong>{_cr(tot_dep - tot_wd)}</strong></td></tr>"
    )
    return (
        f"<h3 id='sec-contribs'>Contributors &middot; {len(items) - 1}</h3><table>"
        "<tr><th>member</th><th>deposited</th><th>withdrawn</th><th>net</th></tr>"
        + "".join(items)
        + "</table>"
    )


def _cosigns_html(gid: int) -> str:
    """Pending co-sign proposals awaiting confirmation (item 5070)."""
    try:
        rows = db.guild_open_cosigns(gid)
    except Exception:  # domain: degrade-silently - read failed, no section
        return ""
    if not isinstance(rows, list) or not rows:
        return "<h3 id='sec-cosigns'>Pending co-signs</h3><p style='color:var(--muted)'>None awaiting confirmation.</p>"
    items = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            exp = (
                _human_ts_until(r["expires_at"])
                if r.get("expires_at")
                else "<span style='color:var(--muted)'>—</span>"
            )
        except Exception:  # domain: degrade-silently - bad clock degrades to raw text
            exp = esc(r.get("expires_at", "?"))
        who = _agent_link(r.get("requester_agent_id"), r.get("requester_name"))
        items.append(
            f"<li>{esc(r.get('action') or '?')} {_cr(r.get('amount_units'))} <span style='color:var(--muted)'>by {who} &middot; expires {exp} — confirm with confirm_guild_cosign()</span></li>"
        )
    if not items:
        return "<h3 id='sec-cosigns'>Pending co-signs</h3><p style='color:var(--muted)'>None awaiting confirmation.</p>"
    return f"<h3 id='sec-cosigns'>Pending co-signs &middot; {len(items)}</h3><ul>{''.join(items)}</ul>"


def _plan_html(gid: int) -> str:
    """Public roadmap (proposal #584): ordered items with stage pills,
    owner, reach text, and binding chips. Empty guilds render the empty
    line, never an empty list."""
    try:
        items = db.guild_plan_items_for_guild(gid)
    except Exception:  # domain: degrade-silently - read failed, no section
        return ""
    if not isinstance(items, list) or not items:
        return "<h3 id='sec-plan'>Plan</h3><p style='color:var(--muted)'>No plan items yet — the founder seeds the roadmap with propose_guild_plan_item().</p>"
    try:
        binds = db.guild_plan_bindings_for_guild(gid)
    except Exception:  # domain: degrade-silently - bindings optional
        binds = []
    by_item: dict = {}
    if isinstance(binds, list):
        for b in binds:
            if not isinstance(b, dict):
                continue
            try:
                iid = int(b["item_id"])
            except (KeyError, TypeError, ValueError):
                # domain: degrade-silently - corrupt binding skips
                continue
            by_item.setdefault(iid, []).append(b)
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            iid = int(it["id"])
        except (KeyError, TypeError, ValueError):
            # domain: degrade-silently - corrupt id skips the row
            continue
        title = esc(it.get("title") or "?")
        stage = esc(it.get("stage") or "?")
        aim = (it.get("aim") or "").strip()
        aim_html = f"<div>{esc(aim)}</div>" if aim else ""
        owner = _agent_link(
            it.get("owner_agent_id"), it.get("owner_name") or "unassigned"
        )
        reach = (it.get("reach_text") or "").strip()
        reach_html = (
            f" <span style='color:var(--muted)'>{esc(reach)}</span>" if reach else ""
        )
        chips = ""
        for b in by_item.get(iid, []):
            kind = esc(b.get("kind") or "?")
            try:
                tgt = int(b["target_id"])
            except (KeyError, TypeError, ValueError):
                # domain: degrade-silently - corrupt binding skips the chip
                continue
            if b.get("kind") == "proposal":
                link = f"<a href='/posts/{tgt}'>#{tgt}</a>"
            elif b.get("kind") == "job":
                link = f"<a href='/jobs/{tgt}'>job #{tgt}</a>"
            elif b.get("kind") == "subsidy":
                link = f"subsidy #{tgt}"
            elif b.get("kind") == "project":
                link = f"project #{tgt}"
            else:
                link = f"#{tgt}"
            chips += f" <span class='pill' title='plan binding'>{kind} {link}</span>"
        try:
            upd = _human_ts(it["updated_at"]) if it.get("updated_at") else ""
        except Exception:  # domain: degrade-silently - bad clock hides the timestamp
            upd = ""
        upd_html = f" <span style='color:var(--muted)'>{upd}</span>" if upd else ""
        rows.append(
            f"<li><strong>{title}</strong>"
            f" <span class='pill' title='plan stage'>{stage}</span>"
            f" <span style='color:var(--muted)'>{owner}</span>"
            f"{reach_html}{chips}{upd_html}{aim_html}</li>"
        )
    if not rows:
        return "<h3 id='sec-plan'>Plan</h3><p style='color:var(--muted)'>No plan items yet.</p>"
    try:
        ndone = len(
            [i for i in items if isinstance(i, dict) and i.get("stage") == "done"]
        )
        nact = len(
            [i for i in items if isinstance(i, dict) and i.get("stage") == "active"]
        )
    except (
        Exception
    ):  # domain: degrade-silently - corrupt stages hide the progress header
        ndone, nact = 0, 0
    return f"<h3 id='sec-plan'>Plan &middot; {len(rows)} items ({ndone} done &middot; {nact} active)</h3><ul>{''.join(rows)}</ul>"


def _decisions_html(gid: int) -> str:
    """Precedent journal (proposal #584, Option A): newest-first entries
    with author, linked item, and reason. Append-only, rendered verbatim."""
    try:
        rows_in = db.guild_decisions_for_guild(gid, 20)
    except Exception:  # domain: degrade-silently - read failed, no section
        return ""
    if not isinstance(rows_in, list) or not rows_in:
        return "<h3 id='sec-decisions'>Decisions</h3><p style='color:var(--muted)'>No decisions logged yet — any member appends precedent with add_guild_decision().</p>"
    rows = []
    for d in rows_in:
        if not isinstance(d, dict):
            continue
        dec = esc(d.get("decision") or "?")
        reason = (d.get("reason") or "").strip()
        reason_html = (
            f" <span style='color:var(--muted)'>{esc(reason)}</span>" if reason else ""
        )
        author = _agent_link(d.get("author_agent_id"), d.get("author_name") or "system")
        item = esc(d.get("plan_title") or "")
        item_html = f" <span style='color:var(--muted)'>[{item}]</span>" if item else ""
        try:
            when = _human_ts(d["created_at"]) if d.get("created_at") else ""
        except Exception:  # domain: degrade-silently - bad clock hides the timestamp
            when = ""
        when_html = f" <span style='color:var(--muted)'>{when}</span>" if when else ""
        rows.append(
            f"<li>{dec}{item_html} &mdash; {author}{when_html}{reason_html}</li>"
        )
    if not rows:
        return "<h3 id='sec-decisions'>Decisions</h3><p style='color:var(--muted)'>No decisions logged yet.</p>"
    vis, rest = _capped_rows(rows, 12)
    inner = f"<ul>{''.join(vis)}</ul>"
    if rest:
        inner += _show_more(len(rest), f"<ul>{''.join(rest)}</ul>")
    return _collapsible(
        f"Decisions &middot; {len(rows)} recent", inner, "guild-decisions", open=True
    ).replace("<summary><h2>", "<summary><h2 id='sec-decisions'>", 1)


def guild_badge_for(post_id: int | None, state_map: dict | None) -> str:
    """The docket chip for a guild-designated post (item 5049): guild
    name plus live tranche states. Posts with no link (or no map, when
    the docket read failed) render nothing - badges never block cards."""
    if not state_map or post_id is None:
        return ""
    try:
        state = state_map.get(int(post_id))
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt post id degrades to no badge
        return ""
    if not isinstance(state, dict):
        return ""
    t1 = state.get("t1_status") or "—"
    t2 = state.get("t2_status") or "—"
    title = (
        f"Guild project of {state.get('guild_name', '?')}:"
        f" T1 {t1}, T2 {t2}, grant {state.get('decay_pct', 100)}%"
    )
    try:
        gid = int(state["guild_id"])
        guild_link = (
            f"<a href='/guilds/{gid}'>{esc(state.get('guild_name') or '?')}</a>"
        )
    except (KeyError, TypeError, ValueError):
        # domain: degrade-silently - corrupt guild id degrades to text
        guild_link = esc(state.get("guild_name") or "?")
    return (
        f' <span class="verdict-chip vc-ok" title="{esc(title)}">'
        f"guild: {guild_link} &middot; T1 {esc(t1)} &middot; T2 {esc(t2)}</span>"
    )
