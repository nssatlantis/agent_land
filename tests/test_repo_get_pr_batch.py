"""Tests for repo_get_pr's batch mode: numbers=[a, b] fetches two pull
requests in one call, concurrently, with per-entry error isolation.
Single-mode behavior (including its raised errors) is unchanged."""

import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_prbatch_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()

# Load the repo's root server package under a private name
# so the server/ package stays untouched; its handlers are what we assert.
_ROOT = Path(__file__).resolve().parent.parent / "server" / "__init__.py"
_spec = importlib.util.spec_from_file_location("agentland_root_server_prbatch", _ROOT)
root_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(root_server)


def _payload(number):
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "state": "open",
        "outcome": "open",
        "checks": {"state": "unknown", "source": "stub"},
        "comments": [],
        "files": [],
    }


def _install_aper(payload_by_number, order=None):
    """Replace github.aget_pr on the loaded server module with a fake that
    records start/end order and raises RepoError for unknown numbers."""

    async def fake(number):
        if order is not None:
            order.append(("start", number))
        p = payload_by_number.get(number)
        if p is None:
            raise root_server.github.RepoError(f"pull request #{number} not found.")
        await asyncio.sleep(0)
        if order is not None:
            order.append(("end", number))
        return dict(p)

    real = root_server.github.aget_pr
    root_server.github.aget_pr = fake
    return real


def test_single_mode_unchanged():
    real = _install_aper({5: _payload(5)})
    try:
        got = asyncio.run(root_server.repo_get_pr(number=5))
        assert got["number"] == 5, got
        assert got["title"] == "PR 5"
        assert got["votes"]["threshold"] == db.pr_vote_threshold()
        assert "eligible_for_merge" in got["votes"]
        # Single mode keeps raising on an unknown PR - isolation is a
        # batch-mode feature only.
        try:
            asyncio.run(root_server.repo_get_pr(number=6))
            raise AssertionError("unknown single PR must still raise")
        except root_server.github.RepoError:
            pass
    finally:
        root_server.github.aget_pr = real
    print("  single mode unchanged (and still raises): ok")


def test_batch_happy_path_keyed_map():
    real = _install_aper({7: _payload(7), 9: _payload(9)})
    try:
        got = asyncio.run(root_server.repo_get_pr(numbers=[7, 9]))
        assert set(got.keys()) == {7, 9}, got
        assert got[7]["title"] == "PR 7" and got[9]["title"] == "PR 9"
        assert "error" not in got[7] and "error" not in got[9]
        assert got[7]["votes"]["threshold"] == db.pr_vote_threshold()
    finally:
        root_server.github.aget_pr = real
    print("  batch returns a keyed map with full views: ok")


def test_batch_fetches_run_concurrently():
    order: list = []
    real = _install_aper({1: _payload(1), 2: _payload(2)}, order=order)
    try:
        got = asyncio.run(root_server.repo_get_pr(numbers=[1, 2]))
        assert set(got.keys()) == {1, 2}
        second_start = order.index(("start", 2))
        first_end = order.index(("end", 1))
        assert second_start < first_end, (
            "second fetch must start before the first finishes",
            order,
        )
    finally:
        root_server.github.aget_pr = real
    print("  batch fetches overlap (second starts before first ends): ok")


def test_unknown_number_isolated_not_fatal():
    real = _install_aper({11: _payload(11)})  # 12 unknown -> RepoError
    try:
        got = asyncio.run(root_server.repo_get_pr(numbers=[12, 11]))
        assert set(got.keys()) == {12, 11}, got
        assert "error" in got[12], got[12]
        assert "not found" in got[12]["error"], got[12]
        assert got[11]["number"] == 11, "healthy sibling must be complete"
        assert "error" not in got[11]
    finally:
        root_server.github.aget_pr = real
    print("  one bad number yields an error entry, batch survives: ok")


def test_my_vote_passthrough_in_both_modes():
    calls: list[int] = []
    real_aper = _install_aper({3: _payload(3), 4: _payload(4)})
    real_my_vote = root_server.db.my_pr_vote

    def fake_my_vote(token, number):
        calls.append(number)
        return +1

    root_server.db.my_pr_vote = fake_my_vote
    token = AGENTS["alpha"]["token"]
    try:
        single = asyncio.run(root_server.repo_get_pr(number=3, token=token))
        assert single["my_vote"] == +1, single.get("my_vote")
        batch = asyncio.run(root_server.repo_get_pr(numbers=[3, 4], token=token))
        assert batch[3]["my_vote"] == +1 and batch[4]["my_vote"] == +1
        # One my_vote lookup per assembled view: single(3), then 3 + 4.
        assert sorted(calls) == [3, 3, 4], calls
    finally:
        root_server.db.my_pr_vote = real_my_vote
        root_server.github.aget_pr = real_aper
    print("  my_vote passthrough works in single and batch modes: ok")


def test_argument_validation():
    cases = [
        (
            {"number": 1, "numbers": [2, 3]},
            "pass either number or numbers, not both.",
        ),
        ({}, "pass either number or numbers."),
        ({"numbers": []}, "numbers accepts at least one pull request."),
        (
            {"numbers": [1, 2, 3]},
            "numbers accepts at most 2 pull requests at once.",
        ),
    ]
    for kwargs, message in cases:
        try:
            asyncio.run(root_server.repo_get_pr(**kwargs))
            raise AssertionError(f"expected ForumError for {kwargs}")
        except db.ForumError as e:
            assert message in str(e), (kwargs, str(e))
    print("  argument validation errors are exact: ok")


if __name__ == "__main__":
    test_single_mode_unchanged()
    test_batch_happy_path_keyed_map()
    test_batch_fetches_run_concurrently()
    test_unknown_number_isolated_not_fatal()
    test_my_vote_passthrough_in_both_modes()
    test_argument_validation()
    print("\n== test_repo_get_pr_batch: all passed ==")
