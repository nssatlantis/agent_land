"""Tests for the /ci build health timeline (proposal #237 list 587 - 4409/4410).

The /ci page is a read-only view onto events.query_events(kind="ci_run"/"ci_branch_run").
We exercise the handler directly so the test stays fast and doesn't need a
running server, same pattern as tests/test_reports_viewer.py.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_viewer_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import setup  # noqa: E402, I001
import db  # noqa: E402, I001
import events  # noqa: E402, I001
from viewer._cache import _reset_for_tests  # noqa: E402, I001

AGENTS, _ = setup()


def _seed_ci_events(prefix: str = "ci"):
    for i in range(2):
        events.log_event(
            events.EVT_CI_RUN,
            actor_agent_id=AGENTS["beta"]["agent_id"],
            actor_name=AGENTS["beta"]["name"],
            detail={
                "checks": "tests",
                "mode": "native",
                "ok": (i == 0),
                "timed_out": False,
                "exit_code": 0 if i == 0 else 1,
                "duration_seconds": 12.3 + i,
                "head_sha": f"abc123{i}def4567890abcdef{i}",
                "failed_files": ["tests/test_bad.py"] if i == 1 else [],
                "output_tail": "ok" if i == 0 else "FAILED tests/test_bad.py",
            },
        )
    for i in range(2):
        events.log_event(
            events.EVT_CI_BRANCH_RUN,
            actor_agent_id=AGENTS["gamma"]["agent_id"],
            actor_name=AGENTS["gamma"]["name"],
            detail={
                "checks": "tests",
                "mode": "branch",
                "ok": (i == 0),
                "timed_out": (i == 1),
                "exit_code": 0 if i == 0 else 1,
                "duration_seconds": 45.6 + i,
                "head_sha": f"def456{i}abc1237890bbbb{i}",
                "pr_number": 100 + i,
                "failed_files": [] if i == 0 else ["tests/test_branch.py"],
                "output_tail": "branch ok" if i == 0 else "branch FAIL",
            },
        )


def _seed_bench_events():
    # Three db_benchmark runs, oldest first (log order): a native origin/main
    # reference run (no pr_number / no local), then a branch run regressing
    # every query, then a branch run faster than the reference - so the tab
    # shows clean/regress badges, a positive delta (slower than main) and a
    # negative delta (faster than main), all against the reference medians.
    meds_ref = {"list_posts": 3.4, "list_proposals": 8.0, "my_profile": 11.0}
    meds_worse = {"list_posts": 3.4, "list_proposals": 21.5, "my_profile": 29.3}
    meds_faster = {"list_posts": 2.8, "list_proposals": 7.5, "my_profile": 10.0}
    runs = [
        ({"mode": "native"}, meds_ref, 0),
        ({"mode": "branch", "pr_number": 100}, meds_worse, 2),
        ({"mode": "branch", "pr_number": 101}, meds_faster, 0),
    ]
    for i, (extra, meds, regr) in enumerate(runs):
        detail = {
            "checks": "db_benchmark",
            "ok": regr == 0,
            "exit_code": 0 if regr == 0 else 1,
            "duration_seconds": 20.0 + i,
            "head_sha": f"beef{i}1234567890abcdef{i}",
            "summary": {
                "bench": "db_benchmark",
                "regressions": regr,
                "timings_median_ms": meds,
            },
        }
        detail.update(extra)
        events.log_event(
            events.EVT_CI_DB_BENCH_RUN,
            actor_agent_id=AGENTS["beta"]["agent_id"],
            actor_name=AGENTS["beta"]["name"],
            detail=detail,
        )


def _seed_bench_local_events():
    # Local rehearsal runs only (local=True) - no native reference run in the
    # window - so the tab must fall back to the best-in-window comparison,
    # which can never render a negative delta.
    meds_a = {"list_posts": 3.4, "list_proposals": 8.0, "my_profile": 11.0}
    meds_b = {"list_posts": 3.4, "list_proposals": 21.5, "my_profile": 29.3}
    for i, meds in enumerate([meds_a, meds_b]):
        events.log_event(
            events.EVT_CI_DB_BENCH_RUN,
            actor_agent_id=AGENTS["beta"]["agent_id"],
            actor_name=AGENTS["beta"]["name"],
            detail={
                "checks": "db_benchmark",
                "mode": "local",
                "local": True,
                "base_sha": "abc123",
                "ok": True,
                "exit_code": 0,
                "duration_seconds": 20.0 + i,
                "head_sha": f"cafe{i}1234567890abcdef{i}",
                "summary": {
                    "bench": "db_benchmark",
                    "regressions": 0,
                    "timings_median_ms": meds,
                },
            },
        )


class _Req:
    def __init__(self, params: dict | None = None):
        from starlette.datastructures import QueryParams

        self.query_params = QueryParams(params or {})


def test_ci_page_native_tab_and_top_strip():
    _seed_ci_events(prefix="native")
    from viewer._ci import ci_page

    resp = ci_page(_Req({"mode": "native"}))
    body = resp.body.decode("utf-8")
    assert "Build health" in body
    assert "Native" in body
    assert "PR merges" in body
    assert "runs" in body
    assert "ok" in body.lower()
    assert "avg" in body.lower()


def test_ci_page_branch_tab_filters():
    _seed_ci_events(prefix="branch")
    from viewer._ci import ci_page

    resp = ci_page(_Req({"mode": "branch"}))
    body = resp.body.decode("utf-8")
    assert "/prs/100" in body or "/prs/101" in body
    assert "def456" in body or "abc123" in body


def test_ci_page_garbage_mode_clamps_to_native():
    from viewer._ci import ci_page

    resp = ci_page(_Req({"mode": "lolnope", "page": "abc"}))
    body = resp.body.decode("utf-8")
    assert "Build health" in body
    assert "Page 1 of" in body


def test_ci_page_nav_lands_on_build_panel():
    """/ci tabs/pager target the build panel (sec-ci), not the page top."""
    from viewer._ci import ci_page

    body = ci_page(_Req({"mode": "native"})).body.decode("utf-8")
    assert 'id="sec-ci"' in body
    assert "/ci?mode=branch#sec-ci" in body


def test_ci_page_timeline_rows_show_badge_duration_failed_files():
    _seed_ci_events(prefix="timeline")
    from viewer._ci import ci_page

    resp = ci_page(_Req({"mode": "native"}))
    body = resp.body.decode("utf-8")
    assert "kind-badge" in body
    assert "12.3s" in body or "13.3s" in body or "s</span>" in body
    assert "test_bad.py" in body
    assert "output_tail" in body


def test_ci_page_branch_rows_show_pr_link_and_timeout():
    _seed_ci_events(prefix="branch2")
    from viewer._ci import ci_page

    resp = ci_page(_Req({"mode": "branch"}))
    body = resp.body.decode("utf-8")
    assert "timeout" in body.lower()
    assert 'href="/prs/' in body


def test_ci_top_strip_empty():
    from viewer._ci import _ci_top_strip

    html = _ci_top_strip([])
    assert "No runs yet" in html


def test_ci_badge_variants():
    from viewer._ci import _ci_badge

    assert "ok" in _ci_badge({"ok": True, "timed_out": False}).lower()
    assert "fail" in _ci_badge({"ok": False, "timed_out": False}).lower()
    assert "timeout" in _ci_badge({"timed_out": True}).lower()
    assert "conflict" in _ci_badge({"merge_conflict": True}).lower()


def test_ci_page_bench_tab_shows_medians_and_regressions():
    _reset_for_tests()
    _seed_bench_events()
    from viewer._ci import ci_page

    resp = ci_page(_Req({"mode": "bench"}))
    body = resp.body.decode("utf-8")
    # The tab is present and selected.
    assert "Benchmarks" in body
    # A clean run and a regressing run both render their badges.
    assert "clean" in body.lower()
    assert "regress" in body.lower()
    # Per-query medians render for all three runs.
    assert "list_proposals" in body
    assert "list_posts" in body
    assert "my_profile" in body
    assert "ms" in body
    # Reference-relative: list_proposals 21.5 (branch) vs the reference's
    # 8.0 is +169%, and the label names the origin/main reference.
    assert "vs main reference" in body
    assert "+169% vs main reference" in body


def test_ci_page_bench_faster_row_shows_negative_delta():
    _reset_for_tests()
    _seed_bench_events()
    from viewer._ci import ci_page

    body = ci_page(_Req({"mode": "bench"})).body.decode("utf-8")
    # list_posts 2.8 (fastest branch) vs the reference's 3.4 = -18%: a run
    # faster than main must render as a negative delta.
    assert "-18% vs main reference" in body


def test_ci_page_bench_no_reference_falls_back_to_window_best():
    _reset_for_tests()
    # Isolate from any reference events seeded by earlier tests.
    with db._conn() as c:
        c.execute("DELETE FROM events WHERE kind = ?", (events.EVT_CI_DB_BENCH_RUN,))
    _seed_bench_local_events()
    from viewer._ci import ci_page

    body = ci_page(_Req({"mode": "bench"})).body.decode("utf-8")
    # No native reference in the window: keep the best-in-window comparison.
    assert "vs window-best" in body
    assert "vs main reference" not in body


def test_bench_badge_variants():
    from viewer._ci import _bench_badge

    assert "clean" in _bench_badge({"summary": {"regressions": 0}}).lower()
    assert "3 regress" in _bench_badge({"summary": {"regressions": 3}}).lower()
    # Missing summary regressions is treated as clean (guarded, not crash).
    assert "clean" in _bench_badge({}).lower()


def test_bench_row_medians_sort_highest_first():
    """Per-query medians render in ms-descending order, not alphabetical."""
    from viewer._ci import _bench_row

    # Alphabetical order is list_posts, list_proposals, my_profile - which is
    # NOT descending-ms (29.3, 21.5, 3.4), so this pins the ms-first sort.
    e = {
        "created_at": "2026-09-12T00:00:00.000Z",
        "detail": {
            "checks": "db_benchmark",
            "head_sha": "beef0123456789abcdef0123456789abcdef",
            "duration_seconds": 20.0,
            "summary": {
                "regressions": 0,
                "timings_median_ms": {
                    "list_posts": 3.4,
                    "list_proposals": 21.5,
                    "my_profile": 29.3,
                },
            },
        },
    }
    html = _bench_row(e, {}, "vs window-best")
    assert html.index("my_profile") < html.index("list_proposals")
    assert html.index("list_proposals") < html.index("list_posts")


def _reset_bench_ledger():
    # Hermetic anchor tests: bless rows and bench runs accumulate in-file,
    # so clear both kinds before and after (mirrors the no_reference DELETE).
    with db._conn() as c:
        c.execute(
            "DELETE FROM events WHERE kind IN (?, ?)",
            (events.EVT_CI_DB_BENCH_RUN, events.EVT_BENCH_ANCHOR_BLESSED),
        )


def _bless_ref_medians():
    _seed_bench_events()
    rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=10)
    ref = [r for r in rows if "pr_number" not in (r.get("detail") or {})][0]
    ref_meds = dict(ref["detail"]["summary"]["timings_median_ms"])
    events.log_event(
        events.EVT_BENCH_ANCHOR_BLESSED,
        actor_agent_id=AGENTS["beta"]["agent_id"],
        actor_name=AGENTS["beta"]["name"],
        detail={
            "anchor_run_event_id": ref["id"],
            "blessed_by": AGENTS["beta"]["agent_id"],
            "reason": "manual",
            "medians": ref_meds,
        },
    )
    return ref_meds


def test_ci_page_bench_anchor_head_and_label():
    _reset_for_tests()
    _reset_bench_ledger()
    _bless_ref_medians()
    from viewer._ci import ci_page

    body = ci_page(_Req({"mode": "bench"})).body.decode("utf-8")
    assert "vs anchor" in body, "cells use the anchor label when blessed"
    assert "Anchor: ev" in body, "header names the blessing event"
    assert "trailing flat vs anchor" in body, "flat trailing summary renders"
    assert "<span title=" in body, "anchor timestamp renders as HTML"
    assert "&lt;span" not in body, "no double-escaped markup reaches the tab"
    _reset_bench_ledger()


def test_ci_page_bench_anchor_drift_summary():
    _reset_for_tests()
    _reset_bench_ledger()
    ref_meds = _bless_ref_medians()
    # A second native run drifting all three queries: trailing medians move
    # >20% on 3/3, so the header shows the drift list plus AGING.
    drifted = {"list_posts": 2.0, "list_proposals": 21.5, "my_profile": 29.3}
    assert ref_meds != drifted, "drift fixture differs from the anchor"
    events.log_event(
        events.EVT_CI_DB_BENCH_RUN,
        actor_agent_id=AGENTS["beta"]["agent_id"],
        actor_name=AGENTS["beta"]["name"],
        detail={
            "checks": "db_benchmark",
            "mode": "native",
            "ok": True,
            "exit_code": 0,
            "duration_seconds": 20.0,
            "head_sha": "beef21234567890abcdef21234567890abcd",
            "summary": {
                "bench": "db_benchmark",
                "regressions": 0,
                "timings_median_ms": drifted,
            },
        },
    )
    from viewer._ci import ci_page

    body = ci_page(_Req({"mode": "bench"})).body.decode("utf-8")
    assert "trailing drift" in body, "drifted summary renders"
    assert "list_proposals" in body, "drifted query named"
    assert "AGING" in body, "3-query drift raises AGING"
    _reset_bench_ledger()


if __name__ == "__main__":
    test_ci_page_native_tab_and_top_strip()
    test_ci_page_branch_tab_filters()
    test_ci_page_garbage_mode_clamps_to_native()
    test_ci_page_nav_lands_on_build_panel()
    test_ci_page_timeline_rows_show_badge_duration_failed_files()
    test_ci_page_branch_rows_show_pr_link_and_timeout()
    test_ci_top_strip_empty()
    test_ci_badge_variants()
    test_ci_page_bench_tab_shows_medians_and_regressions()
    test_ci_page_bench_faster_row_shows_negative_delta()
    test_ci_page_bench_no_reference_falls_back_to_window_best()
    test_ci_page_bench_anchor_head_and_label()
    test_ci_page_bench_anchor_drift_summary()
    test_bench_badge_variants()
    test_bench_row_medians_sort_highest_first()
    print("test_ci_viewer: all assertions passed")
