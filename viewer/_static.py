"""viewer._static - externalized CSS for the viewer.

Serves the site stylesheet as a separate, cacheable resource instead of
inlining it on every page.  Read-only, like every viewer route.
"""

from __future__ import annotations

from starlette.responses import Response

STYLE_CSS = r"""  # 4715: served via _CSS_HASH, already cacheable  :root { --ink:#1a202c; --muted:#4f5d6b; --line:#e2e8f0; --accent:#2b6cb0;
           --ok:#2f855a; --fail:#c53030; --warn:#b7791f; --dim:#a0aec0;
           --ok-tint:#e6fffa; --warn-tint:#fefcbf; --info-tint:#f7fafc;
           --ok-border:#9ae6b4; --warn-border:#ecc94b; --info-border:#a0aec0;
           --banner-ok:#38a169; --banner-fail:#e53e3e; --banner-warn:#d69e2e;
           --border:#e2e8f0; --accent-tint:#ebf4ff; --accent-border:#90cdf4; }
  html { -webkit-text-size-adjust:100%; text-size-adjust:100%; }
  * { box-sizing: border-box; }
  body { margin:0; font:19px/1.65 system-ui, sans-serif; color:var(--ink); background:#f7fafc; }
  header { background:#fff; border-bottom:1px solid var(--line); padding:12px 24px;
           display:flex; align-items:center; gap:18px; flex-wrap:wrap;
           position:sticky; top:0; z-index:10; box-shadow:0 1px 3px rgba(0,0,0,.04); }
  header h1 { margin:0; font-size:22px; }
  header a { color:inherit; text-decoration:none; }
  nav { display:flex; align-items:center; gap:16px; flex-wrap:wrap;
        flex:1; min-width:0; }
  nav a { display:inline-block; color:var(--accent); text-decoration:none; font-size:18px;
           font-weight:700; padding:5px 14px; border:1px solid var(--line); border-radius:8px;
           background:#fff; }
  nav a:hover { border-color:var(--accent); background:#f0f7ff; }
  nav a.active { color:#fff; background:var(--accent); border-color:var(--accent); }
  nav details.nav-dropdown { position:relative; }
  nav details.nav-dropdown > summary { cursor:pointer; list-style:none; user-select:none;
    display:inline-block; color:var(--accent); font-size:18px; font-weight:700;
    padding:5px 34px 5px 14px; border:1px solid var(--line); border-radius:8px; background:#fff;
    position:relative; }
  nav details.nav-dropdown > summary::-webkit-details-marker { display:none; }
  nav details.nav-dropdown > summary::after { content:"▾"; position:absolute; right:12px;
    top:50%; transform:translateY(-50%); color:var(--muted); font-size:14px; }
  nav details.nav-dropdown:not([open]) > summary::after { content:"▸"; }
  nav details.nav-dropdown > summary:hover { border-color:var(--accent); background:#f0f7ff; }
  nav details.nav-dropdown > summary.active { color:#fff; background:var(--accent); border-color:var(--accent); }
  nav details.nav-dropdown > summary.active::after { color:#e6f0ff; }
  nav details.nav-dropdown .nav-dropdown-items { position:absolute; top:100%; left:0; z-index:40;
    margin-top:6px; min-width:180px; background:#fff; border:1px solid var(--line); border-radius:8px;
    box-shadow:0 6px 16px rgba(0,0,0,.10); padding:6px; display:flex; flex-direction:column; gap:2px; }
  nav details.nav-dropdown .nav-dropdown-items a { display:block; border:none; background:transparent;
    border-radius:6px; padding:6px 12px; text-align:left; }
  nav details.nav-dropdown .nav-dropdown-items a:hover { background:#f0f7ff; }
  nav details.nav-dropdown .nav-dropdown-items a.active { color:#fff; background:var(--accent); }
  button { font:inherit; font-size:16px; font-weight:700; color:var(--accent);
           background:#fff; border:1px solid var(--line); border-radius:8px;
           padding:5px 14px; cursor:pointer; }
  button:hover { border-color:var(--accent); background:#f0f7ff; }
  button:active { background:#e8f2fc; }
  .userlink { color:var(--accent); text-decoration:none; }
  .userlink:hover { text-decoration:underline; }
  nav form { margin:0; }
  nav input, .top-search input { padding:5px 10px; border:1px solid var(--line); border-radius:6px;
                font:inherit; font-size:16px; }
  .top-search { margin-left:auto; }
  .utc-pill { display:inline-flex; align-items:center; gap:4px; white-space:nowrap;
              border:1px solid var(--line); border-radius:999px; padding:4px 10px;
              font-size:13px; color:var(--muted); background:#fff; }
  .utc-pill #utc-reset-count { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
                                font-weight:600; color:var(--ink); }
  main { max-width:1400px; margin:20px auto; padding:0 20px; }
  .grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(220px,320px); gap:20px; align-items:start; }
  .content { min-width:0; }
  .rail { display:flex; flex-direction:column; gap:20px; min-width:0; }
  .cards { display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }
  .card { flex:1; min-width:130px; background:#fff; border:1px solid var(--line);
          border-radius:8px; padding:12px 16px; }
  .card .n { font-size:30px; font-weight:600; }
  .card .l { color:var(--muted); font-size:16px; }
  .panel { background:#fff; border:1px solid var(--line); border-radius:8px;
           padding:16px 20px; margin-bottom:20px; }
  .rail .panel { margin-bottom:0; padding:14px 18px; }
  details.panel { padding:8px 20px 16px; }
  details.panel > summary { cursor:pointer; list-style:none; }
  details.panel > summary::-webkit-details-marker { display:none; }
  details.panel > summary h2 { display:inline-block; margin:10px 0 10px;
                               padding-right:18px; position:relative; }
  details.panel > summary h2::after { content:"▾"; position:absolute; right:0;
                                       color:var(--muted); font-size:14px; }
  details.panel:not([open]) > summary h2::after { content:"▸"; }
  h2 { font-size:20px; margin:0 0 10px; }
  table { width:100%; border-collapse:collapse; font-size:17px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; }
  th a { color:var(--accent); text-decoration:none; }
  th a:hover { text-decoration:underline; }
  .table-wrap { overflow-x:auto; }
  .table-wrap table { min-width:900px; }
  .table-wrap tbody tr:nth-child(even) { background:#fbfcfe; }
  .scroll-box { max-height:480px; overflow-y:auto; border:1px solid var(--line);
                 border-radius:4px; }
  .profile-scroll { max-height:480px; overflow-y:auto; }
  details.show-more { margin-top:4px; }
  details.show-more > summary { cursor:pointer; list-style:none; color:var(--accent);
                                 font-size:15px; padding:6px 0; }
  details.show-more > summary::-webkit-details-marker { display:none; }
  details.show-more > summary::after { content:" ▾"; color:var(--muted); }
  details.show-more:not([open]) > summary::after { content:" ▸"; }
  details.show-more > summary:hover { text-decoration:underline; }
  td.num { text-align:right; white-space:nowrap; }
  .subline { display:block; color:var(--muted); font-size:14px; font-weight:normal;
              max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .card-meta2 { display:block; color:var(--muted); font-size:14px; font-weight:normal; }
  .post-top { display:flex; justify-content:space-between; align-items:flex-start; gap:14px; }
  .post-top h3 { flex:1; min-width:0; }
  .post-stats { display:flex; gap:14px; align-items:center; flex-shrink:0;
                 font-size:14px; white-space:nowrap; padding-top:3px; }
  .stat-comments { color:var(--muted); background:var(--info-tint); border:1px solid
                    var(--info-border); border-radius:999px; padding:1px 10px; font-weight:700; }
  .activity-note { color:var(--muted); font-size:14px; }
  .avatar { display:inline-flex; width:22px; height:22px; border-radius:50%; color:#fff;
             font-size:12px; font-weight:700; align-items:center; justify-content:center;
             margin-right:6px; vertical-align:-4px; }
  .tally { color:var(--muted); font-size:13px; font-weight:600; }
  .score-badge { display:inline-block; font-size:14px; font-weight:700;
                  padding:2px 8px; border-radius:6px; }
  .score-badge.score-pos { color:var(--ok); background:var(--ok-tint); }
  .score-badge.score-neg { color:var(--fail); background:var(--warn-tint); }
  .score-badge.score-zero { color:var(--muted); background:var(--info-tint); }
  .vote-bar { display:flex; align-items:center; gap:8px; margin:6px 0 2px;
               font-size:14px; }
  .vote-track { flex:1; height:6px; background:var(--line); border-radius:3px;
                 overflow:hidden; max-width:160px; }
  .vote-fill { height:100%; border-radius:3px; transition:width 0.3s; }
  .vote-fill.vote-ok { background:var(--ok); }
  .vote-fill.vote-fail { background:var(--fail); }
  .vote-fill.vote-warn { background:var(--warn); }
  .vote-label { color:var(--muted); font-size:13px; font-weight:600; white-space:nowrap; }

  .post-excerpt { color:var(--muted); font-size:15px; margin:8px 0 4px;
                   padding:8px 12px; border-left:3px solid var(--line);
                   background:var(--info-tint); border-radius:0 6px 6px 0; }
  .post.post-proposal { box-shadow:inset 3px 0 0 var(--accent); }
  .post.post-smallfix { box-shadow:inset 3px 0 0 var(--warn); }
  .post { background:#fff; border:1px solid var(--line); border-radius:8px;
          padding:14px 18px; margin-bottom:14px;
          transition: border-color 0.15s, box-shadow 0.15s; }
  .post h3 { margin:0 0 4px; font-size:20px; }
  .post h3 a { color:var(--ink); text-decoration:none; }
  .post h3 a:hover { color:var(--accent); text-decoration:underline; }
  .post:hover { border-color:var(--accent); box-shadow:0 2px 8px rgba(0,0,0,0.08); }
  .kind-badge { display:inline-block; font-size:12px; font-weight:700;
                 padding:1px 8px; border-radius:10px; margin-right:8px;
                 vertical-align:2px; color:#fff; }
  .kind-proposal { background:var(--accent); }
  .kind-smallfix { background:var(--warn); color:#0f172a; }
  .kind-idea { background:#6366f1; }
  .tabs { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 12px; align-items:center; }
  .tabs a { background:#fff; border:1px solid var(--line); border-radius:999px;
             padding:4px 12px; font-size:14px; color:var(--accent); text-decoration:none; }
  .tabs a:hover { border-color:var(--accent); }
  .tabs a.active { color:#fff; background:var(--accent); border-color:var(--accent); font-weight:600; }
  .tab-phase { display:inline-flex; gap:6px; align-items:center; }
  .tab-phase-label { font-size:11px; font-weight:600; color:var(--muted); text-transform:uppercase;
                      letter-spacing:0.5px; margin-right:2px; }
  .sort-row { margin:0 0 12px; font-size:15px; color:var(--muted); }
  .sort-row .seg { display:inline-flex; border:1px solid var(--line); border-radius:999px;
                    overflow:hidden; background:#fff; margin-left:6px; }
  .sort-row .seg a { padding:2px 14px; color:var(--muted); text-decoration:none;
                      border-left:1px solid var(--line); }
  .sort-row .seg a:first-child { border-left:none; }
  .sort-row .seg a:hover { color:var(--accent); background:#f0f7ff; }
  .sort-row .seg a.active { color:#fff; background:var(--accent); font-weight:600; }
  .tags-row { margin:0 0 8px; display:flex; gap:6px; flex-wrap:wrap; }
  .tag-chip { display:inline-block; font-size:12px; font-weight:600;
               padding:1px 8px; border-radius:10px; text-decoration:none;
               vertical-align:2px; color:var(--ink); }
  .tag-swatch { display:inline-block; width:10px; height:10px;
                 border-radius:50%; vertical-align:1px; }
  .meta { color:var(--muted); font-size:16px; margin-bottom:8px; }
  hr { border:none; border-top:1px solid var(--line); margin:10px 0; }
  .post-preview { color:var(--muted); font-size:17px; margin-top:6px; }
  .post-body { margin:0 0 8px; }
  .post-body p { margin:6px 0; }
  .post-body h2 { font-size:18px; margin:10px 0 4px; }
  .post-body h3 { font-size:16px; margin:10px 0 4px; }
  .post-page h3 { font-size:26px; font-weight:700; margin-bottom:8px; }
  .post-page .meta { font-size:20px; }
  .post-page .post-body { padding-left:24px; max-width:72ch; border-top:1px solid var(--line); padding-top:12px; }
  .comment .post-body { padding-left:24px; max-width:72ch; }
  .comment:target { background:#ebf8ff; }
  .comment { margin:10px 0; scroll-margin-top:70px; transition: background 0.15s; }
  .comment:hover { background:rgba(0,0,0,0.02); }
  .post-body ul, .post-body ol { margin:6px 0; padding-left:22px; }
  .post-body code { background:#edf2f7; padding:1px 4px; border-radius:3px; font-size:0.9em; }
  .post-body pre { background:#edf2f7; padding:8px 10px; border-radius:6px; overflow-x:auto; }
  .post-body pre code { background:none; padding:0; }
  .post-body blockquote { margin:6px 0; padding:2px 12px; border-left:3px solid var(--line); color:var(--muted); }
  blockquote.quote { margin:8px 0; padding:6px 12px; border-left:3px solid var(--accent);
                      background:rgba(127,127,127,0.06); color:var(--ink); }
  .quote-meta { display:block; margin-top:4px; font-size:15px; color:var(--muted); }
  .quote-meta a { color:var(--accent); text-decoration:none; }
  .thread { border-left:2px solid var(--line); margin:8px 0 0 16px; padding-left:12px; }

  .comment-meta { font-size:19px; }
  .pager { margin:14px 0 4px; font-size:17px; display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .pager a { color:var(--accent); text-decoration:none; padding:2px 8px;
              border-radius:6px; transition: background 0.15s; }
  .pager a:hover { background:var(--info-tint); }
  .pager a.active { font-weight:700; background:var(--accent); color:#fff; text-decoration:none; }
  .pager.top { margin:0 0 12px; }
  .verdict-chip { display:inline-block; font-size:12px; font-weight:700;
                   padding:1px 8px; border-radius:10px; margin-right:8px;
                   vertical-align:2px; color:#fff; }
  .verdict-chip.vc-ok { background:var(--ok); }
  .verdict-chip.vc-fail { background:var(--fail); }
  .verdict-chip.vc-warn { background:var(--warn); color:#0f172a; }
  .verdict-chip.vc-dim { background:var(--dim); color:#0f172a; }
  .docket-card { background:#fff; border:1px solid var(--line); border-radius:8px;
                  padding:14px 18px; margin-bottom:14px;
                  transition: border-color 0.15s, box-shadow 0.15s; }
  .docket-card:hover { border-color:var(--accent); box-shadow:0 2px 8px rgba(0,0,0,0.08); }
  .docket-card.stale-card { border-left:3px solid var(--warn); }
  .docket-top { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
  .docket-top h3 { flex:1; min-width:0; margin:0 0 4px; font-size:19px; }
  .docket-top h3 a { color:var(--ink); text-decoration:none; }
  .docket-top h3 a:hover { color:var(--accent); text-decoration:underline; }
  .docket-chips { display:flex; gap:6px; align-items:center; flex-shrink:0; flex-wrap:wrap; }
  .docket-vote { display:flex; align-items:center; gap:8px; font-size:14px; margin:4px 0; }
  .pr-trail { display:flex; gap:6px; align-items:center; flex-wrap:wrap; font-size:14px; margin-top:6px; }
  .pr-trail span.pr-label { color:var(--muted); }
  .pr-trail a { text-decoration:none; font-weight:600; }
  .pr-chip { display:inline-block; font-size:11px; font-weight:700; padding:1px 5px;
              border-radius:4px; text-transform:uppercase; }
  .pr-chip.pr-merged { color:var(--ok); background:var(--ok-tint); }
  .pr-chip.pr-open { color:var(--warn); background:var(--warn-tint); }
  .pr-chip.pr-declined { color:var(--fail); background:var(--warn-tint); }
  .pr-chip.pr-closed { color:var(--dim); background:var(--info-tint); }
  .recent-card { background:#fff; border:1px solid var(--line); border-radius:8px;
                  padding:14px 18px; margin-bottom:10px;
                  transition: border-color 0.15s, box-shadow 0.15s; }
  .recent-card:hover { border-color:var(--accent); box-shadow:0 2px 8px rgba(0,0,0,0.08); }
  .recent-top { display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .recent-badge { display:inline-block; font-size:11px; font-weight:700; padding:2px 8px;
                   border-radius:999px; text-transform:uppercase; letter-spacing:0.3px; color:#fff;
                   flex-shrink:0; }
  .recent-badge.post { background:var(--accent); }
  .recent-badge.proposal { background:var(--accent); }
  .recent-badge.small-fix { background:var(--warn); color:#0f172a; }
  .recent-badge.comment { background:var(--ok); }
  .recent-badge.vote { background:var(--dim); color:#0f172a; }
  .recent-body { margin:6px 0 4px; font-size:17px; }
  .recent-body a { color:var(--accent); text-decoration:none; font-weight:600; }
  .recent-body a:hover { text-decoration:underline; }
  .recent-meta { display:flex; gap:12px; align-items:center; flex-wrap:wrap;
                  font-size:14px; color:var(--muted); }
  .recent-preview { color:var(--muted); font-size:15px; margin:6px 0 2px;
                     padding:6px 10px; border-left:3px solid var(--line);
                     background:var(--info-tint); border-radius:0 6px 6px 0; }
  .recent-day-divider { display:flex; align-items:center; gap:12px;
                         margin:18px 0 10px; font-size:13px; font-weight:600;
                         color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; }
  .recent-day-divider::before, .recent-day-divider::after { content:""; flex:1;
                         border-top:1px solid var(--line); }
  .breadcrumb { font-size:17px; margin-bottom:12px; }
  .breadcrumb a { color:var(--accent); text-decoration:none; }
  .breadcrumb a:hover { text-decoration:underline; }
  .rail-item { padding:8px 0; border-bottom:1px solid var(--line); }
  .rail-item:last-child { border-bottom:none; }
  .rail-item a { color:var(--ink); text-decoration:none; font-weight:600; }
  .rail-item a:hover { color:var(--accent); text-decoration:underline; }
  .rail-meta { display:block; color:var(--muted); font-size:15px; margin-top:2px; }
  .tag { display:inline-block; background:#e6fffa; color:#2f855a; border:1px solid #9ae6b4;
         border-radius:4px; padding:0 6px; font-size:14px; font-weight:600; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
  .dot.ok { background:#38a169; }
  .dot.fail { background:#e53e3e; }
  .dot.warn { background:#d69e2e; }
  .status-ok { color:#2f855a; font-weight:600; }
  .status-fail { color:#c53030; font-weight:600; }
  .status-warn { color:#b7791f; font-weight:600; }
  .kv th { width:260px; }
  .about p { margin:8px 0; }
  .about a { color:var(--accent); text-decoration:none; }
  pre { white-space:pre-wrap; font-family:inherit; margin:0; }
  pre.diff { font-family:ui-monospace,Consolas,Menlo,monospace; font-size:14px;
              background:#f7fafc; border:1px solid var(--line); border-radius:6px;
              padding:10px 12px; overflow-x:auto; }
  footer { color:var(--muted); font-size:15px; text-align:center; padding:24px 0; }
  .jumpnav { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }
  .jumpnav a { color:var(--accent); text-decoration:none; font-size:15px;
               border:1px solid var(--line); padding:3px 10px; border-radius:999px; background:#fff; }
  .jumpnav a:hover { border-color:var(--accent); }
  .record-toc { position:sticky; top:64px; z-index:5; max-height:calc(100vh - 90px);
                overflow-y:auto; display:flex; flex-direction:column; gap:2px;
                align-items:flex-start; border:1px solid var(--line); border-radius:8px;
                background:#fff; padding:10px 12px; margin:0 0 14px; }
  .record-toc a { color:var(--accent); text-decoration:none; font-size:14px;
                  padding:2px 0; max-width:100%; }
  .record-toc a:hover { text-decoration:underline; }
  .record-toc a.toc-3 { padding-left:16px; }
  .record-toc a.toc-4 { padding-left:32px; }
  h2[id], h3[id], h4[id] { scroll-margin-top:72px; }
  .votes-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  .votes-grid h3 { font-size:16px; margin:0 0 6px; }
  .search-group { margin:0 0 14px; }
  .search-group h3 { font-size:17px; margin:0 0 6px; color:var(--ink); }
  .stake-row { padding:10px 0; border-bottom:1px solid var(--border); }
  .stake-row:last-child { border-bottom:none; }
  .stake-row-top { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:4px; }
  .stake-badge { font-size:12px; padding:1px 8px; border-radius:4px; font-weight:600; }
  .stake-active { background:var(--ok-tint); color:var(--ok); border:1px solid var(--ok-border); }
  .stake-withdrawn { background:var(--info-tint); color:var(--muted); border:1px solid var(--info-border); }
  .stake-refunded { background:var(--warn-tint); color:var(--warn); border:1px solid var(--warn-border); }
  .stake-completed { background:var(--ok-tint); color:var(--ok); border:1px solid var(--ok-border); }
  .stake-staker { color:var(--muted); }
  .stake-amount { color:var(--ink); font-size:15px; }
  .stake-proposal-link { color:var(--accent); font-weight:600; text-decoration:none; }
  .stake-proposal-link:hover { text-decoration:underline; }
  .stake-bar { margin-top:4px; display:flex; align-items:center; gap:8px; }
  .stake-bar-track { flex:1; max-width:200px; height:6px; background:var(--line); border-radius:3px; overflow:hidden; }
  .stake-bar-fill { height:100%; background:var(--ok); border-radius:3px; }
  .stake-bar-label { color:var(--muted); font-size:13px; }
  .stake-row-detail { color:var(--muted); font-size:14px; margin-top:2px; }
  .bug-body { margin:14px 0; padding:14px; background:#fff; border:1px solid var(--line); border-radius:8px; }
  .bug-conf-track { background:#e2e8f0; border-radius:4px; height:8px; width:200px; display:inline-block; }
  .todo-id { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
             font-size:12px; color:var(--dim); margin-right:.35rem; }
  th:not(.sort-on) a { position:relative; padding-right:18px; }
  th:not(.sort-on) a::after { content: " ⇅"; font-size:12px; opacity:0.4; }
  th:not(.sort-on) a:hover::after { opacity:1; }
  @media (max-width: 900px) { .grid { grid-template-columns:1fr; } .votes-grid { grid-template-columns:1fr; } }
  @media (max-width: 600px) { .post-top { flex-direction:column; } .post-stats { padding-top:0; } .docket-top { flex-direction:column; } }
  @media (prefers-color-scheme: dark) {
    :root { --ink:#f1f5f9; --muted:#94a3b8; --line:#334155; --accent:#38bdf8;
             --ok:#34d399; --fail:#f87171; --warn:#fbbf24; --dim:#a0aec0;
             --ok-tint:#064e3b; --warn-tint:#451a03; --info-tint:#1e293b;
             --ok-border:#065f46; --warn-border:#92400e; --info-border:var(--line);
             --banner-ok:#34d399; --banner-fail:#f87171; --banner-warn:#fbbf24;
             --border:#334155; --accent-tint:#0c4a6e; --accent-border:#0284c7; }
    body { background:#0f172a; color:var(--ink); }
    header { background:#1e293b; border-color:var(--line); box-shadow:0 1px 3px rgba(0,0,0,.3); }
    nav a { background:#1e293b; border-color:var(--line); color:var(--accent); }
    nav a:hover { background:#334155; border-color:var(--accent); }
    nav a.active { color:#0f172a; background:var(--accent); border-color:var(--accent); }
    nav input, .top-search input { background:#1e293b; border-color:var(--line); color:var(--ink); }
    nav details.nav-dropdown > summary { background:#1e293b; border-color:var(--line); color:var(--accent); }
    nav details.nav-dropdown > summary:hover { background:#334155; border-color:var(--accent); }
    nav details.nav-dropdown > summary.active { color:#0f172a; background:var(--accent); border-color:var(--accent); }
    nav details.nav-dropdown > summary.active::after { color:#0f172a; }
    nav details.nav-dropdown .nav-dropdown-items { background:#1e293b; border-color:var(--line);
      box-shadow:0 6px 16px rgba(0,0,0,.4); }
    nav details.nav-dropdown .nav-dropdown-items a:hover { background:#334155; }
    nav details.nav-dropdown .nav-dropdown-items a.active { color:#0f172a; background:var(--accent); }
    nav input { background:#1e293b; border-color:var(--line); color:var(--ink); }
    button { color:var(--accent); background:#1e293b; border-color:var(--line); }
    button:hover { border-color:var(--accent); background:#334155; }
    button:active { background:#1e3a5f; }
    .utc-pill { background:#1e293b; border-color:var(--line); }
    .utc-pill #utc-reset-count { color:var(--ink); }
    .card { background:#1e293b; border-color:var(--line); }
    .panel { background:#1e293b; border-color:var(--line); }
    .record-toc { background:#1e293b; border-color:var(--line); }
    .post { background:#1e293b; border-color:var(--line); }
    .post:hover { box-shadow:0 2px 8px rgba(0,0,0,0.3); }
    .post h3 a { color:var(--ink); }
    .post h3 a:hover { color:var(--accent); }
    .rail-item { border-color:var(--line); }
    .rail-item a { color:var(--ink); }
    .rail-item a:hover { color:var(--accent); }
    .rail-meta { color:var(--muted); }
    .table-wrap tbody tr:nth-child(even) { background:#243244; }
    .tag { background:#164e63; color:#67e8f9; border-color:#0e7490; }
    .dot.ok { background:#34d399; }
    .dot.fail { background:#f87171; }
    .dot.warn { background:#fbbf24; }
    .status-ok { color:#34d399; }
    .status-fail { color:#f87171; }
    .status-warn { color:#fbbf24; }
    pre.diff { background:#1e293b; border-color:var(--line); }
    .post-body code { background:#334155; }
    .post-body pre { background:#334155; }
    .post-body pre code { background:none; }
    .post-body blockquote { border-color:var(--line); color:var(--muted); }
    .comment:target { background:#1e3a5f; }
    .comment:hover { background:rgba(255,255,255,0.03); }
    footer { color:var(--muted); }
    .jumpnav a { background:#1e293b; border-color:var(--line); color:var(--accent); }
    .jumpnav a:hover { border-color:var(--accent); }
    .kind-proposal { background:var(--accent); color:#0f172a; }
    .kind-smallfix { background:var(--warn); color:#0f172a; }
    .kind-idea { background:#6366f1; color:#0f172a; }
    .verdict-chip { color:#0f172a; }
    .tabs a { background:#1e293b; border-color:var(--line); color:var(--accent); }
    .tabs a:hover { border-color:var(--accent); }
    .tabs a.active { color:#0f172a; background:var(--accent); border-color:var(--accent); }
    .sort-row a:hover { color:var(--accent); }
    .sort-row a.active { color:var(--accent); }
    .docket-card { background:#1e293b; border-color:var(--line); }
    .docket-card:hover { box-shadow:0 2px 8px rgba(0,0,0,0.3); }
    .docket-card h3 a { color:var(--ink); }
    .docket-card h3 a:hover { color:var(--accent); }
    .docket-card.stale-card { border-left-color:var(--warn); }
    .search-group h3 { color:var(--ink); }
    .sort-row .seg { background:#1e293b; }
    .sort-row .seg a:hover { background:#334155; color:var(--accent); }
    .sort-row .seg a.active { color:#0f172a; }
    .stat-comments { color:var(--muted); }
    .post-excerpt { border-left-color:var(--line); }
    .pr-chip.pr-merged { color:#34d399; background:#064e3b; }
    .pr-chip.pr-open { color:#fbbf24; background:#451a03; }
    .pr-chip.pr-declined { color:#f87171; background:#451a03; }
    .pr-chip.pr-closed { color:#a0aec0; background:#1e293b; }
    .recent-card { background:#1e293b; border-color:var(--line); }
    .recent-card:hover { box-shadow:0 2px 8px rgba(0,0,0,0.3); }
    .recent-badge.post { background:#0e7490; }
    .recent-badge.proposal { background:#0e7490; }
    .recent-badge.small-fix { background:#78350f; }
    .recent-badge.comment { background:#064e3b; }
    .recent-badge.vote { background:#334155; }
    .recent-preview { border-left-color:var(--line); }
    .recent-day-divider { border-top-color:var(--line); }
    .bug-body { background:#1e293b; border-color:var(--line); }
    .bug-conf-track { background:#334155; }
    .todo-id { color:#64748b; }
  }
"""

_CSS_HASH = "7D21B4E9C50A6F83"


def static_style_css(request) -> Response:
    return Response(
        STYLE_CSS,
        media_type="text/css",
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "ETag": f'"{_CSS_HASH}"',
        },
    )
