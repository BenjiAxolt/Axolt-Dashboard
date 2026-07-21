import json
import os
import re
import requests
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from notion_settings import get_setting, set_setting, get_counter, increment_counter
import auth_store
import templates_store
import calendar_store
import flags_store
import email_sender
import vetting

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-prod")

# Per-process, in-memory rate limiting — not shared across gunicorn workers,
# so it's a soft limit rather than a hard guarantee, but it's enough to stop
# casual/scripted spam of unauthenticated endpoints like /forgot-password.
_rate_limit_hits = defaultdict(list)


def is_rate_limited(key_prefix, max_requests, window_seconds):
    """Returns True (and does NOT count this call) if the caller's IP has
    already hit max_requests within window_seconds; otherwise records this
    call and returns False."""
    key = key_prefix + ":" + (request.remote_addr or "unknown")
    now = time.time()
    hits = _rate_limit_hits[key]
    hits[:] = [t for t in hits if now - t < window_seconds]
    if len(hits) >= max_requests:
        return True
    hits.append(now)
    return False

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


GENERIC_FORGOT_PASSWORD_MESSAGE = (
    "If that account exists, the admin has been notified and will be in touch with new login details."
)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None
    if request.method == "POST":
        # Only submissions count toward the limit — just viewing the page
        # (GET) is unrestricted. A rate-limited attacker gets the exact same
        # generic message as everyone else, so there's no signal that
        # limiting kicked in — same principle as not revealing which
        # usernames exist.
        if is_rate_limited("forgot-password", max_requests=5, window_seconds=900):
            return render_template("forgot_password.html", message=GENERIC_FORGOT_PASSWORD_MESSAGE)
        username = (request.form.get("username") or "").strip()
        user = auth_store.find_user(username) if username else None
        message = GENERIC_FORGOT_PASSWORD_MESSAGE
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

    TEST_OFFSET = 53  # TEMPORARY — remove this line and the two uses below to revert
    total_inf    = len(inf_pages) + TEST_OFFSET
    contacted    = sum(1 for p in inf_pages if get_prop(p, "Stage") in CONTACTED) + TEST_OFFSET
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
            try:
                content_links = json.loads(get_prop(p, "Content Links") or "[]")
            except (ValueError, TypeError):
                content_links = []
            seeded.append({
                "id": (get_prop(p, "Name") or "unknown").lower().replace(" ", "_").replace("'", ""),
                "page_id": p["id"],
                "name": get_prop(p, "Name") or "Unknown",
                "handle": get_prop(p, "Handle") or "",
                "followers": fmt_followers(get_prop(p, "Followers")),
                "category": ", ".join(get_prop(p, "Category") or []),
                "delivered": fmt_date(get_prop(p, "Product Delivered")),
                "delivered_raw": get_prop(p, "Product Delivered"),
                "content_links": content_links,
                "affiliate": bool(get_prop(p, "Affiliate Program")),
                "affiliate_date": fmt_date(get_prop(p, "Affiliate Joined Date")),
                "affiliate_date_raw": get_prop(p, "Affiliate Joined Date"),
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


DEFAULT_OUTREACH_TEMPLATE = (
    "<p>Hi {name},</p>"
    "<p>I'm reaching out from Axolt — we make a daily brain-nutrition drink and think your "
    "content on {niche} would be a great fit for our creator program.</p>"
    "<p>We'd love to send you our product to try, no strings attached. If you're interested, "
    "just reply to this email and we'll get you set up.</p>"
    "<p>Best,<br>The Axolt Team</p>"
)


def ensure_default_templates():
    """One-time migration: seed Brand Brief / Outreach Email Template as real
    rows in the new Dashboard Templates database if they don't exist yet,
    carrying over any content from the old plain-text settings."""
    if not templates_store.get_template_by_key("brand_brief"):
        old_text = get_setting("brand_brief", "")
        content = "<p>" + old_text.replace("\n", "</p><p>") + "</p>" if old_text else ""
        templates_store.create_template("Brand Brief", content, key="brand_brief")
    if not templates_store.get_template_by_key("outreach_template"):
        old_text = get_setting("outreach_template", "")
        if old_text:
            content = "<p>" + old_text.replace("\n", "</p><p>") + "</p>"
        else:
            content = DEFAULT_OUTREACH_TEMPLATE
        templates_store.create_template("Outreach Email Template", content, key="outreach_template")


@app.route("/api/templates")
@login_required
def templates_list():
    ensure_default_templates()
    templates = templates_store.list_templates()
    return jsonify({"templates": [{"id": t["id"], "name": t["name"]} for t in templates]})


@app.route("/api/templates/<template_id>")
@login_required
def templates_get(template_id):
    t = templates_store.get_template(template_id)
    if not t:
        return jsonify({"error": "Not found"}), 404
    return jsonify(t)


@app.route("/api/templates", methods=["POST"])
@login_required
def templates_create():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    new_id = templates_store.create_template(name, "")
    return jsonify({"id": new_id, "name": name})


@app.route("/api/templates/<template_id>", methods=["PUT"])
@login_required
def templates_update(template_id):
    data = request.json or {}
    templates_store.update_template(template_id, name=data.get("name"), content=data.get("content"))
    return jsonify({"status": "saved"})


@app.route("/api/templates/<template_id>", methods=["DELETE"])
@login_required
def templates_delete(template_id):
    templates_store.delete_template(template_id)
    return jsonify({"status": "deleted"})


@app.route("/api/calendar/events")
@login_required
def calendar_events():
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        return jsonify({"error": "start and end query params required (YYYY-MM-DD)"}), 400
    return jsonify({"events": calendar_store.list_events(start, end)})


@app.route("/api/calendar/events", methods=["POST"])
@login_required
def calendar_create_event():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    date_iso = (data.get("date") or "").strip()
    description = data.get("description") or ""
    if not name or not date_iso:
        return jsonify({"error": "name and date are required"}), 400
    event_id = calendar_store.create_event(name, date_iso, description)
    return jsonify({"id": event_id})


@app.route("/api/calendar/events/<event_id>", methods=["DELETE"])
@login_required
def calendar_delete_event(event_id):
    calendar_store.delete_event(event_id)
    return jsonify({"status": "deleted"})


@app.route("/api/calendar/events/<event_id>/mark-sent", methods=["POST"])
@login_required
def calendar_mark_sent(event_id):
    """Called when a survey (or outreach nudge) event is actually sent out,
    not just due — this is what schedules its FU1/FU2/FU3 reminders, so the
    calendar only fills up with follow-ups for things that really happened."""
    event = calendar_store.get_event(event_id)
    if not event:
        return jsonify({"error": "Event not found"}), 404

    match = re.match(r"^(.+?) — (.+)$", event["name"])
    if not match:
        return jsonify({"error": "Could not parse this event's label/name"}), 400
    label, name = match.group(1), match.group(2)

    today = datetime.now(timezone.utc).date()
    schedule_followups(today, label, name, [3, 3, 3])
    return jsonify({"status": "sent", "label": label, "name": name})


@app.route("/api/influencers/awaiting-delivery")
@login_required
def influencers_awaiting_delivery():
    pages = query_db(INFLUENCER_DB, filter_body={"property": "Stage", "select": {"equals": "Intake Survey Filled"}})
    return jsonify({"creators": [
        {"id": p["id"], "name": get_prop(p, "Name") or get_prop(p, "Handle") or "Unknown"} for p in pages
    ]})


@app.route("/api/influencers/<page_id>/mark-delivered", methods=["POST"])
@login_required
def influencers_mark_delivered(page_id):
    r = requests.get("https://api.notion.com/v1/pages/" + page_id, headers=NOTION_HEADERS)
    page = r.json()
    name = get_prop(page, "Name") or get_prop(page, "Handle") or "Unknown"

    delivered_date = datetime.now(timezone.utc).date()
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {
            "Stage": {"select": {"name": "Product Delivered"}},
            "Product Delivered": {"date": {"start": delivered_date.isoformat()}},
        }},
    )
    schedule_survey_chain(delivered_date, 14, name)
    schedule_survey_chain(delivered_date, 30, name)
    return jsonify({"status": "delivered"})


@app.route("/api/influencers/<page_id>/delivered-date", methods=["POST"])
@login_required
def influencers_set_delivered_date(page_id):
    """Set (or backdate) the Product Delivered date on a creator already at
    that stage — for creators marked delivered outside the normal Mark
    Delivered button (e.g. edited directly in Notion), which never got a
    date stamped or their 14/30-day survey chain scheduled."""
    data = request.json or {}
    date_str = (data.get("date") or "").strip()
    if not date_str:
        return jsonify({"error": "Date is required"}), 400
    try:
        delivered_date = datetime.fromisoformat(date_str).date()
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400

    r = requests.get("https://api.notion.com/v1/pages/" + page_id, headers=NOTION_HEADERS)
    page = r.json()
    name = get_prop(page, "Name") or get_prop(page, "Handle") or "Unknown"

    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {
            "Product Delivered": {"date": {"start": delivered_date.isoformat()}},
        }},
    )
    schedule_survey_chain(delivered_date, 14, name)
    schedule_survey_chain(delivered_date, 30, name)
    return jsonify({"status": "updated", "date": delivered_date.isoformat()})


@app.route("/api/influencers/<page_id>/affiliate", methods=["POST"])
@login_required
def influencers_set_affiliate(page_id):
    """Mark whether a creator has joined the affiliate program, independent
    of the seeding pipeline — a creator can be seeded without being an
    affiliate, or (once the business allows it) vice versa."""
    data = request.json or {}
    joined = bool(data.get("joined"))
    date_str = (data.get("date") or "").strip()

    props = {"Affiliate Program": {"checkbox": joined}}
    if joined:
        if not date_str:
            return jsonify({"error": "Date is required"}), 400
        try:
            datetime.fromisoformat(date_str)
        except ValueError:
            return jsonify({"error": "Invalid date"}), 400
        props["Affiliate Joined Date"] = {"date": {"start": date_str}}
    else:
        props["Affiliate Joined Date"] = {"date": None}

    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": props},
    )
    return jsonify({"status": "updated", "joined": joined, "date": date_str})


STAGE_OPTIONS = [
    "Lead", "Contacted", "Replied", "Intake Survey Filled", "Product Delivered",
    "14-Day Survey Sent", "14-Day Survey Filled", "30-Day Survey Sent", "30-Day Survey Filled",
    "Unresponsive", "Declined", "Not Now", "Duplicate",
]


@app.route("/api/influencers/<page_id>/stage", methods=["POST"])
@login_required
def influencers_set_stage(page_id):
    """Manually correct a creator's Stage — e.g. Uta filled in her 14-Day
    Survey but there was no way to reflect that outside the automated
    outreach/survey flows. If the chosen stage has a matching date property
    (several stage names exactly match a date field, e.g. "14-Day Survey
    Filled"), stamp it with today if it isn't already set."""
    data = request.json or {}
    stage = (data.get("stage") or "").strip()
    if stage not in STAGE_OPTIONS:
        return jsonify({"error": "Invalid stage"}), 400

    r = requests.get("https://api.notion.com/v1/pages/" + page_id, headers=NOTION_HEADERS)
    page = r.json()
    props = {"Stage": {"select": {"name": stage}}}
    prop_schema = page.get("properties", {}).get(stage, {})
    if prop_schema.get("type") == "date" and not get_prop(page, stage):
        props[stage] = {"date": {"start": datetime.now(timezone.utc).date().isoformat()}}

    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": props},
    )
    return jsonify({"status": "updated", "stage": stage})


@app.route("/api/reports/invoice")
@login_required
def invoice_report():
    """Billing periods run the 6th through the 5th (invoices go out on the
    6th), so this counts what happened in the period ending on a given 5th
    — defaulting to the current in-progress period."""
    start_str = request.args.get("start")
    end_str = request.args.get("end")
    if start_str and end_str:
        start = datetime.fromisoformat(start_str).date()
        end = datetime.fromisoformat(end_str).date()
    else:
        today = datetime.now(timezone.utc).date()
        if today.day >= 6:
            start = today.replace(day=6)
            next_month = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
            end = next_month.replace(day=5)
        else:
            end = today.replace(day=5)
            start = (end.replace(day=1) - timedelta(days=1)).replace(day=6)

    pages = query_db(INFLUENCER_DB)
    delivered, affiliate, content = [], [], []
    for p in pages:
        name = get_prop(p, "Name") or get_prop(p, "Handle") or "Unknown"

        d = get_prop(p, "Product Delivered")
        if d and start.isoformat() <= d[:10] <= end.isoformat():
            delivered.append({"name": name, "date": d[:10]})

        a = get_prop(p, "Affiliate Joined Date")
        if a and start.isoformat() <= a[:10] <= end.isoformat():
            affiliate.append({"name": name, "date": a[:10]})

        try:
            links = json.loads(get_prop(p, "Content Links") or "[]")
        except (ValueError, TypeError):
            links = []
        for item in links:
            ld = (item.get("date") or "")[:10]
            if ld and start.isoformat() <= ld <= end.isoformat():
                content.append({"name": name, "url": item.get("url"), "date": ld})

    delivered.sort(key=lambda x: x["date"])
    affiliate.sort(key=lambda x: x["date"])
    content.sort(key=lambda x: x["date"])

    return jsonify({
        "start": start.isoformat(),
        "end": end.isoformat(),
        "delivered": delivered,
        "affiliate": affiliate,
        "content": content,
    })


@app.route("/api/influencers/<page_id>/content-links", methods=["POST"])
@login_required
def influencers_set_content_links(page_id):
    """Store the full list of content links for a creator, each with its own
    posted date — a creator who posts multiple times needs a date per post,
    not one date for the whole creator. Replaces the whole list each save
    (simpler than a per-item add/delete API, and the list is always small)."""
    data = request.json or {}
    links = data.get("links")
    if not isinstance(links, list):
        return jsonify({"error": "links must be a list"}), 400

    cleaned = []
    for item in links:
        url = (item.get("url") or "").strip()
        date_str = (item.get("date") or "").strip()
        if not url:
            continue
        if not re.match(r"^https?://", url, re.IGNORECASE):
            return jsonify({"error": "Only http(s) links are allowed: " + url}), 400
        if date_str:
            try:
                datetime.fromisoformat(date_str)
            except ValueError:
                return jsonify({"error": "Invalid date: " + date_str}), 400
        cleaned.append({"url": url, "date": date_str})

    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {
            "Content Links": {"rich_text": [{"text": {"content": json.dumps(cleaned)[:2000]}}]},
        }},
    )
    return jsonify({"status": "updated", "links": cleaned})


def vetting_page_to_dict(page):
    try:
        thumbnails = json.loads(get_prop(page, "Post Thumbnails") or "[]")
    except (ValueError, TypeError):
        thumbnails = []
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
        "thumbnails": thumbnails,
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


@app.route("/api/vetting/rename", methods=["POST"])
@login_required
def vetting_rename():
    """The scraper's name extraction occasionally grabs one of Meta's
    highlight badges (e.g. "Strong hooks") instead of the creator's real
    display name — this lets a reviewer correct it by hand as they go."""
    data = request.json or {}
    page_id = data.get("id")
    name = (data.get("name") or "").strip()
    if not page_id:
        return jsonify({"error": "Missing id"}), 400
    if not name:
        return jsonify({"error": "Name cannot be empty"}), 400
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {
            "Name": {"title": [{"text": {"content": name[:2000]}}]},
        }},
    )
    return jsonify({"status": "renamed", "name": name})


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

    props = {
        "Outcome": {"select": {"name": "Vetted"}},
        "Flag Note": {"rich_text": [{"text": {"content": "No email on file — DM only"}}] if not email else []},
    }
    if email:
        props["Email"] = {"email": email}
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": props},
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


def add_business_days(start_date, n):
    d = start_date
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def schedule_followups(start_date, label_prefix, name, offsets):
    """offsets: business-day gaps, each measured from the previous date in the chain."""
    d = start_date
    for i, offset in enumerate(offsets, start=1):
        d = add_business_days(d, offset)
        calendar_store.create_event(label_prefix + " FU" + str(i) + " — " + name, d.isoformat() + "T09:00:00")


def schedule_outreach_chain(name, start_date=None):
    today = start_date or datetime.now(timezone.utc).date()
    nudge_date = add_business_days(today, 3)
    calendar_store.create_event("Instagram Nudge — " + name, nudge_date.isoformat() + "T09:00:00")
    schedule_followups(nudge_date, "Outreach", name, [2, 3, 3])


def schedule_survey_chain(delivered_date, days, name):
    """Only schedules the initial survey-send event. The FU1/FU2/FU3
    reminders aren't scheduled here — they'd sit on the calendar for two
    weeks before they're relevant. They get scheduled later, the day the
    survey is actually sent, via /api/calendar/events/<id>/mark-sent."""
    survey_date = delivered_date + timedelta(days=days)
    label = str(days) + "-Day Survey"
    calendar_store.create_event(label + " — " + name, survey_date.isoformat() + "T09:00:00")


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
    # No email requirement here — a Lead with no email on file can't get a
    # Send button, but still needs to show up so a DM'd-outside-the-app
    # outreach can be logged via "I already reached out myself" and get its
    # follow-up chain scheduled on the calendar.
    pages = query_db(INFLUENCER_DB, filter_body={
        "property": "Stage", "select": {"equals": "Lead"}
    })
    return jsonify({"creators": [outreach_page_to_dict(p) for p in pages]})


@app.route("/api/outreach/draft/<page_id>", methods=["POST"])
@login_required
def outreach_draft(page_id):
    r = requests.get("https://api.notion.com/v1/pages/" + page_id, headers=NOTION_HEADERS)
    page = r.json()
    creator = outreach_page_to_dict(page)

    ensure_default_templates()
    template_row = templates_store.get_template_by_key("outreach_template")
    template = templates_store.html_to_text(template_row["content"]) if template_row else DEFAULT_OUTREACH_TEMPLATE
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
    schedule_outreach_chain(creator["name"] or creator["handle"])
    return jsonify({"status": "sent"})


@app.route("/api/outreach/mark-contacted", methods=["POST"])
@login_required
def outreach_mark_contacted():
    """For outreach done outside the dashboard (DM, a personal email, etc.)
    — records the same Stage/date change and follow-up chain that a normal
    Send would, backdated to whenever the contact actually happened rather
    than today, so the follow-up schedule lines up with reality."""
    data = request.json or {}
    page_id = data.get("id")
    date_str = (data.get("date") or "").strip()
    if not page_id:
        return jsonify({"error": "Missing id"}), 400

    if date_str:
        try:
            contacted_date = datetime.fromisoformat(date_str).date()
        except ValueError:
            return jsonify({"error": "Invalid date: " + date_str}), 400
    else:
        contacted_date = datetime.now(timezone.utc).date()

    r = requests.get("https://api.notion.com/v1/pages/" + page_id, headers=NOTION_HEADERS)
    page = r.json()
    creator = outreach_page_to_dict(page)

    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {
            "Stage": {"select": {"name": "Contacted"}},
            "Outreach Sent": {"date": {"start": contacted_date.isoformat()}},
        }},
    )
    schedule_outreach_chain(creator["name"] or creator["handle"], start_date=contacted_date)
    return jsonify({"status": "marked"})


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


@app.route("/api/scrape/stop", methods=["POST"])
@login_required
def scrape_stop():
    from scraper import job, stop_scrape
    if not job.get("running"):
        return jsonify({"error": "No scrape is currently running"}), 400
    stopped = stop_scrape()
    return jsonify({"status": "stopping" if stopped else "nothing to stop"})


@app.route("/api/manual-add", methods=["POST"])
@login_required
def manual_add_creator():
    from scraper import get_existing_handles, load_dedup, save_dedup

    data = request.json or {}
    handle_key = (data.get("handle") or "").strip().lower().lstrip("@")
    if not handle_key:
        return jsonify({"error": "Handle is required"}), 400

    existing = get_existing_handles() | load_dedup()
    if handle_key in existing:
        return jsonify({"error": "@" + handle_key + " is already in the Influencer database or dedup list"}), 400

    name = (data.get("name") or "").strip()
    social_url = (data.get("social_url") or "").strip()
    email = (data.get("email") or "").strip()
    categories = data.get("categories") or []
    notes = (data.get("notes") or "").strip()

    props = {
        "Name": {"title": [{"text": {"content": name or ("@" + handle_key)}}]},
        "Stage": {"select": {"name": "Lead"}},
        "Handle": {"rich_text": [{"text": {"content": "@" + handle_key}}]},
    }
    try:
        followers = int(data.get("followers"))
        props["Followers"] = {"number": followers}
    except (TypeError, ValueError):
        pass
    if email:
        props["Email"] = {"email": email}
    if social_url:
        props["Social Media"] = {"url": social_url}
    if categories:
        props["Category"] = {"multi_select": [{"name": c} for c in categories[:5]]}
    if notes:
        props["Description"] = {"rich_text": [{"text": {"content": notes[:2000]}}]}

    requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": INFLUENCER_DB}, "properties": props},
    )

    seen = load_dedup()
    seen.add(handle_key)
    save_dedup(seen)

    return jsonify({"status": "added", "handle": "@" + handle_key})


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
