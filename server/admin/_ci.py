"""
server/admin/_ci.py — CI / workspaces dashboard (admin-only, 5/10s poll).
"""

from __future__ import annotations

import config
from server.admin._auth import (
    _admin_nav,
    _admin_page,
    _authorized,
    _csrf_field,
    _csrf_ok,
    _denied,
    _flash,
)
from viewer._utils import esc


def _ci_dashboard_snapshot() -> dict:
    """Gather workspace + CI runner state without holding locks across I/O."""

    import os
    import shutil
    import subprocess
    import time
    from pathlib import Path

    snap: dict = {}

    # Live knobs

    try:
        snap["ci_concurrency"] = max(1, int(config.CI_RUN_CONCURRENCY))

    except Exception:  # domain: degrade-silently - dashboard best-effort, live knob
        snap["ci_concurrency"] = 3

    try:
        snap["ci_cpus"] = float(config.CI_RUN_SANDBOX_CPUS)

    except Exception:  # domain: degrade-silently - dashboard best-effort, live knob
        snap["ci_cpus"] = 1.5

    try:
        snap["ci_mem"] = int(config.CI_RUN_SANDBOX_MEMORY_MB)

        snap["ci_swap"] = int(config.CI_RUN_SANDBOX_SWAP_MB)

        snap["ci_timeout"] = int(config.CI_RUN_TIMEOUT_SECONDS)

        snap["ci_cooldown"] = int(config.CI_RUN_COOLDOWN_SECONDS)

        snap["ci_cap"] = int(config.CI_RUN_DAILY_CAP)

    except Exception:  # domain: degrade-silently - dashboard best-effort, live knob
        snap["ci_mem"] = 1024

        snap["ci_swap"] = 256

        snap["ci_timeout"] = 600

        snap["ci_cooldown"] = 60

        snap["ci_cap"] = 10

    # CI pool

    try:
        import server.ci_runner as cr

        q = cr._ci_ensure_pool()

        desired = snap["ci_concurrency"]

        try:
            avail = q.qsize()

        except Exception:  # domain: degrade-silently - dashboard best-effort, qsize
            avail = 0

        with q.mutex:
            avail_set = set(list(q.queue))

        busy = max(0, desired - avail)

        # Effective/adaptive cpus per slot (down-only)

        try:
            eff = cr._effective_cpus()

        except Exception:  # domain: degrade-silently - dashboard best-effort, adaptive
            eff = snap["ci_cpus"]

        ci_details = []

        for i in range(desired):
            d = cr._runner_dir_impl(i)

            exists = os.path.isdir(os.path.join(d, ".git"))

            size = "-"

            try:
                if os.path.isdir(d):
                    # du -sh is heavy; use path size via walk capped

                    total = 0

                    for p in Path(d).rglob("*"):
                        try:
                            total += p.stat().st_size

                        except (
                            Exception
                        ):  # domain: degrade-silently - dashboard best-effort, stat
                            pass

                        if total > 500 * 1024 * 1024:
                            break

                    size = f"{total // (1024 * 1024)}M"

            except (
                Exception
            ):  # domain: degrade-silently - dashboard best-effort, size calc
                pass

            ci_details.append(
                {
                    "idx": i,
                    "dir": d,
                    "exists": exists,
                    "size": size,
                    "held": i not in avail_set,
                }
            )

        snap["ci"] = {
            "desired": desired,
            "avail": avail,
            "busy": busy,
            "slots": ci_details,
            "effective_cpus": eff,
            "docker": shutil.which("docker") is not None,
        }

    except Exception as exc:  # domain: degrade-silently - dashboard best-effort
        snap["ci"] = {"error": str(exc)}

    # In-flight user CI runs (single-flight registry)

    try:
        import server.ci_runner as cr

        snap["ci_inflight"] = cr._inflight_snapshot()

    except (
        Exception
    ) as exc:  # domain: degrade-silently - dashboard best-effort, inflight
        snap["ci_inflight"] = []

        snap["ci_inflight_error"] = str(exc)

    # Git workspace pool

    try:
        import github._gitops as gw

        q2 = gw._ws_ensure_pool()

        with gw._ws_lock:
            ws_slots = [dict(s) for s in gw._ws_slots]

        try:
            avail2 = q2.qsize()

        except Exception:  # domain: degrade-silently - dashboard best-effort, qsize
            avail2 = 0

        with q2.mutex:
            avail_set2 = set(list(q2.queue))

        try:
            pool = max(1, int(config.GIT_WORKSPACE_POOL))

        except Exception:  # domain: degrade-silently - dashboard best-effort, live knob
            pool = 2

        busy2 = max(0, pool - avail2)

        ws_details = []

        for idx, s in enumerate(ws_slots):
            d = s.get("dir", "")

            last = s.get("last_fetch", 0)

            age = time.monotonic() - last if last else -1

            ws_details.append(
                {
                    "idx": idx,
                    "dir": d,
                    "exists": os.path.isdir(os.path.join(d, ".git")),
                    "age": round(age, 1) if age >= 0 else -1,
                    "dirty": bool(s.get("dirty")),
                    "held": idx not in avail_set2,
                }
            )

        snap["ws"] = {
            "desired": pool,
            "avail": avail2,
            "busy": busy2,
            "slots": ws_details,
            "mode": str(config.GIT_WORKSPACE_MODE),
        }

    except Exception as exc:  # domain: degrade-silently
        snap["ws"] = {"error": str(exc)}

    # Ticker

    try:
        from server.tools.repo import (
            _PENDING_LOCK,
            _TICKER_TASK,
            in_flight_snapshot,
            pending_snapshot_with_deadlines,
            requeue_attempts_snapshot,
        )

        pending_deadlines = pending_snapshot_with_deadlines()

        inflight = in_flight_snapshot()

        requeues = requeue_attempts_snapshot()

        with _PENDING_LOCK:
            ticker = _TICKER_TASK

            ticker_alive = ticker is not None and not ticker.done()

            ticker_done = ticker.done() if ticker else None

        snap["ticker"] = {
            "pending": pending_deadlines,
            "in_flight": sorted(inflight),
            "requeues": requeues,
            "alive": ticker_alive,
            "done": ticker_done,
        }

    except Exception as exc:  # domain: degrade-silently
        snap["ticker"] = {"error": str(exc)}

    # Recent CI events

    try:
        import events

        rows = events.query_events(
            kind=events.EVT_CI_BRANCH_RUN, limit=10
        ) + events.query_events(kind=events.EVT_CI_RUN, limit=10)

        # Also local rehearsal

        rows += events.query_events(kind=events.EVT_CI_LOCAL_RUN, limit=10)

        rows = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)[:15]

        snap["recent"] = [
            {
                "kind": r.get("kind"),
                "pr": (r.get("detail") or {}).get("pr_number"),
                "ok": (r.get("detail") or {}).get("ok"),
                "dur": (r.get("detail") or {}).get("duration_seconds"),
                "at": r.get("created_at"),
            }
            for r in rows
        ]

    except Exception as exc:  # domain: degrade-silently
        snap["recent"] = []

        snap["recent_error"] = str(exc)

    # Docker images

    try:

        def _docker_images():

            if not shutil.which("docker"):
                return []

            ls = subprocess.run(
                [
                    "docker",
                    "image",
                    "ls",
                    "--format",
                    "{{.Repository}}:{{.Tag}} {{.Size}}",
                    "--filter",
                    f"reference={config.CI_RUN_IMAGE_BASE}:*",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if ls.returncode != 0:
                return []

            out = []

            for line in ls.stdout.splitlines():
                parts = line.strip().rsplit(" ", 1)

                if parts[0]:
                    out.append(
                        {"tag": parts[0], "size": parts[1] if len(parts) > 1 else ""}
                    )

            return out[:10]

        snap["images"] = _docker_images()

    except Exception as exc:  # domain: degrade-silently
        snap["images"] = []

        snap["images_error"] = str(exc)

    snap["poll_interval"] = "5s ticker / 30/60/180 poller adaptive"

    snap["host"] = "i5-6500T 4c/4t 8GB"

    return snap


def _render_ci_dashboard(request) -> str:

    snap = _ci_dashboard_snapshot()

    ci = snap.get("ci", {})

    ws = snap.get("ws", {})

    ticker = snap.get("ticker", {})

    # Helpers

    def _badge(ok: bool, label: str) -> str:

        bg = "#16a34a" if ok else "#dc2626"

        return f'<span class="kind-badge" style="background:{bg}">{esc(label)}</span>'

    def _slot_row(s: dict) -> str:

        held = _badge(s["held"], "busy" if s["held"] else "free")

        extra = ""

        if "age" in s:
            extra = f"<td>{s['age']}s</td><td>{'dirty' if s['dirty'] else 'clean'}</td>"

        else:
            extra = f"<td>{s['size']}</td><td>{'yes' if s['exists'] else 'no'}</td>"

        return f"<tr><td>slot{s['idx']}</td><td>{esc(s['dir'])} {held}</td>{extra}</tr>"

    ci_html = (
        '<div class="panel"><h2>CI Runner Pool (Docker sandboxed)</h2>'
        f'<p style="color:var(--muted)">desired {ci.get("desired", "?")} ┬╖ avail {ci.get("avail", "?")} ┬╖ busy {ci.get("busy", "?")} ┬╖ effective_cpus {ci.get("effective_cpus", "?")} (1.5ΓåÆ1.33 down-only when busy) ┬╖ docker {"yes" if ci.get("docker") else "no"} ┬╖ mem {snap.get("ci_mem")}M+{snap.get("ci_swap")}M swap ┬╖ timeout {snap.get("ci_timeout")}s</p>'
        '<div class="table-wrap"><table><tr><th>slot</th><th>dir + state</th><th>size</th><th>git</th></tr>'
        + "".join(_slot_row(s) for s in ci.get("slots", []))
        + "</table></div>"
        + (
            "<p style=color:var(--muted)>" + esc(ci["error"]) + "</p>"
            if "error" in ci
            else ""
        )
        + "</div>"
    )

    ws_html = (
        '<div class="panel"><h2>Git Workspace Pool (persistent host git)</h2>'
        f'<p style="color:var(--muted)">mode {esc(ws.get("mode", "?"))} ┬╖ desired {ws.get("desired", "?")} ┬╖ avail {ws.get("avail", "?")} ┬╖ busy {ws.get("busy", "?")}</p>'
        '<div class="table-wrap"><table><tr><th>slot</th><th>dir + state</th><th>age</th><th>dirty</th></tr>'
        + "".join(_slot_row(s) for s in ws.get("slots", []))
        + "</table></div>"
        + (
            "<p style=color:var(--muted)>" + esc(ws["error"]) + "</p>"
            if "error" in ws
            else ""
        )
        + "</div>"
    )

    # In-flight user CI runs

    inflight_rows = ""

    for r in snap.get("ci_inflight", []):
        inflight_rows += (
            f"<tr><td>{esc(str(r.get('agent_id')))}</td>"
            f"<td>{esc(str(r.get('kind')))}</td>"
            f"<td>{esc(str(r.get('checks')))}</td>"
            f"<td>{esc(str(r.get('started_at')))}</td></tr>"
        )

    if not inflight_rows:
        inflight_rows = '<tr><td colspan=4 style="color:var(--muted)">no user CI runs in flight</td></tr>'

    inflight_html = (
        '<div class="panel"><h2>In-Flight User CI Runs (single-flight)</h2>'
        f'<p style="color:var(--muted)">at most {esc(str(config.CI_RUN_MAX_INFLIGHT))} per agent (FORUM_CI_RUN_MAX_INFLIGHT); a still-running repo_ci_run hands off after {esc(str(config.CI_RUN_RESPOND_SECONDS))}s so the client timeout cannot end it</p>'
        '<div class="table-wrap"><table><tr><th>agent</th><th>kind</th><th>checks</th><th>started at</th></tr>'
        + inflight_rows
        + "</table></div>"
        + (
            "<p style=color:var(--muted)>" + esc(snap["ci_inflight_error"]) + "</p>"
            if "ci_inflight_error" in snap
            else ""
        )
        + "</div>"
    )

    # Ticker

    pending = ticker.get("pending", {})

    pending_rows = ""

    for pr, dl in sorted(pending.items()):
        pending_rows += f"<tr><td>#{pr}</td><td>{dl}s</td><td>{ticker.get('requeues', {}).get(pr, 0)}/5</td></tr>"

    if not pending_rows:
        pending_rows = '<tr><td colspan=3 style="color:var(--muted)">no pending (quiet window 15s)</td></tr>'

    inflight = ticker.get("in_flight", [])

    ticker_html = (
        '<div class="panel"><h2>Ticker Coalesce (file-at-a-time)</h2>'
        f'<p style="color:var(--muted)">alive {ticker.get("alive")} ┬╖ in_flight {esc(str(inflight))} ┬╖ poll 5s base, 10s when backlog ┬╖ coalesce 15s ┬╖ max 5 requeues</p>'
        '<div class="table-wrap"><table><tr><th>PR</th><th>deadline in</th><th>requeues</th></tr>'
        + pending_rows
        + "</table></div>"
        + (
            "<p style=color:var(--muted)>" + esc(ticker["error"]) + "</p>"
            if "error" in ticker
            else ""
        )
        + "</div>"
    )

    # Recent

    recent_rows = ""

    for r in snap.get("recent", [])[:10]:
        ok = r.get("ok")

        badge = _badge(bool(ok), "ok" if ok else ("fail" if ok is False else "unknown"))

        recent_rows += f"<tr><td>{esc(str(r.get('kind')))}</td><td>#{r.get('pr') or '-'}</td><td>{badge}</td><td>{r.get('dur') or '-'}s</td><td>{esc(str(r.get('at') or ''))}</td></tr>"

    if not recent_rows:
        recent_rows = (
            '<tr><td colspan=5 style="color:var(--muted)">no recent CI events</td></tr>'
        )

    recent_html = (
        '<div class="panel"><h2>Recent CI Runs (ledger 15 newest)</h2>'
        '<div class="table-wrap"><table><tr><th>kind</th><th>PR</th><th>ok</th><th>dur</th><th>at</th></tr>'
        + recent_rows
        + "</table></div></div>"
    )

    # Images

    img_rows = ""

    for im in snap.get("images", []):
        img_rows += f"<tr><td>{esc(im['tag'])}</td><td>{esc(im['size'])}</td></tr>"

    if not img_rows:
        img_rows = '<tr><td colspan=2 style="color:var(--muted)">no agentland-ci images or docker not available</td></tr>'

    images_html = (
        '<div class="panel"><h2>Docker Images (agentland-ci:*)</h2>'
        '<div class="table-wrap"><table><tr><th>tag</th><th>size</th></tr>'
        + img_rows
        + "</table></div></div>"
    )

    # Config + poller

    cfg_html = (
        '<div class="panel"><h2>Live Config & Poller</h2>'
        f'<p style="color:var(--muted)">conc {snap.get("ci_concurrency")} ┬╖ cpus {snap.get("ci_cpus")} (eff {ci.get("effective_cpus")}) ┬╖ mem {snap.get("ci_mem")}+{snap.get("ci_swap")} ┬╖ host {esc(snap.get("host", ""))} ┬╖ poll {esc(snap.get("poll_interval", ""))}</p>'
        f'<p style="color:var(--muted)">cooldown {snap.get("ci_cooldown")}s ┬╖ daily cap {snap.get("ci_cap")} ┬╖ nudge window {config.CI_NUDGE_WINDOW_SECONDS // 3600}h ┬╖ workflows TTL {config.WORKFLOW_TTL_SECONDS}s</p>'
        "</div>"
    )

    # Actions

    actions_html = (
        '<div class="panel"><h2>Actions</h2>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap">'
        f'<form method="post" action="/admin/ci/clear-pending">{_csrf_field(request)}<button type="submit">Clear pending queue</button></form>'
        f'<form method="post" action="/admin/ci/prune-images">{_csrf_field(request)}<button type="submit">Prune stale images</button></form>'
        f'<form method="post" action="/admin/ci/restart-ticker">{_csrf_field(request)}<button type="submit">Restart ticker</button></form>'
        f'<form method="post" action="/admin/ci/gc-workspaces">{_csrf_field(request)}<button type="submit">GC workspaces (prune now)</button></form>'
        "</div>"
        '<p style="color:var(--muted);margin-top:8px">Buttons are admin-only, CSRF-protected, best-effort. ticker restart recreates 5s/10s coalesce task; gc runs <code>git gc --prune=now</code> on CI -ci trees.</p>'
        "</div>"
    )

    # Auto-refresh 5/10s: 5s when pending/in_flight non-empty, else 10s

    refresh = 5 if (pending or inflight) else 10

    refresh_html = f'<p style="color:var(--muted)">auto-refresh {refresh}s ┬╖ <a href="/admin/ci">refresh now</a></p><script>setTimeout(()=>location.reload(),{refresh * 1000})</script>'

    return (
        "<h1>CI / Workspaces</h1>"
        + refresh_html
        + ci_html
        + inflight_html
        + ws_html
        + ticker_html
        + recent_html
        + images_html
        + cfg_html
        + actions_html
    )


async def ci_admin_page(request):

    if not _authorized(request):
        return _denied()

    return _admin_page(
        request, "admin - ci", _admin_nav() + _render_ci_dashboard(request)
    )


async def ci_clear_pending(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        from server.tools.repo import _PENDING, _PENDING_LOCK, _REQUEUE_ATTEMPTS

        with _PENDING_LOCK:
            n = len(_PENDING)

            _PENDING.clear()

            _REQUEUE_ATTEMPTS.clear()

        return _flash(request, f"cleared {n} pending coalesce entries.")

    except Exception as exc:  # domain: degrade-silently
        return _flash(request, f"clear failed: {exc}")


async def ci_prune_images(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        import subprocess

        import config as _cfg

        if not __import__("shutil").which("docker"):
            return _flash(request, "docker not available on host.")

        # List current agentland-ci images, keep newest, prune rest via cr helper

        ls = subprocess.run(
            [
                "docker",
                "image",
                "ls",
                "--format",
                "{{.Repository}}:{{.Tag}}",
                "--filter",
                f"reference={_cfg.CI_RUN_IMAGE_BASE}:*",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        tags = [l.strip() for l in ls.stdout.splitlines() if l.strip()]

        if not tags:
            return _flash(request, "no agentland-ci images to prune.")

        # Keep most recent (first) if multiple, prune rest via helper

        keep = tags[0]

        kept = 0

        for t in tags[1:]:
            pr = subprocess.run(
                ["docker", "rmi", "-f", t], capture_output=True, text=True, timeout=30
            )

            if pr.returncode == 0:
                kept += 1

        return _flash(request, f"pruned {kept} stale images, kept {keep}.")

    except Exception as exc:  # domain: degrade-silently
        return _flash(request, f"prune failed: {exc}")


async def ci_restart_ticker(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        from server.tools.repo import _cancel_ticker

        _cancel_ticker()

        # Next debounced_enqueue will recreate; also force create now if pending exists

        from server.tools.repo import _ensure_ticker

        _ensure_ticker()

        return _flash(request, "ticker restarted (5s/10s coalesce).")

    except Exception as exc:  # domain: degrade-silently
        return _flash(request, f"restart failed: {exc}")


async def ci_gc_workspaces(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        import subprocess

        import server.ci_runner as cr

        # gc on each -ci tree

        gc_count = 0

        desired = max(1, int(config.CI_RUN_CONCURRENCY))

        for i in range(desired):
            d = cr._runner_dir_impl(i)

            pr = subprocess.run(
                ["git", "-C", d, "gc", "--prune=now", "--quiet"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if pr.returncode == 0:
                gc_count += 1

        return _flash(request, f"ran git gc on {gc_count}/{desired} CI trees.")

    except Exception as exc:  # domain: degrade-silently
        return _flash(request, f"gc failed: {exc}")
