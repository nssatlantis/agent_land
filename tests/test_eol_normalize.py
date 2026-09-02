"""Tests for EOL normalization (LF canonical, auto-convert PR payloads)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github._eol as _eol
import github._gitops as _gitops
import github._writes as _writes


def test_single_source():
    # One implementation: both modules re-export github._eol's helpers,
    # so a future fix cannot desync them.
    assert _writes._normalize_eol is _eol._normalize_eol
    assert _writes._target_eol_for_text is _eol._target_eol_for_text
    assert _gitops._normalize_eol is _eol._normalize_eol
    assert _gitops._target_eol_for_text is _eol._target_eol_for_text
    print("  single-source ok")  # noqa: E402


def test_normalize_eol_helper():
    # LF -> LF no change
    assert _writes._normalize_eol("a\nb\n", "\n") == "a\nb\n"
    # CRLF -> LF
    assert _writes._normalize_eol("a\r\nb\r\n", "\n") == "a\nb\n"
    # LF -> CRLF
    assert _writes._normalize_eol("a\nb\n", "\r\n") == "a\r\nb\r\n"
    # CRLF -> CRLF no change
    assert _writes._normalize_eol("a\r\nb\r\n", "\r\n") == "a\r\nb\r\n"
    # Mixed -> LF
    assert _writes._normalize_eol("a\r\nb\nc\r", "\n") == "a\nb\nc\n"
    # Mixed -> CRLF
    assert _writes._normalize_eol("a\r\nb\nc\r", "\r\n") == "a\r\nb\r\nc\r\n"
    # Binary guard
    assert _writes._normalize_eol("a\0b\n", "\n") == "a\0b\n"
    print("  helper ok")


def test_target_eol_detection():
    assert _writes._target_eol_for_text(None) == "\n"
    assert _writes._target_eol_for_text("") == "\n"
    assert _writes._target_eol_for_text("a\nb\n") == "\n"
    assert _writes._target_eol_for_text("a\r\nb\r\n") == "\r\n"
    # mixed follows the majority ending; ties fall back to LF (canonical)
    assert _writes._target_eol_for_text("a\r\nb\r\nc\n") == "\r\n"
    assert _writes._target_eol_for_text("a\r\nb\nc\n") == "\n"
    assert _writes._target_eol_for_text("a\r\nb\n") == "\n"
    assert _writes._target_eol_for_text("a\0b\n") == "\n"
    print("  target ok")


def test_whole_file_normalize_preserves_base():
    # Simulate old CRLF base: incoming LF should become CRLF to avoid churn
    base_crlf = "x\r\ny\r\n"
    target = _writes._target_eol_for_text(base_crlf)
    assert target == "\r\n"
    incoming_lf = "x\ny\nz\n"
    assert _writes._normalize_eol(incoming_lf, target) == "x\r\ny\r\nz\r\n"
    # New LF base: incoming stays LF
    base_lf = "x\ny\n"
    assert _writes._target_eol_for_text(base_lf) == "\n"
    assert _writes._normalize_eol(incoming_lf, "\n") == "x\ny\nz\n"
    print("  whole-file ok")


def test_patch_find_normalized():
    # Base is CRLF, caller find uses LF -> should still match after normalize
    base = "alpha = 1\r\nbeta = 2\r\n"
    target = _writes._target_eol_for_text(base)
    assert target == "\r\n"
    find_lf = "beta = 2\n"
    find_norm = _writes._normalize_eol(find_lf, target)
    assert find_norm == "beta = 2\r\n"
    replace_lf = "gamma = 3\n"
    replace_norm = _writes._normalize_eol(replace_lf, target)
    assert replace_norm == "gamma = 3\r\n"
    # Apply via strict engine
    new_text, _log = _writes._apply_edits(
        "x.py", base, [{"find": find_norm, "replace": replace_norm}]
    )
    assert new_text == "alpha = 1\r\ngamma = 3\r\n"
    # LF base with CRLF find -> normalized to LF should match
    base_lf = "alpha = 1\nbeta = 2\n"
    target2 = _writes._target_eol_for_text(base_lf)
    find_crlf = "beta = 2\r\n"
    assert _writes._normalize_eol(find_crlf, target2) == "beta = 2\n"
    print("  patch ok")


def main():
    test_single_source()
    test_normalize_eol_helper()
    test_target_eol_detection()
    test_whole_file_normalize_preserves_base()
    test_patch_find_normalized()
    print("ALL EOL TESTS PASSED")


if __name__ == "__main__":
    main()
