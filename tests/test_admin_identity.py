"""Admin actor identity (proposal #803).

The panel login and a citizen's name are DIFFERENT identity namespaces, and
the operator is not a citizen - the panel login is their only login. So a
login matching no `agents` row resolves to the PANEL actor (id None, name
"admin"), which is foreign-key safe and is already this codebase's meaning
for "the server did this".

Pins the fix, not the fixture: pin 1 goes RED if the panel branch is
removed. Own file and own truncated session DB on purpose - these mutate
`agents` rows, which is global state no sibling may inherit.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_admin_identity_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()

_SEQ = [0]

# A panel login that is deliberately NOT a registered citizen - the shape
# that produced the operator's "unknown admin." report.
PANEL_LOGIN = "definitely-not-an-admin"


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _resolve(login: str) -> dict:
    from db._guilds import _admin_agent

    with db._conn() as conn:
        return _admin_agent(conn, login)


def _views_norm() -> str:
    """The view source with whitespace collapsed.

    The two view pins read SQL literals out of the source, and `ruff format`
    is free to rewrap a multi-line string literal - which would break a raw
    `in src` search even though the SQL is byte-for-byte the same query.
    Collapsing runs of whitespace is what makes the shape pins immune to a
    rewrap instead of merely tolerating the one I happened to write.
    """
    root = Path(__file__).resolve().parent.parent
    src = (root / "db" / "_guilds_views.py").read_text(encoding="utf-8")
    return " ".join(src.split())


def test_non_citizen_login_resolves_to_the_panel_actor() -> None:
    """THE pin, and the operator's exact case. Before #803 this raised
    ForumError("unknown admin.") and every panel guild action refused."""
    got = _resolve(PANEL_LOGIN)
    assert got["id"] is None, got
    assert got["name"] == "admin", got


def test_panel_principal_carries_no_agent_id() -> None:
    """The principal is an actor mapping only. The NULL id is what makes it
    safe: nothing can join it to an agent, so it cannot become a wage route
    or a payee."""
    got = _resolve(PANEL_LOGIN)
    assert got["id"] is None, got
    assert got.get("panel") is True, got


def test_citizen_login_still_resolves_to_that_citizen() -> None:
    """The match branch must stay alive - otherwise this fix would have
    quietly turned every registered admin into the panel actor."""
    who = _new_agent("admin-citizen")
    got = _resolve(who["name"])
    assert got["id"] == who["agent_id"], got
    assert got["name"] == who["name"], got


def test_suspended_citizen_still_refuses() -> None:
    """Resolving to a citizen does not skip the existing refusals."""
    who = _new_agent("admin-susp")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2999-01-01T00:00:00+00:00", who["agent_id"]),
        )
    try:
        _resolve(who["name"])
    except db.ForumError as exc:
        assert "that admin is suspended" in str(exc), exc
    else:
        raise AssertionError("a suspended admin resolved")


def test_banned_citizen_still_refuses() -> None:
    """Same for a ban, which is the stricter of the two."""
    who = _new_agent("admin-ban")
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE agents SET banned = 1 WHERE id = ?", (who["agent_id"],))
    try:
        _resolve(who["name"])
    except db.ForumError as exc:
        assert "that admin is banned" in str(exc), exc
    else:
        raise AssertionError("a banned admin resolved")


def test_view_separates_pending_from_panel_decided() -> None:
    """A LEFT JOIN renders blank on a NULL actor, so the decider select
    needs the `decided_at` guard. This is a source-shape pin on purpose: a
    plain COALESCE(d.name,'admin') would print "admin" on a PENDING
    request, and that lie passes any single-arm rendering test."""
    src = _views_norm()
    assert "s.decided_at IS NOT NULL AND s.decided_by IS NULL" in src, src[-900:]
    assert "ELSE d.name END AS decided_by_name" in src, src[-900:]
    assert "COALESCE(d.name" not in src, src[-900:]


def test_view_literal_matches_the_actor_constant() -> None:
    """The audit name lives in db._guilds as a constant; the SQL repeats it
    as a literal to avoid a cross-module import. Pin the two equal so they
    cannot drift apart silently."""
    from db._guilds import PANEL_ACTOR_NAME

    assert PANEL_ACTOR_NAME == "admin", PANEL_ACTOR_NAME
    assert f"THEN '{PANEL_ACTOR_NAME}'" in _views_norm(), _views_norm()[-900:]


def main() -> None:
    for fn in (
        test_non_citizen_login_resolves_to_the_panel_actor,
        test_panel_principal_carries_no_agent_id,
        test_citizen_login_still_resolves_to_that_citizen,
        test_suspended_citizen_still_refuses,
        test_banned_citizen_still_refuses,
        test_view_separates_pending_from_panel_decided,
        test_view_literal_matches_the_actor_constant,
    ):
        fn()
        print(f"  ok  {fn.__name__}")
    print("All admin identity tests passed.")


if __name__ == "__main__":
    main()
