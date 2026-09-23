"""Workspace panel fields on the /admin/ci snapshot (git pool size/fetch-in).

The Git Workspace Pool panel shows per-slot size (cached rglob, like the
CI-trees panel) and fetch TTL / lock timeout knobs plus per-slot seconds
until the next refetch. Direct snapshot calls on a throwaway DATA_DIR -
no server boot, no auth, no network.
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_admin_ci_panel_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.ci_runner as cr  # noqa: E402
import server.ci_runner._farm as farm  # noqa: E402
from server.admin._auth import _csrf_token  # noqa: E402
from server.admin._ci import (  # noqa: E402
    _ci_dashboard_snapshot,
    _render_ci_dashboard,
    ci_farm_register,
    ci_farm_remove,
)
from tests._setup import config, db, setup  # noqa: E402


def main():
    setup()
    assert db is not None  # facade import sanity
    snap = _ci_dashboard_snapshot()
    ws = snap.get("ws", {})
    assert "error" not in ws, f"ws snapshot failed: {ws.get('error')}"
    assert ws["fetch_ttl"] == int(config.GIT_WORKSPACE_FETCH_TTL)
    assert ws["lock_timeout"] == float(config.GIT_WORKSPACE_LOCK_TIMEOUT)
    assert ws["mode"] == str(config.GIT_WORKSPACE_MODE)
    slots = ws["slots"]
    assert len(slots) == max(1, int(config.GIT_WORKSPACE_POOL))
    for s in slots:
        assert isinstance(s["size"], str) and s["size"].endswith("M"), (
            f"slot size rendered: {s['size']!r}"
        )
        # Fresh pool slots never fetched: fetch_in reads -1 like age.
        assert s["age"] == -1 and s["fetch_in"] == -1, (
            f"fresh slot shows unknown age/fetch: {s}"
        )

    # A recently-fetched slot counts down to the next refetch.
    import github._gitops as gw

    with gw._ws_lock:
        gw._ws_slots[0]["last_fetch"] = time.monotonic()
    try:
        snap2 = _ci_dashboard_snapshot()
        s0 = snap2["ws"]["slots"][0]
        assert 0 <= s0["fetch_in"] <= int(config.GIT_WORKSPACE_FETCH_TTL), (
            f"fetch_in counts down from the TTL: {s0['fetch_in']!r}"
        )
    finally:
        with gw._ws_lock:
            gw._ws_slots[0]["last_fetch"] = 0.0

    # The live CI pool snapshot mirrors host + adaptive cpus, and the
    # rendered caption states the contention reserve rule (ascii).
    ci = snap.get("ci", {})
    assert not ci.get("error"), f"ci snapshot failed: {ci.get('error')}"
    assert ci.get("host_cpus") == cr._host_cpus(), (
        f"snapshot host_cpus must mirror _host_cpus: {ci.get('host_cpus')!r}"
    )
    _ceil = max(1.0, float(config.CI_RUN_SANDBOX_CPUS))
    assert 1.0 <= ci["effective_cpus"] <= _ceil, (
        f"effective_cpus out of bounds: {ci['effective_cpus']!r}"
    )

    # Host-fallback pin: an adaptive-cpus exception must degrade to None (so
    # the caption renders "?") and the config floor, never a false 0.
    _real_host, _real_eff = cr._host_cpus, cr._effective_cpus

    def _boom():
        raise RuntimeError("adaptive probe failed")

    try:
        cr._host_cpus = _boom
        cr._effective_cpus = _boom
        snap3 = _ci_dashboard_snapshot()
        ci3 = snap3.get("ci", {})
        assert not ci3.get("error"), f"fallback snapshot failed: {ci3.get('error')}"
        assert ci3.get("host_cpus") is None, (
            f"host fallback must degrade to None: {ci3.get('host_cpus')!r}"
        )
        assert ci3["effective_cpus"] == float(config.CI_RUN_SANDBOX_CPUS), (
            f"eff fallback must be the config floor: {ci3['effective_cpus']!r}"
        )
    finally:
        cr._host_cpus = _real_host
        cr._effective_cpus = _real_eff

    req = SimpleNamespace(
        cookies=SimpleNamespace(get=lambda _key, _default=None: _default),
        state=SimpleNamespace(csrf_token=""),
    )
    html = _render_ci_dashboard(req)
    assert html.isascii(), "dashboard must render pure ascii (mojibake seps banned)"
    assert "\u252c" not in html and "\u2556" not in html, (
        "box-drawing separator glyphs must not survive render"
    )
    assert "0.1 reserve" in html, "caption must state the contention reserve"
    assert f"host {cr._host_cpus()}" in html, "caption must surface host cpus"
    assert "host ?c" not in html, "healthy-path caption must not show the ? fallback"
    for _stale in ("(1.5", "1.33", "down-only when busy"):
        assert _stale not in html, f"stale caption survived render: {_stale!r}"

    test_farm_panel_renders_and_redacts()
    test_farm_register_remove_handlers()
    test_farm_routes_wired()

    print("test_admin_ci_panel: all ok")


class _StubReq:
    """Minimal admin request: open admin (no ADMIN_PASSWORD), cookie-less
    CSRF minted onto state, async form()."""

    def __init__(self, form):
        self.headers = {}
        self.cookies = {}
        self.state = SimpleNamespace()
        self._form = form

    async def form(self):
        return self._form


def _open_admin():
    saved = os.environ.pop("ADMIN_PASSWORD", None)
    return saved


def _restore_admin(saved):
    if saved is None:
        os.environ.pop("ADMIN_PASSWORD", None)
    else:
        os.environ["ADMIN_PASSWORD"] = saved


def _body(resp):
    return resp.body.decode("utf-8")


def test_farm_panel_renders_and_redacts():
    """The farm panel lists runners with register/remove forms and never
    renders the bearer token (write-only at registration)."""
    row = farm.register_runner("panel-r1", "http://farm1:8731", token="s3cr3t-tok")
    try:
        req = SimpleNamespace(
            cookies=SimpleNamespace(get=lambda _key, _default=None: _default),
            state=SimpleNamespace(csrf_token=""),
        )
        html = _render_ci_dashboard(req)
        assert "panel-r1" in html and "http://farm1:8731" in html
        assert "s3cr3t-tok" not in html, "bearer token must never render"
        assert "/admin/ci/farm-register" in html
        assert "/admin/ci/farm-remove" in html
        assert 'type="password"' in html, "token field must mask input"
        assert "CI_FARM_ENABLED=1" in html, "panel must state the enable step"
    finally:
        farm.remove_runner(row["id"])


def _authed(form):
    """Stub request with a minted CSRF token already in its form."""
    req = _StubReq(dict(form))
    req._form["csrf"] = _csrf_token(req)
    return req


def test_farm_register_remove_handlers():
    """Register/remove handlers: happy paths, duplicates, bad input, CSRF."""
    saved = _open_admin()
    try:
        ok = _authed({"name": "h1", "url": "http://h1:8731", "token": "t1"})
        ok = asyncio.run(ci_farm_register(ok))
        assert "registered runner h1" in _body(ok)
        assert any(r["name"] == "h1" for r in farm.list_runners())

        dup = _authed({"name": "h1", "url": "http://h1:8731", "token": "t1"})
        dup = asyncio.run(ci_farm_register(dup))
        assert "register failed" in _body(dup), "duplicate must fail closed"

        missing = asyncio.run(ci_farm_register(_authed({"name": "x"})))
        assert "all required" in _body(missing)

        badurl = _authed({"name": "x", "url": "gopher://x", "token": "t"})
        badurl = asyncio.run(ci_farm_register(badurl))
        assert "http://" in _body(badurl)

        nocsrf = asyncio.run(
            ci_farm_register(_StubReq({"name": "x", "url": "http://x", "token": "t"}))
        )
        assert "CSRF" in _body(nocsrf)

        rid = next(r["id"] for r in farm.list_runners() if r["name"] == "h1")
        gone = asyncio.run(ci_farm_remove(_authed({"runner_id": str(rid)})))
        assert f"removed runner {rid}" in _body(gone)
        assert all(r["id"] != rid for r in farm.list_runners())

        again = asyncio.run(ci_farm_remove(_authed({"runner_id": str(rid)})))
        assert "already gone" in _body(again)

        bad = asyncio.run(ci_farm_remove(_authed({"runner_id": "nope"})))
        assert "invalid runner id" in _body(bad)
    finally:
        for r in farm.list_runners():
            if r["name"] in ("h1", "x"):
                farm.remove_runner(r["id"])
        _restore_admin(saved)


def test_farm_routes_wired():
    """Both farm actions are POST routes on the admin router."""
    from server.admin import ROUTES

    found = {
        r.path: set(r.methods or [])
        for r in ROUTES
        if getattr(r, "path", "").startswith("/admin/ci/farm-")
    }
    assert found.get("/admin/ci/farm-register", set()) == {"POST"}, found
    assert found.get("/admin/ci/farm-remove", set()) == {"POST"}, found


if __name__ == "__main__":
    main()
