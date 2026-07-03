import os
import requests
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
INFLUENCER_DB = "f07a187424e64bc7b1b992ceced311c5"
CLINIC_DB = "cb01c955a4664a1eb0d66c1f835f1243"

NOTION_HEADERS = {
    "Authorization": "Bearer " + NOTION_TOKEN,
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def query_db(db_id):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/dashboard")
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


@app.route("/api/scrape/start", methods=["POST"])
def scrape_start():
    from scraper import job, start_scrape_thread
    if job["running"]:
        return jsonify({"error": "Already running"}), 400
    data = request.json or {}
    keywords = [k.strip() for k in data.get("keywords", "").split("\n") if k.strip()]
    limit = min(int(data.get("limit", 15)), 25)
    cookies_json = data.get("cookies", "")
    if not keywords:
        return jsonify({"error": "No keywords provided"}), 400
    start_scrape_thread(keywords, limit, cookies_json)
    return jsonify({"status": "started"})


@app.route("/api/scrape/status")
def scrape_status():
    from scraper import job
    return jsonify(job)


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
