import os
import re
import logging

# Optional: requests for SendGrid Email Validation API
try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("wsic.email")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_sendgrid(api_key: str, email: str) -> bool:
    """Call SendGrid Email Validation API v3 (requires separate plan)."""
    if requests is None:
        return True
    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/validations/email",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"email": email},
            timeout=5,
        )
        if resp.status_code == 200:
            result = resp.json()
            verdict = result.get("result", {})
            if verdict.get("verdict") == "Invalid":
                logger.warning(f"[email_validation] SendGrid says {email} is invalid")
                return False
            if verdict.get("score", 0) < 0.3:
                logger.warning(f"[email_validation] SendGrid score too low for {email}: {verdict.get('score')}")
                return False
            return True
        elif resp.status_code == 403:
            logger.debug("[email_validation] SendGrid Email Validation API not enabled on this account")
            return True  # fall through to regex
        else:
            logger.warning(f"[email_validation] SendGrid validation error {resp.status_code}")
            return True  # fail open
    except Exception as e:
        logger.warning(f"[email_validation] Exception during validation: {e}")
        return True  # fail open


def send_email(to_email: str, subject: str, html_content: str):
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@whatshouldicharge.app")
    if not api_key:
        logger.error("[send_email] SENDGRID_API_KEY not set — email not sent")
        return False
    if not to_email or not _EMAIL_RE.match(to_email):
        logger.error(f"[send_email] Invalid recipient email: {to_email}")
        return False
    if not _validate_email_sendgrid(api_key, to_email):
        logger.error(f"[send_email] Email failed SendGrid validation: {to_email}")
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
