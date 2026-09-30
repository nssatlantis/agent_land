"""Ratchet the DOCUMENTED spelling of every BUG_RESOLUTIONS token.

The class this closes: a list of places-that-are-wrong, fixed only where
the author happened to be looking.  Finding #14 on #1522 is the instance -
`resolve_bug_report`'s docstring hyphenated `already-fixed`, a token the
validator at db/_bug_reports.py can never match, and the fix shipped
while a second comment in the same file kept the false spelling.

Why membership against the live tuple, and not a substring ratchet.  The
obvious form is "assert the string `already-fixed` appears nowhere": it
passes trivially the moment the hyphen is gone (there is nothing left to
catch), and it would keep passing if someone renamed the tuple wholesale
or deleted it.  Reading `BUG_RESOLUTIONS` out of the source and asserting
on its members means the test fails when a fourth reason is added without
its documentation following, and fails if any member's documented spelling
drifts from the validated one.  It also cannot be satisfied by a
hand-built fixture, because the value it checks IS the production value.

SELF-EXCLUSION, and it is deliberate rather than a workaround.  The first
rehearsal of this file went RED on itself: this docstring quotes the
hyphenated token three times in order to explain what is forbidden, and
the scan counted its own explanation.  A ratchet that cries wolf is a
ratchet that gets deleted (the discipline is named in #672 item 5321 and
in finding #14's own flip path), so the scan skips THIS file.  The cost is
stated: a hyphenated spelling introduced into this file's own prose would
not be caught, and that is the right trade - the file has no documented
spellings of its own to keep honest, and it is not a surface a reader
would copy a token from.

Scope note, because it is the limit and not a claim of completeness: this
ratchets the documented spelling in .py sources under repo_search's
allowlist, and separately requires every member to be written out in
db/_bug_reports.py outside the tuple itself - so a new reason cannot be
added to BUG_RESOLUTIONS with no discoverable spelling anywhere in the
module that enforces it.  It does not cover prose that allowlist excludes, and it does
not police the `bug_resolutions` TABLE name, which is a different token
(schema.sql:1425, moderation.py:554, two test INSERTs) and is correct as
written - a ratchet keyed on that substring would false-positive on all
five and get deleted for crying wolf.
"""

import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("GITHUB_REPO", "nssatlantis/agent_land")
os.environ.setdefault("GITHUB_TOKEN", "")

_SRC = _ROOT / "db" / "_bug_reports.py"
_TUPLE_RE = re.compile(r"^BUG_RESOLUTIONS\s*=\s*\((.*?)\)\s*$", re.MULTILINE)

# This file.  It quotes the forbidden token in its own docstring to explain
# the rule, so scanning it would fail on the explanation.  See the module
# docstring: the exclusion is a decision, not an oversight.
_SELF = Path(__file__).resolve()

# The same extensions repo_search walks, so the ratchet's population is the
# one a reader would actually search.
_SUFFIXES = (".py", ".md", ".sql", ".sh", ".yml", ".yaml")
_SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def _live_reasons() -> list[str]:
    """BUG_RESOLUTIONS, read out of the production source.

    Deliberately not imported: importing db to read a literal would make
    the test depend on the import side effects of the whole package, and a
    parse of the assignment is the same claim with none of that.
    """
    m = _TUPLE_RE.search(_SRC.read_text(encoding="utf-8"))
    assert m, f"BUG_RESOLUTIONS not found in {_SRC} - did the tuple move?"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_live_tuple_is_underscored_and_nonempty():
    """The vocabulary itself: members are snake_case tokens, and there is
    at least one.  Without this arm an empty or renamed tuple would make
    every membership assertion below vacuous."""
    reasons = _live_reasons()
    assert reasons, "BUG_RESOLUTIONS parsed to an empty vocabulary"
    for r in reasons:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", r), (
            f"{r!r} is not a snake_case token - the documented spellings "
            f"below are matched against this vocabulary, so a change here "
            f"must be a deliberate one"
        )
    assert "already_fixed" in reasons, (
        "the already_fixed token is gone; update this ratchet deliberately "
        f"rather than letting it go quiet - current vocabulary: {reasons}"
    )


def test_every_reason_is_documented_beside_the_validator():
    """The positive control, and the arm that makes the hyphen ratchet
    trustworthy.

    A negative ratchet ("this spelling must not appear") is only worth
    something if the positive one holds: every reason the validator
    accepts must also be WRITTEN somewhere in the module that enforces
    it, outside the tuple literal itself.  Without this, adding a fourth
    reason to BUG_RESOLUTIONS with no discoverable spelling anywhere
    would pass every other arm - the member is snake_case, and its
    hyphenated variant appears nowhere - which is precisely the
    "a list of places-that-are-wrong, chosen by whoever was looking"
    shape this file exists to close.

    The tuple literal is excluded from the search, or the assertion would
    be satisfied by the very declaration it is supposed to audit.
    """
    text = _SRC.read_text(encoding="utf-8")
    m = _TUPLE_RE.search(text)
    assert m, f"BUG_RESOLUTIONS not found in {_SRC} - did the tuple move?"
    outside = text[: m.start(1)] + text[m.end(1) :]
    for reason in _live_reasons():
        assert re.search(rf"\b{re.escape(reason)}\b", outside), (
            f"{reason!r} is accepted by the validator but is never written "
            f"out in {_SRC.name} outside the tuple itself, so a reader "
            f"cannot discover the spelling from the module that enforces "
            f"it. Either document it there, or widen this ratchet "
            f"deliberately - do not let it go quiet."
        )


def test_no_hyphenated_variant_of_any_reason_anywhere():
    """The actual catch.  For each live reason, its hyphenated form must
    appear in no source file - that is the false token a reader copies.

    This is the arm that goes red on today's bytes: before the fix, both
    db/_bug_reports.py and config.py carried the hyphenated spelling.
    """
    offenders: list[str] = []
    scanned = 0
    for reason in _live_reasons():
        bad = reason.replace("_", "-")
        if bad == reason:
            continue  # a single-word reason has no hyphenated variant
        for path in sorted(_ROOT.rglob("*")):
            if path.resolve() == _SELF:
                continue  # see _SELF
            if not path.is_file() or path.suffix not in _SUFFIXES:
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            scanned += 1
            for n, line in enumerate(text.splitlines(), 1):
                if bad in line:
                    rel = path.relative_to(_ROOT).as_posix()
                    offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
    # The scan must have actually looked at something.  Without this, a
    # ratchet that silently scanned zero files would pass forever - which
    # is the vacuous-pin shape this file exists to avoid.
    assert scanned > 0, "the scan read zero files - the ratchet is vacuous"
    assert not offenders, (
        "hyphenated BUG_RESOLUTIONS token(s) in the tree - each is a "
        "spelling the validator can never match:\n  " + "\n  ".join(offenders)
    )


def main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        else:
            print(f"  {name}: ok")
    print(
        "test_bug_resolution_tokens: all assertions passed"
        if not failures
        else f"test_bug_resolution_tokens: {failures} failed"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
