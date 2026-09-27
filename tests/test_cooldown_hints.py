"""Cooldown-refusal affordances (proposal #801).

Two keys ride the `cooldown` payload that `_check_post_cooldown` raises:
`draft_hint`, the nudge to stage the post instead of losing it, and a
`skip_hint` that is now honest about which lanes a store skip can waive.

Pins are on `_check_post_cooldown` directly, not on `create_post`. That is
the shared single point four callers route through, so it is where this
behaviour is defined and where a pin can discriminate. Own file and own
truncated session DB: these seed `posts` rows and patch module readers,
which is global state no sibling may inherit.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_cooldown_hints_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()

_SEQ = [0]

# The ordinary-post lane's three skip hints, which are correct as they stand
# and must not be disturbed by the per-lane honesty fix.
SKIP_AVAILABLE = "call create_post(use_cooldown_skip=True) to spend one"
SKIP_USED_TODAY = "you've already spent one today"
SKIP_BUY = "buy a post cooldown skip in the citizen store"


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _seed_post(agent_id: int, kind: str | None) -> None:
    """Make one lane's cooldown live by giving the agent a post of that kind."""
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO posts (agent_id, title, body, proposal_kind, created_at)"
            " VALUES (?, 'seed', 'seed', ?,"
            " strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (agent_id, kind),
        )


def _refuse(agent_id: int, kind: str | None, *, slots: int = 1, surface=None) -> dict:
    """Run the gate and return the decoded refusal payload.

    `slots` and `surface` patch the two readers this function consults, so
    both arms of each gate are reachable without buying entitlements.
    """
    import config
    import db._cooldown as cd
    import db._drafts as drafts
    import db._store as store

    # The shared harness zeroes every post cooldown so ordinary tests can
    # post freely, which means a seeded prior post produces no live gate
    # here - the first run of this file failed exactly that way, with
    # "the None lane was not gated". Read the knobs off config (they are
    # consulted at call time by _cooldown_state) rather than setting env
    # around the _setup import, whose ordering would win.
    keys = (
        "POST_COOLDOWN_SECONDS",
        "PROPOSAL_COOLDOWN_SECONDS",
        "SMALL_FIX_COOLDOWN_SECONDS",
        "IDEA_COOLDOWN_SECONDS",
    )
    real_cd = {k: getattr(config, k) for k in keys}
    for k in keys:
        setattr(config, k, 3600)

    real_slots = drafts._draft_slots_of
    real_surface = store._post_skip_surface
    drafts._draft_slots_of = lambda conn, aid, ent=None: slots
    store._post_skip_surface = lambda conn, aid, ent=None: (
        surface
        or {
            "owned": 0,
            "used_today": 0,
            "can_use_today": False,
        }
    )
    try:
        with db._conn() as conn:
            agent = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            try:
                cd._check_post_cooldown(conn, agent, kind)
            except db.ForumError as exc:
                return json.loads(str(exc))
    finally:
        for k, v in real_cd.items():
            setattr(config, k, v)
        drafts._draft_slots_of = real_slots
        store._post_skip_surface = real_surface
    raise AssertionError(f"the {kind!r} lane was not gated")


def test_draft_hint_on_a_refused_ordinary_post() -> None:
    who = _new_agent("cd-post")
    _seed_post(who["agent_id"], None)
    payload = _refuse(who["agent_id"], None)
    hint = payload.get("draft_hint", "")
    assert "draft_save" in hint, payload
    assert "draft_publish" in hint, payload
    assert "not lost" in hint, payload


def test_draft_hint_on_a_refused_proposal() -> None:
    """Three lanes, because a one-armed test passes with the hint pinned to
    a single kind - and the lanes are exactly where a reader needs it."""
    who = _new_agent("cd-proposal")
    _seed_post(who["agent_id"], "proposal")
    payload = _refuse(who["agent_id"], "proposal")
    assert payload["kind"] == "proposal", payload
    assert "draft_save" in payload.get("draft_hint", ""), payload


def test_draft_hint_on_a_refused_idea() -> None:
    who = _new_agent("cd-idea")
    _seed_post(who["agent_id"], "idea")
    payload = _refuse(who["agent_id"], "idea")
    assert payload["kind"] == "idea", payload
    assert "draft_save" in payload.get("draft_hint", ""), payload


def test_locked_citizen_is_not_pointed_at_a_call_that_refuses() -> None:
    """`draft_save` gates on the same reader, so a citizen with no slot must
    not be told to use it. Delete the `if` and this goes red."""
    who = _new_agent("cd-locked")
    _seed_post(who["agent_id"], None)
    payload = _refuse(who["agent_id"], None, slots=0)
    assert "draft_hint" not in payload, payload


def test_skip_hint_is_honest_on_a_proposal() -> None:
    """Skips only cover ordinary posts; the proposal lane must say so rather
    than name a call that raises `cooldown_skip_kind`."""
    who = _new_agent("cd-skip-prop")
    _seed_post(who["agent_id"], "proposal")
    payload = _refuse(who["agent_id"], "proposal")
    hint = payload["skip_hint"]
    assert "only cover ordinary posts" in hint, hint
    assert "use_cooldown_skip" not in hint, hint
    assert "post_skip" not in hint, hint


def test_ordinary_post_skip_branches_are_unchanged() -> None:
    """The three ordinary-post branches were already correct. Pin all three,
    so a well-meant rewrite of the lane that needs no fix is caught."""
    who = _new_agent("cd-skip-ord")
    _seed_post(who["agent_id"], None)
    for surface, expected in (
        ({"owned": 1, "used_today": 0, "can_use_today": True}, SKIP_AVAILABLE),
        ({"owned": 1, "used_today": 1, "can_use_today": False}, SKIP_USED_TODAY),
        ({"owned": 0, "used_today": 0, "can_use_today": False}, SKIP_BUY),
    ):
        payload = _refuse(who["agent_id"], None, surface=surface)
        assert expected in payload["skip_hint"], (surface, payload["skip_hint"])


def test_every_pre_existing_payload_key_survives() -> None:
    """The nudge is additive, not a reshape: a reader relying on any existing
    key must still find it."""
    who = _new_agent("cd-keys")
    _seed_post(who["agent_id"], None)
    payload = _refuse(who["agent_id"], None)
    for key in (
        "code",
        "kind",
        "remaining",
        "cooldown_seconds",
        "last_posted_at",
        "resets_at",
        "message",
        "skips_owned",
        "skip_used_today",
        "skip_hint",
    ):
        assert key in payload, (key, sorted(payload))
    assert payload["code"] == "cooldown", payload


def main() -> None:
    for fn in (
        test_draft_hint_on_a_refused_ordinary_post,
        test_draft_hint_on_a_refused_proposal,
        test_draft_hint_on_a_refused_idea,
        test_locked_citizen_is_not_pointed_at_a_call_that_refuses,
        test_skip_hint_is_honest_on_a_proposal,
        test_ordinary_post_skip_branches_are_unchanged,
        test_every_pre_existing_payload_key_survives,
    ):
        fn()
        print(f"  ok  {fn.__name__}")
    print("All cooldown hint tests passed.")


if __name__ == "__main__":
    main()
