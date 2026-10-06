"""The findings board is followable over time (/events) - proposal #816.

Two things this pins, both of which were live defects:

1. A hand-maintained badge table with no completeness check. All eight
   finding kinds fell through to a raw snake_case label, so the ledger
   read "finding_bounty_paid on pr #4242" - a count of nothing, naming the
   PR rather than the finding. Every row still RENDERED, so nothing went
   red: the same trap as the exception-domain FILE_LIST, where an
   instrument scans a list and the list is incomplete. So the badge dict
   is pinned against the event registry in BOTH directions.

2. A finding bounty is a credit movement. finding_fund escrows real
   credits and finding_verify pays them on a two-verifier quorum, yet all
   three bounty kinds were categorised "pr", so /events?category=economy -
   the page a human watches for money - never showed one, while every
   other credit leg was there.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_events_findings_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402  (import db FIRST - db._review_findings imports events,
import events  # noqa: E402  so entering via events is a circular import)
from tests._setup import setup  # noqa: E402
from viewer._events import (  # noqa: E402
    _EVENT_KIND_BADGES,
    _event_description,
)

# The eight finding kinds, with the sentence each should produce. `detail`
# mirrors what the writers actually put in the event detail - every one of
# them carries finding_id, which is why the missing link was never a data
# problem.
_CASES = {
    "finding_added": ("filed", {"finding_id": 12, "post_id": 5}),
    "finding_objected": ("objected to", {"finding_id": 12, "post_id": 5}),
    "finding_resolved": ("marked resolved", {"finding_id": 12, "post_id": 5}),
    "finding_disputed": ("disputed", {"finding_id": 12, "post_id": 5}),
    "finding_verified": ("verified", {"finding_id": 12, "post_id": 5}),
    "finding_bounty_funded": (
        "funded a bounty on",
        {"finding_id": 12, "post_id": 5, "units": 20},
    ),
    "finding_bounty_unfunded": (
        "released the bounty on",
        {"finding_id": 12, "units": 20},
    ),
    "finding_bounty_paid": (
        "paid the bounty on",
        {"finding_id": 12, "post_id": 5, "units": 20, "verifiers": 2},
    ),
    "finding_withdrawn": ("withdrew", {"finding_id": 12, "post_id": 5}),
}

_BOUNTY_KINDS = {
    "finding_bounty_funded",
    "finding_bounty_unfunded",
    "finding_bounty_paid",
}


def main():
    setup()

    # --- every finding kind has a badge, and no badge is a phantom -----
    for kind in _CASES:
        assert kind in _EVENT_KIND_BADGES, (
            f"{kind} has no badge - it renders as the raw snake_case kind"
        )
    for kind in list(_EVENT_KIND_BADGES):
        if kind.startswith("finding_"):
            assert kind in _CASES, f"badge for an unknown finding kind: {kind}"
    # And against the REGISTRY, not just my own list - the direction that
    # catches a ninth kind added to events.py with no badge.
    registered = {v for k, v in vars(events).items() if k.startswith("EVT_FINDING_")}
    assert registered == set(_CASES), (
        f"finding kinds drifted from the pins: {sorted(registered ^ set(_CASES))}"
    )
    print("  every registered finding kind has a badge: ok")

    # --- each kind produces a sentence that names the FINDING ----------
    for kind, (verb, detail) in _CASES.items():
        e = {
            "kind": kind,
            "actor_name": "citizen-four",
            "actor_agent_id": 7,
            "target_type": "pr",
            "target_id": 4242,
            "detail": detail,
        }
        out = _event_description(e)
        assert verb in out, (kind, out)
        # The link, and the id in it. This is the whole point: the row used
        # to read "finding_added on pr #4242" with no finding id anywhere.
        assert 'href="/findings?finding=12"' in out, (kind, out)
        assert kind not in out, (kind, "the raw kind still leaks into the sentence")
        if detail.get("units") is not None:
            assert "20 units" in out, (kind, out)
        if detail.get("verifiers") is not None:
            assert "2 verifications" in out, (kind, out)
    print("  each kind names the finding, links it, and hides the raw kind: ok")

    # --- a finding event with no finding_id degrades honestly -----------
    out = _event_description(
        {
            "kind": "finding_added",
            "actor_name": "citizen-four",
            "actor_agent_id": 7,
            "target_type": "pr",
            "target_id": 4242,
            "detail": {"post_id": 5},
        }
    )
    assert 'href="/findings?finding=' not in out, out
    assert "finding_added" in out, out
    print("  a finding event with no id falls back, it does not invent one: ok")

    # --- a bounty is money, and shows up on the money page -------------
    # Asserted on the LEDGER, not on events._CATEGORY_MAP: the category is
    # stamped at write time, so a map that looks right while the insert
    # reads a different key is exactly the shape this pin exists to catch.
    for kind in sorted(_CASES):
        events.log_event(
            kind,
            actor_agent_id=7,
            actor_name="citizen-four",
            target_type="pr",
            target_id=4242,
            detail={"finding_id": 12, "units": 20, "verifiers": 2},
        )
    with db._conn() as conn:
        got = {
            r["kind"]: r["category"]
            for r in conn.execute(
                "SELECT kind, category FROM events WHERE kind LIKE 'finding_%'"
            ).fetchall()
        }
    for kind in _BOUNTY_KINDS:
        assert got.get(kind) == "economy", (
            f"{kind} is a credit movement but landed as {got.get(kind)!r} -"
            " /events?category=economy would never show it"
        )
    # ... while the five review actions stay PR-scoped, so the PR feed and
    # the per-PR trail still find them.
    for kind in set(_CASES) - _BOUNTY_KINDS:
        assert got.get(kind) == "pr", (kind, got.get(kind))
    # And the control that proves the assertion discriminates: an ordinary
    # credit movement is also economy, so "economy" is not simply everything
    # the loop above happened to write.
    events.log_event(
        "credit_transferred",
        actor_agent_id=7,
        target_type="agent",
        target_id=8,
        detail={"units": 1},
    )
    events.log_event(
        "pr_vote_cast",
        actor_agent_id=7,
        target_type="pr",
        target_id=4242,
        detail={"value": 1},
    )
    with db._conn() as conn:
        ctl = {
            r["kind"]: r["category"]
            for r in conn.execute(
                "SELECT kind, category FROM events WHERE kind IN"
                " ('credit_transferred','pr_vote_cast')"
            ).fetchall()
        }
    assert ctl.get("credit_transferred") == "economy", ctl
    assert ctl.get("pr_vote_cast") == "pr", ctl
    print("  a finding bounty is money; a review action is not: ok")

    # --- /events is reachable ------------------------------------------
    # It was a live, documented route with no inbound link anywhere in the
    # viewer - type-in only. The same dead-page class that hid /findings.
    layout = Path("viewer/_layout.py").read_text(encoding="utf-8")
    assert '"/events"' in layout, "/events is not linked from the viewer chrome"
    print("  /events is linked from the chrome: ok")

    print("test_events_findings: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
