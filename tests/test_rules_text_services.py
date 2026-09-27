"""tests/test_rules_text_services - rule 23's services knobs render from live config.

Rule 23 states the services-shelf price bounds and the active-listing cap.
All three are knobs (SERVICE_MIN_PRICE, SERVICE_MAX_PRICE,
SERVICE_MAX_ACTIVE_PER_AGENT) and db/_services.py already enforces every one
of them from config - but the citizen-facing prose hardcoded the numbers, so a
deployment override would falsify them with no test failing. The module's own
contract (rules_text._rules_text) is that every number resolves from config at
call time, so an .env edit shows up on the next get_rules().

These three pins hold that contract for the services sentence.

On pin 3, note what it does and does not claim. It asserts the VALUES are
absent from the tool description, comparing parsed numbers numerically so no
rewording can reintroduce one (bug #B129). It is *not* deployment-independent,
and the earlier "no override state can excuse" wording was wrong: the forbidden
values are config-derived, so under an override the pin can false-fire on a
legitimate number and can excuse a stale literal whose knob has moved. What it
does guarantee is the property that matters at the shipped defaults, which is
where CI runs - see the per-branch comments for the exact boundary.
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

# Nearest-keyword distance, in whitespace-collapsed characters, from the
# numbers the live docstring legitimately states to a listing word:
#   10 -> 39 ("max_open_orders (1-10) caps ... on the listing.")
#    1 -> 42 (the same 1-10)
# and a reintroduced "4 active listings" puts "listing" 9 characters after the
# 4. A +/-20 window therefore clears both legitimate collisions (39, 42) while
# still catching a reintroduction, with margin on each side. Measured, not
# guessed - see the PR body.
_LISTING_WINDOW = 20


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
    spelling to miss.
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
    # the same two numbers is one assertion.
    #
    # KNOWN LIMITATION, stated rather than papered over: this branch is
    # unscoped, so a deployment that configures a price knob to one of the
    # docstring's other numbers false-fires on a true sentence - 255 sits 9
    # characters from "price_credits", 24 from nothing price-shaped but 255 is
    # the sharp case. A price-keyword proximity window was considered and
    # measured against the live bytes: it CANNOT fix this, because the
    # legitimate "255 chars). price_credits" is 9 chars from its keyword while
    # a reintroduced "12.5 credits" is only 8. No window width separates them.
    # The alternative - allowlisting the other numbers - is a literal list that
    # needs re-auditing on every reword, which is the bug this pin exists to
    # kill. Accepted deliberately: CI runs the defaults, where this branch has
    # full power, and the limitation is visible here rather than implied.
    for knob in ("SERVICE_MIN_PRICE", "SERVICE_MAX_PRICE"):
        value = float(getattr(config, knob))
        assert value not in numbers, (
            f"create_service docstring still states {knob}={value:g} (bug #B129)"
        )

    # The listing cap is scoped to a window around a listing word, because the
    # docstring legitimately states max_open_orders (1-10), ack_visits
    # (default 2, within 2-7), deliver_days (default 3, within 1-14),
    # <= 255 chars and ack*24h. The window is a proximity heuristic, not a
    # guarantee, and its width is measured against the live bytes rather than
    # guessed - see _LISTING_WINDOW above. A wider window would NOT be safe:
    # at +/-60 the "10" (39) and "1" (42) of "max_open_orders (1-10) caps
    # simultaneous open orders on the listing." both fall inside it, so a cap
    # configured to 10 or 1 fired on a true sentence.
    cap = float(config.SERVICE_MAX_ACTIVE_PER_AGENT)
    for m in re.finditer(r"\d+", flat):
        if float(m.group(0)) != cap:
            continue
        window = flat[max(0, m.start() - _LISTING_WINDOW) : m.end() + _LISTING_WINDOW]
        assert "listing" not in window.lower(), (
            f"create_service docstring states the listing cap {cap:g} beside "
            f"{window!r} (bug #B129)"
        )


if __name__ == "__main__":
    test_services_knobs_render_from_live_config()
    test_services_knobs_are_placeholders_not_literals()
    test_create_service_docstring_states_no_services_number()
    print("rule 23 services knob pins: ok")
