"""Test the designs admin panel (proposal #652, PR2f): docket, detail
queues and every POST action through the sole-admin engine. Auth is
Basic alpha:secret with ADMIN_USER=alpha (panel acts as the Basic
username, which must be the admin citizen)."""

import asyncio
import base64
import os
import sys
import tempfile
from pathlib import Path

from starlette.requests import Request

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_designs_admin_panel_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "1"
os.environ["ADMIN_USER"] = "alpha"
os.environ["ADMIN_PASSWORD"] = "secret"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
from server import admin  # noqa: E402

import db._designs as designs  # noqa: E402
import db._designs_issues as issues  # noqa: E402
import db._designs_discuss as discuss  # noqa: E402

_CSRF = admin._CSRF_COOKIE
_AUTH = "Basic " + base64.b64encode(b"alpha:secret").decode()
_BAD_AUTH = "Basic " + base64.b64encode(b"alpha:wrong").decode()


def _urlencode(form):
    from urllib.parse import urlencode

    return urlencode(form).encode()


def _receive(body):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _req(method, path, *, params=None, body=None, cookies=None, headers=None):
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
        "query_string": b"",
        "headers": header_bytes,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "path_params": params or {},
        "state": {},
    }
    return Request(scope, _receive(body_bytes))


def _call(handler, *args, **kw):
    return asyncio.run(handler(*args, **kw))


def _ok_auth():
    return [(b"authorization", _AUTH.encode())]


def _csrf_token():
    rendered = _call(
        admin.designs_admin_page, _req("GET", "/admin/designs", headers=_ok_auth())
    )
    assert rendered.status_code == 200, rendered.status_code
    set_cookie = rendered.headers.get("set-cookie") or ""
    return set_cookie.split(f"{_CSRF}=", 1)[1].split(";", 1)[0]


def _post(handler, path, params, body, csrf):
    return _call(
        handler,
        _req(
            "POST",
            path,
            params=params,
            cookies={_CSRF: csrf},
            body={"csrf": csrf, **body},
            headers=_ok_auth(),
        ),
    )


def _age_design(did):
    with db._conn() as conn:
        conn.execute(
            "UPDATE designs SET created_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (did,),
        )


def main():
    agents, _post_id = setup()
    alpha = agents["alpha"]
    beta = agents["beta"]
    d = designs.create_design(alpha["token"], "Panel design", "Desc")
    did = d["id"]
    f1 = designs.propose_feature(beta["token"], did, "Panel engine block")
    q = discuss.ask_question(beta["token"], did, "Panel fuel?")
    _age_design(did)
    csrf = _csrf_token()

    # --- auth + docket ------------------------------------------------------
    denied = _call(admin.designs_admin_page, _req("GET", "/admin/designs"))
    assert denied.status_code == 401, denied.status_code
    index = _call(
        admin.designs_admin_page, _req("GET", "/admin/designs", headers=_ok_auth())
    )
    assert index.status_code == 200, index.status_code
    assert f"/admin/designs/{did}" in index.body.decode("utf-8")
    missing = _call(
        admin.design_admin_detail_page,
        _req(
            "GET",
            "/admin/designs/424242",
            params={"design_id": 424242},
            headers=_ok_auth(),
        ),
    )
    assert "No such design" in missing.body.decode("utf-8")
    print("  auth+docket: ok")

    # --- decide feature (approve; reject needs a note) -----------------------
    det = _call(
        admin.design_admin_detail_page,
        _req(
            "GET",
            f"/admin/designs/{did}",
            params={"design_id": did},
            headers=_ok_auth(),
        ),
    )
    assert "Panel engine block" in det.body.decode("utf-8")
    r = _post(
        admin.design_admin_decide_feature,
        f"/admin/designs/{did}/decide-feature",
        {"design_id": did},
        {"feature_id": str(f1["feature_id"]), "decision": "approve"},
        csrf,
    )
    assert r.status_code == 200, r.status_code
    assert "accepted" in r.body.decode("utf-8")
    f2 = designs.propose_feature(beta["token"], did, "Panel plaid paint")
    r = _post(
        admin.design_admin_decide_feature,
        f"/admin/designs/{did}/decide-feature",
        {"design_id": did},
        {"feature_id": str(f2["feature_id"]), "decision": "reject"},
        csrf,
    )
    assert "requires a note" in r.body.decode("utf-8")
    r = _post(
        admin.design_admin_decide_feature,
        f"/admin/designs/{did}/decide-feature",
        {"design_id": did},
        {"feature_id": str(f2["feature_id"]), "decision": "reject", "note": "No plaid"},
        csrf,
    )
    assert "rejected" in r.body.decode("utf-8")
    print("  decide-feature: ok")

    # --- answer + resolve + move + toggle --------------------------------------
    r = _post(
        admin.design_admin_answer,
        f"/admin/designs/{did}/answer",
        {"design_id": did},
        {"question_id": str(q["question_id"]), "answer": "Sunlight."},
        csrf,
    )
    assert "answered" in r.body.decode("utf-8")
    i1 = issues.propose_issue(beta["token"], did, "Panel overheat")
    flow_box = _post(
        admin.design_admin_decide_issue,
        f"/admin/designs/{did}/decide-issue",
        {"design_id": did},
        {"issue_id": str(i1["issue_id"]), "decision": "approve"},
        csrf,
    )
    assert "accepted" in flow_box.body.decode("utf-8")
    r = _post(
        admin.design_admin_resolve_issue,
        f"/admin/designs/{did}/resolve-issue",
        {"design_id": did},
        {"issue_id": str(i1["issue_id"])},
        csrf,
    )
    assert "resolved" in r.body.decode("utf-8")
    r = _post(
        admin.design_admin_move_item,
        f"/admin/designs/{did}/move-item",
        {"design_id": did},
        {"kind": "feature", "item_id": str(f1["feature_id"]), "direction": "down"},
        csrf,
    )
    assert "moved" in r.body.decode("utf-8")
    r = _post(
        admin.design_admin_toggle_comments,
        f"/admin/designs/{did}/toggle-comments",
        {"design_id": did},
        {"enabled": "on"},
        csrf,
    )
    assert "enabled" in r.body.decode("utf-8")
    print("  triage-posts: ok")

    # --- system lifecycle: create form, create, meta, close 2-step --------------
    assert "/admin/designs/create" in index.body.decode("utf-8")
    r = _post(
        admin.design_admin_create_design,
        "/admin/designs/create",
        {},
        {
            "title": "Panel system design",
            "description": "Made by panel",
            "request_text": "Wanted: quiet",
            "tag_new_ideas": "on",
        },
        csrf,
    )
    assert "created (system-owned)" in r.body.decode("utf-8")
    with db._conn() as conn:
        sysrow = conn.execute(
            "SELECT id, owner_admin_id FROM designs WHERE title = ?",
            ("Panel system design",),
        ).fetchone()
    assert sysrow is not None and sysrow["owner_admin_id"] is None, dict(sysrow)
    sysdid = int(sysrow["id"])
    sysdet = _call(
        admin.design_admin_detail_page,
        _req(
            "GET",
            f"/admin/designs/{sysdid}",
            params={"design_id": sysdid},
            headers=_ok_auth(),
        ),
    )
    sysbody = sysdet.body.decode("utf-8")
    assert f"/admin/designs/{sysdid}/edit-meta" in sysbody
    assert f"/admin/designs/{sysdid}/close" in sysbody
    assert "Author content directly" in sysbody
    r = _post(
        admin.design_admin_create_feature,
        f"/admin/designs/{sysdid}/feature/create",
        {"design_id": sysdid},
        {"text": "Panel authored feature"},
        csrf,
    )
    assert "added and accepted" in r.body.decode("utf-8")
    with db._conn() as conn:
        direct_fid = int(
            conn.execute(
                "SELECT id FROM design_features WHERE design_id = ?"
                " AND text = ? AND state = 'accepted'",
                (sysdid, "Panel authored feature"),
            ).fetchone()[0]
        )
    r = _post(
        admin.design_admin_create_issue,
        f"/admin/designs/{sysdid}/issue/create",
        {"design_id": sysdid},
        {"text": "Panel authored issue", "feature_id": str(direct_fid)},
        csrf,
    )
    assert "added and accepted" in r.body.decode("utf-8")
    with db._conn() as conn:
        direct_iid = int(
            conn.execute(
                "SELECT id FROM design_issues WHERE design_id = ?"
                " AND text = ? AND state = 'accepted'",
                (sysdid, "Panel authored issue"),
            ).fetchone()[0]
        )
    r = _post(
        admin.design_admin_edit_feature,
        f"/admin/designs/{sysdid}/feature/edit",
        {"design_id": sysdid},
        {"feature_id": str(direct_fid), "text": "Panel authored feature v2"},
        csrf,
    )
    assert "updated" in r.body.decode("utf-8")
    r = _post(
        admin.design_admin_edit_issue,
        f"/admin/designs/{sysdid}/issue/edit",
        {"design_id": sysdid},
        {"issue_id": str(direct_iid), "text": "Panel authored issue v2"},
        csrf,
    )
    assert "updated" in r.body.decode("utf-8")
    r = _post(
        admin.design_admin_remove_feature,
        f"/admin/designs/{sysdid}/feature/remove",
        {"design_id": sysdid},
        {"feature_id": str(direct_fid)},
        csrf,
    )
    assert "removed" in r.body.decode("utf-8")
    r = _post(
        admin.design_admin_edit_design_meta,
        f"/admin/designs/{sysdid}/edit-meta",
        {"design_id": sysdid},
        {
            "title": "Panel system design v2",
            "description": "Made by panel",
            "request_text": "Wanted: quiet",
            "tag_new_ideas": "on",
            "tag_improvements": "on",
        },
        csrf,
    )
    assert "updated" in r.body.decode("utf-8")
    designs.propose_feature(beta["token"], sysdid, "Panel pending widget")
    r = _post(
        admin.design_admin_close_design,
        f"/admin/designs/{sysdid}/close",
        {"design_id": sysdid},
        {},
        csrf,
    )
    assert "tick confirm" in r.body.decode("utf-8")
    r = _post(
        admin.design_admin_close_design,
        f"/admin/designs/{sysdid}/close",
        {"design_id": sysdid},
        {"confirm": "on"},
        csrf,
    )
    assert "archived" in r.body.decode("utf-8")
    sysfrozen = _call(
        admin.design_admin_detail_page,
        _req(
            "GET",
            f"/admin/designs/{sysdid}",
            params={"design_id": sysdid},
            headers=_ok_auth(),
        ),
    )
    assert "Frozen" in sysfrozen.body.decode("utf-8")
    assert f"/admin/designs/{sysdid}/close" not in sysfrozen.body.decode("utf-8")
    print("  system-lifecycle: ok")

    # --- bad csrf + bad auth -----------------------------------------------------
    bad = _call(
        admin.design_admin_answer,
        _req(
            "POST",
            f"/admin/designs/{did}/answer",
            params={"design_id": did},
            body={"csrf": "wrong", "question_id": "1", "answer": "x"},
            headers=_ok_auth(),
        ),
    )
    assert "CSRF" in bad.body.decode("utf-8")
    # --- garbage ids flash a refusal instead of 500 --------------------------------
    junk = _post(
        admin.design_admin_decide_feature,
        f"/admin/designs/{did}/decide-feature",
        {"design_id": did},
        {"feature_id": "abc", "decision": "approve"},
        csrf,
    )
    assert junk.status_code == 200, junk.status_code
    assert "must be an integer" in junk.body.decode("utf-8")
    denied = _call(
        admin.design_admin_answer,
        _req(
            "POST",
            f"/admin/designs/{did}/answer",
            params={"design_id": did},
            cookies={_CSRF: csrf},
            body={"csrf": csrf, "question_id": "1", "answer": "x"},
            headers=[(b"authorization", _BAD_AUTH.encode())],
        ),
    )
    assert denied.status_code == 401, denied.status_code
    print("  guards: ok")

    # --- frozen detail renders state without forms -------------------------------
    closed = discuss.close_design(alpha["token"], did, confirm=True)
    assert closed["status"] == "archived", closed
    frozen = _call(
        admin.design_admin_detail_page,
        _req(
            "GET",
            f"/admin/designs/{did}",
            params={"design_id": did},
            headers=_ok_auth(),
        ),
    )
    frozen_body = frozen.body.decode("utf-8")
    assert "Frozen" in frozen_body, "frozen banner renders"
    assert "method='post'" not in frozen_body, "frozen detail carries no panel forms"
    print("  frozen: ok")

    print("test_designs_admin_panel: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
