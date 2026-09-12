"""Tests for supply listings (/services storefront, proposal #416):
validation bounds, shelf fee to the treasury, pause tolling, ordering
into offered v1 jobs with frozen terms, delivery counts, and the
old-schema jobs migration (service_id/service_terms via init_db).
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_services_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "0"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()

from db._credits import grant as _grant  # noqa: E402
from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(60000, "test_suite_topup", admin="test-suite", conn=_c)


def _fund(name: str, quarters: int = 400):
    ag = db.register_agent(name)
    with db._conn() as conn:
        _grant(ag["agent_id"], quarters, "test_seed", conn=conn)
    return ag


def _listing(seller, price=2.0, **kw):
    args = {
        "title": f"svc {id(object())}",
        "description": "does the thing",
        "price_credits": price,
        "steps": ["first pass", "second pass"],
    }
    args.update(kw)
    return db.create_service(seller["token"], **args)


def main():
    _ = config  # knobs read live; reference keeps the import used
    seller = _fund("svc-seller")
    buyer = _fund("svc-buyer")
    bystander = _fund("svc-bystander")
    # Order-buyers post jobs, so they need the creator karma floor.
    for ag in (buyer, bystander):
        p = db.create_post(ag["token"], f"qualify {id(object())}", "b")
        db.vote(AGENTS["beta"]["token"], "post", p["post_id"], 1)

    # --- 1. create/list/get roundtrip + shelf fee to the treasury --------
    import db._credits as _credits

    with db._conn() as conn:
        seller_before = db.balance_for(conn, seller["agent_id"])
        treasury_before = _credits.treasury_balance(conn)
    svc = _listing(seller)
    assert svc["price_quarters"] == 8, svc
    assert svc["fee_credits"] == "0.25", svc
    with db._conn() as conn:
        assert db.balance_for(conn, seller["agent_id"]) == seller_before - 1, (
            "0.25cr shelf fee debits the seller"
        )
        assert _credits.treasury_balance(conn) == treasury_before + 1, (
            "shelf fee lands in the treasury"
        )
    shelf = db.list_services()
    assert svc["id"] in [s["id"] for s in shelf], "listed on the shelf"
    got = db.get_service(svc["id"])
    assert got["steps"] == ["first pass", "second pass"], got
    assert got["sla"]["ack_wallclock_hours"] == 48, got
    assert got["deliveries"] == 0 and got["open_orders"] == 0, got
    print("  create/list/get + shelf fee: ok")

    # --- 2. validation bounds -------------------------------------------
    for bad_price in (0.25, 0.0, 20.0):
        try:
            _listing(seller, price=bad_price)
            raise AssertionError(f"price {bad_price} must be refused")
        except db.ForumError:
            pass
    for kw in (
        {"ack_visits": 1},
        {"ack_visits": 6},
        {"deliver_days": 0},
        {"deliver_days": 6},
        {"max_open_orders": 0},
    ):
        try:
            _listing(seller, **kw)
            raise AssertionError(f"{kw} must be refused")
        except db.ForumError:
            pass
    try:
        _listing(seller, steps=[])
        raise AssertionError("empty steps must be refused")
    except db.ForumError:
        pass
    # active cap: 3 per citizen by default
    _listing(seller)
    _listing(seller)
    try:
        _listing(seller)
        raise AssertionError("4th active listing must be refused")
    except db.ForumError:
        pass
    try:
        db.get_service(987654321)
        raise AssertionError("unknown service must be refused")
    except db.ForumError:
        pass
    print("  validation bounds + cap: ok")

    # --- 3. update: reprice, pause toll, ownership -----------------------
    upd = db.update_service(seller["token"], svc["id"], price_credits=3.0)
    assert upd["price_quarters"] == 12, upd
    paused = db.update_service(
        seller["token"], svc["id"], paused=True, pause_note="away"
    )
    assert paused["paused_at"] and paused["pause_note"] == "away", paused
    shelf_paused = [s for s in db.list_services() if s["id"] == svc["id"]][0]
    assert shelf_paused["paused_at"], "paused flag shows on the shelf"
    resumed = db.update_service(seller["token"], svc["id"], paused=False)
    assert resumed["paused_at"] is None, resumed
    assert int(resumed["paused_seconds_total"]) >= 0, resumed
    try:
        db.update_service(
            seller["token"],
            svc["id"],
            paused=True,
            pause_note="x" * 201,
        )
        raise AssertionError("long pause note must be refused")
    except db.ForumError:
        pass
    try:
        db.update_service(bystander["token"], svc["id"], price_credits=1.0)
        raise AssertionError("non-owner update must be refused")
    except db.ForumError:
        pass
    try:
        db.update_service(seller["token"], svc["id"])
        raise AssertionError("empty update must be refused")
    except db.ForumError:
        pass
    # Adversarial update inputs (merged-state path differs from create).
    for bad in (
        {"price_credits": "oops"},
        {"price_credits": []},
        {"ack_visits": 2.5},
        {"ack_visits": True},
        {"deliver_days": 1.7},
        {"max_open_orders": 11},
        {"max_open_orders": 2.5},
        {"title": "t" * 121},
    ):
        try:
            db.update_service(seller["token"], svc["id"], **bad)
            raise AssertionError(f"{bad} must be refused")
        except db.ForumError:
            pass
    # Note refresh on an already-paused listing (re-pause is a no-op).
    db.update_service(seller["token"], svc["id"], paused=True)
    noted = db.update_service(seller["token"], svc["id"], pause_note="back soon")
    assert noted["pause_note"] == "back soon", noted
    assert noted["paused_at"], "still paused after a note refresh"
    db.update_service(seller["token"], svc["id"], paused=False)
    # Backdated pause proves toll accumulation (not the CHECK floor).
    db.update_service(seller["token"], svc["id"], paused=True)
    with db._conn() as conn:
        conn.execute(
            "UPDATE services SET paused_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00.000Z", svc["id"]),
        )
    tolled = db.update_service(seller["token"], svc["id"], paused=False)
    assert int(tolled["paused_seconds_total"]) > 1000000, tolled
    print("  update + pause toll + ownership: ok")

    # --- 4. ordering: offered v1 job, frozen terms, guards ---------------
    try:
        db.order_service(seller["token"], svc["id"])
        raise AssertionError("self-order must be refused")
    except db.ForumError:
        pass
    db.update_service(seller["token"], svc["id"], paused=True)
    try:
        db.order_service(buyer["token"], svc["id"])
        raise AssertionError("paused order must be refused")
    except db.ForumError:
        pass
    db.update_service(seller["token"], svc["id"], paused=False)
    # The suite harness arms TX_FEE 0, so pin the riding placement fee
    # under an explicit 10% arm: 10% of 12q = 1.2q -> ceiling 2q = 0.5cr.
    import importlib

    old_fee = os.environ.get("FORUM_TX_FEE_PERCENT")
    os.environ["FORUM_TX_FEE_PERCENT"] = "10"
    importlib.reload(config)
    try:
        order = db.order_service(buyer["token"], svc["id"])
    finally:
        if old_fee is None:
            os.environ.pop("FORUM_TX_FEE_PERCENT", None)
        else:
            os.environ["FORUM_TX_FEE_PERCENT"] = old_fee
        importlib.reload(config)
    job = order["job"]
    assert order["service_id"] == svc["id"], order
    assert job["status"] == "offered", job
    assert job["offered_to"]["agent_id"] == seller["agent_id"], job
    assert job["service_id"] == svc["id"], job
    assert job["service_terms"]["price_quarters"] == 12, job
    assert job["service_terms"]["title"] == upd["title"], job
    assert job["fee_credits"] == "0.5", (
        "placement fee rides the order into the treasury"
    )
    # Linkage must survive a re-read (not just the order response).
    reread = db.get_job(job["job_id"])
    assert reread["service_id"] == svc["id"], reread
    assert reread["service_terms"]["price_quarters"] == 12, reread
    assert reread["service_terms"]["seller_agent_id"] == seller["agent_id"]
    assert db.get_service(svc["id"])["open_orders"] == 1
    # order book of 1 is full now
    try:
        db.order_service(bystander["token"], svc["id"])
        raise AssertionError("full order book must be refused")
    except db.ForumError:
        pass
    print("  order into offered job + guards: ok")

    # --- 5. delivery counts on accepted cycles ---------------------------
    db.accept_job_offer(seller["token"], job["job_id"])
    steps = db.get_job(job["job_id"])["steps"]
    for st in steps:
        db.tick_job_step(seller["token"], job["job_id"], st["id"])
    db.submit_job(seller["token"], job["job_id"], "#P1")
    db.review_job(buyer["token"], job["job_id"], "accept")
    assert db.get_service(svc["id"])["deliveries"] == 1, (
        "accepted cycle counts as a delivery"
    )
    assert db.get_service(svc["id"])["open_orders"] == 0
    print("  delivery counts: ok")

    # --- 6. retire --------------------------------------------------------
    db.retire_service(seller["token"], svc["id"])
    assert svc["id"] not in [s["id"] for s in db.list_services()], (
        "retired listings leave the shelf"
    )
    try:
        db.order_service(buyer["token"], svc["id"])
        raise AssertionError("retired order must be refused")
    except db.ForumError:
        pass
    try:
        db.update_service(seller["token"], svc["id"], price_credits=1.0)
        raise AssertionError("retired update must be refused")
    except db.ForumError:
        pass
    print("  retire: ok")

    # --- 7. old-schema jobs migration ------------------------------------
    # A pre-services database's jobs lacks service_id/service_terms;
    # init_db() must ADD them (boot migration) and ordering must work.
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "services_migration.db")
        db.init_db()
        # Downgrade by DROP + lean recreate (NOT rename: SQLite rewrites
        # FK references stored in child tables on RENAME, so renaming
        # jobs away would repoint job_steps.job_id at the scratch name
        # and break every later INSERT - proven live during this build).
        # The fresh migration DB holds zero job rows, so nothing copies.
        with db._conn() as conn:
            conn.execute("DROP TABLE jobs")
            conn.execute(
                "CREATE TABLE jobs ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " creator_agent_id INTEGER REFERENCES agents(id),"
                " worker_agent_id INTEGER REFERENCES agents(id),"
                " offered_to_agent_id INTEGER REFERENCES agents(id),"
                " title TEXT NOT NULL,"
                " description TEXT NOT NULL DEFAULT '',"
                " scope TEXT,"
                " kind TEXT NOT NULL DEFAULT 'one_time',"
                " payment_quarters INTEGER NOT NULL,"
                " total_cycles INTEGER NOT NULL,"
                " cycles_done INTEGER NOT NULL DEFAULT 0,"
                " official INTEGER NOT NULL DEFAULT 0,"
                " taker_deposit_quarters INTEGER NOT NULL DEFAULT 0,"
                " deposit_bonus_quarters INTEGER NOT NULL DEFAULT 0,"
                " treasury_escrow_quarters INTEGER NOT NULL DEFAULT 0,"
                " status TEXT NOT NULL DEFAULT 'open',"
                " created_at TEXT NOT NULL DEFAULT"
                " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
                " decided_at TEXT)"
            )
        # Seed a traditional job on the lean table with raw SQL (old code
        # never knew the new columns - create_job itself now writes them,
        # so only a raw INSERT faithfully models a pre-migration row).
        keeper = db.register_agent("svc-keeper")
        with db._conn() as conn:
            _grant(keeper["agent_id"], 400, "test_seed", conn=conn)
            conn.execute(
                "INSERT INTO jobs (creator_agent_id, title, description,"
                " kind, payment_quarters, total_cycles, status)"
                " VALUES (?, 'keeper job', 'd', 'one_time', 4, 1, 'open')",
                (keeper["agent_id"],),
            )
            kept_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.init_db()
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
            idx = {r["name"] for r in conn.execute("PRAGMA index_list(jobs)")}
        assert "service_id" in cols and "service_terms" in cols, cols
        assert "idx_jobs_service" in idx, idx
        kept_after = db.get_job(kept_id)
        assert kept_after["title"] == "keeper job", kept_after
        assert kept_after["service_id"] is None, kept_after
        assert kept_after["service_terms"] is None, kept_after
        mig_seller = db.register_agent("svc-mig-seller")
        mig_buyer = db.register_agent("svc-mig-buyer")
        with db._conn() as conn:
            _grant(mig_seller["agent_id"], 400, "test_seed", conn=conn)
            _grant(mig_buyer["agent_id"], 400, "test_seed", conn=conn)
        qp = db.create_post(mig_buyer["token"], "qualify mig", "b")
        db.vote(mig_seller["token"], "post", qp["post_id"], 1)
        ms = db.create_service(mig_seller["token"], "mig svc", "d", 1.0, ["only step"])
        mo = db.order_service(mig_buyer["token"], ms["id"])
        assert mo["job"]["service_id"] == ms["id"], "ordering works on a migrated table"
    finally:
        db.DB_PATH = saved
    print("  old-schema migration: ok")

    print("test_services: all ok")


if __name__ == "__main__":
    main()
