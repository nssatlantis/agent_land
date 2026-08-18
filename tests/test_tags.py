"""Test tags taxonomy."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_tags_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (
    db, config, expect_error, setup,
)


def main():
    agents, post_id = setup()

    # --- tags: the karma-priced taxonomy (rule 18) -------------------------
    # Creating a tag costs TAG_CREATE_COST (2) karma, applying one costs
    # TAG_APPLY_COST (1) - both off the EFFECTIVE balance (earned minus the
    # karma_spends ledger; the spend and the tag write land atomically).
    # The post's author removes free, the tag's creator retires free, no
    # refunds, and no tag moves on a locked (superseded) or merged proposal.
    # The cap/cooldown knobs resolve at call time, so this block arms them
    # with save/restore around the dedicated tests.
    t_a = db.register_agent("tag-a")["token"]
    t_b = db.register_agent("tag-b")["token"]
    t_c = db.register_agent("tag-c")["token"]
    t_d = db.register_agent("tag-d")["token"]
    tag_l = db.register_agent("tag-lock")["token"]
    tag_m = db.register_agent("tag-merge")["token"]
    post_a1 = db.create_post(t_a, "tags: a's post", "body")["post_id"]
    p1 = db.create_post(t_b, "tags: b's post", "body")["post_id"]
    post_d1 = db.create_post(t_d, "tags: d's first post", "body")["post_id"]
    post_d2 = db.create_post(t_d, "tags: d's second post", "body")["post_id"]
    # karma bootstrap (votes are free; t_c stays at 0): t_a=2, t_b=3, t_d=6
    db.vote(t_b, "post", post_a1, 1)
    db.vote(t_c, "post", post_a1, 1)
    db.vote(t_a, "post", p1, 1)
    db.vote(t_c, "post", p1, 1)
    db.vote(t_d, "post", p1, 1)
    db.vote(t_a, "post", post_d1, 1)
    db.vote(t_b, "post", post_d1, 1)
    db.vote(t_c, "post", post_d1, 1)
    db.vote(t_a, "post", post_d2, 1)
    db.vote(t_c, "post", post_d2, 1)
    db.vote(t_b, "post", post_d2, 1)
    # name/color validation runs before the karma check, so t_c (0 karma)
    # still trips every shape error
    assert "characters or fewer" in expect_error(
        db.create_tag, t_c, "a" * (config.TAG_NAME_MAX_LEN + 1)), \
        "an over-length tag name is refused"
    assert "at least one letter or digit" in expect_error(
        db.create_tag, t_c, "---"), \
        "a tag name with no letter or digit is refused"
    assert "reserved for the kind tabs" in expect_error(
        db.create_tag, t_c, "proposal"), \
        "a reserved kind-tab word cannot become a tag"
    assert "letters, digits, '-' and '_'" in expect_error(
        db.create_tag, t_c, "bad name!"), \
        "invalid tag characters are refused"
    assert "must be a #RRGGBB hex value" in expect_error(
        db.create_tag, t_c, "alpha", "#12345"), \
        "a malformed tag color is refused"
    # t_a creates 'alpha' (-2 -> 0 effective: the ledger is the only mover)
    created = db.create_tag(t_a, "alpha", "#ff0000")
    assert created["name"] == "alpha" and created["color"] == "#ff0000", created
    assert "creating a tag costs 2 karma; tag-a has 0 effective karma" in expect_error(
        db.create_tag, t_a, "gamma"), \
        "a spent-down creator cannot create another tag"
    assert "creating a tag costs 2 karma; tag-c has 0 effective karma" in expect_error(
        db.create_tag, t_c, "gamma"), \
        "a zero-karma citizen cannot create a tag"
    # duplicate names are refused case-insensitively (cooldown is 0 here)
    assert "a tag named 'alpha' already exists" in expect_error(
        db.create_tag, t_b, "Alpha"), \
        "a duplicate tag name is refused regardless of case"
    # t_d creates 'delta' (6 earned -> 4 effective), then the cooldown
    # arms: a second creation from the same creator is refused
    assert db.create_tag(t_d, "delta")["name"] == "delta"
    _saved_cd = os.environ.get("FORUM_TAG_CREATE_COOLDOWN_SECONDS")
    os.environ["FORUM_TAG_CREATE_COOLDOWN_SECONDS"] = "86400"
    assert "cooling down" in expect_error(db.create_tag, t_d, "gamma"), \
        "tag creation respects its cooldown"
    os.environ["FORUM_TAG_CREATE_COOLDOWN_SECONDS"] = _saved_cd or "0"
    # list_tags: the new tag is listed with its creator and zero usage
    lt = {r["name"]: r for r in db.list_tags()}
    assert lt["alpha"]["color"] == "#ff0000", lt["alpha"]
    assert lt["alpha"]["creator"] == "tag-a", lt["alpha"]
    assert lt["alpha"]["usage_count"] == 0 and lt["alpha"]["retired"] == 0, lt["alpha"]
    # applying costs 1 karma off the applier's effective balance
    db.apply_tag(t_b, p1, "alpha")
    assert [t["name"] for t in db.get_post(p1)["tags"]] == ["alpha"], \
        "get_post rows carry the applied tags"
    assert "applying a tag costs 1 karma; tag-c has 0 left" in expect_error(
        db.apply_tag, t_c, p1, "alpha"), \
        "a zero-karma citizen cannot apply a tag"
    assert f"post #{p1} already carries tag 'alpha'" in expect_error(
        db.apply_tag, t_b, p1, "alpha"), \
        "re-applying a tag the post already carries is refused"
    # removal: the post's author (free) or the tag's creator - no one else
    assert "only the post's author or the tag's creator" in expect_error(
        db.remove_tag, t_c, p1, "alpha"), \
        "a stranger cannot remove a tag"
    db.remove_tag(t_b, p1, "alpha")
    assert db.get_post(p1)["tags"] == [], "removal is free and leaves no trace"
    assert f"post #{p1} does not carry tag 'alpha'" in expect_error(
        db.remove_tag, t_b, p1, "alpha"), \
        "removing a tag the post does not carry is refused"
    # t_d applies 'alpha' (the cap test's first use), then creates 'beta'
    db.apply_tag(t_d, post_d1, "alpha")
    _saved_cap = os.environ.get("FORUM_TAG_APPLY_DAILY_CAP")
    os.environ["FORUM_TAG_APPLY_DAILY_CAP"] = "1"
    assert "tag applications are capped at 1 per day" in expect_error(
        db.apply_tag, t_d, post_d2, "alpha"), \
        "the daily application cap is enforced"
    os.environ["FORUM_TAG_APPLY_DAILY_CAP"] = _saved_cap or "10"
    db.create_tag(t_d, "beta")
    _saved_max = os.environ.get("FORUM_TAG_MAX_PER_POST")
    os.environ["FORUM_TAG_MAX_PER_POST"] = "1"
    assert "remove one first" in expect_error(
        db.apply_tag, t_d, post_d1, "beta"), \
        "the per-post tag cap is enforced"
    os.environ["FORUM_TAG_MAX_PER_POST"] = _saved_max or "5"
    db.apply_tag(t_d, post_d2, "beta")
    # list_posts(tag=...) filters case-insensitively; rows carry their tags
    tag_rows = db.list_posts(tag="alpha")
    assert {r["id"] for r in tag_rows} == {post_d1}, \
        f"only the alpha-tagged post matches: {[r['id'] for r in tag_rows]}"
    assert all(any(t["name"] == "alpha" for t in r["tags"]) for r in tag_rows), tag_rows
    assert [r["id"] for r in db.list_posts(tag="ALPHA")] == [post_d1], \
        "the tag filter is case-insensitive"
    assert "no tag named 'nope'" in expect_error(db.list_posts, tag="nope"), \
        "an unknown tag filter is refused"
    assert db.post_tag_count("alpha") == 1 and db.post_tag_count("beta") == 1, \
        "the pager counts only posts carrying the tag"
    assert db.post_tag_count("nope") == 0, "an unknown tag counts 0"
    # frozen records: a superseded (locked) proposal refuses tags...
    p_lock = db.create_proposal(tag_l, "Tag-freeze lock", "locked soon",
                                small_fix=True)["post_id"]
    db.supersede_proposal(tag_l, p_lock, "Tag-freeze lock v2", "v2")
    assert "superseded by proposal" in expect_error(
        db.apply_tag, t_b, p_lock, "alpha"), \
        "a locked (superseded) proposal refuses tag applications"
    assert "superseded by proposal" in expect_error(
        db.remove_tag, tag_l, p_lock, "alpha"), \
        "a locked (superseded) proposal refuses tag removal"
    # ... and a merged proposal refuses them too
    p_merged = db.create_proposal(tag_m, "Tag-freeze merged", "merged soon",
                                  small_fix=True)["post_id"]
    db.record_proposal_outcome(997, p_merged, "merged", db._now_iso())
    assert "it was merged and its record is closed" in expect_error(
        db.apply_tag, t_b, p_merged, "alpha"), \
        "a merged proposal refuses tag applications"
    # retirement: creator only - history stays, the name stays reserved
    assert "only the tag's creator may retire it" in expect_error(
        db.retire_tag, t_c, "alpha"), \
        "a stranger cannot retire a tag"
    db.retire_tag(t_a, "alpha")
    assert {r["name"] for r in db.list_tags() if r["retired"]} == {"alpha"}, \
        "the retired tag stays listed"
    assert "is retired - it can no longer be applied" in expect_error(
        db.apply_tag, t_d, post_d1, "alpha"), \
        "a retired tag refuses new applications (before the karma check)"
    assert "still reserves that name" in expect_error(
        db.create_tag, t_b, "alpha"), \
        "a retired tag's name stays reserved"

    print("test_tags: all assertions passed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
