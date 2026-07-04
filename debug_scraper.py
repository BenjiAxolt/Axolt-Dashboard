"""
Standalone local debug tool — NOT part of the deployed app.

Run this on your own machine (with Playwright installed and cookies
exported from business.facebook.com while on the Creator Marketplace
page) to dump the real page HTML so we can identify the correct CSS
selectors for scraper.py.

Usage:
  1. pip3 install playwright && python3 -m playwright install chromium
  2. While on the Creator Marketplace page in Chrome, export your
     cookies (EditThisCookie -> Export) and save them to cookies.json
     in this folder.
  3. python3 debug_scraper.py "health coach"
  4. Send back debug_output/page.html and debug_output/screenshot.png
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

MARKETPLACE_URL = os.environ.get(
    "MARKETPLACE_URL",
    "https://business.facebook.com/latest/creator_marketplace/creators/search"
    "?business_id=1336144087729718&asset_id=415584441641514",
)
MARKETPLACE_PREFIX = "https://business.facebook.com/latest/creator_marketplace"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_output")


SAME_SITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
}


def normalize_cookies(cookies):
    cleaned = []
    for c in cookies:
        c = dict(c)
        same_site = str(c.get("sameSite", "")).lower()
        c["sameSite"] = SAME_SITE_MAP.get(same_site, "Lax")
        cleaned.append(c)
    return cleaned


def main():
    keyword = sys.argv[1] if len(sys.argv) > 1 else "health coach"

    cookies_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.json")
    if not os.path.exists(cookies_path):
        print("Missing cookies.json — export your cookies from business.facebook.com first.")
        sys.exit(1)

    with open(cookies_path) as f:
        cookies = json.load(f)
    cookies = normalize_cookies(cookies)

    os.makedirs(OUT_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        context.add_cookies(cookies)
        page = context.new_page()

        print("Navigating to:", MARKETPLACE_URL)
        page.goto(MARKETPLACE_URL, timeout=30000)
        time.sleep(4)

        print("Landed on:", page.url)
        if not page.url.startswith(MARKETPLACE_PREFIX):
            print("WARNING: did not land on Creator Marketplace — check cookies/login.")

        search_box = page.query_selector("input[placeholder='Search'], input[type='search']")
        if search_box:
            print("Found search box, typing:", keyword)
            search_box.fill(keyword)
            search_box.press("Enter")
            time.sleep(3)
        else:
            print("Could NOT find a search box with the guessed selector — will still dump the page so we can find it.")

        for _ in range(3):
            page.evaluate("window.scrollBy(0, 800)")
            time.sleep(1.5)

        html_path = os.path.join(OUT_DIR, "page.html")
        with open(html_path, "w") as f:
            f.write(page.content())
        print("Saved full page HTML to:", html_path)

        screenshot_path = os.path.join(OUT_DIR, "screenshot.png")
        page.screenshot(path=screenshot_path, full_page=True)
        print("Saved screenshot to:", screenshot_path)

        # Open the first creator's profile page to see where follower/ER/bio data lives
        first_card = page.query_selector("a[aria-label^='Open portfolio for ']")
        if first_card:
            name = first_card.get_attribute("aria-label")
            print("Opening profile for:", name)
            first_card.click()
            time.sleep(4)

            profile_html_path = os.path.join(OUT_DIR, "profile.html")
            with open(profile_html_path, "w") as f:
                f.write(page.content())
            print("Saved profile page HTML to:", profile_html_path)

            profile_screenshot_path = os.path.join(OUT_DIR, "profile_screenshot.png")
            page.screenshot(path=profile_screenshot_path, full_page=True)
            print("Saved profile screenshot to:", profile_screenshot_path)

            view_profile_btn = page.get_by_text("View profile", exact=True)
            if view_profile_btn.count() > 0:
                print("Clicking 'View profile'...")
                try:
                    with context.expect_page(timeout=5000) as new_page_info:
                        view_profile_btn.first.click()
                    profile_page = new_page_info.value
                    profile_page.wait_for_load_state()
                    time.sleep(3)
                    print("New tab opened at:", profile_page.url)
                except Exception:
                    print("No new tab detected — checking current page instead.")
                    time.sleep(3)
                    profile_page = page

                full_profile_html_path = os.path.join(OUT_DIR, "full_profile.html")
                with open(full_profile_html_path, "w") as f:
                    f.write(profile_page.content())
                print("Saved full profile page HTML to:", full_profile_html_path)

                full_profile_screenshot_path = os.path.join(OUT_DIR, "full_profile_screenshot.png")
                profile_page.screenshot(path=full_profile_screenshot_path, full_page=True)
                print("Saved full profile screenshot to:", full_profile_screenshot_path)
            else:
                print("Could not find a 'View profile' button to click.")
        else:
            print("No creator card found to open a profile from.")

        browser.close()


if __name__ == "__main__":
    main()
