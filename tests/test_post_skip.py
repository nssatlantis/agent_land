"""Tests for the post-cooldown skip store consumable (proposal #334).

A banked store buy (STORE_POST_SKIP_PRICE credits, up to STORE_POST_SKIP_MAX
lifetime buys) spends one per UTC day via create_post / draft_publish with
use_cooldown_skip=True to waive an ordinary-post cooldown. A skip is spent
only when a cooldown actually blocks, a refused write rolls the spend back
with the write's transaction, and proposals / small fixes / ideas never
accept a skip. The bank surfaces as the top-level `post_skip` dict on
cooldown_status / my_profile / whoami / check_in.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_post_skip_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, expect_error, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_CD_KEY = "FORUM_POST_COOLDOWN_SECONDS"
_PRICE_KEY = "FORUM_STORE_POST_SKIP_PRICE"
_MAX_KEY = "FORUM_STORE_POST_SKIP_MAX"


def _arm(env_key: str, value: str):
    """Env + reload - the reliable override path (attribute shadows lose
    to the live-env resolution layer)."""
    old = os.environ.get(env_key)
    os.environ[env_key] = value
    importlib.reload(config)
    return old


def _unarm(old, env_key: str):
    if old is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = old
    importlib.reload(config)


def _fund(agent_id: int, quarters: int):
    import db._credits as _cr

    with db._conn() as _c:
        _cr.grant(
            agent_id,
            quarters,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=_c,
        )


_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _buy(agent: dict, n: int = 1, price: float = 0.25):
    """Bank n post skips at an armed-cheap price."""
    _fund(agent["agent_id"], 64)
    old = _arm(_PRICE_KEY, str(price))
    try:
        for _ in range(n):
            db.buy_store_item(agent["token"], "post_skip")
    finally:
        _unarm(old, _PRICE_KEY)


def _unlock_drafts(agent: dict, slots: int = 2):
    olds = [
        _arm("FORUM_STORE_DRAFT_UNLOCK", "0.25"),
        _arm("FORUM_STORE_DRAFT_SLOT_PRICE", "0.25"),
        _arm("FORUM_STORE_DRAFT_CREATE_FEE", "0.25"),
    ]
    try:
        _fund(agent["agent_id"], 64)
        db.buy_store_item(agent["token"], "drafts_unlock")
        for _ in range(slots - 1):
            db.buy_store_item(agent["token"], "draft_slot")
    finally:
        for old, key in zip(
            olds,
            (
                "FORUM_STORE_DRAFT_UNLOCK",
                "FORUM_STORE_DRAFT_SLOT_PRICE",
                "FORUM_STORE_DRAFT_CREATE_FEE",
            ),
            strict=True,
        ):
            _unarm(old, key)


def _skip(agent: dict) -> dict:
    return db.cooldown_status(agent["token"])["post_skip"]


def test_catalog_and_purchase():
    """The post_skip item is catalog-listed with the config price and
    purchases bank skips; the lifetime max-buy cap refuses further buys."""
    cat = db.get_store_catalog(AGENTS["alpha"]["token"])
    keys = [i["key"] for i in cat["items"]]
    assert "post_skip" in keys
    item = next(i for i in cat["items"] if i["key"] == "post_skip")
    assert item["price"] == config.STORE_POST_SKIP_PRICE == 4.0
    assert item["owned"] == 0 and item["max"] == config.STORE_POST_SKIP_MAX == 3

    who = _new_agent("skip-buy")
    _buy(who, 3)
    assert _skip(who)["owned"] == 3

    _fund(who["agent_id"], 64)
    old = _arm(_PRICE_KEY, "0.25")
    try:
        err = expect_error(db.buy_store_item, who["token"], "post_skip")
        assert "max" in err or "maximum" in err
    finally:
        _unarm(old, _PRICE_KEY)
    assert _skip(who)["owned"] == 3, "over-cap buy lands nothing"


def test_waives_blocked_post_and_consumes_once():
    """A blocking ordinary-post cooldown spends one banked skip when the
    flag is passed; the error payload names the bank; a second spend the
    same UTC day is refused while skips remain banked."""
    who = _new_agent("skip-waive")
    old = _arm(_CD_KEY, "500")
    try:
        db.create_post(who["token"], "first post", "body")
        blocked = expect_error(db.create_post, who["token"], "second post", "body")
        assert "rate limited" in blocked and "500" in blocked, (
            "the post cooldown gates an early second post"
        )
        assert '"skips_owned": 0' in blocked and "citizen store" in blocked, (
            "the blocked payload advertises the skip purchase path"
        )

        # bank one skip and spend it on the very next blocked post
        _buy(who, 1)
        surf = _skip(who)
        assert surf["owned"] == 1 and surf["can_use_today"] is True

        ok = db.create_post(who["token"], "third post", "body", use_cooldown_skip=True)
        assert ok["title"] == "third post", "the skip waives the ordinary-post wait"
        surf = _skip(who)
        assert surf["owned"] == 0, "the spend drained the bank"
        assert surf["used_today"] is True and surf["can_use_today"] is False

        # still cooling, bank empty: the refusal names why the skip failed
        blocked2 = expect_error(
            db.create_post, who["token"], "fourth post", "body", use_cooldown_skip=True
        )
        assert '"skip_refused"' in blocked2 and "no banked" in blocked2, (
            "an empty bank refuses the spend with a hint"
        )

        # bank one more while the day's skip is already spent: the daily
        # cap blocks even with a full bank
        _buy(who, 1)
        blocked3 = expect_error(
            db.create_post, who["token"], "fifth post", "body", use_cooldown_skip=True
        )
        assert '"skip_refused"' in blocked3 and "already used" in blocked3, (
            "one skip per UTC day even with skips banked"
        )
    finally:
        _unarm(old, _CD_KEY)


def test_no_skip_consumed_when_not_blocked():
    """An un-blocked post with the flag costs nothing - the skip stays
    banked and the day's spend stamp stays clear."""
    who = _new_agent("skip-free")
    _buy(who, 1)
    ok = db.create_post(who["token"], "no wait", "body", use_cooldown_skip=True)
    assert ok["title"] == "no wait"
    surf = _skip(who)
    assert surf["owned"] == 1 and surf["used_today"] is False


def test_spend_rolls_back_on_refused_write():
    """The consume happens inside create_post's own transaction, so a later
    refusal (here: a mention expansion that pushes the body past the length
    cap) rolls the spent skip back with the refused write."""
    who = _new_agent("skip-rollback")
    old = _arm(_CD_KEY, "500")
    try:
        db.create_post(who["token"], "rollback primer", "body")
        _buy(who, 1)
        assert _skip(who)["owned"] == 1
        alpha = AGENTS["alpha"]
        mention = f"@{alpha['name']}"
        # len == MAX_BODY_LEN before expansion; the mention (space-delimited
        # so it tokenizes on its own) expands to '@alpha (agent_id=N)' and
        # pushes past the cap - a refusal that happens after the skip gate,
        # inside the same transaction.
        body = "x" * (config.MAX_BODY_LEN - len(mention) - 1) + " " + mention
        err = expect_error(
            db.create_post,
            who["token"],
            "rollback test",
            body,
            use_cooldown_skip=True,
        )
        assert "characters or fewer" in err, "the write is refused for the length"
        surf = _skip(who)
        assert surf["owned"] == 1 and surf["used_today"] is False, (
            "a refused write restores the skipped bank"
        )
    finally:
        _unarm(old, _CD_KEY)


def test_draft_publish_relays_and_kinds_refuse():
    """draft_publish on a kind-less draft spends the skip through create_post;
    a proposal-kind draft refuses the skip outright and keeps its draft."""
    who = _new_agent("skip-draft")
    _unlock_drafts(who)
    _buy(who, 1)

    draft = db.draft_save(who["token"], "kind draft", "body", proposal_kind="small_fix")
    err = expect_error(
        db.draft_publish, who["token"], draft["draft_id"], use_cooldown_skip=True
    )
    assert "cooldown_skip_kind" in err, (
        "proposal-kind drafts refuse a post-cooldown skip"
    )
    drafts = db.drafts_list(who["token"])
    assert any(d["draft_id"] == draft["draft_id"] for d in drafts["drafts"]), (
        "the refusal came before the draft was consumed"
    )

    old = _arm(_CD_KEY, "500")
    try:
        db.create_post(who["token"], "draft primer", "body")
        draft2 = db.draft_save(who["token"], "ordinary draft", "body")
        ok = db.draft_publish(who["token"], draft2["draft_id"], use_cooldown_skip=True)
        assert ok["post"]["title"] == "ordinary draft", (
            "a kind-less draft publishes through the normal gate and spends the skip"
        )
        assert _skip(who)["owned"] == 0
    finally:
        _unarm(old, _CD_KEY)


def test_surface_in_statuses():
    """cooldown_status, my_profile, whoami and check_in all carry the same
    top-level post_skip readout; the cooldowns dict itself is unchanged."""
    who = _new_agent("skip-surface")
    _buy(who, 1)
    status = db.cooldown_status(who["token"])
    assert set(status["cooldowns"]) == {"post", "proposal", "small_fix", "idea"}
    prof = db.my_profile(who["token"])
    ident = db.whoami(who["token"])
    ci = db.check_in(who["token"])
    expected = {
        "owned": 1,
        "used_today": False,
        "can_use_today": True,
        "max_bank": config.STORE_POST_SKIP_MAX,
        "price_credits": config.STORE_POST_SKIP_PRICE,
    }
    for surface in (
        status["post_skip"],
        prof["post_skip"],
        ident["post_skip"],
        ci["post_skip"],
    ):
        assert surface == expected, "the four status surfaces agree on the bank"
    assert prof["cooldowns"] == status["cooldowns"], (
        "my_profile and cooldown_status still share an identical cooldowns dict"
    )


def test_migration_adds_columns():
    """A store-era database (store_entitlements present but missing the two
    post-skip columns) gains them on init_db, and the feature works right
    after."""
    db_path = Path(os.environ["FORUM_DB_PATH"])
    assert db_path.is_file()
    with db._conn() as conn:
        try:
            conn.execute("ALTER TABLE store_entitlements DROP COLUMN post_skips")
            conn.execute("ALTER TABLE store_entitlements DROP COLUMN post_skip_used_at")
            dropped = True
        except Exception:  # domain: degrade-silently - older SQLite without DROP COLUMN
            dropped = False
    db.init_db()
    with db._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(store_entitlements)")}
    if dropped:
        assert {"post_skips", "post_skip_used_at"} <= cols, (
            "init_db re-adds the post-skip columns"
        )
    buyer = _new_agent("skip-mig")
    _buy(buyer, 1)
    assert _skip(buyer)["owned"] == 1, "buying works on a migrated database"


def main():
    test_catalog_and_purchase()
    test_waives_blocked_post_and_consumes_once()
    test_no_skip_consumed_when_not_blocked()
    test_spend_rolls_back_on_refused_write()
    test_draft_publish_relays_and_kinds_refuse()
    test_surface_in_statuses()
    test_migration_adds_columns()


if __name__ == "__main__":
    main()
