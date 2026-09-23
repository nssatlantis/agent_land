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
    got = issues.list_issues(did, alpha["token"])
    assert len(got["issues"]) == 2, got["issues"]
    assert got["issues"][1]["feature_text"] == "Build a green engine block"
    blind = issues.list_issues(did, gamma["token"])
    assert len(blind["issues"]) == 1, blind["issues"]
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

    print("test_designs_flow: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
