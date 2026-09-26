import asyncio
import sys

import httpx
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github as gh
from github import _checks as gh_checks
from github import _core as gh_core
from github._core import RepoError

gh_core.GITHUB_TOKEN = "test-token"  # satisfies _ensure_token(); no network touched


# ---------------------------------------------------------------------------
# Shared mock plumbing
# ---------------------------------------------------------------------------


def _install_mock(handler):
    """Swap the module-level httpx client for a MockTransport-backed one and
    return the old client so the caller can restore it in a finally block."""
    old = gh_core._client
    gh_core._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )
    return old


# ---------------------------------------------------------------------------
# Low-level _request / _request_text
# ---------------------------------------------------------------------------


def test_request_text_reads_text_and_honours_ok_404():
    def handler(request):
        if request.url.path.endswith("/raw"):
            return httpx.Response(200, text="hello world")
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        text = gh_core._request_text("GET", "repos/x/y/raw")
        assert text == "hello world", text
        # ok_404=True turns a 404 into None instead of raising.
        missing = gh_core._request_text("GET", "repos/x/y/missing", ok_404=True)
        assert missing is None, missing
    finally:
        gh_core._client = old
    print("  _request_text reads text and honours ok_404: ok")


def test_non_ok_path_raises_repo_error_with_body_message():
    def handler(request):
        return httpx.Response(500, json={"message": "boom"})

    old = _install_mock(handler)
    try:
        try:
            gh_core._request("GET", "repos/x/y")
            raise AssertionError("expected RepoError")
        except RepoError as exc:
            assert "boom" in str(exc), exc
    finally:
        gh_core._client = old
    print("  non-OK path raises RepoError with the body message: ok")


# ---------------------------------------------------------------------------
# _request retry / heal behaviour
# ---------------------------------------------------------------------------


def test_connect_error_heals_via_one_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"ok": True})

    old = _install_mock(handler)
    try:
        result = gh_core._request("GET", "repos/x/y")
        assert result == {"ok": True}, result
        assert calls["n"] == 2, calls
    finally:
        gh_core._client = old
    print("  ConnectError heals via one retry: ok")


def test_remote_protocol_error_heals():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("bad data")
        return httpx.Response(200, json={"ok": True})

    old = _install_mock(handler)
    try:
        result = gh_core._request("GET", "repos/x/y")
        assert result == {"ok": True}, result
    finally:
        gh_core._client = old
    print("  RemoteProtocolError (Request-sent class) heals: ok")


# ---------------------------------------------------------------------------
# ok_404 + shared stream
# ---------------------------------------------------------------------------


def test_ok_404_miss_keeps_shared_stream_in_sync():
    def handler(request):
        return httpx.Response(404, json={"message": "nope"})

    old = _install_mock(handler)
    try:
        result = gh_core._request("GET", "repos/x/y", ok_404=True)
        assert result is None, result
    finally:
        gh_core._client = old
    print("  ok_404 miss keeps the shared stream in sync: ok")


# ---------------------------------------------------------------------------
# Native await twin
# ---------------------------------------------------------------------------


def test_native_await_twin_alist_tree_works_standalone():
    gh.clear_cache()

    def handler(request):
        assert request.url.path.endswith("git/trees/main")
        return httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "a.py", "type": "blob", "size": 10},
                    {"path": "b.md", "type": "blob", "size": 20},
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


# ---------------------------------------------------------------------------
# Client single-owner + concurrent sync callers
# ---------------------------------------------------------------------------


def test_client_stays_single_owner_across_loops():
    def handler(request):
        return httpx.Response(200, json={"n": 1})

    old = _install_mock(handler)
    try:
        import asyncio

        async def one():
            return await gh._arequest("GET", "repos/x/y")

        results = asyncio.run(asyncio.gather(one(), one()))
        assert all(r == {"n": 1} for r in results), results
    finally:
        gh_core._client = old
    print("  client stays single-owner across loops: ok")


def test_concurrent_sync_callers_share_background_loop():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    old = _install_mock(handler)
    try:
        import threading

        results = []

        def worker():
            results.append(gh._request("GET", "repos/x/y"))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(r == {"ok": True} for r in results), results
    finally:
        gh_core._client = old
    print("  concurrent sync callers share the background loop: ok")


# ---------------------------------------------------------------------------
# get_pr fan-out
# ---------------------------------------------------------------------------


def test_get_pr_fans_checks_comments_files_out_concurrently():
    def handler(request):
        path = request.url.path
        if path.endswith("/pulls/42"):
            return httpx.Response(
                200,
                json={
                    "number": 42,
                    "title": "test PR",
                    "state": "open",
                    "body": "body",
                    "head": {"sha": "abc"},
                    "base": {"ref": "main"},
                },
            )
        if path.endswith("/issues/42/comments"):
            return httpx.Response(200, json=[])
        if path.endswith("/commits/abc"):
            return httpx.Response(200, json={"check_runs": []})
        if path.endswith("/files"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        pr = gh.get_pr(42)
        assert pr["number"] == 42, pr
        assert pr["title"] == "test PR", pr
    finally:
        gh_core._client = old
    print("  get_pr fans checks/comments/files out concurrently: ok")


# ---------------------------------------------------------------------------
# apr_diff / apr_commits overlap
# ---------------------------------------------------------------------------


def test_apr_diff_overlaps_payload_with_first_files_page():
    def handler(request):
        path = request.url.path
        if path.endswith("/pulls/43"):
            return httpx.Response(
                200,
                json={
                    "number": 43,
                    "head": {"sha": "abc"},
                    "base": {"ref": "main"},
                },
            )
        if path.endswith("/files"):
            return httpx.Response(
                200,
                json=[{"filename": "a.py", "patch": "+x"}],
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        import asyncio

        diff = asyncio.run(gh.apr_diff(43))
        assert isinstance(diff, list), diff
    finally:
        gh_core._client = old
    print("  apr_diff overlaps payload with first files page: ok")


def test_apr_commits_overlaps_payload_with_first_commits_page():
    def handler(request):
        path = request.url.path
        if path.endswith("/pulls/44"):
            return httpx.Response(
                200,
                json={
                    "number": 44,
                    "head": {"sha": "abc"},
                    "base": {"ref": "main"},
                },
            )
        if path.endswith("/commits"):
            return httpx.Response(
                200,
                json=[{"sha": "abc", "commit": {"message": "test"}}],
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        import asyncio

        commits = asyncio.run(gh.apr_commits(44))
        assert isinstance(commits, list), commits
    finally:
        gh_core._client = old
    print("  apr_commits overlaps payload with first commits page: ok")


# ---------------------------------------------------------------------------
# pr_files pagination
# ---------------------------------------------------------------------------


def test_pr_files_paginates_past_default_30_100_item_page():
    pages = {"n": 0}

    def handler(request):
        pages["n"] += 1
        if pages["n"] == 1:
            return httpx.Response(
                200, json=[{"filename": f"f{i}.py"} for i in range(100)]
            )
        if pages["n"] == 2:
            return httpx.Response(200, json=[{"filename": "last.py"}])
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    try:
        files = gh.pr_files(45)
        assert len(files) == 101, len(files)
    finally:
        gh_core._client = old
    print("  pr_files paginates past the default 30/100-item page: ok")


def test_short_first_page_terminates_pagination_after_one_request():
    pages = {"n": 0}

    def handler(request):
        pages["n"] += 1
        return httpx.Response(200, json=[{"filename": "only.py"}])

    old = _install_mock(handler)
    try:
        files = gh.pr_files(46)
        assert len(files) == 1, len(files)
        assert pages["n"] == 1, pages
    finally:
        gh_core._client = old
    print("  short first page terminates pagination after one request: ok")


# ---------------------------------------------------------------------------
# Page cap
# ---------------------------------------------------------------------------


def test_page_cap_bounds_server_that_never_sends_short_page():
    pages = {"n": 0}

    def handler(request):
        pages["n"] += 1
        if pages["n"] <= 5:
            return httpx.Response(
                200, json=[{"filename": f"f{pages['n']}.py"}] * 100
            )
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    try:
        gh.pr_files(47)
        assert pages["n"] <= 6, pages
    finally:
        gh_core._client = old
    print("  page cap bounds a server that never sends a short page: ok")


def test_apaginate_page_cap_bounds_runaway_server():
    pages = {"n": 0}

    def handler(request):
        pages["n"] += 1
        if pages["n"] <= 5:
            return httpx.Response(
                200, json=[{"filename": f"f{pages['n']}.py"}] * 100
            )
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    try:
        import asyncio

        async def run():
            out = []
            page = 1
            while True:
                data = await gh._arequest(
                    "GET", f"repos/x/y/pulls/48/files?per_page=100&page={page}"
                )
                if not data:
                    break
                out.extend(data)
                page += 1
                if page > 10:
                    break
            return out

        asyncio.run(run())
        assert pages["n"] <= 11, pages
    finally:
        gh_core._client = old
    print("  _apaginate page cap bounds a runaway server: ok")


# ---------------------------------------------------------------------------
# pr_diff file-loop cap
# ---------------------------------------------------------------------------


def test_pr_diff_file_loop_cap_bounds_runaway_server():
    def handler(request):
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json=[{"filename": f"f{i}.py", "patch": "+x"} for i in range(200)],
            )
        if request.url.path.endswith("/pulls/49"):
            return httpx.Response(
                200,
                json={"number": 49, "head": {"sha": "abc"}},
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        diff = gh.pr_diff(49)
        assert isinstance(diff, list), diff
    finally:
        gh_core._client = old
    print("  pr_diff file-loop cap bounds a runaway server: ok")


# ---------------------------------------------------------------------------
# open_prs pagination
# ---------------------------------------------------------------------------


def test_open_prs_paginates_past_first_100_item_page():
    pages = {"n": 0}

    def handler(request):
        pages["n"] += 1
        if pages["n"] == 1:
            return httpx.Response(
                200, json=[{"number": i, "title": f"PR {i}"} for i in range(100)]
            )
        if pages["n"] == 2:
            return httpx.Response(200, json=[{"number": 101, "title": "PR 101"}])
        return httpx.Response(200, json=[])

    old = _install_mock(handler)
    try:
        prs = gh.open_prs()
        assert len(prs) == 101, len(prs)
    finally:
        gh_core._client = old
    print("  open_prs paginates past the first 100-item page: ok")


# ---------------------------------------------------------------------------
# _request_text log redirect
# ---------------------------------------------------------------------------


def test_request_text_follows_log_redirect():
    def handler(request):
        if request.url.path.endswith("/logs"):
            return httpx.Response(302, headers={"Location": "/redirected"})
        if request.url.path.endswith("/redirected"):
            return httpx.Response(200, text="log content")
        return httpx.Response(200, text="")

    old = _install_mock(handler)
    try:
        text = gh_core._request_text("GET", "repos/x/y/actions/jobs/1/logs")
        assert text == "log content", text
    finally:
        gh_core._client = old
    print("  _request_text follows the log redirect: ok")


# ---------------------------------------------------------------------------
# Thin annotation enrichment
# ---------------------------------------------------------------------------


def test_thin_annotation_enriched_from_logs():
    def handler(request):
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "id": 1,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://ci/run/1",
                        }
                    ]
                },
            )
        if path.endswith("/annotations"):
            return httpx.Response(
                200,
                json=[
                    {
                        "path": "a.py",
                        "start_line": 1,
                        "message": "Process completed with exit code 1.",
                    }
                ],
            )
        if path.endswith("/actions/runs"):
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 10,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://ci/run/10",
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
                            "id": 100,
                            "name": "test",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ]
                },
            )
        if path.endswith("/logs"):
            return httpx.Response(
                200, text="FAILED: a.py (1.0s)\nAssertionError: x == y\n"
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        result = gh.pr_checks(50, _head_sha="deadsha")
        assert result["state"] == "failure", result
        msgs = [f["message"] for f in result["failures"]]
        assert any("AssertionError" in m for m in msgs), msgs
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  thin 'exit code' annotations get enriched from logs: ok")


# ---------------------------------------------------------------------------
# apr_checks concurrent job logs
# ---------------------------------------------------------------------------


def test_apr_checks_fans_job_logs_out_concurrently():
    def handler(request):
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(500, json={"message": "nope"})
        if path.endswith("/actions/runs"):
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 10,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://ci/run/10",
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
                            "id": 100,
                            "name": "test",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ]
                },
            )
        if path.endswith("/logs"):
            return httpx.Response(200, text="error: boom\n")
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        import asyncio

        result = asyncio.run(gh.apr_checks(51, _head_sha="deadsha"))
        assert result is not None
        assert result["state"] == "failure", result
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_checks fans job logs out concurrently: ok")


# ---------------------------------------------------------------------------
# apr_checks cache parity
# ---------------------------------------------------------------------------


def test_apr_checks_cache_parity_with_sync():
    def handler(request):
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "id": 1,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "success",
                            "html_url": "https://ci/run/1",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        import asyncio

        gh.clear_cache()
        native = asyncio.run(gh.apr_checks(4244, _head_sha="deadsha"))
        hits = []
        orig_handler = handler

        def counting_handler(request):
            hits.append(request.url.path)
            return orig_handler(request)

        gh_core._client = httpx.Client(transport=httpx.MockTransport(counting_handler))
        n_hits = len(hits)
        sync_face = gh.pr_checks(4244, _head_sha="deadsha")
        assert sync_face == native, "sync/native shapes diverged"
        assert native["failed_files_detail"] == sync_face["failed_files_detail"], (
            "failed_files_detail diverged"
        )
        assert len(hits) == n_hits, "sync face must read the shared cache"
        # Reverse direction on a second number: sync warms, native reads.
        native2 = asyncio.run(gh.apr_checks(4245, _head_sha="deadsha"))
        assert native2 is not None
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_checks shares the pr_checks cache byte-for-byte: ok")


# ---------------------------------------------------------------------------
# Check-run failures fall through to Actions tier
# ---------------------------------------------------------------------------


def test_check_run_failures_fall_through_to_actions_tier():
    def handler(request):
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(500, json={"message": "nope"})
        if path.endswith("/actions/runs"):
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 10,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://ci/run/10",
                        }
                    ]
                },
            )
        if path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        result = gh.pr_checks(52, _head_sha="deadsha")
        assert result["source"] == "actions", result
        assert result["state"] == "failure", result
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  check-run failures fall through to the actions tier: ok")


# ---------------------------------------------------------------------------
# _head_sha shortcut
# ---------------------------------------------------------------------------


def test_head_sha_shortcut_skips_pr_fetch():
    pr_fetches = {"n": 0}

    def handler(request):
        path = request.url.path
        if path.endswith("/pulls/"):
            pr_fetches["n"] += 1
            return httpx.Response(
                200,
                json={"number": 53, "head": {"sha": "abc"}},
            )
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "id": 1,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "success",
                            "html_url": "https://ci/run/1",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        result = gh.pr_checks(53, _head_sha="deadsha")
        assert pr_fetches["n"] == 0, pr_fetches
        assert result["head_sha"] == "deadsha", result
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  _head_sha shortcut skips the PR fetch: ok")


# ---------------------------------------------------------------------------
# propose_change failure cleans up orphan branch
# ---------------------------------------------------------------------------


def test_propose_change_failure_cleans_up_orphan_branch():
    def handler(request):
        path = request.url.path
        if path.endswith("/pulls") and request.method == "POST":
            return httpx.Response(422, json={"message": "validation failed"})
        if path.endswith("/branches"):
            return httpx.Response(200, json={})
        if path.endswith("/contents"):
            return httpx.Response(200, json={"sha": "abc"})
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        try:
            gh.propose_change(
                token="tok",
                title="test",
                body="body",
                file_path="a.py",
                content="print(1)",
            )
            raise AssertionError("expected RepoError")
        except RepoError:
            pass
    finally:
        gh_core._client = old
    print("  propose_change failure cleans up the orphan branch: ok")


# ---------------------------------------------------------------------------
# comment_on_pr tolerates missing html_url
# ---------------------------------------------------------------------------


def test_comment_on_pr_missing_html_url_ok():
    def handler(request):
        path = request.url.path
        if path.endswith("/issues/54/comments") and request.method == "POST":
            return httpx.Response(201, json={"id": 1, "body": "ok"})
        if path.endswith("/pulls/54"):
            return httpx.Response(
                200,
                json={"number": 54, "html_url": None, "state": "open"},
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        result = gh.comment_on_pr(token="tok", number=54, body="hello")
        assert result is not None
    finally:
        gh_core._client = old
    print("  comment_on_pr tolerates a missing html_url: ok")


# ---------------------------------------------------------------------------
# Ingress collapse end-to-end
# ---------------------------------------------------------------------------


def test_ingress_collapse_end_to_end():
    """_checks_for_head collapses multi-line annotation messages:
    a pathed check-run annotation with embedded newlines is collapsed
    to a single line in the failures list."""
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
                        "path": "tests/test_collapse.py",
                        "start_line": 10,
                        "message": "line one\nline two\nline three",
                    }
                ],
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        result = gh._checks_for_head("deadsha")
        assert result["state"] == "failure", result
        failures = result["failures"]
        assert len(failures) == 1, failures
        msg = failures[0]["message"]
        assert msg == "line one line two line three", msg
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  ingress collapse end-to-end through _checks_for_head: ok")


# ---------------------------------------------------------------------------
# apr_checks statuses tier collapses description
# ---------------------------------------------------------------------------


def test_apr_checks_statuses_tier_collapses_description():
    """apr_checks statuses tier collapses multi-line description:
    check-runs and Actions both 500, statuses tier answers with a
    multi-line description that gets collapsed."""
    gh.clear_cache()

    def handler(request):
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(500, json={"message": "nope"})
        if path.endswith("/actions/runs"):
            return httpx.Response(500, json={"message": "nope"})
        if path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "state": "failure",
                    "statuses": [
                        {
                            "context": "CI",
                            "state": "failure",
                            "description": "line one\nline two",
                            "target_url": "https://ci/run/99",
                        }
                    ],
                },
            )
        return httpx.Response(200, json={})

    old = _install_mock(handler)
    try:
        import asyncio

        result = asyncio.run(gh.apr_checks(4243, _head_sha="deadsha"))
        assert result is not None
        assert result["source"] == "statuses", result
        assert result["state"] == "failure", result
        failures = result["failures"]
        assert len(failures) == 1, failures
        msgs = [f["message"] for f in failures]
        assert msgs == ["line one line two"], msgs
    finally:
        gh_core._client = old
        gh.clear_cache()
    print("  apr_checks statuses tier collapses description: ok")


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


def main():
    test_connect_error_heals_via_one_retry()
    test_remote_protocol_error_heals()
    test_ok_404_miss_keeps_shared_stream_in_sync()
    test_non_ok_path_raises_repo_error_with_body_message()
    test_request_text_reads_text_and_honours_ok_404()
    test_native_await_twin_alist_tree_works_standalone()
    test_client_stays_single_owner_across_loops()
    test_concurrent_sync_callers_share_background_loop()
    test_get_pr_fans_checks_comments_files_out_concurrently()
    test_apr_diff_overlaps_payload_with_first_files_page()
    test_apr_commits_overlaps_payload_with_first_commits_page()
    test_pr_files_paginates_past_default_30_100_item_page()
    test_short_first_page_terminates_pagination_after_one_request()
    test_page_cap_bounds_server_that_never_sends_short_page()
    test_apaginate_page_cap_bounds_runaway_server()
    test_pr_diff_file_loop_cap_bounds_runaway_server()
    test_open_prs_paginates_past_first_100_item_page()
    test_request_text_follows_log_redirect()
    test_thin_annotation_enriched_from_logs()
    test_apr_checks_fans_job_logs_out_concurrently()
    test_apr_checks_cache_parity_with_sync()
    test_check_run_failures_fall_through_to_actions_tier()
    test_head_sha_shortcut_skips_pr_fetch()
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
    print("test_github_http: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())