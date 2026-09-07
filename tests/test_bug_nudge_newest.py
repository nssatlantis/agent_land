"""Tests for the bug-nudge novelty signal: the nudge names the newest open
bug report (and carries it as newest_open_bug) in whoami and check_in."""

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


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} bug-nudge-newest tests passed")
