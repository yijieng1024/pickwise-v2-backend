# app/users/email.py
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.config import settings

def send_real_verification_email(to_email: str, token: str):

    message = MIMEMultipart("alternative")
    message["Subject"] = "Verify your PickWise Account 🚀"
    message["From"] = f"PickWise Team <{settings.smtp_username}>"
    message["To"] = to_email
    
    # 本地测试的链接，以后上线了换成前端的真实域名
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
    print(f"\n---> DEBUG SMTP SERVER: '{settings.smtp_server}' <---")
    print(f"---> DEBUG SMTP PORT: {settings.smtp_port} <---\n")

    try:
        with smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port) as server:
            server.login(settings.smtp_username, settings.smtp_password)
            server.sendmail(settings.smtp_username, to_email, message.as_string())
        print(f"Email successfully sent to {to_email}")
    except Exception as e:
        print(f"Failed to send email: {e}")