"""GC of orphaned 'votes: [...]' label definitions.

Every distinct PR vote tally creates a permanent repo-level label definition
(add_pr_label POSTs it repo-wide), while remove_pr_label only unlinks it from
that one PR - so definitions accumulate forever unless swept.  server.poller
._sweep_orphan_vote_labels deletes any 'votes:' definition that no open PR
currently references.  These tests drive the sweep with injected github
helpers (no GitHub calls), and unit-test the delete-label URL encoding.
"""
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_vote_label_gc_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github  # noqa: E402
import server.poller as poller  # noqa: E402
from server.poller import _sweep_orphan_vote_labels  # noqa: E402


class _FakeLabeller:
    """Replaces github's repo-label surface with an in-memory store."""

    def __init__(self, definitions, open_labels, deleted=None):
        self.definitions = list(definitions)
        self.open_labels = set(open_labels)
        self.deleted = [] if deleted is None else deleted

    def list_repo_labels(self):
        return list(self.definitions)

    def open_pr_labels(self):
        return set(self.open_labels)

    def delete_pr_label_definition(self, name):
        self.deleted.append(name)
        self.definitions.remove(name)
        return None


def _patch(**attrs):
    """Replace github module attributes and restore originals on exit."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        saved = {}
        try:
            for k, v in attrs.items():
                saved[k] = getattr(github, k)
                setattr(github, k, v)
            yield
        finally:
            for k, v in saved.items():
                setattr(github, k, v)
    return _ctx()


def test_sweep_deletes_only_orphaned_votes_labels():
    """A 'votes:' definition not on any open PR is deleted; one still on an
    open PR is kept; a non-votes label is never touched."""
    labeller = _FakeLabeller(
        definitions=[
            "votes: [+1 | -0]",   # orphan - will be deleted
            "votes: [+2 | -0]",   # on an open PR - kept
            "votes: [+1 | -2]",   # orphan - will be deleted
            "proposal-hold",      # not a votes label - never touched
            "ci: passing",
        ],
        open_labels=["votes: [+2 | -0]", "proposal-hold"],
    )
    with _patch(
        list_repo_labels=labeller.list_repo_labels,
        open_pr_labels=labeller.open_pr_labels,
        delete_pr_label_definition=labeller.delete_pr_label_definition,
    ):
        deleted = _sweep_orphan_vote_labels()

    assert sorted(deleted) == sorted(["votes: [+1 | -0]", "votes: [+1 | -2]"]), \
        f"unexpected deleted set: {deleted}"
    assert sorted(labeller.deleted) == sorted(
        ["votes: [+1 | -0]", "votes: [+1 | -2]"]
    ), "wrong definitions deleted"
    assert "votes: [+2 | -0]" in labeller.definitions, "live label was deleted"
    assert "proposal-hold" in labeller.definitions, "non-votes label was deleted"
    assert "ci: passing" in labeller.definitions, "non-votes label was deleted"
    print("  sweep deletes only orphaned votes labels: ok")


def test_sweep_filters_by_prefix_and_suffix():
    """Only names matching the exact votes: [ ... ] shape are candidates."""
    labeller = _FakeLabeller(
        definitions=[
            "votes: [+0 | -0]",
            "votes: [+5 | -0]",
            "votesomething",       # prefix-like but not a votes label
            "votes:[+1|-0]",       # missing spaces - not a match
        ],
        open_labels=set(),
    )
    with _patch(
        list_repo_labels=labeller.list_repo_labels,
        open_pr_labels=labeller.open_pr_labels,
        delete_pr_label_definition=labeller.delete_pr_label_definition,
    ):
        deleted = _sweep_orphan_vote_labels()

    assert sorted(deleted) == sorted(["votes: [+0 | -0]", "votes: [+5 | -0]"]), \
        f"expected only the two well-formed votes labels, got: {deleted}"
    print("  sweep filters by votes: [ ] shape: ok")


def test_sweep_no_orphans_does_nothing():
    """When every votes label is live (or there are none), nothing deletes."""
    labeller = _FakeLabeller(
        definitions=["votes: [+3 | -1]"],
        open_labels=["votes: [+3 | -1]"],
    )
    with _patch(
        list_repo_labels=labeller.list_repo_labels,
        open_pr_labels=labeller.open_pr_labels,
        delete_pr_label_definition=labeller.delete_pr_label_definition,
    ):
        deleted = _sweep_orphan_vote_labels()
    assert deleted == [], "nothing should be deleted"
    assert labeller.deleted == []

    empty = _FakeLabeller(definitions=[], open_labels=set())
    with _patch(
        list_repo_labels=empty.list_repo_labels,
        open_pr_labels=empty.open_pr_labels,
        delete_pr_label_definition=empty.delete_pr_label_definition,
    ):
        assert _sweep_orphan_vote_labels() == []
    assert empty.deleted == []
    print("  sweep no-op when nothing orphaned: ok")


def test_sweep_survives_github_failure():
    """A failed repo-label list still runs (returns []); a failed open-PR
    label fetch skips deletion so live labels are never at risk."""
    def boom():
        raise RuntimeError("github down")

    labeller = _FakeLabeller(definitions=["votes: [+1 | -0]"], open_labels=set())
    with _patch(
        list_repo_labels=boom,
        open_pr_labels=labeller.open_pr_labels,
        delete_pr_label_definition=labeller.delete_pr_label_definition,
    ):
        assert _sweep_orphan_vote_labels() == []
    assert labeller.deleted == []

    labeller2 = _FakeLabeller(definitions=["votes: [+1 | -0]"], open_labels=set())
    with _patch(
        list_repo_labels=labeller2.list_repo_labels,
        open_pr_labels=boom,
        delete_pr_label_definition=labeller2.delete_pr_label_definition,
    ):
        assert _sweep_orphan_vote_labels() == []
    assert labeller2.deleted == [], "must not delete when open labels are unknown"
    print("  sweep degrades on github failure: ok")


def test_delete_pr_label_definition_encodes_url():
    """delete_pr_label_definition percent-encodes the label in the DELETE
    path so 'votes: [+3 | -1]' is sent correctly (mirror of the existing
    remove_pr_label encoding test)."""
    real_request = github._core._request
    captured = {}

    def spy(method, path, body=None, ok_404=False):
        captured["method"] = method
        captured["path"] = path
        captured["ok_404"] = ok_404
        return None

    github._core._request = spy
    try:
        github.delete_pr_label_definition("votes: [+3 | -1]")
    finally:
        github._core._request = real_request

    assert captured["method"] == "DELETE", captured
    label = "votes: [+3 | -1]"
    assert urllib.parse.quote(label, safe="") in captured["path"], captured["path"]
    assert "labels/" + urllib.parse.quote(label, safe="") == captured["path"], \
        captured["path"]
    assert captured["ok_404"] is True, "missing definition should not raise"
    print("  delete_pr_label_definition URL encoding: ok")


def test_maybe_gc_vote_labels_is_time_gated():
    """_maybe_gc_vote_labels only runs the sweep once per
    (PR_MERGE_POLL_SECONDS * 8) seconds - a back-to-back double call runs
    the sweep exactly once."""
    calls = []

    def fake_sweep():
        calls.append(1)
        return []

    real_sweep = poller._sweep_orphan_vote_labels
    poller._sweep_orphan_vote_labels = fake_sweep
    poller._last_vote_label_gc = 0.0
    try:
        poller._maybe_gc_vote_labels()
        poller._maybe_gc_vote_labels()   # still inside the window -> no-op
        assert len(calls) == 1, f"sweep ran {len(calls)} times, expected 1"
    finally:
        poller._sweep_orphan_vote_labels = real_sweep
        poller._last_vote_label_gc = 0.0
    print("  maybe_gc_vote_labels time-gated: ok")


def main():
    test_sweep_deletes_only_orphaned_votes_labels()
    test_sweep_filters_by_prefix_and_suffix()
    test_sweep_no_orphans_does_nothing()
    test_sweep_survives_github_failure()
    test_delete_pr_label_definition_encodes_url()
    test_maybe_gc_vote_labels_is_time_gated()
    print("\n== test_vote_label_gc: all passed ==")


if __name__ == "__main__":
    main()
