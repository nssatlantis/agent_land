"""Test the designs MCP tool surface (proposal #652, PR2c): thin wrappers
over the db engine. Pins pass-through parity (auth gates, 2-step flows,
blind readers) - engine semantics stay pinned in test_designs(_flow)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_designs_tools_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402, I001
from server.tools import designs as dtools  # noqa: E402, I001


def _age_design(did):
    with db._conn() as conn:
        conn.execute(
            "UPDATE designs SET created_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (did,),
        )


def main():
    agents, _post_id = setup()
    os.environ["ADMIN_USER"] = "alpha"
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "1"
    alpha = agents["alpha"]
    beta = agents["beta"]

    # --- create + meta (admin gate passes straight through) -----------------
    expect_error(dtools.create_design, beta["token"], "Nope", "Desc")
    d = dtools.create_design(
        alpha["token"],
        "Tools design",
        "Desc",
        request_tags=["new_ideas"],
        request_text="Make it green",
    )
    did = d["id"]
    assert d["status"] == "open", d
    m = dtools.edit_design_meta(alpha["token"], did, description="Greener desc")
    assert m["design_id"] == did, m
    expect_error(dtools.edit_design_meta, beta["token"], did, title="Hijack")
    print("  create+meta: ok")

    # --- feature propose/update/decide/withdraw ------------------------------
    f1 = dtools.propose_feature(beta["token"], did, "Build a green engine block")
    assert f1["state"] == "pending", f1
    u = dtools.update_pending_feature(
        beta["token"], did, f1["feature_id"], text="Build a green engine block v2"
    )
    assert u["state"] == "pending", u
    dec = dtools.decide_feature(alpha["token"], did, f1["feature_id"], True)
    assert dec["approved"] is True, dec
    f9 = dtools.propose_feature(beta["token"], did, "Never mind this one")
    w = dtools.withdraw_feature(beta["token"], did, f9["feature_id"])
    assert w["withdrawn"] is True, w
    expect_error(dtools.decide_feature, beta["token"], did, f1["feature_id"], False)
    print("  features: ok")

    # --- blind readers --------------------------------------------------------
    anon = dtools.get_design(did)
    assert len(anon["features"]) == 1, anon["features"]
    assert anon["features"][0]["author_name"] == "beta", anon["features"][0]
    dock = dtools.list_designs()
    assert any(r["id"] == did for r in dock["designs"]), dock
    print("  readers: ok")

    # --- issues + resolve + move ----------------------------------------------
    i1 = dtools.propose_issue(
        beta["token"], did, "Green engine overheats", feature_id=f1["feature_id"]
    )
    assert i1["state"] == "pending", i1
    expect_error(
        dtools.propose_issue, beta["token"], did, "Bad link", feature_id=999999
    )
    assert (
        dtools.decide_issue(alpha["token"], did, i1["issue_id"], True)["approved"]
        is True
    )
    assert dtools.resolve_issue(alpha["token"], did, i1["issue_id"])["resolved"] is True
    f2 = dtools.propose_feature(beta["token"], did, "Paint it yellow")
    dtools.decide_feature(alpha["token"], did, f2["feature_id"], True)
    assert (
        dtools.move_design_item(alpha["token"], did, "feature", f2["feature_id"], "up")[
            "moved"
        ]
        is True
    )
    got = dtools.list_issues(did)
    assert len(got["issues"]) == 1, got["issues"]
    print("  issues: ok")

    # --- Q&A + comments --------------------------------------------------------
    q = dtools.ask_question(beta["token"], did, "What fuel does it take?")
    assert q["state"] == "open", q
    a = dtools.answer_question(alpha["token"], did, q["question_id"], "Sunlight.")
    assert a["state"] == "answered", a
    expect_error(dtools.add_comment, beta["token"], did, "too early")
    _age_design(did)
    assert dtools.enable_comments(alpha["token"], did)["comments_enabled"] is True
    c = dtools.add_comment(beta["token"], did, "First!")
    assert c["comment_id"] > 0, c
    print("  discuss: ok")

    # --- promote 2-step (one pending row forces need_confirm) ------------------
    f3 = dtools.propose_feature(beta["token"], did, "Maybe add sails")
    assert f3["state"] == "pending", f3
    prev = dtools.promote_preview(did)
    assert prev["design_id"] == did, prev
    first = dtools.promote_to_idea(alpha["token"], did, "T", "B")
    assert first.get("need_confirm") is True, first
    done = dtools.promote_to_idea(
        alpha["token"], did, "Promoted idea title", "Promoted body", confirm=True
    )
    assert done["status"] == "promoted", done
    assert done["idea_post_id"] > 0
    expect_error(dtools.propose_feature, beta["token"], did, "too late now")
    print("  promote: ok")

    # --- close 2-step on a second design ------------------------------------------
    d2 = dtools.create_design(alpha["token"], "Tools design two", "Desc two")
    did2 = d2["id"]
    _age_design(did2)
    c1 = dtools.close_design(alpha["token"], did2, confirm=False)
    if c1.get("need_confirm") is True:
        c2 = dtools.close_design(alpha["token"], did2, confirm=True)
        assert c2["status"] == "archived", c2
    else:
        assert c1["status"] == "archived", c1
    still = dtools.get_design(did2)
    assert still["status"] == "archived", still
    print("  close: ok")

    print("test_designs_tools: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
