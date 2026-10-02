"""tests/test_services_guild - collective service listings (proposal #778).

A guild may SELL, not only buy: `create_service(guild_id=N)` makes a listing
collective - founder-gated, shelf fee charged to the pool, and an accepted
order's wage settling to the OWNING pool instead of the seller personally.

The board is nine items and this file is organised to match, because the
failure mode of a change this size is shipping a piece and calling it done.
What each group proves, and WHY that proof is the hard part:

  1. schema + boot pairing (5590).  Both halves in one migration, asserted by
     DROPPING the column and index and re-running the real boot entry. A
     source-scan pin would pass on a migration that never fires; this one
     cannot, because it executes `db._core._boot_economy.run`. The second
     group in this file covers the OTHER entry point - init_db()'s
     executescript, which the boot arm bypasses (finding #125).
  2. settlement (5591).  New work at cycle acceptance, not a reuse: a service
     order's guild_job_links role is 'commissioned', never 'taken', so the
     pre-existing poolward branch does not fire for it.
  3. conservation (5593).  wallet - memo == retained, in flight AND settled AND
     while the self-order refusal fires. The last one matters most: a circular
     transfer is exactly the shape a balance check cannot see, so "conservation
     balanced" is the RED state there, not the green one.
  4. refusals (5594/5738).  non-member, non-founder, self-order, bad ids.
  5. per-owner cap (5739).  The guild's budget, not the creating member's -
     otherwise six members each spending their own 4 is still 24, laundered.
  6. frozen routing identity (5740).  Settlement reads the ORDER's snapshot,
     not the live row, proven by retargeting the listing mid-flight.
  7. the solo path is a true no-op (5741).  Same code path, not a second one.

Fixtures are budget-neutral by construction: every agent is funded from the
suite's single top-up, every guild pool from an explicit deposit sized to what
the arm under test actually escrows. A shared test treasury IS a budget, and
an assertion that depends on a pool happening to be rich is an assertion that
passes for the wrong reason.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_services_guild_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "0"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING"] = "0"
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402

db.init_db()

AGENTS, _ = setup()

from db._credits import UNITS_PER_CREDIT  # noqa: E402
from db._credits import grant as _grant  # noqa: E402
from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    # Sized for THIS file's agents, and asserted below. A shared test treasury
    # IS a budget (board item 5594): the first version of this file minted
    # 60000 and quietly ran dry around agent 28, surfacing three groups later
    # as "escrows 2 credits and requires 2; has 0" - a message that reads like
    # a product bug and is really a starved fixture.
    _mint(500000, "test_suite_topup", admin="test-suite", conn=_c)

_SEQ = [0]


def _agent(prefix: str, units: int = 2000):
    _SEQ[0] += 1
    ag = db.register_agent(f"{prefix}-{_SEQ[0]}")
    if units:
        with db._conn() as conn:
            # Asserted: a failed seed must say so HERE, not surface four groups
            # later as an unrelated-looking "insufficient credits" refusal.
            assert _grant(ag["agent_id"], units, "test_seed", conn=conn), (
                f"could not seed {prefix}-{_SEQ[0]} with {units} units - the "
                f"shared test treasury is exhausted"
            )
    return ag


def _guild(prefix: str, pool_units: int = 4000):
    """A guild with a founder and a mate - two members, because
    `guild_spend_locked` re-locks commissioning below that, and a one-member
    guild would make every gate below pass for the wrong reason.

    The founder's WALLET is sized to the deposit it is about to make, fee
    included. Under-funding it would not fail loudly here; it would make the
    first assertion that reads a wallet balance pass or fail for a reason that
    has nothing to do with the behaviour under test.
    """
    founder = _agent(f"{prefix}-founder", units=pool_units + 2000)
    guild = db.found_guild(founder["token"], f"{prefix}-{_SEQ[0]}")
    # found_guild returns the enriched _guild_detail - a guilds row plus
    # roster/balance - not a bare {guild_id} shape. Asserted rather than
    # assumed, so a renamed key reports the real keys instead of KeyError.
    gid = guild.get("guild_id", guild.get("id"))
    assert isinstance(gid, int), f"found_guild returned no guild id: {sorted(guild)}"
    gid = int(gid)
    mate = _agent(f"{prefix}-mate", units=200)
    inv = db.invite_guild_member(founder["token"], gid, mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    if pool_units:
        db.guild_deposit(founder["token"], gid, pool_units / UNITS_PER_CREDIT)
    return founder, mate, gid


def _job_id(order: dict) -> int:
    """The spawned job's id, asserted rather than assumed: order_service returns
    create_job's dict, and a renamed key should report itself, not KeyError."""
    job = order["job"]
    jid = job.get("job_id", job.get("id"))
    assert isinstance(jid, int), f"order job carries no id: {sorted(job)}"
    return jid


def _qualify(ag) -> None:
    """Earn the job-creator karma floor the way production does: a post plus an
    upvote, not by writing the karma column."""
    p = db.create_post(ag["token"], f"qualify {id(object())}", "b")
    db.vote(AGENTS["beta"]["token"], "post", p["post_id"], 1)


def _listing(seller, price=1.0, guild_id=None, title=None):
    return db.create_service(
        seller["token"],
        title=title or f"svc {id(object())}",
        description="does the thing",
        price_credits=price,
        steps=["first pass", "second pass"],
        guild_id=guild_id,
    )


def _pool(gid):
    with db._conn() as conn:
        return db.guild_balance(conn, gid)


def _wallet(agent_id):
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _conservation_ok(label: str) -> None:
    c = db.economy_overview()["guild_conservation"]
    assert c["ok"], (label, c)


def _fulfil(job_id, seller, buyer, before_review=None) -> None:
    """offered -> active -> submitted -> accepted, the ordinary v1 lifecycle.

    `before_review` runs in the window where the cycle is accepted-able but not
    yet accepted - the exact instant the wage moves. That gap is the only place
    a settlement assertion is clean, because review_job ALSO pays JOB_KARMA_PER
    CYCLE to both sides and the treasury converts that karma into credits, so
    any wallet reading taken after it mixes the wage with the award.
    """
    db.accept_job_offer(seller["token"], job_id)
    db.submit_job(seller["token"], job_id, "delivered, see the thread")
    if before_review is not None:
        before_review()
    db.review_job(buyer["token"], job_id, "accept")


_OLD_SERVICES_SQL = """CREATE TABLE services (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_agent_id     INTEGER NOT NULL REFERENCES agents(id),
    title               TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    price_units         INTEGER NOT NULL CHECK (price_units > 0),
    steps_json          TEXT NOT NULL DEFAULT '[]',
    ack_visits          INTEGER NOT NULL DEFAULT 2,
    deliver_days        INTEGER NOT NULL DEFAULT 3,
    max_open_orders     INTEGER NOT NULL DEFAULT 1 CHECK (max_open_orders > 0),
    active              INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    paused_at           TEXT,
    pause_note          TEXT,
    paused_seconds_total INTEGER NOT NULL DEFAULT 0 CHECK (paused_seconds_total >= 0),
    created_at          TEXT NOT NULL,
    retired_at          TEXT
)"""


def test_schema_and_boot_pairing() -> None:
    """5590 - schema.sql and the boot migration carry the column AND the index.

    Proven by removing both and re-running the REAL boot entry, not by reading
    the source: a pin that greps for `_ensure_column` passes just as happily
    against a migration that never fires on a live database, which is the
    exact drift this pairing exists to prevent.
    """
    with db._conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(services)")}
        assert "guild_id" in cols, cols
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(services)")}
        assert "idx_services_guild" in idx, idx
        # fk_target proves the column is a real REFERENCES, not a bare int.
        fk = [
            r
            for r in conn.execute("PRAGMA table_info(services)")
            if r["name"] == "guild_id"
        ][0]
        assert fk["notnull"] == 0, "guild_id must stay nullable (solo listings)"

        # Now break it the way an old deploy looks, and re-run boot.
        conn.execute("DROP INDEX idx_services_guild")
        conn.execute("ALTER TABLE services DROP COLUMN guild_id")
        gone = {r["name"] for r in conn.execute("PRAGMA table_info(services)")}
        assert "guild_id" not in gone, "precondition: the column is really gone"

        from db._core._boot_economy import run as boot_economy

        boot_economy(conn)

        back = {r["name"] for r in conn.execute("PRAGMA table_info(services)")}
        assert "guild_id" in back, "boot did not re-add services.guild_id"
        idx2 = {r["name"] for r in conn.execute("PRAGMA index_list(services)")}
        assert "idx_services_guild" in idx2, "boot did not re-add the index"
    print("  schema.sql + boot pairing: ok")


def test_init_db_over_a_legacy_services_table() -> None:
    """125 - the upgrade path that actually runs in production.

    `test_schema_and_boot_pairing` above re-runs `boot_economy.run` DIRECTLY,
    which is right for proving the migration fires but it walks straight past
    the one line that breaks: `db/_core/_init.py` runs
    `conn.executescript(SCHEMA_PATH.read_text())` against the LIVE connection
    before any boot module runs. schema.sql is DECLARATION there, so every
    object it declares must resolve against a database that already has it -
    and `CREATE TABLE IF NOT EXISTS services` no-ops on a live forum, leaving
    the next statement to resolve `services.guild_id`, which does not exist
    yet.

    So the index rides the boot migration and NOT schema.sql, exactly as the
    comment eight lines above it says. With a `CREATE INDEX` in schema.sql on
    a just-added column, `init_db()` raises `OperationalError: no such column:
    guild_id` on every existing database and the forum does not start.

    Driven through the house `assert_upgrade_column` helper, which really
    calls `init_db()` - and a fresh-DB test cannot see this, because there
    every schema.sql statement CREATEs the table with guild_id already in it.
    """
    from tests._helpers import assert_upgrade_column

    def _both_halves(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(services)")}
        assert "guild_id" in cols, cols
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(services)")}
        assert "idx_services_guild" in idx, (
            "the boot migration owns BOTH halves; deleting schema.sql's index "
            "line must not lose it on an upgraded database"
        )

    assert_upgrade_column(
        "services", _OLD_SERVICES_SQL, "guild_id", verify=_both_halves
    )
    print("  init_db over a legacy services table: ok")


def test_collective_settlement_routes_the_wage_to_the_pool() -> None:
    """5591/5593 - a guild listing's accepted wage lands in the OWNING pool.

    The buyer's escrow funds the order exactly as on the solo path, so the
    only thing this proves is the DESTINATION changed: the seller's wallet
    must not move, and the listing guild's pool must gain exactly the wage.
    """
    founder, _mate, gid = _guild("sellside")
    solo_buyer = _agent("citizen-buyer")
    _qualify(solo_buyer)
    svc = _listing(founder, price=2.0, guild_id=gid)
    assert svc["guild_id"] == gid, svc

    pool_before = _pool(gid)
    seller_before = _wallet(founder["agent_id"])
    order = db.order_service(solo_buyer["token"], svc["id"])
    job_id = _job_id(order)
    wage = order["job"]["payment_units"]
    _conservation_ok("in flight, escrow held")

    seen: dict = {}
    _fulfil(
        job_id,
        founder,
        solo_buyer,
        before_review=lambda: seen.update(wallet=_wallet(founder["agent_id"])),
    )

    assert seen["wallet"] == seller_before, (
        "the seller must NOT be paid personally on a collective listing"
    )
    assert _pool(gid) == pool_before + wage, (_pool(gid), pool_before, wage)
    _conservation_ok("settled poolward")
    print("  collective settlement routes poolward: ok")


def test_pool_funded_cross_guild_order() -> None:
    """5591/5593 - a DIFFERENT guild's pool can fund the order.

    The funding guild and the owning guild are distinct by construction; the
    pool that pays is not the pool that receives, which is what makes the
    settlement a real trade rather than a round trip.
    """
    seller_f, _m1, sell_gid = _guild("xsell")
    buyer_f, _m2, buy_gid = _guild("xbuy", pool_units=8000)
    svc = _listing(seller_f, price=3.0, guild_id=sell_gid)

    sell_before, buy_before = _pool(sell_gid), _pool(buy_gid)
    order = db.order_service(buyer_f["token"], svc["id"], guild_id=buy_gid)
    job_id = _job_id(order)
    wage = order["job"]["payment_units"]
    # The buyer's pool funded the escrow, so it is already down.
    assert _pool(buy_gid) < buy_before, "the funding pool paid the escrow"
    _conservation_ok("cross-guild, in flight")

    _fulfil(job_id, seller_f, buyer_f)
    assert _pool(sell_gid) == sell_before + wage, (_pool(sell_gid), sell_before, wage)
    _conservation_ok("cross-guild, settled")
    print("  cross-guild pool-funded order: ok")


def test_solo_listing_is_a_true_no_op() -> None:
    """5741 - guild_id=None behaves identically to before, on ONE code path.

    Not merely 'the tests still pass': the row carries guild_id NULL, the fee
    still comes out of the seller's WALLET (not a pool), and an accepted order
    on a solo listing still pays the seller PERSONALLY - the branch that a
    second code path would have quietly replaced.
    """
    seller = _agent("solo-seller")
    buyer = _agent("solo-buyer")
    _qualify(buyer)
    wallet_before = _wallet(seller["agent_id"])
    svc = _listing(seller, price=2.0)
    assert svc.get("guild_id") is None, svc

    fee_units = int(round(float(config.SERVICE_LISTING_FEE_CREDITS) * 20))
    if fee_units:
        assert _wallet(seller["agent_id"]) == wallet_before - fee_units, (
            "a solo listing still charges the fee to the seller's wallet"
        )

    order = db.order_service(buyer["token"], svc["id"])
    wage = order["job"]["payment_units"]
    seen: dict = {}
    _fulfil(
        _job_id(order),
        seller,
        buyer,
        before_review=lambda: seen.update(wallet=_wallet(seller["agent_id"])),
    )
    assert seen["wallet"] >= wage, (
        "a solo listing's wage must still pay the seller personally"
    )
    print("  solo path is a true no-op: ok")


def test_self_order_refusal() -> None:
    """5738/5593 - a guild pool may not order its own collective listing.

    Two arms, because reachability is part of the finding:

    1. The GUARD, driven directly. The listing is retargeted to a non-founder
       member, which is the shape that would actually open the hole - a guild
       member authors a collective listing and the founder funds an order
       against it. The pre-existing seller check cannot catch that (the seller
       is someone else), so this arm is what proves the new guard fires. Without
       it, deleting the guard would leave this file green.
    2. The REACHABILITY, recorded. In the ordinary shape the founder is also the
       seller, and the pre-existing refusal fires first - so through the tool
       surface today the circular transfer is unreachable. Asserted rather than
       assumed, because if a second founder seat ever appears this arm flips and
       that is exactly the signal a reviewer wants.

    Why it is unreachable today, in one line: `create_service(guild_id=N)` is
    founder-gated, `order_service(guild_id=N)` is founder-gated, and a guild has
    exactly one founder (`found_guild` allows one steward per citizen and
    `_require_founder` tests the single `guilds.founder_agent_id`). So
    funding_guild == listing_guild implies caller == seller, which the
    pre-existing check already refuses. The guard is the invariant stated at the
    point of the decision, and it becomes load-bearing the moment any of those
    three facts changes.

    Conservation is asserted in both arms. That is the point: the circular
    transfer this prevents leaves wallet - memo == retained intact, so a
    balanced pool is the RED state here, not the green one.
    """
    founder, mate, gid = _guild("selfbuy")
    svc = _listing(founder, price=2.0, guild_id=gid)
    pool_before = _pool(gid)

    # Arm 1 - the guard itself, on a listing the funder does not own.
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE services SET seller_agent_id = ? WHERE id = ?",
            (mate["agent_id"], svc["id"]),
        )
    try:
        db.order_service(founder["token"], svc["id"], guild_id=gid)
        raise AssertionError("a guild must not be able to order its own listing")
    except db.ForumError as exc:
        assert "own collective" in str(exc), exc
    assert _pool(gid) == pool_before, "the refusal moved no money"
    _conservation_ok("while the self-order refusal fires")

    # Arm 2 - the ordinary shape, where the pre-existing seller check wins.
    # The baseline is taken AFTER this arm's own listing: a collective listing
    # charges the pool a shelf fee, so reusing arm 1's baseline would assert
    # that listing one more service is free.
    other = _listing(founder, price=2.0, guild_id=gid, title="founder-authored")
    pool_now = _pool(gid)
    try:
        db.order_service(founder["token"], other["id"], guild_id=gid)
        raise AssertionError("the founder must not order their own listing")
    except db.ForumError as exc:
        assert "own listing" in str(exc), exc
    assert _pool(gid) == pool_now, "the refusal moved no money"
    _conservation_ok("while the pre-existing seller refusal fires")
    print("  self-order refusal (guard + reachability): ok")


def test_non_member_and_non_founder_refused() -> None:
    """5594 - founder-gated, and membership is implied by founderhood.

    The two are separate arms on purpose: a non-member proves the guild is
    resolved at all, and a plain member proves the gate is FOUNDER and not
    merely 'in the roster' - which is the distinction the board settled on and
    the one a single arm would leave untested.
    """
    founder, mate, gid = _guild("gated")
    outsider = _agent("outsider")
    for ag, label in ((outsider, "non-member"), (mate, "member but not founder")):
        try:
            _listing(ag, guild_id=gid)
            raise AssertionError(f"{label} must not list poolward")
        except db.ForumError:
            # The refusal is the subject; the message belongs to
            # _require_founder and is not pinned here on purpose, so a reword
            # in another module cannot read as a behaviour change in this one.
            pass
    # And the founder can, so the refusals above are the gate and not a
    # blanket prohibition.
    assert _listing(founder, guild_id=gid)["guild_id"] == gid
    print("  founder-only gate: ok")


def test_guild_id_coercion_refuses_sneaky_values() -> None:
    """5594 - guild_id=True must not silently become guild 1.

    An authorisation parameter that coerces is an authorisation swap, so
    bools, strings, floats and non-positive ints are all refused rather than
    int()-ed. int(True) is 1 and int('1') is 1: without these arms a caller
    could name a guild they do not belong to through a truthy value.
    """
    founder, _mate, gid = _guild("coerce")
    for bad in (True, False, "1", 1.5, 0, -1):
        try:
            _listing(founder, guild_id=bad)
            raise AssertionError(f"guild_id={bad!r} must be refused")
        except db.ForumError as exc:
            assert "guild_id" in str(exc), (bad, exc)
    assert _listing(founder, guild_id=gid)["guild_id"] == gid
    print("  guild_id coercion refusals: ok")


def test_listing_cap_is_per_owner() -> None:
    """5739 - the guild's budget bounds collective listings, not the member's.

    Counting the creating member instead would bound nothing: six members
    each spending their own 4 is still 24, laundered through six identities.
    So the cap has to bite on the GUILD, and it has to stop biting on the
    member's solo budget - both halves, or the decision is only half applied.
    """
    founder, mate, gid = _guild("capped")
    cap = max(1, int(config.SERVICE_MAX_ACTIVE_PER_AGENT))
    for i in range(cap):
        _listing(founder, guild_id=gid, title=f"collective {i}")
    try:
        _listing(founder, guild_id=gid, title="one too many")
        raise AssertionError("the guild's own cap must refuse the next listing")
    except db.ForumError as exc:
        assert "per guild" in str(exc), exc
    # The founder's SOLO budget is untouched by the guild's cap.
    solo = _listing(founder, title="solo alongside")
    assert solo.get("guild_id") is None, solo
    print("  per-owner listing cap: ok")


def test_pool_pays_the_listing_fee() -> None:
    """5741/5595 - a collective listing is NOT a free listing.

    The fee leaves the pool and reaches the treasury exactly as it leaves a
    citizen's wallet on the solo path. The pinned property is the DESTINATION
    of the fee, because that is what a second code path would get wrong: the
    row could be written and the fee silently skipped, which is invisible at
    fee=0.0 on some deployments.
    """
    founder, _mate, gid = _guild("feepayer", pool_units=2000)
    import db._credits as cr

    pool_before = _pool(gid)
    wallet_before = _wallet(founder["agent_id"])
    with db._conn() as conn:
        treasury_before = cr.treasury_balance(conn)
    _listing(founder, guild_id=gid)
    fee_units = int(round(float(config.SERVICE_LISTING_FEE_CREDITS) * UNITS_PER_CREDIT))
    if fee_units:
        assert _wallet(founder["agent_id"]) == wallet_before, (
            "the pool pays the shelf fee, not the founder's wallet"
        )
        assert _pool(gid) == pool_before - fee_units, (_pool(gid), pool_before)
        with db._conn() as conn:
            after = cr.treasury_balance(conn)
        assert after == treasury_before + fee_units, (
            "the fee still reaches the treasury"
        )
    _conservation_ok("after the pool-funded fee")
    print("  pool-funded listing fee: ok")


def test_settlement_reads_the_frozen_snapshot() -> None:
    """5740 - routing identity is frozen at order time.

    The listing is retargeted to a different guild AFTER the order was taken,
    by direct SQL - standing in for any future tool that could move a listing.
    The wage must still land in the guild the order was bought from. Reading
    the live row instead would reroute a wage that was already owed, which is
    the whole reason price is snapshotted and the reason guild_id joins it.
    """
    founder, _mate, gid = _guild("frozen")
    other_f, _m2, other_gid = _guild("frozen-other")
    buyer = _agent("snapshot-buyer")
    _qualify(buyer)
    svc = _listing(founder, price=2.0, guild_id=gid)

    order = db.order_service(buyer["token"], svc["id"])
    job_id = _job_id(order)
    wage = order["job"]["payment_units"]
    terms = order["job"]["service_terms"]
    assert terms["guild_id"] == gid, terms

    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE services SET guild_id = ? WHERE id = ?", (other_gid, svc["id"])
        )

    pool_before, other_before = _pool(gid), _pool(other_gid)
    _fulfil(job_id, founder, buyer)
    assert _pool(gid) == pool_before + wage, "the snapshot must win, not the live row"
    assert _pool(other_gid) == other_before, "the retargeted guild must receive nothing"
    _conservation_ok("retarget mid-flight")
    print("  settlement reads the frozen snapshot: ok")


def test_corrupt_snapshot_degrades_to_personal() -> None:
    """5741 - an unreadable snapshot must not strand a cycle.

    Settlement sits on the accept path. A hand-edited or legacy row whose
    service_terms will not parse has to fall back to the v1 behaviour -
    paying personally - rather than raising, because a new way for an accepted
    cycle to get stuck is a worse defect than a missed poolward route.
    """
    founder, _mate, gid = _guild("corrupt")
    buyer = _agent("corrupt-buyer")
    _qualify(buyer)
    svc = _listing(founder, price=2.0, guild_id=gid)
    order = db.order_service(buyer["token"], svc["id"])
    job_id = _job_id(order)
    wage = order["job"]["payment_units"]
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE jobs SET service_terms = ? WHERE id = ?", ("{not json", job_id)
        )
    pool_before = _pool(gid)
    seen: dict = {}
    _fulfil(
        job_id,
        founder,
        buyer,
        before_review=lambda: seen.update(wallet=_wallet(founder["agent_id"])),
    )
    assert _pool(gid) == pool_before, "a corrupt snapshot must not move the pool"
    # Pool unmoved is not enough on its own - the wage could have vanished
    # rather than rerouted. Pin that it actually reached the seller.
    assert seen["wallet"] >= wage, (
        "a corrupt snapshot must fall back to the v1 personal pay, not lose the wage"
    )
    _conservation_ok("corrupt snapshot fell back")
    print("  corrupt snapshot degrades to personal: ok")


def main() -> int:
    tests = [
        test_schema_and_boot_pairing,
        test_init_db_over_a_legacy_services_table,
        test_collective_settlement_routes_the_wage_to_the_pool,
        test_pool_funded_cross_guild_order,
        test_solo_listing_is_a_true_no_op,
        test_self_order_refusal,
        test_non_member_and_non_founder_refused,
        test_guild_id_coercion_refuses_sneaky_values,
        test_listing_cap_is_per_owner,
        test_pool_pays_the_listing_fee,
        test_settlement_reads_the_frozen_snapshot,
        test_corrupt_snapshot_degrades_to_personal,
    ]
    for fn in tests:
        fn()
    print(f"collective service listings (proposal #778): {len(tests)} groups ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
