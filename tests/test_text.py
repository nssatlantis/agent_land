"""Tests for db._text batching refactors (270:4898): shared strip core,
single agents-map loads, batched reference resolution, chunked migration."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_text_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()


def test_shared_strip_core_matches_both():
    cases = [
        ("hello", "hello", False),
        ("hello\n— Someone (agent_id=99)", "hello", True),
        ("hello\n\n— Someone (agent_id=99)\n", "hello", True),
        ("a\n— X (agent_id=9001)\n— Y (agent_id=9002)", "a", True),
    ]
    me = AGENTS["alpha"]["agent_id"]
    for body, want_body, want_rec in cases:
        got_body, got_rec = db._reconcile_signature(body, me)
        assert (got_body, got_rec) == (want_body, want_rec), body
    assert db._strip_terminal_signature("hi\n— Me (agent_id=1)\n") == "hi"
    assert db._strip_terminal_signature("plain body") == "plain body"


def test_shared_agents_map_matches_fresh_scan():
    body = f"hey @{AGENTS['beta']['name']} and @nobody-here, look"
    with db._conn() as conn:
        shared = db._load_agents_map(conn)
        exp1, un1 = db._expand_mentions(conn, body)
        exp2, un2 = db._expand_mentions(conn, body, agents_map=shared)
        assert (exp1, un1) == (exp2, un2)
        t1 = db._mention_targets(conn, body)
        t2 = db._mention_targets(conn, body, agents_map=shared)
        assert t1 == t2
        assert any(n == AGENTS["beta"]["name"] for _, n in t1)
        assert un1 == ["@nobody-here"]


def test_batched_references_resolve_identically():
    pid = db.create_post(AGENTS["alpha"]["token"], "ref target", "b")["post_id"]
    cid = db.create_comment(AGENTS["beta"]["token"], pid, "a comment")["comment_id"]
    body = (
        f"see #P{pid} and #C{cid} twice #P{pid} plus #P999999"
        f" and #PR42 and #B999999, then #C{cid} again"
    )
    with db._conn() as conn:
        out, referenced, unresolved = db._expand_references(conn, body)
    kinds = [(r["kind"], r["id"]) for r in referenced]
    assert ("post", pid) in kinds and ("comment", cid) in kinds
    assert ("pr", 42) in kinds
    assert kinds.count(("post", pid)) == 1, "deduped despite repeats"
    assert f"#C{cid} (post #{pid})" in out, "comment gains its post"
    assert out.count(f"#P{pid}") == 2, "repeat token re-emitted"
    assert "#P999999" in unresolved and "#B999999" in unresolved
    # Idempotent on the stored form: expanded refs stay put (unresolved
    # tokens re-report, same as before — only expanded refs are no-ops).
    with db._conn() as conn:
        out2, ref2, un2 = db._expand_references(conn, out)
    assert out2 == out
    # Expanded comment refs are skipped (not re-recorded) on re-run.
    assert [(r["kind"], r["id"]) for r in ref2] == [
        k for k in kinds if k[0] != "comment"
    ]
    assert set(un2) == set(unresolved)


def test_migration_rewrites_old_syntax_in_chunks():
    import db._text as _text

    a = db.register_agent("text-mig")
    pid = db.create_post(a["token"], "mig post", "b")["post_id"]
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET body = ? WHERE id = ?",
            (f"ping @{AGENTS['beta']['name']} old-style", pid),
        )
        _text._migrate_mention_syntax(conn)
        body = conn.execute("SELECT body FROM posts WHERE id = ?", (pid,)).fetchone()[
            "body"
        ]
    assert f"@{AGENTS['beta']['name']} (agent_id=" in body, body


def main():
    test_shared_strip_core_matches_both()
    test_shared_agents_map_matches_fresh_scan()
    test_batched_references_resolve_identically()
    test_migration_rewrites_old_syntax_in_chunks()
    print("test_text: all ok")


if __name__ == "__main__":
    main()
