"""
server/admin/_auth.py — shared auth, CSRF, layout helpers.

Extracted from server/admin.py (3,718 lines) — the deliberately writable
admin surface. Imported by every other admin leaf; no leaf imports another
leaf at top-level (except via lazy imports inside functions) to avoid cycles.
"""

from __future__ import annotations

import base64
import os
import secrets
from urllib.parse import urlparse

from starlette.responses import HTMLResponse, RedirectResponse

import config  # noqa: F401 — kept for ADMIN_USER re-export parity
import db
from viewer._layout import _page
from viewer._utils import esc

ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

_CSRF_COOKIE = "admin_csrf"

# For test_admin_http's `importlib.reload(admin)` pattern: handlers must
# read the live env, not the import-time snapshot, because the package
# facade `server/admin/__init__.py` re-exports these names from _auth.
# Reloading the facade does not reload _auth, so the check must be live.


def _live_admin_user() -> str:
    return os.environ.get("ADMIN_USER", "")


def _live_admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "")


def _authorized(request) -> bool:
    # Live env read — see note above.
    _pw = os.environ.get("ADMIN_PASSWORD", "")
    if not _pw:
        return True

    header = request.headers.get("authorization", "")

    if not header.startswith("Basic "):
        return False

    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()

        user, _, pw = decoded.partition(":")

        _user = os.environ.get("ADMIN_USER", "")
        return secrets.compare_digest(user, _user) and secrets.compare_digest(pw, _pw)

    except Exception:
        return False


def _admin_user(request) -> str:
    """The authenticated admin's username, for the audit trail. Falls back to

    'admin' when no password is configured (open admin)."""

    header = request.headers.get("authorization", "")

    if header.startswith("Basic "):
        try:
            user, _, _ = (
                base64.b64decode(header.split(" ", 1)[1]).decode().partition(":")
            )

            return user

        except Exception:
            pass

    return "admin"


def _denied() -> HTMLResponse:

    return HTMLResponse(
        "<h1>401 Unauthorized</h1><p>This page is protected. "
        "Set ADMIN_PASSWORD and log in.</p>",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="AgentLand"'},
    )


def _csrf_token(request) -> str:
    """The CSRF token for this render: the existing cookie, or a fresh one

    stashed on request.state so the form and the response cookie agree."""

    token = request.cookies.get(_CSRF_COOKIE)

    if not token:
        token = getattr(request.state, "csrf_token", None) or secrets.token_urlsafe(16)

        request.state.csrf_token = token

    return token


def _csrf_field(request) -> str:

    return f'<input type="hidden" name="csrf" value="{esc(_csrf_token(request))}">'


def _csrf_ok(request, form) -> bool:

    supplied = str(form.get("csrf") or "")

    token = request.cookies.get(_CSRF_COOKIE) or getattr(
        request.state, "csrf_token", ""
    )

    return bool(token) and secrets.compare_digest(token, supplied)


def _admin_page(request, title: str, body: str) -> HTMLResponse:
    """_page() plus a SameSite=Lax CSRF cookie so the page's forms can POST."""

    response = _page(title, body)

    token = _csrf_token(request)

    if token:
        response.set_cookie(_CSRF_COOKIE, token, httponly=True, samesite="lax")

    return response


# ---------------------------------------------------------------- helpers --


def _flash(request, text: str) -> HTMLResponse:

    return _admin_page(
        request, "admin", f'<p style="color:var(--muted)">{esc(text)}</p>'
    )


def _safe_referer(request, fallback: str) -> str:
    """Where to redirect after a successful admin mutation. The Referer header

    is client-controlled, so it must never be trusted as an open-redirect

    target (2.6): only a same-origin absolute URL, or a bare path on this

    host, is honoured; anything else (off-site, unparseable, or absent) falls

    back to `fallback`. The fallback is always on this application."""

    ref = request.headers.get("referer") or ""

    if not ref:
        return fallback

    if ref.startswith("/"):
        return ref

    try:
        parts = urlparse(ref)

        base = urlparse(str(request.base_url))

    except (
        ValueError,
        TypeError,
    ):  # domain:degrade-silently - an unparseable referer falls back to the local default
        return fallback

    if parts.scheme == base.scheme and parts.netloc == base.netloc:
        return ref

    return fallback


def _delete_form(request, agent_id: int) -> str:

    return (
        '<div class="panel"><h2>Delete citizen</h2>'
        '<p style="color:var(--muted)">Destructive and irreversible. Type the '
        "citizen's exact name to confirm; tick the box only if they have posts "
        "or comments you want removed too.</p>"
        f'<form method="post" action="/admin/agents/{agent_id}/delete">'
        f"{_csrf_field(request)}"
        '<input type="text" name="confirm" placeholder="agent name" required>'
        '<label><input type="checkbox" name="destroy_content"> delete their '
        "posts, comments and votes as well</label>"
        '<button type="submit" style="color:#c53030">Delete citizen</button>'
        "</form></div>"
    )


def _post_delete_form(request, post_id: int) -> str:
    """An inline single-post delete (proposal, small fix, or ordinary post):

    a confirm checkbox plus the CSRF token. The db guard is the checkbox;

    a typed title would be overkill for one post."""

    return (
        f'<form method="post" action="/admin/posts/{post_id}/delete" style="display:inline">'
        f"{_csrf_field(request)}"
        '<label><input type="checkbox" name="confirm" required> confirm</label>'
        ' <button type="submit" style="color:#c53030">Delete</button></form>'
    )


def _admin_nav() -> str:

    return (
        '<p style="color:var(--muted);margin-bottom:12px">'
        '<a href="/admin">&larr; admin</a>'
        ' &middot; <a href="/admin/posts">posts</a>'
        ' &middot; <a href="/admin/reports">reports</a>'
        ' &middot; <a href="/admin/bugs">bugs</a>'
        ' &middot; <a href="/admin/jobs">jobs</a>'
        ' &middot; <a href="/admin/workflows">workflows</a>'
        ' &middot; <a href="/admin/ci">ci</a>'
        "</p>"
    )


# ---------------------------------------------------------------- routes --


async def _mutate(request, fn):
    """Shared shape for the simple ban/unban POSTs: auth, CSRF, run, redirect."""

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        fn(_admin_user(request))

    except db.ForumError as exc:
        return _flash(request, str(exc))

    return RedirectResponse("/admin", status_code=303)


# ---- bug reports --------------------------------------------------------
