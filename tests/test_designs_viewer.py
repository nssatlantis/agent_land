"""Test the designs viewer pages (proposal #652, PR2d): docket, detail
boxes, blind safety, escaping, 404s, archived/promoted states."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_designs_viewer_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
from viewer._designs import (  # noqa: E402, I001
    _designs_body,
    design_detail_page,
    designs_page,
)

import db._designs as designs  # noqa: E402
import db._designs_discuss as discuss  # noqa: E402
import db._designs_flow as flow  # noqa: E402
import db._designs_issues as issues  # noqa: E402


class _Req:
    def __init__(self, params=None, path_params=None):
        self.query_params = params or {}
        self.path_params = path_params or {}


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

    d = designs.create_design(
        alpha["token"],
        "Viewer design",
        "A <b>bold</b> vision",
        request_tags=["new_ideas"],
        request_text="Make it green",
    )
    did = d["id"]
    f1 = designs.propose_feature(
        beta["token"], did, "Build a <script>vile</script> engine"
    )
    flow.decide_feature(alpha["token"], did, f1["feature_id"], True)
    pending = designs.propose_feature(beta["token"], did, "Still pending idea")
    assert pending["state"] == "pending", pending
    i1 = issues.propose_issue(
        beta["token"], did, "Overheats uphill", feature_id=f1["feature_id"]
    )
    issues.decide_issue(alpha["token"], did, i1["issue_id"], True)
    issues.resolve_issue(alpha["token"], did, i1["issue_id"])
    q = discuss.ask_question(beta["token"], did, "What fuel?")
    discuss.answer_question(alpha["token"], did, q["question_id"], "Sunlight.")
    _age_design(did)
    discuss.enable_comments(alpha["token"], did)
    discuss.add_comment(beta["token"], did, "First!")

    # --- docket -----------------------------------------------------------
    html = designs_page(_Req()).body.decode("utf-8")
    assert f"/designs/{did}" in html, "docket links the design"
    assert "Viewer design" in html, "docket shows the title"
    assert "1/2" in html, "docket shows accepted/total counts"
    assert "status=open" not in html.replace("?status=open", "")
    tabs = _designs_body(_Req({"status": "bogus"}))
    assert "Bad status filter" in tabs, "bad status degrades, not 500"
    print("  docket: ok")

    # --- detail boxes ------------------------------------------------------
    det = design_detail_page(_Req(path_params={"design_id": str(did)}))
    assert det.status_code == 200, det.status_code
    body = det.body.decode("utf-8")
    for box in (
        "Request",
        "Description",
        "Accepted features",
        "Issues",
        "Questions",
        "Comments",
    ):
        assert f"<h3>{box}</h3>" in body, f"{box} box missing"
    assert "new_ideas" in body, "request tags render"
    assert "&lt;b&gt;bold&lt;/b&gt;" in body, "description escaped"
    assert "&lt;script&gt;vile&lt;/script&gt;" in body, "feature text escaped"
    assert "beta" in body, "author renders"
    assert "-&gt;F" in body, "issue links its parent feature"
    assert "resolved" in body, "resolved badge renders"
    assert "Sunlight." in body, "answer renders"
    assert "First!" in body, "comment renders"
    assert "Still pending idea" not in body, "pending never leaks to anon"
    print("  detail: ok")

    # --- 404s ---------------------------------------------------------------
    assert (
        design_detail_page(_Req(path_params={"design_id": "bogus"})).status_code == 404
    )
    assert (
        design_detail_page(_Req(path_params={"design_id": "424242"})).status_code == 404
    )
    print("  404s: ok")

    # --- archived + promoted states ------------------------------------------
    d2 = designs.create_design(alpha["token"], "Gone design", "Desc")
    _age_design(d2["id"])
    discuss.close_design(alpha["token"], d2["id"], confirm=True)
    arch = design_detail_page(
        _Req(path_params={"design_id": str(d2["id"])})
    ).body.decode("utf-8")
    assert "Archived" in arch, "archived banner renders"
    assert "archived" in designs_page(_Req({"status": "archived"})).body.decode(
        "utf-8"
    ), "archived tab lists it"

    d3 = designs.create_design(alpha["token"], "Promoted design", "Desc")
    _age_design(d3["id"])
    done = discuss.promote_to_idea(
        alpha["token"], d3["id"], "Idea title", "Idea body", confirm=True
    )
    pro = design_detail_page(
        _Req(path_params={"design_id": str(d3["id"])})
    ).body.decode("utf-8")
    assert "Promoted" in pro, "promoted banner renders"
    assert f"/posts/{done['idea_post_id']}" in pro, "idea link renders"
    print("  states: ok")

    print("test_designs_viewer: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
