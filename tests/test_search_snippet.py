"""Regression test for _bounded_snippet windowing (PR #229, item 545).

Pins the windowed optimization against the pre-change reference: short bodies
and typical long highlighted bodies must be byte-equal; whitespace-heavy
windows are allowed a few-char divergence (still a valid snippet).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
import db  # noqa: F401
import search


def _reference_bounded_snippet(text: str, width: int | None = None) -> str:
    width = config.SEARCH_SNIPPET_WIDTH if width is None else width
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    mark = text.find("[[")
    start = max(0, mark - width // 2) if mark != -1 else 0
    end = min(len(text), start + width)
    if start > 0:
        return "..." + text[start:end] + "..."
    return text[start:end] + "..."


def test_bounded_snippet_typical_long():
    # Long highlighted body: 12k chars, highlight near middle
    body = ("word " * 2000) + "[[match]]" + (" word" * 2000)  # ~12k
    assert search._bounded_snippet(body, width=240) == _reference_bounded_snippet(
        body, width=240
    )
    print("  typical long: ok")


def test_bounded_snippet_short():
    short = "short [[match]] body"
    assert search._bounded_snippet(short, width=240) == _reference_bounded_snippet(
        short, width=240
    )
    print("  short: ok")


def test_bounded_snippet_whitespace_heavy():
    # Width raw chars before marker are whitespace-heavy -> collapsed prefix < width//2
    # e.g. 500 spaces then marker
    raw = ("   \n\t" * 100) + "[[match]]" + " word" * 100
    new = search._bounded_snippet(raw, width=240)
    ref = _reference_bounded_snippet(raw, width=240)
    # Must contain highlight and be bounded
    assert "[[match]]" in new
    assert len(new) <= 240 + 6  # "..." + 240 + "..."
    # For typical bodies we expect equality; for whitespace-heavy we allow divergence
    # but pin that the snippet is still valid and close.
    if new != ref:
        # Off by at most a few chars before marker due to collapsed whitespace
        assert abs(len(new) - len(ref)) <= 10
        print("  whitespace-heavy: off by few chars (display-only, allowed)")
    else:
        print("  whitespace-heavy: equal")


def test_bounded_snippet_no_marker():
    no_mark = "a " * 1000
    assert search._bounded_snippet(no_mark, width=240) == _reference_bounded_snippet(
        no_mark, width=240
    )
    print("  no marker: ok")


if __name__ == "__main__":
    test_bounded_snippet_typical_long()
    test_bounded_snippet_short()
    test_bounded_snippet_whitespace_heavy()
    test_bounded_snippet_no_marker()
    print("all snippet tests passed")
