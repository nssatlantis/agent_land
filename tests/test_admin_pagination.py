"""Admin pagination regression tests."""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_admin_pagination_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ.setdefault("FORUM_POST_COOLDOWN_SECONDS", "0")
os.environ.setdefault("FORUM_PROPOSAL_COOLDOWN_SECONDS", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.admin._jobs import _render_jobs, _render_jobs_manager  # noqa: E402
from server.admin._posts import _render_posts_manager, _render_proposals  # noqa: E402
from tests._setup import db, setup  # noqa: E402


def _request(**query):
    return SimpleNamespace(
        query_params=query,
        cookies=SimpleNamespace(get=lambda _key, _default=None: _default),
        state=SimpleNamespace(csrf_token="test-csrf"),
    )


def _contains_all(text, values):
    return all(value in text for value in values)


def test_admin_pagination():
    agents, _ = setup()
    token = agents["beta"]["token"]

    for i in range(25):
        db.create_proposal(token, f"page-proposal-{i:02d}", "body")
    proposal_page_1 = _render_proposals(_request())
    proposal_page_2 = _render_proposals(_request(proposals_page="2"))
    proposal_page_999 = _render_proposals(_request(proposals_page="999"))
    assert _contains_all(
        proposal_page_1,
        [f"page-proposal-{i:02d}" for i in range(5, 25)],
    )
    assert "page-proposal-04" not in proposal_page_1
    assert _contains_all(
        proposal_page_2,
        [f"page-proposal-{i:02d}" for i in range(5)],
    )
    assert "page-proposal-24" not in proposal_page_2
    assert "page 2 of 2" in proposal_page_2
    assert "page 2 of 2" in proposal_page_999

    for i in range(25):
        db.create_post(token, f"page-post-{i:02d}", "body")
    post_page_1 = _render_posts_manager(_request(kind="post", q="page-post"))
    post_page_2 = _render_posts_manager(_request(kind="post", q="page-post", page="2"))
    assert _contains_all(
        post_page_1,
        [f"page-post-{i:02d}" for i in range(5, 25)],
    )
    assert "page-post-04" not in post_page_1
    assert _contains_all(
        post_page_2,
        [f"page-post-{i:02d}" for i in range(5)],
    )
    assert "page-post-24" not in post_page_2
    assert "page=2" in post_page_2
    assert "Showing 20 of 25 filtered" in post_page_1

    job_ids = set()
    for i in range(25):
        job_ids.add(
            db.create_job_official(
                "root",
                None,
                f"page-job-{i:02d}",
                "Do the work.",
                1.0,
                ["step one"],
                kind="one_time",
                cycles=1,
            )["job_id"]
        )
    jobs_page_1 = db.admin_list_jobs(q="page-job", limit=20, page=1)
    jobs_page_2 = db.admin_list_jobs(q="page-job", limit=20, page=2)
    assert jobs_page_1["total"] == 25
    assert len(jobs_page_1["jobs"]) == 20
    assert len(jobs_page_2["jobs"]) == 5
    assert set(j["job_id"] for j in jobs_page_1["jobs"]).isdisjoint(
        j["job_id"] for j in jobs_page_2["jobs"]
    )
    assert job_ids == set(j["job_id"] for j in jobs_page_1["jobs"]) | set(
        j["job_id"] for j in jobs_page_2["jobs"]
    )
    assert "status=open" in _render_jobs_manager(
        _request(status="open", q="page-job", page="2")
    )
    assert "q=page-job" in _render_jobs_manager(
        _request(status="open", q="page-job", page="2")
    )
    jobs_html_1 = _render_jobs_manager(_request(q="page-job"))
    jobs_html_2 = _render_jobs_manager(_request(q="page-job", page="2"))
    assert "page-job-24" in jobs_html_1
    assert "page-job-04" not in jobs_html_1
    assert "page-job-00" in jobs_html_2
    assert "page 2 of 2" in jobs_html_2
    assert "page=2" in jobs_html_2

    dashboard_jobs_1 = _render_jobs(_request())
    dashboard_jobs_2 = _render_jobs(_request(jobs_page="2"))
    assert "page-job-24" in dashboard_jobs_1
    assert "page-job-04" not in dashboard_jobs_1
    assert "page-job-00" in dashboard_jobs_2
    assert "page 2 of 2" in dashboard_jobs_2
    assert "proposals_page" not in dashboard_jobs_1
    assert "jobs_page=2" in dashboard_jobs_1
    assert "proposals_page" not in dashboard_jobs_2
    assert "jobs_page=2" in dashboard_jobs_2

    with db._conn(immediate=True) as conn:
        db._credits.grant(
            agents["gamma"]["agent_id"],
            10,
            "test_sponsored_deposit",
            target_type="test",
            target_id=1,
            conn=conn,
        )
    sponsored = db.create_job_official(
        "root",
        agents["alpha"]["name"],
        "sponsored-active",
        "Review sponsor owns the decision.",
        1.0,
        ["step one"],
        kind="one_time",
        cycles=1,
        taker_deposit_credits=0.25,
        offer_to=agents["gamma"]["name"],
    )
    db.accept_job_offer(agents["gamma"]["token"], sponsored["job_id"])
    sponsored_board = db.admin_list_jobs(q="sponsored-active", limit=20, page=1)
    assert sponsored_board["jobs"][0]["creator_agent_id"] == agents["alpha"]["agent_id"]
    assert f"/admin/jobs/{sponsored['job_id']}/review" not in _render_jobs(_request())

    status_ids = {}
    for status in ("offered", "active", "completed", "cancelled", "expired"):
        status_ids[status] = db.create_job_official(
            "root",
            None,
            f"status-{status}",
            "Status coverage.",
            1.0,
            ["step one"],
            kind="one_time",
            cycles=1,
        )["job_id"]
    with db._conn() as conn:
        conn.executemany(
            "UPDATE jobs SET status = ? WHERE id = ?",
            [(status, job_id) for status, job_id in status_ids.items()],
        )
    for status, job_id in status_ids.items():
        result = db.admin_list_jobs(statuses=(status,), q=f"status-{status}")
        assert result["total"] == 1
        assert result["jobs"][0]["job_id"] == job_id
    assert db.admin_list_jobs(status="closed", q="status-")["total"] == 2


if __name__ == "__main__":
    test_admin_pagination()
    print("test_admin_pagination: all assertions passed")
