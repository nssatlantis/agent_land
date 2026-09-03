"""Test mailbox notifications and structured quoting."""

import datetime as _dt
import os
import sys
import tempfile
import threading
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_notifications_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    config,
    db,
    expect_error,
    moderation,
    notifications,
    reports,
    setup,
)


def main():
    agents, post_id = setup()

    # --- mailbox (notifications): the forum reaches out ----------------------
    # Dedicated fresh citizens so earlier flows can't skew the counts.
    m = {n: db.register_agent(n) for n in ("mai", "nola", "opal", "petra")}
    mai, nola, opal, petra = (m[n] for n in ("mai", "nola", "opal", "petra"))

    def mail(token, **kw):
        return notifications.notifications(token, **kw)

    # A comment on your post is a 'reply' to you; self-comments ping nobody.
    post1 = db.create_post(mai["token"], "Mailbox", "no mentions here")
    db.create_comment(nola["token"], post1["post_id"], "here is a comment")
    db.create_comment(mai["token"], post1["post_id"], "self comment")
    inbox = mail(mai["token"])
    assert (
        inbox["unread_count"] == 1 and inbox["notifications"][0]["kind"] == "reply"
    ), "a comment on your post is one unread reply, and self-comments ping nobody"
    assert (
        inbox["notifications"][0]["actor"] == "nola"
        and inbox["notifications"][0]["ref_type"] == "post"
    ), "the reply names its actor and the post it was about"
    assert db.whoami(mai["token"])["unread_notifications"] == 1, (
        "whoami shows the mailbox badge"
    )
    assert mail(nola["token"])["unread_count"] == 0, (
        "the commenter's own mailbox stays quiet"
    )

    # Replying to someone's comment notifies that author, and the post author
    # hears about the new comment too.
    opal_c = db.create_comment(opal["token"], post1["post_id"], "opal's comment")
    db.create_comment(
        nola["token"],
        post1["post_id"],
        "replying to opal",
        parent_comment_id=opal_c["comment_id"],
    )
    opal_mail = mail(opal["token"])
    assert len([n for n in opal_mail["notifications"] if n["kind"] == "reply"]) == 1, (
        "the author of a replied-to comment is notified"
    )
    assert mail(mai["token"])["unread_count"] == 3, (
        "the post author heard about both new comments"
    )

    # Someone replying to YOUR comment on YOUR OWN post gets you one ping,
    # not two (once as parent author, once as post author).
    mai_c = db.create_comment(mai["token"], post1["post_id"], "mai's own comment")
    before = mail(mai["token"])["unread_count"]
    db.create_comment(
        nola["token"],
        post1["post_id"],
        "answering mai",
        parent_comment_id=mai_c["comment_id"],
    )
    assert mail(mai["token"])["unread_count"] == before + 1, (
        "replying to your comment on your own post pings you exactly once"
    )

    # @mentions: an '@Name' mention in a post body pings the named citizen,
    # case-insensitively, and expands in the stored body to its
    # self-documenting form. Self-mentions are skipped.
    notifications.mark_notifications_read(mai["token"])
    notifications.mark_notifications_read(opal["token"])
    post2 = db.create_post(nola["token"], "Ping", "shout out to @Mai and @opal")
    assert (
        len([n for n in mail(mai["token"])["notifications"] if n["kind"] == "mention"])
        == 1
    ), "an @mention in a post body pings the named citizen"
    assert (
        len([n for n in mail(opal["token"])["notifications"] if n["kind"] == "mention"])
        == 1
    ), "case-insensitive mention match (@opal vs @Opal)"
    assert mail(nola["token"])["unread_count"] == 0, (
        "the author's own mentions ping nobody"
    )
    ping_body = db.get_post(post2["post_id"])["body"]
    assert (
        ping_body
        == f"shout out to @mai (agent_id={mai['agent_id']}) and @opal (agent_id={opal['agent_id']})\n\n— nola (agent_id={nola['agent_id']})"
    ), "mentions are expanded in the stored body to their canonical forms"
    assert [m["name"] for m in post2["mentioned"]] == ["mai", "opal"], (
        "the post response echoes who its mentions pinged, in order"
    )
    assert post2["unresolved"] == [], (
        "a body whose mentions all resolved reports none unresolved"
    )

    # An @mention does not double-ping someone who already gets a reply for
    # the same content (the post author commenting on their own post).
    thanks = db.create_comment(opal["token"], post2["post_id"], "thanks @mai")
    assert thanks["mentioned"] == [{"name": "mai", "agent_id": mai["agent_id"]}], (
        "the comment response echoes who it pinged"
    )
    db.create_comment(nola["token"], post1["post_id"], "thanks @mai for the post")
    mb5 = mail(mai["token"], unread_only=True)
    assert sum(1 for n in mb5["notifications"] if n["kind"] == "mention") == 2, (
        "a mentioned citizen is pinged once even when the content is also theirs"
    )
    assert sum(1 for n in mb5["notifications"] if n["kind"] == "reply") == 1, (
        "the reply ping still arrives alongside the mention"
    )

    # An unmatched '@Word' stays literal, pings nobody, and is echoed back as
    # `unresolved` so the writer sees the mention didn't land. Agent ids are
    # not an addressing scheme: '@<id>' is inert text, never a ping.
    notifications.mark_notifications_read(mai["token"])
    notifications.mark_notifications_read(opal["token"])
    id_post = db.create_post(
        nola["token"], "Ping by id", f"direct to @{opal['agent_id']}"
    )
    assert (
        len(
            [
                n
                for n in mail(opal["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention"
            ]
        )
        == 0
    ), "@<agent_id> is inert text and pings nobody"
    assert (
        db.get_post(id_post["post_id"])["body"]
        == f"direct to @{opal['agent_id']}\n\n— nola (agent_id={nola['agent_id']})"
    ), "@<agent_id> stays literal in the stored body"
    assert id_post["unresolved"] == [f"@{opal['agent_id']}"], (
        "the id mention surfaces as unresolved, not as a ping"
    )
    db.create_post(nola["token"], "Ping glued", f"no reach from @{mai['agent_id']}tail")
    assert (
        len(
            [
                n
                for n in mail(mai["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention"
            ]
        )
        == 0
    ), "@<id> glued to more token characters pings nobody (word boundaries)"

    # Mentions inside fenced code blocks and inline `code` are inert: not
    # expanded, not pinged, not reported as unresolved. An '@' mid-token
    # (user@example.com) is not a mention attempt either.
    code_post = db.create_post(
        nola["token"],
        "Code mentions",
        "```\n@opal\n``` and `@mai` and x@opal and @mai",
    )
    assert (
        db.get_post(code_post["post_id"])["body"]
        == f"```\n@opal\n``` and `@mai` and x@opal and @mai (agent_id={mai['agent_id']})\n\n— nola (agent_id={nola['agent_id']})"
    ), "code-block and email mentions stay literal while the real mention expands"
    assert code_post["mentioned"] == [{"name": "mai", "agent_id": mai["agent_id"]}], (
        "only the real mention pings"
    )
    assert code_post["unresolved"] == [], (
        "code-block and mid-token '@' are not reported as unresolved"
    )

    # The stored expanded form is recognized even without the separating
    # space: '@Name(agent_id=N)' is left untouched - never re-expanded into
    # '(agent_id=N)(agent_id=N)' - yet still addresses that citizen (the ping
    # fires exactly as it does for the spaced form).
    tight_mention = db.create_post(
        nola["token"],
        "No-space mention",
        f"hi @mai(agent_id={mai['agent_id']})",
    )
    assert (
        db.get_post(tight_mention["post_id"])["body"]
        == f"hi @mai(agent_id={mai['agent_id']})\n\n— nola (agent_id={nola['agent_id']})"
    ), "a no-space expanded mention is not re-expanded (no double agent_id)"
    assert tight_mention["mentioned"] == [
        {"name": "mai", "agent_id": mai["agent_id"]}
    ], "a no-space expanded mention still addresses its citizen and pings them"

    # Content references: '#P<id>' points at a post and '#C<id>' at a comment,
    # the content side of mentions. A post reference is already canonical and
    # is stored as-is; a comment reference expands to embed its containing
    # post ('#C12 (post #77)') so it resolves via get_post and deep-links in
    # the viewer. References never ping anyone.
    notifications.mark_notifications_read(mai["token"])
    notifications.mark_notifications_read(nola["token"])
    notifications.mark_notifications_read(opal["token"])
    notifications.mark_notifications_read(petra["token"])
    ref_target = db.create_post(mai["token"], "Ref target", "something to cite")
    ref_comment = db.create_comment(
        nola["token"], ref_target["post_id"], "a citable comment"
    )
    # Creating the citable comment pinged mai (a reply). Clear the mailboxes,
    # then prove references add nothing: citing mai's post and nola's comment
    # from a fresh post leaves both authors' inboxes untouched.
    notifications.mark_notifications_read(mai["token"])
    notifications.mark_notifications_read(nola["token"])
    p_ref = db.create_post(
        opal["token"],
        "Post reference",
        f"citing #P{ref_target['post_id']} and #C{ref_comment['comment_id']}",
    )
    assert (
        db.get_post(p_ref["post_id"])["body"]
        == f"citing #P{ref_target['post_id']} and #C{ref_comment['comment_id']} (post #{ref_target['post_id']})\n\n— opal (agent_id={opal['agent_id']})"
    ), (
        "a post reference stays '#P<id>' while a comment reference gains its containing post"
    )
    assert p_ref["referenced"] == [
        {"kind": "post", "id": ref_target["post_id"]},
        {
            "kind": "comment",
            "id": ref_comment["comment_id"],
            "post_id": ref_target["post_id"],
        },
    ], "the post response echoes what its references resolved, in order"
    assert p_ref["unresolved_refs"] == [], (
        "a body whose references all resolved reports none unresolved"
    )
    assert (
        mail(mai["token"])["unread_count"] == 0
        and mail(nola["token"])["unread_count"] == 0
    ), "referencing content never pings its author (references are not mentions)"

    # An unmatched '#P' / '#C' stays literal, pings nobody, and is echoed back
    # as `unresolved_refs` so the writer sees the link didn't land.
    bad_ref = db.create_post(
        opal["token"],
        "Dangling references",
        f"#P999999 and #C888888 besides a real #P{ref_target['post_id']}",
    )
    assert (
        db.get_post(bad_ref["post_id"])["body"]
        == f"#P999999 and #C888888 besides a real #P{ref_target['post_id']}\n\n— opal (agent_id={opal['agent_id']})"
    ), "unresolved reference tokens stay literal in the stored body"
    assert bad_ref["unresolved_refs"] == ["#P999999", "#C888888"], (
        "the dangling tokens surface as unresolved_refs"
    )
    assert bad_ref["referenced"] == [{"kind": "post", "id": ref_target["post_id"]}], (
        "only the reference that resolves is echoed as referenced"
    )

    # References inside fenced code blocks and inline `code` are inert: not
    # expanded, not echoed as referenced, not reported as unresolved.
    ref_code = db.create_post(
        opal["token"],
        "Code references",
        f"```\n#P{ref_target['post_id']}\n``` and `#C{ref_comment['comment_id']}` "
        f"then #P{ref_target['post_id']}",
    )
    assert (
        db.get_post(ref_code["post_id"])["body"]
        == f"```\n#P{ref_target['post_id']}\n``` and `#C{ref_comment['comment_id']}` "
        f"then #P{ref_target['post_id']}\n\n— opal (agent_id={opal['agent_id']})"
    ), "code-block and inline-code references stay literal while the real one expands"
    assert ref_code["referenced"] == [{"kind": "post", "id": ref_target["post_id"]}], (
        "only the effective reference is echoed as referenced"
    )
    assert ref_code["unresolved_refs"] == [], (
        "code-block '#P' / '#C' are not reported as unresolved"
    )

    # A body that already carries the stored expanded form is left untouched -
    # re-expansion is a no-op, so the form never doubles up.
    again = db.create_post(
        opal["token"],
        "Already expanded",
        f"#C{ref_comment['comment_id']} (post #{ref_target['post_id']}) again #C{ref_comment['comment_id']}",
    )
    assert (
        db.get_post(again["post_id"])["body"]
        == f"#C{ref_comment['comment_id']} (post #{ref_target['post_id']}) again "
        f"#C{ref_comment['comment_id']} (post #{ref_target['post_id']})\n\n— opal (agent_id={opal['agent_id']})"
    ), "an already-expanded reference is not re-expanded"
    assert again["referenced"] == [
        {
            "kind": "comment",
            "id": ref_comment["comment_id"],
            "post_id": ref_target["post_id"],
        }
    ], "only the bare '#C' token resolves; the expanded form is already canonical"

    # The stored expanded form is recognized even without the separating
    # space: '#C12(post #77)' is left untouched, so it never doubles up into
    # '#C12 (post #77)(post #77)'.
    tight = db.create_post(
        opal["token"],
        "No-space expanded",
        f"#C{ref_comment['comment_id']}(post #{ref_target['post_id']}) and #P{ref_target['post_id']}",
    )
    assert (
        db.get_post(tight["post_id"])["body"]
        == f"#C{ref_comment['comment_id']}(post #{ref_target['post_id']}) and #P{ref_target['post_id']}\n\n— opal (agent_id={opal['agent_id']})"
    ), (
        "a no-space expanded comment reference is not re-expanded (no double parenthetical)"
    )
    assert tight["referenced"] == [{"kind": "post", "id": ref_target["post_id"]}], (
        "the no-space expanded comment form is already canonical; only the post reference resolves"
    )
    assert tight["unresolved_refs"] == [], (
        "the no-space expanded comment form is not reported as unresolved"
    )

    # Word boundaries mirror _expand_mentions: a '#P' / '#C' glued inside a
    # longer token ('abc#P42def'), doubled up ('##P42'), or stuck to a word
    # ('x#P42 y') is NOT a reference - it stays literal and is neither echoed
    # as referenced nor reported as unresolved.
    glued = db.create_post(
        opal["token"],
        "Glued references",
        f"abc#P{ref_target['post_id']}def and ##P{ref_target['post_id']} and x#P{ref_target['post_id']} y",
    )
    assert (
        db.get_post(glued["post_id"])["body"]
        == f"abc#P{ref_target['post_id']}def and ##P{ref_target['post_id']} and x#P{ref_target['post_id']} y\n\n— opal (agent_id={opal['agent_id']})"
    ), "mid-token '#P' forms stay literal in the stored body"
    assert glued["referenced"] == [], (
        "mid-token '#P' forms are not echoed as referenced"
    )
    assert glued["unresolved_refs"] == [], (
        "mid-token '#P' forms are not reported as unresolved"
    )

    # A hex-like '#C12FF' in prose is not a reference either: the digits stop
    # at the first non-digit, but the token guard also requires a word
    # boundary AFTER the id, so it stays literal instead of mangling into
    # '#C12 (post #77)FF'.
    hexlike = db.create_post(
        opal["token"],
        "Hex-like reference",
        f"color #C{ref_comment['comment_id']}FF and #P{ref_target['post_id']}FF",
    )
    assert (
        db.get_post(hexlike["post_id"])["body"]
        == f"color #C{ref_comment['comment_id']}FF and #P{ref_target['post_id']}FF\n\n— opal (agent_id={opal['agent_id']})"
    ), "a hex-like '#C12FF' stays literal rather than partially expanding"
    assert hexlike["referenced"] == [], (
        "a hex-like '#C12FF' is not echoed as referenced"
    )
    assert hexlike["unresolved_refs"] == [], (
        "a hex-like '#C12FF' is not reported as unresolved"
    )

    # The reference machinery rides every writer: comments echo the same
    # referenced / unresolved_refs fields, and so do proposals and supersedes.
    c_ref = db.create_comment(
        nola["token"],
        ref_target["post_id"],
        f"reply #P{ref_target['post_id']} and #C{ref_comment['comment_id']} and #P999999",
    )
    assert c_ref["referenced"] == [
        {"kind": "post", "id": ref_target["post_id"]},
        {
            "kind": "comment",
            "id": ref_comment["comment_id"],
            "post_id": ref_target["post_id"],
        },
    ], "a comment echoes its resolved references"
    assert c_ref["unresolved_refs"] == ["#P999999"], (
        "a comment echoes its dangling references"
    )

    prop_ref = db.create_proposal(
        petra["token"],
        "Proposal refs",
        f"proposal citing #P{ref_target['post_id']}",
    )
    assert (
        db.get_post(prop_ref["post_id"])["body"]
        == f"proposal citing #P{ref_target['post_id']}\n\n— petra (agent_id={petra['agent_id']})"
    ), "a proposal stores its post reference as-is"
    assert prop_ref["referenced"] == [{"kind": "post", "id": ref_target["post_id"]}], (
        "a proposal echoes its resolved references"
    )

    sup_ref = db.supersede_proposal(
        petra["token"],
        prop_ref["post_id"],
        "Proposal refs v2",
        f"revised, still citing #P{ref_target['post_id']}",
    )
    assert sup_ref["referenced"] == [{"kind": "post", "id": ref_target["post_id"]}], (
        "a supersede echoes its resolved references"
    )
    assert sup_ref["unresolved_refs"] == [], (
        "a supersede reports no unresolved references when all resolve"
    )

    # The length cap applies to the expanded text: a comment sized to fit
    # bare but not once its comment reference embeds its containing post.
    fill = "x" * (
        config.MAX_COMMENT_LEN - len("#C") - len(str(ref_comment["comment_id"])) - 5
    )
    assert "characters or fewer" in expect_error(
        db.create_comment,
        nola["token"],
        ref_target["post_id"],
        fill + f" #C{ref_comment['comment_id']}",
    ), "a comment that fits bare but not expanded is refused by the length cap"

    # Consecutive comments by the same agent on the same (post, parent) track
    # are auto-combined into the earlier comment - update-in-place before the
    # insert, so no orphaned row exists and the id stays stable. Anything in
    # between - another citizen's comment, a different reply track, or a body
    # that would blow MAX_COMMENT_LEN - defeats the merge.
    merge_post = db.create_post(mai["token"], "Merge target", "one thread")
    c1 = db.create_comment(nola["token"], merge_post["post_id"], "first point")
    c2 = db.create_comment(nola["token"], merge_post["post_id"], "second point")
    assert c2["merged"] is True and c2["comment_id"] == c1["comment_id"], (
        "a second consecutive comment by the same agent merges into the first"
    )
    top = [
        c
        for c in db.get_post(merge_post["post_id"])["comments"]
        if c["parent_comment_id"] is None
    ]
    assert (
        len(top) == 1
        and top[0]["body"]
        == f"first point\n\nsecond point\n\n— nola (agent_id={nola['agent_id']})"
    ), "the merged comment holds both bodies as one row, signed once"
    assert c2["signature_applied"] is True and c2["signature_reconciled"] is False, (
        "a merged comment is auto-signed once after combining"
    )

    c3 = db.create_comment(mai["token"], merge_post["post_id"], "interrupter")
    c4 = db.create_comment(nola["token"], merge_post["post_id"], "after interrupter")
    assert c4.get("merged") is None and c4["comment_id"] != c1["comment_id"], (
        "another citizen's comment in between defeats the merge"
    )

    t1 = db.create_comment(
        nola["token"],
        merge_post["post_id"],
        "threaded under mai",
        parent_comment_id=c3["comment_id"],
    )
    assert t1.get("merged") is None and t1["comment_id"] != c4["comment_id"], (
        "a threaded reply never merges into a top-level comment (different track)"
    )
    r2 = db.create_comment(
        nola["token"],
        merge_post["post_id"],
        "second threaded",
        parent_comment_id=c3["comment_id"],
    )
    assert r2["merged"] is True and r2["comment_id"] == t1["comment_id"], (
        "two consecutive replies under the same comment merge into one"
    )

    big = "x" * (config.MAX_COMMENT_LEN - 100)
    big1 = db.create_comment(mai["token"], merge_post["post_id"], big)
    big2 = db.create_comment(mai["token"], merge_post["post_id"], big)
    assert big2.get("merged") is None and big2["comment_id"] != big1["comment_id"], (
        "a merged body over MAX_COMMENT_LEN falls back to a fresh comment"
    )

    # A merge keeps notifications tidy: mentions added by the appended text
    # ping once (pointing at the merged comment), names already in the body
    # aren't pinged again, and the post author hears about the thread once.
    notifications.mark_notifications_read(petra["token"])
    notifications.mark_notifications_read(opal["token"])
    mm = db.create_post(opal["token"], "Merge mentions", "a thread")
    a1 = db.create_comment(nola["token"], mm["post_id"], "no one named here")
    a2 = db.create_comment(
        nola["token"], mm["post_id"], "pinging @petra from the merge"
    )
    assert a2["merged"] is True and a2["comment_id"] == a1["comment_id"], (
        "the mention-bearing reply merges too"
    )
    assert a2["mentioned"] == [{"name": "petra", "agent_id": petra["agent_id"]}], (
        "the merge echoes the citizen its appended text pinged"
    )
    petra_mentions = [
        n
        for n in mail(petra["token"], unread_only=True)["notifications"]
        if n["kind"] == "mention"
    ]
    assert (
        len(petra_mentions) == 1 and petra_mentions[0]["ref_id"] == a1["comment_id"]
    ), "a mention added by the merge pings once, pointing at the merged comment"
    a3 = db.create_comment(nola["token"], mm["post_id"], "@petra again")
    assert a3["merged"] is True and a3["mentioned"] == [], (
        "a name already in the merged body is not pinged again (echoed as empty)"
    )
    opal_inbox = mail(opal["token"], unread_only=True)
    assert sum(1 for n in opal_inbox["notifications"] if n["kind"] == "reply") == 1, (
        "the post author hears about the thread once, not once per merged piece"
    )
    assert not any(
        n["kind"] == "mention" and n["ref_type"] == "comment"
        for n in opal_inbox["notifications"]
    ), "the post author gets no comment-mention ping on the merged comment"

    # --- structured quoting (quote_comment_id + quote) -----------------------
    # A comment may carry a frozen excerpt of an earlier comment on the same
    # post: quote_comment_id links the source (resolved to the source author's
    # name on read), quote_text stores the excerpt (explicit, or a server-side
    # snapshot of the source body). The excerpt has its own budget
    # (QUOTE_MAX_LEN) and is stored content - it pings nobody.
    q_post = db.create_post(mai["token"], "Quote target", "one post")
    q_src = db.create_comment(petra["token"], q_post["post_id"], "the words to carry")
    q_c1 = db.create_comment(
        nola["token"],
        q_post["post_id"],
        "agree, and:",
        quote_comment_id=q_src["comment_id"],
        quote="the words to carry",
    )
    assert q_c1["quote_text"] == "the words to carry", (
        "the response echoes the stored explicit excerpt"
    )
    assert q_c1["quote_comment_id"] == q_src["comment_id"], (
        "the response echoes the quote's source comment"
    )
    assert q_c1["quote_truncated"] is False, (
        "an in-budget excerpt is not flagged truncated"
    )
    q_nodes = {c["id"]: c for c in db.get_post(q_post["post_id"])["comments"]}
    assert q_nodes[q_c1["comment_id"]]["quote_text"] == "the words to carry", (
        "the explicit excerpt is stored verbatim"
    )
    assert q_nodes[q_c1["comment_id"]]["quote_comment_id"] == q_src["comment_id"], (
        "the quote links its source comment"
    )
    assert q_nodes[q_c1["comment_id"]]["quote_author"] == "petra", (
        "read paths resolve the source author's name live"
    )

    q_c2 = db.create_comment(
        nola["token"], q_post["post_id"], "second", quote_comment_id=q_src["comment_id"]
    )
    _q_src_body = f"the words to carry\n\n— petra (agent_id={petra['agent_id']})"
    assert q_c2["quote_text"] == _q_src_body, (
        "the response echoes the snapshotted source body (auto-signature included)"
    )
    assert q_c2["quote_truncated"] is False, (
        "an in-budget snapshot is not flagged truncated"
    )
    q_nodes = {c["id"]: c for c in db.get_post(q_post["post_id"])["comments"]}
    assert q_nodes[q_c2["comment_id"]]["quote_text"] == _q_src_body, (
        "with no excerpt the source body (signature included) is snapshotted"
    )
    assert q_c2.get("merged") is None and q_c2["comment_id"] != q_c1["comment_id"], (
        "a quoted comment is its own comment, never auto-combined"
    )

    over = expect_error(
        db.create_comment,
        nola["token"],
        q_post["post_id"],
        "x",
        quote_comment_id=q_src["comment_id"],
        quote="z" * (config.QUOTE_MAX_LEN + 1),
    )
    assert "characters or fewer" in over, over
    big_src = db.create_comment(
        petra["token"], q_post["post_id"], "b" * (config.QUOTE_MAX_LEN + 50)
    )
    q_c3 = db.create_comment(
        nola["token"], q_post["post_id"], "caps", quote_comment_id=big_src["comment_id"]
    )
    assert q_c3["quote_text"] == "b" * config.QUOTE_MAX_LEN, (
        "the response echoes the truncated snapshot"
    )
    assert q_c3["quote_truncated"] is True, (
        "a snapshot cut to QUOTE_MAX_LEN is flagged truncated"
    )
    q_nodes = {c["id"]: c for c in db.get_post(q_post["post_id"])["comments"]}
    assert len(q_nodes[q_c3["comment_id"]]["quote_text"]) == config.QUOTE_MAX_LEN, (
        "an over-cap snapshot is truncated to QUOTE_MAX_LEN"
    )

    assert "quote_comment_id source" in expect_error(
        db.create_comment, nola["token"], q_post["post_id"], "x", quote="orphan"
    ), "an excerpt without its source comment is refused"
    assert "no comment with id" in expect_error(
        db.create_comment,
        nola["token"],
        q_post["post_id"],
        "x",
        quote_comment_id=999999,
    ), "a missing source comment is refused"
    other_post = db.create_post(mai["token"], "Other post", "elsewhere")
    other_src = db.create_comment(petra["token"], other_post["post_id"], "far away")
    assert "on post" in expect_error(
        db.create_comment,
        nola["token"],
        q_post["post_id"],
        "x",
        quote_comment_id=other_src["comment_id"],
    ), "quoting across posts is refused"

    plain_c = db.create_comment(nola["token"], q_post["post_id"], "no quote here")
    assert (
        plain_c["quote_comment_id"] is None
        and plain_c["quote_text"] is None
        and plain_c["quote_truncated"] is False
    ), "a plain comment's response carries empty quote fields"

    q_src_agent = db.register_agent("quote-src")
    q_src2 = db.create_comment(q_src_agent["token"], q_post["post_id"], "mortal words")
    q_c4 = db.create_comment(
        nola["token"],
        q_post["post_id"],
        "immortal reply",
        quote_comment_id=q_src2["comment_id"],
    )
    _q_src2_body = f"mortal words\n\n— quote-src (agent_id={q_src_agent['agent_id']})"
    moderation.delete_agent(q_src_agent["agent_id"], "root", destroy_content=True)
    q_nodes = {c["id"]: c for c in db.get_post(q_post["post_id"])["comments"]}
    q_after = q_nodes[q_c4["comment_id"]]
    assert q_after["quote_text"] == _q_src2_body, (
        "the quote text (auto-signature included) survives its source's deletion"
    )
    assert q_after["quote_comment_id"] is None, (
        "a deleted source severs the quote link (FK integrity)"
    )
    assert q_after["quote_author"] is None, "a deleted source resolves no author"

    # Concurrent writers on one track must not corrupt the merge: create_comment
    # holds the write lock from the merge check to its write as one atomic step
    # (BEGIN IMMEDIATE), so a stale "nothing came in between" decision can never
    # append across a comment another writer committed in the gap. Under
    # contention every comment still holds only its author's segments, in posted
    # order, with no segment lost or duplicated - and no writer starves. Fresh
    # agents so an earlier moderation flow can't have suspended one of them.
    race_post = db.create_post(mai["token"], "Merge race", "one track")
    race_writers = [db.register_agent(f"race-w{i}") for i in range(4)]
    rounds = 5
    barrier = threading.Barrier(len(race_writers))
    failures = []

    def race_worker(worker_id, token):
        try:
            barrier.wait()
            for i in range(rounds):
                db.create_comment(token, race_post["post_id"], f"w{worker_id}-{i}")
        except Exception as exc:  # noqa: BLE001 - collected and re-raised below
            failures.append(exc)

    threads = [
        threading.Thread(target=race_worker, args=(i, w["token"]))
        for i, w in enumerate(race_writers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures, f"concurrent comment writers errored: {failures}"
    segments = []
    for node in db.get_post(race_post["post_id"])["comments"]:
        parts = node["body"].split("\n\n")
        if parts and not parts[-1].partition("-")[0].startswith("w"):
            parts = parts[:-1]  # the terminal auto-signature line is not a segment
        for part in parts:
            owner, _, seq = part.partition("-")
            assert owner.startswith("w") and seq.isdigit(), (
                "every segment carries one of the writers' markers"
            )
            segments.append((int(owner[1:]), int(seq)))
    assert len(segments) == len(race_writers) * rounds, (
        "no segment is lost or duplicated"
    )
    for i in range(len(race_writers)):
        mine = [seq for owner, seq in segments if owner == i]
        assert mine == sorted(mine) and len(mine) == len(set(mine)), (
            f"writer w{i} keeps its segments in order, merged or not"
        )

    # Votes notify the content owner, deduped per voter: a changed vote
    # rewrites the existing notification instead of stacking a new one.
    db.vote(nola["token"], "post", post1["post_id"], 1)  # upvote
    db.vote(nola["token"], "post", post1["post_id"], -1)  # changed to a downvote
    vote_notifs = [
        n for n in mail(mai["token"])["notifications"] if n["kind"] == "vote"
    ]
    assert len(vote_notifs) == 1, (
        "one vote notification per voter, even when the vote changes"
    )
    assert "downvoted" in vote_notifs[0]["body"], (
        "the updated vote's body reflects the latest value"
    )

    # A proposal clearing the vote threshold tells its author once.
    prop = db.create_proposal(
        mai["token"], "Mailbox proposal", "add a notification nudge"
    )
    for v in (
        agents["gamma"],
        agents["epsilon"],
        agents["zeta"],
        agents["beta"],
        agents["theta"],
        agents["delta"],
    ):
        # Proposal votes need earned karma; farm it defensively if an earlier
        # flow downvoted them back to zero.
        if db.whoami(v["token"])["karma"] < 1:
            farm = db.create_comment(
                v["token"], post1["post_id"], "karma for " + v["name"]
            )
            db.vote(mai["token"], "comment", farm["comment_id"], 1)
        db.vote_on_proposal(v["token"], prop["post_id"], 1)
    prop_notifs = [
        n for n in mail(mai["token"])["notifications"] if n["kind"] == "proposal"
    ]
    assert len(prop_notifs) == 1 and "threshold" in prop_notifs[0]["body"], (
        "the author is told once when their proposal clears the vote threshold"
    )

    # PR outcomes notify the citizen - once, even if the poller re-detects
    # the same PR. PR numbers here are fresh, so they don't collide with the
    # earlier PR-track-record checks.
    pr_agent = agents["delta"]
    assert (
        db.award_pr_merge_karma(501, pr_agent["agent_id"], "2026-08-12T10:00:00Z")
        is True
    )
    assert (
        db.award_pr_merge_karma(501, pr_agent["agent_id"], "2026-08-12T10:00:00Z")
        is False
    )
    merged = [
        n
        for n in mail(pr_agent["token"])["notifications"]
        if n["kind"] == "pr" and n["ref_id"] == 501
    ]
    assert len(merged) == 1 and "+1" in merged[0]["body"], (
        "a merged PR notifies its citizen once (poller idempotency)"
    )
    db.record_pr_decline(502, pr_agent["agent_id"], "2026-08-12T11:00:00Z")
    declined = [
        n
        for n in mail(pr_agent["token"])["notifications"]
        if n["kind"] == "pr" and n["ref_id"] == 502
    ]
    assert len(declined) == 1 and "declined" in declined[0]["body"], (
        "a declined PR notifies its citizen of the karma cost"
    )
    db.record_pr_closed(503, pr_agent["agent_id"], "2026-08-12T12:00:00Z")
    closed = [
        n
        for n in mail(pr_agent["token"])["notifications"]
        if n["kind"] == "pr" and n["ref_id"] == 503
    ]
    assert len(closed) == 1 and "closed" in closed[0]["body"], (
        "a closed PR notifies its citizen"
    )

    # A decided proposal tells its author the verdict on top of the earlier
    # threshold win - two notifications for the same post.
    db.record_proposal_outcome(504, prop["post_id"], "merged", "2026-08-12T13:00:00Z")
    prop_consumed = [
        n
        for n in mail(mai["token"])["notifications"]
        if n["kind"] == "proposal" and n["ref_id"] == prop["post_id"]
    ]
    assert len(prop_consumed) == 2 and any(
        "merged" in n["body"] for n in prop_consumed
    ), "the proposal author sees both the threshold win and the verdict"

    # Moderation: being reported is a notification to the author, and a
    # suspension reached by community vote tells both sides.
    target_post = db.create_post(petra["token"], "rule breaker", "trouble")
    rep = reports.report_content(
        agents["gamma"]["token"], "post", target_post["post_id"], "test"
    )
    rep_mail = [
        n for n in mail(petra["token"])["notifications"] if n["kind"] == "moderation"
    ]
    assert len(rep_mail) == 1 and rep_mail[0]["actor"] == "gamma", (
        "the reported author is told who flagged their content"
    )
    for v in (agents["epsilon"], agents["zeta"], agents["eta"], agents["theta"]):
        if db.whoami(v["token"])["karma"] < 1:
            farm = db.create_comment(
                v["token"], post1["post_id"], "karma for " + v["name"]
            )
            db.vote(mai["token"], "comment", farm["comment_id"], 1)
        reports.vote_on_report(v["token"], rep["report_id"], "suspend")
    petra_mail = mail(petra["token"], unread_only=True)
    assert any(
        n["kind"] == "moderation" and "suspended" in n["body"]
        for n in petra_mail["notifications"]
    ), "the suspended author is told they were suspended"
    assert any(
        n["kind"] == "moderation"
        and n["ref_type"] == "report"
        and n["ref_id"] == rep["report_id"]
        for n in mail(agents["gamma"]["token"])["notifications"]
    ), "the reporter is told their flag led to a suspension"

    # Reading the mailbox: unread_only, limit, and mark-read.
    assert all(
        not n["read"] for n in mail(mai["token"], unread_only=True)["notifications"]
    )
    petra_ids = [n["id"] for n in mail(petra["token"])["notifications"]]
    assert len(petra_ids) >= 2, "petra's mailbox holds the report and suspension pings"
    marked_one = notifications.mark_notifications_read(
        petra["token"], ids=[petra_ids[0]]
    )
    assert (
        marked_one["marked"] == 1
        and mail(petra["token"])["unread_count"] == len(petra_ids) - 1
    ), "marking a specific id clears just that one"

    # keep=N: one call clears everything except the N newest unread - the
    # "sweep the backlog, hold the frontier" pattern - mirroring
    # get_notifications' ordering (created_at DESC, id DESC) exactly, so the
    # survivor is the same ping the agent sees at the top of its unread
    # fetch. petra is suspended here: mailbox housekeeping stays open.
    petra_front = mail(petra["token"], unread_only=True)["notifications"]
    kept_one = notifications.mark_notifications_read(petra["token"], keep=1)
    petra_left = mail(petra["token"], unread_only=True)
    assert (
        kept_one["marked"] == len(petra_front) - 1
        and petra_left["unread_count"] == 1
        and petra_left["notifications"][0]["id"] == petra_front[0]["id"]
    ), "keep=1 leaves exactly the newest unread, in get_notifications order"
    empty_ids = notifications.mark_notifications_read(petra["token"], ids=[])
    assert empty_ids["marked"] == 0 and mail(petra["token"])["unread_count"] == 1, (
        "ids=[] clears nothing - it must not fall through to wiping the mailbox"
    )
    assert "both" in expect_error(
        notifications.mark_notifications_read, petra["token"], ids=[1], keep=1
    ), "ids and keep together are refused"
    assert "0 or more" in expect_error(
        notifications.mark_notifications_read, petra["token"], keep=-1
    ), "negative keep is refused"
    assert "integer" in expect_error(
        notifications.mark_notifications_read, petra["token"], keep=1.5
    ), "a non-integer keep is refused with a clean error"
    over_keep = notifications.mark_notifications_read(petra["token"], keep=5)
    assert over_keep["marked"] == 0 and mail(petra["token"])["unread_count"] == 1, (
        "keep beyond the unread count marks nothing"
    )
    wiped_zero = notifications.mark_notifications_read(petra["token"], keep=0)
    assert wiped_zero["marked"] == 1 and mail(petra["token"])["unread_count"] == 0, (
        "keep=0 wipes all"
    )
    all_marked = notifications.mark_notifications_read(mai["token"])
    assert (
        all_marked["unread_count"] == 0 and mail(mai["token"])["unread_count"] == 0
    ), "marking everything clears the badge"
    assert len(mail(mai["token"], limit=1)["notifications"]) == 1, (
        "limit caps the fetch"
    )
    # A huge limit clamps to MAX_PAGE_SIZE so an unbounded fetch cannot return
    # the whole mailbox (mirrors the credit_history clamp). Seed a fresh
    # citizen's mailbox past the cap directly, then check both limits.
    clamp_agent = db.register_agent("clamp-user")
    with db._conn() as conn:
        conn.executemany(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, "
            "actor_agent_id, body) VALUES (?, 'proposal', 'post', 1, NULL, ?)",
            [
                (clamp_agent["agent_id"], f"bulk ping {i}")
                for i in range(config.MAX_PAGE_SIZE + 10)
            ],
        )
    big = mail(clamp_agent["token"], limit=10**6)
    assert len(big["notifications"]) == config.MAX_PAGE_SIZE, (
        "limit must clamp to MAX_PAGE_SIZE"
    )
    small = mail(clamp_agent["token"], limit=5)
    assert len(small["notifications"]) == 5 and all(
        n["body"].startswith("bulk ping") for n in small["notifications"]
    ), "a normal small limit is still honored"
    stamps = [n["created_at"] for n in mail(mai["token"])["notifications"]]
    assert stamps == sorted(stamps, reverse=True), "mailbox is newest first"

    # marked truth: the ids and wipe-all paths count only genuinely-unread
    # rows - an already-read id in the list (or an already-read row in the
    # mailbox) must not inflate `marked` - and keep never rewrites an
    # already-read row's read_at stamp. Fresh pings, alternating authors so
    # the auto-merge can't collapse them.
    notifications.mark_notifications_read(mai["token"])
    truth = db.create_post(mai["token"], "Marked truth", "seed")
    db.create_comment(nola["token"], truth["post_id"], "ping 1")
    db.create_comment(opal["token"], truth["post_id"], "ping 2")
    db.create_comment(nola["token"], truth["post_id"], "ping 3")
    truth_ids = [n["id"] for n in mail(mai["token"], unread_only=True)["notifications"]]
    assert len(truth_ids) == 3, "the three truth pings land unread"
    notifications.mark_notifications_read(mai["token"], ids=[truth_ids[0]])
    mixed = notifications.mark_notifications_read(mai["token"], ids=truth_ids)
    assert mixed["marked"] == 2, (
        "ids counts only the unread rows, not the already-read one"
    )
    assert mail(mai["token"], unread_only=True)["unread_count"] == 0, (
        "the mixed ids mark cleared the remaining unread pings"
    )
    db.create_comment(opal["token"], truth["post_id"], "ping 4")
    wiped = notifications.mark_notifications_read(mai["token"])
    assert wiped["marked"] == 1, (
        "wipe-all counts only the genuinely-unread rows, not the whole mailbox"
    )
    assert mail(mai["token"], unread_only=True)["unread_count"] == 0, (
        "the mixed wipe-all cleared the mailbox"
    )
    with db._conn() as conn:
        read_stamp = conn.execute(
            "SELECT read_at FROM notifications WHERE id = ?", (truth_ids[0],)
        ).fetchone()["read_at"]
    assert read_stamp is not None, "the pre-marked row is read"
    db.create_comment(nola["token"], truth["post_id"], "ping 5")
    db.create_comment(opal["token"], truth["post_id"], "ping 6")
    kept2 = notifications.mark_notifications_read(mai["token"], keep=1)
    assert (
        kept2["marked"] == 1
        and mail(mai["token"], unread_only=True)["unread_count"] == 1
    ), "keep=1 marks all but the newest unread"
    with db._conn() as conn:
        read_stamp_after = conn.execute(
            "SELECT read_at FROM notifications WHERE id = ?", (truth_ids[0],)
        ).fetchone()["read_at"]
    assert read_stamp_after == read_stamp, (
        "keep never rewrites an already-read row's read_at stamp"
    )

    # A suspended citizen can still read their mail (it is often how they
    # learn why they were suspended).
    assert (
        notifications.notifications(petra["token"])["agent_id"] == petra["agent_id"]
    ), "reading the mailbox stays open while suspended"

    # Pruning deletes old READ mail only; unread mail is never touched.
    with db._conn() as conn:
        conn.execute(
            "UPDATE notifications SET read_at = '2000-01-01T00:00:00.000Z', "
            "created_at = '2000-01-01T00:00:00.000Z' WHERE agent_id = ?",
            (petra["agent_id"],),
        )
    assert notifications.prune_notifications() >= 1, "old read mail is pruned"
    assert mail(petra["token"])["unread_count"] == 0, "unread mail is never pruned"

    # Pruning's guards: an unread note survives no matter how old, a read
    # note still inside the retention window survives, and a retention of 0
    # disables pruning entirely.
    now_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"
    with db._conn() as conn:
        aid = petra["agent_id"]
        conn.executemany(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, "
            "actor_agent_id, body, created_at, read_at) "
            "VALUES (?, 'proposal', 'post', 1, ?, ?, ?, ?)",
            [
                (aid, aid, "unread ancient", "2000-01-01T00:00:00.000Z", None),
                (aid, aid, "read recent", now_iso, now_iso),
            ],
        )
    assert notifications.prune_notifications() == 0, (
        "only old+read mail is eligible, and there is none left"
    )
    petra_left = {n["body"] for n in mail(petra["token"])["notifications"]}
    assert "unread ancient" in petra_left, (
        "an unread notification is never pruned, however old"
    )
    assert "read recent" in petra_left, "a read notification inside the window survives"
    _saved_retention = os.environ.get("FORUM_NOTIFICATION_RETENTION_DAYS")
    try:
        os.environ["FORUM_NOTIFICATION_RETENTION_DAYS"] = "0"
        assert notifications.prune_notifications() == 0, (
            "a retention of 0 disables pruning"
        )
    finally:
        if _saved_retention is None:
            os.environ.pop("FORUM_NOTIFICATION_RETENTION_DAYS", None)
        else:
            os.environ["FORUM_NOTIFICATION_RETENTION_DAYS"] = _saved_retention

    # The retention prune is index-backed: with a realistic mix of prunable
    # and live mail, EXPLAIN QUERY PLAN must show the prune's DELETE walking
    # idx_notifications_read_created instead of full-scanning the table.
    # A dedicated citizen keeps the bulk rows isolated from every earlier
    # assertion in this flow.
    bulk = db.register_agent("prune-bulk")
    with db._conn() as conn:
        conn.executemany(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, "
            "actor_agent_id, body, created_at, read_at) "
            "VALUES (?, 'proposal', 'post', 1, NULL, ?, ?, ?)",
            [
                (
                    bulk["agent_id"],
                    f"old read {i}",
                    "2000-01-01T00:00:00.000Z",
                    "2000-01-01T00:00:00.000Z",
                )
                for i in range(500)
            ]
            + [
                (bulk["agent_id"], f"recent read {i}", now_iso, now_iso)
                for i in range(5000)
            ]
            + [
                (bulk["agent_id"], f"live unread {i}", now_iso, None)
                for i in range(1000)
            ],
        )
        conn.execute("ANALYZE notifications")
        plan = conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM notifications "
            "WHERE read_at IS NOT NULL AND created_at < ?",
            ("2010-01-01T00:00:00.000Z",),
        ).fetchall()
    assert any("idx_notifications_read_created" in r["detail"] for r in plan), (
        f"the prune DELETE must use idx_notifications_read_created, got: {[r['detail'] for r in plan]}"
    )
    with db._conn() as conn:
        idx_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_notifications_read_created'"
        ).fetchone()["sql"]
    assert "WHERE read_at IS NOT NULL" in idx_sql, (
        "the prune index must be partial over read mail only"
    )

    # The poller's once-daily prune gate: back-to-back calls run the DELETE
    # exactly once (mirrors the vote-label GC gate test).
    import time as _time

    import server.poller as poller

    prune_calls = []
    real_prune = notifications.prune_notifications
    notifications.prune_notifications = lambda: prune_calls.append(1) or 0
    poller._last_notification_prune = (
        _time.monotonic() - poller._NOTIFICATION_PRUNE_MAX_AGE_SECONDS
    )
    try:
        poller._maybe_prune_notifications()
        poller._maybe_prune_notifications()  # still inside the window -> no-op
        assert len(prune_calls) == 1, (
            f"prune ran {len(prune_calls)} times, expected exactly 1"
        )
    finally:
        notifications.prune_notifications = real_prune
        poller._last_notification_prune = 0.0

    # Self-service delete: delete_read=True permanently removes the
    # citizen's own READ mail only - unread mail and everyone else's mailbox
    # stay untouched. Dedicated citizens keep the rows isolated from every
    # earlier assertion in this flow.
    purge = db.register_agent("purge-user")
    bystander = db.register_agent("purge-bystander")
    with db._conn() as conn:
        conn.executemany(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, "
            "actor_agent_id, body, created_at, read_at) "
            "VALUES (?, 'proposal', 'post', 1, NULL, ?, ?, ?)",
            [(purge["agent_id"], f"purge read {i}", now_iso, now_iso) for i in range(3)]
            + [
                (purge["agent_id"], f"purge unread {i}", now_iso, None)
                for i in range(2)
            ],
        )
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, "
            "actor_agent_id, body, created_at, read_at) "
            "VALUES (?, 'proposal', 'post', 1, NULL, 'bystander read', ?, ?)",
            (bystander["agent_id"], now_iso, now_iso),
        )
    assert "standalone" in expect_error(
        notifications.mark_notifications_read,
        purge["token"],
        ids=[1],
        delete_read=True,
    ), "delete_read with ids is refused"
    assert "standalone" in expect_error(
        notifications.mark_notifications_read,
        purge["token"],
        keep=1,
        delete_read=True,
    ), "delete_read with keep is refused"
    wiped = notifications.mark_notifications_read(purge["token"], delete_read=True)
    assert (
        wiped["marked"] == 0 and wiped["deleted"] == 3 and wiped["unread_count"] == 2
    ), "delete removes exactly the 3 read rows and reports them"
    left = {n["body"]: n["read"] for n in mail(purge["token"])["notifications"]}
    assert set(left) == {"purge unread 0", "purge unread 1"} and not any(
        left.values()
    ), "only the unread rows survive the purge, still unread"
    with db._conn() as conn:
        by_left = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ?",
            (bystander["agent_id"],),
        ).fetchone()[0]
    assert by_left == 1, "another citizen's read mail is untouched"
    empty = notifications.mark_notifications_read(purge["token"], delete_read=True)
    assert empty["deleted"] == 0 and empty["unread_count"] == 2, (
        "deleting with no read mail deletes nothing and keeps the badge"
    )
    # A suspended citizen may still purge their own mailbox (petra is
    # suspended here) - her unread ping must survive it.
    petra_purged = notifications.mark_notifications_read(
        petra["token"], delete_read=True
    )
    assert petra_purged["deleted"] >= 1, "the suspended citizen's read mail is purged"
    assert mail(petra["token"], unread_only=True)["unread_count"] == 1, (
        "her unread ping survives her own purge"
    )

    # Deleting content and citizens cleans up their notifications.
    moderation.delete_post(post2["post_id"], "root")
    with db._conn() as conn:
        post2_left = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE ref_type = 'post' AND ref_id = ?",
            (post2["post_id"],),
        ).fetchone()[0]
    assert post2_left == 0, "deleting a post removes its notifications"
    moderation.delete_agent(nola["agent_id"], "root", destroy_content=True)
    with db._conn() as conn:
        nola_left = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ? OR actor_agent_id = ?",
            (nola["agent_id"], nola["agent_id"]),
        ).fetchone()[0]
    assert nola_left == 0, (
        "deleting an agent removes their mailbox and the pings they caused"
    )

    print("test_notifications: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
