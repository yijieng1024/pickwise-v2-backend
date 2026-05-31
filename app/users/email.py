from email.mime import message
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.config import settings

def send_verification_email(to_email: str, token: str):

    message = MIMEMultipart("alternative")
    message["Subject"] = "Verify your PickWise Account"
    message["From"] = f"PickWise Team <{settings.smtp_username}>"
    message["To"] = to_email
    
    # change this to your frontend URL once you have it set up
    verify_link = f"http://127.0.0.1:8000/auth/verify-email?token={token}"
    
    html = f"""\
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <h2 style="color: #007bff;">Welcome to PickWise!</h2>
        <p>Hi there,</p>
        <p>Thank you for registering. Please verify your email address by clicking the button below:</p>
        <p>
            <a href="{verify_link}" style="display: inline-block; padding: 10px 20px; margin: 10px 0; background-color: #007bff; color: #ffffff; text-decoration: none; border-radius: 5px; font-weight: bold;">
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
    part = MIMEText(html, "html")
    message.attach(part)
    #print(f"\n---> DEBUG SMTP SERVER: '{settings.smtp_server}' <---")
    #print(f"---> DEBUG SMTP PORT: {settings.smtp_port} <---\n")

    try:
        with smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port) as server:
            server.login(settings.smtp_username, settings.smtp_password)
            server.sendmail(settings.smtp_username, to_email, message.as_string())
        print(f"Email successfully sent to {to_email}")
    except Exception as e:
        print(f"Failed to send email: {e}")

def send_password_reset_email(email_to: str, token: str):
    """Sends a secure password reset link to the user."""
    
    # The link to your future Next.js frontend
    reset_link = f"http://localhost:3000/reset-password?token={token}"
    
    message = MIMEMultipart("alternative")
    message['Subject'] = 'Reset Your PickWise Password'
    message['From'] = settings.smtp_username
    message['To'] = email_to

    html_content = f"""
    <html>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f4f5; padding: 40px 0; margin: 0;">
            <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; padding: 40px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);">
                <h2 style="color: #1c1c1e; margin-top: 0;">Password Reset Request</h2>
                <p style="color: #3a3a3c; font-size: 16px; line-height: 1.5;">
                    We received a request to reset the password for your PickWise account. 
                    Click the button below to choose a new password. This link will expire in 15 minutes.
                </p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{reset_link}" style="background-color: #007aff; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 8px; font-weight: 600; display: inline-block;">
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
    
    part = MIMEText(html_content, "html")
    message.attach(part)

    try:
        with smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port) as server:
            server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)
    except Exception as e:
        print(f"Failed to send password reset email to {email_to}: {e}")