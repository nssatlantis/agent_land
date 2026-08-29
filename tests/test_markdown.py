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


def test_anchors_off_by_default():
    src = "# Title\n\n## Section\n\n### Sub"
    out = _markdown(src)
    assert "<h2>Title</h2>" in out
    assert 'id="' not in out


def test_anchors_add_slug_ids_in_lockstep():
    src = "# Intro\n\n### Deep\n\n## Body\n"
    anchored = _markdown(src, anchors=True)
    plain = _markdown(src)
    # headings keep their tags/levels; ids land only in the anchored form
    assert 'id="intro"' in anchored
    assert 'id="deep"' in anchored
    assert 'id="body"' in anchored
    assert 'id="' not in plain
    assert '<h4 id="deep">' in anchored
    assert '<h2 id="intro">' in anchored
    assert '<h3 id="body">' in anchored


def test_anchors_skip_fenced_headings():
    src = "```\n# fenced\n```\n# real\n"
    anchored = _markdown(src, anchors=True)
    assert 'id="real"' in anchored
    assert 'id="fenced"' not in anchored


def test_heading_sections_levels_and_dedupe():
    sections = _utils._heading_sections("# One\n\n## Dupe\n\n### Three\n\n## Dupe\n")
    assert sections == (
        (2, "one", "One"),
        (3, "dupe", "Dupe"),
        (4, "three", "Three"),
        (3, "dupe-2", "Dupe"),
    )
    assert _utils._heading_sections("no headings here\n\n- item\n") == ()


def test_toc_nav_marks_levels_and_empty():
    toc = _utils._toc_nav(((2, "a", "Alpha"), (3, "b", "Beta"), (4, "c", "Gamma")))
    assert 'class="record-toc"' in toc
    assert 'href="#a"' in toc and 'href="#b"' in toc and 'href="#c"' in toc
    assert 'class="toc-3"' in toc and 'class="toc-4"' in toc
    assert "Alpha" in toc and "Beta" in toc and "Gamma" in toc
    assert _utils._toc_nav(()) == ""


def test_split_changes_and_reconstruction():
    text = "operative body\n\n## Changes\n\n- amended\n\nmore\n"
    body, changes = _utils._split_changes(text)
    assert "operative body" in body
    assert "## Changes" in changes
    assert body + "\n" + changes == text  # marker's leading newline absorbed
    # no marker -> (whole, None), matching server/records.py
    assert _utils._split_changes("plain record\n") == ("plain record\n", None)


def test_recent_changes_html_rows_and_empty():
    commits = [
        {
            "short": "abc1234",
            "iso": "2026-08-29T00:00:00Z",
            "subject": "add toc",
            "patch": "+toc\n",
        },
        {
            "short": "def5678",
            "iso": "2026-08-28T00:00:00Z",
            "subject": "fix typo",
            "patch": "-x\n+y\n",
        },
    ]
    html = _utils._recent_changes_html(commits)
    assert "Recent changes" in html
    assert "last 2 commits" in html
    assert "@abc1234" in html and "@def5678" in html
    assert "add toc" in html and "fix typo" in html
    assert html.count("<details") == 3  # outer panel + one per commit
    assert _utils._recent_changes_html([]) == ""


if __name__ == "__main__":
    test_list_closed_before_code_fence()
    test_list_closed_by_blank_line_still_works()
    test_plain_fence_renders()
    test_ordered_list_closed_before_fence()
    test_anchors_off_by_default()
    test_anchors_add_slug_ids_in_lockstep()
    test_anchors_skip_fenced_headings()
    test_heading_sections_levels_and_dedupe()
    test_toc_nav_marks_levels_and_empty()
    test_split_changes_and_reconstruction()
    test_recent_changes_html_rows_and_empty()
    print("test_markdown: all assertions passed")
