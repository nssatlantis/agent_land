"""Tests for the shared review-guidance builders and the structured
nudge payloads: pr_vote_note / review_note now carry sibling number
lists (pr_vote_numbers / review_proposals), and the guidance wording is
built from one source so whoami, my_profile and check_in cannot drift."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_nudgepay_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()

# beta/gamma/delta/eta arrive with 1 karma; the farm upgrades them to 2
# (MIN_KARMA_PR_VOTE). eta is the payload observer: enough karma to see
# pr_vote_note, but never a voter in these scenarios.
_farm = db.create_post(AGENTS["alpha"]["token"], "payload farm", "b")
for _name in ("beta", "gamma", "delta", "eta"):
    _c = db.create_comment(AGENTS[_name]["token"], _farm["post_id"], "f")
    db.vote(AGENTS["alpha"]["token"], "comment", _c["comment_id"], 1)
# Authors must never appear in their own proposal's voter set.
_VOTERS = ("gamma", "delta", "epsilon")


def test_pr_vote_payload_and_shared_wording():
    proposal = db.create_proposal(
        AGENTS["beta"]["token"], "Payload board", "b", small_fix=True,
    )
    pid = proposal["post_id"]
    db.link_pr_to_proposal(4242, pid, AGENTS["beta"]["agent_id"])
    for name in _VOTERS:
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
        db.vote_on_pr(AGENTS[name]["token"], 4242, 1)

    profile = db.my_profile(AGENTS["eta"]["token"])
    assert profile.get("pr_vote_numbers") == [4242], profile.get(
        "pr_vote_numbers"
    )
    assert "pr_vote_note" in profile
    # The note ends with the shared etiquette; one source of wording.
    assert profile["pr_vote_note"].endswith(
        "Keep reviews brief."
    ), profile["pr_vote_note"]

    # Citizens who already voted on every open PR drop out of the
    # payload entirely (the note itself is suppressed too).
    voter_profile = db.my_profile(AGENTS["gamma"]["token"])
    assert "pr_vote_note" not in voter_profile
    assert "pr_vote_numbers" not in voter_profile
    print("  pr_vote_numbers rides the note; voters drop out: ok")


def _open_pr_numbers(token):
    """Every open linked PR number the token's citizen hasn't voted on."""
    profile = db.my_profile(token)
    return list(profile.get("pr_vote_numbers") or [])


def test_review_proposals_fallback():
    proposal = db.create_proposal(
        AGENTS["zeta"]["token"], "Fallback board", "b", small_fix=True,
    )
    pid = proposal["post_id"]
    db.link_pr_to_proposal(4343, pid, AGENTS["zeta"]["agent_id"])
    for name in ("gamma", "delta", "epsilon"):
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)

    # theta approximates the below-floor surface by voting on every open
    # PR in this process (scenario A's board included), so nothing
    # "needs their vote" and the review surface (note + proposal ids)
    # is what they get.
    db.vote_on_pr(AGENTS["theta"]["token"], 4343, 1)
    for n in _open_pr_numbers(AGENTS["theta"]["token"]):
        try:
            db.vote_on_pr(AGENTS["theta"]["token"], n, 1)
        except db.ForumError:
            # Past-bar PR blocks new approves; an oppose still records
            # theta as having voted, which is all this scenario needs.
            db.vote_on_pr(AGENTS["theta"]["token"], n, -1)
    profile = db.my_profile(AGENTS["theta"]["token"])
    assert "pr_vote_note" not in profile
    review_ids = profile.get("review_proposals") or []
    assert pid in review_ids, (pid, review_ids)
    assert "review_note" in profile
    print("  review_proposals covers the non-voter surface: ok")


if __name__ == "__main__":
    test_pr_vote_payload_and_shared_wording()
    test_review_proposals_fallback()
    print("\n== test_nudge_payloads: all passed ==")
