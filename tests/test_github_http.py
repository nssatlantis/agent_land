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


_PR_4242 = {
    "number": 4242,
    "title": "fan-out probe",
    "body": "",
    "head": {"ref": "probe", "sha": "abc123"},
    "base": {"ref": "main"},
    "user": {"login": "someone"},
    "state": "open",
    "created_at": "2026-08-24T00:00:00Z",
    "html_url": "https://github.com/x/y/pull/4242",
}


def test_aget_pr_fans_out_concurrently():
    # Event-gated proof: each of the three wave-2 requests holds until ALL
    # of them have arrived. A sequential chain deadlocks its first request
    # (the gate never opens), so passing this test REQUIRES overlap. The
    # 6s bound keeps a regression a fast failure instead of a hang.
    gh.clear_cache()
    arrived: list[str] = []
    lock = threading.Lock()
    release = asyncio.Event()
    # Concurrency contract: wave 1 is the PR fetch alone (ungated); wave 2
    # overlaps the two comment sources with the files read - THIS gate.
    expected = (
        "/repos/nssatlantis/agent_land/issues/4242/comments",
        "/repos/nssatlantis/agent_land/pulls/4242/comments",
        "/repos/nssatlantis/agent_land/pulls/4242/files",
    )

    async def handler(request):
        path = request.url.path
        with lock:
            arrived.append(path)
            if all(p in arrived for p in expected):
                release.set()
        if path.endswith("/pulls/4242"):
            return httpx.Response(200, json=_PR_4242)
        # Await (not block) the gate: a SYNC handler would freeze the one
        # background-loop thread and starve its own sibling requests.
        try:
            await asyncio.wait_for(release.wait(), timeout=6)
        except asyncio.TimeoutError:
            raise httpx.ConnectError("gate never opened - no wave-2 fan-out", request=request) from None
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    stub_checks = gh._checks_for_head
    gh._checks_for_head = lambda sha: {"state": "unknown", "source": "stub", "runs": []}
    try:
        result = asyncio.run(gh.aget_pr(4242))
        assert result["number"] == 4242 and result["head"] == "probe"
        assert result["comments"] == [] and result["files"] == []
        assert result["checks"]["source"] == "stub"
        assert all(p in arrived for p in expected), arrived
    finally:
        gh._checks_for_head = stub_checks
        gh._client = old
        gh.clear_cache()
    print("  get_pr fans checks/comments/files out concurrently: ok")


def test_aget_pr_cache_and_subcache_parity():
    hits: list[str] = []
    # Rich file entry: the raw GitHub shape carries extra keys (patch,
    # blob_url, ...). If the native path ever writes RAW objects into the
    # shared ("pr_files", n) cache key that sync pr_files fills with the
    # four-field transform, this assertion catches the shape swap.
    raw_file = {
        "filename": "src/app.py",
        "status": "modified",
        "additions": 12,
        "deletions": 3,
        "changes": 15,
        "patch": "@@ -1 +1 @@",
        "blob_url": "https://github.com/x/y/blob/abc/src/app.py",
        "raw_url": "https://github.com/x/y/raw/abc/src/app.py",
    }
    expected_file = {
        "filename": "src/app.py",
        "status": "modified",
        "additions": 12,
        "deletions": 3,
    }

    def handler(request):
        hits.append(request.url.path)
        if request.url.path.endswith("/pulls/4242"):
            return httpx.Response(200, json=_PR_4242)
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[raw_file])
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    stub_checks = gh._checks_for_head
    gh._checks_for_head = lambda sha: {"state": "unknown", "source": "stub"}
    try:
        first = asyncio.run(gh.aget_pr(4242))
        n_after_first = len(hits)
        # aget_pr's files (and the sub-cache it warmed) carry the sync
        # four-field shape - not the raw GitHub objects.
        assert first["files"] == [expected_file], first["files"]
        second = asyncio.run(gh.aget_pr(4242))
        assert second is first or second == first
        assert len(hits) == n_after_first, "cache hit must make zero transport calls"
        # Direct apr_files: same four-field contract from the shared key.
        direct = asyncio.run(gh.apr_files(4242))
        assert direct == [expected_file], direct
        assert len(hits) == n_after_first
        # And the reverse direction: sync pr_files reading whatever the
        # native path warmed must see the transformed shape too.
        assert gh.pr_files(4242) == [expected_file]
        assert len(hits) == n_after_first
    finally:
        gh._checks_for_head = stub_checks
        gh._client = old
        gh.clear_cache()
    print("  aget_pr cache + sub-cache parity with sync path: ok")


def test_gather_error_propagates_as_repo_error():
    def handler(request):
        path, _, _q = str(request.url).partition("?")
        if "/issues/" in path:
            return httpx.Response(500, json={"message": "boom"})
        if path.endswith("/files"):
            return httpx.Response(200, json=[])
        if "/pulls/4243" in path:
            return httpx.Response(200, json=dict(_PR_4242, number=4243))
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    stub_checks = gh._checks_for_head
    gh._checks_for_head = lambda sha: None
    try:
        raised = None
        try:
            asyncio.run(gh.aget_pr(4243))
        except gh.RepoError as exc:
            raised = str(exc)
        assert raised is not None and "boom" in raised, raised
    finally:
        gh._checks_for_head = stub_checks
        gh._client = old
        gh.clear_cache()
    print("  gather failure surfaces as RepoError with body message: ok")


def _install_gated_pair(pair, payloads):
    """Event-gated async mock proving two specific request paths overlap:
    each holds until BOTH have arrived (sequential code deadlocks its first
    request; gathered code passes). Returns the handler to install."""
    arrived: list[str] = []
    lock = threading.Lock()
    release = asyncio.Event()

    async def handler(request):
        path = request.url.path
        with lock:
            arrived.append(path)
            if all(p in arrived for p in pair):
                release.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=6)
        except asyncio.TimeoutError:
            raise httpx.ConnectError("pair gate never opened - no overlap", request=request) from None
        base = payloads.get("pr")
        if base is not None and path.endswith(f"/pulls/{base['number']}"):
            return httpx.Response(200, json=base)
        return httpx.Response(200, json=payloads.get(path, []))

    return handler


def test_apr_diff_overlaps_payload_with_first_page():
    gh.clear_cache()
    pr_payload = dict(_PR_4242, number=5151)
    pair = ("/repos/nssatlantis/agent_land/pulls/5151",
            "/repos/nssatlantis/agent_land/pulls/5151/files")
    payloads = {
        "pr": pr_payload,
        pair[1]: [{"filename": "f.py", "additions": 3}],
    }
    handler = _install_gated_pair(pair, payloads)
    old = _install_mock(handler)
    try:
        diff = asyncio.run(gh.apr_diff(5151))
        assert diff["title"] == "fan-out probe"
        assert diff["files"] == [{"path": "f.py", "status": None, "additions": 3,
                                  "deletions": 0, "changes": 0, "patch": None}]
    finally:
        gh._client = old
        gh.clear_cache()
    print("  apr_diff overlaps payload with first files page: ok")


def test_apr_commits_overlaps_payload_with_first_page():
    gh.clear_cache()
    pr_payload = dict(_PR_4242, number=6161)
    pair = ("/repos/nssatlantis/agent_land/pulls/6161",
            "/repos/nssatlantis/agent_land/pulls/6161/commits")
    payloads = {
        "pr": pr_payload,
        pair[1]: [{"sha": "deadbeef", "commit": {"message": "m",
                   "author": {"name": "n", "date": "2026-08-24T00:00:00Z"}}}],
    }
    handler = _install_gated_pair(pair, payloads)
    old = _install_mock(handler)
    try:
        result = asyncio.run(gh.apr_commits(6161))
        assert result["number"] == 6161 and result["head"] == "probe"
        assert result["commits"] == [{
            "sha": "deadbeef", "message": "m",
            "author_name": "n", "author_date": "2026-08-24T00:00:00Z",
        }]
    finally:
        gh._client = old
        gh.clear_cache()
    print("  apr_commits overlaps payload with first commits page: ok")


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
    test_aget_pr_fans_out_concurrently()
    test_aget_pr_cache_and_subcache_parity()
    test_gather_error_propagates_as_repo_error()
    test_apr_diff_overlaps_payload_with_first_page()
    test_apr_commits_overlaps_payload_with_first_page()
    test_pr_files_paginates_past_the_default_page()
    test_short_first_page_costs_one_request()
    test_pagination_cap_bounds_runaway_servers()
    test_request_text_follows_redirect_to_blob()
    test_supplement_enriches_thin_exit_code_annotations()
    print("test_github_http: all ok")
    return 0



def _serve_pages(hits, path_suffix, pages):
    """Route list-endpoint requests under /pulls/ to canned pages keyed by
    the page= query param; anything else gets an empty list."""
    def handler(request):
        url = str(request.url)
        hits.append(url)
        path, _, query = url.partition("?")
        if "/pulls/" in path and path.rstrip("/").endswith(path_suffix):
            n = 1
            for part in query.split("&"):
                if part.startswith("page="):
                    n = int(part[len("page="):])
            return httpx.Response(
                200, json=pages[n - 1] if n <= len(pages) else []
            )
        return httpx.Response(200, json=[])
    return handler


def test_pr_files_paginates_past_the_default_page():
    hits: list[str] = []
    page1 = [{"filename": f"a{i}.py", "status": "modified", "additions": 1,
              "deletions": 0, "patch": "x"} for i in range(100)]
    page2 = [{"filename": "b7.py", "status": "added", "additions": 9,
              "deletions": 2}]
    handler = _serve_pages(hits, "/files", [page1, page2])
    old = _install_mock(handler)

    def qpage(u):
        for part in u.partition("?")[2].split("&"):
            if part.startswith("page="):
                return int(part[len("page="):])
        return None

    try:
        got = gh.pr_files(4244)
        assert [f["filename"] for f in got[:3]] == ["a0.py", "a1.py", "a2.py"]
        assert got[-1]["filename"] == "b7.py"
        assert len(got) == 101
        assert [qpage(u) for u in hits] == [1, 2], hits
        assert any("per_page=100" in u for u in hits), hits
        # Native twin: same aggregated result through the shared key.
        native = asyncio.run(gh.apr_files(4245))
        assert len(native) == 101 and native[-1]["filename"] == "b7.py"
    finally:
        gh._client = old
        gh.clear_cache()
    print("  pr_files paginates past the default 30/100-item page: ok")


def test_short_first_page_costs_one_request():
    hits: list[str] = []
    handler = _serve_pages(
        hits, "/comments",
        [[{"id": 1, "user": {"login": "a"}, "body": "hi",
           "created_at": "2026-08-24T00:00:00Z"}]],
    )
    old = _install_mock(handler)
    try:
        got = gh.pr_comments(4246)
        assert len(got) == 1 and got[0]["kind"] == "review"
        assert len([u for u in hits if "issues/4246" in u]) == 1
        assert not any("page=2" in u for u in hits), hits
    finally:
        gh._client = old
        gh.clear_cache()
    print("  short first page terminates pagination after one request: ok")


def test_pagination_cap_bounds_runaway_servers():
    hits: list[str] = []
    handler = _serve_pages(
        hits, "/files",
        [[{"filename": "x.py", "status": "modified"}] * 100] * 500,
    )
    saved_cap = gh._PR_PAGE_CAP
    gh._PR_PAGE_CAP = 3
    old = _install_mock(handler)
    try:
        got = gh.pr_files(4247)
        assert len(got) == 300, len(got)
        assert len([u for u in hits if "pr_files" in u or "/files" in u]) == 3
    finally:
        gh._PR_PAGE_CAP = saved_cap
        gh._client = old
        gh.clear_cache()
    print("  page cap bounds a server that never sends a short page: ok")


def test_request_text_follows_redirect_to_blob():
    """GitHub's job-log endpoint answers 302 -> signed blob URL. The text
    reader must follow it (httpx defaults to NOT following), or the
    Actions log tier can never produce a line."""
    def handler(request):
        url = str(request.url)
        if url.endswith("/actions/jobs/777/logs"):
            return httpx.Response(
                302, headers={"Location": "https://blob.example/log.txt"},
            )
        if url == "https://blob.example/log.txt":
            return httpx.Response(200, text="step ok\nerror: boom\n")
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        text = gh._request_text("GET", "actions/jobs/777/logs")
        assert text is not None and "boom" in text, text
    finally:
        gh._client = old
    print("  _request_text follows the log redirect: ok")


def test_supplement_enriches_thin_exit_code_annotations():
    """A pathed-but-content-free annotation ('exit code 1') must not
    suppress the log-tail supplement: the merged failures carry the real
    assertion lines ahead of the thin annotation."""
    def handler(request):
        url = str(request.url)
        path, _, query = url.partition("?")
        if "/check-runs?" in path or path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if path.endswith("/actions/runs") and "head_sha=" in query:
            return httpx.Response(200, json={"workflow_runs": [{
                "id": 1, "name": "CI", "conclusion": "failure",
                "html_url": "https://ci/run/1",
            }]})
        if path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{
                "id": 9, "name": "test", "conclusion": "failure",
            }]})
        if path.endswith("/logs"):
            return httpx.Response(
                302,
                headers={"Location": "https://blob.example/job9.txt"},
            )
        if url == "https://blob.example/job9.txt":
            body = (
                "2026-08-24T22:49:13Z FAILED: test_tags.py\n"
                "2026-08-24T22:49:13Z "
                "AssertionError: a retired tag's name stays reserved\n"
            )
            return httpx.Response(200, text=body)
        return httpx.Response(200, json={})

    result = {
        "source": "check_runs", "state": "failure",
        "runs": [{"name": "CI", "status": "completed",
                  "conclusion": "failure"}],
        "failures": [{"name": "CI", "path": ".github/workflows/ci.yml",
                      "message": "exit code 1", "line": 30}],
    }
    old = _install_mock(handler)
    try:
        gh._supplement_check_run_failures(result, "deadsha")
        msgs = [f["message"] for f in result["failures"]]
        assert any("AssertionError" in m for m in msgs), msgs
        # Log lines were merged IN FRONT of the thin annotation.
        assert any("AssertionError" in m for m in msgs[:2]), msgs
        assert msgs[-1] == "exit code 1", msgs
    finally:
        gh._client = old
        gh.clear_cache()
    print("  thin 'exit code' annotations get enriched from logs: ok")


if __name__ == "__main__":
    sys.exit(main())
