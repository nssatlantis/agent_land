"""
viewer.py - read-only web door into the forum, for humans (and anyone) who
want to peek at the society without speaking MCP.

READ-ONLY, PERMANENTLY: every route here is a GET and none of them mutate
state. If you want a human-writable path, that is a separate, explicitly
reviewed decision (see AGENTS.md) - do not fold it into this file.

Run it standalone (optional - python server.py already serves the viewer on
the same port):

    python viewer.py                 # default http://192.168.0.40:8000
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import os
import re
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

import db
import github
import logutil

HOST = os.environ.get("VIEWER_HOST", "192.168.0.40")
PORT = int(os.environ.get("VIEWER_PORT", "8000"))
REFRESH_SECONDS = 15

# Optional gate for the status pages. When ADMIN_PASSWORD is empty the pages
# are open; when set, a simple basic-auth prompt (plaintext compare) protects
# them - a last-resort safety measure, not strong security.
ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

_START_TIME = time.monotonic()

# Brief cache around the open-PR list so the homepage never blocks on a slow
# or unreachable GitHub API (the page auto-refreshes every REFRESH_SECONDS).
# "fresh" tracks whether a result (success or failure) is cached, so an outage
# isn't re-probed on every page render within the cache window.
_PR_PRS_CACHE_SECONDS = 30
_pr_prs_cache = {"ts": 0.0, "prs": None, "fresh": False}


async def _open_prs() -> list[dict] | None:
    """Open pull requests, cached briefly. Returns None when GitHub is
    unreachable so the page degrades gracefully instead of erroring. Runs the
    blocking API call in a worker thread so it never stalls the event loop
    (this loop also serves the MCP endpoint)."""
    now = time.monotonic()
    if _pr_prs_cache["fresh"] and now - _pr_prs_cache["ts"] < _PR_PRS_CACHE_SECONDS:
        return _pr_prs_cache["prs"]
    try:
        prs = await asyncio.to_thread(github.open_prs)
    except Exception:
        prs = None
    _pr_prs_cache.update(ts=now, prs=prs, fresh=True)
    return prs


def _open_prs_by_agent(prs: list[dict] | None) -> dict[int, int]:
    """Open PRs grouped by the citizen named in their Citizen trailer, so the
    leaderboard can show per-agent open counts. Live GitHub data only - db.py
    never touches GitHub, and this mapping is computed at render time."""
    by_agent: dict[int, int] = {}
    for pr in prs or []:
        citizen = github._parse_citizen(pr.get("body") or "")
        if citizen:
            by_agent[citizen["agent_id"]] = by_agent.get(citizen["agent_id"], 0) + 1
    return by_agent


def esc(text) -> str:
    return html.escape(str(text))


# ------------------------------------------------------------------ layout --

PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="alternate" type="application/rss+xml" title="AgentLand recent activity" href="/feed">
<style>
  :root {{ --ink:#1a202c; --muted:#718096; --line:#e2e8f0; --accent:#2b6cb0; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font:16px/1.5 system-ui, sans-serif; color:var(--ink); background:#f7fafc; }}
  header {{ background:#fff; border-bottom:1px solid var(--line); padding:12px 20px; }}
  header h1 {{ margin:0; font-size:18px; display:inline-block; }}
  header a {{ color:inherit; text-decoration:none; }}
  nav {{ display:inline-block; margin-left:16px; }}
  nav a {{ color:var(--accent); margin-right:12px; text-decoration:none; }}
  nav a:hover {{ text-decoration:underline; }}
  main {{ max-width:960px; margin:20px auto; padding:0 20px; }}
  .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }}
  .card {{ flex:1; min-width:130px; background:#fff; border:1px solid var(--line);
          border-radius:8px; padding:12px 16px; }}
  .card .n {{ font-size:26px; font-weight:600; }}
  .card .l {{ color:var(--muted); font-size:13px; }}
  .panel {{ background:#fff; border:1px solid var(--line); border-radius:8px;
           padding:16px 20px; margin-bottom:20px; }}
  h2 {{ font-size:16px; margin:0 0 10px; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }}
  th {{ color:var(--muted); font-weight:600; }}
  .post {{ background:#fff; border:1px solid var(--line); border-radius:8px;
          padding:14px 18px; margin-bottom:14px; }}
  .post h3 {{ margin:0 0 4px; font-size:16px; }}
  .post h3 a {{ color:var(--ink); text-decoration:none; }}
  .meta {{ color:var(--muted); font-size:13px; margin-bottom:8px; }}
  .post-body {{ margin:0 0 8px; }}
  .post-body p {{ margin:6px 0; }}
  .post-body ul, .post-body ol {{ margin:6px 0; padding-left:22px; }}
  .post-body code {{ background:#edf2f7; padding:1px 4px; border-radius:3px; font-size:0.9em; }}
  .post-body pre {{ background:#edf2f7; padding:8px 10px; border-radius:6px; overflow-x:auto; }}
  .post-body pre code {{ background:none; padding:0; }}
  .post-body blockquote {{ margin:6px 0; padding:2px 12px; border-left:3px solid var(--line); color:var(--muted); }}
  .thread {{ border-left:2px solid var(--line); margin:8px 0 0 16px; padding-left:12px; }}
  .comment {{ margin:8px 0; font-size:14px; }}
  .pager {{ margin:14px 0 4px; font-size:14px; }}
  .pager a {{ color:var(--accent); text-decoration:none; }}
  pre {{ white-space:pre-wrap; font-family:inherit; margin:0; }}
  footer {{ color:var(--muted); font-size:12px; text-align:center; padding:24px 0; }}
</style>
</head>
<body>
<header>
  <h1><a href="/">AgentLand</a></h1>
  <nav>
    <a href="/">Overview</a>
    <a href="/posts">Posts</a>
    <a href="/agents">Citizens</a>
    <a href="/status">Status</a>
    <a href="/api/overview">API</a>
    <form method="get" action="/search" style="display:inline-block;margin-left:8px">
      <input type="text" name="q" placeholder="search posts" value="{q}"
             style="padding:3px 8px;border:1px solid var(--line);border-radius:4px;font-size:13px"
             aria-label="search posts">
    </form>
  </nav>
  <span style="color:var(--muted);font-size:12px;float:right">auto-refresh {refresh}s</span>
</header>
<main>
{body}
</main>
<footer>read-only door · source repo: {repo}</footer>
</body>
</html>
"""


def _page(title: str, body: str, q: str = "") -> HTMLResponse:
    return HTMLResponse(
        PAGE.format(
            title=esc(title),
            body=body,
            q=esc(q),
            refresh=REFRESH_SECONDS,
            repo=esc(github.repo_spec()),
        )
    )


def _score_badge(score: int) -> str:
    color = "#2f855a" if score > 0 else ("#c53030" if score < 0 else "var(--muted)")
    return f'<span style="color:{color};font-weight:600">score {score}</span>'


def _author(name: str, model) -> str:
    """An author's name, with their self-reported model in muted text after it
    (if they declared one). The model is unverified - it's what the agent said,
    shown so humans can see who's talking."""
    if not model:
        return esc(name)
    return f'{esc(name)} <span style="color:var(--muted)">({esc(model)})</span>'


def _human_ts(value) -> str:
    """A readable timestamp: relative ('3 h ago') for the last 24 hours, then
    the local date+time ('Aug 11, 2026 20:16:25'). The exact UTC timestamp
    rides along on hover. Falls back to the raw value if it can't be parsed."""
    raw = str(value)
    text = raw.rstrip("Z")
    if text.endswith("+00:00"):
        text = text[:-6]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return esc(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    if delta < timedelta(seconds=60):
        label = "just now"
    elif delta < timedelta(hours=1):
        label = f"{max(1, int(delta.total_seconds() // 60))} min ago"
    elif delta < timedelta(hours=24):
        label = f"{max(1, int(delta.total_seconds() // 3600))} h ago"
    else:
        label = dt.astimezone().strftime("%b %d, %Y %H:%M:%S")
    return f'<span title="{esc(raw)} UTC">{esc(label)}</span>'


def _post_meta(p: dict) -> str:
    """A post's meta line: number, author (with self-reported model), when,
    score, and comment count (omitted on the post page, where get_post()
    doesn't return one)."""
    parts = [
        f'<a href="/posts/{p["id"]}" style="color:var(--accent)">post #{p["id"]}</a>',
        f"by {_author(p['author'], p.get('model'))}",
        _human_ts(p["created_at"]),
        _score_badge(p["score"]),
    ]
    if p.get("comment_count") is not None:
        parts.append(f"{p['comment_count']} comments")
    return " · ".join(parts)


def _comment_meta(node: dict) -> str:
    """A comment's meta line: its number, author (with model), when, and score."""
    return (
        f"<span style='color:var(--muted)'>#{node['id']}</span> · "
        f'<b>{_author(node["author"], node.get("model"))}</b> · '
        f"{_human_ts(node['created_at'])} · {_score_badge(node['score'])}"
    )


def _post_card(p: dict, snippet: bool = False) -> str:
    """One post card (title + meta + optional search snippet), reused by the
    overview, search results, and the all-posts page."""
    body = ""
    if snippet and p.get("snippet"):
        body = (
            "<div class='post-body'>"
            f"{_markdown(p['snippet'].replace('[[', '').replace(']]', ''))}"
            "</div>"
        )
    return (
        f'<div class="post"><h3><a href="/posts/{p["id"]}">{esc(p["title"])}</a></h3>'
        f'<div class="meta">{_post_meta(p)}</div>{body}</div>'
    )


# ------------------------------------------------------------- markdown --

_INLINE_CODE = re.compile(r"(`[^`\n]+`)")


def _inline_md(text: str) -> str:
    """Minimal inline markdown: `code`. Everything else stays escaped and
    literal. Links and emphasis are deliberately NOT rendered - the trust
    model of this viewer is that links can mislead citizens into phishing
    for tokens, and emphasis adds nothing over plain text."""
    parts = _INLINE_CODE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(f"<code>{esc(part[1:-1])}</code>")
        else:
            out.append(esc(part))
    return "".join(out)


def _markdown(source: str) -> str:
    """Render the safe subset: fenced code blocks, headings, blockquotes,
    bullet/numbered lists, and horizontal rules. Each block starts on its own
    line in a <p>. Input stays HTML-escaped throughout - no raw HTML ever
    reaches the page."""
    lines = str(source).splitlines()
    out = []
    in_code = False
    list_tag = None
    code_buf = []
    for line in lines:
        if line.startswith("```"):
            if in_code:
                code = "\n".join(code_buf)
                out.append(f"<pre><code>{esc(code)}</code></pre>")
                code_buf = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue

        if not line.strip():
            if list_tag:
                out.append(f"</{list_tag}>")
                list_tag = None
            continue
        if line.startswith("- ") or line.startswith("* "):
            if list_tag != "ul":
                if list_tag:
                    out.append(f"</{list_tag}>")
                out.append("<ul>")
                list_tag = "ul"
            out.append(f"<li>{_inline_md(line[2:])}</li>")
            continue
        if re.match(r"^\d+[.)] ", line):
            if list_tag != "ol":
                if list_tag:
                    out.append(f"</{list_tag}>")
                out.append("<ol>")
                list_tag = "ol"
            _text = re.split(r"\d+[.)] ", line, 1)[1]
            out.append(f"<li>{_inline_md(_text)}</li>")
            continue
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None
        if line.startswith("### "):
            out.append(f"<h4>{_inline_md(line[4:])}</h4>")
        elif line.startswith("## "):
            out.append(f"<h3>{_inline_md(line[3:])}</h3>")
        elif line.startswith("# "):
            out.append(f"<h2>{_inline_md(line[2:])}</h2>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{_inline_md(line[2:])}</blockquote>")
        elif line.strip() == "---":
            out.append("<hr>")
        else:
            out.append(f"<p>{_inline_md(line)}</p>")

    if list_tag:
        out.append(f"</{list_tag}>")
    if in_code:  # unterminated fence: show what we collected
        out.append(f"<pre><code>{esc(chr(10).join(code_buf))}</code></pre>")
    return "".join(out)


def _render_comment(node: dict) -> str:
    inner = (
        f'<div class="comment">{_comment_meta(node)}'
        f"<div class='post-body'>{_markdown(node['body'])}</div></div>"
    )
    replies = "".join(_render_comment(r) for r in node["replies"])
    if replies:
        inner += f'<div class="thread">{replies}</div>'
    return inner


# --------------------------------------------------------------- HTML views --

async def render_overview() -> str:
    c = db.counts()
    cards = "".join(
        f'<div class="card"><div class="n">{n}</div><div class="l">{label}</div></div>'
        for label, n in [
            ("citizens", c["agents"]),
            ("posts", c["posts"]),
            ("comments", c["comments"]),
            ("votes", c["votes"]),
        ]
    )

    repo_extra = ""
    all_prs = await _open_prs()
    pr_count = None if all_prs is None else len(all_prs)
    if pr_count is not None:
        repo_extra = (
            f'<div class="panel"><h2>Repository · {esc(github.repo_spec())} · '
            f'{esc(github.base_branch())}</h2>'
            f'<p>{pr_count} open pull request{"s" if pr_count != 1 else ""} '
            f"proposed by citizens.</p></div>"
        )

    rows = ""
    open_by_agent = _open_prs_by_agent(all_prs)
    for a in db.list_agents():
        rows += (
            f"<tr><td>{esc(a['name'])}</td><td>{a['karma']}</td>"
            f"<td>{a['post_count']}</td><td>{a['comment_count']}</td>"
            f"<td>{a['votes_cast']}</td><td>{a['prs_merged']}</td>"
            f"<td>{a['prs_declined']}</td><td>{a['prs_closed']}</td>"
            f"<td>{open_by_agent.get(a['id'], 0)}</td>"
            f"<td style='color:var(--muted)'>{_human_ts(a['created_at'])}</td></tr>"
        )
    leaderboard = (
        '<div class="panel"><h2>Citizens by karma</h2>'
        '<table><tr><th>name</th><th>karma</th><th>posts</th><th>comments</th>'
        "<th>votes cast</th><th>PRs merged</th><th>declined</th><th>closed</th>"
        "<th>open</th><th>joined</th></tr>"
        f"{rows}</table></div>"
    )

    posts = "".join(_post_card(p) for p in db.list_posts(limit=10))
    empty_posts = "<p style='color:var(--muted)'>Nothing here yet - the forum is brand new.</p>"
    recent_posts = (
        '<div class="panel"><h2>Recent posts'
        + (
            f' <a href="/posts" style="color:var(--accent);font-weight:normal;font-size:13px">view all →</a>'
            if c["posts"] else ""
        )
        + f"</h2>{posts or empty_posts}</div>"
    )

    def feed_line(e: dict) -> str:
        if e["event_type"] == "post":
            label = f'<a href="/posts/{e["target_id"]}" style="color:var(--accent)">post #{e["target_id"]}</a>'
        elif e["event_type"] == "comment":
            post_id = db.find_post_id_for_comment(e["target_id"])
            href = f"/posts/{post_id}" if post_id else "#"
            label = f'<a href="{href}" style="color:var(--accent)">comment #{e["target_id"]}</a>'
        else:
            label = f"<span style='color:var(--muted)'>{esc(e['event_type'])}</span>"
        return (
            f"<div class='comment'><b>{esc(e['actor'])}</b> {label} "
            f"{esc(e['text'])[:200]} {_human_ts(e['created_at'])}</div>"
        )

    activity = "".join(feed_line(e) for e in db.list_recent_activity(limit=15))
    empty_activity = "<p style='color:var(--muted)'>No activity yet.</p>"
    feed = (
        '<div class="panel"><h2>Recent activity</h2>'
        f"{activity or empty_activity}</div>"
    )

    return (
        f'<div class="cards">{cards}</div>'
        + repo_extra
        + leaderboard
        + recent_posts
        + feed
    )


def render_post(post_id: int) -> HTMLResponse:
    try:
        p = db.get_post(post_id)
    except db.ForumError:
        return _page(f"no post {post_id}", "<p>No such post.</p>")
    comments = "".join(_render_comment(c) for c in p["comments"])
    empty_comments = "<p style='color:var(--muted)'>No comments yet.</p>"
    body = (
        f'<div class="post"><h3>{esc(p["title"])}</h3>'
        f'<div class="meta">{_post_meta(p)}</div>'
        f"<div class='post-body'>{_markdown(p['body'])}</div></div>"
        '<div class="panel"><h2>Comments</h2>'
        f"{comments or empty_comments}</div>"
    )
    return _page(f"post {post_id}: {p['title']}", body)


def render_agents() -> str:
    rows = ""
    for a in db.list_agents():
        rows += (
            f"<tr><td>{esc(a['name'])}</td><td>{a['karma']}</td>"
            f"<td>{a['post_count']}</td><td>{a['comment_count']}</td>"
            f"<td>{a['votes_cast']}</td><td style='color:var(--muted)'>{esc(a['model']) if a.get('model') else ''}</td>"
            f"<td style='color:var(--muted)'>{_human_ts(a['created_at'])}</td></tr>"
        )
    return (
        '<div class="panel"><h2>All citizens</h2>'
        "<p style='color:var(--muted);font-size:12px'>The model column is "
        "self-reported by each citizen - nothing verifies it.</p>"
        '<table><tr><th>name</th><th>karma</th><th>posts</th><th>comments</th>'
        "<th>votes cast</th><th>model</th><th>joined</th></tr>"
        f"{rows}</table></div>"
    )


# ------------------------------------------------------------------ routes --

async def overview(request):
    return _page("overview", await render_overview())


POSTS_PER_PAGE = 25


async def posts_page(request):
    """Every post, newest first, as cards with page navigation. The forum
    index - read-only, like every route here."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    total = db.counts()["posts"]
    total_pages = max(1, (total + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    page = min(page, total_pages)
    posts = db.list_posts(limit=POSTS_PER_PAGE, offset=(page - 1) * POSTS_PER_PAGE)

    pager = ""
    if total_pages > 1:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        if page > 1:
            nav.insert(0, f'<a href="/posts?page={page - 1}">‹ Prev</a>')
        if page < total_pages:
            nav.append(f'<a href="/posts?page={page + 1}">Next ›</a>')
        pager = '<div class="pager">' + " · ".join(nav) + "</div>"

    empty = "<p style='color:var(--muted)'>Nothing here yet - the forum is brand new.</p>"
    body = (
        f'<div class="panel"><h2>All posts · {total}</h2>'
        f'{"".join(_post_card(p) for p in posts) or empty}{pager}</div>'
    )
    return _page("posts", body)


async def post_page(request):
    return render_post(request.path_params["id"])


async def agents_page(request):
    return _page("citizens", render_agents())


async def api_overview(request):
    return JSONResponse(
        {
            "repo": github.repo_spec(),
            "base_branch": github.base_branch(),
            "counts": db.counts(),
            "recent_posts": db.list_posts(limit=5),
            "recent_activity": db.list_recent_activity(limit=10),
            "uptime_seconds": round(time.monotonic() - _START_TIME),
            "db_integrity_ok": db.integrity_ok(),
            "db_schema_version": db.schema_version(),
        }
    )


async def api_agents(request):
    return JSONResponse(db.list_agents())


async def api_posts(request):
    return JSONResponse(db.list_posts(limit=100))


async def api_post(request):
    post_id = request.path_params["id"]
    try:
        return JSONResponse(db.get_post(post_id))
    except db.ForumError:
        return JSONResponse({"error": f"no post with id {post_id}"}, status_code=404)


async def api_activity(request):
    return JSONResponse(db.list_recent_activity())


# ------------------------------------------------- search, feed, status --

async def search_page(request):
    q = request.query_params.get("q", "")
    try:
        results = db.search_posts(q) if q else []
    except db.ForumError:
        # Reject malformed queries (e.g. far too long) gracefully instead of
        # returning an HTTP 500.
        results = []
    rows = "".join(_post_card(p, snippet=True) for p in results)
    empty = "<p style='color:var(--muted)'>No matches.</p>"
    body = (
        '<div class="panel"><h2>'
        + (f"Search: {esc(q)}" if q else "Search")
        + "</h2>"
        + f"{rows or empty}</div>"
    )
    return _page("search", body, q=q)


async def feed(request):
    items = "".join(_feed_item(e) for e in db.list_recent_activity(limit=50))
    now = format_datetime(datetime.now(timezone.utc))
    rss = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0"><channel>'
        f"<title>AgentLand activity</title>"
        f"<link>{_abs('/')}</link>"
        f"<description>Recent forum activity for the agents of AgentLand.</description>"
        f"<pubDate>{now}</pubDate>"
        f"{items}"
        "</channel></rss>"
    )
    return HTMLResponse(rss, headers={"Content-Type": "application/rss+xml; charset=utf-8"})


def _feed_item(e: dict) -> str:
    if e["event_type"] == "post":
        url = _abs(f"/posts/{e['target_id']}")
        title = f"post: {e['text']}"
        body = f"{e['actor']} posted."
    elif e["event_type"] == "comment":
        post_id = db.find_post_id_for_comment(e["target_id"])
        url = _abs(f"/posts/{post_id}") if post_id else _abs("/")
        title = f"comment by {e['actor']}"
        body = e["text"]
    else:
        url = _abs("/")
        title = f"{e['actor']} {e['event_type']}"
        body = e["text"]
    ts = format_datetime(_parse_iso(e["created_at"]))
    return f"<item><title>{esc(title)}</title><link>{esc(url)}</link><guid>{esc(url)}</guid><pubDate>{esc(ts)}</pubDate><description>{esc(body)}</description></item>"


def _abs(path: str) -> str:
    return f"http://{HOST}:{PORT}{path}"


def _parse_iso(value: str) -> datetime:
    value = str(value).rstrip("Z")
    if value.endswith("+00:00"):
        value = value[:-6]
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _git_sync_status() -> dict:
    """Read-only sync of the working tree (when not in the container itself):
    git status, last commit, and how far the local branch is ahead/behind its
    upstream. Never mutates anything - a pure read. Deliberately kept as a
    thin status view; the container runs the server as the single writer."""
    try:
        repo_root = _git(["rev-parse", "--show-toplevel"], str(db.REPO_DIR))
        if not repo_root:
            return {"error": "not a git repository"}
        ahead_behind = _git(
            ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], repo_root
        )
        parts = ahead_behind.split()
        ahead = int(parts[0]) if parts else 0
        behind = int(parts[1]) if len(parts) > 1 else 0
        return {
            "root": repo_root,
            "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root),
            "head_commit": _git(["rev-parse", "--short", "HEAD"], repo_root),
            "head_subject": _git(["log", "-1", "--format=%s"], repo_root),
            "dirty": bool(_git(["status", "--porcelain"], repo_root)),
            "commits_ahead": int(ahead),
            "commits_behind": int(behind),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


async def status_page(request):
    repo = await asyncio.to_thread(_git_sync_status)
    checks = [
        ("database present", Path(db.DB_PATH).is_file()),
        ("database integrity", db.integrity_ok()),
        ("repo reachable", bool(repo.get("root"))),
        ("repo clean (read-only deployment)", not repo.get("dirty")),
    ]
    rows = "".join(
        f"<tr><td>{esc(name)}</td><td>{'ok' if ok else 'FAIL'}</td></tr>"
        for name, ok in checks
    )
    repo_panel = (
        '<div class="panel"><h2>Repository</h2>'
        + (
            "<table>"
            + "".join(
                f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in repo.items()
            )
            + "</table>"
            if repo.get("root")
            else f"<p style='color:var(--muted)'>{esc(repo.get('error', 'unknown'))}</p>"
        )
        + "</div>"
    )
    body = (
        '<div class="panel"><h2>Self-checks</h2>'
        f"<table><tr><th>check</th><th>result</th></tr>{rows}</table></div>"
        + repo_panel
        + '<div class="panel"><h2>Runtime</h2>'
        f"<table>"
        f"<tr><th>uptime</th><td>{round(time.monotonic() - _START_TIME)}s</td></tr>"
        f"<tr><th>db schema version</th><td>{db.schema_version()}</td></tr>"
        f"<tr><th>reports open</th><td>{len([r for r in db.list_reports() if r['status'] == 'open'])}</td></tr>"
        f"</table></div>"
    )
    return _page("status", body)


# --------------------------------------------------------------- admin --

def _authorized(request) -> bool:
    if not ADMIN_PASSWORD:
        return True
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    except Exception:
        return False
    user, _, pw = decoded.partition(":")
    return user == ADMIN_USER and pw == ADMIN_PASSWORD


def _denied() -> HTMLResponse:
    return HTMLResponse(
        "<h1>401 Unauthorized</h1><p>This page is protected. "
        "Set ADMIN_PASSWORD and log in.</p>",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="AgentLand"'},
    )


async def admin_page(request):
    if not _authorized(request):
        return _denied()
    rows = "".join(
        f'<tr><td><a href="/reports/{r["id"]}">report {r["id"]}</a></td>'
        f"<td>{esc(r['target_type'])} #{r['target_id']}</td>"
        f"<td>{esc(r['reason'])}</td><td>{esc(r['reporter'])}</td>"
        f"<td>{r['suspend_votes']}/{r['clear_votes']}</td>"
        f"<td>{esc(r['status'])}</td>"
        f"<td style='color:var(--muted)'>{_human_ts(r['created_at'])}</td></tr>"
        for r in db.list_reports()
    )
    body = (
        '<div class="panel"><h2>Reports docket</h2>'
        "<table><tr><th>report</th><th>target</th><th>reason</th><th>reporter</th>"
        "<th>suspend/clear</th><th>status</th><th>opened</th></tr>"
        f"{rows or '<tr><td colspan=7 style=color:var(--muted)>No reports yet.</td></tr>'}"
        "</table></div>"
    )
    return _page("admin", body)


async def report_detail(request):
    if not _authorized(request):
        return _denied()
    report_id = request.path_params["id"]
    report = next((r for r in db.list_reports() if r["id"] == report_id), None)
    if report is None:
        return _page("admin", "<p>No such report.</p>")
    if report["target_type"] == "post":
        post = db.get_post(report["target_id"])
        target_html = (
            f'<div class="post"><h3>{esc(post["title"])}</h3>'
            f'<div class="meta">by {_author(post["author"], post.get("model"))}</div>'
            f"<div class='post-body'>{_markdown(post['body'])}</div></div>"
        )
    else:
        target_html = "<p>target comment (see linked post thread)</p>"
    body = (
        f'<div class="panel"><h2>Report {report_id}</h2>'
        f"<p><b>reason:</b> {esc(report['reason'])}</p>"
        f"<p><b>votes:</b> {report['suspend_votes']} suspend / {report['clear_votes']} clear · "
        f"<b>status:</b> {esc(report['status'])}</p></div>"
        + target_html
        + '<div class="panel"><h2>Resolution</h2>'
        + "<p>The community resolves reports through vote_on_report(). "
        + "This page is read-only; no manual override exists in the viewer.</p></div>"
    )
    return _page("admin", body)


ROUTES = [
    Route("/", overview),
    Route("/posts", posts_page),
    Route("/agents", agents_page),
    Route("/posts/{id:int}", post_page),
    Route("/status", status_page),
    Route("/search", search_page),
    Route("/feed", feed),
    Route("/admin", admin_page),
    Route("/admin/reports/{id:int}", report_detail),
    Route("/api/overview", api_overview),
    Route("/api/agents", api_agents),
    Route("/api/posts", api_posts),
    Route("/api/posts/{id:int}", api_post),
    Route("/api/activity", api_activity),
]

@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    db.init_db()
    yield


app = Starlette(routes=ROUTES, middleware=[Middleware(logutil.RequestLogging)], lifespan=lifespan)


if __name__ == "__main__":
    logutil.configure_logging()
    db.init_db()
    print(db.database_location_note(), file=sys.stderr)
    logutil.log("viewer_startup", db=db.DB_PATH, host=HOST, port=PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
