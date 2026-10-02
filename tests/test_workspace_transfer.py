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


def _mint(tok, pid, name):
    """Claim through the ONE workspace tool (proposal #919) and return its
    claim-scoped read and write tickets. Claiming auto-mints both scopes,
    so a test never asks for a capability its claim already confers.
    Tickets pin NO path - the URL is composed per path with transfer_url,
    and the only path authority is the engine guard at redeem."""
    res = WT.workspace_claim(tok, "claim", pid, name)
    return res["read"]["ticket"], res["write"]["ticket"]


def _renew_write(tok, pid, name, expect_shas=None):
    """Re-mint both tickets on a claim already held; the write arm is what
    a burnt-path or expired-ticket caller reaches for. `expect_shas` pins
    the files an upload will overwrite and is proved satisfiable against
    the live tree at mint, so a moved file refuses the RENEW itself."""
    res = WT.workspace_claim(tok, "renew", pid, name, expect_shas=expect_shas)
    return res["write"]["ticket"]


def _sha256_of(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


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
    with db._conn() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(transfer_tickets)")}
    assert "claim_id" in columns, columns
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
            "SELECT ticket_hash, claim_id FROM transfer_tickets"
            " WHERE agent_id = ? AND scope = 'read'",
            (agents["alpha"]["agent_id"],),
        ).fetchone()
    assert minted["ticket"] not in row["ticket_hash"]
    assert row["ticket_hash"] == hashlib.sha256(minted["ticket"].encode()).hexdigest()
    assert row["claim_id"] is not None, row
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
    res = WT.workspace_claim(tok, "claim", pid, "dl")
    tree = res["tree"]
    assert res["claim"]["name"] == "dl", res["claim"]
    assert tree["name"] == "dl" and os.path.isdir(tree["path"]), tree
    WT.workspace_write_file(tok, pid, "dl", "note.txt", content="hello xfer\n")
    read, write = res["read"]["ticket"], res["write"]["ticket"]
    assert read.startswith("xfer_") and write.startswith("xfer_")
    assert read != write, "read and write are separate capabilities"
    # A claim-scoped ticket pins no path and carries no mint manifest at
    # all: the URL is composed per request and the sha comes back on the
    # download, never from a mint-time listing.
    assert "files" not in res
    assert set(res["read"]) == {"ticket", "scope", "expires_at"}, sorted(res["read"])
    assert res["read"]["scope"] == "read" and res["write"]["scope"] == "write"
    assert TT.transfer_url(read, "note.txt") == f"/transfer/{read}/note.txt"
    assert "base" in res and "expires_at" in res
    resp = _run(TR.transfer_download(_req("GET", read, "note.txt")))
    assert resp.status_code == 200, resp.status_code
    assert resp.body == b"hello xfer\n", resp.body
    sha = resp.headers["x-content-sha256"]
    assert len(sha) == 64, sha
    assert sha == hashlib.sha256(b"hello xfer\n").hexdigest(), sha
    assert resp.headers["etag"] == f'"{sha}"'
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "private, no-store"
    # Revalidation hits 304.
    resp304 = _run(
        TR.transfer_download(
            _req(
                "GET",
                read,
                "note.txt",
                headers=[(b"if-none-match", f'"{sha}"'.encode())],
            )
        )
    )
    assert resp304.status_code == 304, resp304.status_code
    # Unknown ticket 404s without leaking.
    resp404 = _run(TR.transfer_download(_req("GET", "xfer_nope123", "note.txt")))
    assert resp404.status_code == 404, resp404.status_code
    # A write ticket cannot download.
    resp400 = _run(TR.transfer_download(_req("GET", write, "note.txt")))
    assert resp400.status_code == 400, resp400.status_code
    # Tree-wide reach REPLACED the per-path ticket list: app.py used to be
    # refused for being outside the mint, and is now served, because the
    # claim-scoped ticket admits any engine-guarded path. The guard, not
    # ticket membership, is the boundary - .github/ is refused right here.
    reach = _run(TR.transfer_download(_req("GET", read, "app.py")))
    assert reach.status_code == 200, (reach.status_code, reach.body)
    assert reach.body == b"X = 1\n", reach.body
    guarded = _run(TR.transfer_download(_req("GET", read, ".github/workflows/ci.yml")))
    assert guarded.status_code == 400, (guarded.status_code, guarded.body)
    assert "protected" in guarded.body.decode(), guarded.body
    print("  HTTP download bytes/headers/304/refusals: ok")


def test_http_upload_apply(agents):
    pid = _prop(agents, "gamma", title="Upload Xfer")
    tok = agents["gamma"]["token"]
    WT.workspace_claim(tok, "claim", pid, "up")
    WT.workspace_write_file(tok, pid, "up", "app.py", content="X = 1\n")
    base_sha = WT.workspace_read_file(tok, pid, "up", "app.py")["content_sha256"]
    # The pin rides the RENEW, not the claim (a fresh tree has nothing to
    # overwrite), and the mint proves it against the live file first.
    w = _renew_write(tok, pid, "up", {"app.py": base_sha})
    body = b"X = 2\nY = 3\n"
    resp = _run(
        TR.transfer_upload(
            _req(
                "POST",
                w,
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
    resp2 = _run(TR.transfer_upload(_req("POST", w, "app.py", body=body)))
    assert resp2.status_code == 409, (resp2.status_code, resp2.body)
    # Identical bytes on a FRESH ticket are a quiet no-op.
    w2 = _renew_write(tok, pid, "up")
    resp3 = _run(TR.transfer_upload(_req("POST", w2, "app.py", body=body)))
    assert resp3.status_code == 200, (resp3.status_code, resp3.body)
    assert _json.loads(resp3.body.decode())["changed"] is False
    # A stale pin now refuses the MINT, which is strictly earlier than the
    # old per-ticket apply-time 409: no ticket exists at all, so a moved
    # file can never produce one that looks pinned but is not.
    assert "tree moved since you read it" in expect_error(
        WT.workspace_claim, tok, "renew", pid, "up", {"app.py": "0" * 64}
    )
    # A pin naming a file the tree does not have refuses too - otherwise it
    # would be a pin that can never fire, which is worse than no pin.
    assert "not a file in this workspace" in expect_error(
        WT.workspace_claim, tok, "renew", pid, "up", {"typo.py": "0" * 64}
    )
    assert WT.workspace_read_file(tok, pid, "up", "app.py")["content"] == "X = 2\nY = 3"
    # Non-UTF8 and empty uploads refuse.
    w4 = _renew_write(tok, pid, "up")
    resp5 = _run(TR.transfer_upload(_req("POST", w4, "bin.dat", body=b"\xff\xfe\n")))
    assert resp5.status_code == 400, (resp5.status_code, resp5.body)
    w5 = _renew_write(tok, pid, "up")
    resp6 = _run(TR.transfer_upload(_req("POST", w5, "empty.txt", body=b"")))
    assert resp6.status_code == 400, (resp6.status_code, resp6.body)
    print("  HTTP upload apply/receipt/pins/no-op/refusals: ok")


def test_http_upload_lock_wait_keeps_event_loop_live(agents):
    pid = _prop(agents, "beta", title="Async Upload Xfer")
    tok = agents["beta"]["token"]
    WT.workspace_claim(tok, "claim", pid, "asyncup")
    WT.workspace_write_file(tok, pid, "asyncup", "held.txt", content="base\n")
    pin = WT.workspace_read_file(tok, pid, "asyncup", "held.txt")["content_sha256"]
    ticket = _renew_write(tok, pid, "asyncup", {"held.txt": pin})
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
                TR.transfer_upload(_req("POST", ticket, "held.txt", body=b"next\n"))
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


def test_http_download_lock_wait_keeps_event_loop_live(agents):
    pid = _prop(agents, "beta", title="Async Download Xfer")
    tok = agents["beta"]["token"]
    WT.workspace_claim(tok, "claim", pid, "asyncdl")
    WT.workspace_write_file(tok, pid, "asyncdl", "held.txt", content="base\n")
    ticket = WT.workspace_claim(tok, "renew", pid, "asyncdl")["read"]["ticket"]
    dest = ws._claim_dir(agents["beta"]["agent_id"], pid, "asyncdl")
    lock_entered = threading.Event()
    read_entered = threading.Event()
    timing = {}
    original_read = ws.read_transfer_bytes

    def observed_read(*args, **kwargs):
        read_entered.set()
        return original_read(*args, **kwargs)

    def hold_lock():
        with ws.workspace_lock(dest):
            lock_entered.set()
            time.sleep(0.5)
            timing["released"] = time.monotonic()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert lock_entered.wait(5)
    try:

        async def run_download():
            download = asyncio.create_task(
                TR.transfer_download(_req("GET", ticket, "held.txt"))
            )
            while not read_entered.is_set():
                await asyncio.sleep(0)
            heartbeat_at = None

            async def heartbeat():
                nonlocal heartbeat_at
                await asyncio.sleep(0)
                heartbeat_at = time.monotonic()

            await heartbeat()
            return await download, heartbeat_at

        with patch.object(ws, "read_transfer_bytes", observed_read):
            response, heartbeat_at = asyncio.run(run_download())
    finally:
        holder.join(5)
    assert not holder.is_alive(), "workspace lock holder did not finish"
    assert heartbeat_at < timing["released"], (heartbeat_at, timing["released"])
    assert response.status_code == 200, (response.status_code, response.body)
    assert response.body == b"base\n", response.body
    print("  async download lock wait keeps the event loop live: ok")
    db.release_workspace(tok, pid, "asyncdl")


def test_http_upload_caps(agents):
    pid = _prop(agents, "delta", title="Cap Xfer")
    tok = agents["delta"]["token"]
    res = WT.workspace_claim(tok, "claim", pid, "cap")
    big = b"z" * ((1 << 20) + 8)
    w = res["write"]["ticket"]
    # Declared length over cap refuses before reading.
    resp = _run(
        TR.transfer_upload(
            _req(
                "POST",
                w,
                "big.txt",
                body=b"tiny",
                headers=[(b"content-length", str(len(big)).encode())],
            )
        )
    )
    assert resp.status_code == 413, (resp.status_code, resp.body)
    # Chunked without a length trips the bounded read instead. The ticket
    # is the same tree-wide capability either way - no re-mint needed.
    resp2 = _run(
        TR.transfer_upload(
            _req_chunked("POST", w, "big2.txt", [big[:700000], big[700000:]])
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
    t = WT.workspace_claim(tok, "claim", pid, "rel")["read"]["ticket"]
    db.release_workspace(tok, pid, "rel")
    resp = _run(TR.transfer_download(_req("GET", t, "README.md")))
    assert resp.status_code == 404, (resp.status_code, resp.body)
    time.sleep(0.001)
    _claim(agents, pid, "rel", who="epsilon")
    resp = _run(TR.transfer_download(_req("GET", t, "README.md")))
    assert resp.status_code == 404, (resp.status_code, resp.body)
    assert "gone" in resp.body.decode()
    with db._conn() as conn:
        conn.execute(
            "UPDATE transfer_tickets SET claim_id = NULL WHERE ticket_hash = ?",
            (hashlib.sha256(t.encode()).hexdigest(),),
        )
    legacy = _run(TR.transfer_download(_req("GET", t, "README.md")))
    assert legacy.status_code == 404, (legacy.status_code, legacy.body)
    assert "gone" in legacy.body.decode()
    db.release_workspace(tok, pid, "rel")
    print("  released claim kills tickets, including after reclaim: ok")


def test_reclaim_blocks_old_upload(agents):
    pid = _prop(agents, "eta", title="Reclaim Xfer")
    tok = agents["eta"]["token"]
    WT.workspace_claim(tok, "claim", pid, "reclaim")
    WT.workspace_write_file(tok, pid, "reclaim", "r.txt", content="before\n")
    pin = WT.workspace_read_file(tok, pid, "reclaim", "r.txt")["content_sha256"]
    old_claim = db.get_workspace(tok, pid, "reclaim")
    dest = ws._claim_dir(agents["eta"]["agent_id"], pid, "reclaim")
    ticket = _renew_write(tok, pid, "reclaim", {"r.txt": pin})
    old_apply = ws.apply_transfer_bytes

    def release_reclaim_then_apply(*args, **kwargs):
        # db-level, deliberately NOT workspace_claim: this runs INSIDE the
        # tree lock that apply_transfer_bytes holds, and every tool-layer
        # claim/release takes that same lock (flock is not reentrant), so
        # driving the tool here would deadlock instead of racing. The
        # interleaving under test is the claim row's, not the lock's.
        released = db.release_workspace(
            tok, pid, "reclaim", claim_id=int(old_claim["id"])
        )
        assert released["status"] == "released", released
        assert ws._retire_claim_tree_locked(dest), dest
        fresh_claim = db.claim_workspace(tok, pid, "reclaim")
        assert fresh_claim["id"] != old_claim["id"], fresh_claim
        ws.ensure_claim_tree(
            int(fresh_claim["agent_id"]),
            pid,
            "reclaim",
            claim_id=int(fresh_claim["id"]),
        )
        return old_apply(*args, **kwargs)

    with patch.object(ws, "apply_transfer_bytes", release_reclaim_then_apply):
        response = _run(
            TR.transfer_upload(_req("POST", ticket, "r.txt", body=b"after\n"))
        )
    assert response.status_code == 404, (response.status_code, response.body)
    assert "gone" in response.body.decode(), response.body
    current = db.get_workspace(tok, pid, "reclaim")
    assert current["id"] != old_claim["id"], current
    assert "could not read" in expect_error(
        WT.workspace_read_file, tok, pid, "reclaim", "r.txt"
    )
    retry = _run(TR.transfer_upload(_req("POST", ticket, "r.txt", body=b"after\n")))
    assert retry.status_code == 404, (retry.status_code, retry.body)
    db.release_workspace(tok, pid, "reclaim")
    print("  reclaim blocks an old redeemed upload without writing the new tree: ok")


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
    WT.workspace_claim(tok, "claim", pid, "trav")
    marker = b"traversal-canary-9q8w7e\n"
    WT.workspace_write_file(tok, pid, "trav", "safe.txt", content=marker.decode())
    ticket = WT.workspace_claim(tok, "renew", pid, "trav")["read"]["ticket"]
    app = Starlette(routes=TR.ROUTES)
    client = TestClient(app, raise_server_exceptions=False)
    # The legit download works end to end (proves the stack, not just handlers).
    good = client.get(TT.transfer_url(ticket, "safe.txt"))
    assert good.status_code == 200 and good.content == marker, good.status_code
    evil = [
        f"/transfer/{ticket}/%2e%2e/%2e%2e/x.txt",
        f"/transfer/{ticket}/..%2F..%2Fx.txt",
        f"/transfer/{ticket}/%252e%252e/x.txt",
        f"/transfer/{ticket}/.git/HEAD",
        f"/transfer/{ticket}/.github/workflows/ci.yml",
        f"/transfer/{ticket}/safe.txt/%2e%2e/x.txt",
    ]
    for url in evil:
        r = client.get(url)
        assert r.status_code != 200 or marker not in r.content, (url, r.status_code)
    # The ticket now reaches the WHOLE tree, so "not the canary" is no
    # longer strong enough for the guarded names: each is a hard 400 that
    # never runs the engine read, on both a claim-scoped read ticket and
    # (for .github) a rename-shaped path.
    for path in (".github/workflows/ci.yml", ".git/HEAD", ".workspace.json", ".git"):
        r = client.get(TT.transfer_url(ticket, path))
        assert r.status_code == 400, (path, r.status_code, r.text)
        assert marker.decode() not in r.text, (path, r.text)
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
    # INVERTED IN SPIRIT, not deleted: a claim-scoped ticket pins NO
    # path, so the mint never sees .github or .git and cannot refuse them.
    # The refusal moved DOWN to the data plane, where it must be exactly
    # as total - the engine guard under the tree lock is the only path
    # authority left, so that is what these arms now pin.
    res = WT.workspace_claim(tok, "claim", pid, "guard")
    read = res["read"]["ticket"]
    write = res["write"]["ticket"]
    assert "files" not in res, res
    # .github is refused on read AND on write, each through the guard.
    guarded_read = _run(
        TR.transfer_download(_req("GET", read, ".github/workflows/ci.yml"))
    )
    assert guarded_read.status_code == 400, (
        guarded_read.status_code,
        guarded_read.body,
    )
    assert "protected" in guarded_read.body.decode(), guarded_read.body
    up = _run(
        TR.transfer_upload(_req("POST", write, ".github/workflows/ci.yml", body=b"x\n"))
    )
    assert up.status_code == 400, (up.status_code, up.body)
    assert "protected" in up.body.decode(), up.body
    # A refused write burns nothing, so the same path stays refused
    # rather than turning into a one-shot.
    up2 = _run(
        TR.transfer_upload(_req("POST", write, ".github/workflows/ci.yml", body=b"y\n"))
    )
    assert up2.status_code == 400, (up2.status_code, up2.body)
    # The SCOPE gate is upstream of the path guard, and the two are not
    # redundant: a write ticket aimed at a guarded path is refused for its
    # scope and never names the path, so the guard is reached only by the
    # ticket that was actually issued for that direction.
    wrong = _run(TR.transfer_download(_req("GET", write, ".github/workflows/ci.yml")))
    assert wrong.status_code == 400, (wrong.status_code, wrong.body)
    assert "write-only" in wrong.body.decode(), wrong.body
    assert "protected" not in wrong.body.decode(), wrong.body
    # .git is refused by the engine guard itself (last line of defense),
    # through the data plane and through the engine directly.
    got = _run(TR.transfer_download(_req("GET", read, ".git/HEAD")))
    assert got.status_code == 400, (got.status_code, got.body)
    assert "managed by the workspace" in got.body.decode(), got.body
    ugit = _run(TR.transfer_upload(_req("POST", write, ".git/config", body=b"x\n")))
    assert ugit.status_code == 400, (ugit.status_code, ugit.body)
    assert "managed by the workspace" in ugit.body.decode(), ugit.body
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
    print("  protected/.git refused at redeem (guard) and engine: ok")


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
    WT.workspace_claim(tok, "claim", pid, "noburn")
    WT.workspace_write_file(tok, pid, "noburn", "k.txt", content="keep\n")
    # One claim-scoped write ticket carries the whole tree and never
    # auto-completes to 'used', so its two paths burn independently - the
    # sibling guarantee this test exists for, now under the new lifetime.
    w = WT.workspace_claim(tok, "renew", pid, "noburn")["write"]["ticket"]
    # A pre-redeem refusal (size cap) burns nothing: not the failed path,
    # not its siblings. Both upload cleanly afterwards on the same ticket.
    big = b"z" * ((1 << 20) + 8)
    refused = _run(
        TR.transfer_upload(
            _req(
                "POST",
                w,
                "k.txt",
                body=big,
                headers=[(b"content-length", str(len(big)).encode())],
            )
        )
    )
    assert refused.status_code == 413, (refused.status_code, refused.body)
    ok1 = _run(TR.transfer_upload(_req("POST", w, "k.txt", body=b"v2\n")))
    assert ok1.status_code == 200, (ok1.status_code, ok1.body)
    ok2 = _run(TR.transfer_upload(_req("POST", w, "j.txt", body=b"new\n")))
    assert ok2.status_code == 200, (ok2.status_code, ok2.body)
    print("  refused upload burns nothing (self or siblings): ok")


def test_upload_hits_per_write_budget(agents):
    import config as _cfg

    pid = _prop(agents, "fresh", title="Budget Xfer")
    tok = agents["fresh"]["token"]
    w = WT.workspace_claim(tok, "claim", pid, "budget")["write"]["ticket"]
    old_max = _cfg.WORKSPACE_CLAIM_MAX_MB
    _cfg.WORKSPACE_CLAIM_MAX_MB = 0.00001
    try:
        resp = _run(TR.transfer_upload(_req("POST", w, "q.txt", body=b"x\n")))
    finally:
        _cfg.WORKSPACE_CLAIM_MAX_MB = old_max
    assert resp.status_code == 400, (resp.status_code, resp.body)
    assert "budget" in resp.body.decode(), resp.body
    print("  upload honors the per-write budget (no bypass): ok")


def test_validation_touches_clocks(agents):
    pid = _prop(agents, "fresh", title="Touch Xfer")
    tok = agents["fresh"]["token"]
    WT.workspace_claim(tok, "claim", pid, "touch")
    WT.workspace_write_file(tok, pid, "touch", "s.txt", content="touch\n")
    ticket = WT.workspace_claim(tok, "renew", pid, "touch")["read"]["ticket"]
    # There is no mint manifest any more: the sha a client pins comes off a
    # real download's X-Content-Sha256 header, so this arm proves both the
    # header and that it matches the bytes on disk.
    probe = _run(TR.transfer_download(_req("GET", ticket, "s.txt")))
    assert probe.status_code == 200, (probe.status_code, probe.body)
    sha = probe.headers["x-content-sha256"]
    assert sha == hashlib.sha256(b"touch\n").hexdigest(), sha

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
                ticket,
                "s.txt",
                headers=[(b"if-none-match", f'"{sha}"'.encode())],
            )
        )
    )
    assert r304.status_code == 304, (r304.status_code, r304.body)
    assert _updated() > "2000-01-01T00:00:00.000Z", _updated()
    # A quiet no-op upload touches too.
    w = WT.workspace_claim(tok, "renew", pid, "touch")["write"]["ticket"]
    _backdate()
    import json as _json

    rnoop = _run(TR.transfer_upload(_req("POST", w, "s.txt", body=b"touch\n")))
    assert rnoop.status_code == 200, (rnoop.status_code, rnoop.body)
    assert _json.loads(rnoop.body.decode())["changed"] is False
    assert _updated() > "2000-01-01T00:00:00.000Z", _updated()
    print("  304 + quiet no-op advance idle clocks: ok")


def test_transfer_touch_runs_under_tree_lock(agents):
    pid = _prop(agents, "fresh", title="Locked Touch Xfer")
    tok = agents["fresh"]["token"]
    with db._conn() as conn:
        held = conn.execute(
            "SELECT id, proposal_id, name FROM workspace_claims"
            " WHERE agent_id = ? AND status = 'active'",
            (agents["fresh"]["agent_id"],),
        ).fetchall()
    for row in held:
        db.release_workspace(
            tok,
            int(row["proposal_id"]),
            str(row["name"]),
            claim_id=int(row["id"]),
        )
    read_ticket, write_ticket = _mint(tok, pid, "lockedtouch")
    WT.workspace_write_file(tok, pid, "lockedtouch", "s.txt", content="touch\n")
    dest = ws._claim_dir(agents["fresh"]["agent_id"], pid, "lockedtouch")
    original_touch = TR._touch_best_effort
    modes = []

    def assert_touch_under_lock(response_factory, mode):
        acquired = threading.Event()
        contenders = []
        blocked_results = []

        def observed_touch(*args, **kwargs):
            modes.append(mode)

            def contend():
                with ws.workspace_lock(dest):
                    acquired.set()

            contender = threading.Thread(target=contend)
            contenders.append(contender)
            contender.start()
            try:
                blocked_results.append(not acquired.wait(0.2))
            finally:
                original_touch(*args, **kwargs)

        with patch.object(TR, "_touch_best_effort", observed_touch):
            response = response_factory()
        for contender in contenders:
            contender.join(2)
        assert blocked_results == [True], blocked_results
        assert all(not contender.is_alive() for contender in contenders)
        return response

    read_response = assert_touch_under_lock(
        lambda: _run(TR.transfer_download(_req("GET", read_ticket, "s.txt"))),
        "read",
    )
    assert read_response.status_code == 200, (
        read_response.status_code,
        read_response.body,
    )

    upload_response = assert_touch_under_lock(
        lambda: _run(
            TR.transfer_upload(_req("POST", write_ticket, "s.txt", body=b"touch\n"))
        ),
        "apply",
    )
    assert upload_response.status_code == 200, (
        upload_response.status_code,
        upload_response.body,
    )
    assert modes == ["read", "apply"], modes
    db.release_workspace(tok, pid, "lockedtouch")
    print("  transfer clock touches run under the tree lock: ok")


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
    WT.workspace_claim(tok, "claim", pid, "unburn")
    WT.workspace_write_file(tok, pid, "unburn", "u.txt", content="base\n")
    w = WT.workspace_claim(tok, "renew", pid, "unburn")["write"]["ticket"]
    # Non-UTF8 fails post-redeem (the path burns, then the apply fails).
    bad = _run(TR.transfer_upload(_req("POST", w, "u.txt", body=b"\xff\xfe\n")))
    assert bad.status_code == 400, (bad.status_code, bad.body)
    # The path is unburned: fixed bytes retry on the SAME ticket.
    good = _run(TR.transfer_upload(_req("POST", w, "u.txt", body=b"fixed\n")))
    assert good.status_code == 200, (good.status_code, good.body)
    assert WT.workspace_read_file(tok, pid, "unburn", "u.txt")["content"] == "fixed"
    retry_ticket = WT.workspace_claim(tok, "renew", pid, "unburn")["write"]["ticket"]
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
            TR.transfer_upload(_req("POST", retry_ticket, "u.txt", body=b"retry\n"))
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
    assert unburn_calls == [(retry_ticket, "u.txt")], unburn_calls
    recovered = _run(
        TR.transfer_upload(_req("POST", retry_ticket, "u.txt", body=b"retry\n"))
    )
    assert recovered.status_code == 200, (recovered.status_code, recovered.body)
    assert WT.workspace_read_file(tok, pid, "unburn", "u.txt")["content"] == "retry"
    print("  failed apply unburns its path (same-ticket retry): ok")


def test_crlf_roundtrip_keeps_target(agents):
    pid = _prop(agents, "beta", title="CRLF Xfer")
    tok = agents["beta"]["token"]
    res = WT.workspace_claim(tok, "claim", pid, "crlf")
    tree = res["tree"]
    assert tree["name"] == "crlf" and os.path.isdir(tree["path"]), tree
    dest = ws._claim_dir(agents["beta"]["agent_id"], pid, "crlf")
    with open(os.path.join(dest, "dos.txt"), "wb") as fh:
        fh.write(b"one\r\ntwo\r\n")
    # The pin is the DOWNLOAD's sha over the raw CRLF bytes - the mint
    # manifest this used to read is gone, and a client that cannot compute
    # the sha from a download has nothing honest to pin.
    probe = _run(TR.transfer_download(_req("GET", res["read"]["ticket"], "dos.txt")))
    assert probe.status_code == 200, (probe.status_code, probe.body)
    pin = probe.headers["x-content-sha256"]
    assert pin == hashlib.sha256(b"one\r\ntwo\r\n").hexdigest(), pin
    # An LF-edited upload against the CRLF pin succeeds: the pin checks
    # the pre-apply tree bytes, normalization targets the stored CRLF.
    w = _renew_write(tok, pid, "crlf", {"dos.txt": pin})
    resp = _run(TR.transfer_upload(_req("POST", w, "dos.txt", body=b"one\nTWO\n")))
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


def test_over_cap_file_refused_at_redeem_and_pin(agents):
    pid = _prop(agents, "epsilon", title="Bigmint Xfer")
    tok = agents["epsilon"]["token"]
    res = WT.workspace_claim(tok, "claim", pid, "big")
    dest = ws._claim_dir(agents["epsilon"]["agent_id"], pid, "big")
    with open(os.path.join(dest, "huge.bin"), "wb") as fh:
        fh.write(b"z" * ((1 << 20) + 8))
    # The claim-scoped mint cannot know the path, so it never refuses -
    # the over-cap file is stopped at the data plane instead, before any
    # full read: same cap, same wording, one layer later.
    got = _run(TR.transfer_download(_req("GET", res["read"]["ticket"], "huge.bin")))
    assert got.status_code == 413, (got.status_code, got.body)
    assert "transfer cap" in got.body.decode(), got.body
    # And the invariant survives where a caller could lean on it: a pin
    # naming an over-cap file is refused at the RENEW, because such a file
    # could never upload either.
    assert "transfer cap" in expect_error(
        WT.workspace_claim, tok, "renew", pid, "big", {"huge.bin": "0" * 64}
    )
    print("  over-cap file refused at download and at a renew pin: ok")


def test_pin_refuses_file_that_is_not_there(agents):
    """A pin guards an OVERWRITE, so a path the tree does not hold can
    never be one. Without this arm getsize raises OSError and the caller
    reads "could not read ... in the workspace" - a different sentence for
    the same refusal, which is how a pin check rots into a mystery."""
    pid = _prop(agents, "alpha", title="Pinabsent Xfer")
    tok = agents["alpha"]["token"]
    _claim(agents, pid, "pinabsent", who="alpha")
    dest = ws._claim_dir(agents["alpha"]["agent_id"], pid, "pinabsent")
    assert not os.path.exists(os.path.join(dest, "absent.txt")), "precondition"
    msg = expect_error(
        WT.workspace_claim, tok, "renew", pid, "pinabsent", {"absent.txt": "0" * 64}
    )
    assert "not a file in this workspace" in msg, msg
    # The refusal is about the MISSING file, not about pins being broken:
    # a pin on a real file at its real sha renews on the same claim.
    WT.workspace_write_file(tok, pid, "pinabsent", "present.txt", content="v\n")
    want = _sha256_of(os.path.join(dest, "present.txt"))
    res = WT.workspace_claim(tok, "renew", pid, "pinabsent", {"present.txt": want})
    assert res["write"]["ticket"], res
    WT.workspace_claim(tok, "release", pid, "pinabsent")
    print("  pin naming a file the tree lacks is refused at the mint: ok")


def test_pin_refuses_a_tree_that_moved_under_it(agents):
    """The whole point of a pin: the tree may not have moved between the
    fetch the sha came from and the upload that would overwrite it."""
    pid = _prop(agents, "beta", title="Pinmoved Xfer")
    tok = agents["beta"]["token"]
    _claim(agents, pid, "pinmoved", who="beta")
    dest = ws._claim_dir(agents["beta"]["agent_id"], pid, "pinmoved")
    WT.workspace_write_file(tok, pid, "pinmoved", "r.txt", content="v1\n")
    fetched = _sha256_of(os.path.join(dest, "r.txt"))
    WT.workspace_write_file(tok, pid, "pinmoved", "r.txt", content="v2\n")
    assert _sha256_of(os.path.join(dest, "r.txt")) != fetched, "precondition: moved"
    msg = expect_error(
        WT.workspace_claim, tok, "renew", pid, "pinmoved", {"r.txt": fetched}
    )
    assert "tree moved since you read it" in msg, msg
    # Positive control on the same claim: the sha the tree ACTUALLY holds
    # renews, so the refusal above is the move and nothing else.
    now = _sha256_of(os.path.join(dest, "r.txt"))
    res = WT.workspace_claim(tok, "renew", pid, "pinmoved", {"r.txt": now})
    assert res["write"]["ticket"], res
    WT.workspace_claim(tok, "release", pid, "pinmoved")
    print("  pin on a file that moved under the tree is refused: ok")


def test_pins_refused_on_claim_and_mint_nothing(agents):
    """action='claim' refuses pins BEFORE the claim is taken, so a refused
    call must leave neither a ticket nor a claim row. The ticket half is
    the load-bearing one - a capability minted for a call that refused is
    the one an agent cannot see it was refused for."""
    pid = _prop(agents, "gamma", title="PinsOnClaim Xfer")
    tok = agents["gamma"]["token"]
    with db._conn() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM transfer_tickets WHERE proposal_id = ?", (pid,)
        ).fetchone()[0]
    assert before == 0, f"precondition: fresh proposal, got {before} ticket rows"
    msg = expect_error(
        WT.workspace_claim, tok, "claim", pid, "pinclaim", {"a.txt": "0" * 64}
    )
    assert "applies to action='renew'" in msg, msg
    with db._conn() as conn:
        after = conn.execute(
            "SELECT COUNT(*) FROM transfer_tickets WHERE proposal_id = ?", (pid,)
        ).fetchone()[0]
        claims = conn.execute(
            "SELECT COUNT(*) FROM workspace_claims"
            " WHERE proposal_id = ? AND name = 'pinclaim' AND status = 'active'",
            (pid,),
        ).fetchone()[0]
    assert after == before, f"a refused claim minted {after - before} ticket(s)"
    assert claims == 0, "a refused claim left an active claim row behind"
    # And the ordinary claim still works for this citizen, so the refusal
    # above is about the pins and not about standing.
    res = WT.workspace_claim(tok, "claim", pid, "pinclaim")
    assert res["read"]["ticket"] and res["write"]["ticket"], res
    WT.workspace_claim(tok, "release", pid, "pinclaim")
    print("  pins refused on action='claim' mint no ticket and no claim: ok")


def test_pin_stored_under_the_validated_path(agents):
    """A pin key differing from its path only by padding is guard-clean
    and hash-correct, so the mint must STORE it under the cleaned path.

    The redeem layer looks a request up by the URL segment it was handed
    (server/_transfer.py), so a stored ' x.py' can never match a request
    for 'x.py'. That is a dead pin - hash-verified once at mint, then
    incapable of ever firing - which is the exact failure _check_pins
    exists to prevent, and the reason the key it returns must be `clean`.
    """
    import json as _json

    pid = _prop(agents, "delta", title="Pinkey Xfer")
    tok = agents["delta"]["token"]
    _claim(agents, pid, "pinkey", who="delta")
    WT.workspace_write_file(tok, pid, "pinkey", "x.py", content="X = 2\n")
    dest = ws._claim_dir(agents["delta"]["agent_id"], pid, "pinkey")
    want = _sha256_of(os.path.join(dest, "x.py"))
    res = WT.workspace_claim(tok, "renew", pid, "pinkey", {" x.py": want})
    ticket = res["write"]["ticket"]
    with db._conn() as conn:
        stored = conn.execute(
            "SELECT expect_shas_json FROM transfer_tickets WHERE ticket_hash = ?",
            (hashlib.sha256(ticket.encode()).hexdigest(),),
        ).fetchone()["expect_shas_json"]
    assert _json.loads(stored) == {"x.py": want}, stored
    # The relocated pin is LIVE, not merely moved: move the file under it
    # and upload the bytes it was taken against. A dead pin skips the
    # check entirely and quietly applies them.
    WT.workspace_write_file(tok, pid, "pinkey", "x.py", content="X = 9\n")
    bad = _run(TR.transfer_upload(_req("POST", ticket, "x.py", body=b"X = 2\n")))
    assert bad.status_code == 409, (bad.status_code, bad.body)
    assert "stale base for" in bad.body.decode(), bad.body
    with open(os.path.join(dest, "x.py"), "rb") as fh:
        assert b"X = 2" not in fh.read(), "the refused upload still landed"
    # Positive control on a fresh ticket: the pin accepts the bytes it
    # names, so the refusal above is the pin firing and nothing else.
    now = _sha256_of(os.path.join(dest, "x.py"))
    ticket2 = WT.workspace_claim(
        tok, "renew", pid, "pinkey", {" x.py": now}
    )["write"]["ticket"]
    ok = _run(TR.transfer_upload(_req("POST", ticket2, "x.py", body=b"X = 9\n")))
    assert ok.status_code == 200, (ok.status_code, ok.body)
    WT.workspace_claim(tok, "release", pid, "pinkey")
    print("  a padded pin key is stored - and enforced - under the clean path: ok")


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
    with db._conn() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(transfer_tickets)")}
    assert "claim_id" in columns, columns
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
    test_http_download_lock_wait_keeps_event_loop_live(agents)
    test_http_upload_caps(agents)
    test_release_kills_ticket(agents)
    test_reclaim_blocks_old_upload(agents)
    test_p2_write_upgrades(agents)
    test_concurrent_redeem_burns_once(agents)
    test_protected_and_git_refused(agents)
    test_terminal_tickets_prune(agents)
    test_ticket_entropy_and_peek(agents)
    test_encoded_traversal_never_serves(agents)
    test_failed_upload_burns_nothing(agents)
    test_upload_hits_per_write_budget(agents)
    test_validation_touches_clocks(agents)
    test_transfer_touch_runs_under_tree_lock(agents)
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
    test_over_cap_file_refused_at_redeem_and_pin(agents)
    test_pin_refuses_file_that_is_not_there(agents)
    test_pin_refuses_a_tree_that_moved_under_it(agents)
    test_pins_refused_on_claim_and_mint_nothing(agents)
    test_pin_stored_under_the_validated_path(agents)
    test_content_write_directory_refused(agents)
    test_expect_shape_validated(agents)
    test_mcp_noop_touches_clocks(agents)
    test_legacy_db_migrates()
    _SB.close()
    print("test_workspace_transfer: all scenarios passed")


if __name__ == "__main__":
    main()
