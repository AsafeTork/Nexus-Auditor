from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, html: str) -> bool:
    """
    Send a transactional email via Resend (resend.com — free up to 3,000/month).
    Falls back silently if RESEND_API_KEY is not set.
    """
    try:
        from flask import current_app
        api_key = current_app.config.get("RESEND_API_KEY", "")
        from_addr = current_app.config.get("EMAIL_FROM", "noreply@xentinel.onrender.com")
    except RuntimeError:
        return False

    if not api_key:
        logger.warning("send_email: RESEND_API_KEY not set, skipping email to %s", to)
        return False

    try:
        import resend  # type: ignore
        resend.api_key = api_key
        resend.Emails.send({
            "from": from_addr,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        return True
    except Exception as exc:
        logger.error("send_email failed to=%s subject=%s err=%s", to, subject, exc)
        return False
