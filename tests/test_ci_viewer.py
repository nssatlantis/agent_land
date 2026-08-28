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
import events  # noqa: E402, I001

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


if __name__ == "__main__":
    test_ci_page_native_tab_and_top_strip()
    test_ci_page_branch_tab_filters()
    test_ci_page_garbage_mode_clamps_to_native()
    test_ci_page_timeline_rows_show_badge_duration_failed_files()
    test_ci_page_branch_rows_show_pr_link_and_timeout()
    test_ci_top_strip_empty()
    test_ci_badge_variants()
    print("test_ci_viewer: all assertions passed")
