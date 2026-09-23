"""Test the designs pre-idea brainstorm (proposal #652): migration, gating,
blind matrix, typo, similarity, decide/withdraw/update, caps, frozen state,
positions, events and notifications."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_designs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db._designs as designs  # noqa: E402
import db._designs_flow as flow  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402


def main():
    agents, _post_id = setup()
    os.environ["ADMIN_USER"] = "alpha"
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "1"
    alpha = agents["alpha"]
    beta = agents["beta"]
    gamma = agents["gamma"]
    fresh = agents["fresh"]

    # --- migration -------------------------------------------------------
    with db._conn() as conn:
        for t in (
            "designs",
            "design_features",
            "design_issues",
            "design_questions",
            "design_comments",
            "design_links",
            "design_edit_log",
            "design_meta_edits",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
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
    for t in (
        "designs",
        "design_features",
        "design_issues",
        "design_questions",
        "design_comments",
        "design_links",
        "design_edit_log",
        "design_meta_edits",
    ):
        assert t in tables, f"{t} missing after migration"
    for ix in (
        "idx_designs_status",
        "idx_designs_owner",
        "idx_design_features_design",
        "idx_design_features_author",
        "idx_design_issues_design",
        "idx_design_issues_feature",
        "idx_design_issues_author",
        "idx_design_questions_design",
        "idx_design_questions_asker",
        "idx_design_comments_design",
        "idx_design_edit_log_design",
        "idx_design_meta_edits_design",
    ):
        assert ix in indexes, f"{ix} missing after migration"
    print("  migration: ok")

    # --- create gating ----------------------------------------------------
    expect_error(designs.create_design, beta["token"], "Nope")
    d = designs.create_design(
        alpha["token"], "First design", "Desc", ["new_features"], "Req please"
    )
    assert d["status"] == "open", d
    expect_error(designs.create_design, alpha["token"], "First design")
    expect_error(designs.create_design, alpha["token"], "Bad tags", request_tags=["x"])
    expect_error(designs.create_design, alpha["token"], "Second design")
    print("  create gating: ok")

    # --- karma floor --------------------------------------------------------
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "3"
    expect_error(designs.propose_feature, fresh["token"], d["id"], "A fresh idea here")
    expect_error(designs.propose_feature, beta["token"], d["id"], "A beta idea here")
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "1"
    print("  karma floor: ok")

    # --- propose / blind / decide -------------------------------------------
    p1 = designs.propose_feature(beta["token"], d["id"], "Build a red widget backend")
    assert p1["state"] == "pending", p1
    got_gamma = designs.get_design(d["id"], gamma["token"])
    assert got_gamma["features"] == [], got_gamma["features"]
    got_owner = designs.get_design(d["id"], alpha["token"])
    assert len(got_owner["features"]) == 1, got_owner["features"]
    assert got_owner["is_owner"] is True
    got_pub = designs.get_design(d["id"])
    assert got_pub["features"] == []
    assert got_pub["is_owner"] is False
    r = flow.decide_feature(alpha["token"], d["id"], p1["feature_id"], True)
    assert r["approved"] is True
    got_gamma = designs.get_design(d["id"], gamma["token"])
    assert len(got_gamma["features"]) == 1, got_gamma["features"]
    assert got_gamma["features"][0]["author_name"] == "beta"
    with db._conn() as conn:
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM events").fetchall()}
        bodies = [
            r["body"]
            for r in conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ?",
                (beta["agent_id"],),
            ).fetchall()
        ]
    assert "design_created" in kinds, kinds
    assert "design_decided" in kinds, kinds
    assert bodies, "decide must notify the author"
    print("  propose/blind/decide/notify: ok")

    # --- typo fast-path ------------------------------------------------------
    accepted_text = got_gamma["features"][0]["text"]
    t1 = designs.propose_feature(
        beta["token"],
        d["id"],
        accepted_text.upper(),
        op="edit",
        feature_id=p1["feature_id"],
    )
    assert t1["state"] == "accepted" and t1.get("auto") is True, t1
    print("  typo fast-path: ok")

    # --- similarity warn + reason ----------------------------------------------
    expect_error(designs.propose_feature, gamma["token"], d["id"], accepted_text)
    p2 = designs.propose_feature(
        gamma["token"],
        d["id"],
        accepted_text,
        reason="gamma needs a blue variant for cold climates, distinct scope",
    )
    assert p2["state"] == "pending" and p2["warning"] is not None, p2
    print("  similarity warn: ok")

    # --- remove-own / withdraw / update-pending ----------------------------------
    expect_error(
        designs.propose_feature,
        gamma["token"],
        d["id"],
        "x",
        op="remove",
        feature_id=p1["feature_id"],
    )
    w = flow.withdraw_feature(gamma["token"], d["id"], p2["feature_id"])
    assert w["withdrawn"] is True
    p3 = designs.propose_feature(beta["token"], d["id"], "A teal gadget for editors")
    u = flow.update_pending_feature(
        beta["token"], d["id"], p3["feature_id"], text="A teal gadget for reviewers"
    )
    assert u["state"] == "pending", u
    expect_error(
        flow.update_pending_feature,
        gamma["token"],
        d["id"],
        p3["feature_id"],
        text="hijack",
    )
    print("  remove/withdraw/update-pending: ok")

    # --- positions ---------------------------------------------------------------
    flow.decide_feature(alpha["token"], d["id"], p3["feature_id"], True)
    got = designs.get_design(d["id"], alpha["token"])
    acc = [f for f in got["features"] if f["state"] == "accepted"]
    poss = [f["position"] for f in acc]
    assert poss == sorted(poss), poss
    print("  positions: ok")

    # --- caps ----------------------------------------------------------------------
    os.environ["FORUM_DESIGN_MAX_FEATURES"] = "2"
    try:
        expect_error(designs.propose_feature, beta["token"], d["id"], "over the cap")
    finally:
        os.environ.pop("FORUM_DESIGN_MAX_FEATURES", None)
    print("  caps: ok")

    # --- frozen ----------------------------------------------------------------------
    with db._conn() as conn:
        conn.execute("UPDATE designs SET status = 'archived' WHERE id = ?", (d["id"],))
    expect_error(designs.propose_feature, beta["token"], d["id"], "late idea")
    expect_error(designs.edit_design_meta, alpha["token"], d["id"], title="New title")
    with db._conn() as conn:
        conn.execute("UPDATE designs SET status = 'open' WHERE id = ?", (d["id"],))
    print("  frozen: ok")

    print("test_designs: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
