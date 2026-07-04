import os
import re
import json
import requests

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-5"

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
BIO_LINK_RE = re.compile(r"https?://(?:www\.)?(?:linktr\.ee|beacons\.ai|[a-zA-Z0-9-]+\.[a-zA-Z]{2,})/\S+")

VET_SYSTEM = (
    "You vet influencer/creator candidates for a health & wellness brand's seeding program. "
    "You are given the brand's brief and a creator's profile info. Judge whether the creator's "
    "content aligns with the brand. Respond ONLY with strict JSON, no markdown fences: "
    '{"aligned": true/false, "confidence": 0-100, "analysis": "1-2 sentence explanation", '
    '"tags": ["tag1","tag2"]} choosing tags only from: Gut Health, Longevity, Hormones, '
    "Biohacking, Functional Medicine."
)


def find_email(text):
    if not text:
        return None
    m = EMAIL_RE.search(text)
    return m.group(0) if m else None


def find_bio_link(bio_text):
    if not bio_text:
        return None
    m = BIO_LINK_RE.search(bio_text)
    return m.group(0) if m else None


def fetch_page_text(url, timeout=8):
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        return r.text
    except Exception:
        return ""


def find_email_for_creator(bio_text, website_url=None):
    # Step 1: email directly in bio
    email = find_email(bio_text)
    if email:
        return email

    # Step 2: bio link (Linktree, Beacons, or similar)
    bio_link = find_bio_link(bio_text)
    if bio_link:
        email = find_email(fetch_page_text(bio_link))
        if email:
            return email

    # Step 3: personal website homepage or contact page
    if website_url:
        for path in ("", "/contact"):
            email = find_email(fetch_page_text(website_url.rstrip("/") + path))
            if email:
                return email

    return None


def call_claude(system, user):
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 500,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []))


def assess_brand_fit(brand_brief, creator):
    user = (
        "BRAND BRIEF:\n" + (brand_brief or "(no brand brief set)") + "\n\n"
        "CREATOR PROFILE:\n"
        "Name: " + creator.get("name", "") + "\n"
        "Handle: " + creator.get("handle", "") + "\n"
        "Bio: " + creator.get("bio", "") + "\n"
        "Categories: " + ", ".join(creator.get("categories", [])) + "\n"
        "Followers: " + str(creator.get("followers", "")) + "\n"
    )
    raw = call_claude(VET_SYSTEM, user)
    cleaned = raw.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return {
            "aligned": False,
            "confidence": 0,
            "analysis": "Could not parse AI response: " + raw[:200],
            "tags": [],
        }


def vet_creator(creator, brand_brief):
    """
    creator: dict with name, handle, bio, categories, followers, website (optional)
    Returns: {outcome, analysis, tags, email, flag_note}
    outcome is one of "Auto-skipped", "Vetted", "Review"
    """
    fit = assess_brand_fit(brand_brief, creator)
    aligned = fit.get("aligned", False)
    confidence = fit.get("confidence", 0)
    analysis = fit.get("analysis", "")
    tags = fit.get("tags", [])

    if not aligned:
        return {"outcome": "Auto-skipped", "analysis": analysis, "tags": tags, "email": None, "flag_note": ""}

    email = find_email_for_creator(creator.get("bio", ""), creator.get("website"))

    if email and confidence >= 80:
        return {"outcome": "Vetted", "analysis": analysis, "tags": tags, "email": email, "flag_note": ""}

    if not email:
        flag_note = "No email found"
    else:
        flag_note = "Content — " + str(confidence) + "% confidence"
    return {"outcome": "Review", "analysis": analysis, "tags": tags, "email": email, "flag_note": flag_note}
