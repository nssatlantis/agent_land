"""Test designs subscriptions (proposal #652, PR2g): migration, store
CRUD, cap, auto-subscribe on contribute, and fan-out receipts (answer +
comment) with single-mail discipline."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_designs_subs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402, I001

import db._designs as designs  # noqa: E402
import db._designs_discuss as discuss  # noqa: E402
import db._designs_flow as flow  # noqa: E402
import db._designs_issues as issues  # noqa: E402


def _age_design(did):
    with db._conn() as conn:
        conn.execute(
            "UPDATE designs SET created_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (did,),
        )


def _unread(agent_id, kind=None):
    with db._conn() as conn:
        if kind is None:
            rows = conn.execute(
                "SELECT kind, ref_type, ref_id FROM notifications"
                " WHERE agent_id = ? AND read_at IS NULL",
                (agent_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT kind, ref_type, ref_id FROM notifications"
                " WHERE agent_id = ? AND read_at IS NULL AND kind = ?",
                (agent_id, kind),
            ).fetchall()
        return [tuple(r) for r in rows]


def main():
    agents, _post_id = setup()
    os.environ["ADMIN_USER"] = "alpha"
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "1"
    alpha = agents["alpha"]
    beta = agents["beta"]
    gamma = agents["gamma"]

    # --- migration ---------------------------------------------------------
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS design_subscriptions")
    db.init_db()
    with db._conn() as conn:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "design_subscriptions" in tables, "table missing after migration"
    assert "idx_design_subscriptions_design" in indexes, "index missing"
    print("  migration: ok")

    d = designs.create_design(alpha["token"], "Sub design", "Desc")
    did = d["id"]
    _age_design(did)

    # --- store CRUD + list shape ----------------------------------------------
    assert db.list_subscriptions(beta["token"])["design_total"] == 0
    s = db.subscribe_design(beta["token"], did)
    assert s["status"] == "subscribed", s
    s = db.subscribe_design(beta["token"], did)
    assert s["status"] == "already_subscribed", s
    res = db.list_subscriptions(beta["token"])
    assert res["design_total"] == 1, res
    assert res["design_subscriptions"][0]["design_id"] == did
    assert res["design_subscriptions"][0]["title"] == "Sub design"
    assert res["design_subscriptions"][0]["status"] == "open"
    # post shape untouched, with zero post subs
    assert res["subscriptions"] == [] and res["total"] == 0 and res["max"] > 0
    expect_error(db.subscribe_design, beta["token"], 424242)
    u = db.unsubscribe_design(beta["token"], did)
    assert u["status"] == "unsubscribed", u
    u = db.unsubscribe_design(beta["token"], did)
    assert u["status"] == "not_subscribed", u
    print("  crud+list: ok")

    # --- auto-subscribe on contribute --------------------------------------------
    f1 = designs.propose_feature(beta["token"], did, "Auto-sub engine block")
    assert db.list_subscriptions(beta["token"])["design_total"] == 1
    i1 = issues.propose_issue(gamma["token"], did, "Auto-sub overheat")
    assert db.list_subscriptions(gamma["token"])["design_total"] == 1
    q = discuss.ask_question(beta["token"], did, "Auto-sub fuel?")
    assert db.list_subscriptions(beta["token"])["design_total"] == 1
    discuss.enable_comments(alpha["token"], did)
    discuss.add_comment(beta["token"], did, "Auto-sub hello")
    assert db.list_subscriptions(beta["token"])["design_total"] == 1
    print("  auto-sub: ok")

    # --- fan-out receipts ----------------------------------------------------------
    flow.decide_feature(alpha["token"], did, f1["feature_id"], True)
    issues.decide_issue(alpha["token"], did, i1["issue_id"], True)
    # gamma: subscriber only (issue decided, never answered for them yet)
    discuss.answer_question(alpha["token"], did, q["question_id"], "Sunlight.")
    gamma_sub = [
        n for n in _unread(gamma["agent_id"], "subscription") if n[1] == "design"
    ]
    assert len(gamma_sub) == 1, gamma_sub
    # beta (author+asker, auto-subscribed): design-kind contributor mail on comment
    discuss.add_comment(gamma["token"], did, "Ping contributors")
    beta_design = [n for n in _unread(beta["agent_id"], "design") if n[1] == "design"]
    assert len(beta_design) >= 1, beta_design
    # gamma's second subscription-kind mail dedups while the first is unread
    gamma_sub2 = [
        n for n in _unread(gamma["agent_id"], "subscription") if n[1] == "design"
    ]
    assert len(gamma_sub2) == 1, gamma_sub2
    print("  fan-out: ok")

    # --- cap: manual refuses, auto-sub skips silently --------------------------------
    os.environ["FORUM_MAX_POST_SUBSCRIPTIONS"] = "1"
    try:
        d2 = designs.create_design(alpha["token"], "Cap design", "Desc")
        with db._conn() as conn:
            conn.execute(
                "UPDATE designs SET created_at = '2020-01-01T00:00:00.000Z'"
                " WHERE id = ?",
                (d2["id"],),
            )
        # gamma holds 1 design sub already -> manual second refused
        expect_error(db.subscribe_design, gamma["token"], d2["id"])
        # ...but contributing still works, it just does not add a sub row
        designs.propose_feature(gamma["token"], d2["id"], "Cap-safe engine")
        with db._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM design_subscriptions WHERE agent_id = ? AND design_id = ?",
                (gamma["agent_id"], d2["id"]),
            ).fetchone()
        assert row is None, "full budget must skip auto-sub, never refuse"
    finally:
        del os.environ["FORUM_MAX_POST_SUBSCRIPTIONS"]
    print("  cap: ok")

    # --- tools surface ---------------------------------------------------------------
    from server.tools import notifications as ntootls  # noqa: E402, I001

    r = ntootls.set_subscription(gamma["token"], action="subscribe", design_id=did)
    assert r["status"] in ("subscribed", "already_subscribed"), r
    listed = ntootls.list_subscriptions(gamma["token"])
    assert listed["design_total"] >= 1, listed
    try:
        ntootls.set_subscription(gamma["token"], action="subscribe")
    except Exception as exc:
        assert "exactly one" in str(exc), exc
    else:
        raise AssertionError("missing target must be refused")
    try:
        ntootls.set_subscription(gamma["token"], design_id=did)
    except TypeError as exc:
        assert "action" in str(exc), exc
    else:
        raise AssertionError("omitted action must be refused by the wrapper schema")
    print("  tools: ok")

    print("test_designs_subscriptions: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
