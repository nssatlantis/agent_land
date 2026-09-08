"""Tests for named rehearsal trees (repo_ci_run(tree=...)).

Persistent per-agent overlay trees so multi-step builds skip the
re-upload + cold-sync every iteration. Git is faked at the _git seam
(record calls, canned SHAs, emulated reset/clean); everything else -
manifest, deltas, replay, caps, sweep, wiring - runs for real on a
throwaway DATA_DIR.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_named_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.ci_runner as ci_runner  # noqa: E402
import server.ci_runner._trees as trees  # noqa: E402
from tests._setup import config, db, setup  # noqa: E402


class _FakeGit:
    """Emulate fetch/rev-parse/reset/clean; mirror clean -xdf wiping
    everything but .git (like the real thing, which is why replayed
    deltas are re-stored from memory)."""

    def __init__(self):
        self.calls = []
        self.fetch_head = "a" * 40

    def __call__(self, tree, *args):
        self.calls.append((tree, args))
        return self._run(tree, args)

    def _run(self, tree, args):
        if args[:1] == ("fetch",):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ("rev-parse", "FETCH_HEAD"):
            return subprocess.CompletedProcess(args, 0, self.fetch_head + "\n", "")
        if args[:1] == ("reset",):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:1] == ("clean",):
            for entry in os.listdir(tree):
                if entry == ".git":
                    continue
                p = os.path.join(tree, entry)
                if os.path.isdir(p) and not os.path.islink(p):
                    import shutil

                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")


def _expect_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except db.ForumError as exc:
        return str(exc)
    except Exception as exc:  # pragma: no cover
        return f"wrong exception {type(exc).__name__}: {exc}"
    raise AssertionError("expected ForumError but call succeeded")


def main():
    agents, _ = setup()
    agent = db.register_agent("tree-owner")
    aid = agent["agent_id"]
    real_git = trees._git
    real_clone = trees._ensure_clone
    fake = _FakeGit()
    trees._git = fake
    trees._ensure_clone = lambda tree: os.makedirs(
        os.path.join(tree, ".git"), exist_ok=True
    )
    old_cap = config.CI_NAMED_TREE_MAX_PER_AGENT
    old_mb = config.CI_NAMED_TREE_MAX_MB
    old_ttl = config.CI_NAMED_TREE_TTL_HOURS
    try:
        # 1. name validation.
        for bad in ["", "a/b", "../x", "a" * 41, "has space", "uni+c"]:
            msg = _expect_error(trees._validate_tree_name, bad)
            assert "1-40" in msg, f"bad {bad!r} not rejected: {msg}"
        assert trees._validate_tree_name("feat-1_x") == "feat-1_x"
        print("  name validation: ok")

        # 2. create + delta applied + manifest.
        d1 = [{"path": "a.py", "content": "A = 1\n"}]
        tree, head, info = trees._prepare_named_tree(aid, "feat", d1)
        assert (Path(tree) / "a.py").read_text() == "A = 1\n"
        assert info["tree"] == "feat" and info["tree_warm"] is False
        assert info["delta_count"] == 1 and info["local"] is True
        assert head.startswith("a" * 12 + "+tree-feat-")
        print("  create: ok")

        # 3. second delta stacks; warm hit (no reset).
        resets = [c for c in fake.calls if c[1][:1] == ("reset",)]
        d2 = [{"path": "b.py", "content": "B = 2\n"}]
        tree2, _, info2 = trees._prepare_named_tree(aid, "feat", d2)
        assert tree2 == tree
        assert (Path(tree) / "a.py").exists() and (Path(tree) / "b.py").exists()
        assert info2["tree_warm"] is True and info2["delta_count"] == 2
        assert [c for c in fake.calls if c[1][:1] == ("reset",)] == resets, (
            "warm hit must skip the reset"
        )
        print("  stacking + warm hit: ok")

        # 4. base move replays stored deltas.
        fake.fetch_head = "b" * 40
        d3 = [{"path": "c.py", "content": "C = 3\n"}]
        _, head3, info3 = trees._prepare_named_tree(aid, "feat", d3)
        assert info3["tree_warm"] is False and head3.startswith("b" * 12)
        assert (Path(tree) / "a.py").read_text() == "A = 1\n", "delta 1 replayed"
        assert (Path(tree) / "b.py").exists() and (Path(tree) / "c.py").exists()
        assert info3["delta_count"] == 3
        print("  base-move replay: ok")

        # 5. replay failure names the file, clears the store, tree stays clean.
        fake.fetch_head = "c" * 40
        # poison delta 1's target by making its replay impossible: the new
        # base no longer matters (fake), so poison via a find-replace delta
        # whose find cannot match a fresh file.
        trees._prepare_named_tree(aid, "feat", [{"path": "q.py", "content": "Q = 1\n"}])
        fake.fetch_head = "d" * 40
        # now delta set = [d1, d2, d3, q]; make q unreplayable by replacing
        # the stored blob with a patch against a missing file.
        import json as _json

        store = os.path.join(tree, ".ci-deltas")
        blobs = sorted(os.listdir(store))
        with open(os.path.join(store, blobs[-1]), "w", encoding="utf-8") as fh:
            _json.dump(
                [{"path": "gone.py", "edits": [{"find": "x", "replace": "y"}]}],
                fh,
            )
        msg = _expect_error(trees._prepare_named_tree, aid, "feat", [])
        assert "no longer applies" in msg and "gone.py" in msg, f"bad msg: {msg}"
        assert not os.path.isdir(store), "store cleared after failed replay"
        print("  replay failure: ok")

        # 6. per-agent cap names the held trees.
        # (re-establish feat: the failed replay above left it manifestless,
        # which the idle sweep correctly reaps as stale.)
        trees._prepare_named_tree(aid, "feat", d1)
        config.CI_NAMED_TREE_MAX_PER_AGENT = 1
        msg = _expect_error(trees._prepare_named_tree, aid, "second", d1)
        assert "cap" in msg and "feat" in msg, f"bad cap msg: {msg}"
        config.CI_NAMED_TREE_MAX_PER_AGENT = old_cap
        print("  cap: ok")

        # 7. ownership: foreign manifest rebuilds instead of serving.
        import json as _json2

        with open(os.path.join(tree, ".ci-tree.json"), "w", encoding="utf-8") as fh:
            _json2.dump({"agent_id": aid + 999, "base_sha": "x"}, fh)
        trees._prepare_named_tree(aid, "feat", [])
        with open(os.path.join(tree, ".ci-tree.json"), encoding="utf-8") as fh:
            assert _json2.load(fh)["agent_id"] == aid, "foreign tree rebuilt"
        print("  ownership: ok")

        # 8. size cap estimated before any write.
        config.CI_NAMED_TREE_MAX_MB = 0.000001
        try:
            msg = _expect_error(
                trees._prepare_named_tree,
                aid,
                "feat",
                [{"path": "big.py", "content": "x" * 100}],
            )
            assert "exceed" in msg, f"bad size msg: {msg}"
        finally:
            config.CI_NAMED_TREE_MAX_MB = old_mb
        print("  size cap: ok")

        # 9. TTL sweep + forget/list round-trip.
        listed = trees.list_named_trees(aid)
        assert any(r["name"] == "feat" for r in listed), "list shows the tree"
        assert trees.forget_named_tree(aid, "nope") is False
        assert trees.forget_named_tree(aid, "feat") is True
        assert trees.list_named_trees(aid) == [], "forgotten tree unlisted"
        trees._prepare_named_tree(aid, "old", d1)
        import json as _json3

        man = os.path.join(trees._named_dir(aid, "old"), ".ci-tree.json")
        with open(man, encoding="utf-8") as fh:
            m = _json3.load(fh)
        m["updated_at"] = time.time() - 10 * 365 * 24 * 3600
        with open(man, "w", encoding="utf-8") as fh:
            _json3.dump(m, fh)
        assert trees._sweep_idle_named_trees() >= 1, "idle tree swept"
        assert trees.list_named_trees(aid) == [], "swept tree unlisted"
        print("  sweep + forget/list: ok")

        # 10. tool wiring: exclusions + forget + kind mapping.
        import server.tools.repo as repo_tool  # noqa: E402

        msg = _expect_error(
            repo_tool.repo_ci_run, agent["token"], "tests", 7, None, "feat"
        )
        assert "mutually exclusive" in msg, f"tree+pr not refused: {msg}"
        msg = _expect_error(
            repo_tool.repo_ci_run,
            agent["token"],
            "tests",
            None,
            None,
            None,
            True,
        )
        assert "needs tree" in msg, f"bare forget not refused: {msg}"
        msg = _expect_error(
            repo_tool.repo_ci_run, agent["token"], "tests", None, None, "bad name!"
        )
        assert "1-40" in msg, f"bad tree name not refused pre-slot: {msg}"
        got = repo_tool.repo_ci_run(
            agent["token"], "tests", None, None, "gone-absent", True
        )
        assert got == {"tree": "gone-absent", "forgot": False}, f"forget miss: {got}"
        assert ci_runner.ledger_kind_for("tests", None, None, "t") == "ci_local_run"
        assert ci_runner.ledger_kind_for("tests") == "ci_run"
        print("  tool wiring: ok")
    finally:
        trees._git = real_git
        trees._ensure_clone = real_clone
        config.CI_NAMED_TREE_MAX_PER_AGENT = old_cap
        config.CI_NAMED_TREE_MAX_MB = old_mb
        config.CI_NAMED_TREE_TTL_HOURS = old_ttl

    print("test_ci_named_trees: all ok")


if __name__ == "__main__":
    main()
