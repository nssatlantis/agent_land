"""Tests for official job positions (PR-2): admin-created standing
roles paid from the treasury instead of escrow - longer cycle caps, the
waived karma floor for the sponsor, treasury-funded wages with
unfunded-skip, no-op refunds on cancel/close, and the admin panel flow."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_jobs_officials_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "10"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "0"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING"] = "0"
# server.admin reads these at import time - set them before that import.
os.environ.setdefault("ADMIN_USER", "root")
os.environ.setdefault("ADMIN_PASSWORD", "secret")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()

# Funded officials need treasury headroom beyond the 4000q genesis.
from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(40000, "test_suite_topup", admin="test-suite", conn=_c)


def _bal(agent_id: int) -> int:
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _treasury() -> int:
    with db._conn() as conn:
        return db.treasury_balance(conn)


def _supply() -> int:
    with db._conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries",
        ).fetchone()[0]


def _mail(token: str) -> list[tuple[str, str]]:
    with db._conn() as conn:
        ag = db._require_agent_by_token(conn, token)
        return [
            (r["kind"], r["body"])
            for r in conn.execute(
                "SELECT kind, body FROM notifications WHERE agent_id = ? ORDER BY id",
                (ag["id"],),
            ).fetchall()
        ]


def test_create_official_waives_gate_and_escrow():
    sponsor = db.register_agent("off-sponsor")  # zero karma, zero credits
    worker = db.register_agent("off-worker")
    job = db.create_job_official(
        "maintainer",
        sponsor["name"],
        "Chronicler",
        "Keep records.",
        2.0,
        ["read PRs", "write entry"],
        kind="recurring",
        cycles=4,
        scope="HISTORY.md",
        offer_to=worker["name"],
    )
    assert job["official"] is True
    assert job["status"] == "offered"
    assert job["total_cycles"] == 4, "beyond the citizen cap of 7 is fine"
    assert job["payment_quarters"] == 8
    assert _bal(sponsor["agent_id"]) == 0, "no escrow, no fees"
    assert any("OFFICIAL position was offered" in b for _, b in _mail(worker["token"]))
    # The citizen gate still bites a plain create_job.
    try:
        db.create_job(sponsor["token"], "t", "d", 1.0, ["s"])
        raise AssertionError("karma gate must not be waived for citizens")
    except db.ForumError as exc:
        assert "effective karma" in str(exc)


def test_official_cycle_cap_knob():
    sponsor = db.register_agent("off-cap")
    db.create_job_official(
        "m",
        sponsor["name"],
        "ok",
        "d",
        1.0,
        ["s"],
        cycles=config.JOB_OFFICIAL_MAX_CYCLES,
    )
    try:
        db.create_job_official(
            "m",
            sponsor["name"],
            "big",
            "d",
            1.0,
            ["s"],
            cycles=config.JOB_OFFICIAL_MAX_CYCLES + 1,
        )
        raise AssertionError("over-cap cycles refused")
    except db.ForumError as exc:
        assert "FORUM_JOB_OFFICIAL_MAX_CYCLES" in str(exc)


def test_accept_pays_wage_from_treasury_supply_neutral():
    sponsor = db.register_agent("off-pay")
    worker = db.register_agent("off-payw")
    t0, s0 = _treasury(), _supply()
    job = db.create_job_official(
        "m", sponsor["name"], "role", "d", 2.0, ["s"], offer_to=worker["name"]
    )
    # Treasury escrow locked at creation (8q * 7 cycles = 56q for default recurring)
    assert _treasury() == t0 - 56, "official escrow locks full payout at creation"
    db.accept_job_offer(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#P1")
    out = db.review_job(sponsor["token"], job["job_id"], "accept")
    assert out["cycles_done"] == 1
    # Wage 8q was already escrowed (56q at creation), now paid from escrow; JOB_CREDIT_CREDITS 1q each still from treasury
    assert _bal(worker["agent_id"]) == 8 + 1
    assert _bal(sponsor["agent_id"]) == 1
    # After one accept of 7-cycle job: creation moves 56 treasury->escrow
    # (supply-neutral), rewards -2 treasury (paired), wage pays from escrow
    # (supply-neutral) => treasury -58, supply unchanged throughout.
    assert _treasury() == t0 - 58  # -56 escrow + -2 rewards
    assert _supply() == s0, "escrow moves principal; supply never moves"
    with db._conn() as conn:
        kw = db._karma_parts(conn, worker["agent_id"])
        kc = db._karma_parts(conn, sponsor["agent_id"])
    assert kw["job_rewards"] == 1 and kc["job_rewards"] == 1
    # The ledger names the source distinctly from escrow payouts.
    with db._conn() as conn:
        reasons = [
            r["reason"]
            for r in conn.execute(
                "SELECT DISTINCT reason FROM credit_entries WHERE reason LIKE"
                " 'official%' OR reason = 'job_reward'",
            ).fetchall()
        ]
    assert "official_job_wage" in reasons


def test_unfunded_treasury_skips_wage_but_serves_cycle():
    sponsor = db.register_agent("off-unf")
    worker = db.register_agent("off-unfw")
    job = db.create_job_official(
        "m", sponsor["name"], "role", "d", 2.0, ["s"], offer_to=worker["name"]
    )
    db.accept_job_offer(worker["token"], job["job_id"])
    # Cycle 1 is served and paid WHILE funded.
    db.submit_job(worker["token"], job["job_id"], "#P1")
    out = db.review_job(sponsor["token"], job["job_id"], "accept")
    assert out["cycles_done"] == 1
    bal = _bal(worker["agent_id"])
    assert bal >= 8, "the funded wage landed"
    # Drain the treasury, then serve cycle 2: with escrow, wage still pays from reserved escrow
    # (legacy unfunded-skip only applied when wage was grant-at-accept, not escrowed)
    with db._conn(immediate=True) as conn:
        from db._credits import burn

        burn(_treasury(), reason="drain", admin="t", conn=conn)
    db.submit_job(worker["token"], job["job_id"], "#P2")
    out = db.review_job(sponsor["token"], job["job_id"], "accept")
    assert out["cycles_done"] == 2, (
        "escrowed wage still counts as served even when treasury dry"
    )
    assert _bal(worker["agent_id"]) == bal + 8, (
        "escrowed wage pays even when treasury dry"
    )
    # No economy unfunded notice for escrowed official wages


def test_cancel_and_admin_close_move_nothing_for_officials():
    sponsor = db.register_agent("off-cancel")
    worker = db.register_agent("off-cancelw")
    job = db.create_job_official(
        "m", sponsor["name"], "role", "d", 2.0, ["s"], offer_to=worker["name"]
    )
    db.accept_job_offer(worker["token"], job["job_id"])
    t0 = _treasury()
    s_bal, w_bal = (_bal(sponsor["agent_id"]), _bal(worker["agent_id"]))
    out = db.admin_cancel_job("maintainer", job["job_id"])
    assert out["status"] == "cancelled" and out["official"] is True
    assert _bal(sponsor["agent_id"]) == s_bal, "no citizen escrow to return"
    assert _bal(worker["agent_id"]) == w_bal
    # Official cancel refunds treasury escrow (full payout reserved at creation)
    assert _treasury() == t0 + 56, "treasury escrow refunded on cancel"
    mails = [b for _, b in _mail(worker["token"])]
    assert any("Admin moderation (maintainer) closed" in m for m in mails)
    # Expiry sweep spares official positions - standing roles never
    # auto-expire, so no treasury escrow moves either.
    stale = db.create_job_official("m", sponsor["name"], "stale role", "d", 2.0, ["s"])
    t1 = _treasury()
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE jobs SET created_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (stale["job_id"],),
        )
    before_sup = _supply()
    assert db._jobs.sweep_expired_jobs() == 0
    assert db.get_job(stale["job_id"])["status"] == "open"
    assert _treasury() == t1
    assert _supply() == before_sup


def test_delete_agent_with_official_jobs_moves_cleanly():
    victim = db.register_agent("off-victim")
    helper = db.register_agent("off-helper")
    j = db.create_job_official("m", victim["name"], "v-role", "d", 2.0, ["s"])
    j2 = db.create_job_official(
        "m", helper["name"], "h-role", "d", 2.0, ["s"], offer_to=victim["name"]
    )
    db.accept_job_offer(victim["token"], j2["job_id"])
    from moderation import delete_agent

    delete_agent(victim["agent_id"], "admin")
    try:
        db.get_job(j["job_id"])
        raise AssertionError("victim's posted official should be purged")
    except db.ForumError:
        pass
    d2 = db.get_job(j2["job_id"])
    assert d2["status"] == "open" and d2["worker"] is None


def test_admin_panel_flow_end_to_end():
    """The real HTTP surface (in-process starlette requests, the same
    shape test_admin_http uses): the auth gate, CSRF pairing, the
    create-official POST landing a real row, and the close POST."""
    import asyncio
    import base64
    from urllib.parse import urlencode

    from starlette.requests import Request

    from server import admin as admin_mod

    auth = "Basic " + base64.b64encode(b"root:secret").decode()
    csrf = "tok"

    def _req(
        method,
        path,
        *,
        body=None,
        cookies=None,
        headers=None,
        path_params=None,
        authed=True,
    ):
        hb = list(headers or [])
        if cookies:
            hb.append(
                (b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode())
            )
        if authed:
            hb.append((b"authorization", auth.encode()))
        bb = urlencode(body).encode() if body is not None else b""
        if body is not None:
            hb.append((b"content-type", b"application/x-www-form-urlencoded"))
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bb, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": hb,
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "path_params": path_params or {},
            "state": {},
        }
        return Request(scope, receive)

    # The docket page renders the Jobs panel with its forms.
    page = asyncio.run(
        admin_mod.admin_page(
            _req("GET", "/admin", cookies={admin_mod._CSRF_COOKIE: csrf})
        )
    )
    assert page.status_code == 200
    assert "Create official position" in page.body.decode()

    # Unauthenticated POST is refused before any state change.
    r = asyncio.run(
        admin_mod.create_official_job(
            _req(
                "POST",
                "/admin/jobs/create-official",
                authed=False,
                body={"title": "x"},
                cookies={},
            )
        )
    )
    assert r.status_code == 401

    # Create an official position through the form.
    r = asyncio.run(
        admin_mod.create_official_job(
            _req(
                "POST",
                "/admin/jobs/create-official",
                cookies={admin_mod._CSRF_COOKIE: csrf},
                body={
                    "csrf": csrf,
                    "title": "Panel Chronicler",
                    "creator": AGENTS["beta"]["name"],
                    "description": "panel-made role",
                    "steps": "step A\nstep B",
                    "payment_credits": "1.5",
                    "kind": "recurring",
                    "cycles": "10",
                    "scope": "HISTORY.md",
                    "offer_to": "",
                },
            )
        )
    )
    assert r.status_code == 200, r.body.decode()[:300]
    listing = [
        j
        for j in db.list_jobs(view="open", limit=50)["jobs"]
        if j["title"] == "Panel Chronicler"
    ]
    assert listing and listing[0]["official"] is True
    job_id = listing[0]["job_id"]
    detail = db.get_job(job_id)
    assert [s["text"] for s in detail["steps"]] == ["step A", "step B"]
    assert detail["payment_credits"] == "1.5"
    assert detail["total_cycles"] == 10

    # Close it through the moderation button.
    r = asyncio.run(
        admin_mod.admin_close_job(
            _req(
                "POST",
                f"/admin/jobs/{job_id}/close",
                path_params={"id": job_id},
                cookies={admin_mod._CSRF_COOKIE: csrf},
                body={"csrf": csrf, "confirm": "on"},
            )
        )
    )
    assert r.status_code == 200, r.body.decode()[:300]
    assert db.get_job(job_id)["status"] == "cancelled"

    # A bad CSRF pair never mutates anything.
    j2 = db.create_job_official(
        "m", AGENTS["gamma"]["name"], "csrf bait", "d", 0.5, ["s"]
    )
    before = db.get_job(j2["job_id"])["status"]
    r = asyncio.run(
        admin_mod.admin_close_job(
            _req(
                "POST",
                f"/admin/jobs/{j2['job_id']}/close",
                path_params={"id": j2["job_id"]},
                cookies={admin_mod._CSRF_COOKIE: "other"},
                body={"csrf": "mismatched", "confirm": "on"},
            )
        )
    )
    assert db.get_job(j2["job_id"])["status"] == before

    # Reactivate an expired official through its moderation button.
    rej = db.create_job_official(
        "m", AGENTS["beta"]["name"], "re-panel", "d", 1.0, ["s"]
    )
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'expired',"
            " decided_at = '2026-02-01T00:00:00.000Z',"
            " treasury_escrow_quarters = 0 WHERE id = ?",
            (rej["job_id"],),
        )
    r = asyncio.run(
        admin_mod.admin_reactivate_job(
            _req(
                "POST",
                f"/admin/jobs/{rej['job_id']}/reactivate",
                path_params={"id": rej["job_id"]},
                cookies={admin_mod._CSRF_COOKIE: csrf},
                body={"csrf": csrf, "confirm": "on"},
            )
        )
    )
    assert r.status_code == 200, r.body.decode()[:300]
    assert db.get_job(rej["job_id"])["status"] == "open"


def test_list_jobs_exposes_offered_to_for_the_panel():
    """ember-flash note (1): open vs offered must be distinguishable in
    list_jobs rows so the panel's worker column cannot mislabel a plain
    open job as held-for-someone."""
    sponsor = db.register_agent("off-ls")
    worker = db.register_agent("off-lsw")
    offered = db.create_job_official(
        "m",
        sponsor["name"],
        "ls-off",
        "d",
        1.0,
        ["s"],
        offer_to=worker["name"],
    )
    open_j = db.create_job_official("m", sponsor["name"], "ls-open", "d", 1.0, ["s"])
    rows = {j["job_id"]: j for j in db.list_jobs(view="open", limit=50)["jobs"]}
    assert rows[offered["job_id"]]["offered_to"] == "off-lsw"
    assert rows[open_j["job_id"]]["offered_to"] is None


def test_event_labels_name_admin_and_never_say_zero_credits():
    """ember-flash notes (2)+(3): the ledger must be readable - official
    creations and moderation closes name the acting admin, and no label
    ever renders 'refunded 0 credits' for an official's empty escrow."""
    sponsor = db.register_agent("off-label")
    worker = db.register_agent("off-labelw")
    job = db.create_job_official(
        "maintainer",
        sponsor["name"],
        "labeled",
        "d",
        2.0,
        ["s"],
        offer_to=worker["name"],
    )
    db.accept_job_offer(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#P1")
    db.review_job(sponsor["token"], job["job_id"], "accept")
    db.admin_cancel_job("maintainer", job["job_id"])

    texts = [
        str(e.get("text", "")) for e in db.recent_activity(kind="events", limit=50)
    ]
    assert any(
        t.startswith('posted the job "labeled"')
        and t.endswith("created by admin maintainer")
        for t in texts
    )
    assert any('closed by admin maintainer: "labeled"' in t for t in texts)
    assert not any("refunded 0 credits" in t for t in texts), (
        "zero-escrow settlements must never render as 'refunded 0 credits'"
    )


def test_reactivate_expired_official_restores_offer_and_reescrows():
    sponsor = db.register_agent("off-react")
    worker = db.register_agent("off-reactw")
    job = db.create_job_official(
        "m",
        sponsor["name"],
        "react role",
        "d",
        2.0,
        ["s"],
        offer_to=worker["name"],
    )
    jid = job["job_id"]
    # Aged past the deadline: the sweep must spare standing roles now.
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE jobs SET created_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (jid,),
        )
    t0 = _treasury()
    assert db._jobs.sweep_expired_jobs() == 0
    assert db.get_job(jid)["status"] == "offered"
    assert _treasury() == t0
    # Simulate a pre-change expiry, then re-activate back to offered.
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'expired',"
            " decided_at = '2026-02-01T00:00:00.000Z',"
            " treasury_escrow_quarters = 0 WHERE id = ?",
            (jid,),
        )
    out = db.admin_reactivate_job("maintainer", jid)
    assert out["status"] == "offered" and out["official"] is True
    with db._conn() as conn:
        esc = conn.execute(
            "SELECT treasury_escrow_quarters FROM jobs WHERE id = ?", (jid,)
        ).fetchone()[0]
        kinds = [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM events WHERE target_type = 'job' AND target_id = ?",
                (jid,),
            ).fetchall()
        ]
    assert esc == 56, "remaining payout re-escrowed from treasury"
    assert _treasury() == t0 - 56
    assert "job_reactivated" in kinds
    mails = [b for _, b in _mail(worker["token"])]
    assert any("re-activated" in m for m in mails)


def test_reactivate_cancelled_official_keeps_worker_and_reescrows_remainder():
    sponsor = db.register_agent("off-react2")
    worker = db.register_agent("off-react2w")
    job = db.create_job_official(
        "m",
        sponsor["name"],
        "react chronicler",
        "d",
        2.0,
        ["s"],
        offer_to=worker["name"],
    )
    jid = job["job_id"]
    db.accept_job_offer(worker["token"], jid)
    db.submit_job(worker["token"], jid, "#P1")
    db.review_job(sponsor["token"], jid, "accept")
    db.submit_job(worker["token"], jid, "#P2")
    db.review_job(sponsor["token"], jid, "accept")
    assert db.get_job(jid)["cycles_done"] == 2
    t0 = _treasury()
    db.admin_cancel_job("maintainer", jid)
    assert db.get_job(jid)["status"] == "cancelled"
    assert _treasury() == t0 + 40, "cancel refunds the 5-cycle remainder"
    out = db.admin_reactivate_job("maintainer", jid)
    assert out["status"] == "active"
    with db._conn() as conn:
        row = conn.execute(
            "SELECT worker_agent_id, treasury_escrow_quarters FROM jobs WHERE id = ?",
            (jid,),
        ).fetchone()
    assert row[0] == worker["agent_id"] and row[1] == 40
    assert _treasury() == t0, "re-activation re-locks the remainder"
    # Refusals: live jobs, unknown ids, citizen-shaped rows, dry treasury.
    try:
        db.admin_reactivate_job("maintainer", jid)
        raise AssertionError("live jobs need no re-activation")
    except db.ForumError as exc:
        assert "re-activation" in str(exc)
    try:
        db.admin_reactivate_job("maintainer", 999999)
        raise AssertionError("unknown jobs refuse")
    except db.ForumError as exc:
        assert "no job with id" in str(exc)
    civ = db.create_job_official("m", sponsor["name"], "civ shape", "d", 1.0, ["s"])
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE jobs SET official = 0, status = 'expired' WHERE id = ?",
            (civ["job_id"],),
        )
    try:
        db.admin_reactivate_job("maintainer", civ["job_id"])
        raise AssertionError("citizen jobs refuse")
    except db.ForumError as exc:
        assert "only official positions" in str(exc)
    broke = db.create_job_official("m", sponsor["name"], "broke role", "d", 2.0, ["s"])
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'expired', treasury_escrow_quarters = 0"
            " WHERE id = ?",
            (broke["job_id"],),
        )
        from db._credits import burn

        burn(_treasury(), reason="drain", admin="t", conn=conn)
    try:
        db.admin_reactivate_job("maintainer", broke["job_id"])
        raise AssertionError("broke treasury refuses")
    except db.ForumError as exc:
        assert "insufficient treasury" in str(exc)
    with db._conn(immediate=True) as conn:
        from db._credits import mint

        mint(40000, "test_suite_re-topup", admin="test-suite", conn=conn)


def test_overdue_official_is_nudged_never_released():
    """Overdue release spares officials: an engaged worker's missed cycle
    still nudges (and still flags overdue on the board), but the standing
    role is never auto-cancelled and no penalty lands."""
    import importlib

    import config as live_config

    sponsor = db.register_agent("off-odue")
    worker = db.register_agent("off-oduew")
    job = db.create_job_official(
        "m",
        sponsor["name"],
        "odue role",
        "d",
        2.0,
        ["s"],
        offer_to=worker["name"],
    )
    jid = job["job_id"]
    db.accept_job_offer(worker["token"], jid)
    old_due = os.environ.get("FORUM_JOB_CYCLE_DUE_HOURS")
    old_rel = os.environ.get("FORUM_JOB_OVERDUE_RELEASE_AFTER")
    os.environ["FORUM_JOB_CYCLE_DUE_HOURS"] = "1"
    os.environ["FORUM_JOB_OVERDUE_RELEASE_AFTER"] = "2"
    importlib.reload(live_config)
    try:
        from datetime import datetime, timedelta, timezone

        past = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        with db._conn(immediate=True) as conn:
            conn.execute(
                "UPDATE events SET created_at = ? WHERE target_type = 'job'"
                " AND target_id = ? AND kind IN ('job_claimed','job_submitted',"
                "'job_cycle_accepted','job_cycle_declined')",
                (past, jid),
            )
        sent = db._jobs.sweep_overdue_job_cycles()
        assert sent >= 1, "the overdue worker is still nudged"
        assert db.get_job(jid)["status"] == "active"
        assert db.get_job(jid)["overdue"] is True
        with db._conn() as conn:
            pen = conn.execute(
                "SELECT amount FROM job_penalties WHERE job_id = ?", (jid,)
            ).fetchone()
            nudge = conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ?"
                " AND kind = 'jobs' AND ref_type = 'job' AND ref_id = ?"
                " AND body LIKE '%submit your work%'",
                (worker["agent_id"], jid),
            ).fetchall()
        assert pen is None, "no penalty lands on officials"
        assert len(nudge) == 1, "worker nudged exactly once"
        assert db._jobs.sweep_overdue_job_cycles() == 0, "no repeat traffic"
    finally:
        if old_due is None:
            os.environ.pop("FORUM_JOB_CYCLE_DUE_HOURS", None)
        else:
            os.environ["FORUM_JOB_CYCLE_DUE_HOURS"] = old_due
        if old_rel is None:
            os.environ.pop("FORUM_JOB_OVERDUE_RELEASE_AFTER", None)
        else:
            os.environ["FORUM_JOB_OVERDUE_RELEASE_AFTER"] = old_rel
        importlib.reload(live_config)


def _panel_req(method, path, *, body=None, path_params=None):
    """Minimal authed admin request for the panel tests below (mirrors the
    local helper inside test_admin_panel_flow_end_to_end)."""
    import base64
    from urllib.parse import urlencode

    from starlette.requests import Request

    from server import admin as admin_mod

    auth = "Basic " + base64.b64encode(b"root:secret").decode()
    csrf = "tok"
    hb = [(b"cookie", f"{admin_mod._CSRF_COOKIE}={csrf}".encode())]
    hb.append((b"authorization", auth.encode()))
    bb = urlencode(body).encode() if body is not None else b""
    if body is not None:
        hb.append((b"content-type", b"application/x-www-form-urlencoded"))
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": bb, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": hb,
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
        "path_params": path_params or {},
        "state": {},
    }
    return Request(scope, receive), csrf


def _valid_official_body(**over):
    body = {
        "csrf": "tok",
        "title": "Form Chronicler",
        "creator": "",
        "description": "keeps the record",
        "steps": "draft the entry\npost the diff",
        "payment_credits": "1",
        "kind": "recurring",
        "cycles": "3",
        "scope": "HISTORY.md",
        "offer_to": "",
    }
    body.update(over)
    return body


def test_official_form_shows_guidance_deposit_and_treasury():
    """Both pages render the shared form: checklist guide, deposit field
    with the 1.0 default, live caps, and the treasury line."""
    import asyncio
    import config as live_config

    from server import admin as admin_mod

    needles = [
        "review rubric",
        'name="taker_deposit"',
        'value="1.0"',
        "Treasury balance",
        f'maxlength="{live_config.JOB_TITLE_MAX_LEN}"',
        'type="number"',
        "never expire",
        "completion bonus",
    ]
    req, _ = _panel_req("GET", "/admin")
    dash = asyncio.run(admin_mod.admin_page(req)).body.decode()
    for needle in needles:
        assert needle in dash, f"dashboard form missing: {needle}"
    req, _ = _panel_req("GET", "/admin/jobs")
    mgr = asyncio.run(admin_mod.jobs_manager_page(req)).body.decode()
    for needle in needles:
        assert needle in mgr, f"manager form missing: {needle} (forms drifted?)"


def test_create_official_deposit_default_and_explicit():
    """Blank deposit posts the 1.0 default; an explicit value sticks."""
    import asyncio

    from server import admin as admin_mod

    req, _ = _panel_req(
        "POST",
        "/admin/jobs/create-official",
        body=_valid_official_body(title="Deposit Default"),
    )
    r = asyncio.run(admin_mod.create_official_job(req))
    assert r.status_code == 200, r.body.decode()[:300]
    row = next(
        j
        for j in db.list_jobs(view="all", limit=500)["jobs"]
        if j["title"] == "Deposit Default"
    )
    assert db.get_job(row["job_id"])["taker_deposit_quarters"] == 4
    req, _ = _panel_req(
        "POST",
        "/admin/jobs/create-official",
        body=_valid_official_body(title="Deposit Explicit", taker_deposit="2.5"),
    )
    r = asyncio.run(admin_mod.create_official_job(req))
    assert r.status_code == 200, r.body.decode()[:300]
    row = next(
        j
        for j in db.list_jobs(view="all", limit=500)["jobs"]
        if j["title"] == "Deposit Explicit"
    )
    assert db.get_job(row["job_id"])["taker_deposit_quarters"] == 10
    req, _ = _panel_req(
        "POST",
        "/admin/jobs/create-official",
        body=_valid_official_body(title="Cycles Default", cycles=""),
    )
    r = asyncio.run(admin_mod.create_official_job(req))
    assert r.status_code == 200, r.body.decode()[:300]
    row = next(
        j
        for j in db.list_jobs(view="all", limit=500)["jobs"]
        if j["title"] == "Cycles Default"
    )
    assert db.get_job(row["job_id"])["total_cycles"] == 7


def test_create_official_bad_input_keeps_values():
    """A refused submit re-renders with the typed input preserved and the
    refusal inline - nothing is created and nothing must be retyped."""
    import asyncio

    from server import admin as admin_mod

    before = len(db.list_jobs(view="all", limit=500)["jobs"])
    req, _ = _panel_req(
        "POST",
        "/admin/jobs/create-official",
        body=_valid_official_body(
            title="Keep My Typing", creator="beta", taker_deposit="abc"
        ),
    )
    r = asyncio.run(admin_mod.create_official_job(req))
    assert r.status_code == 200
    page = r.body.decode()
    assert "bad taker deposit" in page
    assert 'value="Keep My Typing"' in page and 'value="beta"' in page
    assert len(db.list_jobs(view="all", limit=500)["jobs"]) == before
    req, _ = _panel_req(
        "POST",
        "/admin/jobs/create-official",
        body=_valid_official_body(
            title="Keep My Steps", creator="beta", steps="   \n  "
        ),
    )
    r = asyncio.run(admin_mod.create_official_job(req))
    assert r.status_code == 200
    page = r.body.decode()
    assert "checklist step" in page
    assert 'value="Keep My Steps"' in page and 'value="beta"' in page
    assert len(db.list_jobs(view="all", limit=500)["jobs"]) == before


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} officials tests passed")
