"""Tests for the bug-nudge novelty signal: the nudge names the newest open
bug report (and carries it as newest_open_bug) in whoami and check_in;
critical reports route by status first (proposal #609)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bugnewest_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()
TOK = AGENTS["alpha"]["token"]


def test_bug_nudge_silence_then_names_newest():
    # Baseline first: fresh isolated DB, nothing filed yet.
    wn0 = db.whoami(TOK)
    assert "bug_note" not in wn0
    assert wn0.get("newest_open_bug") is None
    ci0 = db.check_in(TOK)
    assert ci0["open_bug_reports"] == 0
    assert ci0["newest_open_bug"] is None
    # File two standalone bugs, then the newest must be named everywhere.
    db.file_bug_report(TOK, "First bug", "body", url="https://example.com/bug/n1")
    b2 = db.file_bug_report(TOK, "Second bug", "body", url="https://example.com/bug/n2")
    wn = db.whoami(TOK)
    assert "bug_note" in wn
    assert f"#{b2['id']}" in wn["bug_note"]
    assert "Second bug" in wn["bug_note"]
    assert wn["newest_open_bug"] == {"id": b2["id"], "title": "Second bug"}
    ci = db.check_in(TOK)
    assert ci["open_bug_reports"] == 2
    assert ci["newest_open_bug"] == {"id": b2["id"], "title": "Second bug"}
    assert any(f"#{b2['id']}" in a for a in ci["suggested_actions"])


def test_confirmed_critical_unclaimed_routes_claim():
    # Confirmed-critical with no live claim leads, even with a newer
    # open-critical and older plain opens around: fix-routing wins.
    rep, dup, ver = (AGENTS[k]["token"] for k in ("beta", "gamma", "delta"))
    url = "https://example.com/bug/conf-crit"
    r = db.file_bug_report(
        rep, "Confirmed critical", "body", url=url, severity="critical"
    )
    db.file_bug_report(dup, "Confirmed critical dup", "body2", url=url)
    db.verify_bug_report(ver, r["id"])
    assert db.get_bug_report(r["id"])["status"] == "confirmed"
    wn = db.whoami(TOK)
    assert "CRITICAL" in wn["bug_note"], wn["bug_note"]
    assert f"claim_bug({r['id']})" in wn["bug_note"], wn["bug_note"]
    assert f"#B{r['id']}" in wn["bug_note"], wn["bug_note"]
    assert wn["top_critical_bug"] == {
        "id": r["id"],
        "title": "Confirmed critical",
        "status": "confirmed",
        "action": "claim",
    }
    ci = db.check_in(TOK)
    assert ci["top_critical_bug"]["id"] == r["id"]
    assert ci["top_critical_bug"]["action"] == "claim"
    # A live claim suppresses the fix routing (doctrine: suppress while
    # claimed); the generic backlog note returns instead.
    db.claim_bug(AGENTS["epsilon"]["token"], r["id"])
    wn2 = db.whoami(TOK)
    assert "CRITICAL" not in wn2.get("bug_note", ""), wn2.get("bug_note")
    assert wn2.get("top_critical_bug") is None, wn2.get("top_critical_bug")
    # Release re-arms the routing (liveness gate, no new filing needed).
    db.claim_bug(AGENTS["epsilon"]["token"], r["id"], action="release")
    wn3 = db.whoami(TOK)
    assert f"claim_bug({r['id']})" in wn3["bug_note"], wn3["bug_note"]
    # Resolve to clean up: later tests must see no live criticals.
    db.resolve_bug_report(rep, r["id"], "invalid", "triage-test cleanup")


def test_critical_open_routes_verify():
    # Open-critical leads over a newer plain low (severity beats novelty)
    # and routes verify, not claim.
    url = "https://example.com/bug/open-crit"
    r = db.file_bug_report(
        AGENTS["beta"]["token"], "Open critical", "body", url=url, severity="critical"
    )
    low = db.file_bug_report(TOK, "Plain low", "body")
    wn = db.whoami(TOK)
    assert "CRITICAL" in wn["bug_note"], wn["bug_note"]
    assert f"verify_bug_report({r['id']})" in wn["bug_note"], wn["bug_note"]
    assert wn["top_critical_bug"] == {
        "id": r["id"],
        "title": "Open critical",
        "status": "open",
        "action": "verify",
    }
    ci = db.check_in(TOK)
    assert ci["newest_open_bug"] == {"id": low["id"], "title": "Plain low"}
    assert ci["top_critical_bug"]["id"] == r["id"]
    # Resolve to clean up: later tests must see no live criticals.
    db.resolve_bug_report(AGENTS["beta"]["token"], r["id"], "invalid", "cleanup")
    db.resolve_bug_report(TOK, low["id"], "invalid", "cleanup")


def test_untriaged_confirmed_never_leads():
    # A confirmed bug with no severity never escalates (doctrine): the
    # generic note stands and no critical rides check_in.
    rep, dup, ver = (AGENTS[k]["token"] for k in ("beta", "gamma", "delta"))
    url = "https://example.com/bug/untriaged"
    r = db.file_bug_report(rep, "Untriaged confirmed", "body", url=url)
    db.file_bug_report(dup, "Untriaged confirmed dup", "body2", url=url)
    db.verify_bug_report(ver, r["id"])
    assert db.get_bug_report(r["id"])["status"] == "confirmed"
    wn = db.whoami(TOK)
    assert "CRITICAL" not in wn.get("bug_note", ""), wn.get("bug_note")
    assert wn.get("top_critical_bug") is None, wn.get("top_critical_bug")
    assert db.check_in(TOK)["top_critical_bug"] is None
    db.resolve_bug_report(rep, r["id"], "invalid", "cleanup")


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} bug-nudge-newest tests passed")
