"""Ticket-minted HTTP file transfers plus P2 write upgrades (proposal #597).

Covers the transfer_tickets record (mint gates, redeem scopes, per-path
burn, expiry, sweep, legacy-DB migration), the HTTP data plane
(GET bytes/headers/304/refusals, POST apply/receipt/refusals, bounded
bodies both declared and chunked), the MCP ticket tools (URL shape, sha
pins), and the P2 workspace_write_file upgrades (sha echo, expect guard,
dry_run, quiet no-op, content EOL normalization).

Trees run against a local bare remote (no network); HTTP handlers are
driven in-process with starlette Request objects (the test_admin_http
pattern), so no server boots.
"""

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_transfer_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github._gitops as gh  # noqa: E402
import github._workspaces as ws  # noqa: E402
import server._transfer as TR  # noqa: E402
import server.tools.repo._transfer as TT  # noqa: E402
import server.tools.repo._workspace as WT  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _mk_remote(tmp):
    bare = os.path.join(tmp, "remote.git")
    seed = os.path.join(tmp, "seed")
    os.makedirs(seed)
    _git("init", "--bare", "-b", "main", bare)
    _git("init", "-b", "main", cwd=seed)
    with open(os.path.join(seed, "README.md"), "w", newline="") as f:
        f.write("seed\n")
    with open(os.path.join(seed, "app.py"), "w", newline="") as f:
        f.write("X = 1\n")
    _git("-C", seed, "add", "-A")
    _git(
        "-C", seed, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-m", "seed"
    )
    _git("-C", seed, "push", bare, "main")
    return bare


_SHARED_BARE = _mk_remote(tempfile.mkdtemp(prefix="agentland_ws_xfer_remote_"))


class _Sandbox:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="agentland_ws_xfer_test_")
        self._orig = {
            "repo_url": ws._repo_url,
            "claims_root": ws._claims_root,
            "gitops_url": gh._repo_url,
        }
        ws._repo_url = lambda with_token=False: _SHARED_BARE
        ws._claims_root = lambda: os.path.join(self.tmp, "claims")
        gh._repo_url = lambda with_token=False: _SHARED_BARE

    def close(self):
        ws._repo_url = self._orig["repo_url"]
        ws._claims_root = self._orig["claims_root"]
        gh._repo_url = self._orig["gitops_url"]


_SB = _Sandbox()


def _prop(agents, who="alpha", title="Xfer Home"):
    return db.create_proposal(agents[who]["token"], title, "body")["post_id"]


def _claim(agents, pid, name="xfer", who="alpha"):
    db.claim_workspace(agents[who]["token"], pid, name)
    ws.ensure_claim_tree(agents[who]["agent_id"], pid, name)
    info = ws.claim_tree_info(agents[who]["agent_id"], pid, name)
    assert info["exists"], info
    return info


def _req(method, ticket, fpath, body=b"", headers=None):
    header_bytes = list(headers or [])
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": f"/transfer/{ticket}/{fpath}",
        "root_path": "",
        "query_string": b"",
        "headers": header_bytes,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "path_params": {"ticket": ticket, "fpath": fpath},
        "state": {},
    }

    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    from starlette.requests import Request

    return Request(scope, receive)


def _req_chunked(method, ticket, fpath, chunks):
    header_bytes = []
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": f"/transfer/{ticket}/{fpath}",
        "root_path": "",
        "query_string": b"",
        "headers": header_bytes,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "path_params": {"ticket": ticket, "fpath": fpath},
        "state": {},
    }
    it = iter(chunks)

    async def receive():
        try:
            chunk = next(it)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    from starlette.requests import Request

    return Request(scope, receive)


def _run(coro):
    return asyncio.run(coro)


def test_table_exists():
    with db._conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        idx = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "transfer_tickets" in tables
    assert "idx_transfer_tickets_agent" in idx or any(
        "transfer_tickets" in i for i in idx
    ), idx
    assert "idx_transfer_tickets_sweep" in idx, idx
    print("  transfer_tickets table + indexes exist: ok")


def test_mint_redeem_roundtrip(agents):
    pid = _prop(agents)
    tok = agents["alpha"]["token"]
    db.claim_workspace(tok, pid, "round")
    minted = db.mint_transfer_ticket(tok, pid, "round", ["README.md"], "read")
    assert minted["ticket"].startswith("xfer_"), minted
    assert minted["paths"] == ["README.md"] and minted["scope"] == "read"
    # Raw secret is stored hashed, never plaintext.
    with db._conn() as conn:
        row = conn.execute(
            "SELECT ticket_hash FROM transfer_tickets"
            " WHERE agent_id = ? AND scope = 'read'",
            (agents["alpha"]["agent_id"],),
        ).fetchone()
    assert minted["ticket"] not in row["ticket_hash"]
    assert row["ticket_hash"] == hashlib.sha256(minted["ticket"].encode()).hexdigest()
    # Read scope never burns: redeem twice.
    t1 = db.redeem_transfer_ticket(minted["ticket"], "read", "README.md")
    t2 = db.redeem_transfer_ticket(minted["ticket"], "read", "README.md")
    assert t1["agent_id"] == agents["alpha"]["agent_id"] == t2["agent_id"]
    # Wrong scope is refused.
    assert "read-only" in expect_error(
        db.redeem_transfer_ticket, minted["ticket"], "write", "README.md"
    )
    # Write scope burns per path: two paths, first stays unused.
    w = db.mint_transfer_ticket(tok, pid, "round", ["a.txt", "b.txt"], "write")
    r1 = db.redeem_transfer_ticket(w["ticket"], "write", "a.txt")
    assert r1["used_paths"] == ["a.txt"], r1
    assert "already uploaded" in expect_error(
        db.redeem_transfer_ticket, w["ticket"], "write", "a.txt"
    )
    r2 = db.redeem_transfer_ticket(w["ticket"], "write", "b.txt")
    assert sorted(r2["used_paths"]) == ["a.txt", "b.txt"], r2
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM transfer_tickets WHERE ticket_hash = ?",
            (hashlib.sha256(w["ticket"].encode()).hexdigest(),),
        ).fetchone()["status"]
    assert status == "used", status
    assert "already used" in expect_error(
        db.redeem_transfer_ticket, w["ticket"], "write", "a.txt"
    )
    print("  mint/redeem roundtrip + per-path burn: ok")


def test_mint_gates(agents):
    pid = _prop(agents, title="Gated Xfer")
    tok = agents["alpha"]["token"]
    db.claim_workspace(tok, pid, "gated")
    # Outsider cannot mint on another citizen's claim.
    assert "of yours" in expect_error(
        db.mint_transfer_ticket,
        agents["beta"]["token"],
        pid,
        "gated",
        ["README.md"],
        "read",
    )
    # Unknown claim refused.
    assert "no active workspace" in expect_error(
        db.mint_transfer_ticket, tok, pid, "nope", ["README.md"], "read"
    )
    # Bad scope refused.
    assert "scope" in expect_error(
        db.mint_transfer_ticket, tok, pid, "gated", ["README.md"], "delete"
    )
    # Over-cap refused.
    many = [f"f{i}.txt" for i in range(50)]
    assert "cap" in expect_error(
        db.mint_transfer_ticket, tok, pid, "gated", many, "read"
    )
    # Pins outside the ticket refused.
    assert "outside this ticket" in expect_error(
        db.mint_transfer_ticket,
        tok,
        pid,
        "gated",
        ["a.txt"],
        "write",
        {"b.txt": "0" * 64},
    )
    # Unknown ticket reads 404 without leaking existence.
    err = expect_error(db.redeem_transfer_ticket, "xfer_nope", "read", "README.md")
    assert "unknown transfer ticket" in err
    print("  mint/redeem gates: ok")


def test_expiry_and_sweep(agents):
    pid = _prop(agents, title="Expiring Xfer")
    tok = agents["alpha"]["token"]
    db.claim_workspace(tok, pid, "exp")
    m = db.mint_transfer_ticket(tok, pid, "exp", ["README.md"], "read")
    with db._conn() as conn:
        conn.execute(
            "UPDATE transfer_tickets SET expires_at = '2000-01-01T00:00:00.000Z'"
            " WHERE ticket_hash = ?",
            (hashlib.sha256(m["ticket"].encode()).hexdigest(),),
        )
    assert "expired" in expect_error(
        db.redeem_transfer_ticket, m["ticket"], "read", "README.md"
    )
    # The raise path rolls the inline mark back (fresh-conn-per-call
    # semantics); the lazy sweep is what persists it.
    assert db.sweep_expired_transfer_tickets() >= 1
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM transfer_tickets WHERE ticket_hash = ?",
            (hashlib.sha256(m["ticket"].encode()).hexdigest(),),
        ).fetchone()["status"]
    assert status == "expired", status
    print("  expiry marks + sweep: ok")


def test_http_download(agents):
    pid = _prop(agents, "beta", title="Download Xfer")
    tok = agents["beta"]["token"]
    _claim(agents, pid, "dl", who="beta")
    WT.workspace_write_file(tok, pid, "dl", "note.txt", content="hello xfer\n")
    t = TT.workspace_fetch_ticket(tok, pid, "dl", ["README.md", "note.txt"])
    assert t["scope"] == "read" and t["ticket"].startswith("xfer_")
    assert [f["path"] for f in t["files"]] == ["README.md", "note.txt"]
    assert all(f["url"].startswith("/transfer/xfer_") for f in t["files"])
    assert all(len(f["sha256"]) == 64 for f in t["files"])
    assert "base" in t and "expires_at" in t
    resp = _run(TR.transfer_download(_req("GET", t["ticket"], "note.txt")))
    assert resp.status_code == 200, resp.status_code
    assert resp.body == b"hello xfer\n", resp.body
    assert resp.headers["x-content-sha256"] == t["files"][1]["sha256"]
    assert resp.headers["etag"] == f'"{t["files"][1]["sha256"]}"'
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "private, no-store"
    # Revalidation hits 304.
    resp304 = _run(
        TR.transfer_download(
            _req(
                "GET",
                t["ticket"],
                "note.txt",
                headers=[(b"if-none-match", f'"{t["files"][1]["sha256"]}"'.encode())],
            )
        )
    )
    assert resp304.status_code == 304, resp304.status_code
    # Unknown ticket 404s without leaking.
    resp404 = _run(TR.transfer_download(_req("GET", "xfer_nope123", "note.txt")))
    assert resp404.status_code == 404, resp404.status_code
    # A write ticket cannot download.
    w = TT.workspace_upload_ticket(tok, pid, "dl", ["note.txt"])
    resp400 = _run(TR.transfer_download(_req("GET", w["ticket"], "note.txt")))
    assert resp400.status_code == 400, resp400.status_code
    # A path outside the ticket is refused.
    resp_out = _run(TR.transfer_download(_req("GET", t["ticket"], "app.py")))
    assert resp_out.status_code == 400, resp_out.status_code
    print("  HTTP download bytes/headers/304/refusals: ok")


def test_http_upload_apply(agents):
    pid = _prop(agents, "gamma", title="Upload Xfer")
    tok = agents["gamma"]["token"]
    _claim(agents, pid, "up", who="gamma")
    WT.workspace_write_file(tok, pid, "up", "app.py", content="X = 1\n")
    read = WT.workspace_read_file(tok, pid, "up", "app.py")
    base_sha = read["content_sha256"]
    w = TT.workspace_upload_ticket(
        tok, pid, "up", ["app.py", "brand.py"], {"app.py": base_sha}
    )
    assert w["scope"] == "write"
    body = b"X = 2\nY = 3\n"
    resp = _run(
        TR.transfer_upload(
            _req(
                "POST",
                w["ticket"],
                "app.py",
                body=body,
                headers=[(b"content-length", str(len(body)).encode())],
            )
        )
    )
    assert resp.status_code == 200, (resp.status_code, resp.body)
    import json as _json

    receipt = _json.loads(resp.body.decode())
    assert receipt["changed"] is True and receipt["path"] == "app.py"
    assert receipt["content_sha256"] == hashlib.sha256(b"X = 2\nY = 3\n").hexdigest()
    # Live tree carries the upload; the diff shows it.
    live = WT.workspace_read_file(tok, pid, "up", "app.py")
    assert live["content"] == "X = 2\nY = 3", live
    assert live["content_sha256"] == receipt["content_sha256"]
    diff = WT.workspace_diff(tok, pid, "up", path="app.py")
    assert "Y = 3" in diff["diff"], diff
    # Same path cannot POST twice on one ticket.
    resp2 = _run(TR.transfer_upload(_req("POST", w["ticket"], "app.py", body=body)))
    assert resp2.status_code == 409, (resp2.status_code, resp2.body)
    # Identical bytes on a FRESH ticket are a quiet no-op.
    w2 = TT.workspace_upload_ticket(tok, pid, "up", ["app.py"])
    resp3 = _run(TR.transfer_upload(_req("POST", w2["ticket"], "app.py", body=body)))
    assert resp3.status_code == 200, (resp3.status_code, resp3.body)
    assert _json.loads(resp3.body.decode())["changed"] is False
    # Stale pin refuses before any byte moves.
    w3 = TT.workspace_upload_ticket(tok, pid, "up", ["app.py"], {"app.py": "0" * 64})
    resp4 = _run(
        TR.transfer_upload(_req("POST", w3["ticket"], "app.py", body=b"Z = 9\n"))
    )
    assert resp4.status_code == 409, (resp4.status_code, resp4.body)
    assert WT.workspace_read_file(tok, pid, "up", "app.py")["content"] == "X = 2\nY = 3"
    # Non-UTF8 and empty uploads refuse.
    w4 = TT.workspace_upload_ticket(tok, pid, "up", ["bin.dat"])
    resp5 = _run(
        TR.transfer_upload(_req("POST", w4["ticket"], "bin.dat", body=b"\xff\xfe\n"))
    )
    assert resp5.status_code == 400, (resp5.status_code, resp5.body)
    w5 = TT.workspace_upload_ticket(tok, pid, "up", ["empty.txt"])
    resp6 = _run(TR.transfer_upload(_req("POST", w5["ticket"], "empty.txt", body=b"")))
    assert resp6.status_code == 400, (resp6.status_code, resp6.body)
    print("  HTTP upload apply/receipt/pins/no-op/refusals: ok")


def test_http_upload_lock_wait_keeps_event_loop_live(agents):
    pid = _prop(agents, "beta", title="Async Upload Xfer")
    tok = agents["beta"]["token"]
    _claim(agents, pid, "asyncup", who="beta")
    WT.workspace_write_file(tok, pid, "asyncup", "held.txt", content="base\n")
    pin = WT.workspace_read_file(tok, pid, "asyncup", "held.txt")["content_sha256"]
    ticket = TT.workspace_upload_ticket(
        tok, pid, "asyncup", ["held.txt"], {"held.txt": pin}
    )
    dest = ws._claim_dir(agents["beta"]["agent_id"], pid, "asyncup")
    lock_entered = threading.Event()
    apply_entered = threading.Event()
    timing = {}
    original_apply = ws.apply_transfer_bytes

    def observed_apply(*args, **kwargs):
        apply_entered.set()
        return original_apply(*args, **kwargs)

    def hold_lock():
        with ws.workspace_lock(dest):
            lock_entered.set()
            time.sleep(0.5)
            timing["released"] = time.monotonic()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert lock_entered.wait(5)
    try:

        async def run_upload():
            upload = asyncio.create_task(
                TR.transfer_upload(
                    _req("POST", ticket["ticket"], "held.txt", body=b"next\n")
                )
            )
            while not apply_entered.is_set():
                await asyncio.sleep(0)
            heartbeat_at = None

            async def heartbeat():
                nonlocal heartbeat_at
                await asyncio.sleep(0)
                heartbeat_at = time.monotonic()

            await heartbeat()
            return await upload, heartbeat_at

        with patch.object(ws, "apply_transfer_bytes", observed_apply):
            response, heartbeat_at = asyncio.run(run_upload())
    finally:
        holder.join(5)
    assert not holder.is_alive(), "workspace lock holder did not finish"
    assert heartbeat_at < timing["released"], (heartbeat_at, timing["released"])
    assert response.status_code == 200, (response.status_code, response.body)
    assert WT.workspace_read_file(tok, pid, "asyncup", "held.txt")["content"] == "next"
    print("  async upload lock wait keeps the event loop live: ok")
    db.release_workspace(tok, pid, "asyncup")


def test_http_upload_caps(agents):
    pid = _prop(agents, "delta", title="Cap Xfer")
    tok = agents["delta"]["token"]
    _claim(agents, pid, "cap", who="delta")
    big = b"z" * ((1 << 20) + 8)
    w = TT.workspace_upload_ticket(tok, pid, "cap", ["big.txt"])
    # Declared length over cap refuses before reading.
    resp = _run(
        TR.transfer_upload(
            _req(
                "POST",
                w["ticket"],
                "big.txt",
                body=b"tiny",
                headers=[(b"content-length", str(len(big)).encode())],
            )
        )
    )
    assert resp.status_code == 413, (resp.status_code, resp.body)
    # Chunked without a length trips the bounded read instead.
    w2 = TT.workspace_upload_ticket(tok, pid, "cap", ["big2.txt"])
    resp2 = _run(
        TR.transfer_upload(
            _req_chunked("POST", w2["ticket"], "big2.txt", [big[:700000], big[700000:]])
        )
    )
    assert resp2.status_code == 413, (resp2.status_code, resp2.body)
    # Nothing landed from either refused upload.
    assert "could not read" in expect_error(
        WT.workspace_read_file, tok, pid, "cap", "big.txt"
    )
    assert "could not read" in expect_error(
        WT.workspace_read_file, tok, pid, "cap", "big2.txt"
    )
    print("  HTTP upload caps (declared + chunked): ok")


def test_release_kills_ticket(agents):
    pid = _prop(agents, "epsilon", title="Released Xfer")
    tok = agents["epsilon"]["token"]
    _claim(agents, pid, "rel", who="epsilon")
    t = TT.workspace_fetch_ticket(tok, pid, "rel", ["README.md"])
    db.release_workspace(tok, pid, "rel")
    resp = _run(TR.transfer_download(_req("GET", t["ticket"], "README.md")))
    assert resp.status_code == 404, (resp.status_code, resp.body)
    time.sleep(0.001)
    _claim(agents, pid, "rel", who="epsilon")
    resp = _run(TR.transfer_download(_req("GET", t["ticket"], "README.md")))
    assert resp.status_code == 404, (resp.status_code, resp.body)
    db.release_workspace(tok, pid, "rel")
    print("  released claim kills tickets, including after reclaim: ok")


def test_p2_write_upgrades(agents):
    pid = _prop(agents, "zeta", title="P2 Xfer")
    tok = agents["zeta"]["token"]
    _claim(agents, pid, "p2", who="zeta")
    r1 = WT.workspace_write_file(tok, pid, "p2", "w.txt", content="one\ntwo\n")
    assert len(r1["content_sha256"]) == 64 and r1["changed"] is True
    read = WT.workspace_read_file(tok, pid, "p2", "w.txt")
    assert read["content_sha256"] == r1["content_sha256"]
    dest = ws._claim_dir(agents["zeta"]["agent_id"], pid, "p2")
    before = os.path.getmtime(os.path.join(dest, "w.txt"))
    # Identical rewrite is a quiet no-op: no write, no mtime move.
    r2 = WT.workspace_write_file(tok, pid, "p2", "w.txt", content="one\ntwo\n")
    assert r2["changed"] is False, r2
    assert os.path.getmtime(os.path.join(dest, "w.txt")) == before
    # Stale pin refuses.
    try:
        WT.workspace_write_file(
            tok, pid, "p2", "w.txt", content="three\n", expect_sha256="0" * 64
        )
    except Exception as exc:
        assert "stale base" in str(exc), exc
    else:
        raise AssertionError("expected stale-base refusal")
    # Live pin succeeds and moves the sha.
    r3 = WT.workspace_write_file(
        tok,
        pid,
        "p2",
        "w.txt",
        content="three\n",
        expect_sha256=r1["content_sha256"],
    )
    assert r3["changed"] is True and r3["content_sha256"] != r1["content_sha256"]
    # dry_run validates without writing.
    r4 = WT.workspace_write_file(
        tok, pid, "p2", "w.txt", content="four\n", dry_run=True
    )
    assert r4.get("dry_run") is True and r4["changed"] is True
    assert WT.workspace_read_file(tok, pid, "p2", "w.txt")["content"] == "three"
    # Content mode normalizes to the file target (LF here).
    r5 = WT.workspace_write_file(tok, pid, "p2", "w.txt", content="a\r\nb\r\n")
    assert r5["changed"] is True
    assert WT.workspace_read_file(tok, pid, "p2", "w.txt")["content"] == "a\nb"
    # Edits mode echoes sha too.
    r6 = WT.workspace_write_file(
        tok, pid, "p2", "w.txt", edits=[{"find": "a\n", "replace": "A\n"}]
    )
    assert len(r6["content_sha256"]) == 64, r6
    print("  P2 write upgrades (sha/expect/dry-run/no-op/EOL): ok")


def test_ticket_entropy_and_peek(agents):
    import base64

    pid = _prop(agents, "theta", title="Entropy Xfer")
    tok = agents["theta"]["token"]
    db.claim_workspace(tok, pid, "entropy")
    m = db.mint_transfer_ticket(tok, pid, "entropy", ["e.txt"], "write")
    # 256-bit secret (>= the 128-bit bar), stored hashed only.
    raw = m["ticket"][len("xfer_") :]
    secret = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    assert len(secret) >= 16, len(secret)
    # Peek validates without consuming: twice, then the redeem still wins.
    db.peek_transfer_ticket(m["ticket"], "write", "e.txt")
    db.peek_transfer_ticket(m["ticket"], "write", "e.txt")
    t = db.redeem_transfer_ticket(m["ticket"], "write", "e.txt")
    assert t["used_paths"] == ["e.txt"], t
    print("  ticket entropy, hash storage, non-burning peek: ok")


def test_encoded_traversal_never_serves(agents):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    pid = _prop(agents, "eta", title="Traversal Xfer")
    tok = agents["eta"]["token"]
    _claim(agents, pid, "trav", who="eta")
    marker = b"traversal-canary-9q8w7e\n"
    WT.workspace_write_file(tok, pid, "trav", "safe.txt", content=marker.decode())
    t = TT.workspace_fetch_ticket(tok, pid, "trav", ["safe.txt"])
    app = Starlette(routes=TR.ROUTES)
    client = TestClient(app, raise_server_exceptions=False)
    # The legit download works end to end (proves the stack, not just handlers).
    good = client.get(t["files"][0]["url"])
    assert good.status_code == 200 and good.content == marker, good.status_code
    evil = [
        f"/transfer/{t['ticket']}/%2e%2e/%2e%2e/x.txt",
        f"/transfer/{t['ticket']}/..%2F..%2Fx.txt",
        f"/transfer/{t['ticket']}/%252e%252e/x.txt",
        f"/transfer/{t['ticket']}/.git/HEAD",
        f"/transfer/{t['ticket']}/.github/workflows/ci.yml",
        f"/transfer/{t['ticket']}/safe.txt/%2e%2e/x.txt",
    ]
    for url in evil:
        r = client.get(url)
        assert r.status_code != 200 or marker not in r.content, (url, r.status_code)
    print("  encoded traversal battery never serves bytes: ok")


def test_concurrent_redeem_burns_once(agents):
    import threading

    pid = _prop(agents, "theta", title="Race Xfer")
    tok = agents["theta"]["token"]
    db.claim_workspace(tok, pid, "race")
    w = db.mint_transfer_ticket(tok, pid, "race", ["r.txt"], "write")
    wins, losses = [], []

    def _try():
        try:
            db.redeem_transfer_ticket(w["ticket"], "write", "r.txt")
            wins.append(1)
        except Exception:
            losses.append(1)

    threads = [threading.Thread(target=_try) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    # BEGIN IMMEDIATE serializes check-then-burn: exactly one redeemer
    # sees the path unused, the rest lose. Without the lock both could win.
    assert len(wins) == 1 and len(losses) == 7, (wins, losses)
    print("  concurrent redeem burns exactly once: ok")


def test_protected_and_git_refused(agents):
    pid = _prop(agents, "theta", title="Guard Xfer")
    tok = agents["theta"]["token"]
    _claim(agents, pid, "guard", who="theta")
    # .github fails fast at mint for BOTH scopes (the data plane refuses
    # it on read and write alike). Guard errors surface as RepoError.
    for _scope_fn in (
        lambda: TT.workspace_fetch_ticket(
            tok, pid, "guard", [".github/workflows/ci.yml"]
        ),
        lambda: TT.workspace_upload_ticket(
            tok, pid, "guard", [".github/workflows/ci.yml"]
        ),
    ):
        try:
            _scope_fn()
        except Exception as exc:
            assert "protected" in str(exc), exc
        else:
            raise AssertionError("expected protected-path refusal at mint")
    # .git is refused by the engine guard itself (last line of defense).
    try:
        ws.read_transfer_bytes(agents["theta"]["agent_id"], pid, "guard", ".git/HEAD")
    except Exception as exc:
        assert "managed by the workspace" in str(exc), exc
    else:
        raise AssertionError("expected .git refusal from the engine guard")
    try:
        ws.apply_transfer_bytes(
            agents["theta"]["agent_id"], pid, "guard", ".git/config", b"x\n"
        )
    except Exception as exc:
        assert "managed by the workspace" in str(exc), exc
    else:
        raise AssertionError("expected .git refusal from the engine guard")
    print("  protected/.git refused at mint and engine: ok")


def test_terminal_tickets_prune(agents):
    pid = _prop(agents, "fresh", title="Prune Xfer")
    tok = agents["fresh"]["token"]
    db.claim_workspace(tok, pid, "prune")
    old = db.mint_transfer_ticket(tok, pid, "prune", ["o.txt"], "read")
    live = db.mint_transfer_ticket(tok, pid, "prune", ["l.txt"], "read")
    with db._conn() as conn:
        conn.execute(
            "UPDATE transfer_tickets SET status = 'used',"
            " used_at = '2000-01-01T00:00:00.000Z',"
            " created_at = '2000-01-01T00:00:00.000Z'"
            " WHERE ticket_hash = ?",
            (hashlib.sha256(old["ticket"].encode()).hexdigest(),),
        )
    db.sweep_expired_transfer_tickets()
    with db._conn() as conn:
        left = {
            r[0]
            for r in conn.execute(
                "SELECT ticket_hash FROM transfer_tickets WHERE agent_id = ?",
                (agents["fresh"]["agent_id"],),
            ).fetchall()
        }
    assert hashlib.sha256(old["ticket"].encode()).hexdigest() not in left
    assert hashlib.sha256(live["ticket"].encode()).hexdigest() in left
    print("  terminal tickets prune, live unused survive: ok")


def test_failed_upload_burns_nothing(agents):
    pid = _prop(agents, "eta", title="Noburn Xfer")
    tok = agents["eta"]["token"]
    _claim(agents, pid, "noburn", who="eta")
    WT.workspace_write_file(tok, pid, "noburn", "k.txt", content="keep\n")
    w = TT.workspace_upload_ticket(tok, pid, "noburn", ["k.txt", "j.txt"])
    # A pre-redeem refusal (size cap) burns nothing: not the failed path,
    # not its siblings. Both upload cleanly afterwards on the same ticket.
    big = b"z" * ((1 << 20) + 8)
    refused = _run(
        TR.transfer_upload(
            _req(
                "POST",
                w["ticket"],
                "k.txt",
                body=big,
                headers=[(b"content-length", str(len(big)).encode())],
            )
        )
    )
    assert refused.status_code == 413, (refused.status_code, refused.body)
    ok1 = _run(TR.transfer_upload(_req("POST", w["ticket"], "k.txt", body=b"v2\n")))
    assert ok1.status_code == 200, (ok1.status_code, ok1.body)
    ok2 = _run(TR.transfer_upload(_req("POST", w["ticket"], "j.txt", body=b"new\n")))
    assert ok2.status_code == 200, (ok2.status_code, ok2.body)
    print("  refused upload burns nothing (self or siblings): ok")


def test_upload_hits_per_write_budget(agents):
    import config as _cfg

    pid = _prop(agents, "fresh", title="Budget Xfer")
    tok = agents["fresh"]["token"]
    _claim(agents, pid, "budget", who="fresh")
    w = TT.workspace_upload_ticket(tok, pid, "budget", ["q.txt"])
    old_max = _cfg.WORKSPACE_CLAIM_MAX_MB
    _cfg.WORKSPACE_CLAIM_MAX_MB = 0.00001
    try:
        resp = _run(TR.transfer_upload(_req("POST", w["ticket"], "q.txt", body=b"x\n")))
    finally:
        _cfg.WORKSPACE_CLAIM_MAX_MB = old_max
    assert resp.status_code == 400, (resp.status_code, resp.body)
    assert "budget" in resp.body.decode(), resp.body
    print("  upload honors the per-write budget (no bypass): ok")


def test_validation_touches_clocks(agents):
    pid = _prop(agents, "fresh", title="Touch Xfer")
    tok = agents["fresh"]["token"]
    _claim(agents, pid, "touch", who="fresh")
    WT.workspace_write_file(tok, pid, "touch", "s.txt", content="touch\n")
    t = TT.workspace_fetch_ticket(tok, pid, "touch", ["s.txt"])
    sha = t["files"][0]["sha256"]

    def _backdate():
        with db._conn() as conn:
            conn.execute(
                "UPDATE workspace_claims SET updated_at = '2000-01-01T00:00:00.000Z'"
                " WHERE proposal_id = ? AND name = 'touch'",
                (pid,),
            )

    def _updated():
        with db._conn() as conn:
            return conn.execute(
                "SELECT updated_at FROM workspace_claims"
                " WHERE proposal_id = ? AND name = 'touch'",
                (pid,),
            ).fetchone()["updated_at"]

    # A 304 is still live use: the idle clock must advance past the mark.
    _backdate()
    r304 = _run(
        TR.transfer_download(
            _req(
                "GET",
                t["ticket"],
                "s.txt",
                headers=[(b"if-none-match", f'"{sha}"'.encode())],
            )
        )
    )
    assert r304.status_code == 304, (r304.status_code, r304.body)
    assert _updated() > "2000-01-01T00:00:00.000Z", _updated()
    # A quiet no-op upload touches too.
    w = TT.workspace_upload_ticket(tok, pid, "touch", ["s.txt"])
    _backdate()
    import json as _json

    rnoop = _run(
        TR.transfer_upload(_req("POST", w["ticket"], "s.txt", body=b"touch\n"))
    )
    assert rnoop.status_code == 200, (rnoop.status_code, rnoop.body)
    assert _json.loads(rnoop.body.decode())["changed"] is False
    assert _updated() > "2000-01-01T00:00:00.000Z", _updated()
    print("  304 + quiet no-op advance idle clocks: ok")


def test_engine_guard_battery():
    import github._workspaces as _eng

    dest = os.path.join("nonexistent-claim-dir")
    for bad in (
        "../x.txt",
        "a/../../x.txt",
        "/abs/x.txt",
        ".workspace.json",
        ".workspace.json.tmp",
        ".git/HEAD",
        ".git",
        ".github/workflows/ci.yml",
        "",
    ):
        try:
            _eng._guard_transfer_path(dest, bad)
        except Exception as exc:
            assert "managed by the workspace" in str(exc) or (
                "invalid path" in str(exc)
                or "relative" in str(exc)
                or "protected directory" in str(exc)
                or "empty" in str(exc)
            ), (bad, exc)
        else:
            raise AssertionError(f"expected guard refusal for {bad!r}")
    clean, _full = _eng._guard_transfer_path(dest, "ok/sub.txt")
    assert clean == "ok/sub.txt", clean
    print("  engine guard battery (traversal/abs/managed/git/protected): ok")


def test_failed_apply_unburns_path(agents):
    pid = _prop(agents, "gamma", title="Unburn Xfer")
    tok = agents["gamma"]["token"]
    _claim(agents, pid, "unburn", who="gamma")
    WT.workspace_write_file(tok, pid, "unburn", "u.txt", content="base\n")
    w = TT.workspace_upload_ticket(tok, pid, "unburn", ["u.txt"])
    # Non-UTF8 fails post-redeem (the path burns, then the apply fails).
    bad = _run(
        TR.transfer_upload(_req("POST", w["ticket"], "u.txt", body=b"\xff\xfe\n"))
    )
    assert bad.status_code == 400, (bad.status_code, bad.body)
    # The path is unburned: fixed bytes retry on the SAME ticket.
    good = _run(TR.transfer_upload(_req("POST", w["ticket"], "u.txt", body=b"fixed\n")))
    assert good.status_code == 200, (good.status_code, good.body)
    assert WT.workspace_read_file(tok, pid, "unburn", "u.txt")["content"] == "fixed"
    retry_ticket = TT.workspace_upload_ticket(tok, pid, "unburn", ["u.txt"])
    apply_entered = threading.Event()
    apply_release = threading.Event()
    unburned = threading.Event()
    unburn_calls = []
    original_unburn = db.unburn_transfer_path

    def failed_apply(*args, **kwargs):
        apply_entered.set()
        if not apply_release.wait(5):
            raise AssertionError("failed apply was not released")
        raise TR.RepoError("injected async apply failure")

    def observed_unburn(raw_ticket, path):
        unburn_calls.append((raw_ticket, path))
        result = original_unburn(raw_ticket, path)
        unburned.set()
        return result

    async def run_cancelled_apply():
        upload = asyncio.create_task(
            TR.transfer_upload(
                _req("POST", retry_ticket["ticket"], "u.txt", body=b"retry\n")
            )
        )
        assert await asyncio.to_thread(apply_entered.wait, 1)
        upload.cancel()
        try:
            await upload
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled upload did not stay cancelled")
        apply_release.set()
        assert await asyncio.to_thread(unburned.wait, 1)

    with (
        patch.object(ws, "apply_transfer_bytes", failed_apply),
        patch.object(db, "unburn_transfer_path", observed_unburn),
    ):
        asyncio.run(run_cancelled_apply())
    assert unburn_calls == [(retry_ticket["ticket"], "u.txt")], unburn_calls
    recovered = _run(
        TR.transfer_upload(
            _req("POST", retry_ticket["ticket"], "u.txt", body=b"retry\n")
        )
    )
    assert recovered.status_code == 200, (recovered.status_code, recovered.body)
    assert WT.workspace_read_file(tok, pid, "unburn", "u.txt")["content"] == "retry"
    print("  failed apply unburns its path (same-ticket retry): ok")


def test_crlf_roundtrip_keeps_target(agents):
    pid = _prop(agents, "beta", title="CRLF Xfer")
    tok = agents["beta"]["token"]
    info = _claim(agents, pid, "crlf", who="beta")
    dest = ws._claim_dir(agents["beta"]["agent_id"], pid, "crlf")
    with open(os.path.join(dest, "dos.txt"), "wb") as fh:
        fh.write(b"one\r\ntwo\r\n")
    assert info["exists"]
    t = TT.workspace_fetch_ticket(tok, pid, "crlf", ["dos.txt"])
    pin = t["files"][0]["sha256"]
    # An LF-edited upload against the CRLF pin succeeds: the pin checks
    # the pre-apply tree bytes, normalization targets the stored CRLF.
    w = TT.workspace_upload_ticket(tok, pid, "crlf", ["dos.txt"], {"dos.txt": pin})
    resp = _run(
        TR.transfer_upload(_req("POST", w["ticket"], "dos.txt", body=b"one\nTWO\n"))
    )
    assert resp.status_code == 200, (resp.status_code, resp.body)
    with open(os.path.join(dest, "dos.txt"), "rb") as fh:
        assert fh.read() == b"one\r\nTWO\r\n"
    print("  CRLF fetch pin + LF upload roundtrips to CRLF: ok")


def test_symlink_paths_refused(agents):
    pid = _prop(agents, "delta", title="Link Xfer")
    tok = agents["delta"]["token"]
    _claim(agents, pid, "link", who="delta")
    dest = ws._claim_dir(agents["delta"]["agent_id"], pid, "link")
    link = os.path.join(dest, "evil")
    try:
        os.symlink(os.path.join(dest, ".git", "HEAD"), link)
    except OSError:
        print("  symlink refusal skipped (no-symlink platform)")
        return
    # Engine (transfer data plane) refuses through-link reads and writes.
    for fn in (
        lambda: ws.read_transfer_bytes(
            agents["delta"]["agent_id"], pid, "link", "evil"
        ),
        lambda: ws.apply_transfer_bytes(
            agents["delta"]["agent_id"], pid, "link", "evil", b"x\n"
        ),
    ):
        try:
            fn()
        except Exception as exc:
            assert "symlink" in str(exc), exc
        else:
            raise AssertionError("expected symlink refusal from the engine guard")
    # MCP surface refuses the same shape on read and write.
    try:
        WT.workspace_read_file(tok, pid, "link", "evil")
    except Exception as exc:
        assert "symlink" in str(exc), exc
    else:
        raise AssertionError("expected symlink refusal from the MCP read guard")
    try:
        WT.workspace_write_file(tok, pid, "link", "evil", content="x\n")
    except Exception as exc:
        assert "symlink" in str(exc), exc
    else:
        raise AssertionError("expected symlink refusal from the MCP write guard")
    os.remove(link)
    print("  symlink-into-.git refused on engine + MCP read/write: ok")


def test_pin_values_validated_at_mint(agents):
    pid = _prop(agents, "zeta", title="Pinval Xfer")
    tok = agents["zeta"]["token"]
    db.claim_workspace(tok, pid, "pinval")
    assert "64-hex sha256" in expect_error(
        db.mint_transfer_ticket,
        tok,
        pid,
        "pinval",
        ["a.txt"],
        "write",
        {"a.txt": "not-a-hash"},
    )
    # Uppercase pins normalize: the upload against them succeeds.
    m = db.mint_transfer_ticket(
        tok, pid, "pinval", ["a.txt"], "write", {"a.txt": "A" * 64}
    )
    import json as _json

    with db._conn() as conn:
        stored = conn.execute(
            "SELECT expect_shas_json FROM transfer_tickets WHERE ticket_hash = ?",
            (hashlib.sha256(m["ticket"].encode()).hexdigest(),),
        ).fetchone()["expect_shas_json"]
    assert _json.loads(stored) == {"a.txt": "a" * 64}, stored
    print("  pin values validated + normalized at mint: ok")


def test_corrupt_row_fails_loud(agents):
    pid = _prop(agents, "beta", title="Corrupt Xfer")
    tok = agents["beta"]["token"]
    db.claim_workspace(tok, pid, "corrupt")
    m = db.mint_transfer_ticket(tok, pid, "corrupt", ["c.txt"], "read")
    with db._conn() as conn:
        conn.execute(
            "UPDATE transfer_tickets SET paths_json = 'not-json' WHERE ticket_hash = ?",
            (__import__("hashlib").sha256(m["ticket"].encode()).hexdigest(),),
        )
    try:
        db.redeem_transfer_ticket(m["ticket"], "read", "c.txt")
    except Exception as exc:
        assert "corrupt" in str(exc), exc
        assert getattr(exc, "detail", {}).get("http_status") == 500, exc
    else:
        raise AssertionError("expected loud failure on a corrupt ticket row")
    print("  corrupt ticket row fails loud (500, never silent-empty): ok")


def test_cap_floor_defaults(agents):
    import config as _cfg

    old = _cfg.TRANSFER_MAX_FILE_MB
    try:
        _cfg.TRANSFER_MAX_FILE_MB = 0
        assert ws._transfer_file_cap_bytes() == 1 << 20
        _cfg.TRANSFER_MAX_FILE_MB = -3
        assert ws._transfer_file_cap_bytes() == 1 << 20
    finally:
        _cfg.TRANSFER_MAX_FILE_MB = old
    print("  non-positive file cap falls back to 1MB: ok")


def test_download_filename_sanitized():
    assert TR._safe_download_filename("note.txt") == "note.txt"
    assert TR._safe_download_filename("a/b/c.txt") == "c.txt"
    assert "\r" not in TR._safe_download_filename("evil\r\nfile.txt")
    assert "\n" not in TR._safe_download_filename("evil\r\nfile.txt")
    assert '"' not in TR._safe_download_filename('qu"ote.txt')
    assert TR._safe_download_filename('"""') == "___"
    print("  download filename sanitized (CRLF/quotes/controls): ok")


def test_non_ascii_ticket_404s(agents):
    # A URL-decoded non-ASCII ticket can never be minted (token_urlsafe
    # is ASCII): it must 404 as unknown, never 500 into a bug report.
    try:
        db.redeem_transfer_ticket("xfer_\u00e9", "read", "a.txt")
    except Exception as exc:
        assert "unknown transfer ticket" in str(exc), exc
        assert getattr(exc, "detail", {}).get("http_status") == 404, exc
    else:
        raise AssertionError("expected 404 for a non-ASCII ticket")
    resp = _run(TR.transfer_download(_req("GET", "xfer_\u00e9", "a.txt")))
    assert resp.status_code == 404, (resp.status_code, resp.body)
    print("  non-ASCII ticket 404s (never 500s): ok")


def test_fetch_mint_enforces_cap(agents):
    pid = _prop(agents, "epsilon", title="Bigmint Xfer")
    tok = agents["epsilon"]["token"]
    _claim(agents, pid, "big", who="epsilon")
    dest = ws._claim_dir(agents["epsilon"]["agent_id"], pid, "big")
    with open(os.path.join(dest, "huge.bin"), "wb") as fh:
        fh.write(b"z" * ((1 << 20) + 8))
    # Over-cap files refuse at mint (they could never download): no
    # full read, no ticket row spent on an unusable path.
    assert "transfer cap" in expect_error(
        TT.workspace_fetch_ticket, tok, pid, "big", ["huge.bin"]
    )
    print("  fetch mint fails fast over the transfer cap: ok")


def test_content_write_directory_refused(agents):
    pid = _prop(agents, "epsilon", title="Dirwrite Xfer")
    tok = agents["epsilon"]["token"]
    _claim(agents, pid, "dirw", who="epsilon")
    dest = ws._claim_dir(agents["epsilon"]["agent_id"], pid, "dirw")
    os.makedirs(os.path.join(dest, "subdir"), exist_ok=True)
    try:
        WT.workspace_write_file(tok, pid, "dirw", "subdir", content="x\n")
    except Exception as exc:
        assert "is a directory" in str(exc), exc
    else:
        raise AssertionError("expected is-a-directory refusal")
    print("  content write to a directory names itself: ok")


def test_expect_shape_validated(agents):
    pid = _prop(agents, "gamma", title="Shape Xfer")
    tok = agents["gamma"]["token"]
    _claim(agents, pid, "shape", who="gamma")
    WT.workspace_write_file(tok, pid, "shape", "s.txt", content="v\n")
    try:
        WT.workspace_write_file(
            tok, pid, "shape", "s.txt", content="v2\n", expect_sha256="zzz"
        )
    except Exception as exc:
        assert "64-hex" in str(exc), exc
    else:
        raise AssertionError("expected shape refusal for a garbage pin")
    print("  garbage expect_sha256 misreports never (shape first): ok")


def test_mcp_noop_touches_clocks(agents):
    pid = _prop(agents, "zeta", title="Nooptouch Xfer")
    tok = agents["zeta"]["token"]
    _claim(agents, pid, "ntouch", who="zeta")
    WT.workspace_write_file(tok, pid, "ntouch", "n.txt", content="same\n")
    dest = ws._claim_dir(agents["zeta"]["agent_id"], pid, "ntouch")
    before_mtime = os.path.getmtime(os.path.join(dest, "n.txt"))
    with db._conn() as conn:
        conn.execute(
            "UPDATE workspace_claims SET updated_at = '2000-01-01T00:00:00.000Z'"
            " WHERE proposal_id = ? AND name = 'ntouch'",
            (pid,),
        )
    r = WT.workspace_write_file(tok, pid, "ntouch", "n.txt", content="same\n")
    assert r["changed"] is False, r
    assert os.path.getmtime(os.path.join(dest, "n.txt")) == before_mtime
    with db._conn() as conn:
        updated = conn.execute(
            "SELECT updated_at FROM workspace_claims"
            " WHERE proposal_id = ? AND name = 'ntouch'",
            (pid,),
        ).fetchone()["updated_at"]
    assert updated > "2000-01-01T00:00:00.000Z", updated
    print("  MCP quiet no-op advances clocks without writing: ok")


def test_public_base_url_parity():
    import config as _cfg

    old = _cfg.PUBLIC_BASE_URL
    try:
        _cfg.PUBLIC_BASE_URL = "https://forum.example/sub/"
        assert TT._transfer_base() == "https://forum.example/sub", TT._transfer_base()
        _cfg.PUBLIC_BASE_URL = ""
        assert TT._transfer_base().startswith("http://"), TT._transfer_base()
    finally:
        _cfg.PUBLIC_BASE_URL = old
    print("  PUBLIC_BASE_URL parity in transfer base: ok")


def test_legacy_db_migrates():
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS transfer_tickets")
    db.init_db()
    with db._conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "transfer_tickets" in tables
    print("  legacy DB gains transfer_tickets on init_db: ok")


def main():
    agents, _post_id = setup()
    test_table_exists()
    test_mint_redeem_roundtrip(agents)
    test_mint_gates(agents)
    test_expiry_and_sweep(agents)
    test_http_download(agents)
    test_http_upload_apply(agents)
    test_http_upload_lock_wait_keeps_event_loop_live(agents)
    test_http_upload_caps(agents)
    test_release_kills_ticket(agents)
    test_p2_write_upgrades(agents)
    test_concurrent_redeem_burns_once(agents)
    test_protected_and_git_refused(agents)
    test_terminal_tickets_prune(agents)
    test_ticket_entropy_and_peek(agents)
    test_encoded_traversal_never_serves(agents)
    test_failed_upload_burns_nothing(agents)
    test_upload_hits_per_write_budget(agents)
    test_validation_touches_clocks(agents)
    test_engine_guard_battery()
    test_public_base_url_parity()
    test_failed_apply_unburns_path(agents)
    test_crlf_roundtrip_keeps_target(agents)
    test_symlink_paths_refused(agents)
    test_pin_values_validated_at_mint(agents)
    test_corrupt_row_fails_loud(agents)
    test_cap_floor_defaults(agents)
    test_download_filename_sanitized()
    test_non_ascii_ticket_404s(agents)
    test_fetch_mint_enforces_cap(agents)
    test_content_write_directory_refused(agents)
    test_expect_shape_validated(agents)
    test_mcp_noop_touches_clocks(agents)
    test_legacy_db_migrates()
    _SB.close()
    print("test_workspace_transfer: all scenarios passed")


if __name__ == "__main__":
    main()
