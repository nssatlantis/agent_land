"""Manual latency harness for the async GitHub surface - NOT auto-run by
tests/run_all.py (the discovery glob only picks up test_*.py files).

Two modes:

  default        - httpx.MockTransport with an artificial per-request delay,
                   proving the fan-out structure: aget_pr's wave-2 reads
                   overlap, so its wall time approaches max(delay) instead
                   of sum(delays).
  BENCH_LIVE=1   - against the real GitHub API using GITHUB_TOKEN (needs
                   network; never runs in CI by default). Compares the sync
                   get_pr chain against the native aget_pr twin on a real
                   pull request (BENCH_PR, default 1).

Run:  python tests/benchmark_github.py
"""

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent / "github.py"
_spec = importlib.util.spec_from_file_location("agentland_root_github", _ROOT)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)

gh.GITHUB_TOKEN = os.environ.get("BENCH_TOKEN", "bench-token")
DELAY_S = float(os.environ.get("BENCH_DELAY_MS", "40")) / 1000.0


def _install_delay_mock(delay: float):
    hits: list[str] = []

    async def handler(request):
        await asyncio.sleep(delay)
        hits.append(request.url.path)
        if request.url.path.endswith("/pulls/9001"):
            return httpx.Response(200, json={
                "number": 9001, "title": "bench", "body": "",
                "head": {"ref": "b", "sha": "s"}, "base": {"ref": "main"},
                "user": {"login": "u"}, "state": "open",
                "created_at": "2026-08-24T00:00:00Z",
                "html_url": "https://github.com/x/y/pull/9001",
            })
        return httpx.Response(200, json=[])

    old = gh._client
    gh._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )
    return hits, old


def bench_fanout() -> None:
    """aget_pr issues 4 requests (PR + two comment sources + files); the
    last three overlap. Sequential cost ~= 4*delay, fan-out ~= 2*delay."""
    hits, old = _install_delay_mock(DELAY_S)
    stub_checks = gh._checks_for_head
    gh._checks_for_head = lambda sha: {"state": "unknown", "source": "stub"}
    try:
        # sequential reference: same four requests, one at a time
        seq_paths = [
            "/repos/nssatlantis/agent_land/pulls/9001",
            "/repos/nssatlantis/agent_land/issues/9001/comments",
            "/repos/nssatlantis/agent_land/pulls/9001/comments",
            "/repos/nssatlantis/agent_land/pulls/9001/files",
        ]
        t0 = time.perf_counter()
        for p in seq_paths:
            asyncio.run(gh._arequest("GET", p))
        seq_ms = (time.perf_counter() - t0) * 1000

        gh.clear_cache()
        t1 = time.perf_counter()
        result = asyncio.run(gh.aget_pr(9001))
        fan_ms = (time.perf_counter() - t1) * 1000

        assert result["number"] == 9001 and len(hits) >= 4
        print("  get_pr requests      : 4")
        print(f"  sequential (naive)   : {seq_ms:7.1f} ms")
        print(f"  fan-out (native)     : {fan_ms:7.1f} ms")
        print(f"  latency saved        : {seq_ms - fan_ms:7.1f} ms  "
              f"({seq_ms / max(fan_ms, 0.001):.2f}x)")
    finally:
        gh._checks_for_head = stub_checks
        gh._client = old
        gh.clear_cache()


def bench_live(pr_number: int) -> None:
    """Real-network comparison of the sync chain vs the native twin."""
    t0 = time.perf_counter()
    sync_result = gh.get_pr(pr_number)
    sync_ms = (time.perf_counter() - t0) * 1000
    gh.clear_cache()

    t1 = time.perf_counter()
    async_result = asyncio.run(gh.aget_pr(pr_number))
    async_ms = (time.perf_counter() - t1) * 1000

    assert sync_result["number"] == async_result["number"]
    assert len(sync_result["comments"]) == len(async_result["comments"])
    assert len(sync_result["files"]) == len(async_result["files"])
    print(f"  live get_pr #{pr_number}: sync {sync_ms:.0f} ms | "
          f"native {async_ms:.0f} ms ({sync_ms / max(async_ms, 0.001):.2f}x)")


def main():
    print(f"benchmark_github: per-request delay {DELAY_S*1000:.0f} ms (MockTransport)")
    bench_fanout()
    if os.environ.get("BENCH_LIVE") == "1" and os.environ.get("GITHUB_TOKEN"):
        gh.clear_cache()
        try:
            bench_live(int(os.environ.get("BENCH_PR", "1")))
        except Exception as exc:
            print(f"  live mode skipped: {type(exc).__name__}: {exc}")
    else:
        print("  live mode off (set BENCH_LIVE=1 + GITHUB_TOKEN to compare on real API)")
    print("benchmark_github: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
