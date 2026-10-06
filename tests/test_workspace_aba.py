"""Focused workspace ABA regressions for the mutator-lock PR."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_aba_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db._workspace_claims as claim_db  # noqa: E402
import github._gitops as gh  # noqa: E402
import github._workspaces as ws  # noqa: E402
import server._transfer as transfer  # noqa: E402
import server.tools.repo._workspace as workspace_tools  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _mk_bare(tmp):
    bare = os.path.join(tmp, "remote.git")
    seed = os.path.join(tmp, "seed")
    os.makedirs(seed)
    _git("init", "--bare", "-b", "main", bare)
    _git("init", "-b", "main", cwd=seed)
    Path(seed, "README.md").write_text("seed\n", encoding="utf-8")
    _git("-C", seed, "add", "-A")
    _git(
        "-C",
        seed,
        "-c",
        "user.email=a@b",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        "seed",
    )
    _git("-C", seed, "push", bare, "main")
    return bare


_SHARED_BARE = _mk_bare(tempfile.mkdtemp(prefix="agentland_workspace_aba_remote_"))


class _Sandbox:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="agentland_workspace_aba_test_")
        self.orig_claims = ws._claims_root
        self.orig_repo = ws._repo_url
        self.orig_git_repo = gh._repo_url
        ws._claims_root = lambda: os.path.join(self.tmp, "claims")
        ws._repo_url = lambda with_token=False: _SHARED_BARE
        gh._repo_url = lambda with_token=False: _SHARED_BARE

    def close(self):
        ws._claims_root = self.orig_claims
        ws._repo_url = self.orig_repo
        gh._repo_url = self.orig_git_repo
        shutil.rmtree(self.tmp, ignore_errors=True)


def _request(ticket, path):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/transfer/{ticket}/{path}",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "path_params": {"ticket": ticket, "fpath": path},
        "state": {},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    from starlette.requests import Request

    return Request(scope, receive)


def test_stale_release_cas(agents):
    tok = agents["alpha"]["token"]
    pid = db.create_proposal(tok, "ABA Release", "body")["post_id"]
    old = db.claim_workspace(tok, pid, "dev")
    assert "id" in old, old
    db.release_workspace(tok, pid, "dev", claim_id=old["id"])
    fresh = db.claim_workspace(tok, pid, "dev")
    assert fresh["id"] != old["id"], (old, fresh)
    assert "changed" in expect_error(db.release_workspace, tok, pid, "dev", old["id"])
    assert db.get_workspace(tok, pid, "dev")["id"] == fresh["id"]
    db.release_workspace(tok, pid, "dev", claim_id=fresh["id"])


def test_claim_workspace_rechecks_after_lock(agents):
    sb = _Sandbox()
    try:
        tok = agents["alpha"]["token"]
        pid = db.create_proposal(tok, "Claim ABA", "body")["post_id"]
        replacement = {}
        lock_factory = workspace_tools.workspace_lock

        @contextmanager
        def raced_lock(path, *, allow_missing=False):
            with lock_factory(path, allow_missing=allow_missing):
                current = db.get_workspace(tok, pid, "dev")
                db.release_workspace(tok, pid, "dev", claim_id=current["id"])
                replacement["claim"] = db.claim_workspace(tok, pid, "dev")
                yield

        outcome = []

        def claim():
            try:
                outcome.append(
                    workspace_tools.workspace_claim(tok, "claim", pid, "dev")
                )
            except BaseException as exc:
                outcome.append(exc)

        with patch.object(workspace_tools, "workspace_lock", raced_lock):
            worker = threading.Thread(target=claim)
            worker.start()
            worker.join(2)
        assert not worker.is_alive(), "claim did not finish"
        assert len(outcome) == 1, outcome
        assert isinstance(outcome[0], db.ForumError), outcome
        assert "changed while waiting" in str(outcome[0]), outcome
        current = db.get_workspace(tok, pid, "dev")
        assert current["id"] == replacement["claim"]["id"], (current, replacement)
        workspace_tools.workspace_claim(tok, "release", pid, "dev")
    finally:
        sb.close()


def test_lifecycle_reclaim_reuses_path_lock(agents):
    sb = _Sandbox()
    try:
        tok = agents["alpha"]["token"]
        pid = db.create_proposal(tok, "Lifecycle Path Lock", "body")["post_id"]
        claimed = workspace_tools.workspace_claim(tok, "claim", pid, "dev")
        old_id = int(claimed["claim"]["id"])
        result = []

        @claim_db._with_workspace_claim_locks
        def lifecycle(token, post_id):
            current = db.get_workspace(token, post_id, "dev")
            db.release_workspace(token, post_id, "dev", claim_id=current["id"])
            replacement = db.claim_workspace(token, post_id, "dev")
            assert replacement["id"] != old_id, (old_id, replacement)
            ws.ensure_claim_tree(
                int(replacement["agent_id"]),
                post_id,
                str(replacement["name"]),
                claim_id=int(replacement["id"]),
            )
            with db._conn() as conn:
                result.append(claim_db.release_workspaces_for_proposal(conn, post_id))

        worker = threading.Thread(target=lambda: lifecycle(tok, pid), daemon=True)
        worker.start()
        worker.join(2)
        assert not worker.is_alive(), "lifecycle path lock recursively deadlocked"
        assert result == [1], result
        assert "no active workspace" in expect_error(db.get_workspace, tok, pid, "dev")
    finally:
        sb.close()


def test_renew_mint_rebinds_claim_under_lock(agents):
    """The mint-under-the-tree-lock ABA fence, re-pinned on its new home.

    The old pin drove ``workspace_fetch_ticket`` and asserted
    ForumError("changed while waiting for its lock"). That exact REFUSAL
    arm no longer lives in the tool layer - ``_do_renew`` resolves the
    claim BEFORE acquiring (owner-scoped, so always the caller's own) and
    never re-checks it after - but the refusal itself SURVIVES one layer
    down, re-worded: ``db.mint_transfer_ticket`` re-resolves the claim and
    refuses a released one with "no active workspace ... of yours ...
    tickets mint on live owned claims only."
    ``test_renew_on_released_claim_refuses`` pins that arm, so nothing in
    this file rests on the refusal being gone. What the renew arm does
    instead of re-checking is stamp the ticket from the claim that is LIVE
    AT MINT, under the lock - so the property to pin here is the stronger
    one, and it is the one that matters: a ticket can never be issued
    against a claim that has already been released.

    Two arms, both real: the mint cannot complete while another holder owns
    the tree lock (the discipline half), and the ticket it hands back is
    bound to the replacement, never to the released record (the safety
    half, which a stale pre-lock record would have failed).
    """
    sb = _Sandbox()
    try:
        tok = agents["beta"]["token"]
        pid = db.create_proposal(tok, "Renew ABA", "body")["post_id"]
        claimed = workspace_tools.workspace_claim(tok, "claim", pid, "mint")
        old_id = int(claimed["claim"]["id"])
        dest = str(claimed["tree"]["path"])
        lock_factory = workspace_tools.workspace_lock
        acquire_attempted = threading.Event()
        minted = threading.Event()
        outcome = []

        @contextmanager
        def observed_lock(path, *, allow_missing=False):
            acquire_attempted.set()
            with lock_factory(path, allow_missing=allow_missing):
                yield

        def renew():
            try:
                outcome.append(
                    workspace_tools.workspace_claim(tok, "renew", pid, "mint")
                )
            except BaseException as exc:
                outcome.append(exc)
            finally:
                minted.set()

        with lock_factory(dest):
            with patch.object(workspace_tools, "workspace_lock", observed_lock):
                worker = threading.Thread(target=renew)
                worker.start()
                assert acquire_attempted.wait(1)
                assert not minted.wait(0.2), (
                    "the mint completed while another holder owned the tree lock"
                )
                replacement, marker = _replace_claim_under_lock(
                    tok, pid, "mint", dest, old_id
                )
        worker.join(5)
        assert not worker.is_alive(), "queued ticket mint did not finish"
        assert len(outcome) == 1, outcome
        res = outcome[0]
        assert not isinstance(res, BaseException), (
            f"renew after a mid-wait replacement: {type(res).__name__}: {res}"
        )
        # Both scopes came back, and each is stamped with the claim that
        # held the tree when it was minted.
        fresh_id = int(replacement["id"])
        digests = [
            hashlib.sha256(res[scope]["ticket"].encode()).hexdigest()
            for scope in ("read", "write")
        ]
        with db._conn() as conn:
            stamped = conn.execute(
                "SELECT scope, claim_id FROM transfer_tickets"
                " WHERE ticket_hash IN (?, ?)",
                digests,
            ).fetchall()
        assert len(stamped) == 2, stamped
        assert {row["scope"]: int(row["claim_id"]) for row in stamped} == {
            "read": fresh_id,
            "write": fresh_id,
        }, (stamped, old_id, fresh_id)
        assert marker.read_text(encoding="utf-8") == "replacement\n"
        workspace_tools.workspace_claim(tok, "release", pid, "mint")
    finally:
        sb.close()


def test_renew_on_released_claim_refuses(agents):
    """The arm this file's sibling test above used to claim was gone.

    A claim replacement that RE-CLAIMS leaves a live owned claim, so the
    mint succeeds and the ticket is stamped with the fresh id - that is
    ``test_renew_mint_rebinds_claim_under_lock`` above. A release with no
    replacement leaves nothing for ``db.mint_transfer_ticket`` to resolve,
    and the refusal still fires - one layer down, carrying the db layer's
    wording rather than the ``_workspace_serialized`` one. Pinning it is
    what lets the sibling's docstring say "re-worded" honestly.

    Construction: hold the tree lock so the renew thread parks at
    acquisition, release the claim underneath it (the release half of
    ``_replace_claim_under_lock``), then let the renew acquire and mint.

    The tree is deliberately NOT retired. Retiring it would race
    ``_acquire_workspace_lock``'s ``_has_git`` probe at the acquire
    boundary, and losing that race yields the *other* legitimate message
    ("changed while waiting for its lock"). Only the CLAIM's absence
    causes the refusal under test, so releasing alone makes the assertion
    land on the db wording deterministically. A future re-check in
    ``_do_renew`` would legitimately move this message one layer up again;
    that is a real behaviour change and should update this assertion.
    """
    sb = _Sandbox()
    try:
        tok = agents["gamma"]["token"]
        pid = db.create_proposal(tok, "Renew Released ABA", "body")["post_id"]
        claimed = workspace_tools.workspace_claim(tok, "claim", pid, "gone")
        claim_id = int(claimed["claim"]["id"])
        dest = str(claimed["tree"]["path"])
        # The CLAIM arm mints this proposal's read AND write tickets
        # (_do_claim -> mint_claim_tickets), so the baseline here is 2,
        # not 0. Asserting an absolute 0 reads the claim's own two rows
        # as though the refused mint had written them - so pin the DELTA
        # the refused renew adds, and pin the baseline as a precondition
        # so that delta cannot go vacuous.
        with db._conn() as conn:
            baseline = conn.execute(
                "SELECT COUNT(*) FROM transfer_tickets WHERE proposal_id = ?",
                (pid,),
            ).fetchone()[0]
        assert baseline == 2, f"precondition: claim minted 2, got {baseline}"
        lock_factory = workspace_tools.workspace_lock
        queued = threading.Event()
        outcome = []

        @contextmanager
        def observed_lock(path, *, allow_missing=False):
            queued.set()
            with lock_factory(path, allow_missing=allow_missing):
                yield

        def renew():
            try:
                outcome.append(
                    workspace_tools.workspace_claim(tok, "renew", pid, "gone")
                )
            except BaseException as exc:
                outcome.append(exc)

        with lock_factory(dest):
            with patch.object(workspace_tools, "workspace_lock", observed_lock):
                worker = threading.Thread(target=renew)
                worker.start()
                assert queued.wait(1), "renew never reached lock acquisition"
                released = db.release_workspace(tok, pid, "gone", claim_id=claim_id)
                assert released["status"] == "released", released
        worker.join(5)
        assert not worker.is_alive(), "queued renew did not finish"
        assert len(outcome) == 1, outcome
        res = outcome[0]
        assert isinstance(res, db.ForumError), (
            f"renew minted a ticket for a released claim: {res!r}"
        )
        assert "of yours" in str(res), res
        assert "tickets mint on live owned claims only" in str(res), res
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM transfer_tickets WHERE proposal_id = ?",
                (pid,),
            ).fetchone()[0]
        assert rows == baseline, (
            f"a refused mint still wrote {rows - baseline} ticket row(s)"
        )
    finally:
        sb.close()
    print("  renew on a released claim -> db refusal, nothing minted: ok")


def test_read_ticket_reclaim_aba(agents):
    sb = _Sandbox()
    try:
        tok = agents["beta"]["token"]
        pid = db.create_proposal(tok, "ABA Download", "body")["post_id"]
        claimed = workspace_tools.workspace_claim(tok, "claim", pid, "dl")
        workspace_tools.workspace_write_file(tok, pid, "dl", "note.txt", "old\n")
        old = db.get_workspace(tok, pid, "dl")
        ticket = claimed["read"]
        original_read = ws.read_transfer_bytes
        fresh = {}

        def reclaim_before_read(*args, **kwargs):
            db.release_workspace(tok, pid, "dl", claim_id=old["id"])
            fresh["claim"] = db.claim_workspace(tok, pid, "dl")
            assert fresh["claim"]["id"] != old["id"], (old, fresh)
            return original_read(*args, **kwargs)

        with patch.object(ws, "read_transfer_bytes", reclaim_before_read):
            response = asyncio.run(
                transfer.transfer_download(_request(ticket["ticket"], "note.txt"))
            )
        assert response.status_code == 404, (response.status_code, response.body)
        assert "gone" in response.body.decode(), response.body
        db.release_workspace(tok, pid, "dl", claim_id=fresh["claim"]["id"])
    finally:
        sb.close()


def test_sweeper_rechecks_live_and_idle_state():
    sb = _Sandbox()
    try:
        root = os.path.join(sb.tmp, "claims", "7", "4242")
        reclaimed = os.path.join(root, "reclaimed")
        os.makedirs(reclaimed)
        Path(reclaimed, ".workspace.json").write_text(
            json.dumps(
                {
                    "agent_id": 7,
                    "proposal_id": 4242,
                    "name": "reclaimed",
                    "updated_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
        assert ws.sweep_released_claim_trees(lambda: {(7, 4242, "reclaimed")}) == 0
        assert os.path.isdir(reclaimed), reclaimed

        idle = os.path.join(root, "touched")
        os.makedirs(idle)
        manifest_path = Path(idle, ".workspace.json")
        manifest_path.write_text(
            json.dumps(
                {
                    "agent_id": 7,
                    "proposal_id": 4242,
                    "name": "touched",
                    "updated_at": 0,
                }
            ),
            encoding="utf-8",
        )
        original_read = ws._read_manifest
        reads = 0

        def refreshed_before_read(path):
            nonlocal reads
            if path != idle:
                return original_read(path)
            reads += 1
            manifest = original_read(path) or {}
            manifest["updated_at"] = time.time()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            return original_read(path)

        with patch.object(ws, "_read_manifest", refreshed_before_read):
            assert ws.sweep_idle_claim_trees() == 0
        assert reads == 1, reads
        assert os.path.isdir(idle), idle
    finally:
        sb.close()


def _replace_claim_under_lock(token, proposal_id, name, dest, claim_id):
    released = db.release_workspace(token, proposal_id, name, claim_id=claim_id)
    assert released["status"] == "released", released
    assert ws._retire_claim_tree_locked(dest), dest
    replacement = db.claim_workspace(token, proposal_id, name)
    assert replacement["id"] != claim_id, (claim_id, replacement)
    ws.ensure_claim_tree(
        int(replacement["agent_id"]), proposal_id, str(replacement["name"])
    )
    marker = Path(dest, "replacement.txt")
    marker.write_text("replacement\n", encoding="utf-8")
    return replacement, marker


def test_lifecycle_release_waits_for_active_mutator_and_reclaims(agents):
    sb = _Sandbox()
    try:
        tok = agents["alpha"]["token"]
        pid = db.create_proposal(tok, "Lifecycle Mutator", "body")["post_id"]
        claimed = workspace_tools.workspace_claim(tok, "claim", pid, "dev")
        old_id = int(claimed["claim"]["id"])
        dest = str(claimed["tree"]["path"])
        lock_factory = workspace_tools.workspace_lock
        entered = threading.Event()
        proceed = threading.Event()
        active_result = []
        release_result = []
        release_done = threading.Event()

        @contextmanager
        def observed_lock(path, *, allow_missing=False):
            with lock_factory(path, allow_missing=allow_missing):
                entered.set()
                assert proceed.wait(5)
                yield

        def active_mutator():
            try:
                active_result.append(
                    workspace_tools.workspace_write_file(
                        tok, pid, "dev", "stale.txt", content="old\n"
                    )
                )
            except BaseException as exc:
                active_result.append(exc)

        def lifecycle_release():
            with db._conn() as conn:
                release_result.append(db.release_workspaces_for_proposal(conn, pid))
            release_done.set()

        with patch.object(workspace_tools, "workspace_lock", observed_lock):
            worker = threading.Thread(target=active_mutator)
            worker.start()
            assert entered.wait(2)
            releaser = threading.Thread(target=lifecycle_release)
            releaser.start()
            assert not release_done.wait(0.2)
            proceed.set()
            worker.join(5)
            releaser.join(5)
        assert not worker.is_alive()
        assert not releaser.is_alive()
        assert not isinstance(active_result[0], BaseException), active_result
        assert release_result == [1], release_result
        replacement = workspace_tools.workspace_claim(tok, "claim", pid, "dev")
        assert int(replacement["claim"]["id"]) != old_id
        assert not Path(dest, "stale.txt").exists()
        workspace_tools.workspace_claim(tok, "release", pid, "dev")
    finally:
        sb.close()


def test_lifecycle_release_waits_for_active_transfer_and_reclaims(agents):
    sb = _Sandbox()
    try:
        tok = agents["beta"]["token"]
        pid = db.create_proposal(tok, "Lifecycle Transfer", "body")["post_id"]
        claimed = workspace_tools.workspace_claim(tok, "claim", pid, "dev")
        old_id = int(claimed["claim"]["id"])
        dest = str(claimed["tree"]["path"])
        entered = threading.Event()
        proceed = threading.Event()
        transfer_result = []
        release_result = []
        release_done = threading.Event()

        def hold_transfer():
            entered.set()
            assert proceed.wait(5)

        def active_transfer():
            try:
                transfer_result.append(
                    ws.apply_transfer_bytes(
                        int(claimed["claim"]["agent_id"]),
                        pid,
                        "dev",
                        "transfer.txt",
                        b"old\n",
                        claim_validator=hold_transfer,
                    )
                )
            except BaseException as exc:
                transfer_result.append(exc)

        def lifecycle_release():
            with db._conn() as conn:
                release_result.append(db.release_workspaces_for_proposal(conn, pid))
            release_done.set()

        worker = threading.Thread(target=active_transfer)
        worker.start()
        assert entered.wait(2)
        releaser = threading.Thread(target=lifecycle_release)
        releaser.start()
        assert not release_done.wait(0.2)
        proceed.set()
        worker.join(5)
        releaser.join(5)
        assert not worker.is_alive()
        assert not releaser.is_alive()
        assert not isinstance(transfer_result[0], BaseException), transfer_result
        assert release_result == [1], release_result
        replacement = workspace_tools.workspace_claim(tok, "claim", pid, "dev")
        assert int(replacement["claim"]["id"]) != old_id
        assert not Path(dest, "transfer.txt").exists()
        workspace_tools.workspace_claim(tok, "release", pid, "dev")
    finally:
        sb.close()


def test_queued_async_mutator_rechecks_claim(agents):
    sb = _Sandbox()
    try:
        tok = agents["alpha"]["token"]
        pid = db.create_proposal(tok, "Queued Async ABA", "body")["post_id"]
        claimed = workspace_tools.workspace_claim(tok, "claim", pid, "dev")
        claim_id = int(claimed["claim"]["id"])
        dest = str(claimed["tree"]["path"])
        lock_factory = workspace_tools.workspace_lock
        acquire_attempted = threading.Event()
        invoked = threading.Event()

        async def stale_write(token, proposal_id, name):
            invoked.set()
            await asyncio.to_thread(
                Path(dest, "stale.txt").write_text,
                "stale\n",
                encoding="utf-8",
            )

        serialized = workspace_tools._workspace_serialized(stale_write)

        @contextmanager
        def observed_lock(path, *, allow_missing=False):
            acquire_attempted.set()
            with lock_factory(path, allow_missing=allow_missing):
                yield

        async def run_reclaim():
            with lock_factory(dest):
                with patch.object(workspace_tools, "workspace_lock", observed_lock):
                    task = asyncio.create_task(serialized(tok, pid, "dev"))
                    assert await asyncio.to_thread(acquire_attempted.wait, 1)
                    _replacement, marker = _replace_claim_under_lock(
                        tok, pid, "dev", dest, claim_id
                    )
            try:
                await task
            except db.ForumError as exc:
                error = str(exc)
            else:
                raise AssertionError(
                    "queued async mutator ran on the replacement claim"
                )
            assert "changed while waiting for its lock" in error, error
            assert not invoked.is_set()
            assert marker.read_text(encoding="utf-8") == "replacement\n"
            assert not Path(dest, "stale.txt").exists()

        asyncio.run(run_reclaim())
        workspace_tools.workspace_claim(tok, "release", pid, "dev")
    finally:
        sb.close()


def test_queued_sync_mutator_rechecks_claim(agents):
    sb = _Sandbox()
    try:
        tok = agents["beta"]["token"]
        pid = db.create_proposal(tok, "Queued Sync ABA", "body")["post_id"]
        claimed = workspace_tools.workspace_claim(tok, "claim", pid, "dev")
        claim_id = int(claimed["claim"]["id"])
        dest = str(claimed["tree"]["path"])
        lock_factory = workspace_tools.workspace_lock
        acquire_attempted = threading.Event()
        invoked = threading.Event()
        outcome = []

        def stale_write(token, proposal_id, name):
            invoked.set()
            Path(dest, "stale.txt").write_text("stale\n", encoding="utf-8")

        serialized = workspace_tools._workspace_serialized(stale_write)

        @contextmanager
        def observed_lock(path, *, allow_missing=False):
            acquire_attempted.set()
            with lock_factory(path, allow_missing=allow_missing):
                yield

        def invoke():
            try:
                serialized(tok, pid, "dev")
            except BaseException as exc:
                outcome.append(exc)

        with lock_factory(dest):
            with patch.object(workspace_tools, "workspace_lock", observed_lock):
                worker = threading.Thread(target=invoke)
                worker.start()
                assert acquire_attempted.wait(1)
                _replacement, marker = _replace_claim_under_lock(
                    tok, pid, "dev", dest, claim_id
                )
        worker.join(2)
        assert not worker.is_alive(), "queued sync mutator did not finish"
        assert len(outcome) == 1, outcome
        assert isinstance(outcome[0], db.ForumError), outcome
        assert "changed while waiting for its lock" in str(outcome[0]), outcome
        assert not invoked.is_set()
        assert marker.read_text(encoding="utf-8") == "replacement\n"
        assert not Path(dest, "stale.txt").exists()
        workspace_tools.workspace_claim(tok, "release", pid, "dev")
    finally:
        sb.close()


def test_acquire_window_replacement_surfaces_forum_error_async(agents):
    """#B117 deterministic pin (async arm): a replacement landing between
    claim-resolve and lock-acquisition destroys the tree first, so
    workspace_lock's missing-tree RepoError fires before the graceful
    re-check ever runs. The contention-based twin above hits that window
    ~1-in-10 under two-burner load; this pin CONSTRUCTS it by landing the
    replacement's destructive half at the acquire boundary itself - zero
    burners, zero scheduling luck. Contract after the fix: the documented
    recoverable ForumError. Failure signature before the fix: RepoError
    ('no workspace tree held') escaping the mutator."""
    sb = _Sandbox()
    try:
        tok = agents["gamma"]["token"]
        pid = db.create_proposal(tok, "B117 acquire window async", "body")["post_id"]
        claimed = workspace_tools.workspace_claim(tok, "claim", pid, "dev")
        claim_id = int(claimed["claim"]["id"])
        lock_factory = workspace_tools.workspace_lock
        invoked = threading.Event()

        async def stale_write(token, proposal_id, name):
            invoked.set()

        serialized = workspace_tools._workspace_serialized(stale_write)

        @contextmanager
        def replacing_lock(path, *, allow_missing=False):
            # The replacement's destructive half, landed at the acquire
            # boundary: the claim is released (CAS on our id) and the tree
            # retired - the real lock then observes the missing tree,
            # exactly what the loaded interleaving produces mid-replacement.
            db.release_workspace(tok, pid, "dev", claim_id=claim_id)
            assert ws._retire_claim_tree_locked(path), path
            with lock_factory(path, allow_missing=allow_missing):
                yield

        async def run():
            with patch.object(workspace_tools, "workspace_lock", replacing_lock):
                await serialized(tok, pid, "dev")

        try:
            asyncio.run(run())
        except db.ForumError as exc:
            error = str(exc)
        except BaseException as exc:
            raise AssertionError(
                "#B117: acquisition-time replacement surfaced "
                f"{type(exc).__name__}({exc}) instead of the recoverable "
                "ForumError('changed while waiting for its lock')"
            ) from exc
        else:
            raise AssertionError("mutator ran on a released claim")
        assert "changed while waiting for its lock" in error, error
        assert not invoked.is_set(), "the stale write must never run"
    finally:
        sb.close()
    print("  acquire-window replacement -> ForumError (async): ok")


def test_acquire_window_replacement_surfaces_forum_error_sync(agents):
    """Sync-arm twin: sync_wrapper shares the resolve-then-acquire
    ordering, so it shares the window and the translation."""
    sb = _Sandbox()
    try:
        tok = agents["delta"]["token"]
        pid = db.create_proposal(tok, "B117 acquire window sync", "body")["post_id"]
        claimed = workspace_tools.workspace_claim(tok, "claim", pid, "dev")
        claim_id = int(claimed["claim"]["id"])
        lock_factory = workspace_tools.workspace_lock
        invoked = threading.Event()

        def stale_write(token, proposal_id, name):
            invoked.set()

        serialized = workspace_tools._workspace_serialized(stale_write)

        @contextmanager
        def replacing_lock(path, *, allow_missing=False):
            db.release_workspace(tok, pid, "dev", claim_id=claim_id)
            assert ws._retire_claim_tree_locked(path), path
            with lock_factory(path, allow_missing=allow_missing):
                yield

        try:
            with patch.object(workspace_tools, "workspace_lock", replacing_lock):
                serialized(tok, pid, "dev")
        except db.ForumError as exc:
            error = str(exc)
        except BaseException as exc:
            raise AssertionError(
                "#B117 (sync arm): acquisition-time replacement surfaced "
                f"{type(exc).__name__}({exc}) instead of the recoverable "
                "ForumError"
            ) from exc
        else:
            raise AssertionError("sync mutator ran on a released claim")
        assert "changed while waiting for its lock" in error, error
        assert not invoked.is_set(), "the stale write must never run"
    finally:
        sb.close()
    print("  acquire-window replacement -> ForumError (sync): ok")


def test_acquire_window_infra_fault_stays_loud(agents):
    """The translation must not widen: with the claim record INTACT and
    the tree gone for unrelated reasons, the fault is infrastructure, not
    a lost race - retrying cannot fix it, so the RepoError('no workspace
    tree held') must surface unchanged. Without this pin, "translate the
    replacement case" could silently drift into "swallow every acquisition
    fault" and a broken deployment would degrade into an endless
    'retry against the current claim' loop."""
    sb = _Sandbox()
    try:
        tok = agents["epsilon"]["token"]
        pid = db.create_proposal(tok, "B117 infra fault stays loud", "body")["post_id"]
        workspace_tools.workspace_claim(tok, "claim", pid, "dev")
        lock_factory = workspace_tools.workspace_lock

        async def noop_write(token, proposal_id, name):
            raise AssertionError("the mutator must not run without a tree")

        serialized = workspace_tools._workspace_serialized(noop_write)

        @contextmanager
        def treeless_lock(path, *, allow_missing=False):
            assert ws._retire_claim_tree_locked(path), path
            with lock_factory(path, allow_missing=allow_missing):
                yield

        async def run():
            with patch.object(workspace_tools, "workspace_lock", treeless_lock):
                await serialized(tok, pid, "dev")

        try:
            asyncio.run(run())
        except Exception as exc:
            assert type(exc).__name__ == "RepoError", (
                "claim-intact tree loss must surface the RepoError"
                f" unchanged, got {type(exc).__name__}: {exc}"
            )
            assert "no workspace tree held" in str(exc), exc
        else:
            raise AssertionError("expected RepoError with the claim intact")
        workspace_tools.workspace_claim(tok, "release", pid, "dev")
    finally:
        sb.close()
    print("  acquire-window infra fault stays loud (RepoError): ok")


def main():
    agents, _post_id = setup()
    test_stale_release_cas(agents)
    test_claim_workspace_rechecks_after_lock(agents)
    test_lifecycle_reclaim_reuses_path_lock(agents)
    test_renew_mint_rebinds_claim_under_lock(agents)
    test_renew_on_released_claim_refuses(agents)
    test_read_ticket_reclaim_aba(agents)
    test_sweeper_rechecks_live_and_idle_state()
    test_lifecycle_release_waits_for_active_mutator_and_reclaims(agents)
    test_lifecycle_release_waits_for_active_transfer_and_reclaims(agents)
    test_queued_async_mutator_rechecks_claim(agents)
    test_queued_sync_mutator_rechecks_claim(agents)
    test_acquire_window_replacement_surfaces_forum_error_async(agents)
    test_acquire_window_replacement_surfaces_forum_error_sync(agents)
    test_acquire_window_infra_fault_stays_loud(agents)
    print("test_workspace_aba: all scenarios passed")


if __name__ == "__main__":
    main()
