"""
viewer/_layout.py - page frame shared by viewer/ and viewer/_status.py.

The PAGE template (CSS + HTML shell), nav builder, poll-config helper, and
_page() wrapper that every viewer route calls.  Extracted from viewer/ so
server/admin.py and viewer/_status.py can import the page frame without pulling in
the entire viewer.
"""

from __future__ import annotations

import time

from starlette.responses import HTMLResponse

import config
import github
from viewer._utils import esc
from viewer._static import _CSS_HASH

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
    <form method="get" action="/search">
      <input type="text" name="q" placeholder="search" value="{q}" aria-label="search">
    </form>
  </nav>
</header>
<main>
{body}
</main>
<footer>read-only door · source repo: {repo}</footer>
<script id="poll-config" type="application/json">{poll_json}</script>
<script>{poll_js}</script>
</body>
</html>
"""

_POLL_JS = """(function () {  var cfg = JSON.parse(document.getElementById('poll-config').textContent || '[]');  if (!cfg.length) return;  var running = false, timers = {};  function poll(entry) {    fetch(entry.path, { headers: { 'X-Fragment': '1' } })      .then(function (r) { if (!r.ok) throw 0; return r.text(); })      .then(function (html) {        var el = document.getElementById(entry.target);        if (el) el.innerHTML = html;      })      .catch(function () {});  }  function start() {    if (running || document.hidden) return;    running = true;    cfg.forEach(function (entry) {      poll(entry);      timers[entry.path] = setInterval(function () { poll(entry); }, entry.every);    });  }  document.addEventListener('visibilitychange', function () {    if (document.hidden) {      Object.keys(timers).forEach(function (k) { clearInterval(timers[k]); });      timers = {}; running = false;    } else start();  });  start();})();"""

_NAV_ITEMS = [
    ("/", "overview", "Overview"),
    ("/posts", "posts", "Posts"),
    ("/recent", "recent", "Recent"),
    ("/pulse", "pulse", "Pulse"),
    ("/proposals", "proposals", "Proposals"),
    ("/prs", "prs", "Pull Requests"),
    ("/bugs", "bugs", "Bugs"),
    ("/reports", "reports", "Reports"),
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

def _nav_dropdown(section: str) -> str:
    open_attr = ' open' if section in _GOVERNANCE_KEYS else ""
    active_cls = ' active' if section in _GOVERNANCE_KEYS else ""
    def _item(href: str, key: str, label: str) -> str:
        cls = ' class="active"' if key == section else ""
        return f'<a href="{href}"{cls}>{label}</a>'
    children = "".join(_item(href, key, label) for href, key, label in _GOVERNANCE_ITEMS)
    return (
        f'<details class="nav-dropdown"{open_attr}>'
        f'<summary{active_cls}>Governance</summary>'
        f'<div class="nav-dropdown-items">{children}</div></details>'
    )

def _nav(section: str) -> str:
    def _link(href: str, key: str, label: str) -> str:
        cls = ' class="active"' if key == section else ""
        return f'<a href="{href}"{cls}>{label}</a>'
    links = [_link(href, key, label) for href, key, label in _NAV_ITEMS]
    links.append(_nav_dropdown(section))
    return " ".join(links)

def _poll_config(*fragments: tuple) -> str:
    import json as _json
    return _json.dumps(
        [{"path": path, "target": target, "every": every} for path, target, every in fragments]
    )

def _page(title: str, body: str, q: str = "", section: str = "",
          poll: str = "[]") -> HTMLResponse:
    return HTMLResponse(
        PAGE.format(
            title=esc(title),
            body=body,
            q=esc(q),
            nav=_nav(section),
            poll_json=poll,
            poll_js=_POLL_JS,
            css_hash=_CSS_HASH,
            repo=esc(github.repo_spec()),
        )
    )
