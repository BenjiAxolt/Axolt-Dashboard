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

_SCROLL_RESULTS_JS = """
() => {
    const card = document.querySelector("a[aria-label^='Open portfolio for ']");
    let el = card ? card.parentElement : null;
    while (el && el !== document.body) {
        const style = getComputedStyle(el);
        const scrollable = (style.overflowY === 'auto' || style.overflowY === 'scroll');
        if (scrollable && el.scrollHeight > el.clientHeight + 50) {
            el.scrollTop = el.scrollHeight;
            return 'container';
        }
        el = el.parentElement;
    }
    window.scrollTo(0, document.body.scrollHeight);
    return 'window';
}
"""


def _scroll_results(page):
    """Meta's card grid is rendered inside its own internally-scrollable
    div (the outer page/toolbar layout is fixed), so scrolling window/body
    is a no-op there — this walks up from a card element to find the real
    scrollable ancestor and scrolls that instead, falling back to window
    scroll only if no such container is found."""
    try:
        return page.evaluate(_SCROLL_RESULTS_JS)
    except Exception:
        return None


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


def _thumbnails_json_capped(thumbnails, max_len=2000):
    """Notion rich_text fields cap at 2000 chars, and this was silently
    failing the entire page write for any creator whose thumbnail URLs
    added up to more than that. Truncating the raw JSON string would risk
    producing invalid JSON, so this drops trailing thumbnails instead until
    the serialized list actually fits."""
    thumbs = list(thumbnails)
    while thumbs:
        encoded = json.dumps(thumbs)
        if len(encoded) <= max_len:
            return encoded
        thumbs.pop()
    return "[]"


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
        props["Post Thumbnails"] = {"rich_text": [{"text": {"content": _thumbnails_json_capped(creator["thumbnails"])}}]}
    if vet_result.get("email"):
        props["Email"] = {"email": vet_result["email"]}

    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": VETTING_QUEUE_DB}, "properties": props},
    )
    if r.status_code >= 300:
        # This call's result was never checked before — a failed write (bad
        # property name, invalid select option, etc.) would silently vanish
        # with the run log still showing "-> Review"/"-> Vetted" as if it
        # succeeded, since that log line only reflects vet_creator's verdict,
        # not whether the Notion page write actually went through.
        log("Vetting queue write FAILED (" + str(r.status_code) + "): " + r.text[:500])
    return r.status_code < 300


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


# Meta shows an optional "highlight" chip (a quality-signal badge, not the
# creator's name) right where the display name would otherwise be — present
# on some profiles, absent on others, which is why a fixed offset after the
# handle sometimes grabbed the badge text instead of the real name.
_HIGHLIGHT_BADGES = {
    "strong hooks", "high engagement", "consistent poster", "consistent posting",
    "fast growing", "fast growing audience", "highly responsive", "great retention",
    "loyal audience", "high completion rate", "brand safe", "brand safe content",
    "responsive creator", "growing audience", "high watch time", "quick responder",
}


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
    while idx < len(lines) and lines[idx].lower() in _HIGHLIGHT_BADGES:
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
    """Pulls post/reel thumbnail image URLs from the profile page's raw HTML,
    along with the specific post's link when the image is wrapped in an
    anchor tag — so a thumbnail can open that exact post instead of just the
    profile. Best-effort: if no anchor-wrapped images are found (Meta's
    markup for this varies), falls back to bare images with no post link,
    and the caller falls back further to the profile URL."""
    pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(?:(?!</a>).)*?<img[^>]+src="([^"]+)"', re.DOTALL)
    seen = set()
    thumbnails = []
    for link, src in pattern.findall(page_html):
        src = html.unescape(src)
        if not ("cdninstagram" in src or "scontent" in src) or src in seen:
            continue
        seen.add(src)
        link = html.unescape(link)
        full_link = link if link.startswith("http") else "https://business.facebook.com" + link
        thumbnails.append({"src": src, "link": full_link})
        if len(thumbnails) >= limit:
            return thumbnails

    if thumbnails:
        return thumbnails

    urls = re.findall(r'<img[^>]+src="([^"]+)"', page_html)
    return [
        {"src": html.unescape(u), "link": None}
        for u in urls if "cdninstagram" in u or "scontent" in u
    ][:limit]


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


def _follower_bucket_label_variants(name):
    """Meta renders bucket labels inconsistently: buckets under 100K use a
    plain hyphen with no spaces ("10K-25K"), but 100K+ buckets use a spaced
    en dash ("100K – 250K") — confirmed via the dropdown's own visible text
    dumped in the run log. Our stored bucket names all use the plain-hyphen
    form (matching the UI checkboxes and FOLLOWER_BUCKET_RANGES), so this
    generates the en-dash variant to try as a fallback when the exact-match
    click fails, rather than only ever failing silently on 100K-250K/250K-1M."""
    if "-" not in name:
        return [name]
    lo, hi = name.split("-", 1)
    return [name, lo + " – " + hi]


def _click_filter_option(page, role, name, debug_label=None):
    """Meta's filter checkboxes/radios expose real accessible roles/names
    (confirmed via DevTools — the class names themselves are Meta's
    auto-generated atomic CSS and change per build, useless as selectors).
    Falls back to matching visible text if the role guess is wrong, since
    that's still stable and matches our own UI labels exactly."""
    try:
        page.get_by_role(role, name=name, exact=True).click(timeout=5000)
        return True
    except Exception as e1:
        try:
            page.get_by_text(name, exact=(role != "checkbox")).first.click(timeout=5000)
            return True
        except Exception as e2:
            if debug_label:
                log(debug_label + " click failed for '" + name + "' — role attempt: " +
                    str(e1).split("\n")[0] + " | text attempt: " + str(e2).split("\n")[0])
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
                # Diagnostic: dump the dropdown's own visible option text so we
                # can see, from the run log alone, whether our bucket labels
                # ("10K-25K" etc.) actually match what Meta renders — without
                # needing anyone to manually inspect DevTools again.
                try:
                    dropdown_text = page.locator("[role='dialog'], [role='menu'], [role='listbox']").last.inner_text()
                    log("Followers dropdown text: " + dropdown_text.replace("\n", " | ")[:400])
                except Exception:
                    log("Could not read Followers dropdown text for diagnostics")

                for bucket in follower_buckets:
                    ok = False
                    for variant in _follower_bucket_label_variants(bucket):
                        ok = _click_filter_option(page, "checkbox", variant, debug_label="Followers")
                        if ok:
                            break
                    log(("Selected" if ok else "FAILED to select") + " follower bucket: " + bucket)
                    # Meta's filter re-fetches results over the network on
                    # every checkbox change — clicking the next box before
                    # that request lands risks it firing with a stale/
                    # incomplete selection snapshot. Give each one time to
                    # actually land instead of racing through all of them.
                    time.sleep(1.2)
                # Re-clicking the filter chip to close (instead of Escape) was
                # tried here to preserve the checkbox selection, but it broke
                # every subsequent card on the page — almost certainly because
                # it re-opens the dropdown rather than closing it, leaving an
                # overlay stuck open that then blocks "View profile" clicks
                # for the rest of the run. Back to Escape, which does not
                # cancel the selection here (confirmed: closing this way still
                # narrows results) — just paired with a longer settle time
                # before anything reads the card list.
                page.keyboard.press("Escape")
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                time.sleep(2)
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
                    _scroll_results(page)
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

                # Track progress by unique creator handle discovered, not raw
                # DOM element count — Meta's marketplace appears to virtualize
                # the grid (removing off-screen cards as new ones load in), so
                # the total element count can plateau even while genuinely new
                # creators keep appearing. Each newly-discovered card is
                # processed immediately, before further scrolling has a chance
                # to virtualize it back out of the DOM.
                discovered = set()
                stale_scrolls = 0
                MAX_STALE_SCROLLS = 12

                while added_this_run < limit and not _stop_requested:
                    cards = page.query_selector_all("a[aria-label^='Open portfolio for ']")
                    new_batch = []
                    for card in cards:
                        aria_label = card.get_attribute("aria-label") or ""
                        handle = aria_label.replace("Open portfolio for ", "").strip()
                        handle_key = handle.lower().strip().lstrip("@")
                        if handle_key and handle_key not in discovered:
                            discovered.add(handle_key)
                            new_batch.append((handle, handle_key, card))

                    if not new_batch:
                        scroll_kind = _scroll_results(page)
                        if stale_scrolls == 0:
                            log("Scroll target: " + str(scroll_kind))
                        time.sleep(random.uniform(3, 4.5))
                        if not page.url.startswith(MARKETPLACE_PREFIX):
                            log("SAFETY STOP: navigated outside the Marketplace (" + page.url + "). Aborting run.")
                            drifted = True
                            break
                        stale_scrolls += 1
                        if stale_scrolls >= MAX_STALE_SCROLLS:
                            log("No more cards to load for: " + keyword + " (" + str(len(discovered)) + " unique found)")
                            break
                        continue

                    stale_scrolls = 0

                    for handle, handle_key, card in new_batch:
                        if added_this_run >= limit or _stop_requested:
                            break
                        try:
                            if handle_key in seen:
                                job["skipped"] += 1
                                continue
                            if not re.fullmatch(r"[a-z0-9._]+", handle_key):
                                # Meta's aria-label occasionally gives us the
                                # creator's display name instead of their real
                                # @username (real IG handles never contain
                                # spaces or other punctuation) — with no
                                # username, there's no reliable way to link to
                                # or dedupe this creator, so skip rather than
                                # write broken links/data.
                                log(handle + " skipped — aria-label gave a display name, not a real @handle")
                                job["skipped"] += 1
                                continue

                            # The card's own href is an internal Meta Business Suite
                            # marketplace path (relative, session-dependent) — it
                            # doesn't open anywhere useful from our own dashboard,
                            # which is why "View profile" never worked. The real,
                            # always-clickable link is just the public Instagram
                            # profile URL, built straight from the handle.
                            profile_url = "https://www.instagram.com/" + handle_key + "/"

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
                                log(handle + ": profile opened in new tab (" + profile_page.url + ")")
                            except Exception as e:
                                profile_page = page
                                time.sleep(2)
                                log(handle + ": no new tab detected, falling back to same page (" + str(e) + ")")

                            try:
                                # The post grid appears to lazy-load — without
                                # scrolling, only whatever renders by default
                                # (as few as 3-4 posts) is in the DOM,
                                # undershooting the 6-thumbnail target.
                                try:
                                    profile_page.evaluate("window.scrollBy(0, 900)")
                                    time.sleep(1.5)
                                except Exception:
                                    pass

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
                            finally:
                                # Guaranteed even on error — an unclosed profile
                                # tab is a whole extra Chromium renderer process
                                # left running, and enough of those leaking
                                # across a long run is what was crashing the
                                # browser with "Target crashed" errors.
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
                            if "crashed" in str(e).lower():
                                # The browser itself is dead — every further
                                # action on this page will fail the same way,
                                # so retrying per-card just spams the log
                                # forever instead of ending the run.
                                log("Browser target crashed — stopping run: " + str(e))
                                _stop_requested = True
                                break
                            log("Error on card: " + str(e))
                            continue

                if drifted or _stop_requested:
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
