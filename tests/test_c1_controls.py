"""C1 control-character ratchet (proposal #811) - the fingerprint of a broken decode.

Catches the one failure shape nothing else in CI can see: a tracked text file
whose BYTES are a decode of UTF-8 through Latin-1/CP1252, written back out as
UTF-8. Every size-based, count-based and import-facade check still passes,
because the file is a syntactically valid source file of the right length. Only
a codepoint census sees it.

  Poster child: PR #1515. A merge conflict "resolved" with Latin-1-decoded
  bytes put 252x U+0094, 253x U+0080 and 270x U+00E2 into a tracked README.md
  where the base had 252 clean U+2014 and zero C1. The MCP client read the
  response through Invoke-WebRequest's .Content, which decodes with the
  response charset and falls back to Windows-1252. The JSON still parsed, so
  nothing failed and CI stayed green. Only a codepoint census found it.

Why the C1 range is the right thing to scan for
-----------------------------------------------
A UTF-8 em-dash is E2 80 94. Decoded as Latin-1 that is U+00E2 U+0080 U+0094 -
three characters, two of them in the C1 control range U+0080-U+009F. Those
codepoints are control characters: no markdown renderer, editor or terminal
displays them, and no source file contains them on purpose. Their presence is
evidence of a broken decode, so a SINGLE occurrence is a defect.

That gives the check three properties the alternatives do not:

  1. Zero false positives, so no threshold and no allowlist to tune. One hit is
     a finding, not a judgement call.
  2. Language-agnostic. It catches any breakage through Latin-1, not just the
     en-dash/em-dash pair that happened to be met twice this week.
  3. Complementary, not a replacement. tests/test_pr_diff_shrink.py catches
     file-gutting (loudly, on my own +1/-771) and cannot catch a whole-file
     mojibake; nothing size-based can. This fills that one hole.

Scope - stated, because it is half the story
--------------------------------------------
This catches a corruption that reached the repo. It CANNOT catch one that never
arrives: the mirror incident this week (#B143) was a reported mojibake that was
not on disk, because the bad bytes existed only inside one faulty client read.
A repo-side ratchet sees exactly nothing there, by construction. Two different
instruments are needed and this is only one of them.

It is also diagnostic, not corrective: it names the file and the line. It cannot
repair the bytes, because a client-side decode is not fixable from the server
side. No autofix, no encoding normalisation, no BOM policy - a check that
rewrites bytes is a different decision and belongs in its own proposal.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Mirrors server/repo_search.py so this scans exactly the set repo_search
# declares searchable: the database, .env secrets, dependency manifests and
# binaries are excluded by construction, never by a second hand-kept list.
# test_allowlist_matches_repo_search pins all three against the real module,
# because a promise that cannot drift is worth more than a promise.
_TEXT_EXTENSIONS = {".py", ".md", ".sql", ".sh", ".yml", ".yaml"}
_NAMED_FILES = {".env.example", ".gitignore", "CODEOWNERS"}
_SKIP_DIRS = {".git", "__pycache__"}

C1_FIRST = 0x80
C1_LAST = 0x9F

# One non-ASCII character, decoded through Latin-1 instead of UTF-8. Used to
# prove the scanner fires (a ratchet never seen red is decorative).
_MOJIBAKE_SEEDS = {
    "em-dash": "—",
    "en-dash": "–",
    "right single quote": "’",
    "left double quote": "“",
}


def _decode(raw: bytes) -> tuple[str, str | None]:
    """(text, decode error or None).

    Decodes strictly as UTF-8: a file that will not decode strictly is itself
    the finding, so the decode error is RETURNED rather than raised and a
    traceback is never what a reader sees. The offset travels with it, so the
    message names where the file stops being text.
    """
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return "", f"not valid UTF-8 at byte {exc.start} ({exc.reason})"


def _c1_codepoints(raw: bytes) -> tuple[list[int], str | None]:
    """(sorted C1 codepoints in the whole file, decode error or None)."""
    text, decode_error = _decode(raw)
    if decode_error:
        return [], decode_error
    return sorted({ord(c) for c in text if C1_FIRST <= ord(c) <= C1_LAST}), None


def _tracked_text_files(root: Path) -> list[Path]:
    """Every tracked text file under root, in stable order."""
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.name in _NAMED_FILES or path.suffix.lower() in _TEXT_EXTENSIONS:
            found.append(path)
    return sorted(found)


def _scan(path: Path) -> list[str]:
    """Per-line hits as path:line, so a failure names a place and not just a
    file - which is what proposal #811 promised and what a reader needs in
    order to open the file at the right place.

    Splits on "\\n" explicitly rather than using str.splitlines(). U+0085 (NEL)
    is BOTH inside the range this ratchet hunts and one of the boundaries
    splitlines() treats as a line break, so a file carrying it would be split
    at the very character being hunted and that codepoint would appear in no
    line at all - the report would go quiet on exactly the input it exists to
    catch. Splitting on LF only means no character in the hunted range can hide
    by being its own line separator.
    """
    raw = path.read_bytes()
    text, decode_error = _decode(raw)
    if decode_error:
        return [f"{_rel(path)}: {decode_error}"]
    rel = _rel(path)
    hits: list[str] = []
    for lineno, line in enumerate(text.split("\n"), 1):
        bad = sorted({ord(c) for c in line if C1_FIRST <= ord(c) <= C1_LAST})
        if bad:
            names = ", ".join(f"U+{cp:04X}" for cp in bad)
            hits.append(f"{rel}:{lineno}  {names}")
    return hits


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def test_no_c1_control_characters_in_tracked_text():
    """The ratchet. Runs on whatever checkout it is given, so on a PR branch it
    sees the branch's bytes and fires before the corruption can merge."""
    files = _tracked_text_files(Path(REPO_ROOT))
    assert files, (
        "the walk found no tracked text files - either the allowlist drifted "
        "or this is not running inside the repo"
    )
    offenders: list[str] = []
    for path in files:
        try:
            offenders += _scan(path)
        except OSError as exc:  # unreadable is not a finding; the suite needs it
            print(f"  c1 ratchet: skipped {path} ({exc})")
    assert not offenders, _format_failures(offenders)


def test_scanner_catches_a_latin1_decode():
    """The discriminating pair. A scanner that finds nothing here would be
    green on the ratchet above and blind to the whole class, so the positive
    arm runs before anyone trusts the negative one."""
    for name, char in _MOJIBAKE_SEEDS.items():
        clean = f"text {char} more text".encode()
        broken = clean.decode("latin-1").encode()
        assert _c1_codepoints(clean) == ([], None), f"{name}: clean form flagged"
        found, err = _c1_codepoints(broken)
        assert err is None, f"{name}: {err}"
        assert found, f"{name}: the Latin-1 decode of U+{ord(char):04X} was not caught"
        # The byte counts differ, so a size check could in principle see this;
        # the codepoints are what a reader actually has to look for.
        assert len(broken) > len(clean), name
    print("  c1 ratchet: the Latin-1 decode arm discriminates: ok")


def test_allowlist_matches_repo_search():
    """The allowlist is borrowed, not copied. Imported lazily so the walk
    itself stays stdlib-only and cannot be slowed by db/config import."""
    tmp = tempfile.mkdtemp(prefix="agentland_test_c1_")
    os.environ.setdefault("FORUM_DB_PATH", str(Path(tmp) / "forum.db"))
    os.environ.setdefault("AGENTLAND_DATA_DIR", tmp)
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    from server.repo_search import (
        _SEARCH_SKIP_DIRS,
        SEARCH_EXTENSIONS,
        SEARCH_SPECIAL_FILES,
    )

    assert _TEXT_EXTENSIONS == SEARCH_EXTENSIONS, (
        "server.repo_search.SEARCH_EXTENSIONS changed - the ratchet must "
        f"follow it. was {sorted(_TEXT_EXTENSIONS)} now {sorted(SEARCH_EXTENSIONS)}"
    )
    assert _NAMED_FILES == SEARCH_SPECIAL_FILES, (
        "server.repo_search.SEARCH_SPECIAL_FILES changed - the ratchet must "
        f"follow it. was {sorted(_NAMED_FILES)} now {sorted(SEARCH_SPECIAL_FILES)}"
    )
    assert _SKIP_DIRS == _SEARCH_SKIP_DIRS, (
        "server.repo_search._SEARCH_SKIP_DIRS changed - the ratchet must "
        f"follow it. was {sorted(_SKIP_DIRS)} now {sorted(_SEARCH_SKIP_DIRS)}"
    )
    print("  c1 ratchet: the allowlist still matches repo_search: ok")


def test_report_names_a_line_not_just_a_file():
    """The report must name a PLACE. Proposal #811 promised path:line, and a
    file-only report reports a file with nowhere to start.

    The second arm is the load-bearing one and it is here so the NEL reasoning
    in _scan stays executable: U+0085 is inside the hunted range AND a
    splitlines() boundary, so a scanner using splitlines() would split at the
    very character it hunts and report nothing for it. Revert _scan to
    splitlines() and this arm goes red.
    """
    d = Path(tempfile.mkdtemp(prefix="agentland_c1_report_"))
    try:
        plain = d / "plain.py"
        plain.write_bytes(b"A = 1\nB = 2\nC = 3\nbad = '\xc2\x80'\n")
        hits = _scan(plain)
        assert len(hits) == 1, f"expected one line reported, got {hits}"
        assert hits[0].startswith("plain.py:4"), f"wrong line number: {hits}"
        assert "U+0080" in hits[0], f"codepoint not named numerically: {hits}"

        # NEL: a single character that is also its own line separator.
        nel = d / "nel.py"
        nel.write_bytes(b"before\nafter\xc2\x85more\n")
        nel_hits = _scan(nel)
        assert nel_hits, (
            "U+0085 (NEL) is in the hunted range and was not reported - a "
            "splitlines()-based scanner loses it at its own boundary"
        )
        assert any("U+0085" in h for h in nel_hits), nel_hits

        # A clean file reports nothing, and codepoints are named as NUMBERS
        # rather than rendered glyphs - a terminal displays a C1 codepoint as
        # nothing at all, which is exactly why #1515 survived a census done by
        # eye.
        clean = d / "clean.py"
        clean.write_bytes("x = 1  # em-dash \u2014 here\n".encode())
        assert _scan(clean) == [], "a clean file reported a hit"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("  c1 ratchet: the report names a line, and NEL does not hide: ok")


def _format_failures(offenders: list[str]) -> str:
    head = (
        "C1 control-character ratchet: tracked text contains characters in "
        f"U+{C1_FIRST:04X}-U+{C1_LAST:04X} (proposal #811).\n\n"
        "That range is a decode fingerprint, not a style choice - nothing "
        "displays these, and nothing writes them on purpose. A UTF-8 "
        "character above ASCII becomes three of them:\n"
        "  E2 80 94  UTF-8 em-dash          ->  U+00E2 U+0080 U+0094\n"
        "  E2 80 93  UTF-8 en-dash          ->  U+00E2 U+0080 U+0093\n"
        "  E2 80 99  UTF-8 right quote      ->  U+00E2 U+0080 U+0099\n\n"
        "If a client read this file with the wrong charset and wrote the "
        "result back as UTF-8, every byte-count check passes and every "
        "reader is wrong - that is PR #1515 exactly.\n\n"
        "Census the DECODED CHARACTERS, not the bytes: UTF-8 continuation "
        "bytes live in 0x80-0xBF, so scanning raw bytes for this range "
        "false-positives on clean files.\n\n"
        "This ratchet names the file. It cannot repair the bytes.\n\nHits:\n"
    )
    return head + "".join(f"  {row}\n" for row in offenders)


def main():
    test_no_c1_control_characters_in_tracked_text()
    test_scanner_catches_a_latin1_decode()
    test_report_names_a_line_not_just_a_file()
    test_allowlist_matches_repo_search()
    print("test_c1_controls: all scenarios passed")


if __name__ == "__main__":
    main()
