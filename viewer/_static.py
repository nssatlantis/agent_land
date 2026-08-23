"""viewer._static -- externalized site stylesheet.

Serves the site stylesheet as a separate, cacheable resource instead of
inlining it in every HTML page.
"""

from __future__ import annotations

from starlette.responses import Response


STYLE_CSS = r""":root {
  --ink:#1a202c; --muted:#4f5d6b; --line:#e2e8f0; --accent:#2b6cb0;
  --ok:#2f855a; --fail:#c53030; --warn:#b7791f; --dim:#a0aec0;
  --ok-tint:#e6fffa; --warn-tint:#fefcbf; --info-tint:#f7fafc;
  --ok-border:#9ae6b4; --warn-border:#ecc94b; --info-border:#a0aec0;
  --accent-tint:#ebf4ff; --accent-border:#90cdf4;
  --banner-ok:#2f855a; --banner-fail:#c53030; --banner-warn:#b7791f;
  --border:var(--line); --border-strong:#cbd5e0;
  --surface:#fff; --surface-alt:#f7fafc; --surface-hover:#edf2f7;
}
* { box-sizing:border-box; margin:0; padding:0; }
body {
  font-family: system-ui, -apple-system, sans-serif;
  font-size: 16px; line-height: 1.55; color: var(--ink);
  background: var(--surface-alt);
}
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
header {
  background:var(--surface); border-bottom:1px solid var(--border);
  padding:12px 20px; display:flex; align-items:center; gap:18px;
  position:sticky; top:0; z-index:10;
}
header h1 { font-size:20px; }
header nav { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
header nav a { font-size:14px; padding:3px 8px; border-radius:4px; }
header nav a.active { background:var(--accent); color:#fff; font-weight:600; }
header form { margin-left:auto; }
header input[type=text] {
  font-size:14px; padding:4px 10px; border:1px solid var(--line);
  border-radius:4px; width:170px;
}
main { max-width:960px; margin:20px auto; padding:0 20px; }
footer { text-align:center; color:var(--muted); font-size:13px; padding:24px 0; }
.panel {
  background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:16px 20px; margin-bottom:14px;
}
.panel h2 { font-size:17px; margin-bottom:8px; }
.panel h3 { font-size:15px; margin-bottom:6px; }
details.panel > summary { cursor:pointer; list-style:none; }
details.panel > summary::-webkit-details-marker { display:none; }
.tabs { display:flex; gap:0; border-bottom:2px solid var(--border); margin-bottom:12px; }
.tabs a {
  padding:6px 14px; font-size:14px; color:var(--muted);
  border-bottom:2px solid transparent; margin-bottom:-2px;
}
.tabs a.active { color:var(--accent); border-bottom-color:var(--accent); font-weight:600; }
.tab-phase { display:flex; gap:0; }
.tab-phase-label {
  font-size:11px; text-transform:uppercase; letter-spacing:.5px;
  color:var(--muted); padding:6px 8px 6px 0;
}
.sort-row { font-size:13px; color:var(--muted); margin-bottom:10px; }
.sort-row a { font-size:13px; margin:0 4px; }
.sort-row a.active { font-weight:600; color:var(--accent); }
.seg { display:inline-flex; gap:2px; }
.seg a { padding:2px 7px; border-radius:4px; }
.seg a.active { background:var(--accent); color:#fff; }
.pager { font-size:13px; color:var(--muted); margin:10px 0; text-align:center; }
.pager a { font-size:13px; margin:0 3px; padding:2px 7px; border-radius:4px; }
.pager a.active { background:var(--accent); color:#fff; font-weight:600; }
.pager.top { margin-bottom:8px; }
.meta { font-size:13px; color:var(--muted); margin-bottom:6px; }
.muted { color:var(--muted); }
.userlink { font-weight:600; }
.post-card {
  background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:14px 18px; margin-bottom:10px;
}
.post-card h3 { font-size:16px; margin-bottom:4px; }
.post-excerpt { font-size:14px; color:var(--muted); margin-top:4px; }
.post-page .post-body { margin-top:10px; }
.post-body h1,.post-body h2,.post-body h3 { margin:14px 0 6px; }
.post-body p { margin:8px 0; }
.post-body pre { background:var(--surface-alt); padding:12px; border-radius:4px; overflow-x:auto; font-size:14px; }
.post-body code { font-size:14px; background:var(--surface-alt); padding:1px 4px; border-radius:3px; }
.post-body pre code { background:none; padding:0; }
.post-body ul,.post-body ol { margin:8px 0 8px 24px; }
.post-body blockquote { border-left:3px solid var(--accent); padding-left:12px; color:var(--muted); margin:8px 0; }
.kind-badge {
  display:inline-block; font-size:11px; font-weight:600;
  padding:1px 7px; border-radius:4px; margin-right:6px;
  text-transform:uppercase; letter-spacing:.3px;
}
.kind-proposal { background:var(--accent-tint); color:var(--accent); border:1px solid var(--accent-border); }
.kind-smallfix { background:var(--warn-tint); color:var(--warn); border:1px solid var(--warn-border); }
.verdict-chip {
  display:inline-block; font-size:12px; font-weight:600;
  padding:2px 8px; border-radius:4px; margin-left:4px;
}
.vc-ok { background:var(--ok-tint); color:var(--ok); }
.vc-fail { background:#fed7d7; color:var(--fail); }
.vc-warn { background:var(--warn-tint); color:var(--warn); }
.vc-dim { background:var(--surface-alt); color:var(--muted); }
.vote-bar { margin:4px 0; }
.vote-track {
  display:inline-block; width:120px; height:8px;
  background:var(--surface-alt); border:1px solid var(--border);
  border-radius:4px; vertical-align:middle; overflow:hidden;
}
.vote-fill { height:100%; border-radius:3px; transition:width .3s; }
.vote-ok { background:var(--ok); }
.vote-fail { background:var(--fail); }
.vote-warn { background:var(--warn); }
.vote-label { font-size:12px; color:var(--muted); margin-left:6px; }
.pr-trail { font-size:13px; margin-top:2px; }
.pr-label { font-weight:600; color:var(--muted); margin-right:4px; }
.pr-chip {
  display:inline-block; font-size:11px; padding:1px 6px;
  border-radius:3px; margin-left:4px;
}
.pr-merged { background:var(--ok-tint); color:var(--ok); }
.pr-open { background:var(--accent-tint); color:var(--accent); }
.pr-declined { background:#fed7d7; color:var(--fail); }
.pr-closed { background:var(--surface-alt); color:var(--muted); }
.bounty-bar-track {
  display:inline-block; width:80px; height:8px;
  background:var(--surface-alt); border:1px solid var(--border);
  border-radius:4px; vertical-align:middle; overflow:hidden;
}
.bounty-bar-fill { height:100%; background:var(--ok); border-radius:3px; }
.stale-card { opacity:.7; }
.tags-row { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }
.tag-chip {
  display:inline-block; font-size:13px; padding:3px 10px;
  border-radius:12px; font-weight:500;
}
.tag-swatch { display:inline-block; width:12px; height:12px; border-radius:50%; vertical-align:middle; }
.overview-cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:14px; }
.overview-card {
  background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:14px 16px; text-align:center;
}
.overview-card .big { font-size:28px; font-weight:700; color:var(--accent); }
.overview-card .label { font-size:13px; color:var(--muted); margin-top:2px; }
.rail-item { padding:4px 0; border-bottom:1px solid var(--border); font-size:14px; }
.rail-meta { font-size:12px; color:var(--muted); display:block; }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th,td { padding:6px 10px; text-align:left; border-bottom:1px solid var(--border); }
th { font-weight:600; font-size:13px; color:var(--muted); }
.search-group { margin-bottom:16px; }
.search-group h3 { font-size:15px; margin-bottom:6px; }
.bounty-card {
  background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:14px 18px; margin-bottom:10px;
}
.recent-day-divider {
  font-size:12px; font-weight:600; color:var(--muted);
  text-transform:uppercase; letter-spacing:.5px;
  padding:10px 0 4px; border-bottom:1px solid var(--border);
  margin-bottom:6px;
}
.banner { padding:12px 20px; border-radius:8px; margin-bottom:12px; display:flex; align-items:center; gap:10px; font-weight:600; }
.banner.ok { background:var(--ok-tint); border:1px solid var(--ok-border); color:var(--ok); }
.banner.fail { background:#fed7d7; border:1px solid #feb2b2; color:var(--fail); }
.banner.warn { background:var(--warn-tint); border:1px solid var(--warn-border); color:var(--warn); }
.dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
.dot.ok { background:var(--ok); }
.dot.fail { background:var(--fail); }
.dot.warn { background:var(--warn); }
.panel.status-panel { border-left:3px solid var(--accent); }
.pulse { display:inline-flex; align-items:center; gap:6px; font-size:13px; padding:4px 10px; border-radius:6px; background:var(--surface-alt); border:1px solid var(--border); }
.pulse .dot { width:8px; height:8px; }
pre.diff { background:var(--surface-alt); padding:12px; border-radius:4px; overflow-x:auto; font-size:13px; line-height:1.4; white-space:pre; }
.breadcrumb { font-size:13px; color:var(--muted); margin-bottom:8px; }
.breadcrumb a { font-size:13px; }
.bignums { display:flex; gap:16px; flex-wrap:wrap; margin:8px 0; }
.bignum { text-align:center; }
.bignum .val { font-size:24px; font-weight:700; color:var(--accent); display:block; }
.bignum .lbl { font-size:12px; color:var(--muted); }
.list-item { padding:8px 0; border-bottom:1px solid var(--border); }
.list-item:last-child { border-bottom:none; }
.comment-body { margin:4px 0 0; font-size:14px; }
details.show-more > summary { cursor:pointer; list-style:none; color:var(--accent); font-size:13px; }
details.show-more > summary::-webkit-details-marker { display:none; }
@media (max-width:640px) {
  main { padding:0 12px; }
  .overview-cards { grid-template-columns:1fr 1fr; }
  header { flex-wrap:wrap; }
  header input[type=text] { width:120px; }
}
"""

_CSS_HASH = "CF49996AD852F01D"


def static_style_css(request) -> Response:
    return Response(
        STYLE_CSS,
        media_type="text/css",
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "ETag": f'"{_CSS_HASH}"',
        },
    )
