import logging
import os
import smtplib
from email.message import EmailMessage
from smtplib import SMTPException

logger = logging.getLogger(__name__)


class PasswordResetEmailError(RuntimeError):
    """Raised when the password reset email cannot be delivered."""


def send_password_reset_email(to_email: str, reset_link: str) -> dict[str, str]:
    """Send a password reset email or fall back to a logged preview in local development."""
    smtp_host = (os.getenv("SMTP_HOST") or "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = (os.getenv("SMTP_USERNAME") or "").strip() or None
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from_email = (os.getenv("SMTP_FROM_EMAIL") or smtp_username or "no-reply@want.local").strip()
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}
    is_production = os.getenv("APP_ENV", "development").lower() == "production"

    preview_message = (
        "If an account exists for that email, reset instructions were generated. "
        "In local development, check the backend logs for the preview link."
    )

    message = EmailMessage()
    message["Subject"] = "Reset your WANT password"
    message["From"] = smtp_from_email
    message["To"] = to_email

    text_content = f"""
We received a request to reset your WANT account password.

Use the link below to choose a new password:
{reset_link}

This link expires in 60 minutes. If you did not request a password reset, you can safely ignore this email.
""".strip()

    html_content = f"""
    <html>
      <body style=\"font-family: Arial, sans-serif; color: #1f2937;\">
        <p>We received a request to reset your WANT account password.</p>
        <p>
          <a href=\"{reset_link}\" style=\"display: inline-block; padding: 10px 16px; background: #213F6B; color: #ffffff; text-decoration: none; border-radius: 6px;\">Reset password</a>
        </p>
        <p>If the button does not work, copy and paste this link into your browser:</p>
        <p><a href=\"{reset_link}\">{reset_link}</a></p>
        <p>This link expires in 60 minutes. If you did not request a password reset, you can safely ignore this email.</p>
      </body>
    </html>
    """.strip()

    message.set_content(text_content)
    message.add_alternative(html_content, subtype="html")

    if not smtp_host:
        logger.warning("SMTP_HOST is not configured. Password reset preview for %s: %s", to_email, reset_link)
        return {"status": "preview", "message": preview_message}

    if smtp_username and not smtp_password:
        logger.error("SMTP_USERNAME is set but SMTP_PASSWORD is missing; cannot send password reset email to %s", to_email)
        if not is_production:
            logger.warning("Falling back to preview reset link for %s: %s", to_email, reset_link)
            return {"status": "preview", "message": preview_message}
        raise PasswordResetEmailError("SMTP credentials are incomplete.")

    try:
        smtp_client = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_client(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo()
            if use_tls and not use_ssl:
                server.starttls()
                server.ehlo()
            if smtp_username and smtp_password:
                server.login(smtp_username, smtp_password)
            server.send_message(message)

        logger.info("Password reset email sent to %s via %s:%s", to_email, smtp_host, smtp_port)
        return {
            "status": "sent",
            "message": "If an account exists for that email, a reset link has been sent.",
        }
    except (OSError, SMTPException, TimeoutError) as exc:
        logger.exception(
            "Failed to send password reset email to %s via host=%s port=%s tls=%s ssl=%s",
            to_email,
            smtp_host,
            smtp_port,
            use_tls,
            use_ssl,
        )
        if not is_production:
            logger.warning("Falling back to preview reset link for %s: %s", to_email, reset_link)
            return {"status": "preview", "message": preview_message}
        raise PasswordResetEmailError("Unable to send password reset email") from exc
