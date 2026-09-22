"""
Transactional email (verification, password reset) over the Brevo HTTP API.

WHY HTTP AND NOT SMTP. This module used to call `smtplib.SMTP_SSL` against
Gmail on port 465. Render's free instances block outbound traffic to every
SMTP port (25/465/587), so every send there failed instantly with
"[Errno 101] Network is unreachable" -- and because the send runs inside a
FastAPI BackgroundTask, registration still returned 201 "check your email"
while nothing was ever delivered. Port 443 is not blocked, so the fix is a
provider with an HTTP API rather than a different SMTP host.

All three senders return True/False rather than raising -- BackgroundTasks
swallows exceptions, so nothing would ever see one -- and log every failure at
ERROR. The API key lives only in the request header and is never logged.

`httpx.post` here is SYNCHRONOUS, which is why every caller must schedule
these through `BackgroundTasks` rather than awaiting them in a handler: a
blocking call inside an async endpoint stalls the whole event loop for up to
_TIMEOUT_SECONDS.
"""

import httpx

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)

_BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT_SECONDS = 15

# Brevo's error bodies are short JSON ({"code": ..., "message": ...}); the cap
# is only there so an HTML error page from a proxy cannot flood the log.
_MAX_LOGGED_BODY = 500


def _send(to_email: str, subject: str, html: str, text: str) -> bool:
    """POST one email to Brevo. Returns True only on a 2xx."""
    payload = {
        "sender": {
            "name": settings.email_sender_name,
            "email": settings.email_sender_address,
        },
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html,
        # Sent alongside the HTML: a text/plain part keeps the mail out of
        # spam filters that penalise HTML-only messages, and is what plain-text
        # clients render instead of the raw markup.
        "textContent": text,
    }

    try:
        response = httpx.post(
            _BREVO_ENDPOINT,
            headers={
                "api-key": settings.brevo_api_key,
                "accept": "application/json",
                "content-type": "application/json",
            },
            json=payload,
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        # DNS, TLS, connect timeout -- the network, not the request.
        logger.error("Brevo request for %s failed: %s", to_email, e)
        return False

    if response.status_code >= 400:
        # Status AND body: a 401 means the key, a 400 usually means the sender
        # address is not a verified sender in Brevo, and only the body
        # distinguishes them. Never log `payload` or the headers -- the key is
        # in there.
        logger.error(
            "Brevo rejected the email to %s: %s %s",
            to_email,
            response.status_code,
            response.text[:_MAX_LOGGED_BODY],
        )
        return False

    logger.info("Email sent to %s (subject=%r)", to_email, subject)
    return True


def send_verification_email(to_email: str, token: str) -> bool:
    # Points at the FRONTEND page, which calls the backend itself and can then
    # offer "resend" on an expired link. The backend's GET /auth/verify-email
    # still works, for links mailed before this change.
    verify_link = f"{settings.frontend_url.rstrip('/')}/verify-email?token={token}"

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

    text = f"""\
Welcome to PickWise!

Thank you for registering. Verify your email address by opening this link:

{verify_link}

The link expires in {settings.email_verification_token_expire_hours} hour(s).
If you did not create a PickWise account, you can ignore this email.

-- The PickWise Team
"""
    return _send(to_email, "Verify your PickWise Account", html, text)


def send_password_reset_email(email_to: str, token: str) -> bool:
    reset_link = f"{settings.frontend_url.rstrip('/')}/reset-password?token={token}"

    html = f"""
    <html>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f4f5; padding: 40px 0; margin: 0;">
            <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; padding: 40px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);">
                <h2 style="color: #1c1c1e; margin-top: 0;">Password Reset Request</h2>
                <p style="color: #3a3a3c; font-size: 16px; line-height: 1.5;">
                    We received a request to reset the password for your PickWise account.
                    Click the button below to choose a new password. This link expires in
                    15 minutes and can only be used once.
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

    text = f"""\
Password reset request

We received a request to reset the password for your PickWise account.
Open this link to choose a new password:

{reset_link}

The link expires in 15 minutes and can only be used once.
If you did not request this reset, you can safely ignore this email.

-- The PickWise Team
"""
    return _send(email_to, "Reset Your PickWise Password", html, text)


def send_google_account_notice_email(email_to: str) -> bool:
    """
    Reply to a forgot-password request for an account that has no local
    password (Google sign-in only).

    Silence would be the smaller change, but it leaves the user staring at
    "check your inbox" with nothing arriving and no way to learn why. It costs
    one send against the daily quota and sits behind the same per-email
    cooldown as a real reset.
    """
    login_link = f"{settings.frontend_url.rstrip('/')}/login"

    html = f"""
    <html>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f4f5; padding: 40px 0; margin: 0;">
            <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; padding: 40px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);">
                <h2 style="color: #1c1c1e; margin-top: 0;">Use &quot;Continue with Google&quot;</h2>
                <p style="color: #3a3a3c; font-size: 16px; line-height: 1.5;">
                    You asked to reset the password for your PickWise account, but this
                    account signs in with Google and has no password to reset.
                </p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{login_link}" style="background-color: #042e61; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 8px; font-weight: 600; display: inline-block;">
                        Sign in with Google
                    </a>
                </div>
                <p style="color: #8e8e93; font-size: 14px; line-height: 1.5; margin-bottom: 0;">
                    If you did not make this request, you can safely ignore this email.
                </p>
            </div>
        </body>
    </html>
    """

    text = f"""\
Use "Continue with Google"

You asked to reset the password for your PickWise account, but this account
signs in with Google and has no password to reset.

Sign in here: {login_link}

If you did not make this request, you can safely ignore this email.

-- The PickWise Team
"""
    return _send(email_to, "Sign in to PickWise with Google", html, text)
