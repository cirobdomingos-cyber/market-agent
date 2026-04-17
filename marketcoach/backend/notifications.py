"""
Email notification dispatch for scheduled events.

Sends HTML emails via SMTP (default: Gmail with App Password). All calls
are fire-and-forget — a failed email never blocks the caller or breaks
the scheduler. Logging captures every send attempt so failures are
visible in Railway logs without crashing the pipeline.

Usage:
    from backend.notifications import notify
    notify("Morning Brief — 2026-04-17", html_body, text_body)
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from backend.config import settings

logger = logging.getLogger(__name__)


def notify(subject: str, body_html: str, body_text: str = "") -> bool:
    """
    Send an email notification. Returns True on success, False on any
    failure. Never raises — callers don't need try/except.

    Silently no-ops when notifications_enabled is False or SMTP creds
    are missing, so the same code path works in dev (no SMTP configured)
    and production (SMTP configured) without branching.
    """
    if not settings.notifications_enabled:
        return False

    if not settings.smtp_user or not settings.smtp_password:
        logger.warning(
            "NOTIFICATIONS_ENABLED=true but SMTP_USER/SMTP_PASSWORD are "
            "missing. Skipping email. Set both in .env or Railway env vars."
        )
        return False

    recipient = settings.notification_email or settings.smtp_user

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[MarketCoach] {subject}"
    msg["From"] = settings.smtp_user
    msg["To"] = recipient

    # Plain-text fallback for email clients that don't render HTML
    if body_text:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))

    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        logger.info("Email sent: '%s' → %s", subject, recipient)
        return True
    except smtplib.SMTPAuthenticationError as exc:
        logger.error(
            "SMTP auth failed (wrong App Password?): %s. "
            "Generate a new one at https://myaccount.google.com/apppasswords",
            exc,
        )
        return False
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False
