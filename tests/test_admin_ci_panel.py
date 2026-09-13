"""Workspace panel fields on the /admin/ci snapshot (git pool size/fetch-in).

The Git Workspace Pool panel shows per-slot size (cached rglob, like the
CI-trees panel) and fetch TTL / lock timeout knobs plus per-slot seconds
until the next refetch. Direct snapshot calls on a throwaway DATA_DIR -
no server boot, no auth, no network.
"""

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
from server.admin._ci import (
    _ci_dashboard_snapshot,  # noqa: E402
    _render_ci_dashboard,  # noqa: E402
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

    req = SimpleNamespace(
        cookies=SimpleNamespace(get=lambda _key, _default=None: _default),
        state=SimpleNamespace(csrf_token=""),
    )
    html = _render_ci_dashboard(req)
    assert "0.1 reserve" in html, "caption must state the contention reserve"
    assert f"host {cr._host_cpus()}" in html, "caption must surface host cpus"
    for _stale in ("(1.5", "1.33", "down-only when busy"):
        assert _stale not in html, f"stale caption survived render: {_stale!r}"

    print("test_admin_ci_panel: all ok")


if __name__ == "__main__":
    main()
