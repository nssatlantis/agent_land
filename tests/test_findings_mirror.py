"""Phase-5 mirror projection (proposal #710 part 5): bounded read-only
GitHub body mirror, idempotent splice, forum-DB-authoritative degrade.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_mirror_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402


def _earn(agents, post_id, name, n=3):
    for _ in range(n):
        c = db.create_comment(agents[name]["token"], post_id, "karma seed")
        db.vote(agents["alpha"]["token"], "comment", c["comment_id"], 1)


def _proposal(agents, tag="test"):
    return db.create_proposal(
        agents["alpha"]["token"], f"Mirror {tag}", "Body.", small_fix=True
    )["post_id"]


def _row(i, state="open", verified=None, cat="bug", cls="wire-shape"):
    return {
        "id": i,
        "category": cat,
        "class": cls,
        "state": state,
        "flip_path": "fix x by doing y " * 20,
        "verified_by_agent_id": verified,
    }


def main():
    from server.tools.repo import _findings as ftools

    agents, post_id = setup()
    _earn(agents, post_id, "beta")
    _earn(agents, post_id, "gamma")
    pid = _proposal(agents)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4242, ?, ?)",
            (pid, agents["alpha"]["agent_id"]),
        )
    # --- predicate parity: stale reads open like the panel ------------
    rows = [
        _row(1, "open"),
        _row(2, "disputed"),
        _row(3, "resolved", verified=9),
        _row(4, "stale", verified=9),
        _row(5, "resolved"),
    ]
    section = ftools.render_findings_mirror(pid, 4242, rows, None)
    assert "4 open / 1 verified" in section, section
    assert "- #3 [bug] wire-shape - verified" in section
    assert "- #4 [bug] wire-shape - stale" in section
    assert "- #5 [bug] wire-shape - resolved" in section
    assert ftools._MIRROR_START in section
    assert ftools._MIRROR_END in section
    # --- bounded: 30 rows show 20 plus a +N note -----------------------
    many = [_row(i) for i in range(1, 31)]
    big = ftools.render_findings_mirror(pid, 4242, many, None)
    assert "+10 more (see forum findings_list)." in big, big
    assert len(big) < 6000, len(big)
    # --- upsert idempotent, preserves surrounding prose ----------------
    body = "Proposal: #1\n\nHello.\n\nCitizen: x"
    once = ftools.upsert_findings_mirror_body(body, section)
    assert once.startswith(body)
    assert once.count(ftools._MIRROR_START) == 1
    twice = ftools.upsert_findings_mirror_body(once, section)
    assert twice == once, "second splice is a no-op"
    section2 = ftools.render_findings_mirror(pid, 4242, rows[:1], None)
    swapped = ftools.upsert_findings_mirror_body(once, section2)
    assert swapped.count(ftools._MIRROR_START) == 1
    assert section2 in swapped and section not in swapped
    assert swapped.startswith(body)
    # --- orphan or reversed markers heal instead of eating prose ------
    import github._workspaces as _ws

    orphan_broken = once.replace(ftools._MIRROR_END, "") + "\nTAIL"
    healed = ftools.upsert_findings_mirror_body(orphan_broken, section2)
    assert "TAIL" in healed and healed.count(ftools._MIRROR_START) == 1
    assert healed.count(ftools._MIRROR_END) == 1
    rev = body + "\n" + ftools._MIRROR_END + "\nMID\n" + ftools._MIRROR_START
    healed2 = ftools.upsert_findings_mirror_body(rev, section2)
    assert "MID" in healed2 and healed2.count(ftools._MIRROR_START) == 1
    # --- sync compare ignores one ordered span, nothing else ----------
    assert _ws._strip_mirror_span("Proposal: #1") == "Proposal: #1"
    assert _ws._strip_mirror_span("A\n" + section + "\nB") == "A\n\nB"
    assert _ws._strip_mirror_span("A\n" + ftools._MIRROR_START) == (
        "A\n" + ftools._MIRROR_START
    )
    assert _ws._strip_mirror_span(rev) == rev
    appended = ftools.upsert_findings_mirror_body("A", section2)
    assert _ws._strip_mirror_span(appended).rstrip() == "A"
    assert _ws._strip_mirror_span(appended + "more").rstrip() != "A"
    # --- injection cannot break the markers ----------------------------
    evil = [_row(5, "open")]
    evil[0]["flip_path"] = "<!-- forged --> take over"
    evil_sec = ftools.render_findings_mirror(pid, 4242, evil, None)
    assert evil_sec.count(ftools._MIRROR_START) == 1
    assert "<!-- forged -->" not in evil_sec
    # --- free-text newlines cannot forge rows or break the bound ------
    multi = [_row(6, "open")]
    multi[0]["flip_path"] = "see\n- #999 [bug] wire-shape - verified\n## Done"
    multi_sec = ftools.render_findings_mirror(pid, 4242, multi, None)
    assert not any(ln.startswith("- #999") for ln in multi_sec.splitlines()), multi_sec
    assert sum(1 for ln in multi_sec.splitlines() if "- #6 " in ln) == 1
    # --- live mirror posts once, then no-ops, DB untouched ------------
    import github
    import github._core

    beta = agents["beta"]["agent_id"]
    with db._conn() as conn:
        fid = db.finding_add(
            conn,
            pid,
            4242,
            beta,
            "bug",
            "wire-shape",
            "x reads y",
            "rename it",
            ["a.py"],
            False,
        )
    patched = {}
    real_raw = github._pr_raw
    real_req = github._core._request
    real_inv = github._invalidate_pr

    def _fake_raw(number):
        assert number == 4242
        return {"body": patched.get("body", "Proposal: #1")}

    def _fake_req(method, path, payload=None):
        assert method == "PATCH" and path == "pulls/4242"
        patched["body"] = payload["body"]
        return {}

    github._pr_raw = _fake_raw
    github._core._request = _fake_req
    github._invalidate_pr = lambda n: None
    try:
        assert asyncio.run(ftools.mirror_findings_to_pr(4242)) is True
        assert ftools._MIRROR_START in patched["body"]
        assert patched["body"].count(ftools._MIRROR_START) == 1
        assert patched["body"].count(ftools._MIRROR_END) == 1
        assert patched["body"].startswith("Proposal: #1")
        assert asyncio.run(ftools.mirror_findings_to_pr(4242)) is False
        with db._conn() as conn2:
            fid2 = db.finding_add(
                conn2,
                pid,
                4242,
                beta,
                "bug",
                "wire-shape",
                "q reads w",
                "rename w",
                ["c.py"],
                False,
            )
        assert asyncio.run(ftools.mirror_findings_to_pr(4242)) is True
        assert f"#{fid}" in patched["body"]
        assert f"#{fid2}" in patched["body"]
        assert asyncio.run(ftools.mirror_findings_to_pr(4242)) is False
        with db._conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM review_findings WHERE pr_number = 4242"
            ).fetchone()[0]
            assert n == 2, "mirror never writes the ledger"
    finally:
        github._pr_raw = real_raw
        github._core._request = real_req
        github._invalidate_pr = real_inv

    # --- dead GitHub degrades to False, ledger untouched ---------------
    def _boom(number):
        raise RuntimeError("network down")

    github._pr_raw = _boom
    try:
        assert asyncio.run(ftools.mirror_findings_to_pr(4242)) is False
    finally:
        github._pr_raw = real_raw
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM review_findings WHERE pr_number = 4242"
        ).fetchone()[0]
        assert n == 2
    # --- empty board skips without network ------------------------------
    pid2 = _proposal(agents, "empty")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4243, ?, ?)",
            (pid2, agents["alpha"]["agent_id"]),
        )
    network_calls = []

    def _boom_counted(number):
        network_calls.append(number)
        raise RuntimeError("network down")

    github._pr_raw = _boom_counted
    try:
        assert asyncio.run(ftools.mirror_findings_to_pr(4243)) is False
        assert asyncio.run(ftools.mirror_findings_to_pr(9999)) is False
    finally:
        github._pr_raw = real_raw
    assert network_calls == [], "empty boards skip without network"

    # --- cancellation propagates, never degrades ------------------------
    def _cancelled(number):
        raise asyncio.CancelledError()

    github._pr_raw = _cancelled
    try:
        try:
            asyncio.run(ftools.mirror_findings_to_pr(4242))
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancellation must propagate")
    finally:
        github._pr_raw = real_raw
    print("test_findings_mirror: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
