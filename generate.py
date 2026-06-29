import os
import json
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
INFLUENCER_DB = "f07a187424e64bc7b1b992ceced311c5"
CLINIC_DB = "cb01c955a4664a1eb0d66c1f835f1243"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

def query_db(db_id):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(f"https://api.notion.com/v1/databases/{db_id}/query", headers=HEADERS, json=body)
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages

def prop(page, name, kind="text"):
    props = page.get("properties", {})
    p = props.get(name, {})
    if not p:
        return ""
    t = p.get("type", "")
    if t == "title":
        items = p.get("title", [])
        return "".join(i.get("plain_text", "") for i in items).strip()
    if t == "rich_text":
        items = p.get("rich_text", [])
        return "".join(i.get("plain_text", "") for i in items).strip()
    if t == "select":
        s = p.get("select") or {}
        return s.get("name", "")
    if t == "multi_select":
        return [s.get("name", "") for s in p.get("multi_select", [])]
    if t == "date":
        d = p.get("date") or {}
        return d.get("start", "")
    if t == "checkbox":
        return p.get("checkbox", False)
    if t == "url":
        return p.get("url", "")
    return ""

def get_name(page):
    props = page.get("properties", {})
    for key in ["Name", "Influencer Name", "Clinic Name", "Title"]:
        if key in props:
            v = prop(page, key, "title")
            if v:
                return v
    for key, p in props.items():
        if p.get("type") == "title":
            items = p.get("title", [])
            v = "".join(i.get("plain_text", "") for i in items).strip()
            if v:
                return v
    return "Unknown"

def get_status(page):
    props = page.get("properties", {})
    for key in ["Status", "Outreach Status", "Stage", "Pipeline Stage"]:
        if key in props:
            v = prop(page, key, "select")
            if v:
                return v
    return ""

def get_channel(page):
    props = page.get("properties", {})
    for key in ["Channel", "Outreach Channel", "Contact Method"]:
        if key in props:
            v = prop(page, key, "select")
            if v:
                return v
    return ""

def fmt_date(ds):
    if not ds:
        return ""
    try:
        d = datetime.fromisoformat(ds.replace("Z", "+00:00"))
        return d.strftime("%-d %b")
    except:
        return ds[:10]

def build_dashboard(inf_pages, clinic_pages):
    today = datetime.now(timezone.utc).strftime("%-d %B %Y")
    today_short = datetime.now(timezone.utc).strftime("%a %-d %b %Y")

    # ── Influencer stats ──
    email_pages = [p for p in inf_pages if "dm" not in get_channel(p).lower()]
    dm_pages = [p for p in inf_pages if "dm" in get_channel(p).lower() or "instagram" in get_channel(p).lower()]

    # Fallback: if channel not set, use status keywords
    if not dm_pages and not email_pages:
        dm_pages = [p for p in inf_pages if "dm" in get_status(p).lower()]
        email_pages = [p for p in inf_pages if p not in dm_pages]

    total_inf = len(inf_pages)
    email_contacted = sum(1 for p in email_pages if get_status(p).lower() not in ["", "lead", "not started"])
    email_intake = sum(1 for p in email_pages if get_status(p).lower() in ["intake filled", "intake complete", "converted", "product delivered", "active", "survey sent"])
    email_delivered = sum(1 for p in email_pages if get_status(p).lower() in ["product delivered", "active", "survey sent", "delivered"])
    email_declined = sum(1 for p in email_pages if "declined" in get_status(p).lower())

    dm_sent = len(dm_pages)
    dm_responded = sum(1 for p in dm_pages if get_status(p).lower() not in ["", "dm sent", "pending", "no response", "not responded"])
    dm_positive = sum(1 for p in dm_pages if get_status(p).lower() in ["positive", "interested", "intake pending", "intake filled", "converted"])
    dm_converted = sum(1 for p in dm_pages if get_status(p).lower() in ["converted", "intake filled", "intake complete"])
    dm_declined = sum(1 for p in dm_pages if "declined" in get_status(p).lower())
    dm_no_resp = sum(1 for p in dm_pages if get_status(p).lower() in ["no response", "pending", "dm sent", "not responded"])

    # ── Clinic stats ──
    total_clinics = len(clinic_pages)
    clinic_replied = sum(1 for p in clinic_pages if get_status(p).lower() not in ["", "outreach sent", "contacted", "lead"])
    clinic_meeting = sum(1 for p in clinic_pages if "meeting" in get_status(p).lower() or "booked" in get_status(p).lower())
    clinic_declined = sum(1 for p in clinic_pages if "declined" in get_status(p).lower())

    # ── DM tracker rows ──
    def status_badge(s):
        sl = s.lower()
        if "converted" in sl or "intake filled" in sl or "intake complete" in sl:
            return '<span class="tag t-pink">✅ Converted</span>'
        if "positive" in sl or "interested" in sl or "intake pending" in sl:
            return '<span class="tag t-green">🟢 Positive</span>'
        if "not now" in sl or "conflict" in sl:
            return '<span class="tag t-amber">🟡 Not Now</span>'
        if "declined" in sl:
            return '<span class="tag t-red">🔴 Declined</span>'
        return '<span class="tag t-grey">⬜ Pending</span>'

    def border_color(s):
        sl = s.lower()
        if "converted" in sl or "intake filled" in sl:
            return "var(--pink)"
        if "positive" in sl or "interested" in sl or "intake pending" in sl:
            return "var(--green)"
        if "not now" in sl:
            return "var(--amber)"
        if "declined" in sl:
            return "var(--red)"
        return "var(--border)"

    dm_rows_html = ""
    for p in dm_pages:
        name = get_name(p)
        status = get_status(p)
        handle_prop = None
        for key in ["Handle", "Instagram Handle", "Instagram", "@handle"]:
            v = prop(p, key, "text")
            if v:
                handle_prop = v
                break
        handle = handle_prop or ""
        note = ""
        for key in ["Notes", "Note", "Comments"]:
            v = prop(p, key, "text")
            if v:
                note = v[:80]
                break
        dm_rows_html += f'''
    <div class="dm-row" style="border-left:3px solid {border_color(status)}">
      <div class="dm-name">{name}</div>
      <div class="dm-handle">{handle}</div>
      <div class="dm-badge">{status_badge(status)}</div>
      <div class="dm-note">{note}</div>
    </div>'''

    # ── Email funnel bars ──
    def bar(label, count, total, color, opacity=""):
        pct = max(4, round(count / max(total, 1) * 100))
        op = f"opacity:{opacity};" if opacity else ""
        return f'''
    <div class="f-row">
      <div class="f-lbl">{label}</div>
      <div class="f-track"><div class="f-bar" style="width:{pct}%;background:{color};{op}min-width:32px">{count}</div></div>
      <div class="f-n" style="color:{color if count else 'var(--dim)'};">{count}</div>
    </div>'''

    email_funnel = (
        bar("Contacted", email_contacted, email_contacted, "var(--purple)") +
        bar("Intake Filled", email_intake, email_contacted, "var(--amber)") +
        bar("Product Delivered", email_delivered, email_contacted, "var(--green)") +
        bar("14-Day Survey", 0, email_contacted, "var(--dark-purple)", "0.5") +
        bar("30-Day Survey", 0, email_contacted, "var(--dark-purple)", "0.3") +
        bar("Declined", email_declined, email_contacted, "var(--red)")
    )

    clinic_funnel = (
        bar("Contacted", total_clinics, total_clinics, "var(--teal)") +
        bar("Replied", clinic_replied, total_clinics, "var(--teal)", "0.55") +
        bar("Meeting Booked", clinic_meeting, total_clinics, "var(--amber)") +
        bar("Partnership Agreed", 0, total_clinics, "var(--green)", "0.5") +
        bar("Declined", clinic_declined, total_clinics, "var(--red)")
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Axolt Seeding Dashboard</title>
<style>
  :root {{
    --purple:#7F77DD;--teal:#1D9E75;--amber:#BA7517;--green:#639922;
    --dark-purple:#534AB7;--pink:#C026A8;--red:#E24B4A;--grey:#888780;
    --bg:#0f0f12;--surface:#1a1a20;--surface2:#22222a;--border:#2a2a34;
    --text:#e8e8f0;--muted:#7a7a88;--dim:#44444f;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;line-height:1.5}}
  .tabs{{display:flex;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100}}
  .tab{{padding:13px 24px;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px}}
  .tab.active{{color:var(--purple);border-bottom-color:var(--purple)}}
  .tab:nth-child(2).active{{color:var(--teal);border-bottom-color:var(--teal)}}
  .page{{display:none;padding:24px;max-width:960px;margin:0 auto}}
  .page.active{{display:block}}
  .ch-header{{display:flex;align-items:center;gap:10px;padding:10px 0 14px;border-bottom:1px solid var(--border);margin-bottom:16px}}
  .ch-title{{font-size:14px;font-weight:700}}
  .ch-pill{{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;border-radius:20px;padding:3px 10px}}
  .pill-email{{background:rgba(127,119,221,.15);color:var(--purple)}}
  .pill-dm{{background:rgba(192,38,168,.15);color:var(--pink)}}
  .pill-clinic{{background:rgba(29,158,117,.15);color:var(--teal)}}
  .stat-row{{display:grid;gap:8px;margin-bottom:16px}}
  .cols-4{{grid-template-columns:repeat(4,1fr)}}
  .cols-2{{grid-template-columns:repeat(2,1fr)}}
  .stat{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:13px 15px}}
  .stat-label{{font-size:10px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin-bottom:5px}}
  .stat-value{{font-size:24px;font-weight:700;line-height:1}}
  .stat-sub{{font-size:10px;color:var(--muted);margin-top:4px}}
  .funnel{{display:flex;flex-direction:column;gap:4px;margin-bottom:20px}}
  .f-row{{display:flex;align-items:center;gap:10px}}
  .f-lbl{{width:148px;font-size:11px;color:var(--muted);text-align:right;flex-shrink:0}}
  .f-track{{flex:1;background:var(--surface2);border-radius:3px;height:18px;overflow:hidden}}
  .f-bar{{height:100%;border-radius:3px;display:flex;align-items:center;padding:0 8px;font-size:10px;font-weight:700;color:rgba(255,255,255,.85)}}
  .f-n{{width:24px;font-size:12px;font-weight:700;text-align:right;flex-shrink:0}}
  .dm-list{{display:flex;flex-direction:column;gap:5px;margin-bottom:16px}}
  .dm-row{{display:flex;align-items:center;gap:10px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:9px 13px}}
  .dm-name{{font-weight:600;font-size:12px;min-width:148px}}
  .dm-handle{{font-size:10px;color:var(--muted);min-width:160px}}
  .dm-badge{{min-width:92px;flex-shrink:0}}
  .dm-note{{font-size:10px;color:var(--muted);flex:1}}
  .tag{{display:inline-block;font-size:9px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;border-radius:3px;padding:2px 7px;margin-top:0}}
  .t-pink{{background:rgba(192,38,168,.15);color:var(--pink)}}
  .t-amber{{background:rgba(186,117,23,.15);color:var(--amber)}}
  .t-green{{background:rgba(99,153,34,.15);color:var(--green)}}
  .t-red{{background:rgba(226,75,74,.15);color:var(--red)}}
  .t-grey{{background:rgba(136,135,128,.15);color:var(--grey)}}
  .div{{height:1px;background:var(--border);margin:22px 0}}
  .slbl{{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin-bottom:10px}}
  .alert{{background:rgba(186,117,23,.08);border:1px solid rgba(186,117,23,.2);border-radius:6px;padding:9px 14px;font-size:11px;color:var(--amber);margin-bottom:20px}}
  .footer{{text-align:center;font-size:10px;color:var(--dim);padding:18px 0 2px;margin-top:20px;border-top:1px solid var(--border)}}
</style>
</head>
<body>
<div class="tabs">
  <div class="tab active" onclick="show('influencers',this)">Influencers</div>
  <div class="tab" onclick="show('clinics',this)">UK Clinics</div>
</div>

<div class="page active" id="influencers">
  <div class="alert">Dashboard auto-refreshed daily at 10:00 UTC · Last update: {today_short}</div>

  <div class="ch-header">
    <div class="ch-title">📧 Email Outreach</div>
    <div class="ch-pill pill-email">{email_contacted} contacted</div>
  </div>

  <div class="stat-row cols-4">
    <div class="stat">
      <div class="stat-label">Total in DB</div>
      <div class="stat-value" style="color:var(--purple)">{total_inf}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Intake Filled</div>
      <div class="stat-value" style="color:var(--amber)">{email_intake}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Product Delivered</div>
      <div class="stat-value" style="color:var(--green)">{email_delivered}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Declined</div>
      <div class="stat-value" style="color:var(--red)">{email_declined}</div>
    </div>
  </div>

  <div class="slbl">Email Funnel</div>
  <div class="funnel">{email_funnel}</div>

  <div class="div"></div>

  <div class="ch-header">
    <div class="ch-title">📸 Instagram DM</div>
    <div class="ch-pill pill-dm">{dm_sent} DMs · {dm_responded} responded</div>
  </div>

  <div class="stat-row cols-4">
    <div class="stat">
      <div class="stat-label">DMs Sent</div>
      <div class="stat-value" style="color:var(--pink)">{dm_sent}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Responded</div>
      <div class="stat-value" style="color:var(--teal)">{dm_responded}</div>
      <div class="stat-sub">{round(dm_responded/max(dm_sent,1)*100)}% response rate</div>
    </div>
    <div class="stat">
      <div class="stat-label">Positive / Converted</div>
      <div class="stat-value" style="color:var(--green)">{dm_positive}</div>
      <div class="stat-sub">{dm_converted} converted</div>
    </div>
    <div class="stat">
      <div class="stat-label">No Response</div>
      <div class="stat-value" style="color:var(--grey)">{dm_no_resp}</div>
    </div>
  </div>

  <div class="slbl">DM Tracker</div>
  <div class="dm-list">{dm_rows_html}</div>

  <div class="footer">Last updated: {today_short} · Auto-generated from Notion · benjiaxolt.github.io/axolt-dashboard</div>
</div>

<div class="page" id="clinics">
  <div class="ch-header">
    <div class="ch-title">🏥 UK Clinics</div>
    <div class="ch-pill pill-clinic">{total_clinics} contacted across 2 batches</div>
  </div>

  <div class="stat-row cols-4">
    <div class="stat">
      <div class="stat-label">Total in DB</div>
      <div class="stat-value" style="color:var(--teal)">{total_clinics}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Outreach Sent</div>
      <div class="stat-value" style="color:var(--teal)">{total_clinics}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Meeting Booked</div>
      <div class="stat-value" style="color:var(--amber)">{clinic_meeting}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Declined</div>
      <div class="stat-value" style="color:var(--red)">{clinic_declined}</div>
    </div>
  </div>

  <div class="slbl">Clinic Funnel</div>
  <div class="funnel">{clinic_funnel}</div>

  <div class="stat-row cols-2">
    <div class="stat" style="border-left:3px solid var(--teal)">
      <div class="stat-label">Batch 1 — 21–22 Jun</div>
      <div class="stat-value" style="color:var(--teal)">25</div>
      <div class="stat-sub">Standard subject line</div>
    </div>
    <div class="stat" style="border-left:3px solid var(--dark-purple)">
      <div class="stat-label">Batch 2 — 25 Jun · A/B Test</div>
      <div class="stat-value" style="color:var(--dark-purple)">20</div>
      <div class="stat-sub">Subject: "Strange ask"</div>
    </div>
  </div>

  <div class="footer">Last updated: {today_short} · Auto-generated from Notion · benjiaxolt.github.io/axolt-dashboard</div>
</div>

<script>
  function show(id,el){{
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    el.classList.add('active');
  }}
</script>
</body>
</html>"""

def post_slack(url, dashboard_url):
    today = datetime.now(timezone.utc).strftime("%-d %B %Y")
    payload = {
        "text": f"📊 *Axolt Seeding Dashboard* — {today}\n👉 <{dashboard_url}|View live dashboard>"
    }
    requests.post(url, json=payload)

if __name__ == "__main__":
    print("Fetching Notion databases...")
    inf_pages = query_db(INFLUENCER_DB)
    clinic_pages = query_db(CLINIC_DB)
    print(f"  Influencers: {len(inf_pages)} pages")
    print(f"  Clinics: {len(clinic_pages)} pages")

    html = build_dashboard(inf_pages, clinic_pages)

    with open("index.html", "w") as f:
        f.write(html)
    print("index.html written.")

    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if slack_url:
        post_slack(slack_url, "https://benjiaxolt.github.io/axolt-dashboard")
        print("Slack notification sent.")
