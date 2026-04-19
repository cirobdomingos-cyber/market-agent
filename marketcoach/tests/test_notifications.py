"""
Tests for the email notification layer and its integration with the
test fixtures.

The hermetic conftest forces notifications_enabled=False for every test,
preventing real SMTP calls even on developer machines where Gmail App
Passwords are configured. These tests lock that behaviour in so the
guard can't silently regress.
"""

from unittest.mock import MagicMock, patch

from backend.config import settings
from backend.notifications import notify


class TestNotifyNoOp:
    """
    When notifications_enabled=False, notify() must return False without
    even attempting SMTP. The autouse fixture in conftest sets this, so
    every test — including tests that don't mock notify explicitly —
    gets a silent no-op for free.
    """

    def test_conftest_disables_notifications(self):
        """The autouse fixture flipped the flag."""
        assert settings.notifications_enabled is False

    def test_notify_returns_false_when_disabled(self):
        result = notify(
            "subject",
            "<p>body</p>",
            "body text",
        )
        assert result is False

    def test_notify_does_not_open_smtp_connection_when_disabled(self):
        """
        Defence in depth: even if someone forgets to mock SMTP and flips
        notifications_enabled=True, notify() bails before constructing a
        smtplib.SMTP client because the user/password are empty by default.
        This test verifies the flag alone is sufficient — smtplib is not
        invoked when enabled=False even if creds exist.
        """
        with patch("backend.notifications.smtplib.SMTP") as mock_smtp, \
             patch.object(settings, "smtp_user", "user@example.com"), \
             patch.object(settings, "smtp_password", "dummy-password"):
            notify("subject", "<p>body</p>", "body text")
        mock_smtp.assert_not_called()
