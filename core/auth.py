"""Email OTP authentication for the hosted multi-user deployment.

Active when SMTP_USER, SMTP_PASS, and ALLOWED_EMAILS are all set (in Streamlit
secrets or env vars). Sends a 6-digit code from Gmail to the visitor's email;
they enter the code to unlock the app for a rolling 5-minute session.

Pure stdlib — no extra dependencies required.
"""

from __future__ import annotations

import secrets as _secrets
import smtplib
import ssl
from email.message import EmailMessage


SESSION_TTL_SECONDS = 5 * 60  # rolling 5-minute session
OTP_TTL_SECONDS = 5 * 60       # codes expire 5 minutes after generation


def generate_otp() -> str:
    """Cryptographically random 6-digit code (zero-padded)."""
    return f"{_secrets.randbelow(1000000):06d}"


def is_email_allowed(email: str, allowlist_csv: str) -> bool:
    """True if `email` appears in the comma-separated allowlist (case-insensitive)."""
    if not allowlist_csv or not allowlist_csv.strip():
        return False  # empty allowlist = nobody allowed (safer default)
    target = (email or "").strip().lower()
    if not target:
        return False
    allowed = {e.strip().lower() for e in allowlist_csv.split(",") if e.strip()}
    return target in allowed


def send_otp_email(
    to_email: str,
    code: str,
    smtp_user: str,
    smtp_pass: str,
    app_name: str = "Job Application Assistant",
) -> None:
    """Send the 6-digit access code to `to_email` via Gmail SMTP.

    Requires a Google App Password (not your regular Gmail password) on accounts
    with 2FA enabled. Raises on connection / auth / send failure.
    """
    msg = EmailMessage()
    msg["Subject"] = f"{app_name} — your access code"
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg.set_content(
        f"Your access code: {code}\n\n"
        f"This code is valid for 5 minutes. Enter it on the login screen to "
        f"access {app_name}.\n\n"
        f"If you didn't request this, you can ignore this email."
    )
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=15) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
