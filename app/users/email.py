"""
Transactional email (verification, password reset).

Two transports. Brevo's HTTP API is used whenever BREVO_API_KEY is set —
it posts to api.brevo.com over 443, which is the only thing that works on
Render: outbound SMTP ports (25/465/587) are blocked on free instances, so
smtplib there dies with "[Errno 101] Network is unreachable" and the mail
silently never goes out. Without the key we fall back to SMTP, which is
fine locally.

Both senders return True/False rather than raising — they run inside
FastAPI BackgroundTasks, where an exception is invisible to the caller —
but every failure is logged at ERROR with the provider's response.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)

_BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT_SECONDS = 15


def _sender_address() -> str:
    """Brevo sends from a verified sender; fall back to the SMTP identity."""
    return settings.email_sender_address or settings.smtp_username


def _send(to_email: str, subject: str, html: str) -> bool:
    if settings.brevo_api_key:
        return _send_via_brevo(to_email, subject, html)
    return _send_via_smtp(to_email, subject, html)


def _send_via_brevo(to_email: str, subject: str, html: str) -> bool:
    try:
        response = requests.post(
            _BREVO_ENDPOINT,
            headers={
                "api-key": settings.brevo_api_key or "",
                "accept": "application/json",
                "content-type": "application/json",
            },
            json={
                "sender": {"name": settings.email_sender_name, "email": _sender_address()},
                "to": [{"email": to_email}],
                "subject": subject,
                "htmlContent": html,
            },
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        logger.error("Brevo request to %s failed: %s", to_email, e)
        return False

    if response.status_code >= 400:
        # Brevo puts the actionable reason in the body (unverified sender,
        # bad key, daily cap), not in the status line.
        logger.error(
            "Brevo rejected the email to %s: HTTP %s %s",
            to_email,
            response.status_code,
            response.text[:500],
        )
        return False

    logger.info("Email sent to %s via Brevo (subject=%r)", to_email, subject)
    return True


def _send_via_smtp(to_email: str, subject: str, html: str) -> bool:
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"{settings.email_sender_name} <{settings.smtp_username}>"
    message["To"] = to_email
    message.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL(
            settings.smtp_server, settings.smtp_port, timeout=_TIMEOUT_SECONDS
        ) as server:
            server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)
    except OSError as e:
        # ENETUNREACH/timeout here on a host that blocks SMTP egress (Render
        # free tier) — set BREVO_API_KEY to route over HTTPS instead.
        logger.error("SMTP send to %s failed: %s", to_email, e)
        return False
    except smtplib.SMTPException as e:
        logger.error("SMTP rejected the email to %s: %s", to_email, e)
        return False

    logger.info("Email sent to %s via SMTP (subject=%r)", to_email, subject)
    return True


def send_verification_email(to_email: str, token: str) -> bool:
    # Hits the API directly: the frontend has no verification page yet. The
    # /api/v2 prefix is required — main.py mounts every router under it.
    verify_link = f"{settings.backend_url.rstrip('/')}/api/v2/auth/verify-email?token={token}"

    html = f"""\
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <h2 style="color: #042e61;">Welcome to PickWise!</h2>
        <p>Hi there,</p>
        <p>Thank you for registering. Please verify your email address by clicking the button below:</p>
        <p>
            <a href="{verify_link}" style="display: inline-block; padding: 10px 20px; margin: 10px 0; background-color: #042e61; color: #ffffff; text-decoration: none; border-radius: 5px; font-weight: bold;">
                Verify My Email
            </a>
        </p>
        <p style="font-size: 12px; color: #777;">
            If the button doesn't work, copy and paste this link into your browser:<br>
            <a href="{verify_link}">{verify_link}</a>
        </p>
        <p>Thanks,<br>The PickWise Team</p>
      </body>
    </html>
    """
    return _send(to_email, "Verify your PickWise Account", html)


def send_password_reset_email(email_to: str, token: str) -> bool:
    reset_link = f"{settings.frontend_url.rstrip('/')}/reset-password?token={token}"

    html = f"""
    <html>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f4f5; padding: 40px 0; margin: 0;">
            <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; padding: 40px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);">
                <h2 style="color: #1c1c1e; margin-top: 0;">Password Reset Request</h2>
                <p style="color: #3a3a3c; font-size: 16px; line-height: 1.5;">
                    We received a request to reset the password for your PickWise account.
                    Click the button below to choose a new password. This link will expire in 15 minutes.
                </p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{reset_link}" style="background-color: #042e61; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 8px; font-weight: 600; display: inline-block;">
                        Reset Password
                    </a>
                </div>
                <p style="color: #8e8e93; font-size: 14px; line-height: 1.5; margin-bottom: 0;">
                    If you did not request this reset, you can safely ignore this email. Your account remains secure.
                </p>
            </div>
        </body>
    </html>
    """
    return _send(email_to, "Reset Your PickWise Password", html)
