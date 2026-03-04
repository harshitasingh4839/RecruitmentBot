import os
import smtplib
from email.message import EmailMessage
from typing import Optional
import logging
logger = logging.getLogger(__name__)

def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if not v:
        raise RuntimeError(f"{name} is not set")
    return v

def send_email_smtp(to_email: str, subject: str, html_body: str, text_body: Optional[str] = None) -> None:
    logger.info(f"Sending email to {to_email} with subject: {subject}")
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT", "587"))
    username = _env("SMTP_USERNAME")
    password = _env("SMTP_PASSWORD")
    from_addr = _env("SMTP_FROM", username)

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject

    # Plain text fallback (important for deliverability)
    msg.set_content(text_body or "Please open the link in the email to connect your calendar.")
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)
        logger.info(f"Email successfully sent to {to_email}")
    except Exception:
        logger.exception(f"Failed to send email to {to_email}")
        raise