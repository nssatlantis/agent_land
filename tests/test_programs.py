"""Test the program/arc ledger (proposal #529): schema migration,
create/list/get, reconciliation maps, N+1 query budget, claims, name
release on complete, check_in parity, events and notifications."""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_programs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    config,
    db,
    expect_error,
    setup,
)


def _pr_row(pr_number: int, state: str, head_sha: str) -> None:
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_rows (pr_number, state, head_sha) VALUES (?, ?, ?)",
            (pr_number, state, head_sha),
        )


def _pr_merged(pr_number: int, agent_id: int, bar=4, mode="auto") -> None:
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_merges (pr_number, agent_id, merged_at,"
            " bar_at_decision, merge_mode)"
            " VALUES (?, ?, '2026-01-01T00:00:00Z', ?, ?)",
            (pr_number, agent_id, bar, mode),
        )


def _pr_recorded(pr_number: int, agent_id: int, status: str) -> None:
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_record (pr_number, agent_id, status, closed_at)"
            " VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
            (pr_number, agent_id, status),
        )


def _events_kinds() -> set[str]:
    with db._conn() as conn:
        return {r["kind"] for r in conn.execute("SELECT kind FROM events").fetchall()}


def _notifications_bodies() -> list[str]:
    with db._conn() as conn:
        return [
            r["body"] for r in conn.execute("SELECT body FROM notifications").fetchall()
        ]


def _counting_wrapper():
    """Wrap db._programs._conn so every execute() is counted."""
    count = [0]
    orig = db._programs._conn

    def wrapper(*args, **kwargs):
        inner = orig(*args, **kwargs)

        @contextlib.contextmanager
        def cm():
            with inner as conn:

                class P:
                    def __init__(self, c):
                        self._c = c

                    def execute(self, *a, **kw):
                        count[0] += 1
                        return self._c.execute(*a, **kw)

                    def __getattr__(self, name):
                        return getattr(self._c, name)

                yield P(conn)

        return cm()

    return count, wrapper, orig


def main():
    agents, _post_id = setup()
    alpha = agents["alpha"]
    beta = agents["beta"]

    # --- schema migration -------------------------------------------------
    # A pre-#529 database lacks the programs tables. init_db() re-runs
    # schema.sql, so CREATE TABLE IF NOT EXISTS must create them and the
    # five indexes must exist.
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS program_items")
        conn.execute("DROP TABLE IF EXISTS programs")
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
    assert "programs" in tables, "programs table missing after migration"
    assert "program_items" in tables, "program_items table missing"
    for idx in (
        "idx_programs_owner",
        "idx_programs_status",
        "idx_program_items_program",
        "idx_program_items_ref",
        "idx_program_items_claimed",
    ):
        assert idx in indexes, f"index {idx} missing after migration"

    # --- create_program ---------------------------------------------------
    prog = db.create_program(alpha["token"], "Arc One")
    assert prog["id"] > 0
    assert prog["owner_id"] == alpha["agent_id"]
    assert prog["status"] == "active"
    assert expect_error(db.create_program, alpha["token"], "arc one")
    assert expect_error(db.create_program, alpha["token"], "")
    assert expect_error(db.create_program, alpha["token"], "x" * 81)
    assert "program_created" in _events_kinds()

    # --- add_program_item -------------------------------------------------
    bug1 = db.file_bug_report(beta["token"], "Bug one", "body")
    bug2 = db.file_bug_report(beta["token"], "Bug two", "body")
    _pr_row(7001, "open", "sha-open-1")
    _pr_row(7002, "open", "sha-open-2")

    item_b1 = db.add_program_item(alpha["token"], prog["id"], "bug", bug1["id"])
    assert item_b1["ref_type"] == "bug"
    assert item_b1["ref_id"] == bug1["id"]
    assert item_b1["state"] == "pending"  # open bug reconciles pending
    item_p1 = db.add_program_item(alpha["token"], prog["id"], "pr", 7001)
    assert item_p1["ref_type"] == "pr"
    assert item_p1["head_sha"] == "sha-open-1"
    assert item_p1["state"] == "in-flight"
    assert expect_error(
        db.add_program_item, alpha["token"], prog["id"], "bug", bug1["id"]
    )
    assert expect_error(db.add_program_item, alpha["token"], prog["id"], "foo", 1)
    assert expect_error(
        db.add_program_item, beta["token"], prog["id"], "bug", bug2["id"]
    )  # non-owner refused
    assert "program_item_added" in _events_kinds()

    # --- reconciliation maps ------------------------------------------------
    # bug: open -> pending (already), confirmed -> in-flight, fixed -> done,
    # closed -> dropped.
    db.confirm_bug_report(bug2["id"])
    db.add_program_item(alpha["token"], prog["id"], "bug", bug2["id"])
    db.fix_bug_report(bug1["id"])

    # PR: merged -> done (held), declined/closed -> blocked, open ->
    # in-flight with head-moved flag, missing row -> blocked.
    _pr_merged(7002, beta["agent_id"], bar=4, mode="auto")
    db.add_program_item(alpha["token"], prog["id"], "pr", 7002)
    _pr_recorded(7003, beta["agent_id"], "declined")
    db.add_program_item(alpha["token"], prog["id"], "pr", 7003)
    _pr_recorded(7004, beta["agent_id"], "closed")
    db.add_program_item(alpha["token"], prog["id"], "pr", 7004)
    # broken reference: no source row at all
    db.add_program_item(alpha["token"], prog["id"], "bug", 999999)
    db.add_program_item(alpha["token"], prog["id"], "pr", 888888)

    detail = db.get_program(prog["id"])
    by_ref = {(it["ref_type"], it["ref_id"]): it for it in detail["items"]}
    assert by_ref[("bug", bug1["id"])]["state"] == "done"
    assert by_ref[("bug", bug2["id"])]["state"] == "in-flight"
    assert by_ref[("pr", 7001)]["state"] == "in-flight"
    assert by_ref[("pr", 7001)]["head_sha"] == "sha-open-1"
    assert by_ref[("pr", 7001)]["head_moved"] is False
    assert by_ref[("pr", 7002)]["state"] == "done"
    assert by_ref[("pr", 7002)]["held"] is True
    assert by_ref[("pr", 7002)]["bar_at_decision"] == 4
    assert by_ref[("pr", 7002)]["merge_mode"] == "auto"
    assert by_ref[("pr", 7003)]["state"] == "blocked"
    assert by_ref[("pr", 7004)]["state"] == "blocked"
    assert by_ref[("bug", 999999)]["state"] == "blocked"
    assert by_ref[("pr", 888888)]["state"] == "blocked"

    # head-moved flag: the PR's head moved since the item was added.
    with db._conn() as conn:
        conn.execute(
            "UPDATE pr_rows SET head_sha = 'sha-open-1b' WHERE pr_number = 7001"
        )
    detail = db.get_program(prog["id"])
    assert detail["items"][0]["head_moved"] is True or any(
        it["ref_id"] == 7001 and it["head_moved"] is True for it in detail["items"]
    )

    # --- N+1 query budget --------------------------------------------------
    # Reconciliation must not run one query per item: items + bug statuses +
    # pr_rows + pr_merges + pr_record = at most 5 queries, constant in the
    # item count.
    count, wrapper, orig = _counting_wrapper()
    db._programs._conn = wrapper
    with db._programs._conn(immediate=True) as conn:
        items = db._programs._items_for(conn, prog["id"])
        db._programs._reconcile_items(conn, items)
    db._programs._conn = orig
    assert count[0] <= 5, f"reconciliation ran {count[0]} queries (N+1 leak)"
    # Add more items: the budget must not grow.
    for i in range(3):
        b = db.file_bug_report(beta["token"], f"Extra {i}", "body")
        db.add_program_item(alpha["token"], prog["id"], "bug", b["id"])
    count2, wrapper2, orig2 = _counting_wrapper()
    db._programs._conn = wrapper2
    with db._programs._conn(immediate=True) as conn:
        items = db._programs._items_for(conn, prog["id"])
        db._programs._reconcile_items(conn, items)
    db._programs._conn = orig2
    assert count2[0] <= 5, f"budget grew with item count: {count2[0]}"

    # --- claims -------------------------------------------------------------
    cap = config.MAX_CLAIMS_PER_COLLABORATOR
    it_a = detail["items"][0]
    claim = db.claim_program_item(beta["token"], prog["id"], it_a["id"])
    assert claim["claimed_by_id"] == beta["agent_id"]
    assert expect_error(
        db.claim_program_item, alpha["token"], prog["id"], it_a["id"]
    )  # already claimed
    assert expect_error(
        db.claim_program_item, beta["token"], prog["id"], it_a["id"]
    )  # self re-claim
    # cap: claim up to the cap, then one more is refused.
    others = [it for it in detail["items"] if it["id"] != it_a["id"]]
    for it in others[: max(0, cap - 1)]:
        db.claim_program_item(beta["token"], prog["id"], it["id"])
    if cap > 0:
        assert expect_error(
            db.claim_program_item, beta["token"], prog["id"], others[cap - 1]["id"]
        )
    assert "program_claimed" in _events_kinds()
    db.release_program_item(beta["token"], prog["id"], it_a["id"])
    assert "program_unclaimed" in _events_kinds()
    assert expect_error(
        db.release_program_item, alpha["token"], prog["id"], it_a["id"]
    )  # not claimed, non-claimer+non-owner path is owner-allowed; use beta
    # owner may release a claim held by someone else
    db.claim_program_item(beta["token"], prog["id"], it_a["id"])
    db.release_program_item(alpha["token"], prog["id"], it_a["id"])

    # expiry sweep: backdate a claim past the timeout, then a read releases it.
    saved_timeout = config.CLAIM_TIMEOUT_SECONDS
    config.CLAIM_TIMEOUT_SECONDS = 1
    try:
        with db._conn() as conn:
            conn.execute(
                "UPDATE program_items SET claimed_at = "
                "'2020-01-01T00:00:00Z' WHERE id = ?",
                (it_a["id"],),
            )
        detail = db.get_program(prog["id"])
        it_a_now = next(it for it in detail["items"] if it["id"] == it_a["id"])
        assert it_a_now["claimed_by_id"] is None, "expired claim not swept"
    finally:
        config.CLAIM_TIMEOUT_SECONDS = saved_timeout

    # --- name release on complete / auto-archive ---------------------------
    # A fresh program whose items can ALL become done: one open bug and one
    # merged PR.  Completing it exercises the auto-archive + name release.
    comp = db.create_program(alpha["token"], "Arc Two")
    cb = db.file_bug_report(beta["token"], "Comp bug", "body")
    db.add_program_item(alpha["token"], comp["id"], "bug", cb["id"])
    _pr_row(7101, "open", "sha-comp-1")
    _pr_merged(7101, beta["agent_id"], bar=4, mode="auto")
    db.add_program_item(alpha["token"], comp["id"], "pr", 7101)
    # Fix the bug so every item reconciles to done.
    db.fix_bug_report(cb["id"])
    detail = db.get_program(comp["id"])
    assert detail["complete"] is True, "all-done program must be complete"
    docket = db.list_programs()["programs"]
    assert all(p["id"] != comp["id"] for p in docket), (
        "complete program must auto-archive out of the active docket"
    )
    # the name is released: another program may reuse it.
    prog2 = db.create_program(alpha["token"], "Arc Two")
    assert prog2["id"] != comp["id"]
    # archived/abandoned also release the name.
    db.update_program(alpha["token"], prog2["id"], "abandoned")
    prog3 = db.create_program(alpha["token"], "Arc Two")
    assert prog3["id"] != prog2["id"]
    assert "program_completed" in _events_kinds()
    assert "program_updated" in _events_kinds()

    # --- check_in parity ----------------------------------------------------
    # An active, non-complete program owned by alpha surfaces in check_in and
    # in _actionable_ids' surfaces under the same predicate.  "Arc One" has
    # blocked items, so it stays non-complete.
    detail = db.get_program(prog["id"])
    assert detail["complete"] is False
    check = db.check_in(alpha["token"])
    assert any("program" in a.lower() for a in check["suggested_actions"]), (
        "check_in must surface the active program line"
    )
    from db._agent import _actionable_ids

    with db._conn() as conn:
        surfaces = _actionable_ids(conn, alpha["agent_id"])
    # "Arc One" is active + non-complete; "Arc Two" is complete (auto-archived
    # from the docket) so it must NOT surface.
    assert surfaces["surfaces"]["programs"] == [prog["id"]], (
        f"_actionable_ids programs surface mismatch: {surfaces['surfaces']['programs']}"
    )

    # --- notifications -------------------------------------------------------
    # An advance (pending -> done) notifies the owner.
    bodies = _notifications_bodies()
    assert any("advanced" in b for b in bodies), "no advance notification"
    assert any("complete" in b.lower() for b in bodies), "no complete notification"

    print("test_programs: ok")


if __name__ == "__main__":
    main()
