import os
import requests
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from notion_settings import get_setting, set_setting, get_counter, increment_counter
import auth_store
import flags_store
import email_sender
import vetting

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
        if session.get("must_reset") and request.endpoint != "set_password":
            return redirect(url_for("set_password"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        if session.get("role") != "Admin":
            return jsonify({"error": "Admin access required"}), 403
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
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        user = auth_store.find_user(username)
        if user and user["password_hash"] and auth_store.verify_password(password, user["password_hash"]):
            session["logged_in"] = True
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["must_reset"] = user["must_reset"]
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
        elif username == DASHBOARD_USER and password == DASHBOARD_PASS and DASHBOARD_PASS:
            # Bootstrap admin account from env vars — always Admin, no forced reset.
            session["logged_in"] = True
            session["username"] = DASHBOARD_USER
            session["role"] = "Admin"
            session["must_reset"] = False
            session["user_id"] = None
            return redirect(url_for("index"))
        else:
            error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/set-password", methods=["GET", "POST"])
def set_password():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        new_password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(new_password) < 8:
            error = "Password must be at least 8 characters."
        elif new_password != confirm:
            error = "Passwords don't match."
        elif not session.get("user_id"):
            error = "Cannot reset this account's password."
        else:
            auth_store.set_password(session["user_id"], new_password, must_reset=False)
            session["must_reset"] = False
            return redirect(url_for("index"))
    return render_template("set_password.html", error=error)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        user = auth_store.find_user(username) if username else None
        # Always show the same message, whether or not the username matched —
        # don't reveal which accounts exist.
        message = "If that account exists, the admin has been notified and will be in touch with new login details."
        if user:
            flags_store.create_flag(username, "Password Reset")
    return render_template("forgot_password.html", message=message)


@app.route("/report-issue", methods=["GET", "POST"])
@login_required
def report_issue():
    message = None
    if request.method == "POST":
        description = (request.form.get("description") or "").strip()
        if description:
            flags_store.create_flag(session.get("username"), "Issue Report", description)
            message = "Thanks — your report has been sent to the admin."
        else:
            message = "Please describe the issue before submitting."
    return render_template("report_issue.html", message=message)


@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        is_admin=(session.get("role") == "Admin"),
        username=session.get("username"),
        role=session.get("role"),
    )


@app.route("/api/profile")
@login_required
def profile_data():
    user_id = session.get("user_id")
    email = ""
    if user_id:
        user = auth_store.find_user(session.get("username"))
        if user:
            email = user.get("email", "")
    return jsonify({
        "username": session.get("username"),
        "role": session.get("role"),
        "email": email,
        "is_bootstrap": user_id is None,
    })


@app.route("/api/profile/change-password", methods=["POST"])
@login_required
def profile_change_password():
    if not session.get("user_id"):
        return jsonify({"error": "This account is managed via server configuration and can't be changed here."}), 400
    data = request.json or {}
    current = data.get("current_password", "")
    new_password = data.get("new_password", "")
    confirm = data.get("confirm", "")

    user = auth_store.find_user(session["username"])
    if not user or not auth_store.verify_password(current, user["password_hash"]):
        return jsonify({"error": "Current password is incorrect."}), 400
    if len(new_password) < 8:
        return jsonify({"error": "New password must be at least 8 characters."}), 400
    if new_password != confirm:
        return jsonify({"error": "Passwords don't match."}), 400

    auth_store.set_password(user["id"], new_password, must_reset=False)
    return jsonify({"status": "changed"})


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
        "updated": datetime.now(timezone(timedelta(hours=2))).strftime("%-d %b %Y %H:%M") + " UTC+2",
    })


@app.route("/api/settings/brand-brief", methods=["GET", "POST"])
@login_required
def brand_brief():
    if request.method == "POST":
        data = request.json or {}
        set_setting("brand_brief", data.get("text", ""))
        return jsonify({"status": "saved"})
    return jsonify({"text": get_setting("brand_brief")})


DEFAULT_OUTREACH_TEMPLATE = (
    "Hi {name},\n\n"
    "I'm reaching out from Axolt — we make a daily brain-nutrition drink and think your "
    "content on {niche} would be a great fit for our creator program.\n\n"
    "We'd love to send you our product to try, no strings attached. If you're interested, "
    "just reply to this email and we'll get you set up.\n\n"
    "Best,\nThe Axolt Team"
)


@app.route("/api/settings/outreach-template", methods=["GET", "POST"])
@login_required
def outreach_template():
    if request.method == "POST":
        data = request.json or {}
        set_setting("outreach_template", data.get("text", ""))
        return jsonify({"status": "saved"})
    return jsonify({"text": get_setting("outreach_template", DEFAULT_OUTREACH_TEMPLATE)})


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


@app.route("/api/vetting/summary")
@login_required
def vetting_summary():
    pages = query_db(VETTING_QUEUE_DB)
    counts = {"Vetted": 0, "Review": 0, "Auto-skipped": 0}
    for p in pages:
        outcome = get_prop(p, "Outcome")
        if outcome in counts:
            counts[outcome] += 1
    return jsonify(counts)


@app.route("/api/analytics/summary")
@login_required
def analytics_summary():
    scraped = get_counter("sv_total_scraped")
    vetted = get_counter("sv_total_vetted")
    review = get_counter("sv_total_review")
    auto_skipped = get_counter("sv_total_auto_skipped")
    approved = get_counter("sv_total_approved")
    review_skipped = get_counter("sv_total_review_skipped")
    passed_vetting = vetted + review

    pass_rate = round(passed_vetting / scraped * 100, 1) if scraped else 0
    approval_rate = round(approved / passed_vetting * 100, 1) if passed_vetting else 0

    return jsonify({
        "total_scraped": scraped,
        "total_passed_vetting": passed_vetting,
        "total_auto_skipped": auto_skipped,
        "total_approved": approved,
        "total_review_skipped": review_skipped,
        "pass_rate": pass_rate,
        "approval_rate": approval_rate,
    })


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
    description_parts = [p for p in [creator.get("niche"), creator.get("bio")] if p]
    if description_parts:
        props["Description"] = {"rich_text": [{"text": {"content": " — ".join(description_parts)[:2000]}}]}

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
    increment_counter("sv_total_approved")
    return jsonify({"status": "approved"})


@app.route("/api/vetting/skip", methods=["POST"])
@login_required
def vetting_skip():
    data = request.json or {}
    page_id = data.get("id")
    if not page_id:
        return jsonify({"error": "Missing id"}), 400

    r = requests.get("https://api.notion.com/v1/pages/" + page_id, headers=NOTION_HEADERS)
    outcome = get_prop(r.json(), "Outcome")
    if outcome == "Review":
        increment_counter("sv_total_review_skipped")

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


@app.route("/api/admin/users", methods=["GET", "POST"])
@login_required
@admin_required
def admin_users():
    if request.method == "POST":
        data = request.json or {}
        username = (data.get("username") or "").strip()
        role = data.get("role") or "User"
        email = (data.get("email") or "").strip()
        if not username:
            return jsonify({"error": "Username is required"}), 400
        if role not in ("Admin", "User"):
            return jsonify({"error": "Invalid role"}), 400
        if auth_store.find_user(username):
            return jsonify({"error": "Username already exists"}), 400
        password = auth_store.create_user(username, role=role, email=email)
        return jsonify({"username": username, "password": password})

    users = auth_store.list_users()
    for u in users:
        u.pop("password_hash", None)
    return jsonify({"users": users})


@app.route("/api/admin/users/<user_id>", methods=["DELETE"])
@login_required
@admin_required
def admin_delete_user(user_id):
    auth_store.delete_user(user_id)
    return jsonify({"status": "deleted"})


@app.route("/api/admin/flags")
@login_required
@admin_required
def admin_flags():
    return jsonify({"flags": flags_store.list_open_flags()})


@app.route("/api/admin/flags/<flag_id>/resolve", methods=["POST"])
@login_required
@admin_required
def admin_resolve_flag(flag_id):
    flags_store.resolve_flag(flag_id)
    return jsonify({"status": "resolved"})


@app.route("/api/admin/flags/<flag_id>/generate-password", methods=["POST"])
@login_required
@admin_required
def admin_flag_generate_password(flag_id):
    data = request.json or {}
    username = (data.get("username") or "").strip()
    user = auth_store.find_user(username)
    if not user:
        return jsonify({"error": "No account found for username: " + username}), 400
    new_password = auth_store.generate_password()
    auth_store.set_password(user["id"], new_password, must_reset=True)
    flags_store.resolve_flag(flag_id)
    return jsonify({"username": username, "password": new_password})


def outreach_page_to_dict(page):
    return {
        "id": page["id"],
        "name": get_prop(page, "Name") or "",
        "handle": get_prop(page, "Handle") or "",
        "niche": ", ".join(get_prop(page, "Category") or []),
        "email": get_prop(page, "Email") or "",
        "bio": get_prop(page, "Description") or "",
        "draft": get_prop(page, "Outreach Draft") or "",
    }


@app.route("/api/outreach/list")
@login_required
def outreach_list():
    pages = query_db(INFLUENCER_DB, filter_body={
        "and": [
            {"property": "Stage", "select": {"equals": "Lead"}},
            {"property": "Email", "email": {"is_not_empty": True}},
        ]
    })
    return jsonify({"creators": [outreach_page_to_dict(p) for p in pages]})


@app.route("/api/outreach/draft/<page_id>", methods=["POST"])
@login_required
def outreach_draft(page_id):
    r = requests.get("https://api.notion.com/v1/pages/" + page_id, headers=NOTION_HEADERS)
    page = r.json()
    creator = outreach_page_to_dict(page)

    template = get_setting("outreach_template", DEFAULT_OUTREACH_TEMPLATE)
    try:
        draft = vetting.generate_outreach_email(template, creator)
    except Exception as e:
        return jsonify({"error": "Could not generate draft: " + str(e)}), 500

    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {"Outreach Draft": {"rich_text": [{"text": {"content": draft[:2000]}}]}}},
    )
    return jsonify({"draft": draft})


@app.route("/api/outreach/send", methods=["POST"])
@login_required
def outreach_send():
    data = request.json or {}
    page_id = data.get("id")
    body = (data.get("body") or "").strip()
    if not page_id or not body:
        return jsonify({"error": "Missing id or body"}), 400

    r = requests.get("https://api.notion.com/v1/pages/" + page_id, headers=NOTION_HEADERS)
    page = r.json()
    creator = outreach_page_to_dict(page)
    if not creator["email"]:
        return jsonify({"error": "This creator has no email on file"}), 400

    try:
        email_sender.send_email(creator["email"], "A quick note from Axolt", body)
    except Exception as e:
        return jsonify({"error": "Failed to send: " + str(e)}), 500

    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {
            "Stage": {"select": {"name": "Contacted"}},
            "Outreach Sent": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        }},
    )
    return jsonify({"status": "sent"})


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
        "follower_buckets": data.get("follower_buckets") or [],
        "interaction_rate": data.get("interaction_rate"),
    }
    start_scrape_thread(keywords, limit, cookies_json, country, filters)
    return jsonify({"status": "started"})


@app.route("/api/scrape/status")
@login_required
def scrape_status():
    from scraper import job
    return jsonify(job)


@app.route("/api/scrape/reset", methods=["POST"])
@login_required
def scrape_reset():
    import copy
    from scraper import job, save_job, DEFAULT_JOB
    if job.get("running"):
        return jsonify({"error": "Cannot reset while a scrape is running"}), 400
    job.clear()
    job.update(copy.deepcopy(DEFAULT_JOB))
    save_job()
    return jsonify({"status": "reset"})


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
