"""Push a claim tree as a single-commit PR (proposal #484, part 6)."""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_push_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import github._gitops as gh  # noqa: E402
import github._workspaces as ws  # noqa: E402
from github._core import RepoError  # noqa: E402
from tests._setup import db, setup  # noqa: E402


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _mk_remote(tmp):
    bare = os.path.join(tmp, "remote.git")
    seed = os.path.join(tmp, "seed")
    os.makedirs(seed)
    _git("init", "--bare", "-b", "main", bare)
    _git("init", "-b", "main", cwd=seed)
    with open(os.path.join(seed, "README.md"), "w") as f:
        f.write("seed\n")
    with open(os.path.join(seed, "OLD.txt"), "w") as f:
        f.write("old\n")
    _git("-C", seed, "add", "-A")
    _git(
        "-C", seed, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-m", "seed"
    )
    _git("-C", seed, "push", bare, "main")
    return bare


_SHARED_BARE = _mk_remote(tempfile.mkdtemp(prefix="agentland_ws_push_remote_"))


@contextmanager
def _no_auth(dest):
    yield


class _PushSandbox:
    """Local-bare remote + stubbed transport (no network, no token)."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="agentland_ws_push_test_")
        self.calls = []
        self.open_prs = []
        self._orig = {
            "repo_url": ws._repo_url,
            "claims_root": ws._claims_root,
            "gitops_url": gh._repo_url,
            "push_auth": ws._push_auth,
            "ensure_token": ws._core._ensure_token,
            "request": ws._core._request,
            "invalidate": ws._core._invalidate_pr,
        }
        ws._repo_url = lambda with_token=False: _SHARED_BARE
        ws._claims_root = lambda: os.path.join(self.tmp, "claims")
        gh._repo_url = lambda with_token=False: _SHARED_BARE
        ws._push_auth = _no_auth
        ws._core._ensure_token = lambda: None

        def _invalidate(number):
            self.calls.append(("invalidate", number, None))

        ws._core._invalidate_pr = _invalidate
        ws._core._request = self._fake_request

    def _fake_request(self, method, path, payload=None, **kw):
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("pulls?head="):
            want = path.split("head=", 1)[1].split("&")[0].split(":", 1)[1]
            return [pr for pr in self.open_prs if pr.get("branch") == want]
        if method == "GET" and path.startswith("pulls?state=open"):
            return list(self.open_prs)
        if method == "POST" and path == "pulls":
            pr = {
                "number": 7,
                "html_url": "http://example/pr/7",
                "branch": (payload or {}).get("head"),
                "title": (payload or {}).get("title"),
                "body": (payload or {}).get("body"),
            }
            self.open_prs.append(pr)
            return pr
        if method == "PATCH" and path.startswith("pulls/"):
            number = int(path.split("/", 1)[1])
            for pr in self.open_prs:
                if pr.get("number") == number:
                    pr.update(payload or {})
                    return pr
            raise RepoError(f"no such PR in fake transport: {path}")
        return {}

    def close(self):
        ws._repo_url = self._orig["repo_url"]
        ws._claims_root = self._orig["claims_root"]
        gh._repo_url = self._orig["gitops_url"]
        ws._push_auth = self._orig["push_auth"]
        ws._core._ensure_token = self._orig["ensure_token"]
        ws._core._request = self._orig["request"]
        ws._core._invalidate_pr = self._orig["invalidate"]
        shutil.rmtree(self.tmp, ignore_errors=True)


def _expect_repo_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except RepoError as exc:
        return str(exc)
    raise AssertionError(f"expected RepoError from {fn.__name__}()")


def _expect_tool_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except Exception as exc:
        return str(exc)
    raise AssertionError(f"expected a tool error from {fn.__name__}()")


def _branch_files(tree, branch):
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", branch],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


def _branch_count(tree, branch):
    out = subprocess.run(
        ["git", "rev-list", "--count", f"main..{branch}"],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def test_push_single_commit():
    sb = _PushSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 31, "ship")
        dest = tree["path"]
        Path(dest, "feat.txt").write_text("feat\n", encoding="utf-8")
        Path(dest, "README.md").write_text("seed\nmore\n", encoding="utf-8")
        res = ws.push_claim_tree(
            11, 31, "ship", "Ship it", "does things", "tester (agent_id=11)"
        )
        assert res["pr_number"] == 7, res
        assert res["first_push"] is True, res
        assert res["branch"] == "claim/11/31/ship", res
        assert res["commit_sha"], res
        assert res["html_url"] == "http://example/pr/7", res
        assert _branch_count(dest, res["branch"]) == 1, res
        names = _branch_files(dest, res["branch"])
        assert "feat.txt" in names, names
        assert "README.md" in names, names
        assert ".workspace.json" not in names, names
        show = subprocess.run(
            ["git", "show", f"{res['branch']}:feat.txt"],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
        )
        assert show.stdout == "feat\n", show.stdout
        log = subprocess.run(
            ["git", "log", "-1", "--format=%B", res["branch"]],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "Ship it" in log.stdout, log.stdout
        assert "Citizen: tester (agent_id=11)" in log.stdout, log.stdout
        posts = [c for c in sb.calls if c[0] == "POST"]
        assert posts and posts[0][1] == "pulls", sb.calls
        assert posts[0][2]["head"] == res["branch"], posts
        assert "Citizen: tester (agent_id=11)" in posts[0][2]["body"], posts
        owner = ws.GITHUB_REPO.split("/")[0]
        get = f"pulls?head={owner}:{res['branch']}&state=open"
        assert ("GET", get, None) in sb.calls, sb.calls
    finally:
        sb.close()
    print("  push single commit (one commit, manifest out, trailer): ok")


def test_push_followup_appends():
    sb = _PushSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 32, "ship")
        dest = tree["path"]
        Path(dest, "one.txt").write_text("one\n", encoding="utf-8")
        first = ws.push_claim_tree(
            11, 32, "ship", "Ship it", "body", "tester (agent_id=11)"
        )
        sha1 = first["commit_sha"]
        Path(dest, "two.txt").write_text("two\n", encoding="utf-8")
        second = ws.push_claim_tree(
            11, 32, "ship", "Ship more", "body", "tester (agent_id=11)"
        )
        assert second["pr_number"] == 7, second
        assert second["first_push"] is False, second
        assert _branch_count(dest, second["branch"]) == 2, second
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha1, second["branch"]],
            cwd=dest,
            check=True,
            capture_output=True,
        )
        assert ("invalidate", 7, None) in sb.calls, sb.calls
        posts = [c for c in sb.calls if c[0] == "POST"]
        assert len(posts) == 1, sb.calls
    finally:
        sb.close()
    print("  push follow-up (appends, no reset, PR reused): ok")


def test_push_stages_deletion():
    sb = _PushSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 33, "del")
        dest = tree["path"]
        os.remove(os.path.join(dest, "OLD.txt"))
        res = ws.push_claim_tree(
            11, 33, "del", "Drop old", "body", "tester (agent_id=11)"
        )
        assert "OLD.txt" not in _branch_files(dest, res["branch"]), res
    finally:
        sb.close()
    print("  push stages deletion: ok")


def test_push_guards():
    sb = _PushSandbox()
    try:
        assert "no workspace tree" in _expect_repo_error(
            ws.push_claim_tree, 11, 34, "missing", "T", "b", "c (agent_id=11)"
        )
        ws.ensure_claim_tree(11, 34, "clean")
        assert "nothing to push" in _expect_repo_error(
            ws.push_claim_tree, 11, 34, "clean", "T", "b", "c (agent_id=11)"
        )
        assert "title is required" in _expect_repo_error(
            ws.push_claim_tree, 11, 34, "clean", "  ", "b", "c (agent_id=11)"
        )
        sb.open_prs.append(
            {
                "number": 9,
                "html_url": "http://example/pr/9",
                "branch": "claim/11/34/ghost",
            }
        )
        ws.ensure_claim_tree(11, 34, "ghost")
        ghost = ws._claim_dir(11, 34, "ghost")
        Path(ghost, "x.txt").write_text("x\n", encoding="utf-8")
        err = _expect_repo_error(
            ws.push_claim_tree, 11, 34, "ghost", "T", "b", "c (agent_id=11)"
        )
        assert "open PR #9" in err, err
    finally:
        sb.close()
    print("  push guards (missing/clean/title/past-life): ok")


def test_push_ignores_other_branch_prs():
    sb = _PushSandbox()
    try:
        sb.open_prs.append(
            {
                "number": 9,
                "html_url": "http://example/pr/9",
                "branch": "claim/0/0/other",
            }
        )
        tree = ws.ensure_claim_tree(11, 36, "mine")
        dest = tree["path"]
        Path(dest, "m.txt").write_text("m\n", encoding="utf-8")
        res = ws.push_claim_tree(11, 36, "mine", "T", "b", "c (agent_id=11)")
        assert res["pr_number"] == 7, res
        assert res["first_push"] is True, res
    finally:
        sb.close()
    print("  push ignores other branches' PRs: ok")


def test_push_refuses_retained_branch():
    sb = _PushSandbox()
    try:
        # Past life: branch pushed, its PR since closed (no open PR recorded).
        clone = tempfile.mkdtemp(prefix="agentland_ws_push_kept_")
        _git("clone", _SHARED_BARE, "w", cwd=clone)
        w = os.path.join(clone, "w")
        _git("-C", w, "checkout", "-b", "claim/11/37/kept")
        _git(
            "-C",
            w,
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-m",
            "past",
        )
        _git("-C", w, "push", "origin", "claim/11/37/kept")
        shutil.rmtree(clone, ignore_errors=True)
        tree = ws.ensure_claim_tree(11, 37, "kept")
        dest = tree["path"]
        Path(dest, "k.txt").write_text("k\n", encoding="utf-8")
        err = _expect_repo_error(
            ws.push_claim_tree, 11, 37, "kept", "T", "b", "c (agent_id=11)"
        )
        assert "already exists on origin" in err, err
    finally:
        sb.close()
    print("  push refuses retained branch: ok")


def test_push_failure_restores_dirty():
    sb = _PushSandbox()
    real_git = ws._git
    seen = []
    pushed = {"done": False}

    def flaky_git(dest, *args, **kw):
        seen.append(list(args))
        if list(args)[:2] == ["push", "origin"] and not pushed["done"]:
            pushed["done"] = True
            raise RepoError("simulated push failure")
        return real_git(dest, *args, **kw)

    ws._git = flaky_git
    try:
        tree = ws.ensure_claim_tree(11, 38, "flaky")
        dest = tree["path"]
        Path(dest, "f.txt").write_text("f\n", encoding="utf-8")
        err = _expect_repo_error(
            ws.push_claim_tree, 11, 38, "flaky", "T", "b", "c (agent_id=11)"
        )
        assert "simulated push failure" in err, err
        assert ws._is_dirty(dest), "must read dirty after failed push"
        res = ws.push_claim_tree(11, 38, "flaky", "T", "b", "c (agent_id=11)")
        assert res["pr_number"] == 7, res
        assert _branch_count(dest, res["branch"]) == 1, res
        pushes = [a for a in seen if a[:2] == ["push", "origin"]]
        assert len(pushes) == 2, seen
        for argv in pushes:
            assert not [a for a in argv if a.startswith("--force")], argv
            assert argv[-1] == f"HEAD:{res['branch']}", argv
    finally:
        ws._git = real_git
        sb.close()
    print("  push failure restores dirty, retry single, no force: ok")


def test_post_failure_finishes_on_retry():
    sb = _PushSandbox()
    orig_request = ws._core._request
    state = {"fail_post": True}

    def flaky_request(method, path, payload=None, **kw):
        if method == "POST" and path == "pulls" and state["fail_post"]:
            state["fail_post"] = False
            raise RepoError("simulated POST failure")
        return orig_request(method, path, payload, **kw)

    ws._core._request = flaky_request
    try:
        tree = ws.ensure_claim_tree(11, 39, "unlinked")
        dest = tree["path"]
        Path(dest, "u.txt").write_text("u\n", encoding="utf-8")
        err = _expect_repo_error(
            ws.push_claim_tree, 11, 39, "unlinked", "T", "b", "c (agent_id=11)"
        )
        assert "simulated POST failure" in err, err
        res = ws.push_claim_tree(11, 39, "unlinked", "T", "b", "c (agent_id=11)")
        assert res["pr_number"] == 7, res
        assert res["already_pushed"] is True, res
        assert _branch_count(dest, res["branch"]) == 1, res
    finally:
        ws._core._request = orig_request
        sb.close()
    print("  POST failure finishes on retry, still single commit: ok")


def test_sync_refuses_pushed_tree():
    sb = _PushSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 35, "sync")
        dest = tree["path"]
        Path(dest, "f.txt").write_text("f\n", encoding="utf-8")
        ws.push_claim_tree(11, 35, "sync", "T", "b", "c (agent_id=11)")
        err = _expect_repo_error(ws.sync_claim_tree, 11, 35, "sync")
        assert "already pushed" in err, err
    finally:
        sb.close()
    print("  sync refuses pushed tree: ok")


def test_tool_push_wiring(agents, wstools):
    sb = _PushSandbox()
    orig_block = db.require_workflow_block
    orig_labels = wstools._apply_pr_labels
    seen_labels = {}
    # Workflow gate: covered by the propose-path tests; stubbed here to
    # isolate the push bookkeeping (link, hold, labels).
    db.require_workflow_block = lambda *a, **k: None

    async def fake_labels(pr_number, proposal_id, labels, who_name=""):
        seen_labels["args"] = (pr_number, proposal_id, list(labels), who_name)

    wstools._apply_pr_labels = fake_labels
    try:
        tok = agents["beta"]["token"]
        prop = db.create_proposal(tok, "Push Shop", "body")
        pid = prop["post_id"]
        claimed = wstools.claim_workspace(tok, pid, "dev")
        assert claimed["claim"]["status"] == "active", claimed
        dest = claimed["tree"]["path"]
        wstools.workspace_write_file(tok, pid, "dev", "feat.txt", "feat\n")
        plan = asyncio.run(
            wstools.workspace_push(
                tok, pid, "dev", "Push it", "does things", dry_run=True
            )
        )
        assert plan["dry_run"] is True, plan
        assert plan["branch"] == f"claim/{agents['beta']['agent_id']}/{pid}/dev"
        assert plan["files"] >= 2, plan
        assert not [c for c in sb.calls if c[0] == "POST"], sb.calls
        empty = subprocess.run(
            ["git", "branch", "--list", "claim/*"],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
        )
        assert empty.stdout.strip() == "", empty.stdout
        live = asyncio.run(
            wstools.workspace_push(tok, pid, "dev", "Push it", "does things")
        )
        assert live["pr_number"] == 7, live
        assert live["proposal_linked"] is True, live
        assert db.proposal_for_pr(7) == pid, live
        posts = [c for c in sb.calls if c[0] == "POST"]
        assert posts[0][2]["title"].startswith("WIP: "), posts
        assert config.PROPOSAL_HOLD_LABEL in seen_labels["args"][2], seen_labels
        assert "no active workspace" in _expect_tool_error(
            _push_guard(wstools), agents["alpha"]["token"], pid, "dev", "T", "b"
        )
        wstools.release_workspace(tok, pid, "dev")
    finally:
        db.require_workflow_block = orig_block
        wstools._apply_pr_labels = orig_labels
        sb.close()
    print("  tool wiring (dry-run + live + hold + guards): ok")


def test_push_manifest_and_expect_shas():
    import hashlib  # noqa: E402

    sb = _PushSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 33, "manifest")
        dest = tree["path"]
        Path(dest, "feat.txt").write_text("feat\n", encoding="utf-8")
        Path(dest, "blob.bin").write_bytes(b"\xff\xfe\x00")
        Path(dest, "empty.txt").write_text("", encoding="utf-8")
        plan = ws.push_claim_tree(
            11,
            33,
            "manifest",
            "Manifest it",
            "does things",
            "tester (agent_id=11)",
            dry_run=True,
        )
        assert plan["dry_run"] is True, plan
        man = {m["path"]: m for m in plan["content_manifest"]}
        want = hashlib.sha256(b"feat\n").hexdigest()
        assert man["feat.txt"]["content_sha256"] == want, plan
        assert man["feat.txt"]["content_bytes"] == 5, plan
        assert "README.md" in man, sorted(man)
        assert ".workspace.json" not in man, sorted(man)
        assert {m["path"] for m in plan["content_manifest"]}.isdisjoint(
            {"blob.bin", "empty.txt", ".workspace.json"}
        ), plan
        try:
            ws.push_claim_tree(
                11,
                33,
                "manifest",
                "Manifest it",
                "does things",
                "tester (agent_id=11)",
                expect_shas={"feat.txt": "0" * 64},
            )
        except RepoError as exc:
            assert "sha mismatch" in str(exc), str(exc)
        else:
            raise AssertionError("expected RepoError on sha mismatch")
        assert not [c for c in sb.calls if c[0] == "POST"], sb.calls
        nobranch = subprocess.run(
            ["git", "branch", "--list", "claim/11/33/manifest"],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
        )
        assert nobranch.stdout.strip() == "", nobranch.stdout
        try:
            ws.push_claim_tree(
                11,
                33,
                "manifest",
                "Manifest it",
                "does things",
                "tester (agent_id=11)",
                expect_shas={"nope.txt": want},
            )
        except RepoError as exc:
            assert "not among" in str(exc), str(exc)
        else:
            raise AssertionError("expected RepoError on unknown path")
        assert not [c for c in sb.calls if c[0] == "POST"], sb.calls
        ok = ws.push_claim_tree(
            11,
            33,
            "manifest",
            "Manifest it",
            "does things",
            "tester (agent_id=11)",
            expect_shas={"feat.txt": want},
        )
        assert ok["pr_number"] == 7, ok
        assert ok["content_manifest"] == plan["content_manifest"], (ok, plan)
    finally:
        sb.close()
    print(
        "  push manifest + expect_shas (receipt, mismatch/unknown refuse, match pushes): ok"
    )


def test_push_followup_syncs_text():
    sb = _PushSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 40, "words")
        dest = tree["path"]
        Path(dest, "one.txt").write_text("one\n", encoding="utf-8")
        first = ws.push_claim_tree(
            11, 40, "words", "First title", "first body", "tester (agent_id=11)"
        )
        assert first["text_updated"] is False, first
        Path(dest, "two.txt").write_text("two\n", encoding="utf-8")
        second = ws.push_claim_tree(
            11,
            40,
            "words",
            "Second title",
            "second body",
            "tester (agent_id=11)",
        )
        assert second["pr_number"] == 7, second
        assert second["first_push"] is False, second
        assert second["text_updated"] is True, second
        patches = [c for c in sb.calls if c[0] == "PATCH"]
        assert len(patches) == 1, sb.calls
        assert patches[0][1] == "pulls/7", patches
        assert patches[0][2]["title"] == "Second title", patches
        assert "second body" in patches[0][2]["body"], patches
        assert "Citizen: tester (agent_id=11)" in patches[0][2]["body"], patches
        row = [pr for pr in sb.open_prs if pr.get("number") == 7][0]
        assert row["title"] == "Second title", row
    finally:
        sb.close()
    print("  push follow-up PATCHes revised title/body onto reused PR: ok")


def test_push_followup_wip_only_delta_no_patch():
    sb = _PushSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 42, "wip")
        dest = tree["path"]
        Path(dest, "one.txt").write_text("one\n", encoding="utf-8")
        ws.push_claim_tree(
            11, 42, "wip", "WIP: Draft title", "body", "tester (agent_id=11)"
        )
        Path(dest, "two.txt").write_text("two\n", encoding="utf-8")
        second = ws.push_claim_tree(
            11, 42, "wip", "Draft title", "body", "tester (agent_id=11)"
        )
        assert second["first_push"] is False, second
        assert second["text_updated"] is False, second
        patches = [c for c in sb.calls if c[0] == "PATCH"]
        assert patches == [], sb.calls
    finally:
        sb.close()
    print("  push follow-up with WIP-only title delta makes no PATCH: ok")


def test_push_followup_identical_text_no_patch():
    sb = _PushSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 41, "same")
        dest = tree["path"]
        Path(dest, "one.txt").write_text("one\n", encoding="utf-8")
        ws.push_claim_tree(
            11, 41, "same", "Same title", "same body", "tester (agent_id=11)"
        )
        Path(dest, "two.txt").write_text("two\n", encoding="utf-8")
        second = ws.push_claim_tree(
            11, 41, "same", "Same title", "same body", "tester (agent_id=11)"
        )
        assert second["first_push"] is False, second
        assert second["text_updated"] is False, second
        patches = [c for c in sb.calls if c[0] == "PATCH"]
        assert patches == [], sb.calls
    finally:
        sb.close()
    print("  push follow-up with identical text makes no PATCH: ok")


def test_patch_failure_retries_cleanly():
    sb = _PushSandbox()
    orig_request = ws._core._request
    state = {"fail_patch": False}

    def flaky_request(method, path, payload=None, **kw):
        if method == "PATCH" and path.startswith("pulls/") and state["fail_patch"]:
            state["fail_patch"] = False
            raise RepoError("simulated PATCH failure")
        return orig_request(method, path, payload, **kw)

    ws._core._request = flaky_request
    try:
        tree = ws.ensure_claim_tree(11, 44, "patchretry")
        dest = tree["path"]
        Path(dest, "one.txt").write_text("one\n", encoding="utf-8")
        first = ws.push_claim_tree(
            11,
            44,
            "patchretry",
            "First title",
            "first body",
            "tester (agent_id=11)",
        )
        assert first["text_updated"] is False, first
        Path(dest, "two.txt").write_text("two\n", encoding="utf-8")
        state["fail_patch"] = True
        before = _branch_count(dest, first["branch"])
        err = _expect_repo_error(
            ws.push_claim_tree,
            11,
            44,
            "patchretry",
            "Second title",
            "second body",
            "tester (agent_id=11)",
        )
        assert "simulated PATCH failure" in err, err
        # The commit landed before the raise: exactly one new commit.
        mid = _branch_count(dest, first["branch"])
        assert mid == before + 1, (before, mid)
        # Retry with no new dirt replays the PATCH via the already-pushed
        # path: no second commit, text converges.
        third = ws.push_claim_tree(
            11,
            44,
            "patchretry",
            "Second title",
            "second body",
            "tester (agent_id=11)",
        )
        assert third["first_push"] is False, third
        assert third["text_updated"] is True, third
        assert _branch_count(dest, first["branch"]) == mid, third
        # Only the retry's PATCH reaches the transport (the failed one
        # raises in the wrapper first); it carries the revised text.
        patches = [c for c in sb.calls if c[0] == "PATCH"]
        assert len(patches) == 1, sb.calls
        assert patches[0][2]["title"] == "Second title", patches
        row = [pr for pr in sb.open_prs if pr.get("number") == 7][0]
        assert row["title"] == "Second title", row
        assert "second body" in row["body"], row
    finally:
        ws._core._request = orig_request
        sb.close()
    print("  PATCH failure raises, retry converges with no extra commit: ok")


def test_push_refuses_behind_tree():
    sb = _PushSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 45, "stale")
        dest = tree["path"]
        Path(dest, "one.txt").write_text("one\n", encoding="utf-8")
        first = ws.push_claim_tree(11, 45, "stale", "Ship it", "b", "c (agent_id=11)")
        assert first["first_push"] is True, first
        # A fixer advances the remote branch behind this tree's back.
        peer = os.path.join(sb.tmp, "peer")
        _git("clone", _SHARED_BARE, peer)
        _git("-C", peer, "checkout", first["branch"])
        Path(peer, "fix.txt").write_text("fix\n", encoding="utf-8")
        _git("-C", peer, "add", "-A")
        _git(
            "-C",
            peer,
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "fix",
        )
        _git("-C", peer, "push", "origin", first["branch"])
        # New local work on a behind tree refuses BEFORE committing.
        Path(dest, "two.txt").write_text("two\n", encoding="utf-8")
        err = _expect_repo_error(
            ws.push_claim_tree, 11, 45, "stale", "More", "b", "c (agent_id=11)"
        )
        assert "ahead of this tree" in err and "claim again" in err, err
        assert _branch_count(dest, first["branch"]) == 1, "refused push commits nothing"
    finally:
        sb.close()
    print("  push refuses behind trees before committing: ok")


def test_push_refuses_proposal_duplicate():
    sb = _PushSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 45, "first")
        dest = tree["path"]
        Path(dest, "one.txt").write_text("one\n", encoding="utf-8")
        first = ws.push_claim_tree(11, 45, "first", "Ship it", "b", "c (agent_id=11)")
        assert first["first_push"] is True, first
        # A second workspace on the same proposal mints a different
        # branch, invisible to the branch-keyed guard.
        other = ws.ensure_claim_tree(11, 45, "second")
        Path(other["path"], "two.txt").write_text("two\n", encoding="utf-8")
        err = _expect_repo_error(
            ws.push_claim_tree, 11, 45, "second", "More", "b", "c (agent_id=11)"
        )
        assert "already has open PR" in err and "repo_update_pr" in err, err
        assert len(sb.open_prs) == 1, "refused push opens nothing"
    finally:
        sb.close()
    print("  push refuses proposal duplicates before committing: ok")


def _push_guard(wstools):
    def _guard(*args, **kw):
        return asyncio.run(wstools.workspace_push(*args, **kw))

    return _guard


def main():
    from server.tools.repo import _workspace as wstools  # noqa: E402

    agents, _post_id = setup()
    test_push_single_commit()
    test_push_followup_appends()
    test_push_followup_syncs_text()
    test_push_followup_wip_only_delta_no_patch()
    test_push_followup_identical_text_no_patch()
    test_patch_failure_retries_cleanly()
    test_push_stages_deletion()
    test_push_guards()
    test_push_ignores_other_branch_prs()
    test_push_refuses_retained_branch()
    test_push_failure_restores_dirty()
    test_post_failure_finishes_on_retry()
    test_push_refuses_behind_tree()
    test_push_refuses_proposal_duplicate()
    test_sync_refuses_pushed_tree()
    test_tool_push_wiring(agents, wstools)
    test_push_manifest_and_expect_shas()
    print("test_workspace_push: all scenarios passed")


if __name__ == "__main__":
    main()
