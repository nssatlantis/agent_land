"""tests/test_rules_text_caps - rule 25's guild caps render from live config.

RULES_TEXT rule 25 states the guild structural caps in prose. It used to
hardcode them, which made the rules advertise 10 members per guild while the
deployment enforced 6. The module's own contract (rules_text._rules_text) is
that every number resolves from config at call time, so an .env edit shows up
on the next get_rules(). These two pins hold that contract for the guild
sentence, so the next hardcoded number fails here instead of shipping.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_rules_caps_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import rules_text  # noqa: E402


def test_guild_caps_render_from_live_config():
    """Rendered rules must carry each LIVE cap beside its label.

    A hardcoded default fails the moment the deployment overrides the knob,
    which is exactly the defect these pins replace. Whitespace is collapsed so
    the assertions do not depend on how the template happens to wrap.
    """
    flat = " ".join(rules_text._rules_text().split())
    for label, value in (
        ("concurrent memberships", config.GUILD_MAX_MEMBERSHIPS),
        ("live guilds", config.GUILD_MAX_GUILDS),
        ("members per guild", config.GUILD_MAX_MEMBERS),
    ):
        needle = f"{value} {label}"
        assert needle in flat, f"rule 25 omits live cap: {needle!r}"


def test_guild_caps_are_placeholders_not_literals():
    """Pin the template so a reintroduced literal cannot pass unnoticed.

    Without this, a future edit that hardcodes today's value would keep
    passing the render check for as long as the config still matched.
    """
    tpl = rules_text._RULES_TPL
    for placeholder in (
        "{GUILD_MAX_MEMBERSHIPS}",
        "{GUILD_MAX_GUILDS}",
        "{GUILD_MAX_MEMBERS}",
    ):
        assert placeholder in tpl, f"lost {placeholder}"


if __name__ == "__main__":
    test_guild_caps_render_from_live_config()
    test_guild_caps_are_placeholders_not_literals()
    print("rule 25 guild cap pins: ok")
