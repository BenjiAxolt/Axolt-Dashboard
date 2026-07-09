import copy
import html
import json
import re
import time
import random
import threading
import requests
import os

import psutil

from notion_settings import increment_counter
from templates_store import get_template_by_key, html_to_text
from vetting import vet_creator

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
INFLUENCER_DB = "f07a187424e64bc7b1b992ceced311c5"
VETTING_QUEUE_DB = "2aec417ae85343dc96049ae73abe9df8"

NOTION_HEADERS = {
    "Authorization": "Bearer " + NOTION_TOKEN,
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

MARKETPLACE_PREFIX = "https://business.facebook.com/latest/creator_marketplace"
MARKETPLACE_URL = os.environ.get(
    "MARKETPLACE_URL",
    MARKETPLACE_PREFIX + "/creators/search?business_id=1336144087729718&asset_id=415584441641514",
)

DEDUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dedup_list.json")
JOB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_state.json")

DEFAULT_JOB = {
    "running": False,
    "log": [],
    "found": 0,
    "added": 0,
    "skipped": 0,
    "auto_skipped": 0,
    "vetted": 0,
    "review": 0,
}


def load_job():
    if not os.path.exists(JOB_FILE):
        return copy.deepcopy(DEFAULT_JOB)
    try:
        with open(JOB_FILE, "r") as f:
            loaded = json.load(f)
        # A restored process never has an actual browser running, even if
        # the last write said otherwise (e.g. the process crashed mid-run).
        loaded["running"] = False
        return loaded
    except Exception:
        return copy.deepcopy(DEFAULT_JOB)


def save_job():
    try:
        with open(JOB_FILE, "w") as f:
            json.dump(job, f)
    except Exception:
        pass


# Global job state — restored from disk so a crash/restart doesn't wipe the
# last run's results before the user has a chance to see them.
job = load_job()

# Playwright's sync API is not thread-safe — the browser can only be driven
# from the thread that launched it. So Stop/watchdog requests (which fire
# from other threads) can't call browser.close() directly; they set this
# flag, and the scrape loop itself (running on its own thread) checks it
# between steps and closes its own browser.
_stop_requested = False


def stop_scrape():
    global _stop_requested
    if job.get("running"):
        log("Stop requested — will halt after the current step...")
        _stop_requested = True
        return True
    return False


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
    job["last_log_time"] = time.time()
    print(msg)
    save_job()


def _mem_mb():
    """Current resident memory across this process AND its children (the
    Playwright driver + Chromium browser + renderer processes), in MB.
    Chromium runs as separate child processes, so measuring only this
    Python process (as an earlier version of this did) massively undercounts
    the real footprint that actually trips Render's 512MB limit."""
    try:
        proc = psutil.Process(os.getpid())
        total = proc.memory_info().rss
        for child in proc.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        return round(total / (1024 * 1024), 1)
    except Exception:
        return -1


def _watchdog(idle_limit_seconds=120):
    """Runs alongside the scrape — if no log() has fired in idle_limit_seconds
    (a real hang, not just a slow-but-progressing run), flag the run to stop
    so it closes its own browser and ends instead of hanging forever."""
    while job.get("running"):
        time.sleep(15)
        if not job.get("running"):
            break
        last = job.get("last_log_time", time.time())
        if time.time() - last > idle_limit_seconds:
            log("Watchdog: no progress for " + str(idle_limit_seconds) + "s — flagging the run to stop.")
            stop_scrape()
            break


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


def add_to_vetting_queue(creator, vet_result, country):
    props = {
        "Name": {"title": [{"text": {"content": creator.get("name") or creator.get("handle") or "Unknown"}}]},
        "Handle": {"rich_text": [{"text": {"content": creator.get("handle", "")}}]},
        "Outcome": {"select": {"name": vet_result["outcome"]}},
        "Niche": {"rich_text": [{"text": {"content": ", ".join(creator.get("categories", []))}}]},
        "Tags": {"multi_select": [{"name": t} for t in vet_result.get("tags", [])[:5]]},
        "Country": {"select": {"name": country}},
        "AI Analysis": {"rich_text": [{"text": {"content": vet_result.get("analysis", "")[:2000]}}]},
        "Flag Note": {"rich_text": [{"text": {"content": vet_result.get("flag_note", "")}}]},
        "Bio": {"rich_text": [{"text": {"content": (creator.get("bio") or "")[:2000]}}]},
    }
    if creator.get("followers"):
        props["Followers"] = {"number": creator["followers"]}
    if creator.get("engagement_rate") is not None:
        props["Engagement Rate"] = {"number": creator["engagement_rate"]}
    if creator.get("profile_url"):
        props["Profile URL"] = {"url": creator["profile_url"]}
    if creator.get("thumbnails"):
        props["Post Thumbnails"] = {"rich_text": [{"text": {"content": json.dumps(creator["thumbnails"])}}]}
    if vet_result.get("email"):
        props["Email"] = {"email": vet_result["email"]}

    requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": VETTING_QUEUE_DB}, "properties": props},
    )


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


AGE_RE = re.compile(r"Aged\s+([\d]+-[\d]+|\d+\+)")
GENDER_VALUES = ["Female", "Male", "Non-binary"]
STAT_VALUE_RE = re.compile(r"^[\d.,]+[KM%]?$")


def parse_insights_stats(text):
    """Parses the 'Total followers / Interaction rate / ...' block into a dict."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    stats = {}
    i = 0
    while i < len(lines):
        if STAT_VALUE_RE.match(lines[i]):
            value = lines[i]
            label = lines[i + 1].replace("ⓘ", "").strip() if i + 1 < len(lines) else ""
            if label:
                stats[label] = value
            i += 2
            if i < len(lines) and lines[i].startswith("Last"):
                i += 1
        else:
            i += 1
    return stats


def parse_profile_meta(body_text):
    """Extracts age/gender from the profile page's full text (Meta's CSS classes
    are randomly generated per build, so we pattern-match text instead)."""
    meta = {"age": None, "gender": None}
    age_m = AGE_RE.search(body_text)
    if age_m:
        meta["age"] = age_m.group(1)
    for g in GENDER_VALUES:
        if re.search(r"(?<![A-Za-z])" + g + r"(?![A-Za-z])", body_text):
            meta["gender"] = g
            break
    return meta


def parse_name_bio(body_text, handle_key):
    """Extracts real display name and bio, which sit right after the handle in
    the profile page's text, before the country/gender/age line cluster starts."""
    raw_lines = [l.strip().replace("​", "") for l in body_text.split("\n")]
    lines = [l for l in raw_lines if l]

    idx = None
    for i, l in enumerate(lines):
        if l.lower().lstrip("@") == handle_key:
            idx = i
            break
    if idx is None:
        return "", ""

    idx += 1
    if idx < len(lines) and lines[idx] == "Responsive":
        idx += 1
    name = lines[idx] if idx < len(lines) else ""
    idx += 1

    stop_idx = len(lines)
    for i in range(idx, len(lines)):
        if lines[i] in GENDER_VALUES or AGE_RE.match(lines[i]):
            stop_idx = max(idx, i - 1)  # exclude the country line just before gender/age
            break

    bio = html.unescape("\n".join(lines[idx:stop_idx]).strip())
    return html.unescape(name), bio


def extract_thumbnails(page_html, limit=6):
    """Pulls post/reel thumbnail image URLs from the profile page's raw HTML."""
    urls = re.findall(r'<img[^>]+src="([^"]+)"', page_html)
    thumbnails = [html.unescape(u) for u in urls if "cdninstagram" in u or "scontent" in u]
    return thumbnails[:limit]


FOLLOWER_BUCKET_RANGES = {
    "Under 10K": (0, 10_000),
    "10K-25K": (10_000, 25_000),
    "25K-50K": (25_000, 50_000),
    "50K-75K": (50_000, 75_000),
    "75K-100K": (75_000, 100_000),
    "100K-250K": (100_000, 250_000),
    "250K-1M": (250_000, 1_000_000),
    "Over 1M": (1_000_000, None),
}

INTERACTION_RATE_THRESHOLDS = {
    "Over 3%": 3.0,
    "Over 5%": 5.0,
    "Over 10%": 10.0,
}


COUNTRY_NAMES = {
    "US": "United States",
    "GB": "United Kingdom",
}


def _filter_toolbar(page):
    """The filter chip's own text label div (e.g. "Followers") has no role —
    but DevTools confirmed its immediate parent does: <div role="combobox"
    tabindex="0" id="js_gt">, wrapping both the label and the chevron icon.
    "Countries" is the one label guaranteed not to collide with anything
    else on the page (creator cards duplicate "Followers"/"More"-ish text
    in their stats), so it anchors a scoped locator for the smallest
    container wrapping every filter chip, used as a fallback below."""
    return page.locator("div:has-text('Countries'):has-text('Followers'):has-text('More')").last


def _open_filter_dropdown(page, label):
    # Primary: the role="combobox" wrapper really exists (confirmed via
    # DevTools) — exact=True kept failing, so this assumes the accessible
    # name isn't a clean exact match (likely includes the chevron icon) and
    # uses a substring match instead.
    try:
        page.get_by_role("combobox", name=label, exact=False).first.click(timeout=4000)
        return True
    except Exception:
        pass
    toolbar = _filter_toolbar(page)
    try:
        toolbar.get_by_text(label, exact=True).first.click(timeout=4000)
        return True
    except Exception:
        return False


def _click_filter_option(page, role, name):
    """Meta's filter checkboxes/radios expose real accessible roles/names
    (confirmed via DevTools — the class names themselves are Meta's
    auto-generated atomic CSS and change per build, useless as selectors).
    Falls back to matching visible text if the role guess is wrong, since
    that's still stable and matches our own UI labels exactly."""
    try:
        page.get_by_role(role, name=name, exact=True).click(timeout=5000)
        return True
    except Exception:
        try:
            page.get_by_text(name, exact=(role != "checkbox")).first.click(timeout=5000)
            return True
        except Exception:
            return False


def apply_marketplace_filters(page, country, follower_buckets, interaction_rate):
    """These are dropdown menus with no 'Apply' button — checking a box
    filters live, and pressing Escape closes the dropdown without losing
    the selection. Each filter is wrapped separately so one failing (e.g.
    a filter Meta has redesigned since) doesn't block the others or abort
    the run — it just logs and moves on with whatever did apply."""
    country_name = COUNTRY_NAMES.get(country)
    if country_name:
        try:
            if not _open_filter_dropdown(page, "Countries"):
                log("Could not open Countries filter dropdown")
            else:
                time.sleep(1)
                if not _click_filter_option(page, "checkbox", country_name):
                    log("Could not select country filter: " + country_name)
                page.keyboard.press("Escape")
                time.sleep(1)
        except Exception as e:
            log("Country filter error: " + str(e))

    if follower_buckets:
        try:
            if not _open_filter_dropdown(page, "Followers"):
                log("Could not open Followers filter dropdown")
            else:
                time.sleep(1)
                for bucket in follower_buckets:
                    if not _click_filter_option(page, "checkbox", bucket):
                        log("Could not select follower bucket: " + bucket)
                    time.sleep(0.3)
                page.keyboard.press("Escape")
                time.sleep(1)
        except Exception as e:
            log("Followers filter error: " + str(e))

    if interaction_rate:
        try:
            # Unlike Followers/Countries, "More" opens a full "Creator
            # filters" modal (confirmed via DevTools) rather than a plain
            # dropdown — selections inside it don't take effect live, and
            # pressing Escape just cancels the whole dialog. It has its own
            # "Show creators" button that must be clicked to actually apply
            # the selection (and it closes the modal itself).
            if not _open_filter_dropdown(page, "More"):
                log("Could not open More filter dropdown")
            else:
                time.sleep(1)
                if not _click_filter_option(page, "radio", interaction_rate):
                    log("Could not select interaction rate: " + interaction_rate)
                    page.keyboard.press("Escape")
                else:
                    try:
                        page.get_by_text("Show creators", exact=True).first.click(timeout=5000)
                    except Exception:
                        log("Could not find Show creators button — closing without applying")
                        page.keyboard.press("Escape")
                time.sleep(1)
        except Exception as e:
            log("Interaction rate filter error: " + str(e))


def followers_in_buckets(followers, bucket_names):
    if not bucket_names or followers is None:
        return not bucket_names
    for name in bucket_names:
        lo, hi = FOLLOWER_BUCKET_RANGES.get(name, (None, None))
        if lo is None:
            continue
        if followers >= lo and (hi is None or followers < hi):
            return True
    return False


def run_scrape(keywords, limit, cookies_json, country, filters=None):
    from playwright.sync_api import sync_playwright

    global _stop_requested
    _stop_requested = False
    filters = filters or {}
    follower_buckets = filters.get("follower_buckets") or []
    min_er = INTERACTION_RATE_THRESHOLDS.get(filters.get("interaction_rate"))

    job["running"] = True
    job["log"] = []
    job["found"] = 0
    job["added"] = 0
    job["skipped"] = 0
    job["auto_skipped"] = 0
    job["vetted"] = 0
    job["review"] = 0

    try:
        log("Loading existing handles from Notion...")
        notion_handles = get_existing_handles()
        persistent_seen = load_dedup()
        seen = notion_handles | persistent_seen
        log("Found " + str(len(notion_handles)) + " handles in Notion, " +
            str(len(persistent_seen)) + " in permanent dedup list ("
            + str(len(seen)) + " total unique).")

        brand_brief_row = get_template_by_key("brand_brief")
        brand_brief = html_to_text(brand_brief_row["content"]) if brand_brief_row else ""
        if not brand_brief:
            log("Warning: no Brand Brief set in Templates — vetting will judge with no brief context.")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--disable-features=site-per-process,TranslateUI",
                    "--js-flags=--max-old-space-size=256",
                ],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )

            # Block only fonts/video — leaving images alone. Blocking images
            # broke the marketplace's lazy-loaded card list entirely (0 cards
            # found), so this page's infinite-scroll rendering appears to
            # depend on image load events completing.
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("media", "font")
                else route.continue_(),
            )

            # Load cookies
            if cookies_json:
                try:
                    cookies = normalize_cookies(json.loads(cookies_json))
                    context.add_cookies(cookies)
                    log("Session cookies loaded.")
                except Exception as e:
                    log("Cookie error: " + str(e))

            page = context.new_page()
            added_this_run = 0

            for keyword in keywords:
                if added_this_run >= limit:
                    break
                if _stop_requested:
                    log("Stop requested — ending run.")
                    break
                keyword = keyword.strip()
                if not keyword:
                    continue

                log("Searching: " + keyword + " (mem: " + str(_mem_mb()) + " MB)")
                page.goto(MARKETPLACE_URL, timeout=30000)
                time.sleep(random.uniform(3, 5))

                if not page.url.startswith(MARKETPLACE_PREFIX):
                    log("SAFETY STOP: navigated outside the Marketplace (" + page.url + "). Aborting run.")
                    break

                # Type the keyword into the real search box (client-side app, not URL-searchable)
                search_box = None
                for selector in [
                    "input[placeholder='Search']",
                    "input[type='search']",
                    "input[aria-label*='Search' i]",
                    "input[placeholder*='Search' i]",
                ]:
                    try:
                        page.wait_for_selector(selector, timeout=8000, state="visible")
                        search_box = page.query_selector(selector)
                        if search_box:
                            break
                    except Exception:
                        continue

                if search_box:
                    search_box.fill(keyword)
                    search_box.press("Enter")
                    time.sleep(random.uniform(2, 4))
                else:
                    log("Page title/url for debugging: " + page.title() + " | " + page.url)
                    log("Could not find search box — selector needs verification.")

                apply_marketplace_filters(page, country, follower_buckets, filters.get("interaction_rate"))
                time.sleep(random.uniform(1, 2))

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

                # Creator cards: Meta's CSS classes are randomly generated per build and
                # useless as selectors — the one stable anchor is this aria-label pattern.
                cards = page.query_selector_all("a[aria-label^='Open portfolio for ']")
                log("Found " + str(len(cards)) + " cards for: " + keyword + " (mem: " + str(_mem_mb()) + " MB)")

                card_index = 0
                stale_scrolls = 0
                MAX_STALE_SCROLLS = 6  # give up on this keyword once scrolling stops loading anything new

                while added_this_run < limit and not _stop_requested:
                    cards = page.query_selector_all("a[aria-label^='Open portfolio for ']")

                    if card_index >= len(cards):
                        prev_count = len(cards)
                        page.evaluate("window.scrollBy(0, 1000)")
                        time.sleep(random.uniform(1.5, 2.5))
                        if not page.url.startswith(MARKETPLACE_PREFIX):
                            log("SAFETY STOP: navigated outside the Marketplace (" + page.url + "). Aborting run.")
                            drifted = True
                            break
                        cards = page.query_selector_all("a[aria-label^='Open portfolio for ']")
                        if len(cards) <= prev_count:
                            stale_scrolls += 1
                            if stale_scrolls >= MAX_STALE_SCROLLS:
                                log("No more cards to load for: " + keyword + " (found " + str(len(cards)) + " total)")
                                break
                            continue
                        stale_scrolls = 0
                        continue

                    if _stop_requested:
                        log("Stop requested — ending run.")
                        break

                    try:
                        card = cards[card_index]

                        aria_label = card.get_attribute("aria-label") or ""
                        handle = aria_label.replace("Open portfolio for ", "").strip()
                        handle_key = handle.lower().strip().lstrip("@")
                        if not handle_key or handle_key in seen:
                            job["skipped"] += 1
                            continue

                        profile_url = card.get_attribute("href") or ""

                        # A modal left over from the previous card can block this
                        # card's click and stall for the full 30s timeout — clear
                        # it defensively before clicking in.
                        page.keyboard.press("Escape")
                        time.sleep(0.5)

                        # Click into the post-detail modal, then "View profile" for full stats
                        card.click(timeout=15000)
                        time.sleep(2)

                        view_profile_btn = page.get_by_text("View profile", exact=True)
                        if view_profile_btn.count() == 0:
                            log("Could not open profile for " + handle + " — skipping.")
                            close_btn = page.query_selector("[aria-label='Close'], [aria-label='close']")
                            if close_btn:
                                close_btn.click()
                            else:
                                page.keyboard.press("Escape")
                            job["skipped"] += 1
                            continue

                        try:
                            with context.expect_page(timeout=5000) as new_page_info:
                                view_profile_btn.first.click()
                            profile_page = new_page_info.value
                            profile_page.wait_for_load_state()
                            time.sleep(2)
                        except Exception:
                            profile_page = page
                            time.sleep(2)

                        body_text = profile_page.inner_text("body")
                        meta = parse_profile_meta(body_text)
                        real_name, bio = parse_name_bio(body_text, handle_key)
                        thumbnails = extract_thumbnails(profile_page.content())

                        insights_el = profile_page.query_selector("[data-pagelet='CreatorProfileInsightsOverview']")
                        stats = parse_insights_stats(insights_el.inner_text()) if insights_el else {}
                        followers = parse_followers(stats.get("Total followers"))
                        engagement_rate = None
                        if stats.get("Interaction rate"):
                            try:
                                engagement_rate = float(stats["Interaction rate"].replace("%", ""))
                            except Exception:
                                engagement_rate = None

                        if profile_page is not page:
                            profile_page.close()

                        # Close the post-detail modal on the main page before continuing
                        close_btn = page.query_selector("[aria-label='Close'], [aria-label='close']")
                        if close_btn:
                            close_btn.click()
                        else:
                            page.keyboard.press("Escape")
                        time.sleep(1)

                        if not followers_in_buckets(followers, follower_buckets):
                            log(handle + " skipped — followers " + str(followers) + " outside selected buckets " + str(follower_buckets))
                            job["skipped"] += 1
                            continue
                        if min_er is not None and (engagement_rate is None or engagement_rate < min_er):
                            log(handle + " skipped — engagement rate " + str(engagement_rate) + " below threshold " + str(min_er))
                            job["skipped"] += 1
                            continue

                        creator = {
                            "name": real_name or handle,
                            "handle": "@" + handle_key,
                            "followers": followers,
                            "engagement_rate": engagement_rate,
                            "categories": [],
                            "bio": bio,
                            "profile_url": profile_url,
                            "thumbnails": thumbnails,
                            "gender": meta.get("gender"),
                            "age": meta.get("age"),
                        }

                        log("Vetting: " + handle)
                        try:
                            vet_result = vet_creator(creator, brand_brief)
                        except Exception as e:
                            log("Vetting error for " + handle + ": " + str(e))
                            job["skipped"] += 1
                            continue

                        add_to_vetting_queue(creator, vet_result, country)
                        seen.add(handle_key)
                        job["found"] += 1
                        job["added"] += 1
                        added_this_run += 1
                        increment_counter("sv_total_scraped")
                        if vet_result["outcome"] == "Auto-skipped":
                            job["auto_skipped"] += 1
                            increment_counter("sv_total_auto_skipped")
                        elif vet_result["outcome"] == "Vetted":
                            job["vetted"] += 1
                            increment_counter("sv_total_vetted")
                        else:
                            job["review"] += 1
                            increment_counter("sv_total_review")
                        log(handle + " -> " + vet_result["outcome"] + " (mem: " + str(_mem_mb()) + " MB)")

                        # Human-speed delay between creators
                        time.sleep(random.uniform(2, 4))

                    except Exception as e:
                        log("Error on card: " + str(e))
                        continue
                    finally:
                        card_index += 1

                if drifted:
                    break

                # Delay between keyword searches
                if added_this_run < limit and not _stop_requested:
                    delay = random.uniform(8, 15)
                    log("Waiting " + str(round(delay, 1)) + "s before next keyword...")
                    time.sleep(delay)

            browser.close()

        log("Done. Vetted " + str(job["added"]) + " creators (" +
            str(job["vetted"]) + " Vetted, " + str(job["review"]) + " Review, " +
            str(job["auto_skipped"]) + " Auto-skipped). Skipped " + str(job["skipped"]) + " (duplicate/filtered).")

    except Exception as e:
        log("Fatal error: " + str(e))
    finally:
        try:
            save_dedup(seen)
        except NameError:
            pass
        job["running"] = False
        save_job()


def start_scrape_thread(keywords, limit, cookies_json, country, filters=None):
    job["running"] = True
    job["last_log_time"] = time.time()
    t = threading.Thread(target=run_scrape, args=(keywords, limit, cookies_json, country, filters), daemon=True)
    t.start()
    threading.Thread(target=_watchdog, daemon=True).start()
