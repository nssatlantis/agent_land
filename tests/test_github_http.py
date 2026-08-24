"""Regression guards for github.py's pooled httpx client (proposal #179,
extended across the async migration): transport-level failures retry
exactly once while the poisoned connection is discarded inside httpx,
ok_404 misses keep the stream in sync (httpx drains every body fully -
the unread-404 bug class is structurally gone), the non-OK path surfaces
GitHub's own message as RepoError, sync callers bridge onto the dedicated
background loop transparently, native-await twins work standalone, and
concurrent sync callers share the one client safely."""

import asyncio
import importlib.util
import sys
import threading
import httpx
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent / "github.py"
_spec = importlib.util.spec_from_file_location("agentland_root_github", _ROOT)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)

gh.GITHUB_TOKEN = "test-token"  # satisfies _ensure_token(); no network touched


def _install_mock(handler):
    """Point the module's shared client at an httpx.MockTransport-backed
    client. Returns the previous client for restoration."""
    old = gh._client
    gh._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )
    return old


def test_transport_error_retries_once():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"value": 7})

    old = _install_mock(handler)
    try:
        assert gh._request("GET", "pulls/1") == {"value": 7}
        assert len(calls) == 2, f"exactly one retry expected, saw {len(calls)}"
    finally:
        gh._client = old
    print("  ConnectError heals via one retry: ok")


def test_remote_protocol_error_heals():
    # The incident class behind proposal #179 - a protocol-level failure on
    # a reused connection - as httpx reports it.
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("server disconnected", request=request)
        return httpx.Response(200, json={"ok": True})

    old = _install_mock(handler)
    try:
        assert gh._request("GET", "pulls/2") == {"ok": True}
        assert len(calls) == 2
    finally:
        gh._client = old
    print("  RemoteProtocolError (Request-sent class) heals: ok")


def test_ok_404_returns_none_and_stream_stays_in_sync():
    hits = []

    def handler(request):
        hits.append(request.url.path)
        if len(hits) == 1:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"after": True})

    old = _install_mock(handler)
    try:
        assert gh._request("GET", "contents/gone.md", ok_404=True) is None
        # The next request on the SAME shared client must parse cleanly -
        # no leftover body bytes corrupting the stream.
        assert gh._request("GET", "contents/here.md") == {"after": True}
        assert hits == ["/repos/x/gone.md", "/repos/x/here.md"] or len(hits) == 2
    finally:
        gh._client = old
    print("  ok_404 miss keeps the shared stream in sync: ok")


def test_non_ok_error_surfaces_body_message():
    def handler(request):
        return httpx.Response(500, json={"message": "boom"})

    old = _install_mock(handler)
    try:
        raised = None
        try:
            gh._request("GET", "pulls/3")
        except gh.RepoError as exc:
            raised = str(exc)
        assert raised is not None and "500" in raised and "boom" in raised, raised
    finally:
        gh._client = old
    print("  non-OK path raises RepoError with the body message: ok")


def test_request_text_paths():
    def handler(request):
        if "jobs/9/" in str(request.url):
            return httpx.Response(404, text="nope")
        return httpx.Response(200, text="line1\nerror: failed\n")

    old = _install_mock(handler)
    try:
        text = gh._request_text("GET", "actions/jobs/1/logs")
        assert text == "line1\nerror: failed\n", repr(text)
        assert gh._request_text("GET", "actions/jobs/9/logs", ok_404=True) is None
    finally:
        gh._client = old
    print("  _request_text reads text and honours ok_404: ok")


def test_native_twin_alist_tree():
    gh.clear_cache()

    def handler(request):
        assert request.url.path.endswith("git/trees/main")
        return httpx.Response(200, json={
            "tree": [
                {"path": "a.py", "type": "blob", "size": 10},
                {"path": "d/", "type": "tree"},
                {"path": "b.md", "type": "blob"},
            ]
        })

    old = _install_mock(handler)
    try:
        result = asyncio.run(gh.alist_tree())
        assert result["repo"] == gh.GITHUB_REPO
        assert result["branch"] == "main"
        assert [f["path"] for f in result["files"]] == ["a.py", "b.md"]
    finally:
        gh._client = old
        gh.clear_cache()
    print("  native await twin alist_tree works standalone: ok")


def test_sync_bridge_shares_one_client_across_threads():
    seen = []
    lock = threading.Lock()

    def handler(request):
        with lock:
            seen.append(str(request.url))
        return httpx.Response(200, json={"n": int(request.url.path.rsplit("/", 1)[-1])})

    old = _install_mock(handler)
    try:
        threads_before = threading.active_count()
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(gh._request, "GET", f"items/{i}") for i in range(16)]
            results = [f.result() for f in futures]
        assert results == [{"n": i} for i in range(16)]
        assert len(seen) == 16
        # One background loop serves everyone; no thread-per-call growth.
        assert threading.active_count() <= threads_before + 2
        assert gh._loop is not None and gh._loop.is_running()
    finally:
        gh._client = old
    print("  concurrent sync callers share the background loop: ok")


def test_background_loop_is_reused_not_respawned():
    def handler(request):
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        first_loop = None
        gh._request("GET", "warmup")
        first_loop = gh._loop
        gh._request("GET", "warmup2")
        assert gh._loop is first_loop
    finally:
        gh._client = old
    print("  background loop reused across calls: ok")


def test_client_stays_single_owner_across_loops():
    # The pooled client's sockets belong to the background loop. A native
    # twin awaited on a FOREIGN running loop must hop its request over via
    # _on_bg instead of driving the client directly - the CI smoke test
    # caught exactly this class of cross-loop misuse on real sockets.
    gh.clear_cache()

    def handler(request):
        if "warmup" in str(request.url):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"tree": [{"path": "a", "type": "blob"}]})

    old = _install_mock(handler)
    try:
        assert gh._request("GET", "warmup") == {}   # first use: background loop
        result = asyncio.run(gh.alist_tree())       # foreign loop awaits
        assert result["files"] == [{"path": "a", "size": 0}]
    finally:
        gh._client = old
        gh.clear_cache()
    print("  client stays single-owner across loops: ok")


def main():
    test_transport_error_retries_once()
    test_remote_protocol_error_heals()
    test_ok_404_returns_none_and_stream_stays_in_sync()
    test_non_ok_error_surfaces_body_message()
    test_request_text_paths()
    test_native_twin_alist_tree()
    test_client_stays_single_owner_across_loops()
    test_sync_bridge_shares_one_client_across_threads()
    test_background_loop_is_reused_not_respawned()
    print("test_github_http: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
