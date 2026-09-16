"""Tests for viewer-500 auto-reports (proposal #521).

First hit files an open bug report (NULL reporter once that migration
lands); repeats bump the queue counter and never touch confidence; the
ServerErrorReports middleware re-raises untouched and only watches GET
outside /mcp; RequestLogging records status 500 on the exception path.
"""

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_server_500_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.middleware as mw_mod  # noqa: E402, I001
from logutil import RequestLogging  # noqa: E402, I001
from server.middleware import ServerErrorReports  # noqa: E402, I001
from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()


def _null_supported():
    """NULL filing works only once the NULL-reporter migration lands."""
    with db._conn() as conn:
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(bug_reports)")}
    return "auto_signature" in cols and int(cols["agent_id"][3]) == 0


def _with_env(settings, fn):
    saved = {}
    for key, value in settings.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        fn()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _hit_row(sig):
    with db._conn() as conn:
        return conn.execute(
            "SELECT signature, path, exc_type, occurrences, report_id"
            " FROM server_error_hits WHERE signature = ?",
            (sig,),
        ).fetchone()


def _bug_row(rid):
    with db._conn() as conn:
        return conn.execute(
            "SELECT id, agent_id, status, confidence, severity, auto_signature"
            " FROM bug_reports WHERE id = ?",
            (rid,),
        ).fetchone()


def test_migration_present():
    with db._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bug_reports)")}
        assert "auto_signature" in cols, "auto_signature column migrated"
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "server_error_hits" in tables, "server_error_hits table migrated"


def test_first_hit_files():
    sig = "GET /recent/:id KeyError t-file-1"
    out = db.record_server_error(
        sig,
        "/recent/:id",
        "KeyError",
        "500 on /recent/:id: KeyError: 'text'",
        "Auto-filed test body.",
        url="/recent/:id",
        evidence="Traceback (most recent call last): ... KeyError: 'text'",
        repro_steps="Request GET /recent/:id.",
    )
    assert out["ok"] is True
    assert out["occurrences"] == 1
    hit = _hit_row(sig)
    assert hit is not None and hit["occurrences"] == 1
    if _null_supported():
        assert out["filed"] is True, out
        assert out["report_id"] is not None
        assert hit["report_id"] == out["report_id"]
        bug = _bug_row(out["report_id"])
        assert bug["agent_id"] is None, "auto-reports carry the NULL reporter"
        assert bug["status"] == "open" and bug["confidence"] == 1
        assert bug["severity"] == "high" and bug["auto_signature"] == sig
    else:
        assert out["filed"] is False and out["report_id"] is None
        assert out["reason"] == "promotion-unavailable", out
        print("  (NULL-reporter migration not on main yet: queue-only)")


def test_repeat_bumps_counter_not_confidence():
    sig = "GET /posts/:id ValueError t-repeat-1"
    first = db.record_server_error(sig, "/posts/:id", "ValueError", "500 t", "b")
    second = db.record_server_error(sig, "/posts/:id", "ValueError", "500 t", "b")
    third = db.record_server_error(sig, "/posts/:id", "ValueError", "500 t", "b")
    assert third["occurrences"] == first["occurrences"] + 2
    assert second["report_id"] == first["report_id"]
    assert third["report_id"] == first["report_id"]
    if first["report_id"] is not None:
        bug = _bug_row(first["report_id"])
        assert bug["confidence"] == 1, "machine repeats never raise confidence"
        with db._conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM bug_reports WHERE auto_signature = ?", (sig,)
            ).fetchone()[0]
        assert n == 1, "one signature files at most one live report"


def test_resolved_report_refiles():
    if not _null_supported():
        print("  (skip refile: needs NULL filing)")
        return
    sig = "GET /status RuntimeError t-refile-1"
    first = db.record_server_error(sig, "/status", "RuntimeError", "500 t", "b")
    assert first["filed"] is True
    rid1 = first["report_id"]
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE bug_reports SET status = 'fixed' WHERE id = ?", (rid1,))
    second = db.record_server_error(sig, "/status", "RuntimeError", "500 t", "b")
    assert second["filed"] is True, second
    assert second["report_id"] != rid1, "resolved report never swallows hits"
    hit = _hit_row(sig)
    assert hit["report_id"] == second["report_id"]
    assert hit["occurrences"] == 2


def test_preseeded_auto_row_links():
    sig = "GET /bugs/:id TypeError t-link-1"
    agent_id = AGENTS["alpha"]["agent_id"]
    with db._conn(immediate=True) as conn:
        cur = conn.execute(
            "INSERT INTO bug_reports"
            " (agent_id, title, body, status, confidence, created_at, auto_signature)"
            " VALUES (?, 'pre-seeded auto row', 'b', 'open', 1,"
            " '2026-09-16T00:00:00Z', ?)",
            (agent_id, sig),
        )
        rid = cur.lastrowid
    out = db.record_server_error(sig, "/bugs/:id", "TypeError", "500 t", "b")
    assert out["filed"] is False and out["report_id"] == rid
    assert out["reason"] == "already-reported", out
    assert _hit_row(sig)["report_id"] == rid


def test_daily_cap_queues_only():
    sig = "GET /economy ZeroDivisionError t-cap-1"

    def _run():
        out = db.record_server_error(sig, "/economy", "ZeroDivisionError", "500 t", "b")
        assert out["filed"] is False and out["report_id"] is None
        assert out["reason"] == "daily-cap", out
        assert out["occurrences"] == 1

    _with_env({"FORUM_SERVER_ERROR_MAX_NEW_PER_DAY": "0"}, _run)


def test_disabled_logs_only():
    sig = "GET /jobs KeyError t-off-1"

    def _run():
        out = db.record_server_error(sig, "/jobs", "KeyError", "500 t", "b")
        assert out["filed"] is False and out["report_id"] is None
        assert out["reason"] == "disabled", out

    _with_env({"FORUM_SERVER_ERROR_REPORTS_ENABLED": "0"}, _run)


async def _boom(scope, receive, send):
    raise ValueError("boom")


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def test_middleware_records_and_reraises():
    sent = []

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "GET", "path": "/recent/123"}
    try:
        asyncio.run(ServerErrorReports(_boom)(scope, _receive, send))
    except ValueError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("middleware swallowed the exception")
    assert sent == [], "nothing is sent before the re-raise"
    with db._conn() as conn:
        hit = conn.execute(
            "SELECT path, exc_type, occurrences FROM server_error_hits"
            " WHERE signature LIKE 'GET /recent/:id ValueError %'",
        ).fetchone()
    assert hit is not None, "GET crash recorded under a redacted signature"
    assert hit["path"] == "/recent/:id" and hit["exc_type"] == "ValueError"


def test_middleware_scope_skips():
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    with db._conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM server_error_hits").fetchone()[0]
    for scope in (
        {"type": "http", "method": "POST", "path": "/"},
        {"type": "http", "method": "GET", "path": "/mcp"},
        {"type": "http", "method": "GET", "path": "/mcp/"},
        {"type": "lifespan"},
    ):
        sent = []

        async def send(msg, _sent=sent):
            _sent.append(msg)

        asyncio.run(ServerErrorReports(_ok)(scope, receive, send))
        if scope["type"] == "http":
            assert sent and sent[0]["status"] == 200
    with db._conn() as conn:
        after = conn.execute("SELECT COUNT(*) FROM server_error_hits").fetchone()[0]
    assert after == before, "out-of-scope requests record nothing"


def test_filing_failure_still_reraises_original():
    real = db.record_server_error

    def _raiser(*args, **kwargs):
        raise RuntimeError("db is down")

    db.record_server_error = _raiser
    flagged = []
    try:

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            pass

        try:
            asyncio.run(
                ServerErrorReports(_boom)(
                    {"type": "http", "method": "GET", "path": "/oops"},
                    receive,
                    send,
                )
            )
            flagged.append("swallowed")
        except RuntimeError:
            flagged.append("masked")
        except ValueError:
            flagged.append("reraised")
    finally:
        db.record_server_error = real
    assert flagged == ["reraised"], flagged


def test_request_logging_marks_500():
    seen = []

    class _Cap(logging.Handler):
        def emit(self, record):
            seen.append(record)

    logger = logging.getLogger("agentland.request")
    cap = _Cap()
    logger.addHandler(cap)
    try:

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            pass

        try:
            asyncio.run(
                RequestLogging(_boom)(
                    {"type": "http", "method": "GET", "path": "/x"}, receive, send
                )
            )
            raised = False
        except ValueError:
            raised = True
        assert raised, "RequestLogging swallowed the exception"
    finally:
        logger.removeHandler(cap)
    assert seen, "request line still logged on the exception path"
    assert seen[-1].msg["status"] == 500, seen[-1].msg


def test_redact_and_frame_helpers():
    assert mw_mod._redact_server_error_path("/posts/123") == "/posts/:id"
    assert mw_mod._redact_server_error_path("/recent") == "/recent"
    assert mw_mod._redact_server_error_path("/") == "/"
    try:
        raise RuntimeError("frame-probe")
    except RuntimeError as exc:
        frame = mw_mod._server_error_repo_frame(exc.__traceback__)
    assert "test_redact_and_frame_helpers" in frame


def main():
    test_migration_present()
    test_first_hit_files()
    test_repeat_bumps_counter_not_confidence()
    test_resolved_report_refiles()
    test_preseeded_auto_row_links()
    test_daily_cap_queues_only()
    test_disabled_logs_only()
    test_middleware_records_and_reraises()
    test_middleware_scope_skips()
    test_filing_failure_still_reraises_original()
    test_request_logging_marks_500()
    test_redact_and_frame_helpers()
    print("test_server_500_reports: all ok")


if __name__ == "__main__":
    main()
