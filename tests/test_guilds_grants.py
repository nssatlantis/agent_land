"""Guild project grants, requested not auto-sent (proposal #643): designation
gate, promotion binding without payment (even when the treasury is dry),
request/approve/decline/cancel lifecycle, one-grant-per-project and
two-per-guild caps, tiered review with large-tier venue, cooldown/cap
math, merge completion with slot freeing, supersede rebinding, legacy T2
expiry, budget gating, and conservation (proposal #611 wallets: supply
fixed, treasury down and pool claim up by the grant).
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_grants_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "0"
os.environ["FORUM_INVOICE_MIN_KARMA"] = "0"
# Pooled 7d grant budget: this file pays ~20cr across its lifecycle tests
# (two-grant, large-tier and merge rounds); the default 20cr window would
# starve the later tests, so widen it file-wide and pin the gate itself
# with a low-budget refusal test instead.
os.environ["FORUM_GUILD_GRANT_BUDGET"] = "40.0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]
_PR = [91000]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, units: int):
    import db._credits as _cr

    with db._conn() as _c:
        ok = _cr.grant(
            agent_id,
            units,
            "guild_grants_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )
    assert ok, "treasury could not fund the test seed"


def _treasury() -> int:
    import db._credits as _cr

    with db._conn() as conn:
        return _cr.treasury_balance(conn)


def _supply() -> int:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE account IN ('agent', 'treasury', 'escrow', 'guild')"
        ).fetchone()
    return int(row[0] or 0)


def _arm(env_key: str, value: str):
    from tests._setup import config as _cfg

    old = os.environ.get(env_key)
    os.environ[env_key] = value
    importlib.reload(_cfg)
    return old


def _unarm(old, env_key: str):
    from tests._setup import config as _cfg

    if old is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = old
    importlib.reload(_cfg)


def _found(name: str | None = None) -> tuple[dict, dict]:
    ag = _new_agent("gg-founder")
    _fund(ag["agent_id"], 600)
    return ag, db.found_guild(ag["token"], name or f"Grants-{_SEQ[0]}")


def _mate(founder: dict, guild: dict, prefix: str = "gg-mate") -> dict:
    mate = _new_agent(prefix)
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.guild_deposit(mate["token"], guild["id"], 10.0)
    return mate


def _old_idea(author: dict, tag: str, commenters: list[dict] | None = None) -> int:
    idea = db.create_proposal(
        author["token"], f"Guild idea {tag}", "A guild-scale build.", idea=True
    )
    pid = idea["post_id"]
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?",
            ("2026-09-01T00:00:00.000Z", pid),
        )
    for c in commenters or []:
        db.create_comment(c["token"], pid, f"looks good ({c['name']})")
    return pid


def _promote(author: dict, idea_id: int, with_todos: bool) -> dict:
    if with_todos:
        db.create_todo_list(author["token"], idea_id, "plan", [{"text": "build"}])
    return db.promote_idea(
        author["token"],
        idea_id,
        f"Build {idea_id}",
        "Full body here.",
        collaborative=True,
    )


def _merge(post_id: int, status: str = "merged") -> int:
    _PR[0] += 1
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id) VALUES (?, ?)",
            (_PR[0], post_id),
        )
        conn.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status,"
            " happened_at) VALUES (?, ?, ?, ?)",
            (_PR[0], post_id, status, "2026-09-17T00:00:00.000Z"),
        )
    return _PR[0]


def _pool(guild_id: int) -> int:
    with db._conn() as conn:
        return db.guild_balance(conn, guild_id)


def _link_for_post(post_id: int) -> dict | None:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_grant_links WHERE post_id = ?", (post_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def _request(founder: dict, guild: dict, post_id: int, amount: float = 2.0) -> dict:
    return db.request_guild_grant(
        founder["token"], guild["id"], post_id, amount, "build funds"
    )


def _approve(founder: dict, req_id: int) -> dict:
    return db.decide_guild_grant(founder["token"], req_id, True, admin=True)


def _funded(tag: str, amount: float = 2.0) -> tuple:
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-f1"), _new_agent("gg-f2")
    idea = _old_idea(mate, tag, [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    prop = _promote(mate, idea, True)
    req = _request(founder, guild, prop["post_id"], amount)
    out = _approve(founder, req["request_id"])
    assert out["status"] == "paid", out
    return founder, guild, mate, idea, prop["post_id"], req


def _complete(pid: int) -> dict:
    pr = _merge(pid)
    with db._conn(immediate=True) as conn:
        out = db.grant_on_merge(conn, pid, pr)
    assert out is not None and out["status"] == "complete", out
    return out


def test_tables_upgrade():
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS guild_grant_links")
        conn.execute("DROP TABLE IF EXISTS guild_grant_requests")
    db.init_db()
    with db._conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert "guild_grant_links" in tables
    assert "guild_grant_requests" in tables
    for idx in (
        "idx_guild_grant_links_guild",
        "idx_guild_grant_links_idea",
        "idx_guild_grant_requests_guild",
        "idx_guild_grant_requests_link",
    ):
        assert idx in indexes, f"{idx} missing after init_db"


def test_promotion_binds_without_paying():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-b1"), _new_agent("gg-b2")
    idea = _old_idea(mate, "bind", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    gid = guild["id"]
    supply_before, treasury_before, pool_before = _supply(), _treasury(), _pool(gid)
    prop = _promote(mate, idea, True)
    link = _link_for_post(prop["post_id"])
    assert link is not None and link["post_id"] == prop["post_id"], link
    assert link["t1_tranche_id"] is None and link["t2_tranche_id"] is None, link
    assert link["status"] == "active", link
    assert _pool(gid) == pool_before
    assert _supply() == supply_before
    assert _treasury() == treasury_before


def test_promotion_succeeds_when_treasury_dry():
    founder, guild = _found()
    mate = _mate(founder, guild)
    c1, c2 = _new_agent("gg-d1"), _new_agent("gg-d2")
    idea = _old_idea(mate, "dry", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    old = _arm("FORUM_GUILD_GRANT_BUDGET", "0")
    try:
        prop = _promote(mate, idea, True)
    finally:
        _unarm(old, "FORUM_GUILD_GRANT_BUDGET")
    link = _link_for_post(prop["post_id"])
    assert link is not None and link["t1_tranche_id"] is None, link


def test_request_and_approve_pays_full_entitlement():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], gid := guild["id"], 25.0)
    c1, c2 = _new_agent("gg-p1"), _new_agent("gg-p2")
    idea = _old_idea(mate, "pay", [c1, c2])
    db.designate_guild_project(founder["token"], gid, idea)
    supply_before, treasury_before, pool_before = _supply(), _treasury(), _pool(gid)
    pid = _promote(mate, idea, True)["post_id"]
    req = _request(founder, guild, pid)
    assert req["status"] == "requested", req
    assert req["tier"] == "small" and req["instance"] == 1, req
    assert req["venue_post_id"] is None, req
    assert _pool(gid) == pool_before
    out = _approve(founder, req["request_id"])
    assert out["status"] == "paid" and out["amount_units"] == 40, out
    assert _pool(gid) == pool_before + 40, (_pool(gid), pool_before)
    assert _supply() == supply_before
    assert _treasury() == treasury_before - 40
    link = _link_for_post(pid)
    assert link["eligible_count"] == 2 and link["decay_pct"] == 100, link
    assert link["t1_tranche_id"] is not None and link["t2_tranche_id"] is None, link
    with db._conn() as conn:
        t1 = conn.execute(
            "SELECT * FROM guild_tranches WHERE id = ?", (link["t1_tranche_id"],)
        ).fetchone()
        req_row = conn.execute(
            "SELECT * FROM guild_grant_requests WHERE id = ?", (req["request_id"],)
        ).fetchone()
    assert t1["status"] == "released" and t1["amount_units"] == 40, dict(t1)
    assert req_row["status"] == "paid", dict(req_row)


def test_one_grant_per_project():
    founder, guild, _mate, _idea, pid, _req = _funded("once")
    try:
        _request(founder, guild, pid)
        raise AssertionError("second grant on one project accepted")
    except Exception as exc:
        assert "one per project" in str(exc), exc


def test_two_lifetime_grants_then_cap():
    old_cd = _arm("FORUM_GUILD_GRANT_COOLDOWN_DAYS", "0")
    try:
        founder, guild, mate, _idea, pid, _req = _funded("cap1", 2.0)
        _complete(pid)
        c1, c2 = _new_agent("gg-k1"), _new_agent("gg-k2")
        idea2 = _old_idea(mate, "cap2", [c1, c2])
        db.designate_guild_project(founder["token"], guild["id"], idea2)
        pid2 = _promote(mate, idea2, True)["post_id"]
        pool_before = _pool(guild["id"])
        req2 = _request(founder, guild, pid2, 1.5)
        assert req2["instance"] == 2, req2
        out2 = _approve(founder, req2["request_id"])
        assert out2["status"] == "paid", out2
        assert out2["amount_units"] == 30, out2
        assert _pool(guild["id"]) == pool_before + 30
        _complete(pid2)
        c3, c4 = _new_agent("gg-k3"), _new_agent("gg-k4")
        idea3 = _old_idea(mate, "cap3", [c3, c4])
        db.designate_guild_project(founder["token"], guild["id"], idea3)
        pid3 = _promote(mate, idea3, True)["post_id"]
        try:
            _request(founder, guild, pid3, 1.0)
            raise AssertionError("third grant accepted")
        except Exception as exc:
            assert "two lifetime grants" in str(exc), exc
    finally:
        _unarm(old_cd, "FORUM_GUILD_GRANT_COOLDOWN_DAYS")


def test_large_tier_files_venue_and_waits():
    old = _arm("FORUM_GUILD_GRANT_PER_MEMBER", "5.0")
    try:
        founder, guild = _found()
        mate = _mate(founder, guild)
        db.guild_deposit(founder["token"], guild["id"], 25.0)
        c1, c2 = _new_agent("gg-v1"), _new_agent("gg-v2")
        idea = _old_idea(mate, "venue", [c1, c2])
        db.designate_guild_project(founder["token"], guild["id"], idea)
        pid = _promote(mate, idea, True)["post_id"]
        pool_before = _pool(guild["id"])
        req = _request(founder, guild, pid, 5.0)
        assert req["tier"] == "large" and req["venue_post_id"], req
        assert _pool(guild["id"]) == pool_before
        out = _approve(founder, req["request_id"])
        assert out["status"] == "paid" and out["amount_units"] == 100, out
        assert _pool(guild["id"]) == pool_before + 100
    finally:
        _unarm(old, "FORUM_GUILD_GRANT_PER_MEMBER")


def test_merge_completes_paid_link_and_frees_slot():
    founder, guild, mate, _idea, pid, _req = _funded("done")
    _complete(pid)
    with db._conn() as conn:
        link = conn.execute(
            "SELECT status, project_id FROM guild_grant_links WHERE post_id = ?",
            (pid,),
        ).fetchone()
        assert link["status"] == "complete", dict(link)
        proj = conn.execute(
            "SELECT status FROM guild_projects WHERE id = ?",
            (link["project_id"],),
        ).fetchone()
        assert proj["status"] == "done", dict(proj)
    idea2 = _old_idea(mate, "done2", [_new_agent("gg-z1"), _new_agent("gg-z2")])
    second = db.designate_guild_project(founder["token"], guild["id"], idea2)
    assert second["idea_post_id"] == idea2


def test_supersede_rebinds_link():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-s1"), _new_agent("gg-s2")
    idea = _old_idea(mate, "chain", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    prop = _promote(mate, idea, True)
    sup = db.supersede_proposal(
        mate["token"],
        prop["post_id"],
        f"Build {idea} v2",
        "Second body here.",
        collaborative=True,
    )
    new_id = sup["post_id"]
    assert _link_for_post(new_id) is not None
    assert _link_for_post(prop["post_id"]) is None
    req = _request(founder, guild, new_id)
    assert req["status"] == "requested", req
    out = _approve(founder, req["request_id"])
    assert out["status"] == "paid", out


def test_todo_creation_moves_no_money():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-w1"), _new_agent("gg-w2")
    idea = _old_idea(mate, "wait", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    pool_before = _pool(guild["id"])
    pid = _promote(mate, idea, False)["post_id"]
    assert _pool(guild["id"]) == pool_before
    db.create_todo_list(mate["token"], pid, "now", [{"text": "go"}])
    link = _link_for_post(pid)
    assert link is not None and link["t1_tranche_id"] is None, link
    assert _pool(guild["id"]) == pool_before


def test_non_collaborative_promotion_expires_link():
    founder, guild = _found()
    mate = _mate(founder, guild)
    c1, c2 = _new_agent("gg-nc1"), _new_agent("gg-nc2")
    idea = _old_idea(mate, "plain", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    db.promote_idea(mate["token"], idea, "Plain build", "Full body here.")
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_grant_links WHERE idea_post_id = ?", (idea,)
        ).fetchone()
    assert row is not None and row["status"] == "expired", dict(row)
    assert row["t1_tranche_id"] is None


def test_request_over_ceiling_refused():
    founder, guild, mate, _idea, pid, _req = _funded("ceil", 2.0)
    _complete(pid)
    c1, c2 = _new_agent("gg-e1"), _new_agent("gg-e2")
    idea2 = _old_idea(mate, "ceil2", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea2)
    pid2 = _promote(mate, idea2, True)["post_id"]
    try:
        _request(founder, guild, pid2, 2.0)
        raise AssertionError("over-ceiling request accepted")
    except Exception as exc:
        assert "at most 30u" in str(exc), exc


def test_request_gates():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-g1"), _new_agent("gg-g2")
    idea = _old_idea(mate, "gated", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    pid = _promote(mate, idea, True)["post_id"]
    try:
        _request(founder, guild, pid + 999999)
        raise AssertionError("linkless post accepted")
    except Exception as exc:
        assert "no active grant project" in str(exc), exc
    other, oguild = _found()
    try:
        db.request_guild_grant(other["token"], oguild["id"], pid, 1.0, "mine?")
        raise AssertionError("foreign post accepted")
    except Exception as exc:
        assert "no active grant project" in str(exc), exc
    try:
        db.request_guild_grant(mate["token"], guild["id"], pid, 1.0, "gimme")
        raise AssertionError("non-founder request accepted")
    except Exception as exc:
        assert "founder" in str(exc).lower(), exc
    f2, g2 = _found()
    m2 = _mate(f2, g2)
    db.guild_deposit(f2["token"], g2["id"], 25.0)
    d1, d2 = _new_agent("gg-g3"), _new_agent("gg-g4")
    idea_b = _old_idea(m2, "bare", [d1, d2])
    db.designate_guild_project(f2["token"], g2["id"], idea_b)
    bare = _promote(m2, idea_b, False)["post_id"]
    try:
        _request(f2, g2, bare)
        raise AssertionError("todo-less post accepted")
    except Exception as exc:
        assert "to-do list" in str(exc), exc


def test_cooldown_gates_request():
    founder, guild, mate, _idea, pid, _req = _funded("cool", 2.0)
    _complete(pid)
    old = _arm("FORUM_GUILD_GRANT_COOLDOWN_DAYS", "99999")
    try:
        c1, c2 = _new_agent("gg-q1"), _new_agent("gg-q2")
        idea2 = _old_idea(mate, "cool2", [c1, c2])
        db.designate_guild_project(founder["token"], guild["id"], idea2)
        pid2 = _promote(mate, idea2, True)["post_id"]
        try:
            _request(founder, guild, pid2, 1.0)
            raise AssertionError("cooldown ignored")
        except Exception as exc:
            assert "cooldown" in str(exc), exc
    finally:
        _unarm(old, "FORUM_GUILD_GRANT_COOLDOWN_DAYS")


def test_decline_ends_request():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-d1"), _new_agent("gg-d2")
    idea = _old_idea(mate, "nope", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    pid = _promote(mate, idea, True)["post_id"]
    pool_before = _pool(guild["id"])
    req = _request(founder, guild, pid)
    out = db.decide_guild_grant(founder["token"], req["request_id"], False, admin=True)
    assert out["status"] == "declined", out
    assert _pool(guild["id"]) == pool_before
    again = _request(founder, guild, pid)
    assert again["status"] == "requested", again


def test_decline_after_entitlement_drift():
    old = _arm("FORUM_GUILD_GRANT_COOLDOWN_DAYS", "0")
    try:
        founder, guild, mate, _idea, pid, _req = _funded("drift", 2.0)
        _complete(pid)
        c1, c2 = _new_agent("gg-r1"), _new_agent("gg-r2")
        idea2 = _old_idea(mate, "drift2", [c1, c2])
        db.designate_guild_project(founder["token"], guild["id"], idea2)
        pid2 = _promote(mate, idea2, True)["post_id"]
        req2 = _request(founder, guild, pid2, 1.0)
        _arm("FORUM_GUILD_GRANT_COOLDOWN_DAYS", "99999")
        out = db.decide_guild_grant(
            founder["token"], req2["request_id"], False, admin=True
        )
    finally:
        _unarm(old, "FORUM_GUILD_GRANT_COOLDOWN_DAYS")
    assert out["status"] == "declined", out
    with db._conn() as conn:
        row = conn.execute(
            "SELECT status FROM guild_grant_requests WHERE id = ?",
            (req2["request_id"],),
        ).fetchone()
    assert row["status"] == "declined", dict(row)


def test_cancel_by_requester():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-x1"), _new_agent("gg-x2")
    idea = _old_idea(mate, "cancel", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    pid = _promote(mate, idea, True)["post_id"]
    req = _request(founder, guild, pid)
    try:
        db.cancel_guild_grant_request(mate["token"], req["request_id"])
        raise AssertionError("stranger cancel accepted")
    except Exception as exc:
        assert "requesting founder" in str(exc), exc
    out = db.cancel_guild_grant_request(founder["token"], req["request_id"])
    assert out["status"] == "cancelled", out
    try:
        db.decide_guild_grant(founder["token"], req["request_id"], True, admin=True)
        raise AssertionError("cancelled request decided")
    except Exception as exc:
        assert "cancelled" in str(exc), exc


def test_merge_without_payment_leaves_link_active():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-u1"), _new_agent("gg-u2")
    idea = _old_idea(mate, "unfunded", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    pid = _promote(mate, idea, True)["post_id"]
    pr = _merge(pid)
    with db._conn(immediate=True) as conn:
        out = db.grant_on_merge(conn, pid, pr)
    assert out is not None and out["status"] == "unfunded", out
    link = _link_for_post(pid)
    assert link is not None and link["status"] == "active", link


def test_legacy_t2_expires_unpaid_and_counts_cap():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-l1"), _new_agent("gg-l2")
    idea = _old_idea(mate, "legacy", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    pid = _promote(mate, idea, True)["post_id"]
    link = _link_for_post(pid)
    with db._conn(immediate=True) as conn:
        from db._credits import treasury_to_guild as _t2g

        assert _t2g(conn, guild["id"], 20, "guild_grant_t1")
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units, note)"
            " VALUES (?, 'grant_t1', ?, ?)",
            (guild["id"], 20, "synthetic legacy T1"),
        )
        cur = conn.execute(
            "INSERT INTO guild_tranches (guild_id, tier, amount_units, status,"
            " project_id, released_at) VALUES (?, 'T1', ?, 'released', ?, ?)",
            (guild["id"], 20, link["project_id"], "2026-09-01T00:00:00.000Z"),
        )
        t1 = int(cur.lastrowid or 0)
        cur2 = conn.execute(
            "INSERT INTO guild_tranches (guild_id, tier, amount_units, status,"
            " project_id, expires_at) VALUES (?, 'T2', ?, 'proposed', ?, ?)",
            (guild["id"], 20, link["project_id"], "2026-09-01T00:00:00.000Z"),
        )
        t2 = int(cur2.lastrowid or 0)
        conn.execute(
            "UPDATE guild_grant_links SET t1_tranche_id = ?, t2_tranche_id = ?"
            " WHERE id = ?",
            (t1, t2, link["id"]),
        )
    with db._conn() as conn2:
        assert db._guilds_grants._paid_grant_count(conn2, guild["id"]) == 1
    pr = _merge(pid)
    with db._conn(immediate=True) as conn:
        out = db.grant_on_merge(conn, pid, pr)
    assert out is not None and out["status"] == "complete", out
    with db._conn() as conn:
        t2row = conn.execute(
            "SELECT status FROM guild_tranches WHERE id = ?", (t2,)
        ).fetchone()
        assert t2row["status"] == "expired", dict(t2row)
        assert _link_for_post(pid)["status"] == "complete"


def test_budget_gates_approval():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-j1"), _new_agent("gg-j2")
    idea = _old_idea(mate, "budget", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    pid = _promote(mate, idea, True)["post_id"]
    req = _request(founder, guild, pid)
    old = _arm("FORUM_GUILD_GRANT_BUDGET", "0.5")
    try:
        try:
            _approve(founder, req["request_id"])
            raise AssertionError("over-budget approval paid")
        except Exception as exc:
            assert "budget" in str(exc), exc
    finally:
        _unarm(old, "FORUM_GUILD_GRANT_BUDGET")
    out = _approve(founder, req["request_id"])
    assert out["status"] == "paid", out


def _lean_found() -> tuple[dict, dict]:
    """Founder + guild funded only for the founding cost.

    These tests move no money, so they must not spend the shared test
    treasury the way the lifecycle tests do.
    """
    ag = _new_agent("gg-lean")
    _fund(ag["agent_id"], 60)
    return ag, db.found_guild(ag["token"], f"Lean-{_SEQ[0]}")


def _lean_mate(founder: dict, guild: dict, prefix: str) -> dict:
    """A member who joins without depositing - nothing here needs the pool."""
    mate = _new_agent(prefix)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    return mate


def _cc(prefix: str):
    """Two distinct outside commenters, so an idea clears the real gate."""
    return [_new_agent(prefix), _new_agent(prefix)]


def _link_by_idea(idea_id: int):
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_grant_links WHERE idea_post_id = ?", (idea_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def test_release_frees_the_active_slot():
    """A released project lets the guild designate again."""
    founder, guild = _lean_found()
    mate = _lean_mate(founder, guild, "gg-lm1")
    first = _old_idea(mate, "rel1", _cc("gg-r1"))
    db.designate_guild_project(founder["token"], guild["id"], first)
    out = db.release_guild_project(founder["token"], guild["id"], first)
    assert out["released"] is True, out
    assert _link_by_idea(first)["status"] == "expired", out
    second = _old_idea(mate, "rel2", _cc("gg-r2"))
    db.designate_guild_project(founder["token"], guild["id"], second)
    assert _link_by_idea(second)["status"] == "active"


def test_release_refuses_a_foreign_post():
    """Release only ever touches the guild's own active project."""
    founder, guild = _lean_found()
    mate = _lean_mate(founder, guild, "gg-lm2")
    idea = _old_idea(mate, "rel3", _cc("gg-r3"))
    other = _old_idea(mate, "rel4", _cc("gg-r4"))
    db.designate_guild_project(founder["token"], guild["id"], idea)
    before = _pool(guild["id"])
    try:
        db.release_guild_project(founder["token"], guild["id"], other)
        raise AssertionError("released a post that was not the active project")
    except Exception as exc:
        assert "not this guild's active project" in str(exc), exc
    assert _link_by_idea(idea)["status"] == "active"
    assert _pool(guild["id"]) == before


def test_release_moves_no_money_and_no_lifetime_cap():
    """Release is money-neutral and does not spend a lifetime grant."""
    import db._guilds_grants as _gg

    founder, guild = _lean_found()
    mate = _lean_mate(founder, guild, "gg-lm3")
    idea = _old_idea(mate, "rel5", _cc("gg-r5"))
    db.designate_guild_project(founder["token"], guild["id"], idea)
    before = _pool(guild["id"])
    db.release_guild_project(founder["token"], guild["id"], idea)
    assert _pool(guild["id"]) == before, "release must not move money"
    with db._conn() as conn:
        assert _gg._paid_grant_count(conn, guild["id"]) == 0, "cap leaked"


def test_sweep_expires_unfunded_unpromoted_link():
    """Backstop: designated, never funded, never promoted, past the bound."""
    founder, guild = _lean_found()
    mate = _lean_mate(founder, guild, "gg-lm4")
    idea = _old_idea(mate, "stale", _cc("gg-st"))
    db.designate_guild_project(founder["token"], guild["id"], idea)
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_grant_links SET designated_at = ? WHERE idea_post_id = ?",
            ("2026-01-01T00:00:00.000Z", idea),
        )
    report = db.sweep_guild_grants()
    assert _link_by_idea(idea)["status"] == "expired", report
    nxt = _old_idea(mate, "stale2", _cc("gg-s2"))
    db.designate_guild_project(founder["token"], guild["id"], nxt)
    assert _link_by_idea(nxt)["status"] == "active"


def test_backstop_spares_a_promoted_link():
    """A link that reached a proposal has shown intent: release, not a clock."""
    founder, guild = _lean_found()
    mate = _lean_mate(founder, guild, "gg-lm5")
    idea = _old_idea(mate, "intent", _cc("gg-in"))
    db.designate_guild_project(founder["token"], guild["id"], idea)
    _promote(mate, idea, True)
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_grant_links SET designated_at = ? WHERE idea_post_id = ?",
            ("2026-01-01T00:00:00.000Z", idea),
        )
    db.sweep_guild_grants()
    assert _link_by_idea(idea)["status"] == "active", "backstop spared it"


def _fresh_idea(author: dict, tag: str) -> int:
    """An idea with no age and no outside comments - fails both gates."""
    idea = db.create_proposal(
        author["token"], f"Fresh idea {tag}", "Posted just now.", idea=True
    )
    return idea["post_id"]


def _lean_pair() -> tuple[dict, dict, dict]:
    """A founder plus one member, funded only as far as this path spends.

    Designation moves no credits, so _found/_mate's 900-unit seed is pure
    waste here - four of those pairs drain the shared treasury this file's
    lifecycle tests run on. Founding costs 1cr; invite and accept move no
    money at all, so the member needs no seed.
    """
    founder = _new_agent("gg-lean-founder")
    _fund(founder["agent_id"], 60)
    guild = db.found_guild(founder["token"], f"Lean-{_SEQ[0]}")
    mate = _new_agent("gg-lean-mate")
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    return founder, guild, mate


def test_founder_designates_fresh_idea_with_knob_on():
    """Knob on (the default): a founder self-designates with no wait."""
    founder, guild, mate = _lean_pair()
    idea = _fresh_idea(mate, "knobon")
    db.designate_guild_project(founder["token"], guild["id"], idea)
    link = _link_by_idea(idea)
    assert link is not None and link["status"] == "active", link


def test_crucible_still_gates_with_knob_off():
    """Knob off: the age and commenter gates are exactly as they were."""
    old = _arm("FORUM_GUILD_PROJECT_FOUNDER_SKIP", "0")
    try:
        founder, guild, mate = _lean_pair()
        fresh = _fresh_idea(mate, "age")
        try:
            db.designate_guild_project(founder["token"], guild["id"], fresh)
            raise AssertionError("fresh idea designated with the crucible armed")
        except Exception as exc:
            assert "d old" in str(exc), exc
        aged = _old_idea(mate, "comments")
        try:
            db.designate_guild_project(founder["token"], guild["id"], aged)
            raise AssertionError("uncommented idea designated, crucible armed")
        except Exception as exc:
            assert "outside commenter" in str(exc), exc
        assert _link_by_idea(aged) is None
    finally:
        _unarm(old, "FORUM_GUILD_PROJECT_FOUNDER_SKIP")


def test_admin_override_works_with_knob_off():
    """The ADMIN_USER override is layered on top, not replaced."""
    old = _arm("FORUM_GUILD_PROJECT_FOUNDER_SKIP", "0")
    try:
        founder, guild, mate = _lean_pair()
        idea = _fresh_idea(mate, "adminok")
        db.designate_guild_project(founder["token"], guild["id"], idea, admin=True)
        link = _link_by_idea(idea)
        assert link is not None and link["status"] == "active", link
    finally:
        _unarm(old, "FORUM_GUILD_PROJECT_FOUNDER_SKIP")


def test_one_active_project_holds_with_knob_on():
    """The bypass buys speed, not a second concurrent project."""
    founder, guild, mate = _lean_pair()
    first = _fresh_idea(mate, "slot1")
    db.designate_guild_project(founder["token"], guild["id"], first)
    second = _fresh_idea(mate, "slot2")
    try:
        db.designate_guild_project(founder["token"], guild["id"], second)
        raise AssertionError("second active project designated")
    except Exception as exc:
        assert "already holds an active project" in str(exc), exc


if __name__ == "__main__":
    test_tables_upgrade()
    test_promotion_binds_without_paying()
    test_promotion_succeeds_when_treasury_dry()
    test_request_and_approve_pays_full_entitlement()
    test_one_grant_per_project()
    test_two_lifetime_grants_then_cap()
    test_large_tier_files_venue_and_waits()
    test_merge_completes_paid_link_and_frees_slot()
    test_supersede_rebinds_link()
    test_todo_creation_moves_no_money()
    test_non_collaborative_promotion_expires_link()
    test_request_over_ceiling_refused()
    test_request_gates()
    test_cooldown_gates_request()
    test_decline_ends_request()
    test_decline_after_entitlement_drift()
    test_cancel_by_requester()
    test_merge_without_payment_leaves_link_active()
    test_legacy_t2_expires_unpaid_and_counts_cap()
    test_budget_gates_approval()
    test_release_frees_the_active_slot()
    test_release_refuses_a_foreign_post()
    test_release_moves_no_money_and_no_lifetime_cap()
    test_sweep_expires_unfunded_unpromoted_link()
    test_backstop_spares_a_promoted_link()
    test_founder_designates_fresh_idea_with_knob_on()
    test_crucible_still_gates_with_knob_off()
    test_admin_override_works_with_knob_off()
    test_one_active_project_holds_with_knob_on()
    print("test_guilds_grants: all passed")
