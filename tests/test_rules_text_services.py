"""tests/test_rules_text_services - rule 23's services knobs render from live config.

Rule 23 states the services-shelf price bounds and the active-listing cap.
All three are knobs (SERVICE_MIN_PRICE, SERVICE_MAX_PRICE,
SERVICE_MAX_ACTIVE_PER_AGENT) and db/_services.py already enforces every one
of them from config - but the citizen-facing prose hardcoded the numbers, so a
deployment override would falsify them with no test failing. The module's own
contract (rules_text._rules_text) is that every number resolves from config at
call time, so an .env edit shows up on the next get_rules().

These three pins hold that contract for the services sentence. Pin 3 is the
deployment-independent one: it asserts the VALUES are *absent* from the tool
description, which no override state can excuse. It parses the numbers out of
the prose and compares them numerically, so no rewording of the docstring can
reintroduce one (bug #B129).
"""

import os
import re
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
    an absence assertion - but the property worth asserting is about VALUES, so
    this parses the numbers out of the docstring and compares them NUMERICALLY.

    The previous version asserted the absence of three byte sequences
    ("0.1-12.5", "0.1 - 12.5", "At most 4 active listings"). That only ever
    proved those three spellings were absent: `4 active listings max` - the
    phrasing the repo's own AGENTS.md:522 already uses - passed it, and so
    would every future rewording (bug #B129). Numeric comparison has no
    spelling to miss, and derives from config so it follows the knobs instead
    of restating them, exactly as test_services_knobs_render_from_live_config
    does for the rendered sentence.
    """
    src = (_ROOT / "server" / "tools" / "economy.py").read_text(encoding="utf-8")
    start = src.index("def create_service(")
    open_q = src.index('"""', start)
    close_q = src.index('"""', open_q + 3)
    flat = " ".join(src[open_q : close_q + 3].split())

    # Positive half: the pin must not be satisfiable by deleting the sentence.
    assert "get_rules()" in flat, "create_service must point at the rendered surface"

    numbers = {float(m.group(0)) for m in re.finditer(r"\d+(?:\.\d+)?", flat)}

    # Price bounds. Numeric equality, so any separator, casing or ordering of
    # the same two numbers is the same assertion.
    for knob in ("SERVICE_MIN_PRICE", "SERVICE_MAX_PRICE"):
        value = float(getattr(config, knob))
        assert value not in numbers, (
            f"create_service docstring still states {knob}={value:g} (bug #B129)"
        )

    # The listing cap is scoped to a window around a listing word, and that
    # window is load-bearing rather than decorative. This docstring
    # legitimately states max_open_orders (1-10), ack_visits (default 2, within
    # 2-7), deliver_days (default 3, within 1-14), <= 255 chars and ack*24h - so
    # a bare "is the number absent" check would fire on a true sentence the day
    # the cap is configured to 2 or 10. Scoping keeps the pin honest instead of
    # trading vacuity for false reds.
    cap = float(config.SERVICE_MAX_ACTIVE_PER_AGENT)
    for m in re.finditer(r"\d+", flat):
        if float(m.group(0)) != cap:
            continue
        window = flat[max(0, m.start() - 60) : m.end() + 60]
        assert "listing" not in window.lower(), (
            f"create_service docstring states the listing cap {cap:g} beside "
            f"{window!r} (bug #B129)"
        )


if __name__ == "__main__":
    test_services_knobs_render_from_live_config()
    test_services_knobs_are_placeholders_not_literals()
    test_create_service_docstring_states_no_services_number()
    print("rule 23 services knob pins: ok")
