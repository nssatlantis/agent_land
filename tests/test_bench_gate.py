"""Tests for the single-anchor benchmark gate (tests/test_benchmark anchor
loading + regression math, single-anchor program #367, step 3/5).

The dispatcher injects the blessed anchor (BENCH_ANCHOR_MEDIANS JSON plus
BENCH_ANCHOR_EVENT_ID); absent or malformed payloads run timing-advisory
(structural pins still enforced), never crash. Pure tests, no DB.

Isolated-subprocess file (the run_all.py convention): importing
tests.test_benchmark has module-level side effects (mkdtemp + DB env),
so this file points its own throwaway env first, exactly like every
other behavior-test file.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bench_gate_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_benchmark import _check_regression, _load_anchor  # noqa: E402


def _with_anchor_env(meds, event_id="99"):
    old_meds = os.environ.get("BENCH_ANCHOR_MEDIANS")
    old_ev = os.environ.get("BENCH_ANCHOR_EVENT_ID")
    if meds is None:
        os.environ.pop("BENCH_ANCHOR_MEDIANS", None)
    else:
        os.environ["BENCH_ANCHOR_MEDIANS"] = meds
    if event_id is None:
        os.environ.pop("BENCH_ANCHOR_EVENT_ID", None)
    else:
        os.environ["BENCH_ANCHOR_EVENT_ID"] = event_id
    return old_meds, old_ev


def _restore_anchor_env(saved):
    old_meds, old_ev = saved
    if old_meds is None:
        os.environ.pop("BENCH_ANCHOR_MEDIANS", None)
    else:
        os.environ["BENCH_ANCHOR_MEDIANS"] = old_meds
    if old_ev is None:
        os.environ.pop("BENCH_ANCHOR_EVENT_ID", None)
    else:
        os.environ["BENCH_ANCHOR_EVENT_ID"] = old_ev


def test_no_env_is_advisory():
    saved = _with_anchor_env(None, None)
    try:
        anchor, event = _load_anchor()
        assert anchor == {} and event is None, "absent anchor reads advisory"
        assert _check_regression("q", 99.0, 0.1, anchor) is False, (
            "empty anchor never flags"
        )
    finally:
        _restore_anchor_env(saved)


def test_valid_payload_loads():
    saved = _with_anchor_env(json.dumps({"a": 10.0, "b": 20}))
    try:
        anchor, event = _load_anchor()
        assert anchor == {"a": 10.0, "b": 20.0}, "medians load"
        assert event == "99", "event id loads"
    finally:
        _restore_anchor_env(saved)


def test_garbage_payload_fails_to_advisory():
    for bad in ("not-json{", "[1,2]", "42", ""):
        saved = _with_anchor_env(bad, "7")
        try:
            anchor, _ = _load_anchor()
            assert anchor == {}, f"garbage payload is advisory, not fatal: {bad!r}"
        finally:
            _restore_anchor_env(saved)


def test_non_numeric_values_filtered():
    saved = _with_anchor_env(
        json.dumps({"ok": 5.0, "flag": True, "text": "x", "nil": None})
    )
    try:
        anchor, _ = _load_anchor()
        assert anchor == {"ok": 5.0}, "only numerics survive"
    finally:
        _restore_anchor_env(saved)


def test_gate_math_against_anchor():
    anchor = {"slow": 10.0, "flat": 10.0, "quick": 10.0}
    assert _check_regression("slow", 13.0, 0.1, anchor) is True, (
        "+30% over the abs floor flags"
    )
    assert _check_regression("flat", 12.5, 5.0, anchor) is False, (
        "+25% inside 2x-stdev noise does not flag"
    )
    assert _check_regression("quick", 11.0, 0.1, anchor) is False, "+10% does not flag"
    assert _check_regression("missing", 99.0, 0.1, anchor) is False, (
        "queries absent from the anchor never flag"
    )
    assert _check_regression("slow", 5.0, 0.1, anchor) is False, (
        "improvements never flag"
    )


def main():
    test_no_env_is_advisory()
    test_valid_payload_loads()
    test_garbage_payload_fails_to_advisory()
    test_non_numeric_values_filtered()
    test_gate_math_against_anchor()
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_bench_gate: all assertions passed")


if __name__ == "__main__":
    main()
