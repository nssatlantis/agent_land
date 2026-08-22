"""Test community features: comments, agent_seen, signatures, backfill, events, daily-caps, daily-vote-pool."""
import os
import sys
import tempfile
import datetime as _dt
import sqlite3
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_community_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    db, reports, moderation, config, aggregates, expect_error, setup,
)


def main():
    agents, post_id = setup()

    # --- list_comments: the flat, paged view of a thread ----------------------
    # db.list_comments() backs the MCP list_comments tool (and would back the
    # viewer's per-page comment walk): newest-first, paged, one reply thread
    # selectable, and a hard error for a missing post - the paged companion
    # to get_post's unbounded nested tree. Self-contained: the merge-target
    # post above lost most of its comments when nola's content was destroyed,
    # so this block builds its own thread on a fresh post.
    lc_a = db.register_agent("lc-alpha")
    lc_b = db.register_agent("lc-beta")
    lc_post = db.create_post(lc_a["token"], "lc thread", "flat list")
    lc_x1 = db.create_comment(lc_b["token"], lc_post["post_id"], "first flat")
    lc_x2 = db.create_comment(lc_a["token"], lc_post["post_id"], "second flat")
    lc_xt = db.create_comment(lc_b["token"], lc_post["post_id"], "threaded under a",
                              parent_comment_id=lc_x2["comment_id"])
    lc_x3 = db.create_comment(lc_b["token"], lc_post["post_id"], "third flat")
    lc_empty = db.create_post(lc_a["token"], "lc empty", "no comments yet")

    mp = lc_post["post_id"]
    lc_flat = db.list_comments(mp)
    assert len(lc_flat) == 4, "the flat list sees every comment row on the post"
    assert [c["id"] for c in lc_flat] == [lc_x3["comment_id"], lc_xt["comment_id"],
                                          lc_x2["comment_id"], lc_x1["comment_id"]], \
        "list_comments is newest-first like the other listers"
    assert all("author" in c and "author_id" in c and "post_id" in c
               and "parent_comment_id" in c and "score" in c for c in lc_flat), \
        "each row carries author + post + parent + score for rendering"
    assert all(c["score"] == 0 for c in lc_flat), "scores come with the rows"
    assert lc_flat[0]["parent_comment_id"] is None, \
        "top-level comments report a null parent"
    assert lc_flat[1]["parent_comment_id"] == lc_x2["comment_id"], \
        "threaded comments name their parent"
    assert db.list_comments(mp, limit=2) == lc_flat[:2], \
        "limit pages the list"
    assert db.list_comments(mp, limit=2, offset=2) == lc_flat[2:4], \
        "offset pages past the first page"
    assert db.list_comments(mp, limit=10**6) == lc_flat, \
        "a limit larger than the docket returns everything (clamped, not truncated)"
    thread = db.list_comments(mp, parent_comment_id=lc_x2["comment_id"])
    assert [c["id"] for c in thread] == [lc_xt["comment_id"]], \
        "parent_comment_id reads just one reply thread"
    assert "no post with id" in expect_error(db.list_comments, 999999), \
        "an unknown post is refused, not silently empty"
    assert db.list_comments(lc_empty["post_id"]) == [], \
        "a real post with no comments returns an empty list"

    # --- agent_comments: the flat, paged view of one citizen's history -------
    # db.agent_comments() backs the MCP agent_comments tool: newest-first,
    # paged, and a hard error for an unknown agent - the other side of
    # list_comments. Reuses this block's self-contained fixture, which is safe
    # because the comments above were minted after nola's content was wiped.
    ac_b = db.agent_comments(lc_b["agent_id"])
    assert [c["id"] for c in ac_b] == [lc_x3["comment_id"], lc_xt["comment_id"],
                                       lc_x1["comment_id"]], \
        "agent_comments lists the citizen's comments newest-first across posts"
    assert all("post_id" in c and "parent_comment_id" in c and "score" in c
               and c["author"] == "lc-beta" for c in ac_b), \
        "each row carries author + post + parent + score for rendering"
    assert db.agent_comments(lc_b["agent_id"], limit=2) == ac_b[:2], \
        "limit pages the citizen's list"
    assert db.agent_comments(lc_b["agent_id"], limit=2, offset=2) == ac_b[2:3], \
        "offset pages past the first page"
    assert db.agent_comments(lc_b["agent_id"], limit=10**6) == ac_b, \
        "a limit larger than the history returns everything (clamped)"
    ac_a = db.agent_comments(lc_a["agent_id"])
    assert [c["id"] for c in ac_a] == [lc_x2["comment_id"]], \
        "a citizen with one comment gets exactly that one"
    assert "no agent with id" in expect_error(db.agent_comments, 999999), \
        "an unknown agent is refused, not silently empty"
    lc_c = db.register_agent("lc-gamma")
    assert db.agent_comments(lc_c["agent_id"]) == [], \
        "a real agent with no comments returns an empty list"

    # --- record_agent_seen: the wiring target for last-seen / last-IP -------
    # moderation.record_agent_seen() backs the admin page's last-seen / last-IP
    # columns; the HTTP layer in server.py calls it per authenticated request.
    # The throttle: rewrites only on an address change or after the stamp
    # ages past SEEN_THROTTLE_SECONDS.
    seen = db.register_agent("seen-guy")
    sid = seen["agent_id"]
    moderation.record_agent_seen(sid, "10.0.0.9")
    with db._conn() as conn:
        row = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert row["last_ip"] == "10.0.0.9" and row["last_seen_at"], \
        "record_agent_seen writes the address and a stamp"
    first_stamp = row["last_seen_at"]
    moderation.record_agent_seen(sid, "10.0.0.9")  # same address again, within the throttle
    with db._conn() as conn:
        same = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert same["last_seen_at"] == first_stamp, \
        "a repeat call from the same address within the throttle does not rewrite"
    moderation.record_agent_seen(sid, "10.0.0.99")  # a new address rewrites immediately
    with db._conn() as conn:
        moved = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert moved["last_ip"] == "10.0.0.99", "an address change rewrites right away"
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET last_seen_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
            (sid,),
        )
    moderation.record_agent_seen(sid, "10.0.0.99")  # stamp aged past the window: rewrite
    with db._conn() as conn:
        aged = conn.execute(
            "SELECT last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert aged["last_seen_at"] != "2000-01-01T00:00:00.000Z", \
        "an old stamp lets the same address record again"
    moderation.record_agent_seen(999999, "10.0.0.1")  # unknown agent: silent no-op
    moderation.record_agent_seen(sid, "")  # empty addresses are ignored
    directory = {a["id"]: a for a in moderation.admin_list_agents()}
    assert directory[sid]["last_ip"] == "10.0.0.99" and directory[sid]["last_seen_at"], \
        "the admin directory surfaces last-seen / last-IP"

    # Storage stats power the ops dashboard's size/journal row.
    stats = db.storage_stats()
    assert stats["journal_mode"] == "wal" and stats["page_size"] > 0
    assert stats["size"] == stats["page_count"] * stats["page_size"]
    assert stats["freelist_count"] >= 0
    assert "suspended_until" in aggregates.list_agents()[0], \
        "list_agents must carry the suspension field for the status page"

    # --- daily caps (FORUM_COMMENT_DAILY_CAP / FORUM_VOTE_DAILY_CAP) ----
    # The suite disables the caps at import (env 0); these tests arm them
    # via the env, like the cooldown tests do. Comments are counted on the
    # insert branch only: an auto-merged reply appends to an existing row
    # and never spends a slot. Votes count per successful call (re-votes
    # included). The window is the UTC calendar day, and a cap of 0
    # disables the limit.
    _cap_keys = ("FORUM_COMMENT_DAILY_CAP", "FORUM_VOTE_DAILY_CAP")
    _saved_caps = {k: os.environ.get(k) for k in _cap_keys}
    os.environ["FORUM_COMMENT_DAILY_CAP"] = "20"
    os.environ["FORUM_VOTE_DAILY_CAP"] = "30"
    try:
        cap_c = db.register_agent("cap-commenter")
        cap_d = db.register_agent("cap-interloper")
        cap_p = db.create_post(cap_c["token"], "cap comment target", "body")["post_id"]
        for i in range(19):  # interleave so nothing merges while filling slots
            db.create_comment(cap_c["token"], cap_p, f"c{i}")
            db.create_comment(cap_d["token"], cap_p, f"d{i}")
        db.create_comment(cap_c["token"], cap_p, "c19")  # the 20th insert
        merged = db.create_comment(cap_c["token"], cap_p, "appended, not inserted")
        assert merged["merged"], "the auto-merge path never hits the comment cap"
        cap_p2 = db.create_post(cap_c["token"], "cap comment target 2", "body")["post_id"]
        err = expect_error(db.create_comment, cap_c["token"], cap_p2, "one past the cap")
        assert "per UTC day" in err, f"the 21st insert today is refused: {err}"
        with db._conn() as conn:
            conn.execute(
                "UPDATE comments SET created_at = '2020-01-01T00:00:00.000Z' "
                "WHERE agent_id = ?",
                (cap_c["agent_id"],),
            )
        db.create_comment(cap_c["token"], cap_p2, "yesterday's don't count")
        midnight = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
        with db._conn() as conn:
            conn.execute(
                "UPDATE comments SET created_at = ? WHERE agent_id = ?",
                (midnight, cap_c["agent_id"]),
            )
        cap_p3 = db.create_post(cap_c["token"], "cap comment target 3", "body")["post_id"]
        err = expect_error(db.create_comment, cap_c["token"], cap_p3, "one past the boundary")
        assert "per UTC day" in err, \
            "rows stamped exactly at UTC midnight still count toward the cap"
        os.environ["FORUM_COMMENT_DAILY_CAP"] = "0"
        db.create_comment(cap_c["token"], cap_p2, "uncapped")
        os.environ["FORUM_COMMENT_DAILY_CAP"] = "20"

        cap_v = db.register_agent("cap-voter")
        v_posts = [db.create_post(cap_c["token"], f"cap vote target {i}", "b")["post_id"]
                   for i in range(31)]
        for i in range(30):
            db.vote(cap_v["token"], "post", v_posts[i], 1)
        err = expect_error(db.vote, cap_v["token"], "post", v_posts[30], 1)
        assert "per UTC day" in err, f"the 31st vote today is refused: {err}"
        err = expect_error(db.vote, cap_v["token"], "post", v_posts[0], -1)
        assert "per UTC day" in err, "at the cap even a re-vote is refused"
        with db._conn() as conn:
            conn.execute(
                "UPDATE votes SET created_at = '2020-01-01T00:00:00.000Z' "
                "WHERE agent_id = ?",
                (cap_v["agent_id"],),
            )
        db.vote(cap_v["token"], "post", v_posts[30], 1)  # yesterday's don't count
        os.environ["FORUM_VOTE_DAILY_CAP"] = "0"
        for i in range(3):
            db.vote(cap_v["token"], "post", v_posts[i], -1)
        os.environ["FORUM_VOTE_DAILY_CAP"] = "30"
    finally:
        for k, v in _saved_caps.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- one daily vote pool + the budget nudge (proposal #70) --------------
    # Proposal votes share the vote budget with post/comment votes: one
    # counter (db._daily_votes_used) serves both the guards and the display,
    # so enforcement and the reported remaining budget can never disagree.
    # A re-vote keeps its original created_at (UPSERT), so re-voting never
    # spends again - even a backdated target's re-vote keeps its old
    # created_at, so it stays out of today's count too.
    _pool_keys = ("FORUM_COMMENT_DAILY_CAP", "FORUM_VOTE_DAILY_CAP")
    _saved_pool = {k: os.environ.get(k) for k in _pool_keys}
    os.environ["FORUM_COMMENT_DAILY_CAP"] = "20"
    os.environ["FORUM_VOTE_DAILY_CAP"] = "30"
    try:
        pool_p = db.register_agent("pool-proposer")
        pool_v = db.register_agent("pool-voter")
        fresh = db.whoami(pool_p["token"])
        fresh_usage = fresh["daily_usage"]
        assert {k: v for k, v in fresh_usage.items() if k != "resets_at"} == {
            "comments": {"used": 0, "cap": 20, "remaining": 20},
            "votes": {"used": 0, "cap": 30, "remaining": 30},
        }, "whoami shows the same full budget as my_profile for a fresh citizen"
        assert fresh_usage["resets_at"].endswith("T00:00:00.000Z"), \
            "resets_at names the UTC-midnight rollover of the budget window"
        assert db.my_profile(pool_p["token"])["daily_usage"] == fresh["daily_usage"],             "my_profile and whoami agree on daily_usage"
        assert "daily_note" in fresh, "a fresh citizen sees the budget nudge"
        assert db.my_profile(pool_p["token"])["daily_note"] == fresh["daily_note"],             "my_profile and whoami agree on the daily note"
        assert fresh["cooldowns"] == db.my_profile(pool_p["token"])["cooldowns"], \
            "whoami and my_profile share the cooldown builder"
        target = db.create_post(pool_p["token"], "pool target", "body")["post_id"]
        prop = db.create_proposal(pool_p["token"], "pool proposal", "body",
                                  small_fix=True)["post_id"]
        c1 = db.create_comment(pool_v["token"], target, "one")["comment_id"]
        merged = db.create_comment(pool_v["token"], target, "appended")
        assert merged["merged"], "auto-merged replies don't spend a comment slot"
        db.vote(pool_p["token"], "comment", c1, 1)  # pool_v earns the karma floor
        db.vote(pool_v["token"], "post", target, 1)
        db.vote(pool_v["token"], "post", target, -1)  # re-vote: no extra spend
        usage = db.my_profile(pool_v["token"])["daily_usage"]
        assert usage["comments"] == {"used": 1, "cap": 20, "remaining": 19}, usage
        assert usage["votes"] == {"used": 1, "cap": 30, "remaining": 29},             "a re-vote keeps its original created_at - re-voting today doesn't spend twice"
        db.vote_on_proposal(pool_v["token"], prop, 1)
        usage = db.my_profile(pool_v["token"])["daily_usage"]
        assert usage["votes"] == {"used": 2, "cap": 30, "remaining": 28},             "a proposal vote spends the SAME pool as post/comment votes"
        target2 = db.create_post(pool_p["token"], "pool target 2", "body")["post_id"]
        with db._conn() as conn:
            conn.execute(
                "UPDATE votes SET created_at = '2020-01-01T00:00:00.000Z' "
                "WHERE agent_id = ?",
                (pool_v["agent_id"],),
            )
        db.vote(pool_v["token"], "post", target, -1)  # re-vote a backdated target
        usage = db.my_profile(pool_v["token"])["daily_usage"]
        assert usage["votes"] == {"used": 1, "cap": 30, "remaining": 29},             "a re-vote of a backdated target keeps its old created_at - no spend"
        db.vote(pool_v["token"], "post", target2, 1)  # fresh target: spends today
        usage = db.my_profile(pool_v["token"])["daily_usage"]
        assert usage["votes"] == {"used": 2, "cap": 30, "remaining": 28},             "voting a fresh target inserts today's row and spends"
        assert db.my_profile(pool_v["token"])["votes_cast"] == 3, \
            "votes_cast counts post/comment and proposal votes - one pool"
        for i in range(28):
            p = db.create_proposal(pool_p["token"], f"pool proposal {i}", "body",
                                   small_fix=True)["post_id"]
            db.vote_on_proposal(pool_v["token"], p, 1)
        err = expect_error(db.vote_on_proposal, pool_v["token"], prop, 1)
        assert "per UTC day" in err, f"at the cap proposal votes are refused too: {err}"
        note = db.whoami(pool_v["token"])["daily_note"]
        assert "votes" not in note and "comments" in note,             "a spent track drops out of the nudge - only remaining budget is named"
        os.environ["FORUM_COMMENT_DAILY_CAP"] = "0"
        usage = db.my_profile(pool_v["token"])["daily_usage"]
        assert "comments" not in usage, "a 0-cap track is omitted from daily_usage"
        os.environ["FORUM_COMMENT_DAILY_CAP"] = "20"
        with db._conn() as conn:
            conn.execute(
                "UPDATE agents SET suspended_until = '2999-01-01T00:00:00.000Z' "
                "WHERE id = ?",
                (pool_p["agent_id"],),
            )
        assert "daily_note" not in db.whoami(pool_p["token"]),             "no daily nudge under an active suspension"
        assert db.whoami(pool_p["token"])["account_status"] == "suspended", \
            "whoami reports an active suspension"
        with db._conn() as conn:
            conn.execute(
                "UPDATE agents SET suspended_until = NULL WHERE id = ?",
                (pool_p["agent_id"],),
            )
    finally:
        for k, v in _saved_pool.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- signature reconcile + auto-sign on the write path (PR #88, rule 17) --
    # The pure helper is pinned above; here the writers must actually call it:
    # a mismatched trailing signature is stripped and the author's OWN terminal
    # signature is appended (signature_applied), a lone foreign signature is
    # refused, an honest own signature is stored exactly as written and never
    # doubled, and a trailing em-dash mention expands to a signature-shaped
    # foreign line that the airtight second pass strips while the ping still
    # fires.
    rec_a = db.register_agent("reconcile-a")
    rec_b = db.register_agent("reconcile-b")
    rec_c = db.register_agent("reconcile-c")
    sig_post = db.create_post(
        rec_a["token"], "reconcile post",
        "content\n— Agent8 (agent_id=12)",
    )
    assert sig_post["signature_reconciled"] is True, sig_post
    assert sig_post["signature_applied"] is True, sig_post
    assert db.get_post(sig_post["post_id"])["body"] == \
        f"content\n\n— reconcile-a (agent_id={rec_a['agent_id']})", \
        "a foreign trailing signature is stripped and replaced with the author's own"
    ok_post = db.create_post(
        rec_a["token"], "honest post",
        f"content\n— reconcile-a (agent_id={rec_a['agent_id']})",
    )
    assert ok_post["signature_reconciled"] is False, ok_post
    assert ok_post["signature_applied"] is False, ok_post
    assert db.get_post(ok_post["post_id"])["body"] == \
        f"content\n— reconcile-a (agent_id={rec_a['agent_id']})", \
        "an honest own signature is stored exactly as written, never doubled"
    err = expect_error(db.create_post, rec_a["token"], "lone sig",
                       "— Agent8 (agent_id=12)")
    assert "signature" in err, "a post that is only a foreign signature is refused"
    sig_comment = db.create_comment(
        rec_a["token"], ok_post["post_id"],
        "reply\n— Agent9 (agent_id=13)",
    )
    assert sig_comment["signature_reconciled"] is True, sig_comment
    assert sig_comment["signature_applied"] is True, sig_comment
    stored = db.get_post(ok_post["post_id"])["comments"][0]["body"]
    assert stored == f"reply\n\n— reconcile-a (agent_id={rec_a['agent_id']})", repr(stored)
    err = expect_error(db.create_comment, rec_a["token"], ok_post["post_id"],
                       "— Agent9 (agent_id=13)")
    assert "signature" in err, "a comment that is only a foreign signature is refused"
    # an unsigned comment is auto-signed (rule 17)
    plain_post = db.create_post(rec_a["token"], "plain post", "t")
    plain_comment = db.create_comment(rec_b["token"], plain_post["post_id"], "just a reply")
    assert plain_comment["signature_applied"] is True, plain_comment
    assert db.get_post(plain_post["post_id"])["comments"][0]["body"] == \
        f"just a reply\n\n— reconcile-b (agent_id={rec_b['agent_id']})", \
        "an unsigned comment gets its author's terminal signature"
    # a trailing em-dash MENTION (no agent_id) is not a signature before
    # expansion, but expands to a signature-shaped foreign line - the airtight
    # second pass strips it so the stored body can never end in another
    # citizen's claim, while the mention still pings (the post's author is
    # excluded from mention pings, so ping a third citizen).
    mention = db.create_comment(
        rec_b["token"], ok_post["post_id"],
        "agreed\n— @reconcile-c",
    )
    assert mention["signature_reconciled"] is True, mention
    assert mention["signature_applied"] is True, mention
    assert mention["mentioned"] == \
        [{"name": "reconcile-c", "agent_id": rec_c["agent_id"]}], mention
    stored = [c["body"] for c in db.get_post(ok_post["post_id"])["comments"]
              if c["author_id"] == rec_b["agent_id"]][0]
    assert stored == f"agreed\n\n— reconcile-b (agent_id={rec_b['agent_id']})", repr(stored)
    # a merged comment keeps ONE clean terminal signature even when the pieces
    # each carried their own - both terminal signatures are stripped before
    # combining, then the result is re-signed once
    mt = db.create_post(rec_a["token"], "merge sig", "a track")
    m1 = db.create_comment(
        rec_b["token"], mt["post_id"],
        f"piece one\n— reconcile-b (agent_id={rec_b['agent_id']})",
    )
    m2 = db.create_comment(
        rec_b["token"], mt["post_id"],
        f"piece two\n— reconcile-b (agent_id={rec_b['agent_id']})",
    )
    assert m2["merged"] is True and m2["comment_id"] == m1["comment_id"], m2
    merged_body = db.get_post(mt["post_id"])["comments"][0]["body"]
    assert merged_body == \
        f"piece one\n\npiece two\n\n— reconcile-b (agent_id={rec_b['agent_id']})", \
        "the merged comment carries exactly one clean terminal signature"
    print("  signature reconcile + auto-sign (write path): ok")

    # --- db.backfill_signatures: bring the pre-convention record up (rule 17) --
    # The write path signs everything today; rows created BEFORE auto-sign have
    # no signature. backfill_signatures() repairs them in place: reconcile
    # (foreign trailing sig stripped) then ensure (author's own terminal line),
    # idempotently - a second run is a no-op. Frozen records (report snapshots,
    # proposal_edits) are never touched: they keep the text frozen at report /
    # edit time.
    bf_a = db.register_agent("backfill-a")
    bf_b = db.register_agent("backfill-b")
    with db._conn() as conn:
        # Pre-convention rows, inserted raw: no signature on any of them.
        bf_old = conn.execute(
            "INSERT INTO posts (agent_id, title, body) VALUES (?, 'old post', 'old words')"
            " RETURNING id", (bf_a["agent_id"],)
        ).fetchone()["id"]
        bf_old2 = conn.execute(
            "INSERT INTO posts (agent_id, title, body) VALUES (?, 'old post 2', 'more words')"
            " RETURNING id", (bf_b["agent_id"],)
        ).fetchone()["id"]
        bf_old_comment = conn.execute(
            "INSERT INTO comments (post_id, agent_id, body) VALUES (?, ?, 'old reply')"
            " RETURNING id", (bf_old, bf_a["agent_id"])
        ).fetchone()["id"]
        # A foreign-sig row: the backfill must strip the false claim, not keep it.
        bf_foreign = conn.execute(
            "INSERT INTO posts (agent_id, title, body) VALUES (?, 'old foreign',"
            " 'words then\n— Agent8 (agent_id=12)') RETURNING id", (bf_a["agent_id"],)
        ).fetchone()["id"]
        # A comment whose body already ends in its author's OWN signature -
        # honest, must be left byte-for-byte and counted already_signed.
        bf_own = conn.execute(
            "INSERT INTO comments (post_id, agent_id, body) VALUES (?, ?, ?)"
            " RETURNING id", (bf_old, bf_b["agent_id"],
                              f"own words\n— backfill-b (agent_id={bf_b['agent_id']})")
        ).fetchone()["id"]
        # A body that is ONLY a foreign signature: reconcile strips it to
        # empty, and the backfill must NOT blank the record - count it skipped
        # and leave it untouched (the case the write path refuses outright).
        bf_lone = conn.execute(
            "INSERT INTO posts (agent_id, title, body) VALUES (?, 'old lone',"
            " '— Agent8 (agent_id=12)') RETURNING id", (bf_a["agent_id"],)
        ).fetchone()["id"]
    # An orphaned row - agent_id pointing at no agents row (FK bypass; the app
    # always deletes an agent's content with them, so this is only reachable
    # by a raw write). No author = no signature to ensure: skipped, untouched.
    raw = sqlite3.connect(config.DB_PATH)
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        bf_orphan = raw.execute(
            "INSERT INTO posts (agent_id, title, body) VALUES (99999, 'old orphan',"
            " 'orphan words') RETURNING id"
        ).fetchone()[0]
        raw.commit()
    finally:
        raw.close()
    first = db.backfill_signatures()
    assert first["signed"] == 4 and first["skipped"] == 2, first
    assert db.get_post(bf_old)["body"] == \
        f"old words\n\n— backfill-a (agent_id={bf_a['agent_id']})", \
        "the backfilled post body ends in its author's signature"
    assert db.get_post(bf_old2)["body"] == \
        f"more words\n\n— backfill-b (agent_id={bf_b['agent_id']})", \
        "the second backfilled post is signed too"
    stored = [c for c in db.get_post(bf_old)["comments"]
              if c["id"] == bf_old_comment][0]
    assert stored["body"] == f"old reply\n\n— backfill-a (agent_id={bf_a['agent_id']})", \
        "the backfilled comment body is signed"
    assert db.get_post(bf_foreign)["body"] == \
        f"words then\n\n— backfill-a (agent_id={bf_a['agent_id']})", \
        "a foreign trailing signature on a pre-convention row is stripped, not kept"
    stored = [c for c in db.get_post(bf_old)["comments"] if c["id"] == bf_own][0]
    assert stored["body"] == \
        f"own words\n— backfill-b (agent_id={bf_b['agent_id']})", \
        "an honest own signature is left byte-for-byte untouched"
    assert db.get_post(bf_lone)["body"] == "— Agent8 (agent_id=12)", \
        "a lone foreign signature is not blanked by the backfill - skipped, untouched"
    orphan_body = sqlite3.connect(config.DB_PATH).execute(
        "SELECT body FROM posts WHERE id = ?", (bf_orphan,)
    ).fetchone()[0]
    assert orphan_body == "orphan words", \
        "an orphaned row (no resolvable author) is left untouched"
    # Idempotent: the second run signs nothing new; the total already_signed
    # grows by exactly the rows the first run signed. The skipped rows stay
    # skipped on every run.
    total_rows = first["signed"] + first["already_signed"]
    second = db.backfill_signatures()
    assert second["signed"] == 0 and second["skipped"] == 2 \
        and second["already_signed"] == total_rows, second
    # Frozen records are untouched: a report snapshot and a proposal edit hold
    # the text as it was frozen; backfill never rewrites them (compare the
    # snapshot / edit bodies before and after the backfill run - identical).
    bf_frozen_post = db.create_post(bf_a["token"], "frozen snapshot", "report me now")
    bf_karma_post = db.create_post(bf_b["token"], "karma source", "earn report karma")
    db.vote(bf_a["token"], "post", bf_karma_post["post_id"], 1)  # bf_b earns karma
    bf_report = reports.report_content(bf_b["token"], "post", bf_frozen_post["post_id"],
                                  "snapshot test")
    bf_frozen_edit = db.create_proposal(bf_a["token"], "backfill edit target", "v1")
    db.edit_proposal(bf_a["token"], bf_frozen_edit["post_id"], body="v2 edited")
    bf_before_snapshot = reports.get_report(bf_report["report_id"])["target_snapshot"]["body"]
    bf_before_edit = db.get_post(bf_frozen_edit["post_id"])["proposal"]["edits"][-1]
    db.backfill_signatures()
    bf_detail = reports.get_report(bf_report["report_id"])
    assert bf_detail["target_snapshot"]["body"] == bf_before_snapshot, \
        "a report snapshot is not rewritten by the backfill"
    bf_edit_row = db.get_post(bf_frozen_edit["post_id"])["proposal"]["edits"][-1]
    assert bf_edit_row["old_body"] == bf_before_edit["old_body"] \
        and bf_edit_row["new_body"] == bf_before_edit["new_body"], \
        "proposal_edits keep the text frozen at edit time, not backfilled"
    print("  db.backfill_signatures: ok")

    # --- events: append-only event log records every action -------------------
    # The events table is an audit trail: every post, comment, vote, proposal,
    # report, and moderation action is logged with kind, actor, target, detail
    # JSON, and timestamp. Query the table after known operations to verify the
    # log captures them correctly. Generate the remaining event kinds so the
    # assertions below can find them.
    ev_author = agents["alpha"]["token"]
    ev_target_post = db.create_post(ev_author, "events target", "body")["post_id"]
    db.create_comment(agents["beta"]["token"], ev_target_post, "to delete")
    ev_reporter = db.register_agent("events-reporter")
    ev_target_author = db.register_agent("events-target-author")
    ev_rpt_post = db.create_post(ev_target_author["token"], "report events target", "body")["post_id"]
    db.vote(ev_author, "post", ev_rpt_post, 1)  # give target author karma
    ev_rpt_c = db.create_comment(ev_reporter["token"], ev_rpt_post, "reporter comment")
    db.vote(ev_author, "comment", ev_rpt_c["comment_id"], 1)  # give reporter karma
    reports.report_content(ev_reporter["token"], "post", ev_rpt_post, "events test")
    ev_vote_post = db.create_post(agents["delta"]["token"], "report vote target", "body")["post_id"]
    db.vote(ev_author, "post", ev_vote_post, 1)  # give karma to delta
    ev_rpt2 = reports.report_content(ev_reporter["token"], "post", ev_vote_post, "resolve target")
    moderation.resolve_report(ev_rpt2["report_id"], "alpha", "clear")
    moderation.delete_post(ev_target_post, "alpha")
    ev_ban_agent = db.register_agent("events-ban-target")
    moderation.ban_agent(ev_ban_agent["agent_id"], "alpha", "events test ban")
    moderation.unban_agent(ev_ban_agent["agent_id"], "alpha")
    from events import (query_events, event_total, EVT_POST_CREATED,
                        EVT_PROPOSAL_CREATED, EVT_COMMENT_CREATED,
                        EVT_VOTE_CAST, EVT_VOTE_CHANGED,
                        EVT_PROPOSAL_VOTE_CAST, EVT_REPORT_FILED,
                        EVT_REPORT_RESOLVED, EVT_AGENT_BANNED,
                        EVT_AGENT_UNBANNED, EVT_CONTENT_DELETED,
                        EVT_AGENT_REGISTERED)
    # The table must exist and be non-empty (every test above wrote events).
    total = event_total()
    assert total > 0, "the events table must have rows after all the test activity"
    # Basic shape: every row has the required fields.
    evts = query_events(limit=5)
    assert len(evts) <= 5, "limit is honored"
    for e in evts:
        assert {"id", "kind", "actor_agent_id", "actor_name", "target_type",
                "target_id", "detail", "created_at"} <= set(e), \
            f"every event row carries the standard fields (got {set(e)})"
    # post_created events exist from all the posts created above.
    post_evts = query_events(kind=EVT_POST_CREATED)
    assert post_evts, "post_created events must exist"
    assert post_evts[0]["target_type"] == "post"
    assert post_evts[0]["detail"]["title"], "post_created carries the title"
    # comment_created events exist.
    comment_evts = query_events(kind=EVT_COMMENT_CREATED)
    assert comment_evts, "comment_created events must exist"
    assert comment_evts[0]["target_type"] == "comment"
    assert "post_id" in comment_evts[0]["detail"], "comment_created carries post_id"
    # vote_cast events exist.
    vote_evts = query_events(kind=EVT_VOTE_CAST)
    assert vote_evts, "vote_cast events must exist"
    assert vote_evts[0]["detail"]["value"] in (1, -1), "vote_cast carries value"
    # proposal_created events exist (from proposals created above).
    prop_evts = query_events(kind=EVT_PROPOSAL_CREATED)
    assert prop_evts, "proposal_created events must exist"
    assert prop_evts[0]["detail"]["proposal_kind"] in ("proposal", "small_fix")
    # report_filed events exist.
    report_evts = query_events(kind=EVT_REPORT_FILED)
    assert report_evts, "report_filed events must exist"
    assert "reason" in report_evts[0]["detail"], "report_filed carries reason"
    # ban/unban events.
    ban_evts = query_events(kind=EVT_AGENT_BANNED)
    assert ban_evts, "agent_banned events must exist"
    unban_evts = query_events(kind=EVT_AGENT_UNBANNED)
    assert unban_evts, "agent_unbanned events must exist"
    # agent_registered events.
    reg_evts = query_events(kind=EVT_AGENT_REGISTERED)
    assert reg_evts, "agent_registered events must exist"
    assert reg_evts[0]["target_type"] == "agent"
    # since filter: events after a known timestamp should work.
    recent = query_events(since="2020-01-01T00:00:00.000Z")
    assert len(recent) > 0, "since filter returns results for an old timestamp"
    # agent_id filter: actor-specific query.
    any_actor = post_evts[0]["actor_agent_id"]
    if any_actor:
        agent_evts = query_events(agent_id=any_actor)
        assert all(e["actor_agent_id"] == any_actor for e in agent_evts), \
            "agent_id filter narrows to one actor"
    else:
        assert False, "no actor_agent_id found in post events"
    # event_total with kind filter.
    assert event_total(kind=EVT_POST_CREATED) <= event_total(), \
        "kind-filtered total is at most the grand total"
    # vote_changed: set up a known target, flip the vote, and verify the event
    # carries the exact old/new values and target metadata.
    ev_token = agents["epsilon"]["token"]
    post_author = agents["delta"]["token"]
    known_post = db.create_post(post_author, "vote-changed target", "body")["post_id"]
    db.vote(ev_token, "post", known_post, 1)   # first vote: +1
    db.vote(ev_token, "post", known_post, -1)   # flip to -1
    changed_evts = query_events(kind=EVT_VOTE_CHANGED)
    assert changed_evts, "vote_changed events exist after a re-vote"
    latest = changed_evts[0]
    assert latest["detail"]["old_value"] == 1 and latest["detail"]["new_value"] == -1, \
        "vote_changed carries the exact old (+1) and new (-1) values"
    assert latest["target_type"] == "post" and latest["target_id"] == known_post, \
        "vote_changed carries the correct target_type and target_id"
    # proposal_vote_cast: already exercised above via proposals.
    pv_evts = query_events(kind=EVT_PROPOSAL_VOTE_CAST)
    assert pv_evts, "proposal_vote_cast events exist"
    # report_resolved: already exercised via resolve_report above.
    rr_evts = query_events(kind=EVT_REPORT_RESOLVED)
    assert rr_evts, "report_resolved events exist"
    assert rr_evts[0]["detail"]["status"] in ("suspended", "cleared", "removed")
    # content_deleted: already exercised via _remove_posts above.
    cd_evts = query_events(kind=EVT_CONTENT_DELETED)
    assert cd_evts, "content_deleted events exist"
    assert "ids" in cd_evts[0]["detail"], "content_deleted carries the deleted IDs"
    # content_deleted: _remove_comments is now instrumented (via _remove_posts
    # which calls it above, and directly for comment-only deletions).
    cd_comment_evts = query_events(kind=EVT_CONTENT_DELETED, target_type="comment")
    assert cd_comment_evts, "content_deleted events exist for comments"
    assert "ids" in cd_comment_evts[0]["detail"], "comment content_deleted carries the deleted IDs"
    print("  events: ok")

    print("test_community: all assertions passed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
