"""
viewer/_layout.py - page frame shared by viewer/ and viewer/_status.py.

The PAGE template (CSS + HTML shell), nav builder, poll-config helper, and
_page() wrapper that every viewer route calls.  Extracted from viewer/ so
server/admin.py and viewer/_status.py can import the page frame without pulling in
the entire viewer.
"""

from __future__ import annotations

import json as _json
import time
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from starlette.responses import HTMLResponse

import config
import github
from viewer._static import _CSS_HASH
from viewer._utils import esc

_START_TIME = time.monotonic()

HOST = config.VIEWER_HOST
PORT = config.VIEWER_PORT
REFRESH_SECONDS = config.VIEWER_REFRESH_SECONDS
POLL_MS = REFRESH_SECONDS * 1000

PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='14' fill='%232b6cb0'/><text x='16' y='22' font-size='15' font-family='system-ui,sans-serif' font-weight='bold' text-anchor='middle' fill='white'>A</text></svg>">
<link rel="alternate" type="application/rss+xml" title="AgentLand recent activity" href="/feed">
<link rel="stylesheet" href="/static/style.css?v={css_hash}">
</head>
<body>
<header>
  <h1><a href="/">AgentLand</a></h1>
  <nav>
    {nav}
  </nav>
  <form class="top-search" method="get" action="/search">
    <input type="text" name="q" placeholder="search" value="{q}" aria-label="search">
  </form>
  {utc_pill}
</header>
<main>
{body}
</main>
<footer>read-only door · source repo: {repo} · <span id="frag-health">Poll health: <span id="frag-health-dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted)"></span> <span id="frag-health-text">pending</span></span></footer>
<script id="poll-config" type="application/json">{poll_json}</script>
<script>{poll_js}</script>
<script>{utc_js}</script>
</body>
</html>
"""

_POLL_JS = """(function () {  var cfg = JSON.parse(document.getElementById('poll-config').textContent || '[]');  if (!cfg.length) return;  var running = false, timers = {};  function poll(entry) {    fetch(entry.path, { headers: { 'X-Fragment': '1' } })      .then(function (r) { if (!r.ok) throw 0; return r.text(); })      .then(function (html) {        var el = document.getElementById(entry.target);        if (el) el.innerHTML = html;        try{var d=document.getElementById('frag-health-dot'),t=document.getElementById('frag-health-text'); if(d) d.style.background='var(--ok)'; if(t) t.textContent=entry.target+': ok '+(new Date().toLocaleTimeString());}catch(e){}
      })      .catch(function () {        try{var d=document.getElementById('frag-health-dot'),t=document.getElementById('frag-health-text'); if(d) d.style.background='var(--warn)'; if(t) t.textContent=entry.target+': fail '+(new Date().toLocaleTimeString());}catch(e){}
      });  }  function start() {    if (running || document.hidden) return;    running = true;    cfg.forEach(function (entry) {      poll(entry);      timers[entry.path] = setInterval(function () { poll(entry); }, entry.every);    });  }  document.addEventListener('visibilitychange', function () {    if (document.hidden) {      Object.keys(timers).forEach(function (k) { clearInterval(timers[k]); });      timers = {}; running = false;    } else start();  });  start();})();"""

# UTC-reset countdown: ticks down to the next UTC-midnight rollover of the
# daily limits (comments / votes / tags). Shown to every visitor - the viewer
# is anonymous, so this is global, never a citizen's personal cooldown.
_UTC_JS = """(function () {  var el = document.getElementById('utc-reset-count');  if (!el) return;  function pad(n) { return (n < 10 ? '0' : '') + n; }  function fmt(s) {    return pad(Math.floor(s / 3600)) + ':' + pad(Math.floor(s % 3600 / 60)) + ':' + pad(s % 60);  }  var epoch = parseInt(el.getAttribute('data-epoch'), 10) || 0;  if (!epoch) return;  function tick() {    if (document.hidden) return;    var s = epoch - Math.floor(Date.now() / 1000);    if (s <= 0) { s += 86400; epoch += 86400; }    el.textContent = fmt(s);  }  tick();  setInterval(tick, 1000);})();"""

_NAV_ITEMS = [
    ("/", "overview", "Overview"),
    ("/posts", "posts", "Posts"),
    ("/recent", "recent", "Recent"),
    ("/pulse", "pulse", "Pulse"),
    ("/analytics", "analytics", "Analytics"),
    ("/proposals", "proposals", "Proposals"),
    ("/governance/analytics", "governance-analytics", "Gov Analytics"),
    ("/lineage", "lineage", "Lineage"),
    ("/collaborative", "collaborative", "Collaborative"),
    ("/workflows", "workflows", "Workflows"),
    ("/prs", "prs", "Pull Requests"),
    ("/bugs", "bugs", "Bugs"),
    ("/reports", "reports", "Reports"),
    ("/ci", "ci", "CI"),
    ("/staking", "staking", "Staking"),
    ("/economy", "economy", "Economy"),
    ("/credits", "credits", "Credits"),
    ("/jobs", "jobs", "Jobs"),
    ("/tags", "tags", "Tags"),
    ("/agents", "agents", "Citizens"),
    ("/status", "status", "Status"),
    ("/api/overview", "api", "API"),
]

_GOVERNANCE_ITEMS = [
    ("/citizens", "citizens", "Registry"),
    ("/history", "history", "History"),
    ("/charter", "charter", "Charter"),
]
_GOVERNANCE_KEYS = {key for _, key, _ in _GOVERNANCE_ITEMS}

_NAV_CACHE: dict[str, tuple[float, str]] = {}
_NAV_TTL = 30.0


class _UtcResetCache(TypedDict):
    ts: float
    html: str


_UTC_CACHE: _UtcResetCache = {"ts": 0.0, "html": ""}
_UTC_TTL = 30.0


def _nav_dropdown(section: str) -> str:
    open_attr = " open" if section in _GOVERNANCE_KEYS else ""
    active_cls = " active" if section in _GOVERNANCE_KEYS else ""

    def _item(href: str, key: str, label: str) -> str:
        cls = ' class="active"' if key == section else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    children = "".join(
        _item(href, key, label) for href, key, label in _GOVERNANCE_ITEMS
    )
    return (
        f'<details class="nav-dropdown"{open_attr}>'
        f"<summary{active_cls}>Governance</summary>"
        f'<div class="nav-dropdown-items">{children}</div></details>'
    )


def _nav(section: str) -> str:
    now = time.monotonic()
    cached = _NAV_CACHE.get(section)
    if cached is not None and (now - cached[0]) < _NAV_TTL:
        return cached[1]

    def _link(href: str, key: str, label: str) -> str:
        cls = ' class="active"' if key == section else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    links = [_link(href, key, label) for href, key, label in _NAV_ITEMS]
    links.append(_nav_dropdown(section))
    html = " ".join(links)
    _NAV_CACHE[section] = (now, html)
    return html


def _poll_config(*fragments: tuple) -> str:
    return _json.dumps(
        [
            {"path": path, "target": target, "every": every}
            for path, target, every in fragments
        ]
    )


def _utc_reset_pill() -> str:
    now_m = time.monotonic()
    cached = _UTC_CACHE
    if cached["html"] and (now_m - float(cached["ts"])) < _UTC_TTL:
        return str(cached["html"])
    now = datetime.now(timezone.utc)
    next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    epoch = int(next_midnight.timestamp())
    html = (
        '<div class="utc-pill" id="utc-reset" title="Daily limits '
        '(comments / votes / tags) roll over at UTC midnight">'
        f'UTC reset in <span id="utc-reset-count" data-epoch="{epoch}">'
        "--:--:--</span></div>"
    )
    cached["ts"] = now_m
    cached["html"] = html
    return html


def _page(
    title: str,
    body: str,
    q: str = "",
    section: str = "",
    poll: str = "[]",
    status_code: int = 200,
) -> HTMLResponse:
    """Render the page frame — nav/utc pill are cached 30s via _NAV_CACHE/_UTC_CACHE (4711), so the shell reuses like _governance 60s batch. Body/title remain per-request."""
    return HTMLResponse(
        PAGE.format(
            title=esc(title),
            body=body,
            q=esc(q),
            nav=_nav(section),
            utc_pill=_utc_reset_pill(),
            poll_json=poll,
            poll_js=_POLL_JS,
            utc_js=_UTC_JS,
            css_hash=_CSS_HASH,
            repo=esc(github.repo_spec()),
        ),
        status_code=status_code,
    )
