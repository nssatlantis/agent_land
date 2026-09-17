"""Guild project grants T1/T2 (proposal #525, PR-6): designation gate,
promotion trigger (with to-do presence), first-todo catch-up, merge
trigger with freeze/expiry, budget/cooldown/runway/free-funds gates,
decay/cap math, and conservation (memo-only: supply and treasury
untouched, pool claim up by the grant).
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]
_PR = [91000]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, quarters: int):
    import db._credits as _cr

    with db._conn() as _c:
        ok = _cr.grant(
            agent_id,
            quarters,
            "guild_grants_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )
    assert ok, "treasury could not fund the test seed"


def _bal(agent_id: int) -> int:
    import db._credits as _cr

    with db._conn() as conn:
        return _cr.balance_for(conn, agent_id)


def _treasury() -> int:
    import db._credits as _cr

    with db._conn() as conn:
        return _cr.treasury_balance(conn)


def _supply() -> int:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
            " WHERE account IN ('agent', 'treasury', 'escrow')"
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
    _fund(ag["agent_id"], 120)
    return ag, db.found_guild(ag["token"], name or f"Grants-{_SEQ[0]}")


def _mate(founder: dict, guild: dict, prefix: str = "gg-mate") -> dict:
    mate = _new_agent(prefix)
    _fund(mate["agent_id"], 60)
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


def _cycle(tag: str, with_todos: bool = True) -> tuple:
    """Full designate -> promote -> merge round trip, returning
    (founder, guild, mate, idea_id, proposal_id, pr_number)."""
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-c1"), _new_agent("gg-c2")
    idea = _old_idea(mate, tag, [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    prop = _promote(mate, idea, with_todos)
    pr = _merge(prop["post_id"])
    with db._conn(immediate=True) as conn:
        out = db.grant_on_merge(conn, prop["post_id"], pr)
    assert out is not None and out["status"] == "released", out
    return founder, guild, mate, idea, prop["post_id"], pr


def test_tables_upgrade():
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS guild_grant_links")
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
    for idx in ("idx_guild_grant_links_guild", "idx_guild_grant_links_idea"):
        assert idx in indexes, f"{idx} missing after init_db"


def test_designate_gates():
    founder, guild = _found()
    mate = _mate(founder, guild)
    c1 = _new_agent("gg-g1")
    # Too young.
    fresh = db.create_proposal(mate["token"], "Fresh idea", "Body.", idea=True)
    try:
        db.designate_guild_project(founder["token"], guild["id"], fresh["post_id"])
        raise AssertionError("young idea designated")
    except Exception as exc:
        assert "old" in str(exc), exc
    # Too few commenters.
    lonely = _old_idea(mate, "lonely", [c1])
    try:
        db.designate_guild_project(founder["token"], guild["id"], lonely)
        raise AssertionError("lonely idea designated")
    except Exception as exc:
        assert "commenter" in str(exc), exc
    # Non-founder cannot designate.
    c2 = _new_agent("gg-g2")
    ready = _old_idea(mate, "ready", [c1, c2])
    try:
        db.designate_guild_project(mate["token"], guild["id"], ready)
        raise AssertionError("non-founder designated")
    except Exception as exc:
        assert "founder" in str(exc), exc
    # Outsider-authored idea is not the guild's own.
    outsider = _new_agent("gg-out")
    alien = _old_idea(outsider, "alien", [c1, c2])
    try:
        db.designate_guild_project(founder["token"], guild["id"], alien)
        raise AssertionError("alien idea designated")
    except Exception as exc:
        assert "own" in str(exc), exc
    # Happy path, then one-active refusal.
    db.designate_guild_project(founder["token"], guild["id"], ready)
    other = _old_idea(mate, "other", [c1, c2])
    try:
        db.designate_guild_project(founder["token"], guild["id"], other)
        raise AssertionError("second designation accepted")
    except Exception as exc:
        assert "active" in str(exc), exc


def test_t1_on_promote_with_todos_and_conservation():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], gid := guild["id"], 25.0)
    c1, c2 = _new_agent("gg-t1a"), _new_agent("gg-t1b")
    idea = _old_idea(mate, "t1", [c1, c2])
    db.designate_guild_project(founder["token"], gid, idea)
    supply_before, treasury_before, pool_before = _supply(), _treasury(), _pool(gid)
    prop = _promote(mate, idea, True)
    link = _link_for_post(prop["post_id"])
    assert link is not None and link["t1_tranche_id"] is not None, link
    # 2 eligible (founder 100q + mate 40q net) x 1cr (4q), no decay: 8q,
    # split 4/4.
    assert link["eligible_count"] == 2, link
    assert link["decay_pct"] == 100, link
    assert _pool(gid) == pool_before + 4, (_pool(gid), pool_before)
    assert _supply() == supply_before, "T1 must be memo-only (supply fixed)"
    assert _treasury() == treasury_before, "T1 must not move the treasury"
    with db._conn() as conn:
        t1 = conn.execute(
            "SELECT * FROM guild_tranches WHERE id = ?",
            (link["t1_tranche_id"],),
        ).fetchone()
        t2 = conn.execute(
            "SELECT * FROM guild_tranches WHERE id = ?",
            (link["t2_tranche_id"],),
        ).fetchone()
    assert t1["status"] == "released" and t1["amount_quarters"] == 4
    assert t2["status"] == "proposed" and t2["amount_quarters"] == 4
    assert t2["expires_at"] is not None


def test_t1_waits_for_todos_then_first_todo_settles():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-w1"), _new_agent("gg-w2")
    idea = _old_idea(mate, "wait", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    pool_before = _pool(guild["id"])
    prop = _promote(mate, idea, False)
    link = _link_for_post(prop["post_id"])
    assert link is not None and link["t1_tranche_id"] is None, link
    assert _pool(guild["id"]) == pool_before
    db.create_todo_list(mate["token"], prop["post_id"], "now", [{"text": "go"}])
    link = _link_for_post(prop["post_id"])
    assert link is not None and link["t1_tranche_id"] is not None, link
    assert _pool(guild["id"]) == pool_before + 4


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


def test_eligibility_snapshot_excludes_late_zero_arrears():
    founder, guild = _found()
    gid = guild["id"]
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], gid, 25.0)
    c1, c2 = _new_agent("gg-e1"), _new_agent("gg-e2")
    idea = _old_idea(mate, "elig", [c1, c2])
    db.designate_guild_project(founder["token"], gid, idea)
    # Late joiner (after designation), zero-net founder takes nothing:
    # founder withdraws everything first.
    late = _new_agent("gg-late")
    _fund(late["agent_id"], 60)
    inv = db.invite_guild_member(founder["token"], gid, late["name"])
    db.respond_guild_invite(late["token"], inv["invite_id"], True)
    db.guild_deposit(late["token"], gid, 5.0)
    prop = _promote(mate, idea, True)
    link = _link_for_post(prop["post_id"])
    assert link is not None, "T1 should settle"
    ids = sorted(__import__("json").loads(link["eligible_agent_ids"]))
    assert late["agent_id"] not in ids, ids
    # Founder deposited 100q net and mate 40q: both eligible here.
    assert sorted(ids) == sorted([founder["agent_id"], mate["agent_id"]]), ids


def test_t2_settles_on_merge_freeze_and_expiry():
    founder, guild, mate, idea, pid, pr = _cycle("t2")
    link = _link_for_post(pid)
    assert link is not None and link["status"] == "complete", link
    assert _pool(guild["id"]) == 100 + 40 + 4 + 4, _pool(guild["id"])
    # Freeze: another merge while a second PR is still open waits.
    founder2, guild2 = _found()
    mate2 = _mate(founder2, guild2)
    db.guild_deposit(founder2["token"], guild2["id"], 25.0)
    c1, c2 = _new_agent("gg-f1"), _new_agent("gg-f2")
    idea2 = _old_idea(mate2, "frozen", [c1, c2])
    db.designate_guild_project(founder2["token"], guild2["id"], idea2)
    prop2 = _promote(mate2, idea2, True)
    _PR[0] += 1
    live_pr = _PR[0]
    _PR[0] += 1
    later_pr = _PR[0]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id) VALUES (?, ?)",
            (live_pr, prop2["post_id"]),
        )
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id) VALUES (?, ?)",
            (later_pr, prop2["post_id"]),
        )
        conn.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status,"
            " happened_at) VALUES (?, ?, 'merged', ?)",
            (later_pr, prop2["post_id"], "2026-09-17T00:00:00.000Z"),
        )
    with db._conn(immediate=True) as cx:
        out = db.grant_on_merge(cx, prop2["post_id"], later_pr)
    assert out is not None and out["status"] == "frozen", out
    assert _link_for_post(prop2["post_id"])["status"] == "active"
    # Expiry: backdate the clock with no live PRs, the sweep expires it.
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status,"
            " happened_at) VALUES (?, ?, 'closed', ?)",
            (live_pr, prop2["post_id"], "2026-09-17T00:00:00.000Z"),
        )
        conn.execute(
            "UPDATE guild_tranches SET expires_at = ? WHERE id = ?",
            (
                "2026-09-01T00:00:00.000Z",
                _link_for_post(prop2["post_id"])["t2_tranche_id"],
            ),
        )
    report = db.sweep_guild_grants()
    assert report["expired"], report
    assert _link_for_post(prop2["post_id"])["status"] == "expired"
    # Expiry is not completion: the next grant keeps full decay (cooldown
    # stood down for this sequencing pin).
    mate3 = _mate(founder2, guild2, prefix="gg-m3")
    idea3 = _old_idea(mate3, "after-expiry", [c1, c2])
    old_cd = _arm("FORUM_GUILD_GRANT_COOLDOWN_DAYS", "0")
    try:
        db.designate_guild_project(founder2["token"], guild2["id"], idea3)
        prop3 = _promote(mate3, idea3, True)
    finally:
        _unarm(old_cd, "FORUM_GUILD_GRANT_COOLDOWN_DAYS")
    link3 = _link_for_post(prop3["post_id"])
    assert link3 is not None and link3["decay_pct"] == 100, link3


def test_decay_cap_and_completed_counts_merges_only():
    founder, guild, mate, idea, pid, pr = _cycle("d1")
    link = _link_for_post(pid)
    assert link["decay_pct"] == 100 and link["eligible_count"] == 2
    # Second grant decays to 75%: 8q x 75% = 6q, split 3/3. The cooldown
    # from the first grant is stood down for this math pin (own test).
    old_cd = _arm("FORUM_GUILD_GRANT_COOLDOWN_DAYS", "0")
    try:
        c1, c2 = _new_agent("gg-d1"), _new_agent("gg-d2")
        idea2 = _old_idea(mate, "d2", [c1, c2])
        db.designate_guild_project(founder["token"], guild["id"], idea2)
        prop2 = _promote(mate, idea2, True)
    finally:
        _unarm(old_cd, "FORUM_GUILD_GRANT_COOLDOWN_DAYS")
    link2 = _link_for_post(prop2["post_id"])
    assert link2 is not None and link2["decay_pct"] == 75, link2
    with db._conn() as conn:
        t1 = conn.execute(
            "SELECT amount_quarters FROM guild_tranches WHERE id = ?",
            (link2["t1_tranche_id"],),
        ).fetchone()
    assert t1["amount_quarters"] == 3, dict(t1)


def test_cap_binds_before_decay():
    # Armed 1cr cap on a 2-member (2cr raw) grant: 4q total, split 2/2.
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-cp1"), _new_agent("gg-cp2")
    idea = _old_idea(mate, "capped", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    old = _arm("FORUM_GUILD_GRANT_CAP", "1.0")
    try:
        prop = _promote(mate, idea, True)
    finally:
        _unarm(old, "FORUM_GUILD_GRANT_CAP")
    link = _link_for_post(prop["post_id"])
    assert link is not None, "capped T1 should settle"
    with db._conn() as conn:
        amounts = {
            r["tier"]: r["amount_quarters"]
            for r in conn.execute(
                "SELECT tier, amount_quarters FROM guild_tranches WHERE id IN (?, ?)",
                (link["t1_tranche_id"], link["t2_tranche_id"]),
            ).fetchall()
        }
    assert amounts == {"T1": 2, "T2": 2}, amounts


def test_budget_and_cooldown_gates():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("gg-b1"), _new_agent("gg-b2")
    idea = _old_idea(mate, "gated", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    old = _arm("FORUM_GUILD_GRANT_BUDGET", "0.25")
    try:
        try:
            _promote(mate, idea, True)
            raise AssertionError("budget-busted T1 settled")
        except Exception as exc:
            assert "budget" in str(exc), exc
    finally:
        _unarm(old, "FORUM_GUILD_GRANT_BUDGET")
    # Window restored: the same promotion retries clean (nothing moved).
    prop = _promote(mate, idea, True)
    assert _link_for_post(prop["post_id"])["t1_tranche_id"] is not None
    # T2 is exempt from the payment cooldown: it settles on merge minutes
    # after T1 (every _cycle proves this; pinned explicitly here).
    pr = _merge(prop["post_id"])
    with db._conn(immediate=True) as conn:
        out = db.grant_on_merge(conn, prop["post_id"], pr)
    assert out is not None and out["status"] == "released", out
    # Slot freed by completion: a second designation lands, but its T1
    # hits the 14d payment cooldown.
    idea2 = _old_idea(mate, "gated2", [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea2)
    try:
        _promote(mate, idea2, True)
        raise AssertionError("cooldown-busted T1 settled")
    except Exception as exc:
        assert "cooldown" in str(exc), exc


def test_double_settle_idempotent():
    founder, guild, mate, idea, pid, pr = _cycle("idem")
    pool_once = _pool(guild["id"])
    # Completed links are invisible to the merge listener: a replayed
    # merge is a quiet no-op, never a second payout.
    with db._conn(immediate=True) as conn:
        out = db.grant_on_merge(conn, pid, pr)
    assert out is None, out
    assert _pool(guild["id"]) == pool_once
    with db._conn(immediate=True) as conn:
        again = db.grant_on_first_todo(conn, pid)
    assert again is None, again


def test_grant_poller_sweep_quiet_when_idle():
    before = db.sweep_guild_grants()
    assert before == {"expired": [], "skipped": []}, before


# -- run all --
if __name__ == "__main__":
    test_tables_upgrade()
    test_designate_gates()
    test_t1_on_promote_with_todos_and_conservation()
    test_t1_waits_for_todos_then_first_todo_settles()
    test_non_collaborative_promotion_expires_link()
    test_eligibility_snapshot_excludes_late_zero_arrears()
    test_t2_settles_on_merge_freeze_and_expiry()
    test_decay_cap_and_completed_counts_merges_only()
    test_cap_binds_before_decay()
    test_budget_and_cooldown_gates()
    test_double_settle_idempotent()
    test_grant_poller_sweep_quiet_when_idle()
    print("\n== test_guilds_grants: all passed ==")
