"""Stacked-PR pins (proposal #660): the /prs stacked chip, the live-base
helper, the stack-step event text, and the shrink-gate base.

Display + pure-helper tests - no database writes, no network.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_stacked_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import setup  # noqa: E402

setup()

import github  # noqa: E402
import server.ci_runner._trees as trees  # noqa: E402
from viewer._events import _event_description  # noqa: E402

try:
    from viewer._pr_helpers import _prs_stacked_chip  # noqa: E402

    _HAS_CHIP = True
except ImportError:  # domain: degrade-silently - chip lands with PR #1377 (Phase 1)
    _HAS_CHIP = False


def test_stacked_chip_quiet_on_main_base():
    if not _HAS_CHIP:
        print("skip - chip lands with PR #1377")
        return
    assert _prs_stacked_chip({"base": ""}) == ""
    assert _prs_stacked_chip({}) == ""
    assert _prs_stacked_chip({"base": github.base_branch()}) == ""


def test_stacked_chip_marks_foreign_base():
    if not _HAS_CHIP:
        print("skip - chip lands with PR #1377")
        return
    html = _prs_stacked_chip({"base": "claim/12/34/parent"})
    assert "stacked" in html
    assert "claim/12/34/parent" in html


def test_stacked_chip_escapes_base():
    if not _HAS_CHIP:
        print("skip - chip lands with PR #1377")
        return
    html = _prs_stacked_chip({"base": "<script>alert(1)</script>"})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_live_pr_base_reads_row():
    raw = {"base": {"ref": "claim/12/34/parent"}}
    with mock.patch.object(github, "_pr_raw", return_value=raw):
        assert trees._live_pr_base(99) == "claim/12/34/parent"


def test_live_pr_base_fails_open():
    with mock.patch.object(github, "_pr_raw", side_effect=Exception("404")):
        assert trees._live_pr_base(99) == github.base_branch()
    with mock.patch.object(github, "_pr_raw", return_value={}):
        assert trees._live_pr_base(99) == github.base_branch()
    evil = {"base": {"ref": "--upload-pack=touch"}}
    with mock.patch.object(github, "_pr_raw", return_value=evil):
        assert trees._live_pr_base(99) == github.base_branch()


def test_stack_step_event_text():
    stacked = {
        "kind": "pr_merged",
        "target_type": "pr",
        "target_id": 7,
        "detail": {"pr_number": 7, "base": "claim/1/2/x", "main_merge": False},
    }
    text = _event_description(stacked)
    assert "stack step" in text
    assert "claim/1/2/x" in text
    normal = {
        "kind": "pr_merged",
        "target_type": "pr",
        "target_id": 7,
        "detail": {"pr_number": 7},
    }
    assert "stack step" not in _event_description(normal)
    assert "merged" in _event_description(normal)


def test_shrink_uses_pr_base_env():
    import tests.test_pr_diff_shrink as shrink

    seen = []

    def fake_git(*args):
        seen.append(args)
        if args[0] == "merge-base":
            return "abc123\n"
        if args[0] == "diff":
            return ""
        return None

    real_git = shrink._git
    real_ref = os.environ.get("GITHUB_BASE_REF")
    shrink._git = fake_git
    try:
        os.environ["GITHUB_BASE_REF"] = "claim/12/34/parent"
        assert shrink._merge_base_diff() == []
        assert seen[0] == ("merge-base", "HEAD", "origin/claim/12/34/parent"), seen
        seen.clear()
        if "GITHUB_BASE_REF" in os.environ:
            del os.environ["GITHUB_BASE_REF"]
        assert shrink._merge_base_diff() == []
        assert seen[0] == ("merge-base", "HEAD", "origin/main"), seen
    finally:
        shrink._git = real_git
        if real_ref is None:
            os.environ.pop("GITHUB_BASE_REF", None)
        else:
            os.environ["GITHUB_BASE_REF"] = real_ref


if __name__ == "__main__":
    test_stacked_chip_quiet_on_main_base()
    print("ok - test_stacked_chip_quiet_on_main_base")
    test_stacked_chip_marks_foreign_base()
    print("ok - test_stacked_chip_marks_foreign_base")
    test_stacked_chip_escapes_base()
    print("ok - test_stacked_chip_escapes_base")
    test_live_pr_base_reads_row()
    print("ok - test_live_pr_base_reads_row")
    test_live_pr_base_fails_open()
    print("ok - test_live_pr_base_fails_open")
    test_stack_step_event_text()
    print("ok - test_stack_step_event_text")
    test_shrink_uses_pr_base_env()
    print("ok - test_shrink_uses_pr_base_env")
