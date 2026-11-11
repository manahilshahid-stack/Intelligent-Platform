"""
Transactional email over HTTPS via Resend (https://resend.com).

Railway blocks outbound SMTP on the Free, Trial and Hobby plans, so all email
goes through Resend's HTTPS API instead.

Environment variables (set on Railway):
  RESEND_API_KEY  Resend API key (re_...)
  EMAIL_FROM      sender, e.g. "Merantix Capital <lp@updates.merantix.com>".
                  Its domain must be verified in the Resend account.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 15


def email_configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY") and os.getenv("EMAIL_FROM"))


def send_email(to: list[str], subject: str, body: str | None = None, *, html: str | None = None) -> None:
    """Send an email (plain text and/or HTML). Raises RuntimeError with a readable message on failure."""
    api_key = os.getenv("RESEND_API_KEY")
    sender = os.getenv("EMAIL_FROM")

    if not (api_key and sender):
        raise RuntimeError("Email is not configured. Set RESEND_API_KEY and EMAIL_FROM as environment variables.")
    if not to:
        raise RuntimeError("No recipient email addresses.")
    if body is None and html is None:
        raise RuntimeError("Email has no body.")

    payload: dict = {"from": sender, "to": to, "subject": subject}
    if body is not None:
        payload["text"] = body
    if html is not None:
        payload["html"] = html

    try:
        resp = httpx.post(
            RESEND_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Email send failed: {type(exc).__name__}: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message") or resp.text
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"Email send failed: Resend returned {resp.status_code}: {detail[:200]}")

    log.info("email sent to %s: %s", to, subject)
