"""Ticker fallback gate: auto host branch-CI enqueues only as fallback.

Post-push truth is the GitHub Actions run (repo_pr_checks for the head
SHA). debounced_enqueue must stay silent by default
(CI_FALLBACK_ENABLED=0) and enqueue only while CI_FALLBACK_ENABLED and
CI_RUN_BRANCH_ENABLED are both on. Manual repo_ci_run(pr_number=...) and
the poller's on-demand fallback are unaffected.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ticker_gate_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import server.tools.repo._ticker as ticker_mod  # noqa: E402

_SAVED: dict = {}


def _shadow(name: str, value):
    _SAVED[name] = getattr(config, name)
    setattr(config, name, value)


def _restore():
    for name, value in _SAVED.items():
        setattr(config, name, value)
    _SAVED.clear()


def _drop(prs):
    with ticker_mod._PENDING_LOCK:
        for p in prs:
            ticker_mod._PENDING.pop(p, None)
            ticker_mod._REQUEUE_ATTEMPTS.pop(p, None)


def test_default_is_silent():
    _shadow("CI_FALLBACK_ENABLED", 0)
    _shadow("CI_RUN_BRANCH_ENABLED", 1)
    try:
        _drop([991001])
        ticker_mod.debounced_enqueue(991001)
        assert 991001 not in ticker_mod.pending_prs_snapshot()
    finally:
        _drop([991001])
        _restore()


def test_fallback_enqueues():
    _shadow("CI_FALLBACK_ENABLED", 1)
    _shadow("CI_RUN_BRANCH_ENABLED", 1)
    try:
        _drop([991002])
        ticker_mod.debounced_enqueue(991002)
        assert 991002 in ticker_mod.pending_prs_snapshot()
    finally:
        _drop([991002])
        _restore()


def test_branch_disabled_is_silent():
    _shadow("CI_FALLBACK_ENABLED", 1)
    _shadow("CI_RUN_BRANCH_ENABLED", 0)
    try:
        _drop([991003])
        ticker_mod.debounced_enqueue(991003)
        assert 991003 not in ticker_mod.pending_prs_snapshot()
    finally:
        _drop([991003])
        _restore()


def main():
    test_default_is_silent()
    test_fallback_enqueues()
    test_branch_disabled_is_silent()
    assert ticker_mod._TICKER_TASK is None
    print("test_ticker_fallback_gate: all ok")


if __name__ == "__main__":
    main()
