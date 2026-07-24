"""
Minimal SMTP email sending (Google Workspace friendly).

Environment variables (set on Railway):
  SMTP_HOST      e.g. smtp.gmail.com
  SMTP_PORT      default 587 (STARTTLS)
  SMTP_USER      e.g. reporting@merantix.com
  SMTP_PASSWORD  Google App Password (not the account password)
  SMTP_FROM      optional; defaults to SMTP_USER

For Google Workspace: create an App Password (Google Account → Security →
2-Step Verification → App passwords) for the sending mailbox.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def send_email(to: list[str], subject: str, body: str) -> None:
    """Send a plain-text email. Raises RuntimeError with a readable message on failure."""
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    port = int(os.getenv("SMTP_PORT", "587"))
    sender = os.getenv("SMTP_FROM") or user

    if not (host and user and password):
        raise RuntimeError(
            "SMTP is not configured. Set SMTP_HOST, SMTP_USER and SMTP_PASSWORD "
            "(and optionally SMTP_PORT, SMTP_FROM) as environment variables."
        )
    if not to:
        raise RuntimeError("No recipient email addresses.")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        log.info("email sent to %s: %s", to, subject)
    except Exception as exc:
        raise RuntimeError(f"Email send failed: {exc}") from exc
