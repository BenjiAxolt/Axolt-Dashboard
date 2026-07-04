import os
import socket
import smtplib
from email.mime.text import MIMEText

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    # Render's network can't route outbound IPv6, but smtp.gmail.com resolves
    # to both IPv4 and IPv6 — force IPv4 so the connection doesn't fail with
    # "Network is unreachable" when the resolver picks an IPv6 address first.
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


def send_email(to_address, subject, body):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not configured")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to_address

    socket.getaddrinfo = _ipv4_only_getaddrinfo
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [to_address], msg.as_string())
    finally:
        socket.getaddrinfo = _orig_getaddrinfo
