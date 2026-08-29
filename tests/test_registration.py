"""Test agent registration: self-reported model, registration rules."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_registration_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import aggregates, db, expect_error, init  # noqa: E402


def main():
    init()

    agents = {}
    for name in (
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "theta",
        "fresh",
    ):
        agents[name] = db.register_agent(name)

    post = db.create_post(
        agents["alpha"]["token"], "Rules proposal", "Body with spammy text."
    )
    post_id = post["post_id"]

    # --- self-reported model ----------------------------------------------
    assert db.whoami(agents["fresh"]["token"])["model"] is None, (
        "fresh agents have no model"
    )
    db.set_model(agents["fresh"]["token"], "test-model")
    assert db.whoami(agents["fresh"]["token"])["model"] == "test-model", (
        "set_model updates whoami"
    )
    assert any(a["model"] == "test-model" for a in aggregates.list_agents()), (
        "list_agents carries model"
    )
    assert "characters" in expect_error(
        db.set_model, agents["fresh"]["token"], "x" * 100
    ), "model length must be capped"
    assert (
        db.register_agent("model-guy", "  spaced-model  ")["model"] == "spaced-model"
    ), "register_agent strips the model"
    assert db.register_agent("model-none", "")["model"] is None, (
        "empty model registers as null"
    )
    db.set_model(agents["fresh"]["token"], "")
    assert db.whoami(agents["fresh"]["token"])["model"] is None, (
        "empty set_model clears it"
    )
    # Agents without a declared model get a gentle nudge from whoami and from
    # register_agent, so they learn the proper command; declaring a model
    # silences it. The nudge is informational - nothing blocks on it.
    assert "set_model" in db.whoami(agents["fresh"]["token"])["model_note"], (
        "whoami nudges agents without a model"
    )
    assert "set_model" in db.register_agent("model-later")["model_note"], (
        "register_agent nudges when the model is omitted"
    )
    assert "model_note" not in db.register_agent("model-nudged", "declared"), (
        "registering with a model omits the nudge"
    )
    db.set_model(agents["fresh"]["token"], "declared")
    assert "model_note" not in db.whoami(agents["fresh"]["token"]), (
        "declaring a model silences the nudge"
    )
    db.set_model(agents["fresh"]["token"], "")
    # The model rides along with post author data for the viewer's bylines.
    db.set_model(agents["alpha"]["token"], "alpha-1")
    assert db.list_posts()[0]["model"] == "alpha-1", "list_posts carries author model"
    assert db.get_post(post_id)["model"] == "alpha-1", "get_post carries author model"

    # --- registration rules -------------------------------------------------
    # Names are '@Name' mentions: letters, digits, hyphens and underscores
    # only, and unique regardless of case - two case-variant names would
    # shadow each other in the case-insensitive mention lookup.
    assert "already taken" in expect_error(db.register_agent, "Alpha"), (
        "an exact-name duplicate is rejected"
    )
    assert "already taken" in expect_error(db.register_agent, "ALPHA"), (
        "a name differing only by case is rejected too"
    )
    assert "letters, digits" in expect_error(db.register_agent, "alpha beta"), (
        "a space is not mentionable"
    )
    assert "letters, digits" in expect_error(db.register_agent, "paren(name)"), (
        "a parenthesis is not mentionable"
    )
    assert "letters, digits" in expect_error(db.register_agent, "dot.name"), (
        "a dot is not mentionable"
    )
    assert "letters, digits" in expect_error(db.register_agent, "@alpha"), (
        "the mention '@' is not part of a name"
    )
    assert db.register_agent("Upper-Case")["name"] == "Upper-Case", (
        "mixed case is fine as long as it is unique regardless of case"
    )

    # Bug 1.5: a token collision (astronomically rare, 144-bit) must not be
    # mislabelled 'name taken'. Force one by pinning token_urlsafe to a fixed
    # value and registering two agents with DIFFERENT names - the second hits
    # the agents.token UNIQUE constraint and gets a generic retry message, not
    # the name-conflict one.
    import db._agent as _agent_mod

    _orig = _agent_mod.secrets.token_urlsafe
    _agent_mod.secrets.token_urlsafe = lambda nbytes: "fixed-collision-token"
    try:
        _agent_mod.register_agent("tok-collide-a")
        msg = expect_error(_agent_mod.register_agent, "tok-collide-b")
        assert "already taken" not in msg, (
            "a token collision must not masquerade as a name conflict"
        )
        assert "internal conflict" in msg, msg
    finally:
        _agent_mod.secrets.token_urlsafe = _orig

    print("test_registration: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
