"""Tests for the viewer's safe markdown renderer (_markdown in viewer/_utils.py).

Focuses on block-structure edges that previously produced malformed HTML - in
particular a fenced code block appearing immediately after a list with no
blank line, where the <pre> used to nest under the still-open <ul>/<ol>.
"""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# viewer/_utils.py is pure (imports only config), but importing it through the
# `viewer` package pulls in the whole server app (regular cycles on __init__).
# Load the module file directly so this stays a lightweight unit test.
_UTILS_PATH = Path(__file__).resolve().parent.parent / "viewer" / "_utils.py"
_spec = importlib.util.spec_from_file_location("viewer_utils", _UTILS_PATH)
_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_utils)
_markdown = _utils._markdown


def test_list_closed_before_code_fence():
    """A fence right after a list (no blank line) must close the list first,
    so the <pre> is a sibling of the <ul> rather than nested inside it."""
    src = "- one\n- two\n```\ncode\n```\n- three"
    out = _markdown(src)
    ul = out.find("<ul>")
    pre = out.find("<pre><code>")
    ul_close = out.find("</ul>")
    assert ul != -1, "list opens"
    assert pre > ul, "code block comes after the list opens"
    assert ul_close < pre, "the list is closed BEFORE the code block starts"
    # the trailing '- three' reopens a fresh list
    assert out.count("<ul>") == 2, "the trailing item starts its own list"


def test_list_closed_by_blank_line_still_works():
    src = "- one\n- two\n\nparagraph"
    out = _markdown(src)
    assert out.count("<ul>") == 1
    assert out.count("</ul>") == 1
    assert "<p>paragraph</p>" in out


def test_plain_fence_renders():
    src = "```\nprint('hi')\n```"
    out = _markdown(src)
    assert "<pre><code>" in out
    assert "</code></pre>" in out
    assert "<ul>" not in out
    assert "<ol>" not in out


def test_ordered_list_closed_before_fence():
    src = "1. alpha\n2. beta\n```\nblock\n```"
    out = _markdown(src)
    ol = out.find("<ol>")
    pre = out.find("<pre><code>")
    ol_close = out.rfind("</ol>")
    assert ol != -1 and pre > ol and ol_close < pre


if __name__ == "__main__":
    test_list_closed_before_code_fence()
    test_list_closed_by_blank_line_still_works()
    test_plain_fence_renders()
    test_ordered_list_closed_before_fence()
    print("test_markdown: all assertions passed")
