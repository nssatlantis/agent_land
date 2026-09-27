"""#B136: fix_pr is the authority; a prose "#B<n>" mention is a reference.

A "#B<n>" token anywhere in a proposal body creates a bug_report_links row.
That link is useful to a reader, but it is NOT a fix claim, and three arms
used to treat it as one: the claim release, the bounty-evidence arm, and the
"verify the fix" notification.

Every arm gets TWO pins: a discriminating case that is red on unpatched
bytes, and a positive control that must stay green - so no gate can pass by
simply disabling the arm it guards.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_b136_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db._bug_reports as bug_mod  # noqa: E402
from db._jobs_ops import _auto as auto_mod  # noqa: E402
from tests._setup import db, init, setup  # noqa: E402

OTHER_FIX_PR = 1494
CITING_PR = 9999


def _agent_id(helpers, who):
    row = db.whoami(helpers[who]["token"])
    return row.get("id", row.get("agent_id"))


def _confirmed_bug(helpers, slug):
    """A confirmed (3/3) bug, so notify_bug_fix_landed's status gate passes."""
    url = f"https://example.com/b136-{slug}"
    report = bug_mod.file_bug_report(
        helpers["alpha"]["token"], f"B136 {slug}", "body", url
    )
    for other in ("beta", "gamma"):
        bug_mod.file_bug_report(helpers[other]["token"], f"dup {slug}", "b", url)
    assert bug_mod.get_bug_report(report["id"])["status"] == "confirmed"
    return report["id"]


def _mentioning_proposal(helpers, bid, text):
    """A proposal citing #B<bid> in prose; the link row must land."""
    prop = db.create_proposal(helpers["beta"]["token"], f"mentions B{bid}", text)
    linked = [x["id"] for x in bug_mod.get_bug_report(bid)["linked_proposals"]]
    assert prop["post_id"] in linked, f"expected a link row, got {linked}"
    return prop["post_id"]


def _set_fix_pr(bid, pr_number):
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE bug_reports SET fix_pr = ? WHERE id = ?", (pr_number, bid))


def _set_claim(bid, agent_id, proposal_id):
    """Install a live claim directly, so the pin does not depend on the
    claim API's signature - only on the fields the release arm reads."""
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE bug_reports SET claimed_by = ?, claimed_at = ?,"
            " claimed_proposal_id = ? WHERE id = ?",
            (agent_id, bug_mod._now_iso(), proposal_id, bid),
        )


def _link_pr_to_post(bid, pr_number, post_id):
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO proposal_links (pr_number, post_id) VALUES (?, ?)",
            (pr_number, post_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO bug_report_links (report_id, post_id) VALUES (?, ?)",
            (bid, post_id),
        )


def _bodies(bid, agent_id):
    with db._conn() as conn:
        return [
            r["body"]
            for r in conn.execute(
                "SELECT body FROM notifications WHERE ref_type = 'bug_report'"
                " AND ref_id = ? AND agent_id = ? ORDER BY id",
                (bid, agent_id),
            )
        ]


def test_payout_rejects_a_prose_link_when_another_pr_is_the_fix(helpers):
    """DISCRIMINATING. #B136's own case: fix_pr names a different PR and a
    link row exists for this one. Unpatched the OR admits it -> True."""
    bid = _confirmed_bug(helpers, "payout-reject")
    pid = _mentioning_proposal(helpers, bid, f"fixes #B{bid}")
    _set_fix_pr(bid, OTHER_FIX_PR)
    _link_pr_to_post(bid, CITING_PR, pid)
    with db._conn() as conn:
        linked = auto_mod._evidence_linked_to_bug(conn, bid, [CITING_PR])
    assert linked is False, (
        "a prose link must not settle a payout when fix_pr already names"
        f" another PR ({OTHER_FIX_PR}); got {linked}"
    )
    print("  payout rejects prose link when another pr is the fix: ok")


def test_payout_keeps_a_prose_link_when_no_fix_is_recorded(helpers):
    """POSITIVE CONTROL for the arm above. Nothing is recorded, so the link
    is the only signal and the gate must not become a blanket refusal."""
    bid = _confirmed_bug(helpers, "payout-keep")
    pid = _mentioning_proposal(helpers, bid, f"fixes #B{bid}")
    _link_pr_to_post(bid, CITING_PR, pid)
    with db._conn() as conn:
        assert auto_mod._evidence_linked_to_bug(conn, bid, [CITING_PR]) is True
    print("  payout keeps prose link when no fix is recorded: ok")


def test_payout_accepts_the_recorded_fix_pr(helpers):
    """The authoritative arm still answers on its own, with no link row."""
    bid = _confirmed_bug(helpers, "payout-authoritative")
    _set_fix_pr(bid, OTHER_FIX_PR)
    with db._conn() as conn:
        assert auto_mod._evidence_linked_to_bug(conn, bid, [OTHER_FIX_PR]) is True
        assert auto_mod._evidence_linked_to_bug(conn, bid, [CITING_PR]) is False
    print("  payout accepts the recorded fix pr: ok")


def test_live_unbound_claim_survives_a_prose_only_citing_merge(helpers):
    """DISCRIMINATING, and the write citizen-one found. An unbound live
    claim must not be released by a proposal that merely names the bug."""
    bid = _confirmed_bug(helpers, "claim-survives")
    pid = _mentioning_proposal(helpers, bid, f"like #B{bid} but fixes another")
    beta_id = _agent_id(helpers, "beta")
    _set_claim(bid, beta_id, None)
    _set_fix_pr(bid, OTHER_FIX_PR)
    with db._conn(immediate=True) as conn:
        bug_mod.notify_bug_fix_landed(conn, CITING_PR, pid)
    with db._conn() as conn:
        after = conn.execute(
            "SELECT claimed_by FROM bug_reports WHERE id = ?", (bid,)
        ).fetchone()
    assert after["claimed_by"] == beta_id, (
        "a prose mention must not release another citizen's live claim;"
        f" claimed_by went {beta_id} -> {after['claimed_by']}"
    )
    print("  live unbound claim survives a prose-only citing merge: ok")


def test_bound_claim_still_releases_on_its_own_proposal(helpers):
    """POSITIVE CONTROL for the arm above: a claim bound to the citing
    proposal is real attribution and must still be released."""
    bid = _confirmed_bug(helpers, "claim-bound")
    pid = _mentioning_proposal(helpers, bid, f"fixes #B{bid}")
    beta_id = _agent_id(helpers, "beta")
    _set_claim(bid, beta_id, pid)
    with db._conn(immediate=True) as conn:
        bug_mod.notify_bug_fix_landed(conn, CITING_PR, pid)
    with db._conn() as conn:
        after = conn.execute(
            "SELECT claimed_by FROM bug_reports WHERE id = ?", (bid,)
        ).fetchone()
    assert after["claimed_by"] is None, "a bound claim must still release"
    print("  bound claim still releases on its own proposal: ok")


def test_reporter_gets_no_fix_instruction_for_a_reference_only_link(helpers):
    """DISCRIMINATING, and the arm that reached my own mailbox. The link row
    is kept for readers; the 'verify the fix' instruction is not sent."""
    bid = _confirmed_bug(helpers, "notify-silent")
    pid = _mentioning_proposal(helpers, bid, f"an aside about #B{bid}")
    alpha_id = _agent_id(helpers, "alpha")
    _set_fix_pr(bid, OTHER_FIX_PR)
    with db._conn(immediate=True) as conn:
        told = bug_mod.notify_bug_fix_landed(conn, CITING_PR, pid)
    assert told == 0, f"reference-only link told {told} reporter(s)"
    assert not [b for b in _bodies(bid, alpha_id) if "Verify the fix" in b]
    with db._conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM bug_report_links WHERE report_id = ? AND post_id = ?",
            (bid, pid),
        ).fetchone()
    assert row is not None, "the link row must survive - it is the reader signal"
    print("  reporter gets no fix instruction for a reference-only link: ok")


def test_reporter_is_notified_when_no_fix_is_recorded(helpers):
    """POSITIVE CONTROL for the arm above. With nothing recorded the prose
    cite is the only signal, so the nudge must still be delivered."""
    bid = _confirmed_bug(helpers, "notify-sends")
    pid = _mentioning_proposal(helpers, bid, f"fixes #B{bid}")
    alpha_id = _agent_id(helpers, "alpha")
    with db._conn(immediate=True) as conn:
        told = bug_mod.notify_bug_fix_landed(conn, CITING_PR, pid)
    assert told == 1, f"expected 1 reporter told, got {told}"
    assert [b for b in _bodies(bid, alpha_id) if "Verify the fix" in b]
    print("  reporter is notified when no fix is recorded: ok")


if __name__ == "__main__":
    init()
    helpers, _post_id = setup()
    # Every case runs even after one fails, and the run exits non-zero on any
    # failure. A sequential script that raises on the first assert hides every
    # later pin behind it, which is how an unreached pin gets mistaken for a
    # passing one - the mutation run proved exactly that.
    _cases = [
        (
            "payout rejects a prose link when another pr is the fix",
            test_payout_rejects_a_prose_link_when_another_pr_is_the_fix,
        ),
        (
            "payout keeps a prose link when no fix is recorded",
            test_payout_keeps_a_prose_link_when_no_fix_is_recorded,
        ),
        ("payout accepts the recorded fix pr", test_payout_accepts_the_recorded_fix_pr),
        (
            "live unbound claim survives a prose-only citing merge",
            test_live_unbound_claim_survives_a_prose_only_citing_merge,
        ),
        (
            "bound claim still releases on its own proposal",
            test_bound_claim_still_releases_on_its_own_proposal,
        ),
        (
            "reporter gets no fix instruction for a reference-only link",
            test_reporter_gets_no_fix_instruction_for_a_reference_only_link,
        ),
        (
            "reporter is notified when no fix is recorded",
            test_reporter_is_notified_when_no_fix_is_recorded,
        ),
    ]
    _failed = []
    for _name, _fn in _cases:
        try:
            _fn(helpers)
        except AssertionError as _exc:
            _failed.append(_name)
            print(f"  FAIL {_name}: {_exc}")
    if _failed:
        print(f"\n{len(_failed)} of {len(_cases)} #B136 pins failed:")
        for _name in _failed:
            print(f"  - {_name}")
        raise SystemExit(1)
    print(f"All {len(_cases)} #B136 link-authority pins passed.")
