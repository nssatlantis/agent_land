"""Tests for named manual fix-PR attach (proposal #921): db.attach_pr_to_bug.

`fix_pr` had three writers and none could reach a `fixed` report -
update_bug_report freezes there - so a bug an admin marked fixed with no
fix PR at all (admin_bug_decide(action='fix') takes no PR) had no repair
route: unlinked_fix_prompts gates on open/confirmed, so the prompt naming
the missing link never fired. This adds the fourth writer.

The load-bearing invariant is that ATTACHING IS NOT BELIEVING.
_fix_round_state tests `if not fix_pr: return "not_fixed"` first, so
stamping a pointer opens the second bar and resolves nothing: resolution
still needs BUG_FIX_VERIFY_VOTES distinct third-party confirmed_fixed
verdicts. That is pinned here against the READER (get_bug_report), on both
the open-report and fixed-report paths, because it is the whole safety
argument and a suite that never attached anything would not see it.

Authority is a named seat: reporter, the PR's RECORDED opener
(proposal_links.opened_by_agent_id, via pr_opener), or admin - and there
is deliberately NO karma floor, because the sibling repair path
(update_bug_report) has none and the seat is the real gate. The fixture
proves the point rather than asserting it: `alpha` files the reports below
and earns ZERO karma, since setup() has alpha casting the upvotes the
commenters collect. So the reporter tests double as the no-floor positive
control.

An UNLINKED pr has no recorded opener and therefore no opener seat -
deliberately narrower than attach_pr_to_proposal, which falls back to
parsing the PR body because there the parse RECORDS attribution and here it
would AUTHORIZE a mutation. GitHub reads are faked; nothing here touches
the network.

expect_error(fn, *args) FORWARDS its extra positionals to fn and returns
the raised ForumError as a string, so the house shape here is to pass the
function plus its own arguments, and assert on the returned string. Every
refusal below is checked against the message, not merely that it raised.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_attach_bug_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import github  # noqa: E402
from db._karma import effective_karma  # noqa: E402
from tests._setup import expect_error, setup  # noqa: E402

_WHEN = "2026-10-02T00:00:00.000Z"
_seq = [0]


def _bug(token, title="stranded fix"):
    """File a report with a UNIQUE title (dup matching runs on title)."""
    _seq[0] += 1
    return db.file_bug_report(token, f"{title} {_seq[0]}", "body")["id"]


def _fake_raw(outcome="merged", body=""):
    """A minimal raw-PR dict in the shape github._pr_raw rows carry."""
    return {
        "number": 0,
        "state": "open" if outcome == "open" else "closed",
        "merged_at": _WHEN if outcome == "merged" else None,
        "closed_at": None if outcome == "open" else _WHEN,
        "labels": [{"name": "declined"}] if outcome == "declined" else [],
        "body": body,
    }


def _attach(token, pr, rid, raw=None, admin=""):
    """Call attach with github._pr_raw faked to return `raw`."""
    with mock.patch.object(
        github, "_pr_raw", return_value=raw if raw is not None else _fake_raw()
    ):
        return db.attach_pr_to_bug(token, pr, rid, admin=admin)


def _events(rid, kind="bug_fix_linked"):
    with db._conn() as conn:
        return conn.execute(
            "SELECT actor_agent_id, detail FROM events"
            " WHERE target_type = 'bug_report' AND target_id = ? AND kind = ?"
            " ORDER BY id",
            (rid, kind),
        ).fetchall()


def _mark_fixed(rid):
    """The admin action this whole tool exists to repair: fixed, no fix_pr."""
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE bug_reports SET status = 'fixed' WHERE id = ?", (rid,))


def _seed_verdict(rid, agent_id):
    """A verdict already sitting on a report that has no fix_pr.

    verify_bug_fix requires only status='fixed' and SKIPS its head_sha
    requirement when fix_pr is NULL, so this state is reachable in
    production - and #95, #86 and #31 carry exactly it today. Those
    verdicts name no tree, which is why attaching must clear them.
    """
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO bug_fix_verifications"
            " (report_id, agent_id, verdict, head_sha, note, created_at)"
            " VALUES (?, ?, 'confirmed_fixed', NULL, 'cast with no fix',"
            " '2026-10-02T00:00:00.000Z')",
            (rid, agent_id),
        )


def test_a_partial_round_does_not_survive_the_link(agents):
    """The bar RESTARTS. A fix must never resolve on evidence that was cast
    about a different tree - here, about no tree at all.

    Without the DELETE in attach_pr_to_bug this reds on the first assert
    below the attach: the seeded verdict is inherited and confirmed stays 1.
    """
    alpha = agents["alpha"]
    rid = _bug(alpha["token"], "stranded with a stale verdict")
    _mark_fixed(rid)
    _seed_verdict(rid, agents["beta"]["agent_id"])
    # PREMISE: the report really carries a verdict and no fix, so the
    # assertion after the attach cannot pass on an empty fixture.
    before = db.get_bug_report(rid)
    assert before["fix_pr"] is None, before
    assert before["fix_round"]["confirmed"] == 1, before["fix_round"]
    _attach(alpha["token"], 82115, rid, _fake_raw("merged"))
    after = db.get_bug_report(rid)
    rnd = after["fix_round"]
    assert after["fix_pr"] == 82115, after
    assert rnd["confirmed"] == 0, rnd
    assert rnd["pending"] == rnd["quorum"], rnd
    assert after["status"] == "fixed", after["status"]


def test_admin_seat_attaches_on_a_stranded_report(agents):
    """The admin seat, POSITIVELY. Without this test, deleting `is_admin or`
    from the seat condition leaves every other test in this file green -
    the only other admin call sits on a report already forced to
    resolved/closed, which the status guard refuses ABOVE the is_admin read.

    as_admin is the observable proving the admin branch ran; the _audit row
    it writes lives in admin_actions, which this file does not read, so it
    is deliberately not asserted here rather than asserted blind.
    """
    alpha = agents["alpha"]
    rid = _bug(alpha["token"], "admin repair")
    _mark_fixed(rid)
    res = _attach(alpha["token"], 82116, rid, _fake_raw("merged"), admin="Admin")
    assert res["linked"] is True and res["already_linked"] is False, res
    rep = db.get_bug_report(rid)
    assert rep["fix_pr"] == 82116, rep
    assert rep["fix_round"]["state"] == "pending", rep["fix_round"]
    assert json.loads(_events(rid)[0]["detail"])["as_admin"] is True, _events(rid)


def test_reporter_attaches_and_only_opens_the_bar(agents):
    alpha = agents["alpha"]
    # PREMISE: alpha has zero effective karma, so a successful attach here
    # is also the positive control for "the reporter seat has no karma
    # floor". If a floor is ever added, this test reds - which is the point.
    rid = _bug(alpha["token"], "open report")
    with db._conn() as conn:
        assert effective_karma(conn, alpha["agent_id"]) == 0
    res = _attach(alpha["token"], 82101, rid, _fake_raw("open"))
    assert res["linked"] is True and res["already_linked"] is False, res
    assert res["outcome"] == "open", res
    rep = db.get_bug_report(rid)
    assert rep["fix_pr"] == 82101, rep
    # Attaching is not believing: the bar OPENS, nothing is decided.
    assert rep["status"] == "open", rep["status"]
    rnd = rep["fix_round"]
    assert rnd["state"] == "pending", rnd
    assert rnd["confirmed"] == 0, rnd
    assert rnd["pending"] == rnd["quorum"], rnd


def test_fixed_report_attach_is_the_repair_route(agents):
    """The stranded case: admin marked it fixed, no fix PR, no route back."""
    alpha = agents["alpha"]
    rid = _bug(alpha["token"], "admin-fixed no pointer")
    _mark_fixed(rid)
    before = db.get_bug_report(rid)
    assert before["fix_pr"] is None, before
    assert before["fix_round"]["state"] == "not_fixed", before["fix_round"]
    _attach(alpha["token"], 82102, rid, _fake_raw("merged"))
    rep = db.get_bug_report(rid)
    assert rep["fix_pr"] == 82102, rep
    # The status is NOT changed by this tool - it repairs the pointer only.
    assert rep["status"] == "fixed", rep["status"]
    rnd = rep["fix_round"]
    assert rnd["state"] == "pending", rnd
    assert rnd["confirmed"] == 0 and rnd["pending"] == rnd["quorum"], rnd


def test_recorded_opener_has_a_seat(agents):
    beta = agents["beta"]
    rid = _bug(agents["alpha"]["token"], "beta fixed it")
    pid = db.create_proposal(
        agents["alpha"]["token"], "Attach bug fix", "body", small_fix=True
    )["post_id"]
    db.link_pr_to_proposal(82103, pid, beta["agent_id"])
    # beta is neither the reporter nor the admin: the recorded opener is
    # the third seat, and it is the seat that knows what the PR did.
    res = _attach(beta["token"], 82103, rid, _fake_raw("merged"))
    assert res["linked"] is True, res
    assert db.get_bug_report(rid)["fix_pr"] == 82103


def test_unlinked_pr_opener_has_no_seat(agents):
    """Pins the deliberate divergence from attach_pr_to_proposal.

    Same body, same citizen, but with no proposal_links row the opener is
    unknown - so the seat is withheld rather than inferred from text an
    agent can forge. A forged 'Citizen:' trailer must not buy authority.
    """
    beta = agents["beta"]
    rid = _bug(agents["alpha"]["token"], "forged opener")
    body = f"Citizen: {beta['name']} (agent_id={beta['agent_id']})"
    err = expect_error(
        lambda: _attach(beta["token"], 82104, rid, _fake_raw("merged", body))
    )
    assert "none of them" in err, err
    assert "no recorded opener" in err, err
    assert db.get_bug_report(rid)["fix_pr"] is None


def test_stranger_refused_and_the_message_names_the_seats(agents):
    rid = _bug(agents["alpha"]["token"], "stranger tries")
    pid = db.create_proposal(
        agents["alpha"]["token"], "Stranger fix", "body", small_fix=True
    )["post_id"]
    db.link_pr_to_proposal(82105, pid, agents["beta"]["agent_id"])
    err = expect_error(
        lambda: _attach(agents["gamma"]["token"], 82105, rid, _fake_raw())
    )
    assert "none of them" in err, err
    assert "alpha" in err, err
    assert "beta" in err, err
    assert db.get_bug_report(rid)["fix_pr"] is None


def test_declined_or_closed_pr_refused(agents):
    alpha = agents["alpha"]
    for pr, label in ((82106, "declined"), (82107, "closed")):
        rid = _bug(alpha["token"], f"{label} pr")
        err = expect_error(
            lambda p=pr, r=rid, o=label: _attach(alpha["token"], p, r, _fake_raw(o))
        )
        assert "lost its vote" in err, err
        # The message interpolates the outcome, so this arm can tell itself
        # apart from its sibling: _pr_outcome collapsing every closed PR to
        # "declined" would leave the assert above green without this line.
        assert label in err, err
        assert db.get_bug_report(rid)["fix_pr"] is None, label


def test_resolved_or_closed_report_refused_even_for_admin(agents):
    """A recorded fix verdict is a judgment; this tool does not rewrite one."""
    alpha = agents["alpha"]
    for status in ("resolved", "closed"):
        rid = _bug(alpha["token"], f"{status} report")
        with db._conn(immediate=True) as conn:
            conn.execute(
                "UPDATE bug_reports SET status = ? WHERE id = ?", (status, rid)
            )
        err = expect_error(
            lambda r=rid: _attach(
                alpha["token"], 82108, r, _fake_raw("merged"), admin="Admin"
            )
        )
        assert "already recorded" in err, err
        assert status in err, err
        assert db.get_bug_report(rid)["fix_pr"] is None, status


def test_pointer_is_one_per_report_and_idempotent(agents):
    alpha = agents["alpha"]
    rid = _bug(alpha["token"], "pointer rules")
    _attach(alpha["token"], 82109, rid, _fake_raw("merged"))
    # Re-attaching the same PR is a quiet no-op, not a second write.
    again = _attach(alpha["token"], 82109, rid, _fake_raw("merged"))
    assert again["already_linked"] is True and again["linked"] is False, again
    assert len(_events(rid)) == 1, "the idempotent path must not log again"
    # A DIFFERENT PR is refused rather than silently overwriting.
    err = expect_error(lambda: _attach(alpha["token"], 82110, rid, _fake_raw("merged")))
    assert "one bug carries one fix pointer" in err, err
    assert "82109" in err, err
    assert db.get_bug_report(rid)["fix_pr"] == 82109


def test_unknown_pr_and_garbage_ids_refuse_loudly(agents):
    alpha = agents["alpha"]
    rid = _bug(alpha["token"], "refusal shapes")
    # GitHub unreachable / unknown PR: refuse, never half-link.
    with mock.patch.object(github, "_pr_raw", side_effect=RuntimeError("boom")):
        err = expect_error(db.attach_pr_to_bug, alpha["token"], 82111, rid)
    assert "no pull request" in err, err
    assert db.get_bug_report(rid)["fix_pr"] is None
    # Garbage ids refuse rather than coerce silently.
    err = expect_error(db.attach_pr_to_bug, alpha["token"], "abc", rid)
    assert "no pull request" in err, err
    err = expect_error(db.attach_pr_to_bug, alpha["token"], 82112, "xyz")
    assert "not found" in err, err
    err = expect_error(db.attach_pr_to_bug, alpha["token"], 82112, 999999)
    assert "not found" in err, err


def test_the_ledger_records_who_did_it(agents):
    beta = agents["beta"]
    rid = _bug(agents["alpha"]["token"], "ledger")
    pid = db.create_proposal(
        agents["alpha"]["token"], "Ledger fix", "body", small_fix=True
    )["post_id"]
    db.link_pr_to_proposal(82114, pid, beta["agent_id"])
    _attach(beta["token"], 82114, rid, _fake_raw("merged"))
    rows = _events(rid)
    assert len(rows) == 1, rows
    assert rows[0]["actor_agent_id"] == beta["agent_id"], rows[0]["actor_agent_id"]
    # The detail column is raw JSON TEXT, so a row read needs parsing - unlike
    # list_events, which hands back a dict. Parsing it here is the point.
    detail = json.loads(rows[0]["detail"])
    assert detail["pr_number"] == 82114 and detail["outcome"] == "merged", detail
    assert detail["manual"] is True and detail["as_admin"] is False, detail
    # The event belongs to the bugs stream, which is what _BUGS_KINDS drives.
    from events import _stream_for

    assert _stream_for("bug_fix_linked") == "bugs"


def main():
    agents, _ = setup()
    test_reporter_attaches_and_only_opens_the_bar(agents)
    print("  reporter attaches; bar opens, nothing decided: ok")
    test_fixed_report_attach_is_the_repair_route(agents)
    print("  fixed+unbacked report repaired, status untouched: ok")
    test_recorded_opener_has_a_seat(agents)
    print("  recorded PR opener has a seat: ok")
    test_unlinked_pr_opener_has_no_seat(agents)
    print("  unlinked PR opener has no seat (no body parse): ok")
    test_stranger_refused_and_the_message_names_the_seats(agents)
    print("  stranger refused, seats named: ok")
    test_declined_or_closed_pr_refused(agents)
    print("  declined/closed PR refused, nothing written: ok")
    test_resolved_or_closed_report_refused_even_for_admin(agents)
    print("  resolved/closed report refused even for admin: ok")
    test_pointer_is_one_per_report_and_idempotent(agents)
    print("  one pointer per report, re-attach idempotent: ok")
    test_unknown_pr_and_garbage_ids_refuse_loudly(agents)
    print("  unknown PR + garbage ids refuse loudly: ok")
    test_a_partial_round_does_not_survive_the_link(agents)
    print("  a partial round is cleared, never inherited: ok")
    test_admin_seat_attaches_on_a_stranded_report(agents)
    print("  admin seat attaches on a stranded report: ok")
    test_the_ledger_records_who_did_it(agents)
    print("  ledger names the actor and lands in the bugs stream: ok")
    print("test_manual_attach_bug: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
