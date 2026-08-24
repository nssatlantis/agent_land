"""Regression guards for github.py's thread-local keep-alive pool
(proposal #179): an ok_404 miss used to return without draining the
response body, desynchronising the reused connection's stream, and the
reconnect-retry fallback only caught socket-level errors - so a single
http.client.HTTPException (CannotSendRequest / ResponseNotReady /
BadStatusLine) poisoned the per-thread handle until process restart,
failing every later call on that thread in well under a millisecond.
These tests pin the healing contract with scripted fake connections:
protocol-level failures reconnect exactly once and succeed, drained
404-ok bodies keep the stream in sync, healthy reuse never reconnects,
and the non-OK RepoError path still reads the body."""

import http.client
import importlib.util
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent / "github.py"
_spec = importlib.util.spec_from_file_location("agentland_root_github", _ROOT)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)

gh.GITHUB_TOKEN = "test-token"  # satisfies _ensure_token(); no network touched


class FakeResponse:
    def __init__(self, status=200, body=b'{"value": 7}'):
        self.status = status
        self._body = body
        self.read_called = False

    def read(self):
        self.read_called = True
        return self._body


class ScriptedConn:
    """A scripted HTTPSConnection stand-in: request()/getresponse() raise
    their configured exception once (if any), then behave normally."""

    def __init__(self, req_exc=None, resp=None, resp_exc=None):
        self.req_exc = req_exc
        self.resp = resp if resp is not None else FakeResponse()
        self.resp_exc = resp_exc
        self.sock = object()
        self.closed = False
        self.request_calls = 0

    def request(self, *args, **kwargs):
        self.request_calls += 1
        if self.req_exc is not None:
            exc, self.req_exc = self.req_exc, None
            raise exc

    def getresponse(self):
        if self.resp_exc is not None:
            exc, self.resp_exc = self.resp_exc, None
            raise exc
        return self.resp

    def close(self):
        self.closed = True
        self.sock = None


def _reset_pool():
    try:
        gh._conn.handle = None
    except AttributeError:
        pass


def _install(*conns):
    """Patch http.client.HTTPSConnection so github.py's REAL pool logic
    constructs the scripted conns in order (repeating the last one).
    Returns (created_list, patch_context_manager)."""
    made = []
    lock = threading.Lock()

    def factory(*args, **kwargs):
        with lock:
            conn = conns[len(made)] if len(made) < len(conns) else conns[-1]
            made.append(conn)
            return conn

    class _Patched:
        def __init__(self):
            self._original = None

        def __enter__(self):
            self._original = http.client.HTTPSConnection
            http.client.HTTPSConnection = factory
            return self

        def __exit__(self, *exc_info):
            http.client.HTTPSConnection = self._original
            return False

    return made, _Patched()


def _run(method="GET", path="pulls/1", **kwargs):
    return gh._request(method, path, **kwargs)


def test_cannot_send_request_retries_on_fresh_connection():
    _reset_pool()
    bad = ScriptedConn(req_exc=http.client.CannotSendRequest("Request-sent"))
    good = ScriptedConn(resp=FakeResponse(200, b'{"value": 7}'))
    made, patcher = _install(bad, good)
    with patcher:
        assert _run() == {"value": 7}
    assert bad.closed is True, "poisoned handle must be closed on heal"
    assert gh._conn.handle is good, "pool must point at the healthy conn"
    assert len(made) == 2, f"exactly one reconnect expected, saw {len(made)}"
    print("  CannotSendRequest heals via one reconnect: ok")


def test_bad_status_line_heals():
    _reset_pool()
    bad = ScriptedConn(resp_exc=http.client.BadStatusLine(""))
    good = ScriptedConn(resp=FakeResponse(200, b'{"ok": true}'))
    made, patcher = _install(bad, good)
    with patcher:
        assert _run() == {"ok": True}
    assert len(made) == 2 and bad.closed
    print("  BadStatusLine heals via one reconnect: ok")


def test_response_not_ready_heals():
    _reset_pool()
    bad = ScriptedConn(resp_exc=http.client.ResponseNotReady("Request-sent"))
    good = ScriptedConn(resp=FakeResponse(200, b'{"n": 1}'))
    made, patcher = _install(bad, good)
    with patcher:
        assert _run() == {"n": 1}
    assert len(made) == 2 and bad.closed
    print("  ResponseNotReady heals via one reconnect: ok")


def test_ok_404_drains_body_before_returning():
    _reset_pool()
    conn = ScriptedConn(resp=FakeResponse(404, b'{"message": "Not Found"}'))
    made, patcher = _install(conn)
    with patcher:
        assert _run(ok_404=True) is None
    assert conn.resp.read_called is True, "404-ok body must be drained"
    assert len(made) == 1, "draining must not force a reconnect"
    print("  ok_404 drains body before returning: ok")


def test_ok_404_in_request_text_drains_too():
    _reset_pool()
    conn = ScriptedConn(resp=FakeResponse(404, b"gone"))
    made, patcher = _install(conn)
    with patcher:
        assert gh._request_text("GET", "actions/jobs/1/logs", ok_404=True) is None
    assert conn.resp.read_called is True
    assert len(made) == 1
    print("  _request_text ok_404 drains too: ok")


def test_healthy_reuse_does_not_reconnect():
    _reset_pool()
    conn = ScriptedConn(resp=FakeResponse(200, b'{"a": 1}'))
    made, patcher = _install(conn)
    with patcher:
        first = _run(path="a")
        second = _run(path="b")
    assert first == {"a": 1} and second == {"a": 1}
    assert len(made) == 1, "healthy connections must be reused"
    assert conn.request_calls == 2
    print("  healthy reuse stays on one connection: ok")


def test_non_ok_error_path_still_reads_body_and_raises():
    _reset_pool()
    conn = ScriptedConn(resp=FakeResponse(500, b'{"message": "boom"}'))
    made, patcher = _install(conn)
    raised = None
    with patcher:
        try:
            _run()
        except gh.RepoError as e:
            raised = str(e)
    assert raised is not None and "500" in raised and "boom" in raised, raised
    assert conn.resp.read_called is True, "error path reads the body"
    print("  non-OK path raises RepoError with body message: ok")


def main():
    test_cannot_send_request_retries_on_fresh_connection()
    test_bad_status_line_heals()
    test_response_not_ready_heals()
    test_ok_404_drains_body_before_returning()
    test_ok_404_in_request_text_drains_too()
    test_healthy_reuse_does_not_reconnect()
    test_non_ok_error_path_still_reads_body_and_raises()
    print("test_github_http: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
