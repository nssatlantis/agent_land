"""Tests for config.py dotenv parsing (bug 1.8): quoted .env values must lose
their surrounding quote marks, embedded/unbalanced quotes must survive."""

import os
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


if __name__ == "__main__":
    test_parse_dotenv_strips_matching_quotes()
    test_load_dotenv_applies_unquoted_value()
    test_parse_dotenv_missing_or_empty_file()
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_config: all assertions passed")
