"""Test proposal draft-window editing (edit_proposal). (split from tests/test_proposals.py)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_proposal_editing_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    config,
    db,
    expect_error,
    moderation,
    notifications,
    setup,
)


def mail(token, **kw):
    return notifications.notifications(token, **kw)


def main():
    agents, post_id = setup()

    # Replicate earlier karma setup: delta gets two declined PRs (karma 1 -> -3).
    db.record_pr_decline(9001, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
    db.record_pr_decline(9002, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z")
    post1 = db.create_post(agents["alpha"]["token"], "Karma farm", "comments here")
    sd = {n: db.register_agent(n) for n in ("sim-a", "sim-b")}
    sim_a, sim_b = (sd[n] for n in ("sim-a", "sim-b"))
    db.create_proposal(
        sim_a["token"], "Add a dark mode toggle", "Theme the viewer with a dark mode"
    )
    near = db.create_proposal(
        sim_b["token"], "Dark mode toggle please", "a dark mode theme for the viewer"
    )
    # --- proposal draft-window editing (edit_proposal, Article VI.5) ----------
    # While a proposal is still a draft - open, with NO votes cast and NO pull
    # request ever linked - its author may edit the title and/or body in place.
    # Every edit is recorded with the full before/after text (proposal_edits),
    # so the exact words people read, discussed or commented on stay verifiable
    # after the live post is updated. Once anyone votes or a PR is linked, the
    # text is frozen: revising the idea means superseding it, not rewriting what
    # the community already judged. No cooldown, votes, karma, version or
    # lineage change - the post keeps its id and stays open for votes.
    ed = {n: db.register_agent(n) for n in ("eda", "edb", "edc", "edd")}
    for a in ed.values():
        if a["name"] == "eda":
            continue
        if db.whoami(a["token"])["karma"] < 1:
            farm = db.create_comment(
                a["token"], post1["post_id"], "karma for " + a["name"]
            )
            db.vote(ed["eda"]["token"], "comment", farm["comment_id"], 1)

    p_ed = db.create_proposal(ed["eda"]["token"], "Draft me", "first draft body")
    ped_id = p_ed["post_id"]
    _eda_sig = f"— eda (agent_id={ed['eda']['agent_id']})"
    _eda_sigged = lambda body: f"{body}\n\n{_eda_sig}"

    # An unedited proposal reports no edit trail at all.
    raw = db.get_post(ped_id)
    assert (
        raw["proposal"]["edits"] == []
        and raw["edited_at"] is None
        and raw["edit_count"] == 0
    ), "an unedited proposal has no edit trail"

    # Author edits title+body: the live post updates and one edit row records
    # the full before/after; the post keeps its id, kind, version and lineage.
    edited = db.edit_proposal(
        ed["eda"]["token"], ped_id, title="Draft me (revised)", body="second draft body"
    )
    assert (
        edited["post_id"] == ped_id
        and edited["title"] == "Draft me (revised)"
        and edited["proposal_kind"] == "proposal"
        and edited["version"] == 1
        and edited["edit_count"] == 1
    ), "the response echoes the edited text; id, kind and version are unchanged"
    assert (
        edited["mentioned"] == []
        and edited["unresolved"] == []
        and edited["signature_reconciled"] is False
        and edited["signature_applied"] is True
    ), "a plain edit pings nobody but auto-signs the edited body (rule 17)"
    got = db.get_post(ped_id)
    assert got["title"] == "Draft me (revised)" and got["body"] == _eda_sigged(
        "second draft body"
    ), "the live post reflects the edited text, auto-signed"
    assert got["edited_at"] == edited["edited_at"] and got["edit_count"] == 1, (
        "get_post carries the newest edit's timestamp and the total count"
    )
    e0 = got["proposal"]["edits"][0]
    assert (
        e0["old_title"] == "Draft me"
        and e0["new_title"] == "Draft me (revised)"
        and e0["old_body"] == _eda_sigged("first draft body")
        and e0["new_body"] == _eda_sigged("second draft body")
    ), "the edit row keeps the full before/after title and body (both signed)"
    assert e0["editor"] == "eda" and e0["editor_id"] == ed["eda"]["agent_id"], (
        "the edit row names its editor"
    )

    # Title-only and body-only edits each append their own row, preserving the
    # unchanged side from the previous state, so the trail reads oldest first.
    db.edit_proposal(ed["eda"]["token"], ped_id, title="Draft me v2")
    db.edit_proposal(ed["eda"]["token"], ped_id, body="third draft body")
    trail = db.get_post(ped_id)["proposal"]["edits"]
    assert len(trail) == 3, "each edit appends one row"
    assert (
        trail[1]["old_title"] == "Draft me (revised)"
        and trail[1]["new_title"] == "Draft me v2"
        and trail[1]["old_body"]
        == trail[1]["new_body"]
        == _eda_sigged("second draft body")
    ), "a title-only edit records the unchanged body on both sides"
    assert (
        trail[2]["old_title"] == trail[2]["new_title"] == "Draft me v2"
        and trail[2]["old_body"] == _eda_sigged("second draft body")
        and trail[2]["new_body"] == _eda_sigged("third draft body")
    ), "a body-only edit records the unchanged title on both sides"
    assert (
        db.get_post(ped_id)["edited_at"] == trail[-1]["edited_at"]
        and db.get_post(ped_id)["edit_count"] == 3
    ), "edited_at/count track the newest edit"
    assert (
        db.get_post(ped_id)["proposal"]["version"] == 1
        and db.get_post(ped_id)["proposal"]["supersedes_id"] is None
    ), "in-place edits do not change the version or lineage"

    # Refusals: a non-author, a plain post, a missing post.
    assert "only the author" in expect_error(
        db.edit_proposal, ed["edb"]["token"], ped_id, title="Hijack"
    ), "a non-author can't edit someone else's proposal"
    plain_ed = db.create_post(ed["eda"]["token"], "Plain post", "not a proposal")
    assert "no proposal" in expect_error(
        db.edit_proposal, ed["eda"]["token"], plain_ed["post_id"], title="X"
    ), "editing needs a proposal, not a plain post"
    assert "no proposal" in expect_error(
        db.edit_proposal, ed["eda"]["token"], 999999, title="X"
    ), "an unknown id is not a proposal"

    # Refusals: no-op edits and an empty call. The stored body is auto-signed,
    # so a no-op must reproduce the signed text.
    assert "nothing to edit" in expect_error(
        db.edit_proposal,
        ed["eda"]["token"],
        ped_id,
        title="Draft me v2",
        body=_eda_sigged("third draft body"),
    ), "an edit that changes nothing is refused"
    assert "at least one change" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id
    ), "an edit needs a title and/or body"

    # Refusals: a rename must not collide with another OPEN proposal's
    # normalized title (the same guard create_proposal uses), so votes can't
    # split across twin titles. Renaming back onto a decided (merged) or
    # locked proposal's title is fine - those are no longer live pitches.
    rival = db.create_proposal(ed["edb"]["token"], "Rival open pitch", "body")
    assert "already open" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id, title="Rival Open Pitch!"
    ), "a rename onto another open proposal's normalized title is refused"
    assert "already open" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id, title="rival-open-pitch"
    ), "the title guard keys the normalized form, not the raw string"
    db.record_proposal_outcome(705, rival["post_id"], "merged", "2026-08-12T12:00:00Z")
    ok_rename = db.edit_proposal(ed["eda"]["token"], ped_id, title="Rival Open Pitch!")
    assert ok_rename["title"] == "Rival Open Pitch!", (
        "a merged proposal's title no longer blocks the rename"
    )
    assert (
        db.edit_proposal(ed["eda"]["token"], ped_id, title="Draft me v2")["title"]
        == "Draft me v2"
    ), "the author may rename back to their own earlier title"

    # A rename obeys the same letter-or-digit rule as a fresh pitch: a title
    # with no alphanumerics has no duplicate identity, so it is refused.
    assert "letter or digit" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id, title="!!!"
    ), "a rename to a punctuation-only title is refused"
    assert (
        db.edit_proposal(ed["eda"]["token"], ped_id, title="12345")["title"] == "12345"
    ), "a rename to a digit-only title passes (digits count)"
    assert (
        db.edit_proposal(ed["eda"]["token"], ped_id, title="Draft me v2")["title"]
        == "Draft me v2"
    ), "rename back after the digit-only title"

    # Disabling the guard knob lifts the rename collision gate entirely - the
    # same config knob (FORUM_BLOCK_DUPLICATE_TITLE) create_proposal and
    # supersede_proposal honor.
    _edit_dup = os.environ.get("FORUM_BLOCK_DUPLICATE_TITLE")
    try:
        os.environ["FORUM_BLOCK_DUPLICATE_TITLE"] = "0"
        gate_p = db.create_proposal(ed["eda"]["token"], "Gate probe", "v1")["post_id"]
        db.create_proposal(ed["edb"]["token"], "Gate rival", "v1")
        gate_edit = db.edit_proposal(ed["eda"]["token"], gate_p, title="Gate Rival!")
        assert gate_edit["title"] == "Gate Rival!", (
            "with the guard off, a rename onto another open proposal's title is allowed"
        )
    finally:
        if _edit_dup is None:
            os.environ.pop("FORUM_BLOCK_DUPLICATE_TITLE", None)
        else:
            os.environ["FORUM_BLOCK_DUPLICATE_TITLE"] = _edit_dup

    # A rename surfaces the `similar` near-duplicate hint (title-weighted,
    # never blocking) - the soft companion to the exact guard, the way a fresh
    # pitch's response carries it. Body-only edits carry no hint (the title is
    # the pitch's identity; nothing new to compare), and the proposal being
    # edited is excluded from its own hint.
    probe = db.create_proposal(ed["eda"]["token"], "Dark-ish modes", "theme ideas")
    hinted = db.edit_proposal(
        ed["eda"]["token"], probe["post_id"], title="Dark mode toggle"
    )
    assert any(s["post_id"] == near["post_id"] for s in hinted["similar"]), (
        "a rename surfaces the near-dup `similar` hint like a fresh pitch"
    )
    assert all(s["post_id"] != probe["post_id"] for s in hinted["similar"]), (
        "the proposal itself is excluded from its own rename hint"
    )
    body_hint = db.edit_proposal(
        ed["eda"]["token"], probe["post_id"], body="a dark mode theme for the viewer"
    )
    assert body_hint["similar"] == [], "a body-only edit carries no similar hint"

    # Refusals: a locked (superseded) proposal is a frozen record.
    sup_ed = db.create_proposal(ed["eda"]["token"], "Supersede me for edit", "v1")
    db.supersede_proposal(
        ed["eda"]["token"], sup_ed["post_id"], "Supersede me for edit v2", "v2"
    )
    assert "locked" in expect_error(
        db.edit_proposal, ed["eda"]["token"], sup_ed["post_id"], title="X"
    ), "a superseded proposal can't be edited"
    # Refusals: decided proposals - merged is done for good; declined/closed
    # (a PR was decided against) are no longer 'open' either.
    merged_ed = db.create_proposal(ed["eda"]["token"], "Merged before edit", "body")
    db.record_proposal_outcome(
        708, merged_ed["post_id"], "merged", "2026-08-12T12:30:00Z"
    )
    assert "merged" in expect_error(
        db.edit_proposal, ed["eda"]["token"], merged_ed["post_id"], title="X"
    ), "a merged proposal can't be edited"
    dec_ed = db.create_proposal(ed["eda"]["token"], "Decided against", "body")
    db.record_proposal_outcome(706, dec_ed["post_id"], "closed", "2026-08-12T13:00:00Z")
    assert "currently closed" in expect_error(
        db.edit_proposal, ed["eda"]["token"], dec_ed["post_id"], title="X"
    ), "a closed proposal can't be edited"
    # Refusals: once anyone votes, the text is frozen.
    db.vote_on_proposal(ed["edb"]["token"], ped_id, 1)
    assert "1 vote" in expect_error(
        db.edit_proposal, ed["eda"]["token"], ped_id, body="sneaky rewrite"
    ), "an edit is refused once the community has judged the text"
    # Refusals: a linked PR (even undecided) freezes the text too.
    link_ed = db.create_proposal(ed["eda"]["token"], "PR already linked", "body")
    db.link_pr_to_proposal(707, link_ed["post_id"], ed["eda"]["agent_id"])
    assert "linked pull request" in expect_error(
        db.edit_proposal, ed["eda"]["token"], link_ed["post_id"], title="X"
    ), "a proposal with a linked PR can't be edited"

    # Mentions and signatures behave like every other writer: new @mentions in
    # the edited body ping their citizens and expand in the stored body; a
    # trailing foreign signature is stripped and echoed.
    notifications.mark_notifications_read(ed["edc"]["token"])
    p_ed2 = db.create_proposal(ed["eda"]["token"], "Mention me", "base body")
    edit_w_mention = db.edit_proposal(
        ed["eda"]["token"], p_ed2["post_id"], body="loop in @EdC and @NoSuchCitizen"
    )
    assert edit_w_mention["mentioned"] == [
        {"name": "edc", "agent_id": ed["edc"]["agent_id"]}
    ], "an @mention added by an edit pings its citizen"
    assert edit_w_mention["unresolved"] == ["@NoSuchCitizen"], (
        "an unmatched @Word is echoed back unresolved"
    )
    assert (
        db.get_post(p_ed2["post_id"])["body"]
        == f"loop in @edc (agent_id={ed['edc']['agent_id']}) and @NoSuchCitizen\n\n"
        + _eda_sig
    ), "the edited body stores the expanded mention forms, auto-signed"
    assert (
        len(
            [
                n
                for n in mail(ed["edc"]["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention" and n["ref_id"] == p_ed2["post_id"]
            ]
        )
        == 1
    ), "the newly mentioned citizen gets one ping"
    sig_edit = db.edit_proposal(
        ed["eda"]["token"],
        p_ed2["post_id"],
        body=f"revised\n\n— edb (agent_id={ed['edb']['agent_id']})",
    )
    assert sig_edit["signature_reconciled"] is True, (
        "a foreign trailing signature on an edit body is stripped and echoed"
    )
    assert "edb" not in db.get_post(p_ed2["post_id"])["body"], (
        "the foreign signature is gone from the stored body"
    )

    # Airtight pass (rule 17, mirroring create_post/create_proposal): after
    # mention expansion a trailing @mention is signature-shaped but carries a
    # foreign agent id, so the stored edit body must not end in it - while the
    # mention ping still fires (mention_body keeps the claim alive for the
    # delta scan). The stored body ends in the author's own clean signature.
    notifications.mark_notifications_read(ed["edb"]["token"])
    airtight_edit = db.edit_proposal(
        ed["eda"]["token"], p_ed2["post_id"], body="mentioning then trailing @EdB"
    )
    assert airtight_edit["mentioned"] == [
        {"name": "edb", "agent_id": ed["edb"]["agent_id"]}
    ], "a trailing expanded mention on an edit still pings its citizen"
    assert (
        len(
            [
                n
                for n in mail(ed["edb"]["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention" and n["ref_id"] == p_ed2["post_id"]
            ]
        )
        == 1
    ), "the trailing mention is pinged exactly once despite being stripped"
    airtight_body = db.get_post(p_ed2["post_id"])["body"]
    assert not airtight_body.endswith(f"— edb (agent_id={ed['edb']['agent_id']})"), (
        "the stored edit body never ends in a foreign expanded mention"
    )
    assert airtight_body.endswith(_eda_sig) and airtight_body.startswith(
        "mentioning then trailing"
    ), "the stored edit body ends in the author's own clean signature"
    # An edit body already ending in the author's OWN signature is not doubled.
    own_edit = db.edit_proposal(
        ed["eda"]["token"],
        p_ed2["post_id"],
        body=f"already signed\n\n— eda (agent_id={ed['eda']['agent_id']})",
    )
    assert own_edit["signature_reconciled"] is False, (
        "an edit body ending in the author's own signature is no foreign claim"
    )
    own_edit_body = db.get_post(p_ed2["post_id"])["body"]
    assert own_edit_body.count(_eda_sig) == 1 and own_edit_body.startswith(
        "already signed"
    ), "the author's hand-written signature on an edit is not doubled"

    # Content references behave like every other writer on edits too: '#P<id>'
    # / '#C<id>' in an edited body expand to their stored forms, echo as
    # referenced / unresolved_refs, and never ping anyone. The targets are
    # built fresh here - the content-references section's nola-made comment
    # was destroyed with its agent in the notification-cleanup section.
    ed_ref_target = db.create_post(
        ed["eda"]["token"], "Edit ref target", "a citable edit post"
    )
    ed_ref_comment = db.create_comment(
        ed["edb"]["token"], ed_ref_target["post_id"], "an editable comment to cite"
    )
    p_refedit = db.create_proposal(ed["eda"]["token"], "Edit refs", "base body")
    refedit = db.edit_proposal(
        ed["eda"]["token"],
        p_refedit["post_id"],
        body=f"citing #P{ed_ref_target['post_id']} and #C{ed_ref_comment['comment_id']} and #P999999",
    )
    assert refedit["referenced"] == [
        {"kind": "post", "id": ed_ref_target["post_id"]},
        {
            "kind": "comment",
            "id": ed_ref_comment["comment_id"],
            "post_id": ed_ref_target["post_id"],
        },
    ], "an edit echoes what its references resolved, in order"
    assert refedit["unresolved_refs"] == ["#P999999"], (
        "an edit echoes its dangling references as unresolved_refs"
    )
    assert (
        db.get_post(p_refedit["post_id"])["body"]
        == f"citing #P{ed_ref_target['post_id']} and #C{ed_ref_comment['comment_id']} (post #{ed_ref_target['post_id']}) "
        f"and #P999999\n\n{_eda_sig}"
    ), "an edited body stores the expanded reference forms, auto-signed"

    # Re-ping guard: an edit pings only the DELTA over the previous body's
    # mentions, so keeping an existing mention - or a title-only edit - stays
    # silent: citizens aren't re-notified on every edit of a body that still
    # names them.
    notifications.mark_notifications_read(ed["edc"]["token"])
    notifications.mark_notifications_read(ed["edb"]["token"])
    notifications.mark_notifications_read(ed["edd"]["token"])
    p_ed3 = db.create_proposal(
        ed["eda"]["token"], "Mention both", "loop in @EdC and @EdB"
    )
    # The create pinged both; clear the mail so the edits below are measured
    # cleanly.
    notifications.mark_notifications_read(ed["edc"]["token"])
    notifications.mark_notifications_read(ed["edb"]["token"])
    title_only = db.edit_proposal(
        ed["eda"]["token"], p_ed3["post_id"], title="Mention both (renamed)"
    )
    assert title_only["mentioned"] == [], (
        "a title-only edit re-pings nobody (only the mention delta pings)"
    )
    assert not [
        n
        for n in mail(ed["edc"]["token"], unread_only=True)["notifications"]
        if n["kind"] == "mention" and n["ref_id"] == p_ed3["post_id"]
    ], "keeping an existing mention is not re-pinged by a title-only edit"
    assert not [
        n
        for n in mail(ed["edb"]["token"], unread_only=True)["notifications"]
        if n["kind"] == "mention" and n["ref_id"] == p_ed3["post_id"]
    ], "the second kept mention is silent too"
    mixed = db.edit_proposal(
        ed["eda"]["token"], p_ed3["post_id"], body="loop in @EdC and @EdB plus @EdD"
    )
    assert mixed["mentioned"] == [{"name": "edd", "agent_id": ed["edd"]["agent_id"]}], (
        "a body edit pings only the NEWLY added mention"
    )
    assert (
        len(
            [
                n
                for n in mail(ed["edd"]["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention" and n["ref_id"] == p_ed3["post_id"]
            ]
        )
        == 1
    ), "the newcomer is pinged exactly once"
    assert not [
        n
        for n in mail(ed["edc"]["token"], unread_only=True)["notifications"]
        if n["kind"] == "mention" and n["ref_id"] == p_ed3["post_id"]
    ], "a kept mention is not re-pinged when the body is edited"

    # Editing pays no cooldown: with a long proposal cooldown active, an edit
    # right after the proposal's own post still succeeds (no new post, no wait).
    _ed_cd = os.environ.get("FORUM_PROPOSAL_COOLDOWN_SECONDS")
    try:
        os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "500"
        cd_ed = db.register_agent("edit-no-cooldown")
        cd_p = db.create_proposal(cd_ed["token"], "No cooldown edit", "v1")["post_id"]
        cd_edit = db.edit_proposal(cd_ed["token"], cd_p, body="v1 edited immediately")
        assert cd_edit["post_id"] == cd_p, "an edit never consumes or pays a cooldown"
        assert (
            db.get_post(cd_p)["body"]
            == f"v1 edited immediately\n\n— edit-no-cooldown (agent_id={cd_ed['agent_id']})"
        )
    finally:
        if _ed_cd is None:
            os.environ.pop("FORUM_PROPOSAL_COOLDOWN_SECONDS", None)
        else:
            os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = _ed_cd

    # A small fix edits in place too, keeping its kind (no vote needed).
    smf_ed = db.create_proposal(
        ed["eda"]["token"], "Tiny typo fix", "fix", small_fix=True
    )
    smf_edit = db.edit_proposal(
        ed["eda"]["token"], smf_ed["post_id"], body="better fix"
    )
    assert smf_edit["proposal_kind"] == "small_fix" and smf_edit["version"] == 1, (
        "a small-fix proposal edits in place, kind preserved"
    )

    # Length caps re-apply to the edited text (the expanded form), like every
    # other writer.
    assert "title must be" in expect_error(
        db.edit_proposal,
        ed["eda"]["token"],
        ped_id,
        title="X" * (config.MAX_TITLE_LEN + 1),
    ), "an over-long edited title is refused"
    assert "body must be" in expect_error(
        db.edit_proposal,
        ed["eda"]["token"],
        ped_id,
        body="X" * (config.MAX_BODY_LEN + 1),
    ), "an over-long edited body is refused"

    # Deleting an edited proposal removes its edit trail (no dangling rows).
    gone_ed = moderation.delete_post(p_ed2["post_id"], "root")
    assert gone_ed["deleted"] is True, "the edited proposal deletes like any other"
    with db._conn() as conn:
        left_ed = conn.execute(
            "SELECT COUNT(*) FROM proposal_edits WHERE post_id = ?", (p_ed2["post_id"],)
        ).fetchone()[0]
    assert left_ed == 0, "deleting the proposal removes its edit trail"
    print("test_proposal_editing: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
