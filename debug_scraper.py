"""
Standalone local debug tool — NOT part of the deployed app.

Run this on your own machine (with Playwright installed and your
Facebook session cookies) to dump the real Meta Creator Marketplace
page HTML so we can identify the correct CSS selectors for scraper.py.

Usage:
  1. pip install playwright && playwright install chromium
  2. Export your Facebook cookies (e.g. via the EditThisCookie extension)
     and save them to cookies.json in this folder.
  3. python3 debug_scraper.py "health coach" US
  4. Send back debug_output/page.html and debug_output/screenshot.png
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

MARKETPLACE_PREFIX = "https://www.facebook.com/creator/marketplace"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_output")


def main():
    keyword = sys.argv[1] if len(sys.argv) > 1 else "health coach"
    country = sys.argv[2] if len(sys.argv) > 2 else "US"

    cookies_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.json")
    if not os.path.exists(cookies_path):
        print("Missing cookies.json — export your Facebook session cookies there first.")
        sys.exit(1)

    with open(cookies_path) as f:
        cookies = json.load(f)

    os.makedirs(OUT_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        context.add_cookies(cookies)
        page = context.new_page()

        url = MARKETPLACE_PREFIX + "/search?q=" + keyword.replace(" ", "+") + "&country=" + country
        print("Navigating to:", url)
        page.goto(url, timeout=30000)
        time.sleep(4)

        print("Landed on:", page.url)
        if not page.url.startswith(MARKETPLACE_PREFIX):
            print("WARNING: navigated outside the Marketplace — check login/cookies.")

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

        # Quick candidate-selector probe
        candidates = [
            "[data-testid='creator-card']", ".creator-card", "[role='article']",
            "[data-testid='creator-handle']", ".creator-handle",
            "[data-testid='creator-name']", ".creator-name",
            "[data-testid='follower-count']", ".follower-count",
            "[data-testid='creator-bio']", ".creator-bio",
            "[data-testid='creator-category']", ".creator-category",
        ]
        print("\n--- Selector probe (element counts found) ---")
        for sel in candidates:
            count = len(page.query_selector_all(sel))
            print(sel.ljust(35), count)

        browser.close()


if __name__ == "__main__":
    main()
