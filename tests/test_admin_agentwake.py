"""The /admin/agentwake control surface (proposal #806).

Modelled on tests/test_admin_ci_panel.py, which is the same kind of page:
a registry an operator edits through forms, with a credential in one of the
columns. The pins that matter here are the security-adjacent ones, because
this page is the only writable admin surface that can put text into OTHER
citizens' agent sessions:

- every POST refuses without auth, and refuses a bad/missing CSRF token;
- the bearer token never appears in the rendered HTML, and an empty edit
  field means "keep it", not "clear it";
- broadcasting is REFUSED on an unauthenticated panel, not merely
  annotated - _authorized is fail-open by default, and an open panel must
  not be able to message every registered agent;
- the destructive action confirms on the FORM tag, per house convention;
- the page says when the master switch is off, because a registered row on
  a dead poller looks exactly like a broken feature.

No pytest and no TestClient here: stub request objects plus asyncio.run,
like the farm panel's suite. No pytest in this repo.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_awake_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
# Open admin for most of this file (ADMIN_PASSWORD unset), which is what
# makes the open-panel refusal arms observable.
os.environ.pop("ADMIN_PASSWORD", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.datastructures import FormData  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
from server.admin import _agentwake as ag  # noqa: E402
from server.admin._auth import _CSRF_COOKIE, _csrf_token  # noqa: E402
from server.poller import _broadcast as bc  # noqa: E402
from server.poller import _wake as wake  # noqa: E402
from tests._setup import setup  # noqa: E402

AGENTS, _ = setup()


class _StubReq:
    """Minimal admin request: open admin, cookie-less CSRF, async form().

    The form is a real starlette FormData rather than a dict, because the
    broadcast handler reads it with `getlist` - a dict would raise
    AttributeError and the page would report a generic failure for every
    multi-valued field.
    """

    def __init__(self, form=None):
        self.headers = {}
        self.cookies = {}
        self.state = SimpleNamespace()
        self.query_params = {}
        pairs = [
            (key, value)
            for key, value in (form or {}).items()
            for value in (value if isinstance(value, (list, tuple)) else [value])
        ]
        self._form = FormData(pairs)

    async def form(self):
        return self._form


def _auth_header():
    """A Basic header matching whatever the live env asks for."""
    import base64

    user = os.environ.get("ADMIN_USER", "")
    pw = os.environ.get("ADMIN_PASSWORD", "")
    raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"authorization": f"Basic {raw}"}


def _authed(form=None):
    """Stub request with a minted CSRF token and a valid Basic header."""
    req = _StubReq(dict(form or {}))
    # The token is injected as a real field, because _csrf_ok reads it off
    # the parsed form - exactly where a browser would put it.
    req._form = FormData([*req._form.multi_items(), ("csrf", _csrf_token(req))])
    # Present so that a password-protected run authenticates; ignored by
    # _authorized when ADMIN_PASSWORD is unset (open admin).
    req.headers.update(_auth_header())
    return req


def _call(handler, form=None):
    return asyncio.run(handler(_authed(form)))


def _render():
    req = SimpleNamespace(
        cookies=SimpleNamespace(get=lambda _k, _d=None: _d),
        state=SimpleNamespace(csrf_token=""),
    )
    return ag._body(req)


def _register(agent_id, directory="dir", token="", enabled=True):
    """Idempotent, so a fixture may re-register across the file."""
    with db._conn(immediate=True) as conn:
        conn.execute("DELETE FROM agent_wake_endpoints WHERE agent_id = ?", (agent_id,))
    return wake.register_endpoint(
        agent_id, directory, "http://oc", token, enabled=enabled
    )


def _reset_registry():
    """Start a test from an empty registry.

    The arming refusal is per-form, not per-registry, but a leftover enabled
    row from an earlier test makes the open-panel assertions about the PAGE
    depend on test order - which is exactly the kind of coupling that makes
    a suite lie on a reordering.
    """
    with db._conn(immediate=True) as conn:
        conn.execute("DELETE FROM agent_wake_endpoints")


# --- wiring ----------------------------------------------------------------


def test_routes_are_wired():
    from server.admin import ROUTES

    found = {r.path: set(r.methods or set()) for r in ROUTES}
    assert found.get("/admin/agentwake") is not None, sorted(found)
    for leg in (
        "/admin/agentwake/register",
        "/admin/agentwake/update",
        "/admin/agentwake/remove",
        "/admin/agentwake/broadcast",
        "/admin/agentwake/broadcast-preview",
    ):
        assert found.get(leg) == {"POST"}, f"{leg}: {found.get(leg)}"


def test_nav_links_the_page():
    from server.admin._auth import _admin_nav

    assert 'href="/admin/agentwake"' in _admin_nav()


# --- rendering -------------------------------------------------------------


def test_page_renders_rows_without_ever_showing_the_token():
    aid = AGENTS["alpha"]["agent_id"]
    _register(aid, directory="AgentLand_AgentX_alpha", token="s3cr3t-tok")
    html = _render()
    assert "AgentLand_AgentX_alpha" in html, html[:400]
    assert "alpha" in html
    assert "s3cr3t-tok" not in html, "the bearer token rendered into the page"
    assert 'type="password"' in html, "the token input is not masked"
    assert "token stored - leave blank to keep" in html


def test_page_emits_no_mojibake_and_has_the_register_form():
    """Scoped rather than a blanket isascii().

    The shared `_ts_or_dash` helper renders a real em dash for a missing
    timestamp, so an absolute isascii() pin would fail on house code this
    page legitimately reuses - and `test_admin_mojibake.py` already records
    that em dashes are deliberately out of scope for the mojibake sweep.
    What must not appear is a mis-decoded sequence, so the pin is: the ONLY
    non-ascii character permitted is that one em dash, and it may only come
    from the helper.
    """
    from viewer._utils import _ts_or_dash

    html = _render()
    bad = sorted({c for c in html if ord(c) > 127 and c != "—"})
    assert not bad, (
        f"mojibake or a stray non-ascii char: {[(c, hex(ord(c))) for c in bad]}"
    )
    if "—" in html:
        assert _ts_or_dash(None) in html, "an em dash appeared from somewhere else"
    assert "/admin/agentwake/register" in html
    assert "agent_id" in html


def test_master_switch_banner_says_why_nothing_is_happening():
    saved = config.AGENT_WAKE_ENABLED
    try:
        config.AGENT_WAKE_ENABLED = 0
        off = _render()
        config.AGENT_WAKE_ENABLED = 1
        on = _render()
    finally:
        config.AGENT_WAKE_ENABLED = saved
    assert "The poller is OFF" in off, off[:600]
    assert "AGENT_WAKE_ENABLED" in off
    assert "arms nothing on its own" in off
    assert "The poller is OFF" not in on
    assert "is <b>on</b>" in on


def test_auto_refresh_ignores_prefilled_fields():
    """REGRESSION. The pause check used to ask "does any non-hidden input
    have a value?", which is true of every registered row's pre-filled
    directory/url - and a broadcast is impossible without a registered row.
    So the page would never have refreshed, and the "watch it below" notice
    would have hung forever behind a permanently-stale table.

    The fix scopes the check to `.typed` fields, so the pin asserts the
    pre-filled inputs are NOT marked and the message box IS.
    """
    _register(AGENTS["alpha"]["agent_id"], directory="AgentLand_AgentX_alpha")
    html = _render()
    assert 'name="directory" value="AgentLand_AgentX_alpha"' in html
    assert 'class="typed" name="directory" value=' not in html, (
        "a pre-filled input is marked .typed, so the page never refreshes"
    )
    assert 'class="typed" name="url" value=' not in html
    assert 'class="typed" name="token"' in html, "the token field is not .typed"
    assert 'class="typed" name="message"' in html, "the message box is not .typed"
    # And the script must key on the class, not on a blanket input sweep.
    assert 'querySelectorAll(".typed")' in ag._refresh_html()
    assert ":not([type=hidden])" not in ag._refresh_html(), (
        "the pause check still sweeps every visible input, so a pre-filled "
        "directory value pins the page open forever"
    )


def test_refresh_script_is_only_emitted_while_a_broadcast_runs():
    bid = bc.create_broadcast([AGENTS["alpha"]["agent_id"]], "hi")
    try:
        running_html = asyncio.run(ag.agent_wake_page(_StubReq())).body.decode()
        assert "auto-refresh" in running_html, "no poll script while work is in flight"
    finally:
        bc.repair_running()
    idle = _render()
    assert "auto-refresh" not in idle, "a finished page still carries a poll script"
    assert bc.get_broadcast(bid)["status"] == "abandoned"


def test_an_open_panel_refuses_to_write_the_registry_at_all():
    """Why this page needs a password for EVERY write, not just for arming.

    The earlier policy - "inert rows are harmless, so only arming needs a
    password" - did not hold up, and each part of this pin is one of the
    reasons:

      - `UNIQUE(agent_id)` means a visitor can SQUAT a citizen's id, so the
        operator's own later registration is refused until someone finds the
        unauthenticated delete. A feature that anyone can lock is not usable.
      - the banner tells the operator to set ADMIN_PASSWORD and restart -
        which is precisely the act that makes every row already in the table
        live. The fix instruction armed the payload.
      - the registry is READABLE on an open panel, so every citizen's
        `directory` and `url` - the operator's LAN topology - leaks to any
        visitor who can reach the server.
      - `directory` is consumed as a FILESYSTEM PATH by the opencode.json
        fallback, so a planted value is both a request target and a
        file-read primitive. (That leg is now also refused at the data
        layer by _check_directory, so the two defences overlap rather than
        depending on each other.)

    So: on a fail-open panel the mutable surface is refused outright. The
    page still RENDERS, so an operator can see the switch is off and what to
    set - it just cannot be written to.
    """
    aid = AGENTS["fresh"]["agent_id"]
    assert ag._panel_open() is True

    # Armed or inert, the refusal is the same: a row written here is a row
    # the operator's own later password would make deliverable.
    for form in (
        {
            "agent_id": str(aid),
            "directory": "d",
            "url": "http://169.254.169.254",
            "enabled": "1",
        },
        {"agent_id": str(aid), "directory": "d", "url": "http://127.0.0.1:4096"},
    ):
        resp = _call(ag.agent_wake_register, form)
        assert "ADMIN_PASSWORD" in resp.body.decode(), resp.body[:300]
        assert wake.endpoint_for_agent(aid, require_enabled=False) is None, (
            f"an open panel wrote a row from {form!r}"
        )

    # A password lifts the gate, so this is a real gate and not a blanket
    # denial: the operator's own registration is what was blocked.
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        ok = _call(
            ag.agent_wake_register,
            {
                "agent_id": str(aid),
                "directory": "d",
                "url": "http://127.0.0.1:4096",
                "enabled": "1",
            },
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert ok.status_code == 303, ok.status_code
    row = wake.endpoint_for_agent(aid)
    assert row is not None and int(row["enabled"]) == 1, row


def test_open_panel_cannot_ARM_via_the_edit_form_either():
    """The SECOND write path. Register was gated, then the edit form walked
    straight past it - the same hole wearing a different hat, which is why
    both handlers go through the same guard.

    And the direction matters: the guard must refuse ARMING on an open
    panel without refusing the operator's ability to turn something OFF.
    The earlier guard took the raw form value and tested it for truthiness,
    and `"0"` is truthy in Python - so on an open panel the page refused to
    DISABLE an armed endpoint, and answered an attempt to turn something off
    with the *enabling* message. A guard that blocks the safe direction
    pushes the operator to set a password rather than to ask why.
    """
    aid = AGENTS["theta"]["agent_id"]
    _reset_registry()
    endpoint_id = _register(aid, enabled=0)
    assert int(wake.endpoint_for_agent(aid, require_enabled=False)["enabled"]) == 0
    assert ag._panel_open() is True
    resp = _call(
        ag.agent_wake_update,
        {
            "endpoint_id": str(endpoint_id),
            "directory": "dir",
            "url": "http://oc",
            "enabled_set": "1",
            "enabled": "1",
        },
    )
    assert "ADMIN_PASSWORD" in resp.body.decode(), resp.body[:300]
    assert int(wake.endpoint_for_agent(aid, require_enabled=False)["enabled"]) == 0, (
        "an open panel armed the endpoint through the edit form"
    )
    # A password lifts the gate, so the refusal is a real gate and not a
    # blanket denial.
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        ok = _call(
            ag.agent_wake_update,
            {
                "endpoint_id": str(endpoint_id),
                "enabled_set": "1",
                "enabled": "1",
            },
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert ok.status_code == 303, ok.status_code
    assert int(wake.endpoint_for_agent(aid)["enabled"]) == 1


def test_arming_guard_does_not_block_the_safe_direction():
    """De-arming must never be gated, with or without a password.

    This is the leg that catches the truthiness bug directly: the form
    value for "turn it off" is the string "0", and a guard written as
    `if enabled_value and _panel_open()` reads that as arming. The refusal
    was therefore reported for the wrong action, on the wrong code path,
    and the operator could not disarm anything from a fail-open panel.
    """
    aid = AGENTS["gamma"]["agent_id"]
    _reset_registry()
    endpoint_id = _register(aid, enabled=1)
    assert int(wake.endpoint_for_agent(aid)["enabled"]) == 1
    # The bare enable/disable button posts `enabled` with no `enabled_set`.
    ag._guard_arm("0")
    ag._guard_arm(False)
    ag._guard_arm(None)
    # And the 0->1 transition on an OPEN panel is still refused, so the
    # previous three lines are not the whole story.
    try:
        ag._guard_arm(True)
    except db.ForumError as exc:
        assert "ADMIN_PASSWORD" in str(exc), exc
    else:
        raise AssertionError("arming was allowed on an open panel")
    # The same values through the real handler, on an open panel.
    refused = _call(
        ag.agent_wake_update, {"endpoint_id": str(endpoint_id), "enabled": "0"}
    )
    assert int(wake.endpoint_for_agent(aid)["enabled"]) == 1, (
        "de-arming was not the action that got through"
    )
    assert refused is not None


def test_a_second_real_broadcast_is_refused_while_one_runs():
    """'One at a time' must be a server invariant, not a UI claim.

    A double-clicked Send passes every gate twice and creates two running
    rows; the lock then serialises them, so the SECOND full fan-out fires
    after the first and spends money on a selection nobody re-ticked.
    """
    aid = AGENTS["alpha"]["agent_id"]
    _register(aid)
    first = bc.create_broadcast([aid], "one")
    try:
        try:
            bc.create_broadcast([aid], "two")
        except db.ForumError as exc:
            assert "already running" in str(exc), exc
        else:
            raise AssertionError("queued a second real broadcast while one ran")
        # A PREVIEW is still allowed - it contacts nobody.
        preview = bc.create_broadcast([aid], "peek", dry_run=True)
        assert preview
    finally:
        bc.repair_running()
    assert bc.get_broadcast(first)["status"] == "abandoned"


def test_confirm_attribute_survives_html_tokenization():
    """The onsubmit attribute must reach the JS parser intact.

    This is the test that let the bug through. The previous version sliced
    the RAW html string, asserted the literal started with a double quote,
    and asserted it contained no `&` - so it not only failed to check the
    attribute round trip, it actively FORBADE the fix: the correct escaper
    (`esc()`) necessarily emits `&quot;`, which that assertion rejected.
    The raw slice is the wrong level of abstraction entirely - a browser
    never sees it. The tokenizer decodes entities, and a double quote
    arriving in the RAW string ends the attribute right there.

    So: tokenize like a browser (stdlib `HTMLParser`, which applies
    character-reference decoding), then require the DECODED value to be
    `return confirm(<a valid JSON string>)` - a balanced, parseable
    literal naming the right agent. `json.loads` is the assertion that
    matters: it fails on the truncated `return confirm(` the old code
    produced, where the DOM saw an unterminated string.
    """
    from html.parser import HTMLParser

    class _Onsubmit(HTMLParser):
        """Collect onsubmit values the way a browser resolves them."""

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.values: list[str] = []

        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name == "onsubmit" and value is not None:
                    self.values.append(value)

    parser = _Onsubmit()
    parser.feed(_render())

    # Every remove button on the page, not "exactly one": the registry is
    # shared across this file, and a strict count would make the pin depend
    # on how many rows earlier tests left behind. What must hold is that
    # EVERY confirm attribute is well formed.
    removes = [
        v for v in parser.values if v.startswith("return confirm(") and v.endswith(")")
    ]
    assert removes, f"no well-formed onsubmit confirm found; saw {parser.values!r}"
    for value in parser.values:
        assert value.startswith("return confirm(") and value.endswith(")"), (
            f"a confirm attribute did not survive tokenization: {value!r}"
        )

    # The inner argument must be a COMPLETE, parseable string literal - this
    # is the exact line the old code failed: the tokenizer cut the attribute
    # at the literal's opening quote, leaving `return confirm(`.
    for value in removes:
        inner = value[len("return confirm(") : -1]
        decoded = json.loads(inner)
        assert decoded.startswith("Remove the wake endpoint for "), decoded
        assert decoded.endswith("?"), decoded
        # The agent name is carried, so the prompt names WHO is being cut
        # off rather than asking an abstract question.
        named = decoded[len("Remove the wake endpoint for ") : -1]
        assert named and named != "?", decoded


def test_blank_token_field_keeps_the_stored_token():
    """A blank field means "keep it", not "clear it" - pinned end to end.

    The old pin for this was `assert ("" or None) is None`: a tautology over
    two literals, asserting nothing about the program. It could not have
    failed for any edit to the handler. This drives the real HTTP handler
    with a genuinely blank field and reads the row back.
    """
    aid = AGENTS["beta"]["agent_id"]
    _reset_registry()
    endpoint_id = _register(aid, token="s3cr3t")
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        _call(
            ag.agent_wake_update,
            {
                "endpoint_id": str(endpoint_id),
                "directory": "dir",
                "url": "http://oc",
                "enabled_set": "1",
                "enabled": "1",
                "token": "",
            },
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    stored = wake.endpoint_for_agent(aid, require_enabled=False)["token"]
    assert stored == "s3cr3t", (
        f"a blank token field CLEARED the stored credential: {stored!r}"
    )


def test_token_can_be_explicitly_cleared():
    """A blank field means 'keep it', so clearing needs its own intent -
    otherwise a stored credential is only revocable by deleting the row."""
    aid = AGENTS["beta"]["agent_id"]
    _reset_registry()
    endpoint_id = _register(aid, token="s3cr3t")
    # The affordance's presence is asserted BEFORE the clear, while a token
    # is still stored - a post-clear check alone would pass even if the
    # affordance never rendered at all.
    assert wake.endpoint_for_agent(aid)["token"] == "s3cr3t", "fixture did not store"
    assert "clear stored token" in _edit_block_for(_render(), endpoint_id), (
        "no clear affordance on a row that HAS a token"
    )
    # The edit form carries `enabled` because the box is ticked, and a ticked
    # box is arming - so this leg needs a password for that reason, not for
    # the token clear itself.
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        _call(
            ag.agent_wake_update,
            {
                "endpoint_id": str(endpoint_id),
                "directory": "dir",
                "url": "http://oc",
                "enabled_set": "1",
                "enabled": "1",
                "token_clear": "1",
            },
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert wake.endpoint_for_agent(aid)["token"] == "", "the token survived a clear"
    # And it is GONE once nothing is stored - offering "clear" on a row with
    # no token is a control that lies about what it will do.
    _register(aid, token="")
    assert "clear stored token" not in _edit_block_for(_render(), endpoint_id), (
        "a clear control is offered on a row with no stored token"
    )


def _edit_block_for(html, endpoint_id):
    """The <details>edit</details> block for one endpoint id, or "".

    Scoped to the details block because the toggle and remove forms in the
    same row repeat the same endpoint_id; matching the raw id alone would
    hand back the enable button's form and assert against the wrong element.
    """
    marker = "<details><summary>edit</summary>"
    if marker not in html:
        return ""
    tail = html[html.index(marker) :]
    end = tail.find("</details>")
    block = tail[: end + len("</details>")] if end > 0 else tail
    return block if f'value="{endpoint_id}"' in block else ""


def test_the_csrf_cookie_round_trip_a_browser_actually_performs():
    """The one gap my review of this PR left with NO executed evidence.

    Every other test here drives a handler with a cookie-less stub, so
    `_csrf_ok` only ever saw its `request.state` fallback - the
    `SameSite=Lax` cookie that IS the cross-site defence was never involved,
    and nothing asserted `Set-Cookie`. If `agent_wake_page` stopped routing
    through `_admin_page`, every other pin in this file would still pass and
    the real page would 200 with "CSRF token missing" on every submission.

    So this drives the real page handler, reads the cookie it sets, and then
    POSTs with that cookie carried back - the sequence a browser performs -
    and asserts the write is ACCEPTED. The negative leg matters as much: a
    mismatched cookie must still be refused, or the round trip proves only
    that `_csrf_ok` is absent, not that it works.
    """
    aid = AGENTS["delta"]["agent_id"]
    _reset_registry()
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        # 1. The page renders and sets the cookie.
        page_req = _StubReq()
        page_req.headers.update(_auth_header())
        page_req.cookies = {}
        rendered = asyncio.run(ag.agent_wake_page(page_req))
        assert rendered.status_code == 200, rendered.status_code
        set_cookie = rendered.headers.get("set-cookie") or ""
        assert f"{_CSRF_COOKIE}=" in set_cookie, (
            f"the agentwake page set no CSRF cookie: {set_cookie!r}"
        )
        cookie_token = set_cookie.split(f"{_CSRF_COOKIE}=", 1)[1].split(";", 1)[0]
        assert cookie_token, "the CSRF cookie is empty"

        # 2. The rendered form carries the SAME token, so a browser's
        #    hidden field and its cookie agree.
        body = rendered.body.decode()
        assert f'name="csrf" value="{cookie_token}"' in body, (
            "the form field and the cookie disagree"
        )

        # 3. A POST carrying that cookie back is accepted - the round trip.
        ok_req = _StubReq(
            {
                "agent_id": str(aid),
                "directory": "dir",
                "url": "http://oc",
                "csrf": cookie_token,
            }
        )
        ok_req.cookies = {_CSRF_COOKIE: cookie_token}
        ok_req.headers.update(_auth_header())
        accepted = asyncio.run(ag.agent_wake_register(ok_req))
        assert accepted.status_code == 303, (
            f"the round-trip POST was refused: {accepted.status_code} "
            f"{accepted.headers.get('location')}"
        )
        assert "notice=error" not in (accepted.headers.get("location") or ""), (
            "the round-trip POST reported an error notice"
        )
        assert wake.endpoint_for_agent(aid, require_enabled=False) is not None, (
            "the round-trip POST did not write the row"
        )

        # 4. A mismatched cookie is still refused, so (3) is not passing
        #    because the check is missing.
        bad_req = _StubReq(
            {
                "endpoint_id": str(_register(aid)),
                "enabled": "0",
                "csrf": "a-different-token",
            }
        )
        bad_req.cookies = {_CSRF_COOKIE: cookie_token}
        bad_req.headers.update(_auth_header())
        refused = asyncio.run(ag.agent_wake_update(bad_req))
        assert refused.status_code == 200, (
            "a mismatched CSRF token was accepted - the check is not running"
        )
        assert "CSRF" in refused.body.decode(), refused.body[:300]
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)


def _newest_broadcast():
    """The most recent broadcast row whatever its status.

    Reading through `active_broadcast()` is a race: the handler spawns the
    walk as a fire-and-forget task, and `asyncio.run` gives that task a turn
    before the assertion runs - so a one-agent preview can already be `done`
    by the time the test looks. That is correct behaviour (a preview
    contacts nobody, so it finishes instantly) and a bad thing to assert
    against. The pin is about WHICH row was created, so it asks for the
    newest row by recency.
    """
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_wake_broadcasts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row is not None else None


def test_preview_handler_creates_a_dry_run_row_and_sends_nothing():
    """REGRESSION. `dry_run=True` was pinned only at the _broadcast layer,
    never at the handler - so deleting the keyword at the call site would
    have turned the read-only Preview button into a real fan-out with no
    test going red. The pin has to drive the HANDLER."""
    aid = AGENTS["gamma"]["agent_id"]
    _register(aid)
    sent: list[str] = []
    real = wake.send_wake
    wake.send_wake = lambda endpoint, session_id, text: sent.append(session_id) or True
    real_sel = wake.select_session
    wake.select_session = lambda endpoint, directory: {"id": "ses_x", "model": {}}
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(
            ag.agent_wake_broadcast_preview, {"agent": [str(aid)], "message": "hi"}
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
        wake.send_wake = real
        wake.select_session = real_sel
    assert resp.status_code == 303, resp.status_code
    row = _newest_broadcast()
    assert row is not None, (
        "the preview handler queued no row at all; redirect said "
        f"{resp.headers.get('location')!r}"
    )
    # The row the PAGE created, read back by id - not whatever a direct
    # create_broadcast(dry_run=True) would have left, which is the layer
    # this pin exists to cover.
    queued = bc.get_broadcast(row["id"])
    assert queued["dry_run"] == 1, (
        f"the preview was queued as a REAL send: {queued['dry_run']!r} "
        "- deleting dry_run=True at the handler turns Preview into a fan-out"
    )
    assert not sent, "the preview handler contacted a session"


def test_write_failures_are_reported_not_swallowed():
    """`notice=no-rows` and `notice=error` are the two paths that stop a
    failed write from reading as success - the exact failure the generic
    handler's own comment says it exists to prevent. Neither was driven."""
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        missing = _call(ag.agent_wake_update, {"endpoint_id": "999999", "enabled": "1"})
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert missing.status_code == 303, f"expected a redirect, got {missing.status_code}"
    assert "notice=no-rows" in missing.headers["location"], missing.headers["location"]

    aid = AGENTS["delta"]["agent_id"]
    endpoint_id = _register(aid, directory="unchanged")
    real = wake.update_endpoint
    wake.update_endpoint = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk"))
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(
            ag.agent_wake_update, {"endpoint_id": str(endpoint_id), "enabled": "1"}
        )
    finally:
        wake.update_endpoint = real
        os.environ.pop("ADMIN_PASSWORD", None)
    # The generic arm flashes rather than redirecting, so the pin reads the
    # rendered notice. What matters is that it says FAILED and that the row
    # is untouched - not that it took the 303 path.
    assert "the change failed" in resp.body.decode(), resp.body[:300]
    assert "notice=updated" not in resp.headers.get("location", "")
    assert wake.endpoint_for_agent(aid)["directory"] == "unchanged"


def test_open_panel_refuses_to_broadcast_and_says_why():
    assert ag._panel_open() is True, "this file runs with ADMIN_PASSWORD unset"
    html = _render()
    assert "admin panel is unauthenticated" in html, html[:600]
    # BOTH buttons carry the disabled attribute. The message box stays so
    # the operator can see the feature exists.
    assert 'type="submit" disabled>send</button>' in html
    assert 'type="submit" disabled>preview (sends nothing)</button>' in html
    assert "textarea" in html
    # Registering without arming stays available - an inert row contacts
    # nobody - so the page is still useful on an open panel.
    assert "/admin/agentwake/register" in html


def test_with_a_password_the_broadcast_arms():
    """The ARMED/disabled state is the thing under test, so the pin has to
    name the attribute - `">send<"` is the same substring either way, which
    made the previous version of this test pass with the button disabled."""
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        assert ag._panel_open() is False
        html = _render()
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert "unauthenticated" not in html
    assert 'type="submit">send</button>' in html, "the send button is not armed"
    assert 'type="submit" disabled>send</button>' not in html


def test_remove_form_confirms_on_the_form_tag():
    aid = AGENTS["beta"]["agent_id"]
    _register(aid)
    html = _render()
    marker = 'action="/admin/agentwake/remove"'
    idx = html.index(marker)
    start = html.rindex("<form", 0, idx)
    end = html.index(">", idx)
    tag = html[start : end + 1]
    assert "onsubmit=" in tag, f"the confirm is not on the <form> tag: {tag!r}"
    assert "confirm(" in tag, tag


def test_broadcast_box_states_that_it_bypasses_the_budget():
    """The cost line is the honesty requirement.

    A manual send is not rationed by the daily wake budget, and the page
    says so in the same breath as the agent count - a UI implying otherwise
    would be lying in the direction that costs money.
    """
    html = _render()
    assert "does <b>not</b> draw on the automatic-wake" in html, html[:800]
    assert "ignores quiet hours" in html
    assert str(bc.MAX_MESSAGE_CHARS) in html


# --- POST handlers ---------------------------------------------------------


def test_register_creates_a_row():
    """Arming needs a password, so the ARMED case is driven with one set."""
    aid = AGENTS["gamma"]["agent_id"]
    _reset_registry()
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(
            ag.agent_wake_register,
            {
                "agent_id": str(aid),
                "directory": "d1",
                "url": "http://host:4096",
                "enabled": "1",
            },
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert resp.status_code == 303, f"expected a redirect, got {resp.status_code}"
    assert "notice=registered" in resp.headers["location"]
    row = wake.endpoint_for_agent(aid)
    assert row["directory"] == "d1", row
    assert int(row["enabled"]) == 1


def test_register_surfaces_a_refusal_verbatim():
    aid = AGENTS["gamma"]["agent_id"]
    _register(aid)
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(
            ag.agent_wake_register,
            {"agent_id": str(aid), "directory": "d1", "url": "http://h"},
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert resp.status_code == 200, resp
    assert "already has an endpoint" in resp.body.decode(), resp.body[:300]


def test_register_refuses_a_bad_url():
    aid = AGENTS["delta"]["agent_id"]
    _reset_registry()
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(
            ag.agent_wake_register,
            {"agent_id": str(aid), "directory": "d1", "url": "ftp://h"},
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert "http://" in resp.body.decode(), resp.body[:300]
    assert wake.endpoint_for_agent(aid, require_enabled=False) is None, (
        "a refused form still wrote a row"
    )


def test_register_refuses_a_directory_that_escapes_the_tree():
    """The escape guard is at the DATA layer, so it holds for every caller -
    the page, a CLI, anything. Pinned here because this form is the one
    place a stranger's typed string reaches it.

    `../../../../etc` is not a cosmetic input: `directory` is opened as a
    FILESYSTEM PATH by the opencode.json fallback, so this value is an
    arbitrary-file-read probe aimed at the server, not at the operator's
    own machine.
    """
    aid = AGENTS["delta"]["agent_id"]
    _reset_registry()
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(
            ag.agent_wake_register,
            {"agent_id": str(aid), "directory": "../../../../etc", "url": "http://h"},
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert "directory" in resp.body.decode(), resp.body[:300]
    assert wake.endpoint_for_agent(aid, require_enabled=False) is None, (
        "an escaping directory still wrote a row"
    )


def test_remove_deletes_the_row():
    aid = AGENTS["eta"]["agent_id"]
    endpoint_id = _register(aid)
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(ag.agent_wake_remove, {"endpoint_id": str(endpoint_id)})
        assert resp.status_code == 303
        assert "notice=removed" in resp.headers["location"]
        assert wake.endpoint_for_agent(aid, require_enabled=False) is None
        again = _call(ag.agent_wake_remove, {"endpoint_id": str(endpoint_id)})
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert "notice=gone" in again.headers["location"]


def test_update_toggles_enabled_without_touching_anything_else():
    aid = AGENTS["epsilon"]["agent_id"]
    endpoint_id = _register(aid, directory="keepme", token="s3cr3t")
    # The bare toggle form: an explicit value, no marker key. A password is
    # set because the arming guard reads `enabled` on this path too, and
    # this fixture starts enabled.
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(
            ag.agent_wake_update, {"endpoint_id": str(endpoint_id), "enabled": "0"}
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert resp.status_code == 303, resp
    row = wake.endpoint_for_agent(aid, require_enabled=False)
    assert int(row["enabled"]) == 0, row
    assert row["directory"] == "keepme", row
    assert row["token"] == "s3cr3t", "the toggle wiped the token"


def test_update_edit_form_can_turn_enabled_off():
    """The unticked-checkbox case.

    An unticked box sends NO `enabled` key, which is indistinguishable from a
    form that does not manage the flag - so the edit form carries an
    `enabled_set` marker. Without honouring it, an operator could never
    disable an endpoint from the edit form at all.
    """
    aid = AGENTS["zeta"]["agent_id"]
    endpoint_id = _register(aid)
    # enabled_set present, enabled absent -> unticked -> off. Disabling is
    # not arming, so this leg needs a password only because EVERY write on
    # an unauthenticated panel is refused (see
    # test_an_open_panel_refuses_to_write_the_registry_at_all) - the row
    # would otherwise be a squat, and a squat the operator's own later
    # password would make live.
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        _call(
            ag.agent_wake_update,
            {
                "endpoint_id": str(endpoint_id),
                "directory": "d",
                "url": "http://oc",
                "enabled_set": "1",
            },
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert int(wake.endpoint_for_agent(aid, require_enabled=False)["enabled"]) == 0
    # enabled_set present + enabled=1 -> on. That IS arming, so it is
    # refused twice over: by the write gate and by _guard_arm.
    # See test_open_panel_cannot_ARM_via_the_edit_form_either.
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        _call(
            ag.agent_wake_update,
            {
                "endpoint_id": str(endpoint_id),
                "enabled_set": "1",
                "enabled": "1",
            },
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert int(wake.endpoint_for_agent(aid)["enabled"]) == 1


def test_update_refuses_a_non_integer_id():
    resp = _call(ag.agent_wake_update, {"endpoint_id": "abc", "enabled": "1"})
    assert "not an integer" in resp.body.decode(), resp.body[:300]


# --- auth and CSRF on every POST -------------------------------------------

_POSTS = (
    (
        "register",
        ag.agent_wake_register,
        {"agent_id": "1", "directory": "d", "url": "http://h"},
    ),
    ("update", ag.agent_wake_update, {"endpoint_id": "1", "enabled": "1"}),
    ("remove", ag.agent_wake_remove, {"endpoint_id": "1"}),
    ("broadcast", ag.agent_wake_broadcast, {"agent": ["1"], "message": "hi"}),
    ("preview", ag.agent_wake_broadcast_preview, {"agent": ["1"], "message": "hi"}),
)


def test_every_post_refuses_a_missing_csrf_token():
    for name, handler, form in _POSTS:
        req = _StubReq(dict(form))  # deliberately no csrf key
        resp = asyncio.run(handler(req))
        assert resp.status_code == 200, f"{name}: {resp.status_code}"
        assert "CSRF" in resp.body.decode(), f"{name}: {resp.body[:200]}"
    # And nothing was written by any of them.
    assert not bc.active_broadcast(), "a CSRF-less POST queued a broadcast"


def test_every_post_refuses_an_unauthenticated_caller():
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        for name, handler, form in _POSTS:
            req = _authed(form)
            req.headers.pop(
                "authorization", None
            )  # authenticated-looking CSRF, no Basic
            resp = asyncio.run(handler(req))
            assert resp.status_code == 401, f"{name}: {resp.status_code}"
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert not bc.active_broadcast(), "an unauthenticated POST queued a broadcast"


def test_page_refuses_an_unauthenticated_caller():
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = asyncio.run(ag.agent_wake_page(_StubReq()))
        assert resp.status_code == 401, resp.status_code
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)


# --- broadcast from the page ------------------------------------------------


def test_broadcast_is_refused_on_an_open_panel():
    aid = AGENTS["theta"]["agent_id"]
    _register(aid)
    assert ag._panel_open() is True
    resp = _call(ag.agent_wake_broadcast, {"agent": [str(aid)], "message": "hi"})
    assert resp.status_code == 200, resp.status_code
    assert "disabled while the admin panel is unauthenticated" in resp.body.decode()
    assert bc.active_broadcast() is None, "an open panel queued a broadcast"


def test_broadcast_queues_a_row_and_returns_immediately():
    """The handler must not block: a six-agent broadcast is six minutes."""
    aid = AGENTS["theta"]["agent_id"]
    _register(aid)
    before = _newest_broadcast()
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(ag.agent_wake_broadcast, {"agent": [str(aid)], "message": "hi"})
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert resp.status_code == 303, resp.status_code
    assert "notice=queued" in resp.headers["location"]
    # By recency, not by liveness: the walk is a fire-and-forget task and
    # `asyncio.run` can hand it a turn before this line, so a one-agent
    # broadcast may already be `done`. What is pinned is that the handler
    # created the row at all.
    row = _newest_broadcast()
    assert row is not None and row["id"] != (before or {}).get("id"), (
        "no broadcast row was created"
    )
    assert row["total"] == 1, row
    # And it was a REAL broadcast, not a preview: the page has two buttons
    # and they must not have converged.
    assert int(row["dry_run"]) == 0, row
    # A stranded running row is repaired on the next boot, exactly as a
    # restart would.
    assert bc.repair_running() == (1 if row["status"] == "running" else 0)


def test_broadcast_surfaces_an_empty_selection():
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(ag.agent_wake_broadcast, {"message": "hi"})
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert "tick at least one agent" in resp.body.decode(), resp.body[:300]


def test_broadcast_enforces_the_fan_out_cap_at_the_page_too():
    os.environ["ADMIN_PASSWORD"] = "secret"
    saved = config.AGENT_WAKE_BROADCAST_MAX_AGENTS
    config.AGENT_WAKE_BROADCAST_MAX_AGENTS = 1
    try:
        resp = _call(
            ag.agent_wake_broadcast,
            {"agent": ["1", "2", "3"], "message": "hi"},
        )
    finally:
        config.AGENT_WAKE_BROADCAST_MAX_AGENTS = saved
        os.environ.pop("ADMIN_PASSWORD", None)
    assert "at most 1" in resp.body.decode(), resp.body[:300]
    assert bc.active_broadcast() is None, "an over-cap broadcast still queued"


# --- the operator-chosen broadcast gap (proposal #814) -----------------------


def _record_run_broadcast(calls):
    """Swap `run_broadcast` for a recorder, so the kwarg is observable.

    A plain function, not an `async def`: the handler hands the return value
    straight to `asyncio.create_task`, so a normal function records
    SYNCHRONOUSLY and returns a coroutine to satisfy that call. An `async def`
    stub would only record if the spawned task were allowed to run, and
    `asyncio.run` is under no obligation to run it - the capture would be a
    race and the pin a coin flip.
    """

    def fake(broadcast_id, *, gap_seconds=None):
        calls.append(gap_seconds)

        async def _noop():
            return None

        return _noop()

    return fake


def test_the_gap_box_shows_the_live_knob_not_a_literal():
    import re

    saved = config.AGENT_WAKE_BROADCAST_GAP_SECONDS
    config.AGENT_WAKE_BROADCAST_GAP_SECONDS = 137
    try:
        html = _render()
    finally:
        config.AGENT_WAKE_BROADCAST_GAP_SECONDS = saved
    tag = re.search(r'<input[^>]*name="gap"[^>]*>', html)
    assert tag, "the send form has no gap box"
    assert 'value="137"' in tag.group(0), tag.group(0)
    # type=text, NOT type=number, and no min/max/step. A number input's
    # value-sanitisation algorithm turns an invalid entry into "", so a browser
    # would submit a typo as BLANK and the server would answer with the
    # CONFIGURED default - a silent, different broadcast from the one typed,
    # which is the failure this form exists to prevent. Client constraints are
    # also only a bubble; the server refusal names the offending value.
    assert 'type="text"' in tag.group(0), tag.group(0)
    assert 'inputmode="numeric"' in tag.group(0), tag.group(0)
    assert "min=" not in tag.group(0), tag.group(0)
    assert "max=" not in tag.group(0), tag.group(0)


def test_an_out_of_range_configured_gap_is_never_pre_filled():
    """The knob is a live, UNVALIDATED env read, so it can be unsendable.

    `config.__getattr__` applies only `int` and re-reads on every access, so
    -1 or 99999 is accepted silently. Pre-filling either would make the box's
    OWN default fail its own parser - and because a cleared box delegates to
    the knob, the operator would be told they "typed" a figure they never
    touched. The configured number is still named in the copy, so the page
    does not quietly lie about it either.
    """
    import re

    saved = config.AGENT_WAKE_BROADCAST_GAP_SECONDS
    try:
        for bad in (-1, bc.MAX_GAP_SECONDS + 1):
            config.AGENT_WAKE_BROADCAST_GAP_SECONDS = bad
            html = _render()
            tag = re.search(r'<input[^>]*name="gap"[^>]*>', html)
            assert tag, "the send form has no gap box"
            assert "value=" not in tag.group(0), (bad, tag.group(0))
            assert "outside the 0-" in html, (bad, html[:400])
    finally:
        config.AGENT_WAKE_BROADCAST_GAP_SECONDS = saved


def test_a_blank_gap_means_the_config_default_never_zero():
    # THE arm worth pinning. `int(form.get("gap") or 0)` - the obvious
    # one-liner - satisfies every other case here and turns "left blank" into
    # "no pause at all", which is the opposite of what the knob exists to
    # prevent. `None` is the only value that keeps the default resolved in
    # the engine, at send time, where the knob is actually read.
    for extra in ({}, {"gap": ""}, {"gap": "   "}):
        form = {"agent": ["1"], "message": "hi", **extra}
        got = ag._gap_seconds(asyncio.run(_StubReq(form).form()))
        assert got is None, (extra, got)


def test_a_typed_gap_reaches_the_send_and_writes_the_row():
    calls = []
    saved_run = bc.run_broadcast
    before = _newest_broadcast()
    bc.run_broadcast = _record_run_broadcast(calls)
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(
            ag.agent_wake_broadcast, {"agent": ["1"], "message": "hi", "gap": "250"}
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
        bc.run_broadcast = saved_run
        # The stubbed task never runs, so the row would sit `running` and lock
        # out the next broadcast on the one-at-a-time invariant.
        bc.repair_running()
    assert resp.status_code == 303, resp.status_code
    # The CONTROL half of this arm: a valid gap is honoured, not refused.
    # 303 happens only when create_broadcast returned, so this is also the
    # proof a row was written - which is what gives the refusal arm its teeth.
    assert (_newest_broadcast() or {}).get("id") != (before or {}).get("id")
    assert calls == [250], calls


def test_a_bad_gap_is_refused_without_writing_a_row():
    before = _newest_broadcast()
    bodies = {}
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        # chr(0xB2) is U+00B2 SUPERSCRIPT TWO: `isdigit()` calls it True and
        # `int()` then raises ValueError, so a naive int() would fall into the
        # generic error flash instead of naming the problem. Spelled chr() on
        # purpose - this file's writer normalises non-ASCII on the way in.
        for bad in ("12.5", "abc", "1_000", "2e3", "-5", "99999", chr(0xB2)):
            resp = _call(
                ag.agent_wake_broadcast, {"agent": ["1"], "message": "hi", "gap": bad}
            )
            body = resp.body.decode()
            bodies[bad] = body
            assert resp.status_code == 200, (bad, resp.status_code)
            assert "gap" in body.lower(), (bad, body[:200])
            # The message must NAME the value. That is the only thing
            # separating the two refusal arms, and a ForumError("invalid
            # gap") would satisfy every assertion above this line.
            assert str(bad) in body, (bad, body[:200])
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
    assert (_newest_broadcast() or {}).get("id") == (before or {}).get("id"), (
        "a refused gap still queued a broadcast"
    )
    # ...and the two arms are not one message wearing different numbers.
    assert bodies["12.5"] != bodies["99999"], "one refusal text for both arms"


def test_the_preview_still_runs_with_no_gap():
    calls = []
    saved_run = bc.run_broadcast
    bc.run_broadcast = _record_run_broadcast(calls)
    os.environ["ADMIN_PASSWORD"] = "secret"
    try:
        resp = _call(
            ag.agent_wake_broadcast_preview,
            {"agent": ["1"], "message": "hi", "gap": "900"},
        )
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
        bc.run_broadcast = saved_run
        bc.repair_running()
    assert resp.status_code == 303, resp.status_code
    # A preview contacts nobody and exists to show the results table fast, so
    # a typed gap must not leak into it.
    assert calls == [0], calls
    # ...and the box is on the send form only, so it cannot be misread as
    # governing the preview button sitting directly above it.
    assert _render().count('name="gap"') == 1


def test_the_cost_line_names_the_gap_instead_of_claiming_immediately():
    # The bare word "immediately" SURVIVES in the new copy ("the first
    # immediately"), so pinning it would be vacuous. The old SENTENCE is the
    # invariant: the page must not describe a broadcast as starting every
    # ticked agent at once, and must name the gap in its place.
    html = _render()
    assert "ticked agent, immediately" not in html, html[:400]
    assert "one every" in html, html[:400]


def test_main_registers_every_test_in_this_module():
    import inspect
    import re

    text = Path(__file__).resolve().read_text(encoding="utf-8")
    body = text.split("def main():", 1)[1]
    registered = {
        name for name in re.findall(r"^\s+(test_[A-Za-z0-9_]+),\s*$", body, re.M)
    }
    defined = {
        name
        for name, fn in globals().items()
        if name.startswith("test_") and inspect.isfunction(fn)
    }
    missing = sorted(defined - registered)
    assert not missing, f"defined but never run by main(): {missing}"


def main():
    tests = [
        test_routes_are_wired,
        test_nav_links_the_page,
        test_page_renders_rows_without_ever_showing_the_token,
        test_page_emits_no_mojibake_and_has_the_register_form,
        test_master_switch_banner_says_why_nothing_is_happening,
        test_auto_refresh_ignores_prefilled_fields,
        test_refresh_script_is_only_emitted_while_a_broadcast_runs,
        test_an_open_panel_refuses_to_write_the_registry_at_all,
        test_open_panel_cannot_ARM_via_the_edit_form_either,
        test_arming_guard_does_not_block_the_safe_direction,
        test_a_second_real_broadcast_is_refused_while_one_runs,
        test_the_csrf_cookie_round_trip_a_browser_actually_performs,
        test_confirm_attribute_survives_html_tokenization,
        test_blank_token_field_keeps_the_stored_token,
        test_token_can_be_explicitly_cleared,
        test_preview_handler_creates_a_dry_run_row_and_sends_nothing,
        test_write_failures_are_reported_not_swallowed,
        test_open_panel_refuses_to_broadcast_and_says_why,
        test_with_a_password_the_broadcast_arms,
        test_remove_form_confirms_on_the_form_tag,
        test_broadcast_box_states_that_it_bypasses_the_budget,
        test_register_creates_a_row,
        test_register_surfaces_a_refusal_verbatim,
        test_register_refuses_a_directory_that_escapes_the_tree,
        test_register_refuses_a_bad_url,
        test_update_toggles_enabled_without_touching_anything_else,
        test_update_edit_form_can_turn_enabled_off,
        test_update_refuses_a_non_integer_id,
        test_remove_deletes_the_row,
        test_every_post_refuses_a_missing_csrf_token,
        test_every_post_refuses_an_unauthenticated_caller,
        test_page_refuses_an_unauthenticated_caller,
        test_broadcast_is_refused_on_an_open_panel,
        test_broadcast_queues_a_row_and_returns_immediately,
        test_broadcast_surfaces_an_empty_selection,
        test_broadcast_enforces_the_fan_out_cap_at_the_page_too,
        test_the_gap_box_shows_the_live_knob_not_a_literal,
        test_an_out_of_range_configured_gap_is_never_pre_filled,
        test_a_blank_gap_means_the_config_default_never_zero,
        test_a_typed_gap_reaches_the_send_and_writes_the_row,
        test_a_bad_gap_is_refused_without_writing_a_row,
        test_the_preview_still_runs_with_no_gap,
        test_the_cost_line_names_the_gap_instead_of_claiming_immediately,
        test_main_registers_every_test_in_this_module,
    ]
    failed = []
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:
            failed.append(fn.__name__)
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        print(f"\n{len(failed)}/{len(tests)} FAILED: {failed}")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")


if __name__ == "__main__":
    main()
