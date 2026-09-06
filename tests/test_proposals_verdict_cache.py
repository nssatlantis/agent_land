"""Pilot migration pin (315:4955): _cached_verdict renders byte-identical
values through the shared viewer/_cache helper as the old bespoke
_VERDICT_CACHE did - cold render equals warm render equals a direct
_proposal_verdict call, across every verdict shape."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_verdict_cache_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import init  # noqa: E402
from viewer import _cache  # noqa: E402
from viewer import _proposals as _mod  # noqa: E402
from viewer._render_helpers import _proposal_verdict  # noqa: E402


def _prop(pid, **kw):
    base = {
        "id": pid,
        "status": "open",
        "approved": False,
        "stale": False,
        "locked": False,
        "superseded_by_id": None,
        "proposal_kind": "proposal",
        "review_requested": False,
    }
    base.update(kw)
    return base


_SHAPES = [
    _prop(1, status="merged"),
    _prop(2, approved=True),
    _prop(3),
    _prop(4, locked=True),
    _prop(5, proposal_kind="idea"),
    _prop(6, proposal_gain="idea", stale=True, open_days=9),
    _prop(7, review_requested=True),
    _prop(8, status="declined"),
]


def test_cached_matches_direct_cold_and_warm():
    """Every verdict shape: cold render, warm render and the direct compute
    are all byte-identical - the migration changes the cache, not the card."""
    _cache._reset_for_tests()
    for p in _SHAPES:
        cold = _mod._cached_verdict(p)
        warm = _mod._cached_verdict(p)
        assert cold == _proposal_verdict(p)
        assert warm == cold


def test_fetch_runs_once_per_pid():
    """The underlying verdict computes once per pid no matter how many
    times the card renders within the TTL window."""
    _cache._reset_for_tests()
    calls = []
    real = _mod._proposal_verdict

    def counting(p):
        calls.append(p.get("id"))
        return real(p)

    _mod._proposal_verdict = counting
    try:
        p = _prop(101, approved=True)
        assert _mod._cached_verdict(p) == real(p)
        assert _mod._cached_verdict(p) == real(p)
        assert _mod._cached_verdict(_prop(102, approved=True))
    finally:
        _mod._proposal_verdict = real
    assert calls == [101, 102]


def test_pid_none_bypasses_cache():
    """A verdict without a pid computes every time and stores nothing -
    the same contract the bespoke cache kept."""
    _cache._reset_for_tests()
    p = {"status": "open", "approved": True}
    assert _mod._cached_verdict(p) == _proposal_verdict(p)
    assert _mod._cached_verdict(p) == _proposal_verdict(p)
    assert _cache._CACHE == {}


def test_key_is_namespaced():
    """The pilot keys its entries ("verdict", pid) inside the shared cache
    so later panels can never collide with it."""
    _cache._reset_for_tests()
    _mod._cached_verdict(_prop(5, approved=True))
    assert ("verdict", 5) in _cache._CACHE
    assert len(_cache._CACHE) == 1


if __name__ == "__main__":
    init()
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
    print(f"{len(fns)} tests passed")
