import os
import requests
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from notion_settings import get_setting, set_setting

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-prod")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
INFLUENCER_DB = "f07a187424e64bc7b1b992ceced311c5"
CLINIC_DB = "cb01c955a4664a1eb0d66c1f835f1243"
VETTING_QUEUE_DB = "2aec417ae85343dc96049ae73abe9df8"
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "BenjiAxolt")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

NOTION_HEADERS = {
    "Authorization": "Bearer " + NOTION_TOKEN,
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def query_db(db_id, filter_body=None):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if filter_body:
            body["filter"] = filter_body
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            "https://api.notion.com/v1/databases/" + db_id + "/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def get_prop(page, name):
    p = page.get("properties", {}).get(name, {})
    if not p:
        return None
    t = p.get("type")
    if t == "title":
        return "".join(i.get("plain_text", "") for i in p.get("title", [])).strip()
    if t == "rich_text":
        return "".join(i.get("plain_text", "") for i in p.get("rich_text", [])).strip()
    if t == "select":
        s = p.get("select") or {}
        return s.get("name")
    if t == "checkbox":
        return p.get("checkbox", False)
    if t == "date":
        d = p.get("date") or {}
        return d.get("start")
    if t == "number":
        return p.get("number")
    if t == "multi_select":
        return [s.get("name", "") for s in p.get("multi_select", [])]
    if t == "email":
        return p.get("email")
    if t == "url":
        return p.get("url")
    return None


def fmt_date(ds):
    if not ds:
        return ""
    try:
        d = datetime.fromisoformat(ds.replace("Z", "+00:00"))
        return d.strftime("%-d %b")
    except Exception:
        return ds[:10]


def fmt_followers(n):
    if not n:
        return ""
    if n >= 1000000:
        return str(round(n / 1000000, 1)) + "M"
    if n >= 1000:
        return str(round(n / 1000, 1)) + "K"
    return str(int(n))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if (request.form.get("username") == DASHBOARD_USER and
                request.form.get("password") == DASHBOARD_PASS):
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/dashboard")
@login_required
def dashboard_data():
    inf_pages = query_db(INFLUENCER_DB)
    clinic_pages = query_db(CLINIC_DB)

    inf_pages = [p for p in inf_pages if get_prop(p, "Stage") != "Duplicate"]
    clinic_pages = [p for p in clinic_pages if get_prop(p, "Stage") != "Duplicate"]

    CONTACTED = ["Contacted", "Replied", "Intake Survey Filled", "Product Delivered",
                 "14-Day Survey Sent", "14-Day Survey Filled",
                 "30-Day Survey Sent", "30-Day Survey Filled", "Unresponsive", "Declined", "Not Now"]
    INTAKE    = ["Intake Survey Filled", "Product Delivered",
                 "14-Day Survey Sent", "14-Day Survey Filled",
                 "30-Day Survey Sent", "30-Day Survey Filled"]
    DELIVERED = ["Product Delivered", "14-Day Survey Sent", "14-Day Survey Filled",
                 "30-Day Survey Sent", "30-Day Survey Filled"]
    S14       = ["14-Day Survey Sent", "14-Day Survey Filled"]
    S30       = ["30-Day Survey Sent", "30-Day Survey Filled"]

    total_inf    = len(inf_pages)
    contacted    = sum(1 for p in inf_pages if get_prop(p, "Stage") in CONTACTED)
    intake       = sum(1 for p in inf_pages if get_prop(p, "Stage") in INTAKE)
    delivered    = sum(1 for p in inf_pages if get_prop(p, "Stage") in DELIVERED)
    survey_14    = sum(1 for p in inf_pages if get_prop(p, "Stage") in S14)
    survey_30    = sum(1 for p in inf_pages if get_prop(p, "Stage") in S30)
    declined_inf = sum(1 for p in inf_pages if get_prop(p, "Stage") == "Declined")

    seeded = []
    for p in inf_pages:
        if get_prop(p, "Stage") in DELIVERED:
            stage = get_prop(p, "Stage") or ""
            if "30-Day" in stage:
                sc, sb = "#534AB7", "rgba(83,74,183,0.15)"
            elif "14-Day" in stage:
                sc, sb = "#7F77DD", "rgba(127,119,221,0.15)"
            else:
                sc, sb = "#639922", "rgba(99,153,34,0.15)"
            seeded.append({
                "id": (get_prop(p, "Name") or "unknown").lower().replace(" ", "_").replace("'", ""),
                "name": get_prop(p, "Name") or "Unknown",
                "handle": get_prop(p, "Handle") or "",
                "followers": fmt_followers(get_prop(p, "Followers")),
                "category": ", ".join(get_prop(p, "Category") or []),
                "delivered": fmt_date(get_prop(p, "Product Delivered")),
                "stage": stage,
                "sc": sc,
                "sb": sb,
            })

    total_cli    = len(clinic_pages)
    cli_replied  = sum(1 for p in clinic_pages if get_prop(p, "Stage") not in [None, "Lead", "Contacted"])
    cli_meeting  = sum(1 for p in clinic_pages if get_prop(p, "Stage") in ["Meeting Booked", "Meeting Held"])
    cli_partner  = sum(1 for p in clinic_pages if get_prop(p, "Stage") in ["Partnership Agreed", "Active"])
    cli_declined = sum(1 for p in clinic_pages if get_prop(p, "Stage") == "Declined")

    return jsonify({
        "influencers": {
            "total": total_inf,
            "contacted": contacted,
            "intake": intake,
            "delivered": delivered,
            "survey_14": survey_14,
            "survey_30": survey_30,
            "declined": declined_inf,
            "seeded": seeded,
        },
        "clinics": {
            "total": total_cli,
            "replied": cli_replied,
            "meeting": cli_meeting,
            "partner": cli_partner,
            "declined": cli_declined,
        },
        "updated": datetime.now(timezone.utc).strftime("%-d %b %Y %H:%M UTC"),
    })


@app.route("/api/settings/brand-brief", methods=["GET", "POST"])
@login_required
def brand_brief():
    if request.method == "POST":
        data = request.json or {}
        set_setting("brand_brief", data.get("text", ""))
        return jsonify({"status": "saved"})
    return jsonify({"text": get_setting("brand_brief")})


def vetting_page_to_dict(page):
    return {
        "id": page["id"],
        "name": get_prop(page, "Name") or "",
        "handle": get_prop(page, "Handle") or "",
        "niche": get_prop(page, "Niche") or "",
        "tags": get_prop(page, "Tags") or [],
        "country": get_prop(page, "Country") or "",
        "followers": get_prop(page, "Followers"),
        "engagement_rate": get_prop(page, "Engagement Rate"),
        "avg_views": get_prop(page, "Avg Views"),
        "email": get_prop(page, "Email") or "",
        "profile_url": get_prop(page, "Profile URL") or "",
        "bio": get_prop(page, "Bio") or "",
        "analysis": get_prop(page, "AI Analysis") or "",
        "flag_note": get_prop(page, "Flag Note") or "",
    }


@app.route("/api/vetting/list")
@login_required
def vetting_list():
    outcome = request.args.get("outcome", "Vetted")
    if outcome not in ("Vetted", "Review"):
        return jsonify({"error": "Invalid outcome"}), 400
    pages = query_db(VETTING_QUEUE_DB, filter_body={
        "property": "Outcome", "select": {"equals": outcome}
    })
    return jsonify({"creators": [vetting_page_to_dict(p) for p in pages]})


@app.route("/api/vetting/approve", methods=["POST"])
@login_required
def vetting_approve():
    data = request.json or {}
    page_id = data.get("id")
    if not page_id:
        return jsonify({"error": "Missing id"}), 400

    r = requests.get("https://api.notion.com/v1/pages/" + page_id, headers=NOTION_HEADERS)
    page = r.json()
    creator = vetting_page_to_dict(page)

    props = {
        "Name": {"title": [{"text": {"content": creator["name"] or creator["handle"] or "Unknown"}}]},
        "Stage": {"select": {"name": "Lead"}},
    }
    if creator["handle"]:
        props["Handle"] = {"rich_text": [{"text": {"content": creator["handle"]}}]}
    if creator["followers"]:
        props["Followers"] = {"number": creator["followers"]}
    if creator["email"]:
        props["Email"] = {"email": creator["email"]}
    if creator["profile_url"]:
        props["Social Media"] = {"url": creator["profile_url"]}
    if creator["tags"]:
        props["Category"] = {"multi_select": [{"name": t} for t in creator["tags"][:5]]}

    requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": INFLUENCER_DB}, "properties": props},
    )
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"archived": True},
    )
    return jsonify({"status": "approved"})


@app.route("/api/vetting/skip", methods=["POST"])
@login_required
def vetting_skip():
    data = request.json or {}
    page_id = data.get("id")
    if not page_id:
        return jsonify({"error": "Missing id"}), 400
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"archived": True},
    )
    return jsonify({"status": "skipped"})


@app.route("/api/vetting/move-to-vetted", methods=["POST"])
@login_required
def vetting_move_to_vetted():
    data = request.json or {}
    page_id = data.get("id")
    email = (data.get("email") or "").strip()
    if not page_id:
        return jsonify({"error": "Missing id"}), 400
    if not email:
        return jsonify({"error": "Email is required to move to Vetted"}), 400
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {
            "Outcome": {"select": {"name": "Vetted"}},
            "Email": {"email": email},
            "Flag Note": {"rich_text": []},
        }},
    )
    return jsonify({"status": "moved"})


@app.route("/api/scrape/start", methods=["POST"])
@login_required
def scrape_start():
    from scraper import job, start_scrape_thread
    if job["running"]:
        return jsonify({"error": "Already running"}), 400
    data = request.json or {}
    keywords = [k.strip() for k in data.get("keywords", "").split("\n") if k.strip()]
    country = data.get("country", "")
    limit = min(int(data.get("limit", 15)), 25)
    cookies_json = data.get("cookies", "")
    if not keywords:
        return jsonify({"error": "No keywords provided"}), 400
    if country not in ("US", "GB"):
        return jsonify({"error": "Choose a valid country"}), 400
    filters = {
        "followers_min": data.get("followers_min"),
        "followers_max": data.get("followers_max"),
        "min_er": data.get("min_er"),
    }
    start_scrape_thread(keywords, limit, cookies_json, country, filters)
    return jsonify({"status": "started"})


@app.route("/api/scrape/status")
@login_required
def scrape_status():
    from scraper import job
    return jsonify(job)


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
