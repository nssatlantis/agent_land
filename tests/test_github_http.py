"""Regression guards for the github package's pooled httpx client (proposal #179,
extended across the async migration): transport-level failures retry
exactly once while the poisoned connection is discarded inside httpx,
ok_404 misses keep the stream in sync (httpx drains every body fully -
the unread-404 bug class is structurally gone), the non-OK path surfaces
GitHub's own message as RepoError, sync callers bridge onto the dedicated
background loop transparently, native-await twins work standalone, and
concurrent sync callers share the one client safely."""

import asyncio
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github as gh  # noqa: E402
import github._checks as gh_checks  # noqa: E402
import github._core as gh_core  # noqa: E402
import github._reads as gh_reads  # noqa: E402

gh_core.GITHUB_TOKEN = "test-token"  # satisfies _ensure_token(); no network touched


def _install_mock(handler):
    """Point the module's shared client at an httpx.MockTransport-backed
    client. Returns the previous client for restoration."""
    old = gh_core._client
    gh_core._client = httpx.AsyncClient(
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
        assert gh_core._request("GET", "pulls/1") == {"value": 7}
        assert len(calls) == 2, f"exactly one retry expected, saw {len(calls)}"
    finally:
        gh_core._client = old
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
        assert gh_core._request("GET", "pulls/2") == {"ok": True}
        assert len(calls) == 2
    finally:
        gh_core._client = old
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
        assert gh_core._request("GET", "contents/gone.md", ok_404=True) is None
        # The next request on the SAME shared client must parse cleanly -
        # no leftover body bytes corrupting the stream.
        assert gh_core._request("GET", "contents/here.md") == {"after": True}
        assert hits == ["/repos/x/gone.md", "/repos/x/here.md"] or len(hits) == 2
    finally:
        gh_core._client = old
    print("  ok_404 miss keeps the shared stream in sync: ok")


def test_non_ok_error_surfaces_body_message():
    def handler(request):
        return httpx.Response(500, json={"message": "boom"})

    old = _install_mock(handler)
    try:
        raised = None
        try:
            gh_core._request("GET", "pulls/3")
        except gh.RepoError as exc:
            raised = str(exc)
        assert raised is not None and "500" in raised and "boom" in raised, raised
    finally:
        gh_core._client = old
    print("  non-OK path raises RepoError with the body message: ok")


def test_request_text_paths():
    def handler(request):
        if "jobs/9/" in str(request.url):
            return httpx.Response(404, text="nope")
        return httpx.Response(200, text="line1\nerror: failed\n")

    old = _install_mock(handler)
    try:
        text = gh_core._request_text("GET", "actions/jobs/1/logs")
        assert text == "line1\nerror: failed\n", repr(text)
        assert gh_core._request_text("GET", "actions/jobs/9/logs", ok_404=True) is None
    finally:
        gh_core._client = old
    print("  _request_text reads text and honours ok_404: ok")


def test_native_twin_alist_tree():
    gh.clear_cache()

    def handler(request):
        assert request.url.path.endswith("git/trees/main")
        return httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "a.py", "type": "blob", "size": 10},
                    {"path": "d/", "type": "tree"},
                    {"path": "b.md", "type": "blob"},
                ],
                "truncated": True,
            },
        )

    old = _install_mock(handler)
    try:
        result = asyncio.run(gh.alist_tree())
        assert result["repo"] == gh.GITHUB_REPO
        assert result["branch"] == "main"
        assert [f["path"] for f in result["files"]] == ["a.py", "b.md"]
        assert result["truncated"] is True, "GitHub's truncated flag is surfaced"
    finally:
        gh_core._client = old
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
            futures = [
                pool.submit(gh_core._request, "GET", f"items/{i}") for i in range(16)
            ]
            results = [f.result() for f in futures]
        assert results == [{"n": i} for i in range(16)]
        assert len(seen) == 16
        # One background loop serves everyone; no thread-per-call growth.
        assert threading.active_count() <= threads_before + 2
        assert gh_core._loop is not None and gh_core._loop.is_running()
    finally:
        gh_core._client = old
    print("  concurrent sync callers share the background loop: ok")


def test_background_loop_is_reused_not_respawned():
    def handler(request):
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        first_loop = None
        gh_core._request("GET", "warmup")
        first_loop = gh_core._loop
        gh_core._request("GET", "warmup2")
        assert gh_core._loop is first_loop
    finally:
        gh_core._client = old
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
        assert gh_core._request("GET", "warmup") == {}  # first use: background loop
        result = asyncio.run(gh.alist_tree())  # foreign loop awaits
        assert result["files"] == [{"path": "a", "size": 0}]
    finally:
        gh_core._client = old
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
            raise httpx.ConnectError(
                "gate never opened - no wave-2 fan-out", request=request
            ) from None
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    stub_checks = gh_checks._checks_for_head
    gh_checks._checks_for_head = lambda sha: {
        "state": "unknown",
        "source": "stub",
        "runs": [],
    }
    try:
        result = asyncio.run(gh.aget_pr(4242))
        assert result["number"] == 4242 and result["head"] == "probe"
        assert result["comments"] == [] and result["files"] == []
        assert result["checks"]["source"] == "stub"
        assert all(p in arrived for p in expected), arrived
    finally:
        gh_checks._checks_for_head = stub_checks
        gh_core._client = old
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
    stub_checks = gh_checks._checks_for_head
    gh_checks._checks_for_head = lambda sha: {"state": "unknown", "source": "stub"}
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
        gh_checks._checks_for_head = stub_checks
        gh_core._client = old
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
    stub_checks = gh_checks._checks_for_head
    gh_checks._checks_for_head = lambda sha: None
    try:
        raised = None
        try:
            asyncio.run(gh.aget_pr(4243))
        except gh.RepoError as exc:
            raised = str(exc)
        assert raised is not None and "boom" in raised, raised
    finally:
        gh_checks._checks_for_head = stub_checks
        gh_core._client = old
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
            raise httpx.ConnectError(
                "pair gate never opened - no overlap", request=request
            ) from None
        base = payloads.get("pr")
        if base is not None and path.endswith(f"/pulls/{base['number']}"):
            return httpx.Response(200, json=base)
        return httpx.Response(200, json=payloads.get(path, []))

    return handler


def test_apr_diff_overlaps_payload_with_first_page():
    gh.clear_cache()
    pr_payload = dict(_PR_4242, number=5151)
    pair = (
        "/repos/nssatlantis/agent_land/pulls/5151",
        "/repos/nssatlantis/agent_land/pulls/5151/files",
    )
    payloads = {
        "pr": pr_payload,
        pair[1]: [{"filename": "f.py", "additions": 3}],
    }
    handler = _install_gated_pair(pair, payloads)
    old = _install_mock(handler)
    try:
        diff = asyncio.run(gh.apr_diff(5151))
        assert diff["title"] == "fan-out probe"
        assert diff["files"] == [
            {
                "path": "f.py",
                "status": None,
                "additions": 3,
                "deletions": 0,
                "changes": 0,
                "patch": None,
            }
        ]
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_diff overlaps payload with first files page: ok")


def test_apr_commits_overlaps_payload_with_first_page():
    gh.clear_cache()
    pr_payload = dict(_PR_4242, number=6161)
    pair = (
        "/repos/nssatlantis/agent_land/pulls/6161",
        "/repos/nssatlantis/agent_land/pulls/6161/commits",
    )
    payloads = {
        "pr": pr_payload,
        pair[1]: [
            {
                "sha": "deadbeef",
                "commit": {
                    "message": "m",
                    "author": {"name": "n", "date": "2026-08-24T00:00:00Z"},
                },
            }
        ],
    }
    handler = _install_gated_pair(pair, payloads)
    old = _install_mock(handler)
    try:
        result = asyncio.run(gh.apr_commits(6161))
        assert result["number"] == 6161 and result["head"] == "probe"
        assert result["commits"] == [
            {
                "sha": "deadbeef",
                "message": "m",
                "author_name": "n",
                "author_date": "2026-08-24T00:00:00Z",
            }
        ]
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_commits overlaps payload with first commits page: ok")


def test_ingress_collapse_end_to_end():
    """Ingress collapse survives the full `_checks_for_head` read path: a
    failing check-run annotation with newlines is collapsed at fetch time,
    and `_checks_for_head` returns the collapsed message and path."""

    def handler(request):
        path, _, _query = str(request.url).partition("?")
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "id": 77,
                            "status": "completed",
                            "conclusion": "failure",
                            "name": "test",
                            "head_sha": "deadsha",
                        }
                    ]
                },
            )
        if path.endswith("/annotations"):
            return httpx.Response(
                200,
                json=[
                    {
                        "path": "tests/\nbad.py",
                        "message": "AssertionError:\nexpected 1 == 2",
                        "annotation_level": "failure",
                    }
                ],
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        result = gh_checks._checks_for_head("deadsha")
        assert result["state"] == "failure", result
        assert result["failures"], result
        first = result["failures"][0]
        assert first["message"] == "AssertionError: expected 1 == 2", first
        assert first["path"] == "tests/ bad.py", first
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  ingress collapse end-to-end through _checks_for_head: ok")


def test_apr_checks_statuses_tier_collapses_description():
    """The async statuses tier collapses multi-line status descriptions the
    same way the sync tier does - the last ingress tier, so the
    untrusted-text class stays closed on the native path too."""

    def handler(request):
        path, _, _query = str(request.url).partition("?")
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": []})
        if path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "state": "failure",
                    "statuses": [
                        {
                            "context": "ci",
                            "state": "failure",
                            "description": "line one\r\nline two",
                            "target_url": "https://blob.example/status.txt",
                        }
                    ],
                },
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        result = asyncio.run(gh.apr_checks(4246, _head_sha="deadsha"))
        assert result["source"] == "statuses", result["source"]
        assert result["state"] == "failure", result["state"]
        msgs = [f["message"] for f in result["failures"]]
        assert msgs == ["line one line two"], msgs
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  async statuses tier collapses multi-line descriptions: ok")


# ---------------------------------------------------------------------------
# _group_failures_by_file unit tests
# ---------------------------------------------------------------------------


def test_group_failures_by_file_check_runs():
    failures = [
        {"name": "CI", "path": "tests/test_a.py", "message": "AssertionError: x"},
        {"name": "CI", "path": "tests/test_a.py", "message": "ValueError: y"},
        {"name": "CI", "path": "tests/test_b.py", "message": "KeyError: z"},
    ]
    detail = gh_checks._group_failures_by_file(failures)
    assert len(detail) == 2, detail
    assert detail[0]["path"] == "tests/test_a.py"
    assert detail[0]["errors"] == ["AssertionError: x", "ValueError: y"]
    assert detail[1]["path"] == "tests/test_b.py"
    assert detail[1]["errors"] == ["KeyError: z"]
    print("  group_failures_by_file check-runs: ok")


def test_group_failures_by_file_actions_tier():
    failures = [
        {"name": "CI / test", "message": "some error before any FAILED"},
        {"name": "CI / test", "message": "FAILED: test_x.py (1.2s)"},
        {"name": "CI / test", "message": "AssertionError: expected 1 == 2"},
        {"name": "CI / test", "message": "FAILED: test_y.py (0.8s)"},
        {"name": "CI / test", "message": "ValueError: bad value"},
    ]
    detail = gh_checks._group_failures_by_file(failures)
    assert len(detail) == 3, detail
    paths = [d["path"] for d in detail]
    assert "(unknown)" in paths
    assert "tests/test_x.py" in paths
    assert "tests/test_y.py" in paths
    x = next(d for d in detail if d["path"] == "tests/test_x.py")
    assert x["errors"] == [
        "FAILED: test_x.py (1.2s)",
        "AssertionError: expected 1 == 2",
    ]
    y = next(d for d in detail if d["path"] == "tests/test_y.py")
    assert y["errors"] == ["FAILED: test_y.py (0.8s)", "ValueError: bad value"]
    print("  group_failures_by_file actions tier: ok")


def test_group_failures_by_file_capped():
    failures = [
        {"name": "CI", "path": f"tests/test_{i}.py", "message": f"err {i}"}
        for i in range(7)
    ]
    detail = gh_checks._group_failures_by_file(failures)
    assert len(detail) == 5, detail
    print("  group_failures_by_file capped at 5: ok")


def test_group_failures_by_file_long_message_truncated():
    failures = [
        {"name": "CI", "path": "tests/test_a.py", "message": "F" * 300},
    ]
    detail = gh_checks._group_failures_by_file(failures)
    assert len(detail[0]["errors"][0]) == 200
    print("  group_failures_by_file truncates long messages: ok")


# ---------------------------------------------------------------------------
# pr_checks failed_files_detail end-to-end
# ---------------------------------------------------------------------------


def test_pr_checks_failed_files_detail_end_to_end():
    """pr_checks wires failed_files_detail end-to-end: a pathed check-run
    annotation is grouped under its collapsed path, and the message is
    surfaced verbatim (no newlines in the input, so no collapse needed)."""
    gh.clear_cache()

    def handler(request):
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "id": 77,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://ci/run/77",
                        }
                    ]
                },
            )
        if path.endswith("/check-runs/77/annotations"):
            return httpx.Response(
                200,
                json=[
                    {
                        "path": "  tests/test_x.py  ",
                        "start_line": 42,
                        "message": "AssertionError: expected 1 == 2",
                    }
                ],
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        result = gh.pr_checks(4246, _head_sha="deadsha")
        assert result["source"] == "check_runs", result["source"]
        assert result["state"] == "failure", result["state"]
        detail = result.get("failed_files_detail")
        assert detail is not None, result
        assert len(detail) == 1, detail
        assert detail[0]["path"] == "tests/test_x.py", detail[0]
        assert detail[0]["errors"] == ["AssertionError: expected 1 == 2"], detail[0]
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  pr_checks failed_files_detail end-to-end: ok")


# ---------------------------------------------------------------------------
# apr_checks failed_files_detail async tier pins
# ---------------------------------------------------------------------------


def test_apr_checks_failed_files_detail_check_runs_tier():
    """apr_pins :485 - async check-runs tier sets failed_files_detail:
    a pathed annotation is grouped under its collapsed path."""
    gh.clear_cache()

    def handler(request):
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "id": 88,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://ci/run/88",
                        }
                    ]
                },
            )
        if path.endswith("/check-runs/88/annotations"):
            return httpx.Response(
                200,
                json=[
                    {
                        "path": "tests/test_async.py",
                        "start_line": 10,
                        "message": "ValueError: async path pin",
                    }
                ],
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        import asyncio

        result = asyncio.run(gh.apr_checks(4247, _head_sha="deadsha"))
        assert result is not None
        assert result["source"] == "check_runs", result["source"]
        assert result["state"] == "failure", result["state"]
        detail = result.get("failed_files_detail")
        assert detail is not None, result
        assert len(detail) == 1, detail
        assert detail[0]["path"] == "tests/test_async.py", detail[0]
        assert detail[0]["errors"] == ["ValueError: async path pin"], detail[0]
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_checks failed_files_detail check-runs tier: ok")


def test_apr_checks_failed_files_detail_statuses_tier():
    """apr_pins :538 - async statuses tier sets failed_files_detail:
    check-runs and Actions both 500, statuses tier answers."""
    gh.clear_cache()

    def handler(request):
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(500, json={"message": "not found"})
        if path.endswith("/actions/runs"):
            return httpx.Response(500, json={"message": "not found"})
        if path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "state": "failure",
                    "statuses": [
                        {
                            "context": "CI",
                            "state": "failure",
                            "description": "AssertionError: status tier pin",
                            "target_url": "https://ci/run/99",
                        }
                    ],
                },
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        import asyncio

        result = asyncio.run(gh.apr_checks(4248, _head_sha="deadsha"))
        assert result is not None
        assert result["source"] == "statuses", result["source"]
        assert result["state"] == "failure", result["state"]
        detail = result.get("failed_files_detail")
        assert detail is not None, result
        assert len(detail) >= 1, detail
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_checks failed_files_detail statuses tier: ok")


def test_group_failures_by_file_count_line_is_not_a_file():
    """The run_all.py count line 'FAILED: 1 of 5 test files' must NOT parse
    as file '1' - the old regex had digits in the class, so the count line
    created a fake file bucket. The #B87 guard stops at the first space,
    so the count line yields None and both the count and the FAILED FILES:
    digest bucket under (unknown)."""
    failures = [
        {"path": None, "message": "FAILED: 1 of 5 test files"},
        {"path": None, "message": "FAILED FILES: test_foo.py, test_bar.py"},
    ]
    detail = gh_checks._group_failures_by_file(failures)
    paths = [g["path"] for g in detail]
    assert "1" not in paths, paths
    assert "(unknown)" in paths, paths
    assert len(detail) == 1, detail
    assert detail[0]["path"] == "(unknown)", detail[0]
    assert len(detail[0]["errors"]) == 2, detail[0]
    print("  count line is not a file; both lines bucket under (unknown): ok")


def test_group_failures_by_file_inferred_marker():
    """#B131 follow-up: a bare .py token with no slash (e.g. 'FAILED: config.py')
    gets the tests/ prefix synthesis and must carry inferred: True so the
    provenance is distinguishable from a real API-sourced path. A path that
    already contains a slash (e.g. 'FAILED: db/_jobs.py') is NOT synthesized
    and must NOT carry inferred."""
    failures = [
        {"path": None, "message": "FAILED: config.py"},
        {"path": None, "message": "FAILED: db/_jobs.py"},
    ]
    detail = gh_checks._group_failures_by_file(failures)
    by_path = {g["path"]: g for g in detail}
    inferred_entry = by_path.get("tests/config.py")
    assert inferred_entry is not None, by_path
    assert inferred_entry.get("inferred") is True, inferred_entry
    real_entry = by_path.get("db/_jobs.py")
    assert real_entry is not None, by_path
    assert "inferred" not in real_entry, real_entry
    print("  inferred marker present on synthesized paths only: ok")


def test_group_failures_by_file_source_seam_order_independence():
    """#B131 follow-up: the source seam (Actions log lines vs check-run
    annotations) must not carry current_file across. A check-run
    annotation with path=None goes to (unknown), not under a filename
    from a different source's log. Order-independence: the same two
    messages in both orders yield the same attribution."""
    actions_line = {"message": "FAILED: tests/test_foo.py (line 42)"}
    annotation = {"path": None, "message": "Process completed with exit code 1."}
    merged = [actions_line, annotation]
    detail = gh_checks._group_failures_by_file(merged)
    by_path = {g["path"]: g for g in detail}
    assert "tests/test_foo.py" in by_path, by_path
    assert len(by_path["tests/test_foo.py"]["errors"]) == 1, by_path
    assert "(unknown)" in by_path, by_path
    assert len(by_path["(unknown)"]["errors"]) == 1, by_path
    reversed_merged = [annotation, actions_line]
    detail2 = gh_checks._group_failures_by_file(reversed_merged)
    by_path2 = {g["path"]: g for g in detail2}
    assert by_path == by_path2, (by_path, by_path2)
    print("  source-seam reset + order-independence: ok")


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
    test_apaginate_cap_bounds_runaway_servers()
    test_pr_diff_invalidates_when_the_head_moves()
    test_apr_diff_invalidates_when_the_head_moves()
    test_pr_diff_names_the_head_it_read_within_the_window()
    test_apr_diff_names_the_head_it_read_within_the_window()
    test_pr_diff_cap_bounds_runaway_servers()
    test_open_prs_paginates_past_the_default_page()
    test_open_prs_pagination_clamps_per_page_above_the_cap()
    test_request_text_follows_redirect_to_blob()
    test_supplement_enriches_thin_exit_code_annotations()
    test_apr_checks_fans_out_job_logs()
    test_apr_checks_cache_parity_with_sync()
    test_apr_checks_falls_through_to_actions_tier()
    test_apr_checks_head_sha_shortcut_skips_pr_fetch()
    test_propose_change_failure_cleans_up_orphan_branch()
    test_comment_on_pr_missing_html_url_ok()
    test_ingress_collapse_end_to_end()
    test_apr_checks_statuses_tier_collapses_description()
    test_group_failures_by_file_check_runs()
    test_group_failures_by_file_actions_tier()
    test_group_failures_by_file_capped()
    test_group_failures_by_file_long_message_truncated()
    test_pr_checks_failed_files_detail_end_to_end()
    test_apr_checks_failed_files_detail_check_runs_tier()
    test_apr_checks_failed_files_detail_statuses_tier()
    test_group_failures_by_file_count_line_is_not_a_file()
    test_group_failures_by_file_inferred_marker()
    test_group_failures_by_file_source_seam_order_independence()
    test_etag_revalidation_serves_304_without_a_body()
    test_etag_stale_copy_refetches_with_a_fresh_validator()
    test_pr_has_label_reuses_passed_row_without_a_fetch()
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
                    n = int(part[len("page=") :])
            return httpx.Response(200, json=pages[n - 1] if n <= len(pages) else [])
        return httpx.Response(200, json=[])

    return handler


def test_pr_files_paginates_past_the_default_page():
    hits: list[str] = []
    page1 = [
        {
            "filename": f"a{i}.py",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "patch": "x",
        }
        for i in range(100)
    ]
    page2 = [{"filename": "b7.py", "status": "added", "additions": 9, "deletions": 2}]
    handler = _serve_pages(hits, "/files", [page1, page2])
    old = _install_mock(handler)

    def qpage(u):
        for part in u.partition("?")[2].split("&"):
            if part.startswith("page="):
                return int(part[len("page=") :])
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
        gh_core._client = old
        gh.clear_cache()
    print("  pr_files paginates past the default 30/100-item page: ok")


def test_short_first_page_costs_one_request():
    hits: list[str] = []
    handler = _serve_pages(
        hits,
        "/comments",
        [
            [
                {
                    "id": 1,
                    "user": {"login": "a"},
                    "body": "hi",
                    "created_at": "2026-08-24T00:00:00Z",
                }
            ]
        ],
    )
    old = _install_mock(handler)
    try:
        got = gh.pr_comments(4246)
        assert len(got) == 1 and got[0]["kind"] == "review"
        assert len([u for u in hits if "issues/4246" in u]) == 1
        assert not any("page=2" in u for u in hits), hits
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  short first page terminates pagination after one request: ok")


def test_pagination_cap_bounds_runaway_servers():
    hits: list[str] = []
    handler = _serve_pages(
        hits,
        "/files",
        [[{"filename": "x.py", "status": "modified"}] * 100] * 500,
    )
    saved_cap = gh_reads._PR_PAGE_CAP
    gh_reads._PR_PAGE_CAP = 3
    old = _install_mock(handler)
    try:
        got = gh.pr_files(4247)
        assert len(got) == 300, len(got)
        assert len([u for u in hits if "pr_files" in u or "/files" in u]) == 3
    finally:
        gh_reads._PR_PAGE_CAP = saved_cap
        gh_core._client = old
        gh.clear_cache()
    print("  page cap bounds a server that never sends a short page: ok")


def test_apaginate_cap_bounds_runaway_servers():
    hits: list[str] = []
    page = [{"sha": f"{i:040x}", "commit": {"message": "m"}} for i in range(100)]
    handler = _serve_pages(hits, "/commits", [page] * 500)
    saved_cap = gh_reads._PR_PAGE_CAP
    gh_reads._PR_PAGE_CAP = 3
    old = _install_mock(handler)
    try:
        got = asyncio.run(gh._apaginate("pulls/4249/commits", page))
        assert len(got) == 300, len(got)
        assert len([u for u in hits if "/commits" in u]) == 2, hits
    finally:
        gh_reads._PR_PAGE_CAP = saved_cap
        gh_core._client = old
        gh.clear_cache()
    print("  _apaginate page cap bounds a server that never sends a short page: ok")


def test_pr_diff_invalidates_when_the_head_moves():
    # #B195: the diff key carried no head sha, so a push inside the TTL
    # served the PREVIOUS head's patch and the payload named no commit. The
    # key and the payload both carry it now.
    gh.clear_cache()
    hits: list[str] = []
    head = {"ref": "p", "sha": "aaaa111"}

    def handler(request):
        url = str(request.url)
        hits.append(url)
        path, _, _query = url.partition("?")
        if path.endswith("/pulls/7171"):
            payload = dict(_PR_4242, number=7171, head=dict(head))
            return httpx.Response(200, json=payload)
        if path.endswith("/files"):
            files = [{"filename": "a.py", "additions": 1, "patch": "+ " + head["sha"]}]
            return httpx.Response(200, json=files)
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    try:
        first = gh.pr_diff(7171)
        assert first["head_sha"] == "aaaa111", first
        assert first["files"][0]["patch"] == "+ aaaa111"
        # The head moves. Pop ONLY pr_raw - the head-keyed diff entry still
        # holds A's payload - so the read must re-resolve and serve B.
        head["sha"] = "bbbb222"
        gh_core._pr_cache._store.pop(("pr_raw", 7171), None)
        second = gh.pr_diff(7171)
        assert second["head_sha"] == "bbbb222", second
        assert second["files"][0]["patch"] == "+ bbbb222"
        # A warm read costs no transport at all.
        before = len(hits)
        third = gh.pr_diff(7171)
        assert third["head_sha"] == "bbbb222"
        assert len(hits) == before, hits[before:]
        # Invalidation sweeps the head-keyed entry too.
        gh_core._invalidate_pr(7171)
        assert ("pr_diff", 7171, "bbbb222") not in gh_core._pr_cache._store
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  pr_diff cache key carries the head sha: ok")


def test_apr_diff_invalidates_when_the_head_moves():
    # Same contract on the native twin, plus the peek: the head comes from
    # the warm pr_raw entry (no serial pre-fetch), so a warm read is free.
    gh.clear_cache()
    hits: list[str] = []
    head = {"ref": "p", "sha": "cccc333"}

    def handler(request):
        url = str(request.url)
        hits.append(url)
        path, _, _query = url.partition("?")
        if path.endswith("/pulls/7172"):
            payload = dict(_PR_4242, number=7172, head=dict(head))
            return httpx.Response(200, json=payload)
        if path.endswith("/files"):
            files = [{"filename": "b.py", "additions": 2, "patch": "+ " + head["sha"]}]
            return httpx.Response(200, json=files)
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    try:
        first = asyncio.run(gh.apr_diff(7172))
        assert first["head_sha"] == "cccc333", first
        head["sha"] = "dddd444"
        gh_core._pr_cache._store.pop(("pr_raw", 7172), None)
        second = asyncio.run(gh.apr_diff(7172))
        assert second["head_sha"] == "dddd444", second
        assert second["files"][0]["patch"] == "+ dddd444"
        before = len(hits)
        asyncio.run(gh.apr_diff(7172))
        assert len(hits) == before, hits[before:]
        gh_core._invalidate_pr(7172)
        assert ("pr_diff", 7172, "dddd444") not in gh_core._pr_cache._store
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_diff cache key carries the head sha: ok")


def test_pr_diff_names_the_head_it_read_within_the_window():
    # #B195 follow-up (finding #144): the head-keyed key is right, but the
    # head is resolved from the TTL-cached pr_raw, so within one pr_raw
    # window after a push the OLD head is still served. That bound is the
    # honest one and the docstrings now say it; pin it so the guarantee
    # cannot quietly go back to resting on prose. Goes RED if someone
    # evicts pr_raw on push, which would close the window for real - re-point
    # this arm then, do not delete it.
    gh.clear_cache()
    hits: list[str] = []
    head = {"ref": "p", "sha": "eeee555"}

    def handler(request):
        url = str(request.url)
        hits.append(url)
        path, _, _query = url.partition("?")
        if path.endswith("/pulls/7173"):
            payload = dict(_PR_4242, number=7173, head=dict(head))
            return httpx.Response(200, json=payload)
        if path.endswith("/files"):
            files = [{"filename": "c.py", "additions": 1, "patch": "+ " + head["sha"]}]
            return httpx.Response(200, json=files)
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    try:
        first = gh.pr_diff(7173)
        assert first["head_sha"] == "eeee555", first
        # The head moves and pr_raw is NOT evicted - nothing evicts it on a
        # push, only the outcome poller's _invalidate_pr does.
        head["sha"] = "ffff666"
        stale = gh.pr_diff(7173)
        assert stale["head_sha"] == "eeee555", stale
        assert stale["files"][0]["patch"] == "+ eeee555"
        # Zero transport for that repeat: it is a pure cache read, which is
        # what makes this a window rather than a fetch.
        before = len(hits)
        again = gh.pr_diff(7173)
        assert again["head_sha"] == "eeee555"
        assert len(hits) == before, hits[before:]
        # Past the window the fresh head is served, so the bound ends at the
        # eviction rather than at the key.
        gh_core._invalidate_pr(7173)
        fresh = gh.pr_diff(7173)
        assert fresh["head_sha"] == "ffff666", fresh
        assert fresh["files"][0]["patch"] == "+ ffff666"
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  pr_diff names the head it read, window and all: ok")


def test_apr_diff_names_the_head_it_read_within_the_window():
    # Same honest bound on the twin, whose head is PEEKED rather than
    # fetched - so it is the same window, entered the same way.
    gh.clear_cache()
    hits: list[str] = []
    head = {"ref": "p", "sha": "1111aaaa"}

    def handler(request):
        url = str(request.url)
        hits.append(url)
        path, _, _query = url.partition("?")
        if path.endswith("/pulls/7174"):
            payload = dict(_PR_4242, number=7174, head=dict(head))
            return httpx.Response(200, json=payload)
        if path.endswith("/files"):
            files = [{"filename": "d.py", "additions": 1, "patch": "+ " + head["sha"]}]
            return httpx.Response(200, json=files)
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    try:
        first = asyncio.run(gh.apr_diff(7174))
        assert first["head_sha"] == "1111aaaa", first
        head["sha"] = "2222bbbb"
        # pr_raw stays warm, so the peek returns the OLD head and the
        # head-keyed entry answers with zero transport.
        stale = asyncio.run(gh.apr_diff(7174))
        assert stale["head_sha"] == "1111aaaa", stale
        assert stale["files"][0]["patch"] == "+ 1111aaaa"
        before = len(hits)
        asyncio.run(gh.apr_diff(7174))
        assert len(hits) == before, hits[before:]
        gh_core._invalidate_pr(7174)
        fresh = asyncio.run(gh.apr_diff(7174))
        assert fresh["head_sha"] == "2222bbbb", fresh
        assert fresh["files"][0]["patch"] == "+ 2222bbbb"
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_diff names the head it read, window and all: ok")


def test_pr_diff_cap_bounds_runaway_servers():
    hits: list[str] = []
    page = [
        {
            "filename": f"f{i}.py",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "patch": "x",
        }
        for i in range(100)
    ]

    def handler(request):
        url = str(request.url)
        hits.append(url)
        path, _, query = url.partition("?")
        if path.endswith("/pulls/4243"):
            return httpx.Response(200, json=dict(_PR_4242, number=4243))
        if path.endswith("/files"):
            n = 1
            for part in query.split("&"):
                if part.startswith("page="):
                    n = int(part[len("page=") :])
            return httpx.Response(200, json=page if n <= 500 else [])
        return httpx.Response(200, json=[])

    saved_cap = gh_reads._PR_PAGE_CAP
    gh_reads._PR_PAGE_CAP = 3
    old = _install_mock(handler)
    try:
        got = gh.pr_diff(4243)
        assert len(got["files"]) == 300, len(got["files"])
        assert len([u for u in hits if "/files" in u]) == 3, hits
    finally:
        gh_reads._PR_PAGE_CAP = saved_cap
        gh_core._client = old
        gh.clear_cache()
    print("  pr_diff file-loop cap bounds a runaway server: ok")


def test_open_prs_paginates_past_the_default_page():
    """open_prs() used to be a single page read - silent truncation past
    GITHUB_PRS_PER_PAGE (default 100) once a repo outgrew one page. The
    page loop now mirrors _paginated_closed_pulls: a full 100-item page 1 +
    a short 10-item page 2 must merge into one list, and the native async
    twin aopen_prs() must reach the same result through the shared
    cache."""
    hits: list[str] = []

    def _pr_row(n: int) -> dict:
        return {
            "number": n,
            "title": f"pr {n}",
            "head": {"ref": f"head-{n}", "sha": f"sha{n:040x}"},
            "base": {"ref": "main"},
            "user": {"login": f"user{n}"},
            "created_at": "2026-08-30T00:00:00Z",
            "html_url": f"https://github.com/nssatlantis/agent_land/pull/{n}",
            "mergeable_state": "clean",
            "body": f"Citizen: test (agent_id={n})",
            "labels": [{"name": f"label-{n}"}],
        }

    page1 = [_pr_row(n) for n in range(100, 0, -1)]
    page2 = [_pr_row(n) for n in range(110, 100, -1)]

    def handler(request):
        url = str(request.url)
        hits.append(url)
        path, _, query = url.partition("?")
        if not path.endswith("/pulls"):
            return httpx.Response(404, json={"message": "not found"})
        n = 1
        for part in query.split("&"):
            if part.startswith("page="):
                n = int(part[len("page=") :])
        if n == 1:
            return httpx.Response(200, json=page1)
        if n == 2:
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    try:
        got = gh.open_prs()
        nums = [r["number"] for r in got]
        assert nums == list(range(100, 0, -1)) + list(range(110, 100, -1)), (
            f"got={nums} hits={hits}"
        )
        assert got[0]["citizen"] is not None
        assert got[-1]["citizen"] is not None
        assert got[0]["labels"] == ["label-100"], got[0]
        assert len([u for u in hits if "/pulls?" in u and "state=open" in u]) == 2
        before = len(hits)
        native = asyncio.run(gh_reads.aopen_prs())
        assert [r["number"] for r in native] == [r["number"] for r in got]
        assert len(hits) == before, hits
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  open_prs paginates past the first 100-item page: ok")


def test_open_prs_pagination_clamps_per_page_above_the_cap():
    """#B131: the open page loop stops at len(batch) < per_page while the
    request is clamped to _GITHUB_MAX_PER_PAGE (100), so a
    FORUM_GITHUB_PRS_PER_PAGE above 100 stopped after page 1 and silently
    dropped every open PR past the newest 100. Knob at 150 with a full
    100-item page 1: the clamp makes per_page 100, so page 2 must still be
    fetched - sync and native-async twins both."""
    hits: list[str] = []

    def _pr_row(n: int) -> dict:
        return {
            "number": n,
            "title": f"pr {n}",
            "head": {"ref": f"head-{n}", "sha": f"sha{n:040x}"},
            "base": {"ref": "main"},
            "user": {"login": f"user{n}"},
            "created_at": "2026-08-30T00:00:00Z",
            "html_url": f"https://github.com/nssatlantis/agent_land/pull/{n}",
            "mergeable_state": "clean",
            "body": f"Citizen: test (agent_id={n})",
            "labels": [{"name": f"label-{n}"}],
        }

    page1 = [_pr_row(n) for n in range(100, 0, -1)]
    page2 = [_pr_row(n) for n in range(110, 100, -1)]

    def handler(request):
        url = str(request.url)
        hits.append(url)
        path, _, query = url.partition("?")
        if not path.endswith("/pulls"):
            return httpx.Response(404, json={"message": "not found"})
        n = 1
        for part in query.split("&"):
            if part.startswith("page="):
                n = int(part[len("page=") :])
        if n == 1:
            return httpx.Response(200, json=page1)
        if n == 2:
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    old_per_page = gh_reads.config.GITHUB_PRS_PER_PAGE
    gh_reads.config.GITHUB_PRS_PER_PAGE = 150
    try:
        gh.clear_cache()
        got = gh.open_prs()
        assert len(got) == 110, f"sync stopped early: {len(got)} rows {hits}"
        open_hits = [u for u in hits if "/pulls?" in u and "state=open" in u]
        assert len(open_hits) == 2, open_hits
        assert all("per_page=100" in u for u in open_hits), open_hits
        gh.clear_cache()
        before = len(hits)
        native = asyncio.run(gh_reads.aopen_prs())
        assert len(native) == 110, f"async stopped early: {len(native)} rows"
        async_hits = [u for u in hits[before:] if "state=open" in u]
        assert len(async_hits) == 2, async_hits
    finally:
        gh_reads.config.GITHUB_PRS_PER_PAGE = old_per_page
        gh_core._client = old
        gh.clear_cache()
    print("  open_prs clamps per_page above the 100 cap: ok")


def test_etag_revalidation_serves_304_without_a_body():
    """Item A: a fresh GET stores the ETag; the next GET revalidates with
    If-None-Match and GitHub's 304 (no body, no rate-limit quota) is served
    from the etag store - the caller gets the stored value and no further
    JSON was transferred."""
    hits: list[str] = []

    def handler(request):
        if request.headers.get("If-None-Match") == '"v1"':
            hits.append("304")
            return httpx.Response(304)
        hits.append("200")
        return httpx.Response(
            200,
            json={"number": 42, "title": "demo"},
            headers={"ETag": '"v1"'},
        )

    old = _install_mock(handler)
    try:
        first = gh_core._request("GET", "pulls/4242")
        assert first["title"] == "demo"
        assert hits == ["200"]
        second = gh_core._request("GET", "pulls/4242")
        assert second == {"number": 42, "title": "demo"}
        assert hits == ["200", "304"]
        # A cached 304 must not poison the TTL read caches of other PRs.
        assert gh_core._request("GET", "pulls/4243")["title"] == "demo"
        assert hits == ["200", "304", "200"]
    finally:
        gh_core._client = old
        gh_core._etag_store.clear()
    print("  etag revalidation serves a 304 from the store: ok")


def test_etag_stale_copy_refetches_with_a_fresh_validator():
    """Item A: when the resource changed, GitHub answers 200 with a new ETag
    (never 304) and the store is refreshed so the next read revalidates
    against the new version."""
    hits: list[str] = []

    def handler(request):
        inm = request.headers.get("If-None-Match")
        if inm == '"v2"':
            hits.append("304-v2")
            return httpx.Response(304)
        if inm == '"v1"':
            hits.append("200-v2")
            return httpx.Response(
                200,
                json={"number": 42, "title": "changed"},
                headers={"ETag": '"v2"'},
            )
        hits.append("200-v1")
        return httpx.Response(
            200,
            json={"number": 42, "title": "original"},
            headers={"ETag": '"v1"'},
        )

    old = _install_mock(handler)
    try:
        titles = [
            gh_core._request("GET", "pulls/4244")["title"],
            gh_core._request("GET", "pulls/4244")["title"],
            gh_core._request("GET", "pulls/4244")["title"],
        ]
        assert titles == ["original", "changed", "changed"], titles
        assert hits == ["200-v1", "200-v2", "304-v2"], hits
    finally:
        gh_core._client = old
        gh_core._etag_store.clear()
    print("  stale etag triggers a fresh 200 and stores the new validator: ok")


def test_pr_has_label_reuses_passed_row_without_a_fetch():
    """Item B: pr_has_label(_pr=row) never fetches the PR - the poller's
    hold gate pays zero API calls for the labels the row already carries, and
    both GitHub label shapes (dict vs flattened string list) are accepted."""
    hits: list[str] = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        assert gh.pr_has_label(99, "hold", _pr={"labels": [{"name": "Hold"}]}) is True
        assert gh.pr_has_label(99, "wip", _pr={"labels": ["wip"]}) is True
        assert gh.pr_has_label(99, "hold", _pr={"labels": ["wip"]}) is False
        assert not hits, hits
    finally:
        gh_core._client = old
    print("  pr_has_label reuses a supplied row without any fetch: ok")


def test_request_text_follows_redirect_to_blob():
    """GitHub's job-log endpoint answers 302 -> signed blob URL. The text
    reader must follow it (httpx defaults to NOT following), or the
    Actions log tier can never produce a line."""

    def handler(request):
        url = str(request.url)
        if url.endswith("/actions/jobs/777/logs"):
            return httpx.Response(
                302,
                headers={"Location": "https://blob.example/log.txt"},
            )
        if url == "https://blob.example/log.txt":
            return httpx.Response(200, text="step ok\nerror: boom\n")
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        text = gh_core._request_text("GET", "actions/jobs/777/logs")
        assert text is not None and "boom" in text, text
    finally:
        gh_core._client = old
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
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 1,
                            "name": "CI",
                            "conclusion": "failure",
                            "html_url": "https://ci/run/1",
                        }
                    ]
                },
            )
        if path.endswith("/jobs"):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 9,
                            "name": "test",
                            "conclusion": "failure",
                        }
                    ]
                },
            )
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
        "source": "check_runs",
        "state": "failure",
        "runs": [{"name": "CI", "status": "completed", "conclusion": "failure"}],
        "failures": [
            {
                "name": "CI",
                "path": ".github/workflows/ci.yml",
                "message": "exit code 1",
                "line": 30,
            }
        ],
    }
    old = _install_mock(handler)
    try:
        gh_checks._supplement_check_run_failures(result, "deadsha")
        msgs = [f["message"] for f in result["failures"]]
        assert any("AssertionError" in m for m in msgs), msgs
        # Log lines were merged IN FRONT of the thin annotation.
        assert any("AssertionError" in m for m in msgs[:2]), msgs
        assert msgs[-1] == "exit code 1", msgs
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  thin 'exit code' annotations get enriched from logs: ok")


def _actions_tier_handler(hits, *, job_bodies=None, gate=None):
    """Routes the Actions tier for one head sha. Two failed jobs (111
    test / 112 lint); when *gate* is an (Event, order-list) pair,
    /logs requests record start/end around a both-started barrier so a
    sequential caller deadlocks instead of passing."""
    job_bodies = job_bodies or {}

    async def handler(request):
        url = str(request.url)
        path, _, query = url.partition("?")
        hits.append(url)
        if path.endswith("/pulls/4244") or path.endswith("/pulls/4245"):
            return httpx.Response(
                200,
                json={
                    "number": 4244,
                    "head": {"sha": "deadsha", "ref": "b"},
                    "base": {"ref": "main"},
                },
            )
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if path.endswith("/actions/runs") and "head_sha=" in query:
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 31,
                            "name": "CI",
                            "conclusion": "failure",
                            "html_url": "https://ci/run/31",
                        }
                    ]
                },
            )
        if path.endswith("/jobs"):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {"id": 111, "name": "test", "conclusion": "failure"},
                        {"id": 112, "name": "lint", "conclusion": "failure"},
                    ]
                },
            )
        m = re.search(r"/actions/jobs/(\d+)/logs$", path)
        if m:
            jid = m.group(1)
            body = job_bodies.get(jid, f"error: boom-{jid}\n")
            if gate is not None:
                ev, order = gate
                order.append(("start", jid))
                if len([e for e in order if e[0] == "start"]) >= 2:
                    ev.set()
                await asyncio.wait_for(ev.wait(), 2)
                order.append(("end", jid))
            return httpx.Response(200, text=body)
        return httpx.Response(200, json={})

    return handler


def test_apr_checks_fans_out_job_logs():
    """The expensive tail - failed jobs' log downloads - must overlap:
    both requests start before either completes. A sequential chain
    deadlocks on the gate and times out."""
    hits: list = []
    order: list = []
    gate = asyncio.Event()
    handler = _actions_tier_handler(
        hits,
        job_bodies={"111": "error: boom-111\n", "112": "error: boom-112\n"},
        gate=(gate, order),
    )
    old = _install_mock(handler)
    try:
        result = asyncio.run(gh.apr_checks(4244, _head_sha="deadsha"))
        assert result["source"] == "actions", result["source"]
        assert result["state"] == "failure"
        names = [f["name"] for f in result["failures"]]
        assert sorted(names) == ["CI / lint", "CI / test"], names
        msgs = {f["name"]: f["message"] for f in result["failures"]}
        assert msgs["CI / test"] == "error: boom-111"
        assert msgs["CI / lint"] == "error: boom-112"
        # Overlap proof: the second log request STARTED before the first
        # ENDED (a sequential implementation times out on the gate).
        starts = [i for i, ev in enumerate(order) if ev[0] == "start"]
        ends = [i for i, ev in enumerate(order) if ev[0] == "end"]
        assert max(starts) < min(ends), order
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_checks fans job logs out concurrently: ok")


def test_apr_checks_cache_parity_with_sync():
    hits: list = []
    handler = _actions_tier_handler(
        hits, job_bodies={"111": "error: x\n", "112": "error: y\n"}
    )
    old = _install_mock(handler)
    try:
        native = asyncio.run(gh.apr_checks(4244, _head_sha="deadsha"))
        n_hits = len(hits)
        sync_face = gh.pr_checks(4244, _head_sha="deadsha")
        assert sync_face == native, "sync/native shapes diverged"
        assert len(hits) == n_hits, "sync face must read the shared cache"
        # Reverse direction on a second number: sync warms, native reads.
        native2 = asyncio.run(gh.apr_checks(4245, _head_sha="deadsha"))
        _ = gh.pr_checks(4245, _head_sha="deadsha")
        # Per red read on the actions tier: check-runs probe + runs list
        # + jobs list + two log downloads = 5 transport calls.
        assert len(hits) == n_hits + 5, (n_hits, len(hits))
        assert native2 == gh.pr_checks(4245, _head_sha="deadsha")
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_checks shares the pr_checks cache byte-for-byte: ok")


def test_apr_checks_falls_through_to_actions_tier():
    hits: list = []
    handler = _actions_tier_handler(hits)

    def failing(request):
        url = str(request.url)
        path, _, query = url.partition("?")
        hits.append(url)
        if path.endswith("/check-runs"):
            return httpx.Response(404, json={"message": "no check runs"})
        return handler(request)

    old = _install_mock(failing)
    try:
        result = asyncio.run(gh.apr_checks(4244, _head_sha="deadsha"))
        assert result["source"] == "actions", result["source"]
        assert any("check-runs" in u for u in hits), hits
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  check-run failures fall through to the actions tier: ok")


def test_apr_checks_head_sha_shortcut_skips_pr_fetch():
    hits: list = []
    handler = _actions_tier_handler(hits)
    old = _install_mock(handler)
    try:
        asyncio.run(gh.apr_checks(4244, _head_sha="deadsha"))
        assert not any("/pulls/" in u for u in hits), hits
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  _head_sha shortcut skips the PR fetch: ok")


def test_propose_change_failure_cleans_up_orphan_branch():
    # Bug 1.6: when a propose_change's file PUT (or a later step) fails after
    # the feature branch was created, the branch would be left orphaned (a
    # ref with no PR on it). propose_change must best-effort DELETE the branch
    # before re-raising, so a failed propose leaves no dangling ref.
    branch = "proposal/orphan-test/20260829-000000"
    calls = []

    def handler(request):
        method = request.method
        path = request.url.path
        calls.append((method, path))
        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base123"}})
        if method == "GET" and "/contents/" in path:
            return httpx.Response(404, json={"message": "no file"})
        if method == "POST" and path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": f"refs/heads/{branch}"})
        if method == "PUT" and "/contents/" in path:
            return httpx.Response(500, json={"message": "boom"})
        if method == "DELETE" and path.endswith("/git/refs/heads/" + branch):
            return httpx.Response(204, json=None)
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        raised = None
        try:
            gh.propose_change(
                [{"path": "docs/x.md", "content": "hello"}],
                title="t",
                body="b",
                citizen="curious-alpha (agent_id=3)",
                branch=branch,
            )
        except gh.RepoError as exc:
            raised = str(exc)
        assert raised is not None and "boom" in raised, raised
        # The orphan branch must have been deleted before the error propagated.
        assert any(
            m == "DELETE" and p.endswith("/git/refs/heads/" + branch) for m, p in calls
        ), calls
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  propose_change failure cleans up the orphan branch: ok")


def test_comment_on_pr_missing_html_url_ok():
    # Bug 1.7: comment_on_pr read data["html_url"] with a hard subscript -
    # a payload that omitted html_url would KeyError. Guarded with .get() it
    # returns None instead of crashing.
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": 7,
                "user": {"login": "someone"},
                "created_at": "2026-08-29T00:00:00Z",
                # html_url deliberately absent
            },
        )

    old = _install_mock(handler)
    try:
        result = gh.comment_on_pr(5, "hello world")
        assert result["comment_id"] == 7
        assert result["author"] == "someone"
        assert result["html_url"] is None, result
    finally:
        gh_core._client = old
    print("  comment_on_pr tolerates a missing html_url: ok")


if __name__ == "__main__":
    sys.exit(main())
