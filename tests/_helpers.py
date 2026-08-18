"""Shared helpers for the tests/ package."""

from tests._setup import expect_error  # noqa: F401


def mail(token, **kw):
    """Fetch notifications for an agent (thin wrapper)."""
    from tests._setup import notifications
    return notifications.notifications(token, **kw)
