import os
import requests

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_ADDRESS = os.environ.get("RESEND_FROM_ADDRESS", "onboarding@resend.dev")


def send_email(to_address, subject, body):
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY not configured")

    r = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": "Bearer " + RESEND_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "from": RESEND_FROM_ADDRESS,
            "to": [to_address],
            "subject": subject,
            "text": body,
        },
        timeout=10,
    )
    if r.status_code >= 300:
        raise RuntimeError("Resend API error " + str(r.status_code) + ": " + r.text[:300])
