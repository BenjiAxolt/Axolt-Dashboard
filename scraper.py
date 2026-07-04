import json
import time
import random
import threading
import requests
import os

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
INFLUENCER_DB = "f07a187424e64bc7b1b992ceced311c5"

NOTION_HEADERS = {
    "Authorization": "Bearer " + NOTION_TOKEN,
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

MARKETPLACE_PREFIX = "https://www.facebook.com/creator/marketplace"

DEDUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dedup_list.json")

# Global job state
job = {
    "running": False,
    "log": [],
    "found": 0,
    "added": 0,
    "skipped": 0,
}


def load_dedup():
    if not os.path.exists(DEDUP_FILE):
        return set()
    try:
        with open(DEDUP_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_dedup(seen):
    try:
        with open(DEDUP_FILE, "w") as f:
            json.dump(sorted(seen), f)
    except Exception as e:
        log("Dedup save error: " + str(e))


def log(msg):
    job["log"].append(msg)
    print(msg)


def get_existing_handles():
    handles = set()
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            "https://api.notion.com/v1/databases/" + INFLUENCER_DB + "/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        data = r.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            handle_prop = props.get("Handle", {})
            if handle_prop.get("type") == "rich_text":
                text = "".join(i.get("plain_text", "") for i in handle_prop.get("rich_text", []))
                if text:
                    handles.add(text.lower().strip())
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return handles


def add_to_notion(name, handle, followers, categories, profile_url):
    props = {
        "Name": {"title": [{"text": {"content": name}}]},
        "Stage": {"select": {"name": "Lead"}},
    }
    if handle:
        props["Handle"] = {"rich_text": [{"text": {"content": handle}}]}
    if followers:
        props["Followers"] = {"number": followers}
    if categories:
        props["Category"] = {"multi_select": [{"name": c} for c in categories[:5]]}
    if profile_url:
        props["Profile URL"] = {"url": profile_url}

    requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": INFLUENCER_DB}, "properties": props},
    )


def parse_followers(text):
    if not text:
        return None
    text = text.strip().upper().replace(",", "")
    try:
        if "M" in text:
            return int(float(text.replace("M", "")) * 1000000)
        if "K" in text:
            return int(float(text.replace("K", "")) * 1000)
        return int(text)
    except Exception:
        return None


def run_scrape(keywords, limit, cookies_json, country, filters=None):
    from playwright.sync_api import sync_playwright

    filters = filters or {}
    followers_min = filters.get("followers_min")
    followers_max = filters.get("followers_max")
    min_er = filters.get("min_er")

    job["running"] = True
    job["log"] = []
    job["found"] = 0
    job["added"] = 0
    job["skipped"] = 0

    try:
        log("Loading existing handles from Notion...")
        notion_handles = get_existing_handles()
        persistent_seen = load_dedup()
        seen = notion_handles | persistent_seen
        log("Found " + str(len(notion_handles)) + " handles in Notion, " +
            str(len(persistent_seen)) + " in permanent dedup list ("
            + str(len(seen)) + " total unique).")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )

            # Load cookies
            if cookies_json:
                try:
                    cookies = json.loads(cookies_json)
                    context.add_cookies(cookies)
                    log("Session cookies loaded.")
                except Exception as e:
                    log("Cookie error: " + str(e))

            page = context.new_page()
            added_this_run = 0

            for keyword in keywords:
                if added_this_run >= limit:
                    break
                keyword = keyword.strip()
                if not keyword:
                    continue

                log("Searching: " + keyword)
                url = (MARKETPLACE_PREFIX + "/search?q=" + keyword.replace(" ", "+") +
                       "&country=" + country)
                page.goto(url, timeout=30000)
                time.sleep(random.uniform(3, 5))

                if not page.url.startswith(MARKETPLACE_PREFIX):
                    log("SAFETY STOP: navigated outside the Marketplace (" + page.url + "). Aborting run.")
                    break

                # Scroll to load results
                drifted = False
                for _ in range(3):
                    page.evaluate("window.scrollBy(0, 800)")
                    time.sleep(random.uniform(1, 2))
                    if not page.url.startswith(MARKETPLACE_PREFIX):
                        drifted = True
                        break
                if drifted:
                    log("SAFETY STOP: navigated outside the Marketplace (" + page.url + "). Aborting run.")
                    break

                # Find creator cards — selectors may need updating based on Meta's current DOM
                cards = page.query_selector_all("[data-testid='creator-card'], .creator-card, [role='article']")
                log("Found " + str(len(cards)) + " cards for: " + keyword)

                for card in cards:
                    if added_this_run >= limit:
                        break

                    try:
                        # Extract handle
                        handle_el = card.query_selector("[data-testid='creator-handle'], .creator-handle")
                        handle = handle_el.inner_text().strip() if handle_el else ""
                        if not handle:
                            # Try to get from link
                            link_el = card.query_selector("a[href*='instagram.com'], a[href*='facebook.com']")
                            if link_el:
                                href = link_el.get_attribute("href") or ""
                                handle = href.split("/")[-1].split("?")[0]

                        handle_key = handle.lower().strip().lstrip("@")
                        if handle_key in seen:
                            job["skipped"] += 1
                            continue

                        # Extract name
                        name_el = card.query_selector("[data-testid='creator-name'], .creator-name, h3, h4")
                        name = name_el.inner_text().strip() if name_el else handle

                        # Extract followers
                        followers_el = card.query_selector("[data-testid='follower-count'], .follower-count")
                        followers = parse_followers(followers_el.inner_text() if followers_el else "")

                        if followers_min is not None and (followers is None or followers < followers_min):
                            job["skipped"] += 1
                            continue
                        if followers_max is not None and (followers is None or followers > followers_max):
                            job["skipped"] += 1
                            continue

                        # Extract engagement rate (avg views / followers), if available
                        views_el = card.query_selector("[data-testid='avg-views'], .avg-views")
                        avg_views = parse_followers(views_el.inner_text() if views_el else "")
                        engagement_rate = None
                        if avg_views and followers:
                            engagement_rate = round((avg_views / followers) * 100, 2)

                        if min_er is not None and (engagement_rate is None or engagement_rate < min_er):
                            job["skipped"] += 1
                            continue

                        # Extract categories
                        cat_els = card.query_selector_all("[data-testid='creator-category'], .creator-category")
                        categories = [el.inner_text().strip() for el in cat_els]

                        # Profile URL
                        profile_link = card.query_selector("a")
                        profile_url = profile_link.get_attribute("href") if profile_link else ""

                        log("Adding: " + name + " (" + handle + ")" + (" · " + str(followers) + " followers" if followers else ""))
                        add_to_notion(name, "@" + handle_key if handle_key else handle, followers, categories, profile_url)
                        seen.add(handle_key)
                        job["added"] += 1
                        added_this_run += 1
                        job["found"] += 1

                        # Human-speed delay between creators
                        time.sleep(random.uniform(2, 4))

                    except Exception as e:
                        log("Error on card: " + str(e))
                        continue

                # Delay between keyword searches
                if added_this_run < limit:
                    delay = random.uniform(8, 15)
                    log("Waiting " + str(round(delay, 1)) + "s before next keyword...")
                    time.sleep(delay)

            browser.close()

        log("Done. Added " + str(job["added"]) + " new creators, skipped " + str(job["skipped"]) + " duplicates.")

    except Exception as e:
        log("Fatal error: " + str(e))
    finally:
        try:
            save_dedup(seen)
        except NameError:
            pass
        job["running"] = False


def start_scrape_thread(keywords, limit, cookies_json, country, filters=None):
    t = threading.Thread(target=run_scrape, args=(keywords, limit, cookies_json, country, filters), daemon=True)
    t.start()
