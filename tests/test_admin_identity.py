"""Admin actor identity (proposal #797): the panel login and the recorded
actor are two different namespaces, and ADMIN_AGENT_NAME bridges them.

Pins the fix rather than the fixture: pin 1 and pin 2 both go RED if the
knob is deleted, which is the mutation arm. Own file and own truncated
session DB on purpose - these pins mutate `agents` rows and env, which is
global state no sibling may inherit.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_admin_identity_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ.pop("ADMIN_AGENT_NAME", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()

_SEQ = [0]

# A login that is deliberately NOT a registered citizen - the shape that
# produced the operator's "unknown admin." report.
PANEL_LOGIN = "operator-panel-login"


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _resolve(login: str, knob: str | None) -> dict:
    """Resolve through the real engine helper under a knob value."""
    from db._guilds import _admin_agent

    prev = os.environ.get("ADMIN_AGENT_NAME")
    try:
        if knob is None:
            os.environ.pop("ADMIN_AGENT_NAME", None)
        else:
            os.environ["ADMIN_AGENT_NAME"] = knob
        with db._conn() as conn:
            return _admin_agent(conn, login)
    finally:
        if prev is None:
            os.environ.pop("ADMIN_AGENT_NAME", None)
        else:
            os.environ["ADMIN_AGENT_NAME"] = prev


def test_knob_resolves_actor_when_login_is_not_a_citizen() -> None:
    """THE pin. Without the knob this raises "unknown admin." - the exact
    operator-visible failure. With it, the decision resolves."""
    actor = _new_agent("admin-actor")
    got = _resolve(PANEL_LOGIN, actor["name"])
    assert got["id"] == actor["agent_id"], got
    assert got["name"] == actor["name"], got


def test_knob_wins_over_a_login_naming_a_different_citizen() -> None:
    """Attribution: the recorded actor is the knob's citizen, not the
    login's. Guards against a fix that resolved the login and ignored
    the knob, which would also make pin 1 pass."""
    login_agent = _new_agent("admin-login")
    knob_agent = _new_agent("admin-knob")
    got = _resolve(login_agent["name"], knob_agent["name"])
    assert got["id"] == knob_agent["agent_id"], got
    assert got["id"] != login_agent["agent_id"], got


def test_default_holds_when_knob_is_unset() -> None:
    """A deployment whose ADMIN_USER already names a citizen is
    unaffected: the knob defaults to the login."""
    who = _new_agent("admin-default")
    got = _resolve(who["name"], None)
    assert got["id"] == who["agent_id"], got


def test_refusal_keeps_prefix_and_names_the_offending_value() -> None:
    """Substring-compatible with the three existing engine pins, and now
    carries the diagnosis and the remedy."""
    try:
        _resolve(PANEL_LOGIN, "not-a-citizen-xyz")
    except db.ForumError as exc:
        msg = str(exc)
    else:
        raise AssertionError("a non-agent knob resolved to an actor")
    assert msg.startswith("unknown admin"), msg
    assert "not-a-citizen-xyz" in msg, msg
    assert "ADMIN_AGENT_NAME" in msg, msg


def test_suspended_actor_still_refuses_through_the_knob() -> None:
    """The bridge maps identity; it does not bypass the refusals."""
    who = _new_agent("admin-susp")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2999-01-01T00:00:00+00:00", who["agent_id"]),
        )
    try:
        _resolve(PANEL_LOGIN, who["name"])
    except db.ForumError as exc:
        assert "that admin is suspended" in str(exc), exc
    else:
        raise AssertionError("a suspended actor resolved through the knob")


def test_banned_actor_still_refuses_through_the_knob() -> None:
    """Same for a ban, which is the stricter of the two."""
    who = _new_agent("admin-ban")
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE agents SET banned = 1 WHERE id = ?", (who["agent_id"],))
    try:
        _resolve(PANEL_LOGIN, who["name"])
    except db.ForumError as exc:
        assert "that admin is banned" in str(exc), exc
    else:
        raise AssertionError("a banned actor resolved through the knob")


def main() -> None:
    for fn in (
        test_knob_resolves_actor_when_login_is_not_a_citizen,
        test_knob_wins_over_a_login_naming_a_different_citizen,
        test_default_holds_when_knob_is_unset,
        test_refusal_keeps_prefix_and_names_the_offending_value,
        test_suspended_actor_still_refuses_through_the_knob,
        test_banned_actor_still_refuses_through_the_knob,
    ):
        fn()
        print(f"  ok  {fn.__name__}")
    print("All admin identity tests passed.")


if __name__ == "__main__":
    main()
