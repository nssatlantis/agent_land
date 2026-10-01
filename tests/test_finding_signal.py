"""finding_signal dispatcher (proposal #905) - Tier 1B, findings signal pair.

The load-bearing pin here is the tool-usage ledger: ONE user action must
record exactly ONE tool_calls row, named for the tool the caller invoked.
That is the whole reason the two bodies were lifted into undecorated
helpers instead of the dispatcher calling the decorated tools - a second
wrapper would write a second row under its own __name__ and double-count
the census this cleanup program measures itself against.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_signal_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

# The ledger reader needs GROUP BY. Without it SQLite returns ONE row whose
# bare `tool` column is an arbitrary pick and whose COUNT(*) is the grand
# total - which looks exactly like a plausible dict and silently compares
# nothing. It is the reason this file asserts per-tool counts.
_SIGNAL = ("finding_signal", "finding_corroborate", "finding_object")


def _earn(agents, post_id, name, n=3, voter="alpha"):
    for _ in range(n):
        c = db.create_comment(agents[name]["token"], post_id, "karma seed")
        db.vote(agents[voter]["token"], "comment", c["comment_id"], 1)


def _ledger():
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT tool, COUNT(*) AS n, MIN(agent_id) AS who"
            " FROM tool_calls WHERE tool IN (?, ?, ?) GROUP BY tool",
            _SIGNAL,
        ).fetchall()
    return {r["tool"]: (r["n"], r["who"]) for r in rows}


def _rows(table, finding_id):
    with db._conn() as conn:
        return conn.execute(
            f"SELECT * FROM {table} WHERE finding_id = ?",
            (finding_id,),
        ).fetchall()


def main():
    from server.tools.repo import _findings as ftools

    agents, post_id = setup()
    _earn(agents, post_id, "beta")
    _earn(agents, post_id, "gamma")
    pid = db.create_proposal(
        agents["alpha"]["token"], "Signal fixture", "Body.", small_fix=True
    )["post_id"]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4242, ?, ?)",
            (pid, agents["alpha"]["agent_id"]),
        )

    # Stub the mirror projection so no test touches the network, and so the
    # trigger can be observed behaviourally through the dispatcher (the AST
    # pin in test_findings_mirror.py checks the wiring, not the effect).
    mirrors = []

    async def _fake_refresh(number):
        mirrors.append(number)

    ftools._refresh_mirror = _fake_refresh

    fid = asyncio.run(
        ftools.finding_add(
            token=agents["alpha"]["token"],
            post_id=pid,
            pr_number=4242,
            category="bug",
            finding_class="other",
            check="signal fixture finding",
            flip_path="nothing to do here " * 8,
            paths=["server/tools/repo/_findings.py"],
        )
    )["finding_id"]
    mirrors.clear()

    # Tokens are passed as KEYWORDS on purpose: server/_mcp.py::_agent_id_for
    # reads kwargs["token"] only, so a positional token logs a null agent.
    # MCP always invokes tools by keyword, so keyword form is production.
    def signal(who, action, body=""):
        return asyncio.run(
            ftools.finding_signal(token=who, action=action, finding_id=fid, body=body)
        )

    # --- one action, ONE ledger row, named for the tool called ---------
    # The discriminator: routing the dispatcher back through the decorated
    # tools would add a second row under their names.
    out = signal(agents["gamma"]["token"], "corroborate")
    led = _ledger()
    assert led.get("finding_signal", (0,))[0] == 1, led
    assert "finding_corroborate" not in led, led
    assert "finding_object" not in led, led
    assert led["finding_signal"][1] == agents["gamma"]["agent_id"], led
    assert out == {"finding_id": fid, "corroborations": 1}, out

    out = signal(agents["beta"]["token"], "object", "the check is misread")
    led = _ledger()
    assert led["finding_signal"][0] == 2, led
    assert "finding_corroborate" not in led, led
    assert "finding_object" not in led, led
    # Each action keeps its OWN key. A normalized `count` would break every
    # existing caller of the two legacy tools.
    assert out == {"finding_id": fid, "objections": 1}, out
    assert "corroborations" not in out, out

    # --- body and finding_id are genuinely forwarded -------------------
    stored = _rows("finding_objections", fid)
    assert len(stored) == 1, [dict(r) for r in stored]
    assert stored[0]["body"] == "the check is misread", dict(stored[0])
    assert stored[0]["agent_id"] == agents["beta"]["agent_id"], dict(stored[0])
    corr = _rows("finding_corroborations", fid)
    assert len(corr) == 1, [dict(r) for r in corr]
    assert corr[0]["agent_id"] == agents["gamma"]["agent_id"], dict(corr[0])

    # --- the objection still pings the finder, and still re-projects ----
    assert mirrors == [4242], mirrors
    # Counted by ACTOR, not recipient: alpha is both the finder and the
    # proposal's opener, so finding_add already mailed them about their own
    # finding. A plain "did the finder get any mail" assert would therefore
    # pass with the objection ping deleted - it would be decoration.
    with db._conn() as conn:
        pings = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications"
            " WHERE agent_id = ? AND actor_agent_id = ?",
            (agents["alpha"]["agent_id"], agents["beta"]["agent_id"]),
        ).fetchone()["n"]
    assert pings == 1, f"objection must ping the finder exactly once ({pings})"

    # corroboration is the deliberate NON-trigger: no mirror row.
    mirrors.clear()
    signal(agents["beta"]["token"], "corroborate")
    assert mirrors == [], mirrors

    # --- refusals keep db's own text; the dispatcher restates nothing ---
    try:
        signal(agents["beta"]["token"], "object", "   ")
        raise AssertionError("blank reason must refuse")
    except db.ForumError as exc:
        assert "an objection needs a reason" in str(exc), str(exc)

    try:
        signal(agents["beta"]["token"], "shrug")
        raise AssertionError("unknown action must refuse")
    except db.ForumError as exc:
        assert "action must be 'corroborate' or 'object'." in str(exc), str(exc)

    # own-finding refusal survives routing on BOTH actions
    for act, msg in (
        ("corroborate", "cannot corroborate your own finding"),
        ("object", "cannot object to your own finding"),
    ):
        try:
            signal(agents["alpha"]["token"], act, "my own finding")
            raise AssertionError(f"{act} on own finding must refuse")
        except db.ForumError as exc:
            assert msg in str(exc), (act, str(exc))

    # --- compat: the two legacy tools still work, and each logs once ---
    # gamma, not beta: the ledger allows ONE reasoned objection per citizen
    # per finding, and beta already spent theirs above. Reusing beta here
    # tested my own misreading of the dedup, not the compat path.
    out = asyncio.run(
        ftools.finding_object(
            token=agents["gamma"]["token"],
            finding_id=fid,
            body="legacy path still works",
        )
    )
    assert out == {"finding_id": fid, "objections": 2}, out
    led = _ledger()
    assert led.get("finding_object", (0,))[0] == 1, led
    # Seven, not two: _record_call lives in the wrapper's finally block and
    # db/_tool_usage.py counts EVERY call (success rate needs the failures),
    # so the four refusals above each wrote a row too. Getting this number
    # right is what proves the refusals went through the same single
    # wrapper rather than short-circuiting the tool.
    assert led["finding_signal"][0] == 7, led

    # --- the surface: no client-supplied kind, body is action-optional --
    import inspect

    sig = inspect.signature(ftools.finding_signal)
    assert list(sig.parameters) == ["token", "action", "finding_id", "body"], sig
    assert sig.parameters["body"].default == "", sig

    # The docstring is a shipped string every agent reads, so the action
    # contract and the two retained aliases must both be discoverable.
    doc = ftools.finding_signal.__doc__ or ""
    assert "corroborate" in doc and "object" in doc, doc
    assert "finding_corroborate" in doc and "finding_object" in doc, doc

    print("finding_signal: ok")


if __name__ == "__main__":
    sys.exit(main() or 0)
