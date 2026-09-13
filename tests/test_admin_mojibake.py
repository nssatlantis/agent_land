"""Admin panels source census: no misdecoded glyph sequences (mojibake).

The ASCII sweep (post-#456 follow-up, proposal #460) replaced the
misdecoded non-ASCII sequences that reached the /admin/* panels with
their ASCII intent. This pin blocks regressions: the sequences must stay
absent from the source of every admin panel renderer. Genuine U+2014
em-dashes are not flagged - they are correctly encoded and outside the
sweep's scope (the repo uses them elsewhere).

Sequences (UTF-8 bytes read as cp437 / cp1252) and their ASCII intent:
  '\u252c\u2556'  (┬╖, was U+00B7 middle dot)      -> '|'
  '\u0393\u00c7\u00f6'  (ΓÇö, was U+2014 em dash)  -> '-'
  '\u0393\u00e5\u00c6'  (ΓåÆ, was U+2192 arrow)    -> '->'
  '\u251c\u00f9'  (├ù, was U+00D7 multiplication)  -> 'x'
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_PANEL_FILES = [
    "server/admin/_agents.py",
    "server/admin/_bugs.py",
    "server/admin/_jobs.py",
    "server/admin/_posts.py",
    "server/admin/_reports.py",
]

_MOJIBAKE_SEQ = {
    "\u252c\u2556": "|",
    "\u0393\u00c7\u00f6": "-",
    "\u0393\u00e5\u00c6": "->",
    "\u251c\u00f9": "x",
}


def main():
    flagged = []
    for rel in _PANEL_FILES:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        for seq, ascii_intent in _MOJIBAKE_SEQ.items():
            n = text.count(seq)
            if n:
                flagged.append(f"{rel}: {n} x '{seq}' (ASCII intent '{ascii_intent}')")
    assert not flagged, "mojibake sequences survived the ASCII sweep:\n" + "\n".join(
        flagged
    )
    print("test_admin_mojibake: all admin panels clean of misdecoded glyph sequences")


if __name__ == "__main__":
    main()
