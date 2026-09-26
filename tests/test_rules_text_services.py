"""tests/test_rules_text_services - rule 23's services knobs render from live config.

Rule 23 states the services-shelf price bounds and the active-listing cap.
All three are knobs (SERVICE_MIN_PRICE, SERVICE_MAX_PRICE,
SERVICE_MAX_ACTIVE_PER_AGENT) and db/_services.py already enforces every one
of them from config - but the citizen-facing prose hardcoded the numbers, so a
deployment override would falsify them with no test failing. The module's own
contract (rules_text._rules_text) is that every number resolves from config at
call time, so an .env edit shows up on the next get_rules().

These three pins hold that contract for the services sentence. Pin 3 is the
deployment-independent one: it asserts the literals are *absent* from the tool
description, which no override state can excuse.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_rules_services_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
import rules_text  # noqa: E402

_KNOBS = (
    "SERVICE_MIN_PRICE",
    "SERVICE_MAX_PRICE",
    "SERVICE_MAX_ACTIVE_PER_AGENT",
)


def test_services_knobs_render_from_live_config():
    """Rendered rules must carry each LIVE services value.

    Whitespace is collapsed so the assertions do not depend on how the
    template happens to wrap.
    """
    flat = " ".join(rules_text._rules_text().split())
    lo, hi = config.SERVICE_MIN_PRICE, config.SERVICE_MAX_PRICE
    price = f"between {lo:g} and {hi:g} credits"
    assert price in flat, f"rule 23 omits the live price bounds: {price!r}"
    cap = f"{config.SERVICE_MAX_ACTIVE_PER_AGENT} active listings each"
    assert cap in flat, f"rule 23 omits the live listing cap: {cap!r}"


def test_services_knobs_are_placeholders_not_literals():
    """Pin the template so a reintroduced literal cannot pass unnoticed.

    Without this, a future edit that hardcodes today's value would keep
    passing the render check for as long as the config still matched.
    """
    tpl = rules_text._RULES_TPL
    for knob in _KNOBS:
        assert "{" + knob + "}" in tpl, f"rule 23 lost its {{{knob}}} placeholder"


def test_create_service_docstring_states_no_services_number():
    """The tool description must name the knob, never the value.

    A tool docstring is a plain string literal and cannot interpolate config,
    so the honest shape is a pointer to the surface that does. That makes this
    an absence assertion rather than a value assertion, and absence holds on
    every deployment - which is the whole point.
    """
    src = (_ROOT / "server" / "tools" / "economy.py").read_text(encoding="utf-8")
    start = src.index("def create_service(")
    open_q = src.index('"""', start)
    close_q = src.index('"""', open_q + 3)
    doc = src[open_q : close_q + 3]
    for literal in ("0.1-12.5", "0.1 - 12.5", "At most 4 active listings"):
        assert literal not in doc, f"create_service still states {literal!r}"


if __name__ == "__main__":
    test_services_knobs_render_from_live_config()
    test_services_knobs_are_placeholders_not_literals()
    test_create_service_docstring_states_no_services_number()
    print("rule 23 services knob pins: ok")
