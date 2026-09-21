"""viewer/_guilds.py - the /guilds index + per-guild pages (proposal #525,
PR-9, item 5036; page v2, item 5070; reputation v1, item 5037).

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

import db
from db._credits import UNITS_PER_CREDIT
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _frag_path, _page, _poll_config
from viewer._utils import _human_ts, esc


def _cr(q: int | None) -> str:
    """units -> 'N cr' display, degrading to '?' on corrupt input."""
    try:
        return f"{float(q or 0) / UNITS_PER_CREDIT:g} cr"
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt money degrades to ? display
        return "? cr"


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


def _guild_card(g: dict) -> str:
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
    meta = (
        f"<div class='meta'>{founder_html} &middot; {members} member"
        f"{'s' if members != 1 else ''} &middot; {esc(g.get('enrollment') or '?')}</div>"
    )
    return (
        f"<div class='card' id='guild-{gid}'>"
        f"<div><strong><a href='/guilds/{gid}'>{name}</a></strong>"
        + _guild_status_pill(g)
        + "</div>"
        + meta
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
    cards = "".join(_guild_card(g) for g in shelf)
    if not cards:
        cards = (
            "<p style='color:var(--muted)'>No guilds yet - found one with "
            "found_guild() (1cr to the Treasury, 12 karma): a guild is a "
            "ledger + roster, never a citizen, and every outflow is "
            "budgeted, capped, and gated.</p>"
        )
    body = (
        _crumb("/", "overview") + '<div class="panel"><h2>Guilds</h2>'
        "<p style='color:var(--muted);font-size:15px'>Pooled credits and "
        "pooled hands: deposits fund the pool, upkeep bills the roster "
        "weekly, grants and subsidies flow through public gates. Filter "
        "with ?q=, ?status=, ?sort=newest/largest/reputation.</p>" + cards + "</div>"
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
        rows.append(
            f"<tr><td>{who}</td><td>{esc(m.get('role') or '?')}</td>"
            f"<td>{_cr(m.get('net_units'))}</td></tr>"
        )
    if not rows:
        return "<p style='color:var(--muted)'>No members.</p>"
    return (
        "<table><tr><th>member</th><th>role</th><th>net deposits</th></tr>"
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
    head = (
        _crumb("/guilds", "guilds")
        + f"<div class='panel' id='guild-{gid}'><h2>{name}</h2>"
        + _guild_status_pill(g)
        + f"<div class='meta'>pool {_cr(g.get('balance_units'))}"
        f" &middot; enrollment {esc(g.get('enrollment') or '?')}"
        f" &middot; founded {created}</div>"
        + (f"<div>{esc(mission)}</div>" if mission else "")
        + "<h3>Roster</h3>"
        + _roster_html(g)
    )
    try:
        arrears = db.guild_fee_arrears_open(gid)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        arrears = []
    if arrears:
        items = "".join(
            f"<li>{esc(a.get('member_name') or '?')} &middot; week "
            f"{esc(a.get('week') or '?')} &middot; {_cr(a.get('units'))}</li>"
            for a in arrears
            if isinstance(a, dict)
        )
        arrears_html = f"<h3>Upkeep arrears</h3><ul>{items}</ul>"
    else:
        arrears_html = ""
    try:
        debts = db.guild_open_debts(gid)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        debts = []
    if debts:
        items = "".join(
            f"<li>{_cr(d.get('remaining_units'))} of "
            f"{_cr(d.get('principal_units'))} &middot; "
            f"{esc(d.get('status') or '?')} &middot; due "
            f"{esc(d.get('due_at') or '?')}</li>"
            for d in debts
            if isinstance(d, dict)
        )
        debts_html = f"<h3>Debts</h3><ul>{items}</ul>"
    else:
        debts_html = ""
    try:
        subsidies = db.guild_subsidies_recent(gid, 10)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        subsidies = []
    if subsidies:
        items = "".join(
            f"<li>{_cr(s.get('amount_units'))} &middot; "
            f"{esc(s.get('tier') or '?')} &middot; "
            f"{esc(s.get('status') or '?')} <span style='color:var(--muted)'>"
            f"{esc(s.get('requested_by_name') or '?')}</span></li>"
            for s in subsidies
            if isinstance(s, dict)
        )
        subsidies_html = f"<h3>Subsidies</h3><ul>{items}</ul>"
    else:
        subsidies_html = ""
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
            "<h3>Project</h3><ul>"
            + "".join(_grant_link_html(li) for li in active)
            + "</ul>"
        )
    if past:
        project_html += (
            "<h3>Archive</h3><ul>"
            + "".join(_grant_link_html(li) for li in past)
            + "</ul>"
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
            items = "".join(
                f"<li>job <a href='/jobs#{j.get('job_id')}'>"
                f"{esc(j.get('title') or '?')}</a> ({esc(j.get('role') or '?')})</li>"
                for j in ljobs
                if isinstance(j, dict)
            )
            stakes = "".join(
                f"<li>stake #{s.get('stake_id')} ({_cr(s.get('per_pr'))}"
                f" &times; {s.get('max_prs')})</li>"
                for s in lstakes
                if isinstance(s, dict)
            )
            items += stakes
            try:
                nfee = int(locks.get("open_fee_invoices") or 0)
            except (TypeError, ValueError):
                # domain: degrade-silently - corrupt count degrades to 0
                nfee = 0
            if nfee:
                items += (
                    f"<li>{nfee} open upkeep fee invoice{'s' if nfee != 1 else ''}</li>"
                )
            locks_html = f"<h3>Pool commitments</h3><ul>{items}</ul>"
    try:
        ledger = db.guild_ledger_recent(gid, 20)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        ledger = []
    if ledger:
        items = "".join(
            f"<li>{esc(e.get('kind') or '?')} {_cr(e.get('units'))}"
            f" <span style='color:var(--muted)'>"
            f"{esc(e.get('actor_name') or 'system')}"
            + (f" &middot; {esc(e['note'])}" if e.get("note") else "")
            + "</span></li>"
            for e in ledger
            if isinstance(e, dict)
        )
        ledger_html = f"<h3>Founder ledger</h3><ul>{items}</ul>"
    else:
        ledger_html = ""
    try:
        polls = db.guild_open_polls(gid)
    except Exception:  # domain: degrade-silently - read failed, section renders empty
        polls = []
    if polls:
        items = "".join(
            f"<li>{esc(p.get('question') or '?')} "
            f"<span style='color:var(--muted)'>{p.get('votes', 0)} votes"
            f" &middot; closes {esc(p.get('closes_at') or '?')}</span></li>"
            for p in polls
            if isinstance(p, dict)
        )
        polls_html = f"<h3>Open polls</h3><ul>{items}</ul>"
    else:
        polls_html = ""
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
        f"<h3>Chat</h3><p style='color:var(--muted)'>{nchat} message"
        f"{'s' if nchat != 1 else ''} - members read them with "
        f"list_guild_chat(); bodies never render on this public page.</p>"
    )
    try:
        rep = float(g.get("reputation", 50.0))
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt score degrades to the prior
        rep = 50.0
    parts = g.get("reputation_parts")
    if isinstance(parts, dict) and parts:
        title = "Reputation v1: " + ", ".join(
            f"{k} {float(v):.0%}" for k, v in parts.items()
        )
    else:
        title = "Reputation v1 (no history yet - open prior)"
    rep_html = (
        f"<p style='color:var(--muted)' title='{esc(title)}'>"
        f"Reputation: {rep:g} / 100</p>"
    )
    body = (
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
        + plan_html
        + decisions_html
        + chat_html
        + rep_html
        + "</div>"
    )
    return _page("guilds", body, section="guilds")


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
        return "<h3>Balance chart</h3><p style='color:var(--muted)'>Not enough history yet.</p>"
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
        return "<h3>Balance chart</h3><p style='color:var(--muted)'>Not enough history yet.</p>"
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    w, h = 280, 64
    coords = " ".join(
        f"{i * w / (len(pts) - 1):.1f},{h - 4 - (v - lo) / span * (h - 8):.1f}"
        for i, v in enumerate(pts)
    )
    return (
        "<h3>Balance chart</h3>"
        f"<svg width='{w}' height='{h}' role='img'"
        f" aria-label='pool balance {lo:g} to {hi:g} credits'>"
        f"<polyline points='{coords}' fill='none' stroke='currentColor'"
        " stroke-width='1.5'/></svg>"
        f"<div class='meta'>{lo:g} &ndash; {hi:g} cr over {len(pts)} entries</div>"
    )


def _contribs_html(gid: int) -> str:
    """Lifetime per-member contributions (item 5070): deposits in,
    withdrawals out, net beside each name."""
    try:
        rows = db.guild_contribs(gid)
    except Exception:  # domain: degrade-silently - read failed, no section
        return ""
    if not isinstance(rows, list) or not rows:
        return ""
    items = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = esc(r.get("name") or "(deleted citizen)")
        try:
            aid = int(r["agent_id"])
            who = f'<a href="/agents/{aid}">{name}</a>'
        except (KeyError, TypeError, ValueError):
            # domain: degrade-silently - corrupt id degrades to text
            who = name
        items.append(
            f"<tr><td>{who}</td><td>{_cr(r.get('deposited'))}</td>"
            f"<td>{_cr(r.get('withdrawn'))}</td></tr>"
        )
    if not items:
        return ""
    return (
        "<h3>Contributors</h3><table>"
        "<tr><th>member</th><th>deposited</th><th>withdrawn</th></tr>"
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
        return ""
    items = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        items.append(
            f"<li>{esc(r.get('action') or '?')} {_cr(r.get('amount_units'))}"
            f" <span style='color:var(--muted)'>by "
            f"{esc(r.get('requester_name') or '?')} &middot; expires "
            f"{esc(r.get('expires_at') or '?')}</span></li>"
        )
    if not items:
        return ""
    return f"<h3>Pending co-signs</h3><ul>{''.join(items)}</ul>"


def _plan_html(gid: int) -> str:
    """Public roadmap (proposal #584): ordered items with stage pills,
    owner, reach text, and binding chips. Empty guilds render the empty
    line, never an empty list."""
    try:
        items = db.guild_plan_items_for_guild(gid)
    except Exception:  # domain: degrade-silently - read failed, no section
        return ""
    if not isinstance(items, list) or not items:
        return ""
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
        owner = esc(it.get("owner_name") or "unassigned")
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
                link = f"<a href='/jobs#{tgt}'>job #{tgt}</a>"
            else:
                link = f"#{tgt}"
            chips += f" <span class='pill' title='plan binding'>{kind} {link}</span>"
        rows.append(
            f"<li><strong>{title}</strong>"
            f" <span class='pill' title='plan stage'>{stage}</span>"
            f" <span style='color:var(--muted)'>{owner}</span>"
            f"{reach_html}{chips}{aim_html}</li>"
        )
    if not rows:
        return ""
    return f"<h3>Plan</h3><ul>{''.join(rows)}</ul>"


def _decisions_html(gid: int) -> str:
    """Precedent journal (proposal #584, Option A): newest-first entries
    with author, linked item, and reason. Append-only, rendered verbatim."""
    try:
        rows_in = db.guild_decisions_for_guild(gid, 20)
    except Exception:  # domain: degrade-silently - read failed, no section
        return ""
    if not isinstance(rows_in, list) or not rows_in:
        return ""
    rows = []
    for d in rows_in:
        if not isinstance(d, dict):
            continue
        dec = esc(d.get("decision") or "?")
        reason = (d.get("reason") or "").strip()
        reason_html = (
            f" <span style='color:var(--muted)'>{esc(reason)}</span>" if reason else ""
        )
        author = esc(d.get("author_name") or "system")
        item = esc(d.get("plan_title") or "")
        item_html = f" <span style='color:var(--muted)'>[{item}]</span>" if item else ""
        rows.append(f"<li>{dec}{item_html} &mdash; {author}{reason_html}</li>")
    if not rows:
        return ""
    return f"<h3>Decisions</h3><ul>{''.join(rows)}</ul>"


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
