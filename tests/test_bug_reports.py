"""
tests/test_bug_reports.py — bug report system tests.

file_bug_report, confidence tracking, #B references, viewer helpers.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bug_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, init, setup  # noqa: E402

import db._bug_reports as bug_mod  # noqa: E402


def test_file_and_get(helpers):
    reporter = helpers["alpha"]
    r = bug_mod.file_bug_report(reporter["token"], "Login 500", "Server crashes on login", "https://example.com/login")
    assert r["id"] >= 1
    assert r["title"] == "Login 500"
    assert r["body"] == "Server crashes on login"
    assert r["url"] == "https://example.com/login"
    assert r["confidence"] == 1
    assert r["status"] == "open"
    assert r["duplicate_of"] is None
    assert r["new_confidence"] == 1
    full = bug_mod.get_bug_report(r["id"])
    assert full["id"] == r["id"]
    assert full["reporter_name"] == reporter["name"]
    assert full["duplicates"] == []
    assert full["linked_proposals"] == []
    print("  file_and_get: ok")


def test_duplicate_raises_confidence(helpers):
    alpha = helpers["alpha"]
    beta = helpers["beta"]
    gamma = helpers["gamma"]
    r1 = bug_mod.file_bug_report(alpha["token"], "Login 500", "crash", "https://example.com/login-dup")
    assert r1["confidence"] == 1
    r2 = bug_mod.file_bug_report(beta["token"], "Login broken", "500 error", "https://example.com/login-dup")
    assert r2["duplicate_of"] == r1["id"]
    assert r2["confidence"] == 1
    full = bug_mod.get_bug_report(r1["id"])
    assert full["confidence"] == 2
    assert len(full["duplicates"]) == 1
    r3 = bug_mod.file_bug_report(gamma["token"], "Login fail", "500", "https://example.com/login-dup")
    assert r3["duplicate_of"] == r1["id"]
    assert r3["confidence"] == 1
    full2 = bug_mod.get_bug_report(r1["id"])
    assert full2["confidence"] == 3
    print("  duplicate raises confidence: ok")


def test_threshold_confirms(helpers):
    alpha = helpers["alpha"]
    beta = helpers["beta"]
    gamma = helpers["gamma"]
    r = bug_mod.file_bug_report(alpha["token"], "DB lock", "sqlite locked", "https://example.com/db-lock")
    bug_mod.file_bug_report(beta["token"], "DB lock dup", "locked", "https://example.com/db-lock")
    bug_mod.file_bug_report(gamma["token"], "DB lock dup2", "locked", "https://example.com/db-lock")
    full = bug_mod.get_bug_report(r["id"])
    assert full["confidence"] == 3
    assert full["status"] == "confirmed"
    print("  threshold confirms: ok")


def test_different_urls_no_duplicate(helpers):
    alpha = helpers["alpha"]
    beta = helpers["beta"]
    r1 = bug_mod.file_bug_report(alpha["token"], "Login 500", "crash", "https://example.com/login-diff")
    r2 = bug_mod.file_bug_report(beta["token"], "Signup 500", "crash", "https://example.com/signup-diff")
    assert r1["id"] != r2["id"]
    assert bug_mod.get_bug_report(r1["id"])["confidence"] == 1
    assert bug_mod.get_bug_report(r2["id"])["confidence"] == 1
    print("  different urls no duplicate: ok")


def test_fixed_bug(helpers):
    alpha = helpers["alpha"]
    r = bug_mod.file_bug_report(alpha["token"], "Typo", "fix me", None)
    karma_before = db.whoami(alpha["token"])["karma"]
    assert bug_mod.fix_bug_report(r["id"])["status"] == "fixed"
    assert bug_mod.get_bug_report(r["id"])["status"] == "fixed"
    karma_after = db.whoami(alpha["token"])["karma"]
    assert karma_after == karma_before + 1, \
        f"fixing a bug report credits +1 karma: {karma_before} -> {karma_after}"
    print("  fixed bug: ok")


def test_list_filter(helpers):
    alpha = helpers["alpha"]
    before = bug_mod.list_bug_reports()
    before_count = before["total"]
    bug_mod.file_bug_report(alpha["token"], "Bug A", "body", None)
    bug_mod.file_bug_report(alpha["token"], "Bug B", "body", None)
    all_reports = bug_mod.list_bug_reports()
    assert all_reports["total"] == before_count + 2
    assert all_reports["reports"][0]["title"] == "Bug B"
    assert all_reports["reports"][1]["title"] == "Bug A"
    print("  list filter: ok")


def test_reference_expansion(helpers):
    alpha = helpers["alpha"]
    r = bug_mod.file_bug_report(alpha["token"], "Login bug", "body", None)
    post = db.create_post(alpha["token"], "Reference test", f"See #B{r['id']}")
    refs = post.get("referenced", [])
    assert any(ref.get("kind") == "bug_report" and ref.get("id") == r["id"]
               for ref in refs), f"expected bug_report ref in {refs}"
    print("  reference expansion: ok")


def test_linked_proposals(helpers):
    alpha = helpers["alpha"]
    beta = helpers["beta"]
    r = bug_mod.file_bug_report(alpha["token"], "Login bug", "body", None)
    db.create_post(beta["token"], "Fix login", "Fix the login bug #B" + str(r["id"]))
    prop = db.create_proposal(
        beta["token"], "Fix login proposal",
        "Fix bug #B" + str(r["id"]),
    )
    full = bug_mod.get_bug_report(r["id"])
    assert any(p["id"] == prop["post_id"] for p in full["linked_proposals"])
    print("  linked proposals: ok")


def test_viewer_bugs_page(helpers):
    """Smoke test: bugs_page renders."""
    from viewer._bugs import bugs_page

    alpha = helpers["alpha"]
    bug_mod.file_bug_report(alpha["token"], "Test Bug", "body", None)

    class FakeRequest:
        query_params = {}

    resp = bugs_page(FakeRequest())
    assert resp.status_code == 200
    assert "Test Bug" in resp.body.decode()
    print("  viewer bugs page: ok")


def test_viewer_bug_detail(helpers):
    """Smoke test: bug_detail_page renders."""
    from viewer._bugs import bug_detail_page

    alpha = helpers["alpha"]
    r = bug_mod.file_bug_report(alpha["token"], "Detail Bug", "body text", None)

    class FakeRequest:
        path_params = {"id": r["id"]}

    resp = bug_detail_page(FakeRequest())
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "Detail Bug" in body
    assert "body text" in body
    print("  viewer bug detail: ok")


def test_api_bugs(helpers):
    """Smoke test: api_bugs returns JSON."""
    from starlette.requests import Request
    from viewer._api import api_bugs
    import asyncio

    alpha = helpers["alpha"]
    bug_mod.file_bug_report(alpha["token"], "API Bug", "body", None)

    class FakeScope:
        def __init__(self):
            self["type"] = "http"
            self["query_string"] = b""
            self["method"] = "GET"
            self["server"] = ("127.0.0.1", 8000)

    scope = {"type": "http", "query_string": b"", "method": "GET",
             "server": ("127.0.0.1", 8000), "path": "/api/bugs",
             "headers": []}

    req = Request(scope)
    resp = api_bugs(req)
    data = resp.body
    import json
    result = json.loads(data)
    assert result["total"] >= 1
    assert any(r["title"] == "API Bug" for r in result["reports"])
    print("  api bugs: ok")


def test_small_fix_gates_bug_confidence(helpers):
    """small_fix with #B must be confirmed (Rule 21, #309 follow-up)."""
    import uuid
    reporter = helpers["alpha"]
    beta = helpers["beta"]
    gamma = helpers["gamma"]
    from db._core import ForumError
    url = f"https://example.com/gate-{uuid.uuid4()}"
    r = bug_mod.file_bug_report(reporter["token"], "Gate bug", "body", url)
    assert r["confidence"] == 1
    assert bug_mod.get_bug_report(r["id"])["status"] == "open"
    # 1/3 -> small_fix with #B must be rejected
    try:
        db.create_proposal(reporter["token"], "Fix gate bug", f"Fix #B{r['id']}", small_fix=True)
        assert False, "1/3 bug must block small_fix"
    except ForumError as e:
        assert "not confirmed" in str(e) and f"#{r['id']}" in str(e)
    # normal proposal with same #B is still allowed
    p = db.create_proposal(reporter["token"], "Fix gate bug normal", f"Fix #B{r['id']} via normal", small_fix=False)
    assert p["proposal_kind"] == "proposal"
    # small_fix without #B is still allowed (typo etc)
    p2 = db.create_proposal(reporter["token"], "Fix typo", "fix typo", small_fix=True)
    assert p2["proposal_kind"] == "small_fix"
    # 2/3 still blocked
    bug_mod.file_bug_report(beta["token"], "Gate dup2", "body", url)
    assert bug_mod.get_bug_report(r["id"])["confidence"] == 2
    assert bug_mod.get_bug_report(r["id"])["status"] == "open"
    try:
        db.create_proposal(reporter["token"], "Fix gate bug2", f"Fix #B{r['id']}", small_fix=True)
        assert False, "2/3 bug must still block small_fix"
    except ForumError:
        pass
    # 3/3 -> confirmed, now allowed
    bug_mod.file_bug_report(gamma["token"], "Gate dup3", "body", url)
    assert bug_mod.get_bug_report(r["id"])["status"] == "confirmed"
    assert bug_mod.get_bug_report(r["id"])["confidence"] == 3
    p3 = db.create_proposal(reporter["token"], "Fix gate bug3", f"Fix #B{r['id']}", small_fix=True)
    assert p3["proposal_kind"] == "small_fix"
    print("  small_fix gates bug confidence: ok")


if __name__ == "__main__":
    init()
    helpers, _post_id = setup()
    test_file_and_get(helpers)
    test_duplicate_raises_confidence(helpers)
    test_threshold_confirms(helpers)
    test_different_urls_no_duplicate(helpers)
    test_fixed_bug(helpers)
    test_list_filter(helpers)
    test_reference_expansion(helpers)
    test_linked_proposals(helpers)
    test_viewer_bugs_page(helpers)
    test_viewer_bug_detail(helpers)
    test_api_bugs(helpers)
    test_small_fix_gates_bug_confidence(helpers)
    print("All bug report tests passed.")
