"""Test repo tools: search, read, PR planning, patch mode, CI reads, helpers."""
import base64
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_repo_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    db, reports, aggregates, github,
    repo_search, setup,
)


def main():
    agents, post_id = setup()

    # --- repo_search: the walker covers exactly the allowlist --------------
    # search_files reads the checked-out working tree, restricted to an
    # EXTENSION allowlist plus a few named specials, so the database, .env
    # secrets, dependency manifests and binaries are never read, and
    # .git / __pycache__ subtrees are pruned.
    tree = Path(tempfile.mkdtemp(prefix="agentland_search_test_"))
    marker = "needle-in-haystack"
    (tree / "src").mkdir()
    (tree / "src" / "mod.py").write_text(
        "def f():\n    {0} = 1\n".format(marker), encoding="utf-8")
    (tree / "docs").mkdir()
    (tree / "docs" / "guide.md").write_text("see the {0}\n".format(marker), encoding="utf-8")
    (tree / "schema.sql").write_text(
        "CREATE TABLE t (x TEXT); -- {0}\n".format(marker), encoding="utf-8")
    (tree / "deploy").mkdir()
    (tree / "deploy" / "run.sh").write_text("echo {0}\n".format(marker), encoding="utf-8")
    (tree / "ci.yml").write_text("jobs:\n  build: {0}\n".format(marker), encoding="utf-8")
    (tree / ".env.example").write_text("# {0}\nFORUM_X=1\n".format(marker), encoding="utf-8")
    (tree / ".gitignore").write_text("*.pyc\n{0}\n".format(marker), encoding="utf-8")
    (tree / "CODEOWNERS").write_text("* @nssatlantis\n# {0}\n".format(marker), encoding="utf-8")
    # excluded by the allowlist / pruning, however the marker is present
    (tree / ".env").write_text("SECRET={0}\n".format(marker), encoding="utf-8")
    (tree / "forum.db").write_bytes(b"sqlite\x00" + marker.encode() + b"\x00bytes")
    (tree / "requirements.txt").write_text("# {0}\nrequests\n".format(marker), encoding="utf-8")
    (tree / "src" / "notes.txt").write_text("not searchable {0}\n".format(marker), encoding="utf-8")
    (tree / ".git").mkdir()
    (tree / ".git" / "config").write_text("[core]\n\t{0}\n".format(marker), encoding="utf-8")
    pycache = tree / "src" / "__pycache__"
    pycache.mkdir()
    (pycache / "mod.py").write_text("# {0}\n".format(marker), encoding="utf-8")

    res = repo_search.search_files(marker, root=tree)
    assert res["query"] == marker
    got = {m["path"] for m in res["matches"]}
    assert got == {
        "src/mod.py", "docs/guide.md", "schema.sql", "deploy/run.sh",
        "ci.yml", ".env.example", ".gitignore", "CODEOWNERS",
    }, "search must cover exactly the allowlisted files, got {}".format(sorted(got))

    # matches carry 1-based line numbers and the matching text
    mod = next(m for m in res["matches"] if m["path"] == "src/mod.py")
    assert mod["matches"][0]["line_number"] == 2 and marker in mod["matches"][0]["text"]

    # a differently-cased query still hits (case-insensitive substring)
    assert len(repo_search.search_files(marker.upper(), root=tree)["matches"]) == len(res["matches"])

    # excluded files never appear, whichever of their names is asked for
    for q in ("SECRET", "sqlite", "requests", "not searchable", "core"):
        assert all(".env" != m["path"] and not m["path"].endswith((".db", ".txt"))
                   and not m["path"].startswith((".git/", "src/__pycache__/"))
                   for m in repo_search.search_files(q, root=tree)["matches"]), \
            f"query {q!r} must not reach excluded files"

    # long matched lines are trimmed with an ellipsis
    (tree / "src" / "long.py").write_text(
        "x = '{0}'\n".format("y" * 300), encoding="utf-8")
    lmatch = next(m for m in repo_search.search_files("y" * 10, root=tree)["matches"]
                  if m["path"] == "src/long.py")
    ltext = lmatch["matches"][0]["text"]
    assert len(ltext) <= 160 and ltext.endswith("..."), "long lines must be trimmed"

    # max_results bounds the number of files returned
    assert len(repo_search.search_files(marker, max_results=2, root=tree)["matches"]) <= 2

    # empty / too-short / too-long queries are rejected
    for q in ("", "x", "x" * 201):
        try:
            repo_search.search_files(q, root=tree)
        except github.RepoError:
            pass
        else:
            raise AssertionError(f"search should reject query {q!r}")

    shutil.rmtree(tree, ignore_errors=True)

    # --- repo_read_file _slice_line_range: pure slice logic, no token ------
    # The MCP smoke in test_client.py is GITHUB_TOKEN-gated (CI never sets a
    # token, so the feature never runs there), but _slice_line_range is pure -
    # test it directly: exact slice semantics, trailing-newline total_lines,
    # both-or-neither, start<1, end<start, past-end names total, over-cap names
    # 1000, and the exact error wording (locks the message fix).
    no_nl = "alpha\nbeta\ngamma"
    content, total = github._slice_line_range("t.txt", no_nl, 1, 3)
    assert content == no_nl and total == 3, \
        "a 1..total_lines range reconstructs a file without a trailing newline exactly"
    assert github._slice_line_range("t.txt", no_nl, 2, 3) == ("beta\ngamma", 3), \
        "a range slice is the exact 1-based inclusive cut"
    assert github._slice_line_range("t.txt", no_nl, 1, 1) == ("alpha", 3), \
        "a single-line range returns just that line"
    assert github._slice_line_range("t.txt", no_nl, 3, 3) == ("gamma", 3), \
        "the last line of a no-newline file is a valid single-line range"

    with_nl = "alpha\nbeta\n"
    content, total = github._slice_line_range("t.txt", with_nl, 1, 3)
    assert content == with_nl and total == 3, \
        "a file ending in a newline reports one extra, empty final line"
    assert github._slice_line_range("t.txt", with_nl, 1, 2) == ("alpha\nbeta", 3), \
        "the extra final line never leaks into a 1..2 range"
    assert github._slice_line_range("t.txt", with_nl, 3, 3) == ("", 3), \
        "the final range line of a trailing-newline file is the empty part"

    # both-or-neither: a lone param errors naming the one that WAS provided
    try:
        github._slice_line_range("t.txt", "a\nb", 1, None)
    except github.RepoError as e:
        assert str(e) == ("repo_read_file line range: line_start was given without "
                          "its pair - 'line_start' and 'line_end' must be passed together."), \
            f"a lone line_start must name line_start as given: {e}"
    else:
        raise AssertionError("a lone line_start must error")
    try:
        github._slice_line_range("t.txt", "a\nb", None, 2)
    except github.RepoError as e:
        assert str(e) == ("repo_read_file line range: line_end was given without "
                          "its pair - 'line_start' and 'line_end' must be passed together."), \
            f"a lone line_end must name line_end as given: {e}"
    else:
        raise AssertionError("a lone line_end must error")

    for start, end in ((0, 2), (-1, 2)):
        try:
            github._slice_line_range("t.txt", no_nl, start, end)
        except github.RepoError as e:
            assert f"'line_start' must be >= 1, got {start}" in str(e), \
                f"a start below 1 must error naming the value: {e}"
        else:
            raise AssertionError(f"start {start} must error")
    try:
        github._slice_line_range("t.txt", no_nl, 10, 5)
    except github.RepoError as e:
        assert "'line_end' must be >= 'line_start' (10), got 5" in str(e), \
            f"an end below start must error naming both values: {e}"
    else:
        raise AssertionError("an end below start must error")
    content, total = github._slice_line_range("t.txt", no_nl, 1, 4)
    assert content == no_nl and total == 3, \
        "a range past the end is clamped to total_lines, returning all available lines"
    try:
        github._slice_line_range("t.txt", "a\nb", 1, 1001)
    except github.RepoError as e:
        assert "1001 lines is too large - at most 1000 lines per read" in str(e), \
            f"a range over the cap must name the cap, not the file: {e}"
    else:
        raise AssertionError("a range over the cap must error")

    # --- PR outcome classification (repo_get_pr) ---------------------------
    assert github._pr_outcome({"state": "open", "merged_at": None, "labels": []}) == "open"
    assert github._pr_outcome({
        "state": "closed", "merged_at": "2026-08-11T00:00:00Z", "labels": [],
    }) == "merged", "a closed PR with merged_at is merged"
    assert github._pr_outcome({
        "state": "closed", "merged_at": None, "labels": [{"name": "declined"}],
    }) == "declined", "a closed PR with a declined label is declined"
    assert github._pr_outcome({
        "state": "closed", "merged_at": None, "labels": [{"name": "DECLINED"}],
    }) == "declined", "the declined label matches case-insensitively"
    assert github._pr_outcome({"state": "closed", "merged_at": None, "labels": []}) == "closed", \
        "a closed PR with no merge or label is closed-other"
    assert github._pr_outcome({
        "state": "closed", "merged_at": "2026-08-11T00:00:00Z",
        "labels": [{"name": "declined"}],
    }) == "merged", "a merged PR stays merged even with a declined label"
    assert github._pr_outcome({}) == "open", "an unlabelled, open-shaped PR defaults to open"

    # --- multi-file PR planning (repo_propose_change -> propose_change) ---
    # dry_run plans never touch GitHub, so this is safe to test anywhere. The
    # plan must list every file the PR will touch, one commit each, with the
    # citizen trailer attached.
    plan = github.propose_change(
        [
            {"path": "docs/one.md", "content": "one"},
            {"path": "docs/two.md", "content": "two"},
        ],
        title="multi-file change",
        body="implements the plan",
        citizen="curious-alpha (agent_id=3)",
        dry_run=True,
    )
    assert plan["dry_run"] is True
    assert plan["changes"] == ["docs/one.md", "docs/two.md"], \
        "the plan must list every file the PR will touch"
    assert plan["commit_message"] == "multi-file change\n\nCitizen: curious-alpha (agent_id=3)", \
        "the citizen trailer rides along on every commit"
    assert plan["branch"].startswith("proposal/"), "a proposal-named branch is auto-generated"
    assert plan["content_manifest"] == [
        {"path": "docs/one.md", "content_bytes": 3,
         "content_sha256": hashlib.sha256(b"one").hexdigest()},
        {"path": "docs/two.md", "content_bytes": 3,
         "content_sha256": hashlib.sha256(b"two").hexdigest()},
    ], "the plan must echo per-file byte counts and sha256 of what was received"

    # --- empty content is rejected (repo content integrity) ---
    # The #70 failure mode: a payload that arrives empty must never open a PR
    # (or an empty-file commit). Deletion is the update path's delete op.
    try:
        github.propose_change(
            [{"path": "db.py", "content": ""}], title="empty", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("empty content must be rejected by propose_change")
    except github.RepoError as exc:
        assert "empty" in str(exc), str(exc)
    try:
        github.update_pr(
            1, [{"path": "db.py", "content": ""}],
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("empty content must be rejected by update_pr")
    except github.RepoError as exc:
        assert "empty" in str(exc), str(exc)

    # --- patch / find-replace mode (PR #72) ---
    # The pure apply core is network-free and deliberately strict: exact
    # substring find-replace applied IN ORDER (each against the result of the
    # previous), every find matching exactly once or the requested occurrence
    # - never a guess the caller cannot see to correct.
    out, log = github._apply_edits("docs/f.txt", "one two one", [
        {"find": "one", "replace": "1", "occurrence": 2},
        {"find": "two", "replace": "2"},
    ])
    assert out == "one 2 1", out
    assert log == [
        {"find": "one", "replace": "1", "occurrence": 2, "matched": 2},
        {"find": "two", "replace": "2", "occurrence": 1, "matched": 1},
    ], log

    out, _ = github._apply_edits("docs/f.txt", "a\nbb\nccc\n", [
        {"find": "bb", "replace": "B"},
    ])
    assert out == "a\nB\nccc\n", out

    # an empty replace deletes the matched block
    out, _ = github._apply_edits("docs/f.txt", "keep\n\n// TODO drop\nkeep2\n", [
        {"find": "\n// TODO drop", "replace": ""},
    ])
    assert out == "keep\n\nkeep2\n", out

    # finds may span lines and carry unicode
    out, _ = github._apply_edits("docs/f.txt", "hé\nwörld", [
        {"find": "é\nwö", "replace": "E/W"},
    ])
    assert out == "hE/Wrld", out

    # a find that never matches fails closed, with a re-read hint
    try:
        github._apply_edits("docs/f.txt", "abc", [{"find": "xyz", "replace": "q"}])
        raise AssertionError("a find that doesn't match must error")
    except github.RepoError as exc:
        assert "did not match" in str(exc), str(exc)

    # an ambiguous find (2+ matches, no occurrence) is an error, not a guess
    try:
        github._apply_edits("docs/f.txt", "a a a", [{"find": "a", "replace": "b"}])
        raise AssertionError("an ambiguous find must error")
    except github.RepoError as exc:
        assert "occurrence" in str(exc), str(exc)

    # an out-of-range occurrence is an error
    try:
        github._apply_edits("docs/f.txt", "a", [{"find": "a", "replace": "b", "occurrence": 2}])
        raise AssertionError("an out-of-range occurrence must error")
    except github.RepoError as exc:
        assert "out of range" in str(exc), str(exc)

    # edits shape validation at the github layer (mirrors server.py's normalizer)
    for bad, needle in [
        ("nope", "edits"),
        ([], "edits"),
        ([{"replace": "x"}], "find"),
        ([{"find": "", "replace": "x"}], "find"),
        ([{"find": "a"}], "replace"),
        ([{"find": "a", "replace": "x", "occurrence": 0}], "occurrence"),
        ([{"find": "a", "replace": "x", "occurrence": True}], "occurrence"),
        ([{"find": "a", "replace": "x", "occurrence": None}], "occurrence"),
    ]:
        try:
            github._validate_edits("docs/f.txt", bad)
            raise AssertionError(f"malformed edits {bad!r} must be rejected")
        except github.RepoError as exc:
            assert needle in str(exc), (bad, str(exc))
    try:
        github._validate_edits(
            "docs/f.txt",
            [{"find": "x", "replace": "y"}] * (github._MAX_EDITS_PER_FILE + 1),
        )
        raise AssertionError("too many edits must be rejected")
    except github.RepoError as exc:
        assert "too many edits" in str(exc), str(exc)

    # the pure apply core also refuses an empty find directly - _validate_edits
    # catches it upstream, but a direct call must error, not spin forever.
    try:
        github._apply_edits("docs/f.txt", "abc", [{"find": "", "replace": "x"}])
        raise AssertionError("an empty find must error, not loop")
    except github.RepoError as exc:
        assert "must not be empty" in str(exc), str(exc)

    # an empty edits list is a legal no-op for the pure core (the validators
    # demand non-empty, but a direct call just passes the text through)
    out, log = github._apply_edits("docs/f.txt", "abc", [])
    assert (out, log) == ("abc", []), (out, log)

    # ops apply in order against the RESULT of the previous op, so a find may
    # match text an earlier op just introduced
    out, _ = github._apply_edits("docs/f.txt", "a b", [
        {"find": "a", "replace": "x"},
        {"find": "x", "replace": "y"},
    ])
    assert out == "y b", out

    # direct calls are defensively guarded against malformed replace /
    # occurrence types - the validators catch these upstream, but the pure
    # core must raise a clean error, not a raw TypeError.
    for bad, needle in [
        ({"find": "a", "replace": 42}, "replace"),
        ({"find": "a", "replace": "x", "occurrence": None}, "occurrence"),
    ]:
        try:
            github._apply_edits("docs/f.txt", "a", [bad])
            raise AssertionError(f"malformed direct-call op {bad!r} must error")
        except github.RepoError as exc:
            assert needle in str(exc), (bad, str(exc))

    # --- patch resolution against a fake GitHub ---
    real_request = github._request

    # the github layer enforces one write mode per entry (server.py's
    # normalizer does too) - rejected before a single GitHub read, standalone
    # callers included.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        raise AssertionError(f"exclusivity must be rejected before any request: {method} {path}")

    github._request = fake_request
    try:
        github.propose_change(
            [{"path": "README.md", "content": "x",
              "edits": [{"find": "a", "replace": "b"}]}],
            title="t", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("content and edits on one entry must be rejected")
    except github.RepoError as exc:
        assert "both 'content' and 'edits'" in str(exc), str(exc)
    finally:
        github._request = real_request
    assert calls == [], "the exclusivity rejection must not hit GitHub"

    calls = []
    github._request = fake_request
    try:
        github.update_pr(
            1,
            [{"path": "app.py", "delete": True,
              "edits": [{"find": "a", "replace": "b"}]}],
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("edits and delete on one entry must be rejected")
    except github.RepoError as exc:
        assert "more than one of" in str(exc), str(exc)
    finally:
        github._request = real_request
    assert calls == [], "the exclusivity rejection must not hit GitHub"

    calls = []
    github._request = fake_request
    try:
        github.update_pr(
            1,
            [{"path": "app.py"}],
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("an entry with no write mode must be rejected")
    except github.RepoError as exc:
        assert "needs 'content', 'edits' or 'delete'" in str(exc), str(exc)
    finally:
        github._request = real_request
    assert calls == [], "the no-mode rejection must not hit GitHub"

    # content-mode entries must carry a real non-empty string: null (the key
    # present with a null value - .get returns None, not the default) and
    # non-string values crash the manifest encoding if they get through.
    for bad in (None, 42, 1.5, ["x"]):
        try:
            github.propose_change(
                [{"path": "README.md", "content": bad}], title="t", body="b",
                citizen="curious-alpha (agent_id=3)", dry_run=True,
            )
            raise AssertionError(f"propose_change must reject content {bad!r}")
        except github.RepoError as exc:
            assert "non-empty string" in str(exc), (bad, str(exc))
        try:
            github.update_pr(
                1, [{"path": "app.py", "content": bad}],
                citizen="curious-alpha (agent_id=3)", dry_run=True,
            )
            raise AssertionError(f"update_pr must reject content {bad!r}")
        except github.RepoError as exc:
            assert "non-empty string" in str(exc), (bad, str(exc))
    try:
        github.propose_change(
            [{"path": "README.md"}], title="t", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("a change without 'content' must be rejected")
    except github.RepoError as exc:
        assert "non-empty string" in str(exc), str(exc)

    # patch dry_run resolves the base with exactly one read (a patch can't be
    # previewed without it), the manifest carries the APPLIED result, and
    # patch_log echoes every op.
    calls = []
    base_b64 = base64.b64encode(b"old\nmiddle\nend\n").decode("ascii")

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path.startswith("contents/README.md?ref="):
            return {"content": base_b64, "sha": "base-sha"}
        raise AssertionError(f"dry-run patch must only fetch the base, got {method} {path}")

    github._request = fake_request
    try:
        plan = github.propose_change(
            [{"path": "README.md", "edits": [{"find": "middle", "replace": "patched"}]}],
            title="patch demo", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
    finally:
        github._request = real_request
    assert calls == [("GET", "contents/README.md?ref=main")], calls
    assert plan["changes"] == ["README.md"]
    assert plan["content_manifest"] == [{
        "path": "README.md",
        "content_bytes": len(b"old\npatched\nend\n"),
        "content_sha256": hashlib.sha256(b"old\npatched\nend\n").hexdigest(),
    }], "the manifest must describe the APPLIED patch result"
    assert plan["patch_log"] == [{
        "path": "README.md",
        "edits": [{"find": "middle", "replace": "patched", "occurrence": 1, "matched": 1}],
    }], plan["patch_log"]

    # update_pr's manifest is computed for a valid content write too (not
    # just propose_change): dry_run needs only the ownership PR read.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "pulls/9":
            return {"state": "open", "head": {"ref": "feature/x"}, "title": "T"}
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    try:
        plan = github.update_pr(
            9, [{"path": "db.py", "content": "x"}],
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
    finally:
        github._request = real_request
    assert plan["content_manifest"] == [{
        "path": "db.py", "content_bytes": 1,
        "content_sha256": hashlib.sha256(b"x").hexdigest(),
    }], "update_pr must echo the manifest for a valid content write"
    assert calls == [("GET", "pulls/9")], calls

    # the manifest counts UTF-8 bytes, not characters
    plan = github.propose_change(
        [{"path": "docs/u.md", "content": "héllo"}], title="unicode", body="b",
        citizen="curious-alpha (agent_id=3)", dry_run=True,
    )
    assert plan["content_manifest"] == [{
        "path": "docs/u.md", "content_bytes": 6,
        "content_sha256": hashlib.sha256("héllo".encode("utf-8")).hexdigest(),
    }], plan["content_manifest"]

    # content-mode dry_run stays 100% network-free (regression for #71)
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        raise AssertionError("content-mode dry-run must not touch GitHub")

    github._request = fake_request
    try:
        plan = github.propose_change(
            [{"path": "docs/new.md", "content": "hello"}],
            title="t", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
    finally:
        github._request = real_request
    assert calls == [], f"content-mode dry-run made {len(calls)} GitHub request(s)"
    assert plan["patch_log"] == []

    # a patch on a file that does not exist (ok_404 -> None) fails closed
    def fake_request(method, path, body=None, ok_404=False):
        assert method == "GET"
        return None

    github._request = fake_request
    try:
        github.propose_change(
            [{"path": "nope.md", "edits": [{"find": "x", "replace": "y"}]}],
            title="t", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("patching a missing file must error")
    except github.RepoError as exc:
        assert "use 'content' to create" in str(exc), str(exc)
    finally:
        github._request = real_request

    # a binary file (non-UTF-8) can't be patched
    def fake_request(method, path, body=None, ok_404=False):
        assert method == "GET"
        return {"content": base64.b64encode(b"\xff\xfe\x00binary").decode("ascii"), "sha": "s"}

    github._request = fake_request
    try:
        github.propose_change(
            [{"path": "logo.png", "edits": [{"find": "x", "replace": "y"}]}],
            title="t", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("patching a binary file must error")
    except github.RepoError as exc:
        assert "not UTF-8" in str(exc), str(exc)
    finally:
        github._request = real_request

    # a real (non-dry-run) patch PUT carries the applied content and the base
    # sha, sharing the resolution GET - no extra round-trips.
    calls = []
    base_b64 = base64.b64encode(b"v1\nkeep\n").decode("ascii")

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path.startswith("contents/README.md?ref="):
            return {"content": base_b64, "sha": "base-sha"}
        if method == "GET" and path.startswith("git/ref/heads/"):
            return {"object": {"sha": "head-sha"}}
        if method == "POST" and path == "git/refs":
            return {"ref": "refs/heads/proposal/x", "object": {"sha": "head-sha"}}
        if method == "PUT" and path == "contents/README.md":
            assert body["sha"] == "base-sha", body
            assert body["content"] == base64.b64encode(b"v2\nkeep\n").decode("ascii"), body
            return {"content": {"sha": "put-sha"}}
        if method == "POST" and path == "pulls":
            return {"number": 7, "html_url": "https://github.com/x/y/pull/7"}
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    try:
        plan = github.propose_change(
            [{"path": "README.md", "edits": [{"find": "v1", "replace": "v2"}]}],
            title="patch real", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=False,
        )
    finally:
        github._request = real_request
    assert plan["pr_number"] == 7
    assert plan["content_manifest"][0]["content_sha256"] == \
        hashlib.sha256(b"v2\nkeep\n").hexdigest(), "real-path manifest is the applied result"
    assert ("GET", "contents/README.md?ref=main") in calls
    assert calls.count(("PUT", "contents/README.md")) == 1

    # repo_update_pr resolves patch entries against the PR BRANCH head (so a
    # patch stacks on the PR's own earlier commits), not the base branch.
    calls = []
    base_b64 = base64.b64encode(b"orig\n").decode("ascii")

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "pulls/9":
            return {"state": "open", "head": {"ref": "feature/x"}, "title": "T"}
        if method == "GET" and path.startswith("contents/app.py?ref=feature/x"):
            return {"content": base_b64, "sha": "br-sha"}
        if method == "PUT" and path == "contents/app.py":
            assert body["sha"] == "br-sha", body
            assert body["content"] == base64.b64encode(b"new\n").decode("ascii"), body
            return {"content": {"sha": "x"}}
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    try:
        plan = github.update_pr(
            9,
            [{"path": "app.py", "edits": [{"find": "orig", "replace": "new"}]}],
            citizen="curious-alpha (agent_id=3)", dry_run=False,
        )
    finally:
        github._request = real_request
    assert plan["patch_log"] == [{
        "path": "app.py",
        "edits": [{"find": "orig", "replace": "new", "occurrence": 1, "matched": 1}],
    }], plan["patch_log"]
    assert plan["content_manifest"][0]["content_sha256"] == hashlib.sha256(b"new\n").hexdigest()
    assert ("GET", "contents/app.py?ref=feature/x") in calls

    # update_pr accepts the forum-facing get_pr() result as _pr: there head
    # is a string, not a dict - the exact shape server.py repo_update_pr
    # passes. A forum dict used to raise TypeError here (string indices must
    # be integers, not 'str').
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    try:
        plan = github.update_pr(
            9,
            [{"path": "app.py", "content": "fresh\n"}],
            title="T2",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
            _pr={"state": "open", "head": "feature/x", "title": "T"},
        )
    finally:
        github._request = real_request
    assert plan["dry_run"] is True
    assert plan["branch"] == "feature/x"
    assert plan["changes"] == ["app.py"]
    assert not calls, "the dry-run must not touch GitHub"

    # --- repo CI reads: tiered checks, commits, read-at-ref, list_prs ------
    # pr_checks tries check runs, then Actions runs, then the combined commit
    # status; each tier's failure falls into the next, and a total outage
    # degrades to 'unknown', never an error.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "pulls/9":
            return {"head": {"sha": "abc123"}, "state": "open"}
        if method == "GET" and path.startswith("commits/abc123/check-runs"):
            raise github.RepoError("GitHub API 403 on GET commits/abc123/check-runs")
        if method == "GET" and path.startswith("actions/runs?head_sha=abc123"):
            return {"workflow_runs": [
                {"id": 11, "name": "CI", "status": "completed", "conclusion": "failure",
                 "html_url": "https://github.com/x/y/actions/runs/11"},
                {"id": 12, "name": "static", "status": "completed", "conclusion": "success",
                 "html_url": "https://github.com/x/y/actions/runs/12"},
            ]}
        if method == "GET" and path == "actions/runs/11/jobs?per_page=100":
            return {"jobs": [
                {"id": 111, "name": "test", "status": "completed", "conclusion": "failure"},
                {"id": 112, "name": "other", "status": "completed", "conclusion": "success"},
            ]}
        if method == "GET" and path == "actions/jobs/111/logs":
            return "ok\n== GET /api/posts -> 200 ==\nFAILED test_thing\nError: mypy found 2 errors\n"
        raise AssertionError(f"unexpected request {method} {path}")

    real_request_text = github._request_text
    github._request = fake_request
    github._request_text = fake_request
    github.clear_cache()
    try:
        checks = github.pr_checks(9)
    finally:
        github._request = real_request
        github._request_text = real_request_text
    assert checks["source"] == "actions", checks
    assert checks["state"] == "failure", checks
    assert [r["name"] for r in checks["runs"]] == ["CI", "static"], checks["runs"]
    assert checks["failures"][0]["name"] == "CI / test", checks["failures"]
    assert checks["failures"][0]["log_url"] == \
        "https://github.com/nssatlantis/agent_land/actions/runs/11/job/111", checks["failures"]
    assert any("mypy" in f["message"] for f in checks["failures"]), checks["failures"]
    assert ("GET", "commits/abc123/check-runs?per_page=50") in calls, calls
    assert ("GET", "actions/runs?head_sha=abc123&per_page=50") in calls, calls

    # the check-runs tier wins when it answers (no Actions fallback), and its
    # failure annotations carry path/line/message.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "pulls/9":
            return {"head": {"sha": "def456"}, "state": "open"}
        if method == "GET" and path.startswith("commits/def456/check-runs"):
            return {"check_runs": [
                {"id": 21, "name": "test", "status": "completed", "conclusion": "failure",
                 "html_url": "u21"},
                {"id": 22, "name": "static", "status": "in_progress", "conclusion": None,
                 "html_url": "u22"},
            ]}
        if method == "GET" and path == "check-runs/21/annotations?per_page=100":
            return [{"path": "db.py", "start_line": 42, "message": "undefined name 'x'"}]
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    github.clear_cache()
    try:
        checks = github.pr_checks(9)
    finally:
        github._request = real_request
    assert checks["source"] == "check_runs", checks
    assert checks["state"] == "failure", checks
    assert checks["failures"] == [{
        "name": "test", "path": "db.py", "line": 42,
        "message": "undefined name 'x'", "log_url": "u21",
    }], checks["failures"]
    assert ("GET", "actions/runs?head_sha=def456&per_page=50") not in calls, calls

    # empty check runs and empty Actions fall through to the combined commit
    # status tier; its failure entries carry the description.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "pulls/9":
            return {"head": {"sha": "ghi789"}, "state": "open"}
        if method == "GET" and path.startswith("commits/ghi789/check-runs"):
            raise github.RepoError("GitHub API 404")
        if method == "GET" and path.startswith("actions/runs?head_sha=ghi789"):
            return {"workflow_runs": []}
        if method == "GET" and path == "commits/ghi789/status":
            return {"state": "failure", "total_count": 1, "statuses": [
                {"context": "continuous-integration", "state": "failure",
                 "description": "The CI build failed", "target_url": "https://ci/x/1"},
            ]}
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    github.clear_cache()
    try:
        checks = github.pr_checks(9)
    finally:
        github._request = real_request
    assert checks["source"] == "statuses", checks
    assert checks["state"] == "failure", checks
    assert checks["failures"][0]["message"] == "The CI build failed", checks["failures"]
    assert checks["runs"][0]["conclusion"] == "failure", checks["runs"]

    # a total outage degrades to 'unknown' with empty runs/failures.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "pulls/9":
            return {"head": {"sha": "jkl999"}, "state": "open"}
        raise github.RepoError("GitHub API 500 on everything")

    github._request = fake_request
    github.clear_cache()
    try:
        checks = github.pr_checks(9)
    finally:
        github._request = real_request
    assert checks["state"] == "unknown" and checks["source"] is None, checks
    assert checks["runs"] == [] and checks["failures"] == [], checks

    # pr_commits paginates (100/page) and carries sha/message/author/date.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "pulls/9":
            return {"head": {"ref": "feature/x"}, "base": {"ref": "main"}, "state": "open"}
        if method == "GET" and path == "pulls/9/commits?per_page=100&page=1":
            return [{"sha": f"s{i}", "commit": {"message": f"m{i}",
                    "author": {"name": "a", "date": "d"}}} for i in range(100)]
        if method == "GET" and path == "pulls/9/commits?per_page=100&page=2":
            return [{"sha": "s100", "commit": {"message": "m100",
                    "author": {"name": "a", "date": "d"}}}]
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    github.clear_cache()
    try:
        commits = github.pr_commits(9)
    finally:
        github._request = real_request
    assert len(commits["commits"]) == 101, commits
    assert commits["commits"][-1]["sha"] == "s100", commits
    assert commits["head"] == "feature/x" and commits["base"] == "main", commits
    assert ("GET", "pulls/9/commits?per_page=100&page=2") in calls, calls

    # read_file's optional ref passes through to the contents API and echoes
    # in the response; the default stays the base branch.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "contents/README.md?ref=deadbeef":
            return {"content": base64.b64encode(b"# hi\n").decode("ascii"), "size": 5}
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    try:
        got = github.read_file("README.md", ref="deadbeef")
    finally:
        github._request = real_request
    assert got["ref"] == "deadbeef", got
    assert got["content"] == "# hi\n", got

    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        return None

    github._request = fake_request
    try:
        github.read_file("nope.py", ref="noref")
        raise AssertionError("reading a missing file at a ref must error")
    except github.RepoError as exc:
        assert "noref" in str(exc), str(exc)
    finally:
        github._request = real_request
    assert calls == [("GET", "contents/nope.py?ref=noref")], calls

    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "contents/README.md?ref=main":
            return {"content": base64.b64encode(b"x\n").decode("ascii"), "size": 2}
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    try:
        got = github.read_file("README.md")
    finally:
        github._request = real_request
    assert got["ref"] == "main", got

    # list_prs: closed/all rows carry the lifecycle, `since` filters on
    # updated_at, and bad arguments are named in the error.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "pulls?state=closed&sort=updated&direction=desc&per_page=50":
            return [
                {"number": 1, "title": "a", "head": {"ref": "h"}, "base": {"ref": "main"},
                 "user": {"login": "u"}, "created_at": "2026-08-01T00:00:00Z",
                 "updated_at": "2026-08-15T00:00:00Z", "state": "closed",
                 "merged_at": "2026-08-14T00:00:00Z", "closed_at": "2026-08-15T00:00:00Z",
                 "html_url": "u1"},
                {"number": 2, "title": "b", "head": {"ref": "h2"}, "base": {"ref": "main"},
                 "user": {"login": "u"}, "created_at": "2026-07-01T00:00:00Z",
                 "updated_at": "2026-07-20T00:00:00Z", "state": "closed",
                 "merged_at": None, "closed_at": "2026-07-20T00:00:00Z",
                 "html_url": "u2"},
            ]
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    try:
        closed = github.list_prs(state="closed", since="2026-08-01T00:00:00Z")
    finally:
        github._request = real_request
    assert [r["number"] for r in closed] == [1], closed
    assert closed[0]["outcome"] == "merged" and closed[0]["merged_at"], closed
    assert ("GET", "pulls?state=closed&sort=updated&direction=desc&per_page=50") in calls

    try:
        github.list_prs(state="bogus")
        raise AssertionError("a bogus state must error")
    except github.RepoError as exc:
        assert "state must be 'open', 'closed' or 'all'" in str(exc), str(exc)

    try:
        github.list_prs(since="not-a-date")
        raise AssertionError("a bogus since must error")
    except github.RepoError as exc:
        assert "ISO-8601" in str(exc), str(exc)

    try:
        github.list_prs(since="2026-08-18T00:00:00+05:30")
        raise AssertionError("a non-Z since must error")
    except github.RepoError as exc:
        assert "ending in 'Z'" in str(exc), str(exc)

    # list_prs(state='open', since=...) filters the cached open list on
    # created_at, so only open PRs created at or after the since timestamp
    # are returned.
    open_calls = []

    def fake_open_request(method, path, body=None, ok_404=False):
        open_calls.append((method, path))
        if method == "GET" and path.startswith("pulls?state=open"):
            return [
                {"number": 10, "title": "new", "head": {"ref": "h"}, "base": {"ref": "main"},
                 "user": {"login": "u"}, "created_at": "2026-08-10T00:00:00Z",
                 "html_url": "u10", "mergeable_state": "clean", "body": ""},
                {"number": 11, "title": "old", "head": {"ref": "h2"}, "base": {"ref": "main"},
                 "user": {"login": "u"}, "created_at": "2026-07-01T00:00:00Z",
                 "html_url": "u11", "mergeable_state": "clean", "body": ""},
            ]
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_open_request
    try:
        github.clear_cache()
        opened = github.list_prs(state="open", since="2026-08-01T00:00:00Z")
    finally:
        github._request = real_request
        github.clear_cache()
    assert [r["number"] for r in opened] == [10], opened

    fresh_before = db.whoami(agents["fresh"]["token"])["karma"]
    assert fresh_before == 0, "fresh agent should still be at 0 karma"
    assert db.award_pr_merge_karma(101, agents["fresh"]["agent_id"], "2026-08-11T00:00:00Z") is True
    assert db.award_pr_merge_karma(101, agents["fresh"]["agent_id"], "2026-08-11T00:00:00Z") is False, \
        "re-awarding the same PR must be a no-op"
    fresh_after = db.whoami(agents["fresh"]["token"])["karma"]
    assert fresh_after == fresh_before + 1, "a merged PR credits exactly PR_MERGE_KARMA karma"
    assert db.award_pr_merge_karma(102, 999999, "2026-08-11T00:00:00Z") is False, \
        "merges credited to a missing agent must be skipped, not crash"
    by_id = {a["id"]: a for a in aggregates.list_agents()}
    assert by_id[agents["fresh"]["agent_id"]]["karma"] == fresh_before + 1, \
        "list_agents must include merge karma"
    assert by_id[agents["fresh"]["agent_id"]]["last_active"] >= by_id[agents["fresh"]["agent_id"]]["created_at"], \
        "list_agents must expose last_active, falling back to the join date"
    # Merge karma is the same number used by the gates: fresh can now report.
    reports.report_content(agents["fresh"]["token"], "post", post_id, "now earned")

    # --- github pure helpers: path validation, markdown, base64, status -----
    # These network-free helpers are exercised only through their callers
    # today; pin their contracts directly so a regressions is caught at the
    # unit, not via a full PR flow.
    #
    # _validate_path: relative, no traversal, no leading slash, no empty
    # segments - the guard standing between user input and the contents API.
    assert github._validate_path("db.py") == "db.py"
    assert github._validate_path("src/util/thing.py") == "src/util/thing.py"
    for bad in ("", "  ", "/etc/passwd", "../secret", "a/../b", "a//b", "a/./b", "a/", "a/.."):
        try:
            github._validate_path(bad)
        except github.RepoError as exc:
            assert "path" in str(exc), (bad, exc)
        else:
            raise AssertionError(f"_validate_path must reject {bad!r}")
    # _escape_md: backslash-escape the markdown-significant chars so a title
    # with stars/underscores/brackets/backticks renders as plain text.
    assert github._escape_md("a*b_c[d]e`f`g\\h") == \
        "a\\*b\\_c\\[d\\]e\\`f\\`g\\\\h", github._escape_md("a*b_c[d]e`f`g\\h")
    assert github._escape_md("plain text") == "plain text"
    assert github._escape_md("") == ""
    # _decode_content_text: base64 round-trip; non-UTF-8 bytes are binary and
    # patch mode must refuse them (read_file instead serves a note).
    assert github._decode_content_text("a.py", {"content": base64.b64encode(
        "hello\n".encode("utf-8")).decode("ascii")}) == "hello\n"
    try:
        github._decode_content_text("a.py", {"content": base64.b64encode(
            b"\xff\xfe\x00").decode("ascii")})
        raise AssertionError("binary content must be refused by patch decode")
    except github.RepoError as exc:
        assert "not UTF-8" in str(exc) and "binary" in str(exc), str(exc)
    try:
        github._decode_content_text("a.py", None)
        raise AssertionError("a missing file must be refused by patch decode")
    except github.RepoError as exc:
        assert "use 'content' to create" in str(exc), str(exc)
    # _checks_for_head: maps the combined commit-status API to the tiered
    # green/red shape (source='statuses'), and never raises when GitHub is
    # unreachable (a failure -> None, so the PR view degrades instead of
    # erroring).
    real_request = github._request
    try:
        calls = []
        github._request = lambda method, path, body=None, ok_404=False: (
            calls.append((method, path)) or {"state": "failure", "total_count": 1}
        )
        checks = github._checks_for_head("abc123")
        assert checks["source"] == "statuses" and checks["state"] == "failure", checks
        github._request = lambda method, path, body=None, ok_404=False: (
            calls.append((method, path)) or {"state": "success", "total_count": 0}
        )
        assert github._checks_for_head("abc123")["state"] == "success"
        github._request = lambda method, path, body=None, ok_404=False: (
            calls.append((method, path)) or (_ for _ in ()).throw(github.RepoError("down"))
        )
        assert github._checks_for_head("abc123") is None, \
            "an unreachable GitHub must degrade to None, not raise"
    finally:
        github._request = real_request
    tier_probe = [("GET", "commits/abc123/check-runs?per_page=50"),
                  ("GET", "actions/runs?head_sha=abc123&per_page=50"),
                  ("GET", "commits/abc123/status")]
    assert calls == tier_probe * 3, \
        "empty check-runs and Actions tiers fall through to the commit-status endpoint"
    print("  github pure helpers: ok")

    # --- github.recently_closed_prs: parse the poller's input shape ---------
    # The outcome poller reads closed PRs and classifies each one. The parse
    # runs through the same fake _request as open_prs; assert the mapping
    # (citizen trailer, proposal stamp, labels) reaches the returned rows.
    real_request = github._request
    try:
        calls = []
        github._request = lambda method, path, body=None, ok_404=False: (
            calls.append((method, path)) or [
                {"number": 5, "title": "t", "user": {"login": "bob"},
                 "merged_at": "2026-08-11T00:00:00Z", "closed_at": "2026-08-11T01:00:00Z",
                 "labels": [{"name": "declined"}],
                 "body": "stuff\n\nCitizen: curious-alpha (agent_id=3)\n\nProposal: #4"},
                {"number": 6, "title": "u", "user": {"login": "alice"},
                 "merged_at": None, "closed_at": "2026-08-11T02:00:00Z",
                 "labels": [], "body": "human-made, no trailer"},
            ]
        )
        closed = github.recently_closed_prs(per_page=2)
        assert calls == [("GET", "pulls?state=closed&sort=updated&direction=desc&per_page=2")], \
            "recently_closed_prs hits the closed-pulls endpoint with the page size"
        assert closed[0]["number"] == 5 and closed[0]["merged_at"] == "2026-08-11T00:00:00Z", closed[0]
        assert closed[0]["labels"] == ["declined"], closed[0]
        assert closed[0]["citizen"] == {"name": "curious-alpha", "agent_id": 3}, closed[0]
        assert closed[0]["proposal_post_id"] == 4, closed[0]
        assert closed[1]["citizen"] is None and closed[1]["proposal_post_id"] is None, \
            "a PR without a Citizen trailer maps to no citizen / proposal"
    finally:
        github._request = real_request
    print("  github.recently_closed_prs: ok")

    # --- repo_spec / base_branch: the wired identity ------------------------
    # The tools' target repo is config/process-env driven; these are the pure
    # reads every repo tool reports through (and the viewer's api_overview).
    assert github.repo_spec(), "the tools must be wired to a repo slug"
    assert "/" in github.repo_spec(), "the repo slug must be owner/name"
    assert github.base_branch() == github.GITHUB_BASE_BRANCH, \
        "base_branch must match github's configured GITHUB_BASE_BRANCH"
    print("  github repo_spec/base_branch: ok")

    # --- open-PR helper: one batched opener map (server's prs_open count) --
    # linked_pr_openers returns {pr_number: opener} for every linked PR from a
    # single query - the server's _open_pr_count_for reads it instead of a
    # per-PR connection.  Set up a link for PR 101 to epsilon.
    _opener_prop = db.create_proposal(
        agents["alpha"]["token"], "opener helper setup", "body",
    )
    db.link_pr_to_proposal(101, _opener_prop["post_id"], agents["epsilon"]["agent_id"])
    links = db.linked_pr_openers()
    assert links.get(101) == {
        "name": agents["epsilon"]["name"],
        "agent_id": agents["epsilon"]["agent_id"],
    }, "linked_pr_openers maps an existing link to its recorded opener"
    map_prop = db.create_proposal(agents["gamma"]["token"], "opener map", "body")
    db.link_pr_to_proposal(777, map_prop["post_id"], agents["zeta"]["agent_id"])
    db.link_pr_to_proposal(778, map_prop["post_id"], agents["theta"]["agent_id"])
    links = db.linked_pr_openers()
    assert links[777] == {
        "name": agents["zeta"]["name"], "agent_id": agents["zeta"]["agent_id"]
    }, "a fresh link appears in the map with its recorded opener"
    assert links[778] == {
        "name": agents["theta"]["name"], "agent_id": agents["theta"]["agent_id"]
    }, "the map holds every linked PR in one lookup"
    print("  linked_pr_openers: ok")

    # --- regression test for PR #169: defensive JSON parsing in repo helpers ---
    # FastMCP sometimes passes list[dict] parameters as raw JSON strings.
    # The server's _changes_for_repo_propose and _changes_for_repo_update must
    # parse these correctly and raise clear ForumError for invalid JSON.
    from server import repo_helpers as rh

    # Valid JSON string -> list[dict] (passed as files parameter, not file_path)
    valid_json = '[{"path": "a.md", "content": "hello"}]'
    parsed = rh._changes_for_repo_propose(None, None, valid_json)
    assert parsed == [{"path": "a.md", "content": "hello"}], \
        "valid JSON string must be parsed to list[dict]"

    # Valid JSON string for update
    parsed_update = rh._changes_for_repo_update(valid_json)
    assert parsed_update == [{"path": "a.md", "content": "hello"}], \
        "valid JSON string must be parsed in update path too"

    # Invalid JSON -> clear ForumError
    invalid_json = 'not valid json {'
    try:
        rh._changes_for_repo_propose(None, None, invalid_json)
        raise AssertionError("invalid JSON must raise ForumError")
    except db.ForumError as e:
        assert "invalid JSON" in str(e), f"error message must mention invalid JSON: {e}"

    try:
        rh._changes_for_repo_update(invalid_json)
        raise AssertionError("invalid JSON must raise ForumError in update path")
    except db.ForumError as e:
        assert "invalid JSON" in str(e), f"error message must mention invalid JSON: {e}"

    # None and list inputs still work (backwards compatibility)
    # For propose: None files is only valid with file_path + content provided
    assert rh._changes_for_repo_propose("a.md", "hello", None) == [{"path": "a.md", "content": "hello"}], \
        "file_path + content with None files works"
    assert rh._changes_for_repo_propose(None, None, [{"path": "b.md", "content": "x"}]) == \
        [{"path": "b.md", "content": "x"}], "list input passes through"
    assert rh._changes_for_repo_update(None) == [], "None passes through in update"
    assert rh._changes_for_repo_update([{"path": "c.md", "content": "y"}]) == \
        [{"path": "c.md", "content": "y"}], "list input passes through in update"

    # Empty list is rejected (existing behavior)
    try:
        rh._changes_for_repo_propose([], None, None)
        raise AssertionError("empty list must be rejected")
    except db.ForumError:
        pass
    try:
        rh._changes_for_repo_update([])
        raise AssertionError("empty list must be rejected in update")
    except db.ForumError:
        pass

    print("  defensive JSON parsing: ok")

    print("test_repo: all assertions passed")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
