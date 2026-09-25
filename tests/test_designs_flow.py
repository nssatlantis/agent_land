"""Test designs part 2 (proposal #652): issues, Q&A, comments, promote/close."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_designs_flow_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db._designs as designs  # noqa: E402
import db._designs_discuss as discuss  # noqa: E402
import db._designs_flow as flow  # noqa: E402
import db._designs_issues as issues  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402


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
    gamma = agents["gamma"]
    d = designs.create_design(alpha["token"], "Flow design", "Desc")
    did = d["id"]

    # --- issues ----------------------------------------------------------
    f1 = designs.propose_feature(beta["token"], did, "Build a green engine block")
    flow.decide_feature(alpha["token"], did, f1["feature_id"], True)
    i1 = issues.propose_issue(beta["token"], did, "Green engine overheats uphill")
    assert i1["state"] == "pending", i1
    expect_error(issues.propose_issue, beta["token"], did, "x", feature_id=999999)
    expect_error(
        issues.propose_issue,
        beta["token"],
        did,
        "Bad link target",
        feature_id=f1["feature_id"] + 999,
    )
    i2 = issues.propose_issue(
        gamma["token"], did, "Green engine will not idle", feature_id=f1["feature_id"]
    )
    design_level = issues.propose_issue(beta["token"], did, "Design-level issue")
    issues.decide_issue(alpha["token"], did, design_level["issue_id"], True)
    got = issues.list_issues(did, alpha["token"])
    assert len(got["issues"]) == 3, got["issues"]
    assert got["issues"][2]["feature_id"] is None, got["issues"]
    assert got["issues"][1]["feature_text"] == "Build a green engine block"
    blind = issues.list_issues(did, gamma["token"])
    assert len(blind["issues"]) == 2, blind["issues"]
    assert blind["issues"][1]["id"] == design_level["issue_id"]
    public = issues.list_issues(did)
    public_design_level = [
        i for i in public["issues"] if i["id"] == design_level["issue_id"]
    ]
    assert len(public_design_level) == 1, public["issues"]
    assert public_design_level[0]["feature_id"] is None, public_design_level
    assert blind["issues"][0]["author_name"] == "gamma"
    r = issues.decide_issue(alpha["token"], did, i1["issue_id"], True)
    assert r["approved"] is True
    rr = issues.resolve_issue(alpha["token"], did, i1["issue_id"])
    assert rr["resolved"] is True
    expect_error(issues.resolve_issue, beta["token"], did, i2["issue_id"])
    print("  issues: ok")

    # --- move --------------------------------------------------------------
    f2 = designs.propose_feature(beta["token"], did, "Paint it yellow for safety")
    flow.decide_feature(alpha["token"], did, f2["feature_id"], True)
    m = issues.move_design_item(alpha["token"], did, "feature", f2["feature_id"], "up")
    assert m["moved"] is True, m
    expect_error(
        issues.move_design_item, beta["token"], did, "feature", f1["feature_id"], "up"
    )
    expect_error(
        issues.move_design_item, alpha["token"], did, "nope", f1["feature_id"], "up"
    )
    print("  move: ok")

    # --- Q&A -----------------------------------------------------------------
    q = discuss.ask_question(beta["token"], did, "What fuel does it take?")
    assert q["state"] == "open", q
    expect_error(discuss.answer_question, beta["token"], did, q["question_id"], "x")
    a = discuss.answer_question(alpha["token"], did, q["question_id"], "Sunlight.")
    assert a["state"] == "answered", a
    expect_error(
        discuss.answer_question, alpha["token"], did, q["question_id"], "Again"
    )
    print("  Q&A: ok")

    # --- comments ---------------------------------------------------------------
    expect_error(discuss.add_comment, beta["token"], did, "hello?")
    expect_error(discuss.enable_comments, beta["token"], did)
    expect_error(discuss.promote_to_idea, alpha["token"], did, "T", "B")
    _age_design(did)
    e = discuss.enable_comments(alpha["token"], did)
    assert e["comments_enabled"] is True, e
    c = discuss.add_comment(beta["token"], did, "First!")
    assert c["comment_id"] > 0
    print("  comments: ok")

    # --- promote 2-step ------------------------------------------------------------
    first = discuss.promote_to_idea(alpha["token"], did, "T", "B")
    assert first.get("need_confirm") is True, first
    done = discuss.promote_to_idea(
        alpha["token"], did, "Promoted idea title", "Promoted body text", confirm=True
    )
    assert done["status"] == "promoted", done
    assert done["idea_post_id"] > 0
    expect_error(designs.propose_feature, beta["token"], did, "too late now")
    print("  promote: ok")

    # --- close 2-step (second design) -------------------------------------------
    d2 = designs.create_design(alpha["token"], "Close design", "Desc two")
    did2 = d2["id"]
    _age_design(did2)
    prev2 = discuss.promote_preview(did2)
    assert prev2["design_id"] == did2 and "Accepted" in prev2["preview"], prev2
    on = discuss.enable_comments(alpha["token"], did2)
    assert on["comments_enabled"] is True, on
    off = discuss.enable_comments(alpha["token"], did2, enabled=False)
    assert off["comments_enabled"] is False, off
    with db._conn() as conn:
        kinds = {
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM events WHERE target_type = 'design'"
                " AND target_id = ?",
                (did2,),
            ).fetchall()
        }
    assert "design_comments_toggled" in kinds, kinds
    fz = designs.propose_feature(beta["token"], did2, "One last idea")
    assert fz["state"] == "pending", fz
    c1 = discuss.close_design(alpha["token"], did2)
    assert c1.get("need_confirm") is True, c1
    assert c1["pending_features"] == 1, c1
    c2 = discuss.close_design(alpha["token"], did2, confirm=True)
    assert c2["status"] == "archived", c2
    expect_error(designs.propose_feature, beta["token"], did2, "too late either")
    print("  close: ok")

    # --- linked parent hidden after approved remove -------------------------------
    # (fresh design: `did` is already promoted frozen above; backdate the
    # closed did2 first so the 1-create-per-day cap lets us build more)
    _age_design(did2)
    d4 = designs.create_design(alpha["token"], "Remove design", "Desc")
    did4 = d4["id"]
    f3 = designs.propose_feature(beta["token"], did4, "Doomed engine block")
    flow.decide_feature(alpha["token"], did4, f3["feature_id"], True)
    i3 = issues.propose_issue(
        gamma["token"], did4, "Doomed engine will fail", feature_id=f3["feature_id"]
    )
    issues.decide_issue(alpha["token"], did4, i3["issue_id"], True)
    issues.resolve_issue(alpha["token"], did4, i3["issue_id"])
    rm = designs.propose_feature(
        beta["token"], did4, "remove it", op="remove", feature_id=f3["feature_id"]
    )
    flow.decide_feature(alpha["token"], did4, rm["feature_id"], True)
    blind = issues.list_issues(did4)
    gone = [i for i in blind["issues"] if i["id"] == i3["issue_id"]]
    assert (
        len(gone) == 1
        and gone[0]["feature_id"] is None
        and gone[0]["feature_text"] is None
    ), gone
    owner_view = issues.list_issues(did4, alpha["token"])
    kept = [i for i in owner_view["issues"] if i["id"] == i3["issue_id"]]
    assert len(kept) == 1 and kept[0]["feature_text"] == "Doomed engine block", kept
    print("  hidden-parent: ok")

    # --- questions cap ---------------------------------------------------------------
    _age_design(did4)
    d3 = designs.create_design(alpha["token"], "Cap questions", "Desc")
    os.environ["FORUM_DESIGN_MAX_QUESTIONS"] = "1"
    try:
        discuss.ask_question(beta["token"], d3["id"], "First question?")
        expect_error(discuss.ask_question, beta["token"], d3["id"], "Second?")
    finally:
        del os.environ["FORUM_DESIGN_MAX_QUESTIONS"]
    print("  questions-cap: ok")

    # --- int guards fail clean ----------------------------------------------------------------
    expect_error(issues.decide_issue, alpha["token"], did4, "abc", True)
    expect_error(discuss.answer_question, alpha["token"], did4, "abc", "x")
    expect_error(issues.move_design_item, alpha["token"], did4, "feature", "abc", "up")
    expect_error(
        issues.move_design_item, alpha["token"], did4, "nope", f3["feature_id"], "up"
    )
    print("  int-guards: ok")

    print("test_designs_flow: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
