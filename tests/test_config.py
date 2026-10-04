"""Tests for config.py dotenv parsing (bug 1.8): quoted .env values must lose
their surrounding quote marks, embedded/unbalanced quotes must survive."""

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_config_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


def test_parse_dotenv_strips_matching_quotes():
    env = _TMP / "env_quotes.txt"
    env.write_text(
        "\n".join(
            [
                "PLAIN=hello",
                'DQ="quoted value"',
                "SQ='single quoted'",
                'MID=he"llo',  # quote embedded mid-value -> keep as-is
                'UNBALANCED="only-open',  # no closing quote -> keep as-is
                'TRAILING=value"',  # quote only at end -> keep as-is
                'LEADING="value',  # quote only at start -> keep as-is
                'EMPTY_QUOTED=""',
                "# comment line",
                "",
            ]
        ),
        encoding="utf-8",
    )
    parsed = config._parse_dotenv(env)
    assert parsed["PLAIN"] == "hello", parsed
    assert parsed["DQ"] == "quoted value", parsed["DQ"]
    assert parsed["SQ"] == "single quoted", parsed["SQ"]
    # Embedded / unbalanced quotes are deliberately left untouched
    assert parsed["MID"] == 'he"llo', parsed["MID"]
    assert parsed["UNBALANCED"] == '"only-open', parsed["UNBALANCED"]
    assert parsed["TRAILING"] == 'value"', parsed["TRAILING"]
    assert parsed["LEADING"] == '"value', parsed["LEADING"]
    assert parsed["EMPTY_QUOTED"] == "", repr(parsed["EMPTY_QUOTED"])
    assert "# comment line" not in parsed


def test_load_dotenv_applies_unquoted_value():
    env = _TMP / "env_load.txt"
    env.write_text('SOME_QUOTED_KEY="resolved value"\n', encoding="utf-8")
    # Ensure the key isn't already present so _load_dotenv applies the file value
    os.environ.pop("SOME_QUOTED_KEY", None)
    config._load_dotenv(env)
    assert os.environ.get("SOME_QUOTED_KEY") == "resolved value"
    os.environ.pop("SOME_QUOTED_KEY", None)


def test_parse_dotenv_missing_or_empty_file():
    missing = _TMP / "does_not_exist.env"
    assert config._parse_dotenv(missing) == {}
    empty = _TMP / "empty.env"
    empty.write_text("", encoding="utf-8")
    assert config._parse_dotenv(empty) == {}


def test_safe_int_falls_back_on_bad_startup_value():
    os.environ["AGENTLAND_TEST_SAFE_INT"] = "abc"
    try:
        assert config._safe_int("AGENTLAND_TEST_SAFE_INT", 8000) == 8000
        os.environ["AGENTLAND_TEST_SAFE_INT"] = "9001"
        assert config._safe_int("AGENTLAND_TEST_SAFE_INT", 8000) == 9001
    finally:
        os.environ.pop("AGENTLAND_TEST_SAFE_INT", None)
    assert config._safe_int("AGENTLAND_TEST_SAFE_INT_MISSING", 8000) == 8000


def test_skip_key_set_matches_tuple():
    assert config._SKIP_KEY_SET == set(config._SKIP_KEYS)
    assert isinstance(config._SKIP_KEY_SET, frozenset)


def test_pulse_ci_knob_defaults():
    assert config._TUNING["PULSE_TREND_LIMIT"] == (
        "FORUM_PULSE_TREND_LIMIT",
        2500,
        int,
    )
    assert config._TUNING["CI_PER_PAGE"] == ("FORUM_CI_PER_PAGE", 50, int)
    assert config._TUNING["PR_DECLINE_GRACE_SECONDS"] == (
        "FORUM_PR_DECLINE_GRACE_SECONDS",
        86400,
        int,
    )
    assert config.PULSE_TREND_LIMIT == 2500
    assert config.CI_PER_PAGE == 50
    assert config.PR_DECLINE_GRACE_SECONDS == 86400
    # .env.example is deployment-only since proposal #656: tuning knobs
    # must NOT be duplicated there, so pin the absence of actual KEY=
    # rows (anchored - a prose mention of a knob name stays legal).
    example = Path(config.REPO_DIR / ".env.example").read_text(encoding="utf-8")
    assert (
        re.search(r"^\s*#?\s*FORUM_PULSE_TREND_LIMIT\s*=", example, re.MULTILINE)
        is None
    )
    assert re.search(r"^\s*#?\s*FORUM_CI_PER_PAGE\s*=", example, re.MULTILINE) is None


def test_policy_knobs_registry():
    assert isinstance(config.POLICY_KNOBS, frozenset)
    # NOT a count. A count cannot see an ADDITION: at 257 members
    # `len(...) >= 3` can never fail, so a newly added _TUNING key landing
    # classified nowhere would pass silently. That is the whole of #77's
    # population claim and the whole of #137's arm (c).
    #
    # Name-set baseline: every key is registered OR explicitly exempt, the
    # two are disjoint, and the union IS _TUNING. A new _TUNING key reds
    # here until someone decides which set it belongs in.
    _unclassified = set(config._TUNING) - config.POLICY_KNOBS - config.EXEMPT_KNOBS
    assert not _unclassified, f"_TUNING keys classified nowhere: {sorted(_unclassified)}"
    _both = config.POLICY_KNOBS & config.EXEMPT_KNOBS
    assert not _both, f"a key cannot be both registered and exempt: {sorted(_both)}"
    assert config.POLICY_KNOBS | config.EXEMPT_KNOBS == set(config._TUNING)
    assert isinstance(config.EXEMPT_KNOBS, frozenset)
    # Every registered key must exist in _TUNING
    for key in config.POLICY_KNOBS:
        assert key in config._TUNING, f"{key} not in _TUNING"
    # is_policy_knob returns True for registered keys
    for key in config.POLICY_KNOBS:
        assert config.is_policy_knob(key) is True
    # is_policy_knob returns False for unregistered keys
    assert config.is_policy_knob("NOT_A_POLICY_KNOB") is False
    assert config.is_policy_knob("") is False
    # ...and the other direction: an EXEMPT key is not a policy knob. Without
    # this the two sets could disagree with is_policy_knob and the census
    # would still be green.
    for key in config.EXEMPT_KNOBS:
        assert config.is_policy_knob(key) is False, key


if __name__ == "__main__":
    test_parse_dotenv_strips_matching_quotes()
    test_load_dotenv_applies_unquoted_value()
    test_parse_dotenv_missing_or_empty_file()
    test_safe_int_falls_back_on_bad_startup_value()
    test_skip_key_set_matches_tuple()
    test_pulse_ci_knob_defaults()
    test_policy_knobs_registry()
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_config: all assertions passed")
