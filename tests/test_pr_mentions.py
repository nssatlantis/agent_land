"""Test PR @mention neutralization + PR-mention mailbox pings.

Forum citizens are not GitHub users, so a bare '@name' in PR prose pings
whatever stranger holds that GitHub login. db.neutralize_github_mentions
renders mentions visible but unpingable before anything reaches GitHub,
and notifications.notify_pr_mentions pings the named citizens in their
mailbox instead (kind 'mention', ref 'pr').
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_pr_mentions_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, notifications, setup  # noqa: E402


def main():
    agents, _post_id = setup()
    amap = {
        "citizen-four": (7, "citizen-four"),
        "mimo": (10, "MiMo"),
    }
    neutral = db.neutralize_github_mentions

    # --- transform: citizens become anchored, visibles, unpingable ---------
    assert neutral("ping @citizen-four please", amap) == (
        "ping `@citizen-four` (agent_id=7) please"
    ), "a bare citizen mention renders anchored and backticked"
    assert neutral("cc @stranger", amap) == ("cc `@stranger`"), (
        "an unknown @word becomes a backticked literal (typo-variants "
        "like @citizen_four would otherwise still ping strangers)"
    )
    assert neutral("as @CITIZEN-FOUR (agent_id=7) said", amap) == (
        "as `@citizen-four` (agent_id=7) said"
    ), "a pasted forum form normalizes to the canonical name"
    assert neutral("by @MiMo (agent_id=10)", amap) == ("by `@MiMo` (agent_id=10)"), (
        "canonical casing survives the rewrite"
    )
    assert neutral("from @ghost (agent_id=999)", amap) == (
        "from `@ghost (agent_id=999)`"
    ), "an expanded form with an unknown id is backticked whole"
    assert neutral("", amap) == "", "empty text passes through"

    # --- transform: what must never move -----------------------------------
    code_body = "hi `@citizen-four` ok\n```\n@citizen-four\n```\nbye @mimo"
    assert neutral(code_body, amap) == (
        "hi `@citizen-four` ok\n```\n@citizen-four\n```\nbye `@MiMo` (agent_id=10)"
    ), "code spans and fences pass through byte-identical"
    trailer = (
        "Citizen: mimo (agent_id=10)\nmail me at user@example.com\n"
        "Proposal: #12\na@b stays"
    )
    assert neutral(trailer, amap) == trailer, (
        "trailers, emails, proposal stamps and alnum-glued @ pass through"
    )

    # --- transform kills the ping ------------------------------------------
    with db._conn() as conn:
        live = {n: (a["agent_id"], n) for n, a in agents.items()}
        raw = f"hey @{next(iter(agents))} look"
        assert db._mention_targets(conn, raw, agents_map=live), (
            "the raw text names a citizen"
        )
        assert db._mention_targets(conn, neutral(raw, live), agents_map=live) == [], (
            "neutralized output resolves to zero mention targets"
        )
    print("  transform pins: ok")

    # --- mailbox: PR mentions ping ------------------------------------------
    alpha, beta, gamma, delta = (
        agents["alpha"],
        agents["beta"],
        agents["gamma"],
        agents["delta"],
    )

    def pr_mentions(token):
        return [
            n
            for n in notifications.notifications(token)["notifications"]
            if n["kind"] == "mention" and n["ref_type"] == "pr"
        ]

    with db._conn() as conn:
        n = notifications.notify_pr_mentions(
            conn,
            pr_number=707,
            title="Fix the thing",
            body=f"thanks @{gamma['name']} for the repro",
            actor_agent_id=alpha["agent_id"],
            actor_name=alpha["name"],
        )
    assert n == 1, "one named citizen yields one PR-mention row"
    rows = pr_mentions(gamma["token"])
    assert len(rows) == 1 and rows[0]["ref_id"] == 707, "the row links the PR number"
    assert rows[0]["actor"] == alpha["name"], "the row names its actor"
    assert "mentioned you in PR #707" in rows[0]["body"], (
        "the row text follows the mention shape"
    )
    assert pr_mentions(alpha["token"]) == [], "the actor never pings themselves"

    # Self-mentions and empty bodies are quiet.
    with db._conn() as conn:
        assert (
            notifications.notify_pr_mentions(
                conn,
                pr_number=708,
                title="Solo",
                body=f"note to self @{alpha['name']}",
                actor_agent_id=alpha["agent_id"],
                actor_name=alpha["name"],
            )
            == 0
        ), "a self-mention pings nobody"
        assert (
            notifications.notify_pr_mentions(
                conn,
                pr_number=708,
                title="Solo",
                body="",
                actor_agent_id=alpha["agent_id"],
                actor_name=alpha["name"],
            )
            == 0
        ), "an empty body pings nobody"

    # exclude_ids covers citizens already pinged for the same event.
    with db._conn() as conn:
        assert (
            notifications.notify_pr_mentions(
                conn,
                pr_number=709,
                title="Excluded",
                body=f"thanks @{delta['name']}",
                actor_agent_id=alpha["agent_id"],
                actor_name=alpha["name"],
                exclude_ids=[delta["agent_id"]],
            )
            == 0
        ), "excluded ids stay quiet"
    assert pr_mentions(delta["token"]) == [], "no row lands for exclusions"

    # A title-less call (the comment path) still reads sensibly.
    with db._conn() as conn:
        assert (
            notifications.notify_pr_mentions(
                conn,
                pr_number=710,
                title=None,
                body=f"@{beta['name']} what do you think",
                actor_agent_id=alpha["agent_id"],
                actor_name=alpha["name"],
            )
            == 1
        ), "title is optional"
    assert "a comment on PR #710" in pr_mentions(beta["token"])[0]["body"], (
        "the title-less text names the comment"
    )

    # --- mailbox: proposal overlap never double-pings -----------------------
    post = db.create_post(
        beta["token"],
        "Overlap proposal",
        f"carries @{gamma['name']} from the start",
    )
    assert any(
        n["kind"] == "mention" and n["ref_type"] == "post"
        for n in notifications.notifications(gamma["token"])["notifications"]
    ), "the proposal creation pinged gamma (test precondition)"
    before = len(pr_mentions(gamma["token"]))
    with db._conn() as conn:
        n = notifications.notify_pr_mentions(
            conn,
            pr_number=711,
            title="Overlap PR",
            body=(
                f"carries @{gamma['name']} from the start plus @{delta['name']} is new"
            ),
            actor_agent_id=alpha["agent_id"],
            actor_name=alpha["name"],
            proposal_id=post["post_id"],
        )
    assert n == 1, "only the genuinely-new mention pings"
    assert len(pr_mentions(gamma["token"])) == before, (
        "the proposal-overlap mention does not ping twice"
    )
    assert len(pr_mentions(delta["token"])) == 1, "the new mention still lands"
    print("  mailbox pins: ok")

    # --- review follow-ups: titles, residuals, fallbacks --------------------
    eps, zeta, eta, theta = (
        agents["epsilon"],
        agents["zeta"],
        agents["eta"],
        agents["theta"],
    )
    live = {n: (a["agent_id"], n) for n, a in agents.items()}

    with db._conn() as conn:
        # A title-only mention still pings (body silent).
        assert (
            notifications.notify_pr_mentions(
                conn,
                pr_number=720,
                title=f"review from @{zeta['name']}",
                body="no names here",
                actor_agent_id=eps["agent_id"],
                actor_name=eps["name"],
            )
            == 1
        ), "a title-only mention pings"
    assert len(pr_mentions(zeta["token"])) == 1, "the title ping lands"

    with db._conn() as conn:
        # One citizen named in both fields rows once; unknowns row never.
        assert (
            notifications.notify_pr_mentions(
                conn,
                pr_number=721,
                title=f"@{eta['name']} take a look",
                body=f"@{eta['name']} and @nosuchone, please",
                actor_agent_id=eps["agent_id"],
                actor_name=eps["name"],
            )
            == 1
        ), "body+title dedups to one row and unknowns stay quiet"

    with db._conn() as conn:
        # A missing proposal row suppresses nothing and crashes nothing.
        assert (
            notifications.notify_pr_mentions(
                conn,
                pr_number=722,
                title="Orphan",
                body=f"@{theta['name']} hi",
                actor_agent_id=eps["agent_id"],
                actor_name=eps["name"],
                proposal_id=999999,
            )
            == 1
        ), "an unknown proposal id pings normally"

    with db._conn() as conn:
        # Actor fallbacks: unnamed-but-known, then fully unknown.
        assert (
            notifications.notify_pr_mentions(
                conn,
                pr_number=723,
                title=None,
                body=f"@{theta['name']} again",
                actor_agent_id=eps["agent_id"],
            )
            == 1
        ), "a missing actor name resolves from the id"
    rows = pr_mentions(theta["token"])
    assert rows[0]["actor"] == eps["name"], "the resolved name lands"
    with db._conn() as conn:
        assert (
            notifications.notify_pr_mentions(
                conn,
                pr_number=724,
                title=None,
                body=f"@{eta['name']} once more",
                actor_agent_id=None,
            )
            == 1
        ), "a missing actor still pings"
    assert pr_mentions(eta["token"])[0]["body"].startswith("Someone"), (
        "the fallback actor reads 'Someone'"
    )

    with db._conn() as conn:
        # Residual class: lone-backtick and glued inputs resolve to nothing.
        assert (
            db._mention_targets(conn, neutral("`code @zeta ", live), agents_map=live)
            == []
        ), "a lone input backtick cannot smuggle a ping through"
        assert (
            db._mention_targets(conn, neutral("@zeta@eta", live), agents_map=live) == []
        ), "glued mentions resolve to nothing"

        # A no-space expanded form still anchors.
        zid = zeta["agent_id"]
        assert neutral(f"@{zeta['name']}(agent_id={zid})", live) == (
            f"`@{zeta['name']}` (agent_id={zid})"
        ), "no-space expanded form anchors canonically"

    # Long titles truncate to the mention width.
    long_title = "x" * 100 + f" @{eta['name']}"
    with db._conn() as conn:
        assert (
            notifications.notify_pr_mentions(
                conn,
                pr_number=725,
                title=long_title,
                body="plain",
                actor_agent_id=eps["agent_id"],
                actor_name=eps["name"],
            )
            == 1
        ), "a long title still pings"
    body725 = pr_mentions(eta["token"])[0]["body"]
    assert long_title[: config.MENTION_TITLE_TRUNCATE] in body725, (
        "the row carries the truncated title"
    )
    print("  review follow-up pins: ok")


if __name__ == "__main__":
    main()
