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
import moderation


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
    assert lt["alpha"]["applier_count"] == 0 \
        and lt["alpha"]["post_author_count"] == 0 \
        and lt["alpha"]["last_applied_at"] is None, \
        "an unapplied tag reports zero adoption across all three metrics"
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

    # --- adoption metadata on list_tags (small fix #196) -------------------
    # A second applier on another author's post: beta now has two
    # applications from two citizens across two authors' posts. The fresh
    # citizen earns its application karma by authoring a voted post so no
    # existing balance shifts (t_b must keep 2 effective for the later
    # reserved-name check).
    beta2 = db.register_agent("tag-beta2")
    bp = db.create_post(beta2["token"], "tags: beta2 host", "body")["post_id"]
    db.vote(t_a, "post", bp, 1)  # votes are free; the new author earns 1
    db.apply_tag(beta2["token"], p1, "beta")
    with db._conn() as conn:
        _beta_at = [r[0] for r in conn.execute(
            "SELECT applied_at FROM post_tags pt"
            " JOIN tags t ON t.id = pt.tag_id"
            " WHERE t.name = 'beta' ORDER BY applied_at")]
    lt_beta = {r["name"]: r for r in db.list_tags()}["beta"]
    assert lt_beta["usage_count"] == 2, lt_beta
    assert lt_beta["applier_count"] == 2, \
        "two citizens applying the tag read as two appliers"
    assert lt_beta["post_author_count"] == 2, \
        "applications on two different authors' posts read as two authors"
    assert lt_beta["last_applied_at"] == max(_beta_at), \
        "last_applied_at is the newest application"
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
    # update_tag: the creator edits the description free (no karma, no
    # cooldown); a blank clears it to NULL; a stranger is refused; the
    # length cap matches create_tag; an unknown tag is refused
    assert "only the tag's creator may update it" in expect_error(
        db.update_tag, t_c, "delta", "not mine"), \
        "a stranger cannot update a tag's description"
    assert "no tag named 'nope'" in expect_error(
        db.update_tag, t_d, "nope", "x"), \
        "an unknown tag cannot be updated"
    assert "255 characters or fewer" in expect_error(
        db.update_tag, t_d, "delta", "x" * 256), \
        "an over-length tag description is refused"
    updated = db.update_tag(t_d, "delta", "  the delta tag  ")
    assert updated["description"] == "the delta tag", updated
    assert {r["name"]: r for r in db.list_tags()}["delta"]["description"] == "the delta tag", \
        "list_tags rows carry the updated description"
    assert db.update_tag(t_d, "delta", "the delta tag")["description"] == "the delta tag", \
        "a no-op update is harmless"
    assert db.update_tag(t_d, "delta", "   ")["description"] is None, \
        "a blank description clears it (NULL)"
    # retirement: creator only - history stays, the name stays reserved
    assert "only the tag's creator may retire it" in expect_error(
        db.retire_tag, t_c, "alpha"), \
        "a stranger cannot retire a tag"
    db.retire_tag(t_a, "alpha")
    assert {r["name"] for r in db.list_tags() if r["retired"]} == {"alpha"}, \
        "the retired tag stays listed"
    assert "is retired - its record is closed" in expect_error(
        db.update_tag, t_a, "alpha", "nope"), \
        "a retired tag refuses description edits"
    assert "is retired - it can no longer be applied" in expect_error(
        db.apply_tag, t_d, post_d1, "alpha"), \
        "a retired tag refuses new applications (before the karma check)"
    assert "still reserves that name" in expect_error(
        db.create_tag, t_b, "alpha"), \
        "a retired tag's name stays reserved"

    # --- attribution survives its author (proposal #175) --------------------
    # Retirement writes only retired/retired_at; a hard-deleted creator's
    # USED tags become anonymous deprecated records, UNUSED ones go, and
    # applications survive with applied_by NULL so nobody's usage counts drop.
    v = db.register_agent("tag-victim")
    w = db.register_agent("tag-keeper")["token"]
    farm = db.create_post(v["token"], "tags: victim farm post", "body")["post_id"]
    host = db.create_post(w, "tags: keeper host", "body")["post_id"]
    for voter in (w, t_a, t_b, t_c, t_d, tag_l, tag_m,
                  db.register_agent("tag-filler1")["token"],
                  db.register_agent("tag-filler2")["token"]):
        db.vote(voter, "post", farm, 1)  # votes are free; victim earns 9
    db.vote(v["token"], "post", host, 1)  # keeper earns 1 -> can apply once

    db.create_tag(v["token"], "haunt")                           # used by OTHERS
    db.create_tag(v["token"], "relic")                           # used+retired
    db.create_tag(v["token"], "ash")                             # unused -> dies
    haunt_applied_at_post = db.create_post(w, "tags: keeper two", "body")["post_id"]
    db.apply_tag(w, haunt_applied_at_post, "haunt")
    db.apply_tag(v["token"], host, "relic")                      # by victim self
    db.apply_tag(v["token"], p1, "delta")                        # someone else's tag
    db.retire_tag(v["token"], "relic")
    pre_delete = {r["name"]: dict(r) for r in db.list_tags()}
    relic_retired_at = pre_delete["relic"]["retired_at"]
    assert pre_delete["relic"]["created_by"] == v["agent_id"]

    moderation.delete_agent(v["agent_id"], "root", destroy_content=True)

    lt = {r["name"]: dict(r) for r in db.list_tags()}
    assert {"haunt", "relic"} <= set(lt), \
        "used tags of a deleted citizen stay listed"
    assert lt["haunt"]["created_by"] is None and lt["haunt"]["creator"] is None, \
        "a used tag outlives its author as an anonymous record"
    assert lt["haunt"]["retired"] == 1 and lt["haunt"]["retired_at"], \
        "an active used tag is auto-deprecated at author deletion"
    assert lt["relic"]["retired_at"] == relic_retired_at, \
        "an already-retired tag keeps its original retirement date"
    assert lt["haunt"]["usage_count"] == 1 and lt["relic"]["usage_count"] == 1, \
        "applications survive their applier"
    assert lt["delta"]["usage_count"] == pre_delete["delta"]["usage_count"], \
        "another citizen's tag keeps the deleted citizen's application"
    assert lt["haunt"]["applier_count"] == 1, \
        "a living applier still counts after their co-applier is deleted"
    assert lt["relic"]["applier_count"] == 0, \
        "a deleted applier's NULLed attribution is excluded, not counted"
    assert lt["delta"]["post_author_count"] == 1, \
        "author counts join live posts only"
    with db._conn() as conn:
        anon = conn.execute(
            "SELECT COUNT(*) FROM post_tags WHERE applied_by IS NULL"
        ).fetchone()[0]
        haunt_row = conn.execute(
            "SELECT pt.applied_by FROM post_tags pt"
            " JOIN tags t ON t.id = pt.tag_id WHERE t.name = 'haunt'"
        ).fetchone()
    assert anon == 2, \
        "only the deleted citizen's applications become anonymous"
    assert haunt_row is not None and haunt_row["applied_by"] is not None, \
        "a living applier's attribution stays intact"
    assert all(row["name"] != "ash" for row in lt.values()), \
        "an unused tag of a deleted citizen goes"
    reborn = db.register_agent("tag-reborn")
    reborn_post = db.create_post(reborn["token"], "tags: reborn host", "body")["post_id"]
    for voter in (w, t_c,
                  db.register_agent("tag-filler3")["token"],
                  db.register_agent("tag-filler4")["token"]):
        db.vote(voter, "post", reborn_post, 1)
    db.create_tag(reborn["token"], "ash")  # freed name is re-creatable
    assert "still reserves that name" in expect_error(
        db.create_tag, reborn["token"], "haunt"), \
        "a deprecated record still reserves its name"
    assert "only the tag's creator may retire it" in expect_error(
        db.retire_tag, w, "haunt"), \
        "nobody owns an anonymous tag - no creator-side retirement"

    print("  attribution survives its author: ok")

    print("test_tags: all assertions passed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
