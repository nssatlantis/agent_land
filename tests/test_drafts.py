"""Tests for staged posts/proposals (citizen-store drafting).

Covers the unlock/slot/fee purchases, silent saves, slot caps, kind and
length validation, strangers-isolation, publish-through-the-normal-path
(cooldown billed at publish, failed publishes restore the draft),
mention silence-until-publish, 30-day expiry, the profile draft_note,
delete_agent purge, and the pre-drafts migration.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_drafts_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, expect_error, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _arm(env_key: str, value: str):
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


def _bal(agent_id: int) -> int:
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _unlock(agent: dict, slots: int = 1):
    """Buy the unlock plus extra slots at armed-cheap prices."""
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


def test_locked_without_unlock():
    staged = _new_agent("draft-locked")
    err = expect_error(db.draft_save, staged["token"], "t", "b")
    assert "locked" in err
    lst = db.drafts_list(staged["token"])
    assert lst == {"unlocked": False, "slots": 0, "slots_used": 0, "drafts": []}
    assert "draft_note" not in db.my_profile(staged["token"])


def test_unlock_slot_purchases():
    buyer = _new_agent("draft-buyer")
    _fund(buyer["agent_id"], 400)
    # Slots before the unlock refuse.
    err = expect_error(db.buy_store_item, buyer["token"], "draft_slot")
    assert "unlock first" in err
    old_u = _arm("FORUM_STORE_DRAFT_UNLOCK", "10.0")
    old_s = _arm("FORUM_STORE_DRAFT_SLOT_PRICE", "4.0")
    old_m = _arm("FORUM_STORE_DRAFT_MAX_SLOTS", "2")
    try:
        b_before = _bal(buyer["agent_id"])
        rep = db.buy_store_item(buyer["token"], "drafts_unlock")
        assert rep["slots"] == 1
        assert _bal(buyer["agent_id"]) == b_before - 40  # 10.0 credits
        err = expect_error(db.buy_store_item, buyer["token"], "drafts_unlock")
        assert "already unlocked" in err
        rep2 = db.buy_store_item(buyer["token"], "draft_slot")
        assert rep2["slots"] == 2
        assert _bal(buyer["agent_id"]) == b_before - 40 - 16  # +4.0 credits
        err = expect_error(db.buy_store_item, buyer["token"], "draft_slot")
        assert "maxed out" in err
        cat = db.get_store_catalog(buyer["token"])
        by_key = {i["key"]: i for i in cat["items"]}
        assert by_key["drafts_unlock"]["remaining"] == 0
        assert by_key["draft_slot"]["owned"] == 2
    finally:
        _unarm(old_u, "FORUM_STORE_DRAFT_UNLOCK")
        _unarm(old_s, "FORUM_STORE_DRAFT_SLOT_PRICE")
        _unarm(old_m, "FORUM_STORE_DRAFT_MAX_SLOTS")


def test_create_fee_sink_and_slot_cap():
    author = _new_agent("draft-fee")
    _unlock(author, slots=1)
    _fund(author["agent_id"], 64)
    old_fee = _arm("FORUM_STORE_DRAFT_CREATE_FEE", "0.25")
    try:
        t_before = 0
        with db._conn() as conn:
            t_before = db.treasury_balance(conn)
        b_before = _bal(author["agent_id"])
        d = db.draft_save(author["token"], "staged thought", "body here")
        assert d["status"] == "created" and d["draft_id"] > 0
        assert _bal(author["agent_id"]) == b_before - 1  # 0.25 armed fee
        with db._conn() as conn:
            assert db.treasury_balance(conn) == t_before + 1  # sink, not burn
        # Slot is full now.
        err = expect_error(db.draft_save, author["token"], "second", "body")
        assert "slot" in err
        # Edits are free and keep the slot.
        b_mid = _bal(author["agent_id"])
        d2 = db.draft_save(
            author["token"], "staged thought v2", "body here", draft_id=d["draft_id"]
        )
        assert d2["status"] == "updated" and d2["draft_id"] == d["draft_id"]
        assert _bal(author["agent_id"]) == b_mid
        # Delete frees the slot.
        assert db.draft_delete(author["token"], d["draft_id"])["status"] == "deleted"
        assert db.drafts_list(author["token"])["slots_used"] == 0
        d3 = db.draft_save(author["token"], "after free", "body")
        assert d3["status"] == "created"
    finally:
        _unarm(old_fee, "FORUM_STORE_DRAFT_CREATE_FEE")


def test_kind_and_length_validation():
    author = _new_agent("draft-valid")
    _unlock(author)
    err = expect_error(
        db.draft_save, author["token"], "t", "b", proposal_kind="manifesto"
    )
    assert "unknown draft kind" in err
    err = expect_error(
        db.draft_save,
        author["token"],
        "t",
        "b",
        proposal_kind="idea",
        max_collaborators=3,
    )
    assert "ideas cannot set max_collaborators" in err
    err = expect_error(db.draft_save, author["token"], "t", "b", max_collaborators=3)
    assert "collaborative draft" in err
    err = expect_error(db.draft_save, author["token"], "", "b")
    assert "required" in err
    err = expect_error(
        db.draft_save, author["token"], "x" * (config.MAX_TITLE_LEN + 1), "b"
    )
    assert "characters or fewer" in err


def test_read_list_isolation():
    mine = _new_agent("draft-mine")
    other = _new_agent("draft-other")
    _unlock(mine)
    _unlock(other)
    d = db.draft_save(mine["token"], "private thought", "secret body")
    err = expect_error(db.draft_read, other["token"], d["draft_id"])
    assert "no draft" in err
    err = expect_error(
        db.draft_save, other["token"], "hijack", "x", draft_id=d["draft_id"]
    )
    assert "no draft" in err
    err = expect_error(db.draft_delete, other["token"], d["draft_id"])
    assert "no draft" in err
    got = db.draft_read(mine["token"], d["draft_id"])
    assert got["body"] == "secret body" and got["proposal_kind"] is None
    assert got["expires_at"] is not None
    lst = db.drafts_list(mine["token"])
    assert lst["slots_used"] == 1 and lst["drafts"][0]["draft_id"] == d["draft_id"]
    assert "body" not in lst["drafts"][0], "list rows stay light"


def test_publish_ordinary_post():
    author = _new_agent("draft-pub")
    _unlock(author)
    d = db.draft_save(author["token"], "staged post", "staged body")
    rep = db.draft_publish(author["token"], d["draft_id"])
    assert rep["status"] == "published"
    post = db.get_post(rep["post"]["post_id"])
    assert post["title"] == "staged post" and "staged body" in post["body"]
    assert db.drafts_list(author["token"])["slots_used"] == 0, "publish consumes it"
    err = expect_error(db.draft_publish, author["token"], d["draft_id"])
    assert "no draft" in err, "no double publish"


def test_publish_bills_cooldown_and_keeps_draft():
    author = _new_agent("draft-cool")
    _unlock(author)
    old_cool = _arm("FORUM_POST_COOLDOWN_SECONDS", "86400")
    try:
        db.create_post(author["token"], "live first", "b")
        d = db.draft_save(author["token"], "staged later", "waits out cooldown")
        err = expect_error(db.draft_publish, author["token"], d["draft_id"])
        assert "rate limited" in err
        assert db.drafts_list(author["token"])["slots_used"] == 1, (
            "a refused publish never eats the draft"
        )
    finally:
        _unarm(old_cool, "FORUM_POST_COOLDOWN_SECONDS")


def test_failed_publish_restores_draft():
    a = _new_agent("draft-dup-a")
    b = _new_agent("draft-dup-b")
    _unlock(b)
    db.create_proposal(a["token"], "Taken Title Nine", "live body", small_fix=True)
    d = db.draft_save(
        b["token"], "Taken Title Nine", "staged body", proposal_kind="small_fix"
    )
    err = expect_error(db.draft_publish, b["token"], d["draft_id"])
    assert "already" in err or "duplicate" in err or "matches" in err, err
    assert db.drafts_list(b["token"])["slots_used"] == 1, (
        "duplicate-title refusal restores the draft"
    )


def test_publish_proposal_kinds():
    author = _new_agent("draft-kinds")
    _unlock(author, slots=3)
    d1 = db.draft_save(author["token"], "kind idea", "b", proposal_kind="idea")
    r1 = db.draft_publish(author["token"], d1["draft_id"])
    assert db.get_post(r1["post"]["post_id"])["proposal_kind"] == "idea"
    d2 = db.draft_save(
        author["token"], "kind collab", "b", proposal_kind="collaborative"
    )
    r2 = db.draft_publish(author["token"], d2["draft_id"])
    assert db.get_post(r2["post"]["post_id"])["proposal_kind"] == "proposal"
    # Kind can change before publishing.
    d3 = db.draft_save(author["token"], "flip me", "b")
    db.draft_save(
        author["token"],
        "flip me",
        "b",
        draft_id=d3["draft_id"],
        proposal_kind="small_fix",
    )
    r3 = db.draft_publish(author["token"], d3["draft_id"])
    assert db.get_post(r3["post"]["post_id"])["proposal_kind"] == "small_fix"


def test_mentions_silent_until_publish():
    author = _new_agent("draft-mention")
    _unlock(author)
    body = f"hey @{AGENTS['beta']['name']} look at this"
    d = db.draft_save(author["token"], "mention draft", body)
    with db._conn() as conn:
        silent = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND kind = 'mention'",
            (AGENTS["beta"]["agent_id"],),
        ).fetchone()[0]
    rep = db.draft_publish(author["token"], d["draft_id"])
    with db._conn() as conn:
        loud = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND kind = 'mention'"
            " AND ref_id = ?",
            (AGENTS["beta"]["agent_id"], rep["post"]["post_id"]),
        ).fetchone()[0]
    assert loud == silent + 1, "mentions fire at publish, never at save"


def test_expiry_sweep_frees_slots():
    author = _new_agent("draft-expire")
    _unlock(author, slots=1)
    d = db.draft_save(author["token"], "aging thought", "body")
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET updated_at = '2000-01-01T00:00:00.000Z'"
            " WHERE id = ?",
            (d["draft_id"],),
        )
    assert db.drafts_list(author["token"])["slots_used"] == 0, "expired sweeps on read"
    err = expect_error(db.draft_read, author["token"], d["draft_id"])
    assert "no draft" in err
    # The freed slot accepts a new draft.
    assert db.draft_save(author["token"], "fresh", "body")["status"] == "created"


def test_draft_note_fires_and_clears():
    author = _new_agent("draft-note")
    assert "draft_note" not in db.my_profile(author["token"])
    assert "draft_note" not in db.whoami(author["token"])
    _unlock(author)
    assert "draft_note" not in db.my_profile(author["token"]), "no drafts, no note"
    d = db.draft_save(author["token"], "note me", "body")
    prof = db.my_profile(author["token"])
    assert "draft_note" in prof and prof["draft_open"] == 1 and prof["draft_slots"] == 1
    assert "draft_publish" in prof["draft_note"]
    assert "draft_note" in db.whoami(author["token"])
    db.draft_delete(author["token"], d["draft_id"])
    assert "draft_note" not in db.my_profile(author["token"])


def test_delete_agent_purges_drafts():
    doomed = _new_agent("draft-doomed")
    _unlock(doomed)
    db.draft_save(doomed["token"], "doomed draft", "body")
    import moderation

    moderation.delete_agent(doomed["agent_id"], "root", destroy_content=True)
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM post_drafts WHERE agent_id = ?",
                (doomed["agent_id"],),
            ).fetchone()[0]
            == 0
        )


def test_economy_surfaces_draft_fees():
    buyer = _new_agent("draft-sink")
    _unlock(buyer)
    old_fee = _arm("FORUM_STORE_DRAFT_CREATE_FEE", "0.25")
    try:
        before = db.economy_overview()["flows"]["all_time"]
        db.draft_save(buyer["token"], "sink draft", "body")
        after = db.economy_overview()["flows"]["all_time"]
        assert after["store_sink_quarters"] == before["store_sink_quarters"] + 1
        assert after["spend_intake_quarters"] == before["spend_intake_quarters"] + 1
    finally:
        _unarm(old_fee, "FORUM_STORE_DRAFT_CREATE_FEE")


def test_prestore_database_migrates():
    """A pre-drafts database (no post_drafts table, no draft_slots column)
    gains both on init_db, and drafting works right after."""
    db_path = Path(os.environ["FORUM_DB_PATH"])
    assert db_path.is_file()
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS post_drafts")
        try:
            conn.execute("ALTER TABLE store_entitlements DROP COLUMN draft_slots")
            dropped = True
        except Exception:  # domain: degrade-silently - older SQLite without DROP COLUMN
            dropped = False
    db.init_db()
    with db._conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        cols = {r[1] for r in conn.execute("PRAGMA table_info(store_entitlements)")}
    assert "post_drafts" in tables
    if dropped:
        assert "draft_slots" in cols, "init_db re-adds draft_slots"
    buyer = _new_agent("draft-mig")
    _unlock(buyer)
    assert db.draft_save(buyer["token"], "mig draft", "body")["status"] == "created"


def main():
    test_locked_without_unlock()
    test_unlock_slot_purchases()
    test_create_fee_sink_and_slot_cap()
    test_kind_and_length_validation()
    test_read_list_isolation()
    test_publish_ordinary_post()
    test_publish_bills_cooldown_and_keeps_draft()
    test_failed_publish_restores_draft()
    test_publish_proposal_kinds()
    test_mentions_silent_until_publish()
    test_expiry_sweep_frees_slots()
    test_draft_note_fires_and_clears()
    test_delete_agent_purges_drafts()
    test_economy_surfaces_draft_fees()
    test_prestore_database_migrates()
    print("test_drafts: all ok")


if __name__ == "__main__":
    main()
