"""Stacked-PR phase 1 pins (proposal #660): the /prs stacked chip.

Pure display tests - no database writes, no network.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_stacked_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import setup  # noqa: E402

setup()

import github  # noqa: E402
from viewer._pr_helpers import _prs_stacked_chip  # noqa: E402


def test_stacked_chip_quiet_on_main_base():
    assert _prs_stacked_chip({"base": ""}) == ""
    assert _prs_stacked_chip({}) == ""
    assert _prs_stacked_chip({"base": github.base_branch()}) == ""


def test_stacked_chip_marks_foreign_base():
    html = _prs_stacked_chip({"base": "claim/12/34/parent"})
    assert "stacked" in html
    assert "claim/12/34/parent" in html


def test_stacked_chip_escapes_base():
    html = _prs_stacked_chip({"base": "<script>alert(1)</script>"})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


if __name__ == "__main__":
    test_stacked_chip_quiet_on_main_base()
    print("ok - test_stacked_chip_quiet_on_main_base")
    test_stacked_chip_marks_foreign_base()
    print("ok - test_stacked_chip_marks_foreign_base")
    test_stacked_chip_escapes_base()
    print("ok - test_stacked_chip_escapes_base")
