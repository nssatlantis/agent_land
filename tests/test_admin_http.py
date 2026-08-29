"""Admin HTTP-layer test: drives admin.py's route handlers in-process against
a temp database.

Run: python tests/test_admin_http.py   (stdlib + the already-installed
starlette; no server needed)

tests/ covers the db-level admin functions (ban_agent, delete_agent,
resolve_report, ...) but nothing has ever touched the admin HTTP surface: the
basic-auth gate, the CSRF token machinery and the form-handling routes that
wrap those db calls. This file closes that gap by calling the handlers directly
with in-process starlette Request objects, exactly as the viewer's own pages
would - headers, cookies, a urlencoded form body, and path params.

Covers:
- the basic-auth gate: denied without/wrong credentials (401 + WWW-Authenticate),
  admitted with the right ones, and the open-admin mode (no ADMIN_PASSWORD)
- the CSRF machinery: fresh-token generation stashed on request.state, cookie
  reuse, the form field carrying the token, and compare_digest validation
  (matching / mismatched / missing)
- the form routes, each through its full POST shape:
  ban / unban (mutate helper), delete_agent (typed-name confirmation + the
  destroy_content guard), delete_post (confirm checkbox), resolve_report
  (clear / suspend) - asserting the 303 redirect, the db effect, and that a
  bad CSRF or wrong confirmation never mutates anything
- the pages: /admin, per-agent detail, per-report detail (200 for real rows,
  a graceful flash for missing ones)
- the audit trail: every successful action leaves an admin_actions row signed
  with the authenticated admin username
"""

import asyncio
import atexit
import base64
import importlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from starlette.requests import Request

_TMP = Path(tempfile.mkdtemp(prefix="agentland_admin_test_"))
# Tear the temp db down even when an assertion fails - the explicit rmtree in
# main() only runs on the success path.
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_REPORT_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_IDEA_COOLDOWN_SECONDS"] = "0"
# admin.py reads these at import time - set them before the import.
os.environ["ADMIN_USER"] = "root"
os.environ["ADMIN_PASSWORD"] = "secret"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402 - env must be set before the import
import moderation  # noqa: E402
import reports  # noqa: E402
from server import admin  # noqa: E402 - reads ADMIN_USER/ADMIN_PASSWORD at import

_CSRF = admin._CSRF_COOKIE
_AUTH = "Basic " + base64.b64encode(b"root:secret").decode()


def _req(
    method, path, *, params=None, query=None, body=None, cookies=None, headers=None
):
    """A minimal in-process starlette Request. `body` is a dict that gets
    urlencoded as the form payload, with the matching Content-Type header."""
    header_bytes = list(headers or [])
    if cookies:
        cookie_hdr = "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()
        header_bytes.append((b"cookie", cookie_hdr))
    if body is not None:
        body_bytes = _urlencode(body)
        header_bytes.append((b"content-type", b"application/x-www-form-urlencoded"))
    else:
        body_bytes = b""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "root_path": "",
        "query_string": _urlencode(query) if query else b"",
        "headers": header_bytes,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "path_params": params or {},
        "state": {},
    }
    return Request(scope, _receive(body_bytes))


def _urlencode(form):
    from urllib.parse import urlencode

    return urlencode(form).encode()


def _receive(body):
    """A receive channel that yields the whole form body once, then signals
    the end of the stream - the shape starlette's form parser expects."""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _call(handler, *args, **kw):
    return asyncio.run(handler(*args, **kw))


async def _load_form(request):
    return await request.form()


def _audit_rows():
    """Every admin_actions row, newest first - the trail each action leaves."""
    conn = sqlite3.connect(os.environ["FORUM_DB_PATH"])
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT admin_user, action, target_type, target_id, detail "
                "FROM admin_actions ORDER BY id"
            )
        ]
    finally:
        conn.close()


def _flash_ok(resp):
    """A flash page (the no-mutation failure shape) is a 200 admin page."""
    assert resp.status_code == 200, f"expected flash (200), got {resp.status_code}"
    return resp.body


def main():
    db.init_db()

    alice = db.register_agent("alice")
    bob = db.register_agent("bob")
    a_token, b_token = alice["token"], bob["token"]

    # bob needs >=1 karma to file a report (MIN_KARMA_MOD).
    bob_post = db.create_post(b_token, "bob's stone", "hello")
    db.vote(a_token, "post", bob_post["post_id"], 1)
    alice_post = db.create_post(a_token, "alice's stone", "world")
    report = reports.report_content(
        b_token, "post", alice_post["post_id"], "flagged for review"
    )
    report_id = report["report_id"]

    # --- basic-auth gate ---------------------------------------------------
    no_auth = _req("GET", "/admin")
    assert admin._authorized(no_auth) is False, "no credentials: denied"
    denied = _call(admin.admin_page, no_auth)
    assert denied.status_code == 401, "no credentials: 401"
    assert "Basic" in denied.headers.get("WWW-Authenticate", ""), (
        "401 carries WWW-Authenticate"
    )

    wrong_auth = _req(
        "GET",
        "/admin",
        headers=[(b"authorization", b"Basic " + base64.b64encode(b"root:nope"))],
    )
    assert admin._authorized(wrong_auth) is False, "wrong password: denied"

    # A crafted non-ASCII Basic payload decodes to valid UTF-8 but must be
    # denied as a 401, not surface a compare_digest TypeError as a 500.
    nonascii_auth = _req(
        "GET",
        "/admin",
        headers=[
            (
                b"authorization",
                b"Basic " + base64.b64encode("r\u00f6\u00f6t:nope".encode("utf-8")),
            )
        ],
    )
    assert admin._authorized(nonascii_auth) is False, (
        "non-ASCII credentials: denied, not a 500"
    )
    nonascii_resp = _call(admin.admin_page, nonascii_auth)
    assert nonascii_resp.status_code == 401, "non-ASCII credentials: 401, not a 500"

    ok_auth = _req("GET", "/admin", headers=[(b"authorization", _AUTH.encode())])
    assert admin._authorized(ok_auth) is True, "right credentials: admitted"
    assert admin._admin_user(ok_auth) == "root", "audit username comes from the header"

    page = _call(admin.admin_page, ok_auth)
    assert page.status_code == 200, "authenticated admin page renders"
    assert b"Reports" in page.body and b"Citizens" in page.body, (
        "the docket page carries reports, proposals and citizens panels"
    )

    # --- CSRF machinery ----------------------------------------------------
    fresh = _req("GET", "/admin")
    token = admin._csrf_token(fresh)
    assert token and len(token) > 8, "fresh token generated when no cookie present"
    assert admin._csrf_token(fresh) == token, (
        "token stashed on request.state, stable per render"
    )
    assert (
        f'name="csrf" value="{token}"'.encode() in admin._csrf_field(fresh).encode()
    ), "the form field carries the same token"

    reused = _req("GET", "/admin", cookies={_CSRF: "known-token"})
    assert admin._csrf_token(reused) == "known-token", (
        "existing cookie is reused, not regenerated"
    )

    ok_form = _req(
        "POST", "/admin", cookies={_CSRF: "tok"}, body={"csrf": "tok", "confirm": "x"}
    )
    assert admin._csrf_ok(ok_form, asyncio.run(_load_form(ok_form))) is True, (
        "matching cookie + form token passes compare_digest"
    )
    bad_form = _req(
        "POST", "/admin", cookies={_CSRF: "tok"}, body={"csrf": "WRONG", "confirm": "x"}
    )
    assert admin._csrf_ok(bad_form, asyncio.run(_load_form(bad_form))) is False, (
        "mismatched token is rejected"
    )
    no_csrf = _req("POST", "/admin", body={"confirm": "x"})
    assert admin._csrf_ok(no_csrf, asyncio.run(_load_form(no_csrf))) is False, (
        "missing token is rejected"
    )

    # The response cookie round-trip: the rendered page sets the CSRF cookie
    # and the POST carries it back.
    rendered = _call(admin.admin_page, ok_auth)
    set_cookie = rendered.headers.get("set-cookie") or ""
    assert f"{_CSRF}=" in set_cookie, "the admin page sets the admin_csrf cookie"
    cookie_token = set_cookie.split(f"{_CSRF}=", 1)[1].split(";", 1)[0]

    # --- ban / unban through the mutate route ------------------------------
    resp = _call(
        admin.ban_agent,
        _req(
            "POST",
            f"/admin/agents/{bob['agent_id']}/ban",
            params={"id": bob["agent_id"]},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert resp.status_code == 303 and resp.headers.get("location") == "/admin", (
        "ban redirects back to the docket"
    )
    assert [a for a in moderation.admin_list_agents() if a["id"] == bob["agent_id"]][0][
        "banned"
    ], "ban actually bans the citizen"

    resp = _call(
        admin.ban_agent,
        _req(
            "POST",
            f"/admin/agents/{bob['agent_id']}/ban",
            params={"id": bob["agent_id"]},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert b"already banned" in _flash_ok(resp), "re-banning is a flash, not a crash"

    resp = _call(
        admin.unban_agent,
        _req(
            "POST",
            f"/admin/agents/{bob['agent_id']}/unban",
            params={"id": bob["agent_id"]},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert resp.status_code == 303, "unban redirects"
    assert not [
        a for a in moderation.admin_list_agents() if a["id"] == bob["agent_id"]
    ][0]["banned"], "unban restores the citizen"

    # CSRF and auth still guard the mutation routes even when the db call
    # would succeed.
    resp = _call(
        admin.ban_agent,
        _req(
            "POST",
            f"/admin/agents/{bob['agent_id']}/ban",
            params={"id": bob["agent_id"]},
            body={"csrf": "WRONG"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    _flash_ok(resp)
    assert not [
        a for a in moderation.admin_list_agents() if a["id"] == bob["agent_id"]
    ][0]["banned"], "a bad CSRF must never mutate anything"

    resp = _call(
        admin.ban_agent,
        _req(
            "POST",
            f"/admin/agents/{bob['agent_id']}/ban",
            params={"id": bob["agent_id"]},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token},
        ),
    )
    assert resp.status_code == 401, "the auth gate guards the mutation routes too"

    # --- delete_agent: typed-name confirmation + destroy_content -----------
    b2 = db.register_agent("carol")
    c_post = db.create_post(b2["token"], "carol's post", "to be destroyed")
    c_id = b2["agent_id"]

    resp = _call(
        admin.delete_agent,
        _req(
            "POST",
            f"/admin/agents/{c_id}/delete",
            params={"id": c_id},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token, "confirm": "WRONG"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert b"confirmation mismatch" in _flash_ok(resp), "typo'd confirmation is refused"
    assert moderation.agent_name(c_id) == "carol", "nothing deleted on a mismatch"

    resp = _call(
        admin.delete_agent,
        _req(
            "POST",
            f"/admin/agents/{c_id}/delete",
            params={"id": c_id},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token, "confirm": "carol"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert (
        b"posts" in _flash_ok(resp).lower() or b"comments" in _flash_ok(resp).lower()
    ), "deleting a citizen with content but no destroy_content is refused"
    assert moderation.agent_name(c_id) == "carol", "the destroy-content guard held"

    resp = _call(
        admin.delete_agent,
        _req(
            "POST",
            f"/admin/agents/{c_id}/delete",
            params={"id": c_id},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token, "confirm": "carol", "destroy_content": "on"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert resp.status_code == 303, "correctly-confirmed delete redirects"
    assert moderation.agent_name(c_id) is None, "the citizen is gone"
    from db import ForumError

    try:
        db.get_post(c_post["post_id"])
        raise AssertionError("destroyed content must be gone")
    except ForumError:
        pass

    resp = _call(
        admin.delete_agent,
        _req(
            "POST",
            "/admin/agents/999999/delete",
            params={"id": 999999},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token, "confirm": "x"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert b"no such agent" in _flash_ok(resp), "missing agent is a graceful flash"

    # --- delete_post: confirm checkbox + referer ---------------------------
    keep = db.create_post(b_token, "keep me", "body")
    resp = _call(
        admin.delete_post,
        _req(
            "POST",
            f"/admin/posts/{keep['post_id']}/delete",
            params={"id": keep["post_id"]},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert b"must be ticked" in _flash_ok(resp), "an unticked confirm box is refused"
    assert db.get_post(keep["post_id"])["title"] == "keep me", "the post survived"

    resp = _call(
        admin.delete_post,
        _req(
            "POST",
            f"/admin/posts/{keep['post_id']}/delete",
            params={"id": keep["post_id"]},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token, "confirm": "on"},
            headers=[
                (b"authorization", _AUTH.encode()),
                (b"referer", b"/admin/agents/2"),
            ],
        ),
    )
    assert resp.status_code == 303, "ticked delete redirects"
    assert resp.headers.get("location") == "/admin/agents/2", (
        "delete_post redirects to the referer (the page it was clicked from)"
    )
    try:
        db.get_post(keep["post_id"])
        raise AssertionError("deleted post must be gone")
    except ForumError:
        pass

    # --- delete_post: _safe_referer trusts only same-origin or bare paths --
    def _delete_with_referer(ref):
        headers = [(b"authorization", _AUTH.encode())]
        if ref is not None:
            headers.append((b"referer", ref.encode()))
        p = db.create_post(b_token, "ref post", "body")["post_id"]
        resp = _call(
            admin.delete_post,
            _req(
                "POST",
                f"/admin/posts/{p}/delete",
                params={"id": p},
                cookies={_CSRF: cookie_token},
                body={"csrf": cookie_token, "confirm": "on"},
                headers=headers,
            ),
        )
        assert resp.status_code == 303, "ticked delete redirects"
        return resp.headers.get("location")

    # Same-origin absolute URL (scheme + host match the request) is honoured.
    assert _delete_with_referer("http://127.0.0.1:8000/admin/posts/9") == (
        "http://127.0.0.1:8000/admin/posts/9"
    ), "same-origin absolute referer is kept"
    # Off-site referer is never used as an open-redirect target.
    assert _delete_with_referer("https://evil.example.com/phish") == "/admin", (
        "off-site referer falls back"
    )
    # A missing referer falls back too.
    assert _delete_with_referer(None) == "/admin", "missing referer falls back"

    # --- resolve_report: clear / suspend -----------------------------------
    resp = _call(
        admin.resolve_report,
        _req(
            "POST",
            f"/admin/reports/{report_id}/resolve",
            params={"id": report_id},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token, "action": "clear"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert resp.status_code == 303, "clear redirects"
    assert [r for r in reports.list_reports() if r["id"] == report_id][0][
        "status"
    ] == "cleared", "clear closes the report"

    rep2 = reports.report_content(b_token, "post", alice_post["post_id"], "second flag")
    resp = _call(
        admin.resolve_report,
        _req(
            "POST",
            f"/admin/reports/{rep2['report_id']}/resolve",
            params={"id": rep2["report_id"]},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token, "action": "suspend"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert resp.status_code == 303, "suspend redirects"
    assert db.whoami(a_token)["suspended_until"] is not None, (
        "suspend suspends the author"
    )
    assert [r for r in reports.list_reports() if r["id"] == rep2["report_id"]][0][
        "status"
    ] == "suspended", "the report records the suspension"

    resp = _call(
        admin.resolve_report,
        _req(
            "POST",
            f"/admin/reports/{report_id}/resolve",
            params={"id": report_id},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token, "action": "nonsense"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert resp.status_code == 200, "an unknown action is a graceful flash"
    assert [r for r in reports.list_reports() if r["id"] == report_id][0][
        "status"
    ] == "cleared", "an invalid action never changes the report"

    # --- pages: agent detail + report detail -------------------------------
    detail = _call(
        admin.agent_detail,
        _req(
            "GET",
            f"/admin/agents/{alice['agent_id']}",
            params={"id": alice["agent_id"]},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert detail.status_code == 200 and b"Citizen detail" in detail.body, (
        "agent detail renders"
    )
    missing = _call(
        admin.agent_detail,
        _req(
            "GET",
            "/admin/agents/999999",
            params={"id": 999999},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    _flash_ok(missing), "missing agent detail is a graceful flash"

    rep_page = _call(
        admin.report_detail,
        _req(
            "GET",
            f"/admin/reports/{report_id}",
            params={"id": report_id},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert rep_page.status_code == 200 and b"Report" in rep_page.body, (
        "report detail renders"
    )
    assert b"Reporter" in rep_page.body and b"Reported author" in rep_page.body, (
        "the detail page shows the reporter and reported-author panels"
    )
    assert b"Reported content" in rep_page.body, (
        "the detail page renders the frozen content snapshot"
    )
    assert b"resolved by" in rep_page.body, (
        "the detail page credits the resolver (or 'community vote')"
    )
    assert b"Sibling reports" in rep_page.body, "the detail page lists sibling reports"
    rep_missing = _call(
        admin.report_detail,
        _req(
            "GET",
            "/admin/reports/999999",
            params={"id": 999999},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    _flash_ok(rep_missing), "missing report detail is a graceful flash"

    # --- /admin/reports index: the active/resolved split --------------------
    idx = _call(
        admin.reports_index,
        _req("GET", "/admin/reports", headers=[(b"authorization", _AUTH.encode())]),
    )
    assert idx.status_code == 200, "the reports index renders"
    assert b"Active reports" in idx.body and b"Resolved reports" in idx.body, (
        "the index splits active and resolved reports into two sections"
    )
    assert b"cleared" in idx.body, "a resolved report shows in the resolved section"
    idx_open = _call(
        admin.reports_index,
        _req(
            "GET",
            "/admin/reports",
            query={"status": "open"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert (
        idx_open.status_code == 200
        and b"Active reports" in idx_open.body
        and b"Resolved reports" not in idx_open.body
    ), "?status=open shows only the active section"
    idx_target = _call(
        admin.reports_index,
        _req(
            "GET",
            "/admin/reports",
            query={"target": "comment"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert idx_target.status_code == 200, "the ?target filter renders"

    # A report on deleted content renders as a resolved 'removed' record, and
    # the snapshot survives (the reports revamp). alice is suspended by the
    # resolve test above, so a fresh author and voter carry this case.
    dave = db.register_agent("dave")
    doomed = db.create_post(dave["token"], "doomed post", "this will be deleted")
    removed_rep = reports.report_content(b_token, "post", doomed["post_id"], "sweep me")
    erin = db.register_agent("erin")
    erin_post = db.create_post(erin["token"], "erin's stone", "hi")
    db.vote(b_token, "post", erin_post["post_id"], 1)
    reports.vote_on_report(erin["token"], removed_rep["report_id"], "suspend")
    moderation.delete_post(doomed["post_id"], "root")
    removed_row = [
        r for r in reports.list_reports() if r["id"] == removed_rep["report_id"]
    ][0]
    assert removed_row["status"] == "removed", (
        "deleted content sweeps its report to 'removed'"
    )
    assert "this will be deleted" in (removed_row["target_preview"] or ""), (
        "the snapshot preview survives content deletion"
    )
    removed_page = _call(
        admin.report_detail,
        _req(
            "GET",
            f"/admin/reports/{removed_rep['report_id']}",
            params={"id": removed_rep["report_id"]},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert removed_page.status_code == 200 and b"removed" in removed_page.body, (
        "a 'removed' report renders as resolved with its status badge"
    )
    assert b"this will be deleted" in removed_page.body, (
        "the detail page shows the frozen snapshot of the deleted content"
    )
    assert b"erin" in removed_page.body, (
        "the archived voter identity shows on the detail page"
    )
    assert b"dave" in removed_page.body, (
        "the flagged author panel still names the citizen"
    )
    idx_resolved = _call(
        admin.reports_index,
        _req(
            "GET",
            "/admin/reports",
            query={"status": "resolved"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert idx_resolved.status_code == 200 and b"removed" in idx_resolved.body, (
        "a 'removed' report appears in the resolved split"
    )

    # A report on a quoted comment freezes the quote fields in its snapshot,
    # so the admin detail page can render what the flagged comment quoted.
    frank = db.register_agent("frank")
    f_post = db.create_post(frank["token"], "frank's post", "thread")
    quote_src = db.create_comment(frank["token"], f_post["post_id"], "original words")
    quote_comment = db.create_comment(
        b_token, f_post["post_id"], "quoting", quote_comment_id=quote_src["comment_id"]
    )
    quoted_rep = reports.report_content(
        b_token, "comment", quote_comment["comment_id"], "flagged"
    )
    qr = reports.get_report(quoted_rep["report_id"])
    _q_src_body = f"original words\n\n— frank (agent_id={frank['agent_id']})"
    assert (
        qr["target_snapshot"].get("quote_comment_id") == quote_src["comment_id"]
        and qr["target_snapshot"].get("quote_text") == _q_src_body
    ), "a comment report's snapshot carries the quote fields (signed source body)"
    quoted_page = _call(
        admin.report_detail,
        _req(
            "GET",
            f"/admin/reports/{quoted_rep['report_id']}",
            params={"id": quoted_rep["report_id"]},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert quoted_page.status_code == 200 and b"original words" in quoted_page.body, (
        "the report detail renders the flagged comment's quote"
    )

    # --- bug reports admin --------------------------------------------------
    import db._bug_reports as bug_mod

    bug_user = db.register_agent("bugreporter")
    bug_token = bug_user["token"]
    bug_r = bug_mod.file_bug_report(bug_token, "Admin test bug", "broken thing", None)
    bug_id = bug_r["id"]

    # /admin/bugs index page
    bugs_page = _call(
        admin.bugs_index,
        _req("GET", "/admin/bugs", headers=[(b"authorization", _AUTH.encode())]),
    )
    assert bugs_page.status_code == 200 and b"Admin test bug" in bugs_page.body, (
        "the bugs index renders with the new report"
    )
    assert b"bugs" in bugs_page.body.lower(), "the nav shows the bugs link"

    # /admin/bugs/{id} detail page
    bug_det = _call(
        admin.bug_detail,
        _req(
            "GET",
            f"/admin/bugs/{bug_id}",
            params={"id": bug_id},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert bug_det.status_code == 200 and b"Admin test bug" in bug_det.body, (
        "the bug detail page renders"
    )
    assert b"Confirm bug" in bug_det.body, "the confirm button shows for open bugs"
    assert b"Mark fixed" in bug_det.body, "the fix button shows for non-fixed bugs"

    # CSRF guard: POST without CSRF is refused
    bad_csrf = _call(
        admin.admin_confirm_bug,
        _req(
            "POST",
            f"/admin/bugs/{bug_id}/confirm",
            params={"id": bug_id},
            cookies={_CSRF: "tok"},
            body={"csrf": "WRONG"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert b"CSRF" in bad_csrf.body, "bad CSRF is refused on confirm"

    # confirm via POST
    confirm_resp = _call(
        admin.admin_confirm_bug,
        _req(
            "POST",
            f"/admin/bugs/{bug_id}/confirm",
            params={"id": bug_id},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert confirm_resp.status_code == 303, "confirm redirects after success"
    assert bug_mod.get_bug_report(bug_id)["status"] == "confirmed"

    # after confirm, the confirm button is gone
    bug_det2 = _call(
        admin.bug_detail,
        _req(
            "GET",
            f"/admin/bugs/{bug_id}",
            params={"id": bug_id},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert b"Confirm bug" not in bug_det2.body, (
        "confirm button disappears after confirming"
    )
    assert b"Mark fixed" in bug_det2.body, "fix button still shows for confirmed bugs"

    # fix via POST
    fix_resp = _call(
        admin.admin_fix_bug,
        _req(
            "POST",
            f"/admin/bugs/{bug_id}/fix",
            params={"id": bug_id},
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert fix_resp.status_code == 303, "fix redirects after success"
    assert bug_mod.get_bug_report(bug_id)["status"] == "fixed"

    # after fix, neither button shows
    bug_det3 = _call(
        admin.bug_detail,
        _req(
            "GET",
            f"/admin/bugs/{bug_id}",
            params={"id": bug_id},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert b"Confirm bug" not in bug_det3.body, "confirm button gone after fix"
    assert b"Mark fixed" not in bug_det3.body, "fix button gone after fix"

    # missing bug shows a flash error
    missing = _call(
        admin.bug_detail,
        _req(
            "GET",
            "/admin/bugs/99999",
            params={"id": 99999},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert missing.status_code == 200 and b"not found" in missing.body.lower(), (
        "missing bug shows a flash error"
    )

    # audit trail for confirm and fix
    bug_audit = [
        r
        for r in _audit_rows()
        if r["target_type"] == "bug_report" and r["target_id"] == bug_id
    ]
    assert any(
        r["action"] == "confirm_bug_report" and r["admin_user"] == "root"
        for r in bug_audit
    ), "confirm left a signed audit row"
    assert any(
        r["action"] == "fix_bug_report" and r["admin_user"] == "root" for r in bug_audit
    ), "fix left a signed audit row"

    # --- /admin/workflows close-stale (review D7/W9) -------------------------
    # A decided-but-retryable proposal keeps an open create-pr run until the
    # reconcile sweep closes it; the admin page offers a one-click sweep that
    # appears only while such runs exist.
    wf_agent = db.register_agent("wfstale")
    stale_post = db.create_proposal(wf_agent["token"], "admin stale run", "decided")
    stale_pid = stale_post["post_id"]
    with db._conn() as conn:
        db.record_proposal_outcome(
            88123, stale_pid, "declined", db._now_iso(), conn=conn
        )
    stale_run_id = None
    with db._conn() as conn:
        row = conn.execute(
            "SELECT id FROM workflow_runs WHERE proposal_id = ? AND status = 'open'",
            (stale_pid,),
        ).fetchone()
        assert row is not None, "the decided proposal still holds an open run"
        stale_run_id = row["id"]

    # the button is rendered while there is something to close
    wf_page = _call(
        admin.workflows_admin_page,
        _req("GET", "/admin/workflows", headers=[(b"authorization", _AUTH.encode())]),
    )
    assert wf_page.status_code == 200 and b"close stale" in wf_page.body.lower(), (
        "the close-stale button renders while a stale run exists"
    )

    # the POST is auth-gated and CSRF-guarded
    no_auth = _call(
        admin.workflow_close_stale,
        _req(
            "POST",
            "/admin/workflows/close-stale",
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token},
        ),
    )
    assert no_auth.status_code == 401, "close-stale refuses unauthenticated POSTs"
    bad_csrf = _call(
        admin.workflow_close_stale,
        _req(
            "POST",
            "/admin/workflows/close-stale",
            cookies={_CSRF: "tok"},
            body={"csrf": "WRONG"},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert b"CSRF" in bad_csrf.body, "close-stale refuses a bad CSRF token"

    # the real POST closes the stale run and reports the count
    close_resp = _call(
        admin.workflow_close_stale,
        _req(
            "POST",
            "/admin/workflows/close-stale",
            cookies={_CSRF: cookie_token},
            body={"csrf": cookie_token},
            headers=[(b"authorization", _AUTH.encode())],
        ),
    )
    assert close_resp.status_code == 200, "close-stale renders the count"
    assert (
        b"closed 1 stale workflow run" in close_resp.body.lower()
        or b"closed 1 stale workflow runs" in close_resp.body.lower()
    ), "the close-stale flash names how many runs were closed"
    with db._conn() as conn:
        row = conn.execute(
            "SELECT status, decided_at FROM workflow_runs WHERE id = ?",
            (stale_run_id,),
        ).fetchone()
        assert row["status"] == "declined" and row["decided_at"], (
            "the stale run closes to the proposal's exact decided status"
        )

    # once clean, the button disappears
    wf_page2 = _call(
        admin.workflows_admin_page,
        _req("GET", "/admin/workflows", headers=[(b"authorization", _AUTH.encode())]),
    )
    assert b"close stale" not in wf_page2.body.lower(), (
        "the close-stale button hides once nothing stale remains"
    )

    # --- audit trail -------------------------------------------------------
    rows = _audit_rows()
    by_action = {}
    for r in rows:
        by_action.setdefault(r["action"], []).append(r)
    assert any(
        r["admin_user"] == "root"
        and r["action"] == "ban"
        and r["target_id"] == bob["agent_id"]
        for r in rows
    ), "the ban left a signed audit row"
    assert any(
        r["admin_user"] == "root"
        and r["action"] == "unban"
        and r["target_id"] == bob["agent_id"]
        for r in rows
    ), "the unban left a signed audit row"
    assert any(
        r["admin_user"] == "root" and r["action"] == "delete" and r["target_id"] == c_id
        for r in rows
    ), "the citizen delete left a signed audit row"
    assert any(
        r["admin_user"] == "root"
        and r["action"] == "delete_post"
        and r["target_id"] == keep["post_id"]
        for r in rows
    ), "the post delete left a signed audit row"
    assert any(
        r["admin_user"] == "root"
        and r["action"] == "resolve_report"
        and r["target_id"] == report_id
        for r in rows
    ), "the report resolution left a signed audit row"

    # --- open-admin mode (no ADMIN_PASSWORD) --------------------------------
    os.environ["ADMIN_PASSWORD"] = ""
    importlib.reload(admin)
    open_req = _req("GET", "/admin")
    assert admin._authorized(open_req) is True, (
        "no password: open admin admits everyone"
    )
    assert admin._admin_user(open_req) == "admin", (
        "open admin falls back to the 'admin' audit username"
    )
    open_page = _call(admin.admin_page, open_req)
    assert open_page.status_code == 200, "open admin page renders without credentials"

    print("test_admin: all assertions passed")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
