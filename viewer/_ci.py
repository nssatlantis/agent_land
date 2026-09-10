"""
viewer/_ci.py - CI build health timeline page.

Read-only page for CI runs: /ci with tabs Native vs PR merges vs Local
rehearsals, top strip and timeline. Data comes from events.query_events for
ci_run / ci_branch_run / ci_local_run kinds; no db writes.
"""

from __future__ import annotations

import statistics

from starlette.requests import Request
from starlette.responses import HTMLResponse

import config
from events import (
    bench_anchor_aging,
    bench_anchor_base_for,
    bench_anchor_drifted,
    bench_native_series,
    bench_pct,
    bench_regressions_for,
    event_total,
    query_events,
)
from viewer._cache import _cached
from viewer._layout import _page
from viewer._utils import _human_ts, esc

# (315:4961) stats cache via shared viewer._cache._cached helper.
_STATS_CACHE_NS = ("stats",)
_STATS_TTL = 60.0


def _ci_top_strip(events: list[dict]) -> str:
    """Top strip: N runs / ok% / avg duration / timeout rate.

    Degrades to muted dashes when no events match the current filter.
    """
    total = len(events)
    if total == 0:
        return (
            '<div style="display:flex;gap:12px;flex-wrap:wrap;margin:8px 0">'
            '<span style="color:var(--muted)">No runs yet</span></div>'
        )
    ok_n = 0
    dur_sum = 0.0
    dur_cnt = 0
    timeout_n = 0
    for e in events:
        d = e.get("detail") or {}
        ok = d.get("ok")
        if ok is True:
            ok_n += 1
        elif ok is None and d.get("exit_code") == 0:
            ok_n += 1
        dur = d.get("duration_seconds")
        if isinstance(dur, (int, float)):
            dur_sum += float(dur)
            dur_cnt += 1
        if d.get("timed_out"):
            timeout_n += 1
    ok_pct = int((ok_n / total) * 100) if total else 0
    avg_dur = (dur_sum / dur_cnt) if dur_cnt else 0
    return (
        '<div style="display:flex;gap:12px;flex-wrap:wrap;margin:8px 0">'
        f"<span><b>{total}</b> runs</span> · "
        f"<span>{ok_pct}% ok</span> · "
        f"<span>avg {avg_dur:.1f}s</span> · "
        f"<span>{timeout_n} timeouts</span>"
        "</div>"
    )


def _ci_badge(detail: dict) -> str:
    """Badge for a single CI run: success / failure / timeout."""
    if detail.get("timed_out"):
        return '<span class="kind-badge" style="background:var(--warn);color:white">timeout</span>'
    ok = detail.get("ok")
    if ok is True or (ok is None and detail.get("exit_code") == 0):
        return '<span class="kind-badge" style="background:var(--ok);color:white">ok</span>'
    if detail.get("merge_conflict"):
        return '<span class="kind-badge" style="background:var(--warn);color:white">conflict</span>'
    return '<span class="kind-badge" style="background:var(--fail);color:white">fail</span>'


def _ci_row(e: dict) -> str:
    """One timeline row: when|mode|sha7→/prs/{n}|badge|duration|failed_files with <details>."""
    detail = e.get("detail") or {}
    when = _human_ts(e["created_at"])
    mode = esc(str(detail.get("mode") or e.get("kind") or "native"))
    head_sha = str(detail.get("head_sha") or detail.get("base_sha") or "")
    sha7 = esc(head_sha[:7]) if head_sha else "—"
    pr_number = detail.get("pr_number")
    if pr_number:
        sha_html = f'<a href="/prs/{int(pr_number)}" style="color:var(--accent)">{sha7} → #{int(pr_number)}</a>'
    elif head_sha:
        sha_html = esc(sha7)
    else:
        sha_html = '<span style="color:var(--muted)">—</span>'
    badge = _ci_badge(detail)
    dur = detail.get("duration_seconds")
    dur_html = (
        f"{float(dur):.1f}s"
        if isinstance(dur, (int, float))
        else '<span style="color:var(--muted)">—</span>'
    )
    checks = esc(str(detail.get("checks") or ""))
    checks_html = (
        f'<span style="color:var(--muted);font-size:13px">{checks}</span>'
        if checks
        else ""
    )
    failed = detail.get("failed_files") or e.get("failed_files") or []
    if isinstance(failed, str):
        failed = [failed]
    failed_html = ""
    if failed:
        shown = ", ".join(esc(str(f)) for f in list(failed)[:5])
        more = f" +{len(failed) - 5} more" if len(failed) > 5 else ""
        failed_html = (
            f'<div style="font-size:13px;color:var(--fail)">{shown}{more}</div>'
        )
    output_tail = detail.get("output_tail") or detail.get("output") or ""
    tail_html = ""
    if output_tail:
        # truncate the raw text before escaping: cutting the escaped form
        # can split an entity mid-sequence and shortens by escaped length.
        tail_raw = str(output_tail)
        if len(tail_raw) > 4000:
            tail_raw = tail_raw[:4000] + "\n… truncated"
        tail_esc = esc(tail_raw)
        tail_html = f'<details style="margin-top:4px"><summary style="cursor:pointer;color:var(--muted);font-size:13px">output_tail</summary><pre style="max-height:300px;overflow:auto;background:var(--code);padding:8px;border-radius:4px">{tail_esc}</pre></details>'
    return (
        '<div class="row" style="padding:8px 0;border-bottom:1px solid var(--border)">'
        f'<span style="color:var(--muted);font-size:13px">{when}</span> · '
        f'<span style="font-size:13px">{mode}</span> · '
        f"{sha_html} · {badge} · "
        f'<span style="font-size:13px">{dur_html}</span> '
        f"{checks_html}"
        f"{failed_html}"
        f"{tail_html}"
        "</div>"
    )


def _bench_badge(detail: dict) -> str:
    """Badge for a single db_benchmark run: clean (regressions==0) /
    regression. Contrast with _ci_badge: bench 'ok' is the harness exit code,
    but the number that matters is regressions==0."""
    regr = bench_regressions_for([{"detail": detail}])
    if regr:
        return (
            '<span class="kind-badge" style="background:var(--warn);color:white">'
            f"{regr} regress</span>"
        )
    return (
        '<span class="kind-badge" style="background:var(--ok);color:white">clean</span>'
    )


def _bench_anchor_head(anchor: dict | None, rows: list[dict]) -> str:
    """Anchor identity + trailing summary above the Benchmarks table: who
    blessed which run when, whether it is aging, and which queries drifted
    with their trailing series (capped at 10 names). Always a state line -
    the no-anchor fallback is informative, not empty."""
    style = "style='color:var(--muted);font-size:13px;margin:0 0 8px'"
    if anchor is None:
        return (
            f"<p {style}>No anchor blessed — deltas fall back to the newest "
            "native reference (window-best with no reference). Bless one with "
            "the bless_bench_anchor tool.</p>"
        )
    aging, reason = bench_anchor_aging(anchor, rows)
    who = esc(str(anchor.get("blessed_by_name") or "system"))
    head = (
        f"<p {style}>Anchor: ev{anchor.get('bless_event_id')} by {who} "
        f"({esc(_human_ts(str(anchor.get('blessed_at') or '')))})"
    )
    if aging:
        head += f" — AGING: {esc(reason)}"
    drifted = bench_anchor_drifted(anchor, rows)
    if not drifted:
        return head + " — trailing flat vs anchor.</p>"
    series = bench_native_series(rows)
    medians = anchor.get("medians") or {}
    parts = []
    for q in sorted(drifted)[:10]:
        s = series.get(q, [])
        if not s:
            continue
        base = medians.get(q)
        if not isinstance(base, (int, float)) or isinstance(base, bool):
            parts.append(f"{esc(q)}: {', '.join(f'{v:.1f}' for v in s)} vs anchor ?")
            continue
        trailing = statistics.median(s)
        pct = bench_pct(trailing, float(base))
        trail = ", ".join(f"{v:.1f}" for v in s)
        parts.append(f"{esc(q)}: {trail} vs anchor {float(base):.1f} ({pct:+d}%)")
    more = f" (+{len(drifted) - 10} more)" if len(drifted) > 10 else ""
    return head + f" — trailing drift: {'; '.join(parts)}{more}.</p>"


def _bench_row(e: dict, bests: dict[str, float], label: str) -> str:
    """One db_benchmark timeline row: when|mode|sha7|badge|duration plus a
    collapsible per-query median table (median ms + signed Δ% vs the shared
    comparison base - the blessed anchor when one exists, else the newest
    native reference or window-best fallback; negative = faster).
    `bests` and `label` come from events.bench_anchor_base_for (same math as
    the check-in nudge)."""
    detail = e.get("detail") or {}
    when = _human_ts(e["created_at"])
    checks = esc(str(detail.get("checks") or "db_benchmark"))
    head_sha = str(detail.get("head_sha") or "")
    sha7 = esc(head_sha[:7]) if head_sha else "—"
    dur = detail.get("duration_seconds")
    dur_html = f"{float(dur):.1f}s" if isinstance(dur, (int, float)) else "—"
    badge = _bench_badge(detail)
    summary = detail.get("summary") or {}
    meds = summary.get("timings_median_ms")
    rows_html = ""
    if isinstance(meds, dict) and meds:
        cells = []
        for q in sorted(meds):
            latest = meds[q]
            if not isinstance(latest, (int, float)):
                continue
            base = bests.get(str(q))
            pct = bench_pct(latest, base) if base is not None else None
            delta = ""
            if pct is not None:
                col = (
                    "var(--ok)"
                    if pct <= 0
                    else ("var(--warn)" if pct < 20 else "var(--fail)")
                )
                delta = f' <span style="color:{col}">{pct:+d}% {label}</span>'
            else:
                delta = ' <span style="color:var(--muted)">no ref</span>'
            cells.append(
                "<tr>"
                f"<td style='text-align:left'>{esc(str(q))}</td>"
                f"<td style='text-align:right'><b>{latest:.1f} ms</b>{delta}</td>"
                "</tr>"
            )
        table = (
            '<div style="border-top:1px solid var(--border);padding-top:6px;margin-top:4px">'
            '<table style="width:100%;border-collapse:collapse;font-size:13px">'
            "<caption style='text-align:left;color:var(--muted);font-size:12px;padding:2px 0'>"
            f"median ms per query {label} ({len(meds)} queries)</caption>"
            + "".join(cells)
            + "</table></div>"
        )
        rows_html = (
            f'<details style="margin-top:4px"><summary '
            f'style="cursor:pointer;color:var(--muted);font-size:13px">'
            f"per-query medians</summary>{table}</details>"
        )
    return (
        '<div class="row" style="padding:8px 0;border-bottom:1px solid var(--border)">'
        f'<span style="color:var(--muted);font-size:13px">{when}</span> · '
        f'<span style="font-size:13px">{checks}</span> · '
        f'<span style="color:var(--muted)">{sha7}</span> · '
        f"{badge} · {dur_html}"
        f"{rows_html}"
        "</div>"
    )


def ci_page(request: Request) -> HTMLResponse:
    """The /ci page: tabs Native vs PR merges vs Local rehearsals vs
    Benchmarks, ?mode= filter on the ci_run kinds + top strip + timeline."""
    mode = (request.query_params.get("mode") or "native").lower()
    if mode not in ("native", "branch", "local", "bench"):
        if mode in ("pr", "merges", "pr_merges", "branch_run"):
            mode = "branch"
        elif mode in ("rehearsal", "overlay", "local_run"):
            mode = "local"
        elif mode in ("db_bench", "db_benchmark", "benchmark", "benchmarks"):
            mode = "bench"
        else:
            mode = "native"
    kind = {
        "native": "ci_run",
        "branch": "ci_branch_run",
        "local": "ci_local_run",
        "bench": "ci_db_bench_run",
    }[mode]
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    per_page = int(config.CI_PER_PAGE or 50)
    # Wide stats window on the shared _cached helper, 60s TTL per (kind,
    # mode): the first hit fetches the 500-row window and falls back to
    # `event_total` on failure (key left absent, so the next request
    # retries). Holds a (stats_evts, total) tuple; the inner
    # `with_total=True` is the single source of truth.
    _cache_key = (*_STATS_CACHE_NS, kind, mode)
    try:
        stats_evts, total = _cached(
            _cache_key,
            _STATS_TTL,
            lambda: query_events(kind=kind, limit=500, offset=0, with_total=True),
        )
    except Exception:  # noqa: BLE001
        # domain: degrade-silently - stats query failure loses richness, not data
        stats_evts = None
        try:
            total = event_total(kind=kind)
        except (  # domain: degrade-silently - same as above; fall back to 0
            Exception
        ):
            total = 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    if page == 1 and stats_evts is not None:
        # The stats window is a superset of page 1 (same filters,
        # newest-first): slice it instead of running a second query.
        evts = stats_evts[:per_page]
    else:
        offset = (page - 1) * per_page
        evts = query_events(kind=kind, limit=per_page, offset=offset)
        if stats_evts is None:
            stats_evts = evts
    native_cls = "active" if mode == "native" else ""
    branch_cls = "active" if mode == "branch" else ""
    local_cls = "active" if mode == "local" else ""
    bench_cls = "active" if mode == "bench" else ""
    tabs = (
        '<div class="tabs">'
        f'<a href="/ci?mode=native#sec-ci" class="{native_cls}">Native</a>'
        f'<a href="/ci?mode=branch#sec-ci" class="{branch_cls}">PR merges</a>'
        f'<a href="/ci?mode=local#sec-ci" class="{local_cls}">Local</a>'
        f'<a href="/ci?mode=bench#sec-ci" class="{bench_cls}">Benchmarks</a>'
        "</div>"
    )
    top_strip = _ci_top_strip(stats_evts)

    def _href_for_page(n: int) -> str:
        base = f"/ci?mode={mode}&page={n}" if n > 1 else f"/ci?mode={mode}"
        # Land back on the runs table, not the top of the page.
        return base + "#sec-ci"

    pager = ""
    if total_pages > 1:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        if page > 1:
            nav.insert(0, f'<a href="{esc(_href_for_page(page - 1))}">\u2039 Prev</a>')
        if page < total_pages:
            nav.append(f'<a href="{esc(_href_for_page(page + 1))}">Next \u203a</a>')
        pager = '<div class="pager">' + " \u00b7 ".join(nav) + "</div>"
    empty = "<p style='color:var(--muted)'>No CI runs yet — the runner is idle.</p>"
    bench_head = ""
    if mode == "bench":
        bench_bests, bench_label, bench_anchor = bench_anchor_base_for(stats_evts or [])
        rows_html = (
            "".join(_bench_row(e, bench_bests, bench_label) for e in evts)
            if evts
            else empty
        )
        bench_head = _bench_anchor_head(bench_anchor, stats_evts or [])
    else:
        rows_html = "".join(_ci_row(e) for e in evts) if evts else empty
    summary = f'<p class="meta" style="margin:0 0 8px">Page {page} of {total_pages} · {total} runs</p>'
    hint = ""
    if mode == "branch":
        hint = "<p style='color:var(--muted);font-size:13px'>Branch mode: each run tests the merge of <code>main</code> into the PR head; sha7 links to the PR.</p>"
    elif mode == "local":
        hint = "<p style='color:var(--muted);font-size:13px'>Local mode: <code>repo_ci_run(files=[...])</code> rehearsals — the pre-push overlay of your diff on <code>origin/main</code>, tested in the same Docker sandbox as branch runs (ledger kind <code>ci_local_run</code>).</p>"
    elif mode == "bench":
        hint = "<p style='color:var(--muted);font-size:13px'>Benchmark mode: <code>repo_ci_run(checks='db_benchmark')</code> runs. Each row's median is compared against the blessed anchor (falling back to the newest native origin/main reference run, then window-best) — a negative delta means faster than the anchor; clean = <code>regressions==0</code>.</p>"
    body = (
        '<div class="panel" id="sec-ci"><h2>Build health</h2><p style=\'color:var(--muted);font-size:15px\'>CI runs via the sandboxed runner — native (main), PR merges (branch), local rehearsal (files=) and db_benchmark medians. Each row shows when, mode, head sha, badge, duration and failed files; expand output_tail for logs.</p>'
        + tabs
        + top_strip
        + summary
        + bench_head
        + rows_html
        + pager
        + hint
        + "</div>"
    )
    return _page("CI", body, section="ci")
