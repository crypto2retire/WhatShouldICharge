import os
import re
import logging

logger = logging.getLogger("wsic.email")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def send_email(to_email: str, subject: str, html_content: str):
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@whatshouldicharge.app")
    if not api_key:
        logger.error("[send_email] SENDGRID_API_KEY not set — email not sent")
        return False
    if not to_email or not _EMAIL_RE.match(to_email):
        logger.error(f"[send_email] Invalid recipient email: {to_email}")
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=subject,
            html_content=html_content,
        )
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        logger.info(f"[send_email] Sent to {to_email} from {from_email}, status={response.status_code}")
        if response.status_code >= 400:
            logger.error(f"[send_email] SendGrid error status {response.status_code}")
            return False
        return True
    except Exception as e:
        logger.error(f"[send_email] FAILED to send to {to_email}: {type(e).__name__}: {e}")
        return False
