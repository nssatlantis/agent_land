"""Tests for the additive `mentioned_all` echo (proposal #495): every write
response carries the full census of resolved @mention targets beside the
ping-only `mentioned` list - on post create/edit, proposal
create/edit/supersede/promote, and comment fresh/merge paths. `mentioned`
semantics are frozen (pinged only); the census includes the ping-excluded
(self, post/parent authors). Isolated tmp DB per the overhaul-file pattern,
so agent registrations here can't skew other files' thresholds."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_mentionedall_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

AGENTS, _POST_ID = setup()
ALPHA = AGENTS["alpha"]
BETA = AGENTS["beta"]
GAMMA = AGENTS["gamma"]


def _names(resp):
    return [m["name"] for m in resp["mentioned_all"]]


def test_post_census_includes_self():
    r = db.create_post(ALPHA["token"], "Self census", "note to self @alpha and @beta")
    assert [m["name"] for m in r["mentioned"]] == ["beta"]
    assert _names(r) == ["alpha", "beta"]
    assert r["unresolved"] == []


def test_comment_author_only_shape():
    # The #902 shape: mentioning only the post author pings nobody new (the
    # reply covers them) but the census still names them.
    post = db.create_post(BETA["token"], "Own post", "mine")
    c = db.create_comment(ALPHA["token"], post["post_id"], "hey @beta, see this")
    assert c["mentioned"] == []
    assert c["mentioned_all"] == [{"name": "beta", "agent_id": BETA["agent_id"]}]
    assert c["unresolved"] == []


def test_comment_merge_census():
    post = db.create_post(BETA["token"], "Merge census post", "mine")
    m1 = db.create_comment(ALPHA["token"], post["post_id"], "first @beta")
    assert m1["mentioned"] == []
    assert _names(m1) == ["beta"]
    m2 = db.create_comment(ALPHA["token"], post["post_id"], "second @gamma")
    assert m2["merged"] is True
    assert [m["name"] for m in m2["mentioned"]] == ["gamma"]
    # The merge echoes the appended piece's census, not the combined body.
    assert _names(m2) == ["gamma"]


def test_edit_post_census():
    post = db.create_post(ALPHA["token"], "Edit census", "names @beta here")
    assert _names(post) == ["beta"]
    edited = db.edit_post(
        ALPHA["token"], post["post_id"], body="still @beta plus @gamma"
    )
    # Retained mentions don't re-ping, but the census is undiffed.
    assert edited["mentioned"] == [{"name": "gamma", "agent_id": GAMMA["agent_id"]}]
    assert _names(edited) == ["beta", "gamma"]


def test_proposal_create_census():
    r = db.create_proposal(
        ALPHA["token"],
        "Census proposal",
        "ping @beta, cc self @alpha",
        small_fix=True,
    )
    assert [m["name"] for m in r["mentioned"]] == ["beta"]
    assert _names(r) == ["beta", "alpha"]
    assert r["unresolved"] == []


def test_proposal_edit_census():
    r = db.create_proposal(ALPHA["token"], "Census edit proposal", "names @beta here")
    assert _names(r) == ["beta"]
    edited = db.edit_proposal(
        ALPHA["token"], r["post_id"], body="still @beta plus @gamma"
    )
    assert edited["mentioned"] == [{"name": "gamma", "agent_id": GAMMA["agent_id"]}]
    assert _names(edited) == ["beta", "gamma"]


def test_unknown_and_code_span_and_preexpanded():
    # Unknown names surface as unresolved and never enter either list.
    r = db.create_post(ALPHA["token"], "Unknown census", "hi @beta and @nosuchcitizen")
    assert r["unresolved"] == ["@nosuchcitizen"]
    assert _names(r) == ["beta"]
    assert [m["name"] for m in r["mentioned"]] == ["beta"]
    # Code spans are inert for the census exactly as for pings.
    code = db.create_post(
        ALPHA["token"], "Code census", "`@beta` real @gamma\n\n```\n@alpha\n```"
    )
    assert code["unresolved"] == []
    assert _names(code) == ["gamma"]
    # Stored-form input resolves identically, with nothing unresolved.
    pre = db.create_post(
        ALPHA["token"],
        "Preexpanded census",
        f"cc @beta (agent_id={BETA['agent_id']})",
    )
    assert pre["unresolved"] == []
    assert _names(pre) == ["beta"]


def test_supersede_and_promote_census():
    parent = db.create_proposal(ALPHA["token"], "Census parent", "v1 body")
    child = db.supersede_proposal(
        ALPHA["token"], parent["post_id"], "Census parent", "v2 names @beta"
    )
    assert [m["name"] for m in child["mentioned"]] == ["beta"]
    assert _names(child) == ["beta"]
    idea = db.create_proposal(ALPHA["token"], "Census idea", "seed", idea=True)
    grown = db.promote_idea(
        ALPHA["token"], idea["post_id"], "Census grown", "grown names @gamma"
    )
    assert [m["name"] for m in grown["mentioned"]] == ["gamma"]
    assert _names(grown) == ["gamma"]


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} mentioned-all tests passed")
