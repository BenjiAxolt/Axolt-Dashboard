import os
import requests
from datetime import datetime, timezone

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
INFLUENCER_DB = "f07a187424e64bc7b1b992ceced311c5"
CLINIC_DB = "cb01c955a4664a1eb0d66c1f835f1243"
DASHBOARD_URL = "https://benjiaxolt.github.io/Axolt-Dashboard"

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
    if t == "email":
        return p.get("email")
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

def followers_fmt(n):
    if not n:
        return ""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(int(n))

def bar(label, count, total, color, dim=False):
    pct = max(4, round(count / max(total, 1) * 100))
    op = "opacity:0.4;" if dim and count == 0 else ""
    txt = color if count else "var(--dim)"
    return (
        '<div class="f-row">'
        f'<div class="f-lbl">{label}</div>'
        '<div class="f-track">'
        f'<div class="f-bar" style="width:{pct}%;background:{color};{op}min-width:28px">{count}</div>'
        '</div>'
        f'<div class="f-n" style="color:{txt}">{count}</div>'
        '</div>'
    )

def dm_badge(p):
    decision = get_prop(p, "DM Decision")
    stage = get_prop(p, "Stage") or ""
    delivered_stages = [
        "Intake Survey Filled", "Product Delivered",
        "14-Day Survey Sent", "14-Day Survey Filled",
        "30-Day Survey Sent", "30-Day Survey Filled"
    ]
    if stage in delivered_stages:
        return (
            '<span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;'
            'background:rgba(192,38,168,.15);color:var(--pink)">Converted</span>',
            "var(--pink)"
        )
    if decision == "Interested":
        return (
            '<span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;'
            'background:rgba(99,153,34,.15);color:var(--green)">Interested</span>',
            "var(--green)"
        )
    if decision == "Not Now":
        return (
            '<span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;'
            'background:rgba(186,117,23,.15);color:var(--amber)">Not Now</span>',
            "var(--amber)"
        )
    if decision == "Declined":
        return (
            '<span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;'
            'background:rgba(226,75,74,.15);color:var(--red)">Declined</span>',
            "var(--red)"
        )
    return (
        '<span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;'
        'background:rgba(136,135,128,.15);color:var(--grey)">No Response</span>',
        "var(--border)"
    )

CSS = """
  :root{--purple:#7F77DD;--teal:#1D9E75;--amber:#BA7517;--green:#639922;--dark-purple:#534AB7;--pink:#C026A8;--red:#E24B4A;--grey:#888780;--bg:#0f0f12;--surface:#1a1a20;--surface2:#22222a;--border:#2a2a34;--text:#e8e8f0;--muted:#7a7a88;--dim:#44444f}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;line-height:1.5}
  .tabs{display:flex;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100}
  .tab{padding:13px 24px;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px;user-select:none}
  .tab.active{color:var(--purple);border-bottom-color:var(--purple)}
  .tab:nth-child(2).active{color:var(--teal);border-bottom-color:var(--teal)}
  .page{display:none;padding:24px;max-width:980px;margin:0 auto}
  .page.active{display:block}
  .ch-header{display:flex;align-items:center;gap:10px;padding:10px 0 14px;border-bottom:1px solid var(--border);margin-bottom:16px}
  .ch-title{font-size:14px;font-weight:700}
  .ch-pill{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;border-radius:20px;padding:3px 10px}
  .pill-e{background:rgba(127,119,221,.15);color:var(--purple)}
  .pill-d{background:rgba(192,38,168,.15);color:var(--pink)}
  .pill-c{background:rgba(29,158,117,.15);color:var(--teal)}
  .stat-row{display:grid;gap:8px;margin-bottom:16px}
  .g4{grid-template-columns:repeat(4,1fr)}
  .g2{grid-template-columns:repeat(2,1fr)}
  .stat{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:13px 15px}
  .stat-label{font-size:10px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin-bottom:5px}
  .stat-value{font-size:24px;font-weight:700;line-height:1}
  .stat-sub{font-size:10px;color:var(--muted);margin-top:4px}
  .funnel{display:flex;flex-direction:column;gap:4px;margin-bottom:20px}
  .f-row{display:flex;align-items:center;gap:10px}
  .f-lbl{width:148px;font-size:11px;color:var(--muted);text-align:right;flex-shrink:0}
  .f-track{flex:1;background:var(--surface2);border-radius:3px;height:18px;overflow:hidden}
  .f-bar{height:100%;border-radius:3px;display:flex;align-items:center;padding:0 8px;font-size:10px;font-weight:700;color:rgba(255,255,255,.85)}
  .f-n{width:24px;font-size:12px;font-weight:700;text-align:right;flex-shrink:0}
  .dm-list{display:flex;flex-direction:column;gap:5px;margin-bottom:16px}
  .dm-row{display:flex;align-items:center;gap:10px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:9px 13px}
  .dm-name{font-weight:600;font-size:12px;min-width:148px}
  .dm-handle{font-size:10px;color:var(--muted);min-width:150px}
  .dm-badge{min-width:110px;flex-shrink:0}
  .dm-note{font-size:10px;color:var(--muted);flex:1}
  .seed-header{display:flex;gap:10px;padding:6px 13px;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);margin-bottom:4px}
  .seed-list{display:flex;flex-direction:column;gap:4px;margin-bottom:16px}
  .seed-row{display:flex;align-items:center;gap:10px;background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--green);border-radius:6px;padding:9px 13px}
  .seed-name{font-weight:600;font-size:12px;min-width:148px}
  .seed-handle{font-size:10px;color:var(--muted);min-width:130px}
  .seed-followers{font-size:11px;font-weight:600;color:var(--purple);min-width:52px}
  .seed-cat{font-size:10px;color:var(--muted);flex:1}
  .seed-del{font-size:10px;color:var(--amber);min-width:56px;text-align:center}
  .seed-stage{min-width:160px;text-align:right}
  .div{height:1px;background:var(--border);margin:22px 0}
  .slbl{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin-bottom:10px}
  .alert{background:rgba(186,117,23,.08);border:1px solid rgba(186,117,23,.2);border-radius:6px;padding:9px 14px;font-size:11px;color:var(--amber);margin-bottom:20px}
  .footer{text-align:center;font-size:10px;color:var(--dim);padding:18px 0 2px;margin-top:20px;border-top:1px solid var(--border)}
  .empty{font-size:11px;color:var(--dim);font-style:italic;padding:12px 14px}
"""

JS = """
  function show(id,el){
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    el.classList.add('active');
  }
"""

def build_dashboard(inf_pages, clinic_pages):
    today_short = datetime.now(timezone.utc).strftime("%a %-d %b %Y")

    inf_pages = [p for p in inf_pages if get_prop(p, "Stage") != "Duplicate"]
    clinic_pages = [p for p in clinic_pages if get_prop(p, "Stage") != "Duplicate"]

    dm_pages = [p for p in inf_pages if get_prop(p, "DM Outreach") is True]
    email_pages = [p for p in inf_pages if get_prop(p, "DM Outreach") is not True]

    delivered_stages = [
        "Product Delivered", "14-Day Survey Sent", "14-Day Survey Filled",
        "30-Day Survey Sent", "30-Day Survey Filled"
    ]
    intake_stages = ["Intake Survey Filled"] + delivered_stages

    email_contacted = sum(1 for p in email_pages if get_prop(p, "Stage") not in [None, "Lead"])
    email_intake = sum(1 for p in email_pages if get_prop(p, "Stage") in intake_stages)
    email_delivered = sum(1 for p in email_pages if get_prop(p, "Stage") in delivered_stages)
    email_survey_14 = sum(1 for p in email_pages if get_prop(p, "Stage") in ["14-Day Survey Sent", "14-Day Survey Filled"])
    email_survey_30 = sum(1 for p in email_pages if get_prop(p, "Stage") in ["30-Day Survey Sent", "30-Day Survey Filled"])
    email_declined = sum(1 for p in email_pages if get_prop(p, "Stage") == "Declined")

    dm_sent = len(dm_pages)
    dm_responded = sum(1 for p in dm_pages if get_prop(p, "DM Response") is True)
    dm_interested = sum(1 for p in dm_pages if get_prop(p, "DM Decision") == "Interested")
    dm_converted = sum(1 for p in dm_pages if get_prop(p, "Stage") in intake_stages)
    dm_no_resp = sum(1 for p in dm_pages if get_prop(p, "DM Decision") in ["No Response", None])
    resp_rate = round(dm_responded / max(dm_sent, 1) * 100)

    total_clinics = len(clinic_pages)
    clinic_replied = sum(1 for p in clinic_pages if get_prop(p, "Stage") not in [None, "Lead", "Contacted"])
    clinic_meeting = sum(1 for p in clinic_pages if get_prop(p, "Stage") in ["Meeting Booked", "Meeting Held", "Replied"])
    clinic_declined = sum(1 for p in clinic_pages if get_prop(p, "Stage") == "Declined")

    seeded = [p for p in inf_pages if get_prop(p, "Stage") in delivered_stages]
    seeded_rows = []
    for p in seeded:
        name = get_prop(p, "Name") or "Unknown"
        handle = get_prop(p, "Handle") or ""
        stage = get_prop(p, "Stage") or ""
        delivered = fmt_date(get_prop(p, "Product Delivered"))
        followers = followers_fmt(get_prop(p, "Followers"))
        category = ", ".join(get_prop(p, "Category") or [])
        if "30-Day" in stage:
            sc, sb = "var(--dark-purple)", "rgba(83,74,183,0.15)"
        elif "14-Day" in stage:
            sc, sb = "var(--purple)", "rgba(127,119,221,0.15)"
        else:
            sc, sb = "var(--green)", "rgba(99,153,34,0.15)"
        stage_span = (
            '<span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;'
            'background:' + sb + ';color:' + sc + '">' + stage + '</span>'
        )
        seeded_rows.append(
            '<div class="seed-row">'
            '<div class="seed-name">' + name + '</div>'
            '<div class="seed-handle">' + handle + '</div>'
            '<div class="seed-followers">' + followers + '</div>'
            '<div class="seed-cat">' + category + '</div>'
            '<div class="seed-del">' + delivered + '</div>'
            '<div class="seed-stage">' + stage_span + '</div>'
            '</div>'
        )

    if seeded_rows:
        seeded_section = (
            '<div class="seed-header">'
            '<div style="min-width:148px">Name</div>'
            '<div style="min-width:130px">Handle</div>'
            '<div style="min-width:52px">Followers</div>'
            '<div style="flex:1">Category</div>'
            '<div style="min-width:56px;text-align:center">Delivered</div>'
            '<div style="min-width:160px;text-align:right">Stage</div>'
            '</div>'
            '<div class="seed-list">' + "".join(seeded_rows) + '</div>'
        )
    else:
        seeded_section = '<div class="empty">No influencers seeded yet.</div>'

    dm_rows = []
    for p in dm_pages:
        name = get_prop(p, "Name") or "Unknown"
        handle = get_prop(p, "Handle") or ""
        note = (get_prop(p, "Note") or "")[:90]
        badge, border = dm_badge(p)
        dm_rows.append(
            '<div class="dm-row" style="border-left:3px solid ' + border + '">'
            '<div class="dm-name">' + name + '</div>'
            '<div class="dm-handle">' + handle + '</div>'
            '<div class="dm-badge">' + badge + '</div>'
            '<div class="dm-note">' + note + '</div>'
            '</div>'
        )
    dm_section = "".join(dm_rows) if dm_rows else '<div class="empty">No DMs recorded yet.</div>'

    email_funnel = (
        bar("Contacted", email_contacted, email_contacted, "var(--purple)") +
        bar("Intake Filled", email_intake, email_contacted, "var(--amber)") +
        bar("Product Delivered", email_delivered, email_contacted, "var(--green)") +
        bar("14-Day Survey", email_survey_14, email_contacted, "var(--dark-purple)", dim=True) +
        bar("30-Day Survey", email_survey_30, email_contacted, "var(--dark-purple)", dim=True) +
        bar("Declined", email_declined, email_contacted, "var(--red)")
    )
    clinic_funnel = (
        bar("Contacted", total_clinics, total_clinics, "var(--teal)") +
        bar("Replied", clinic_replied, total_clinics, "var(--teal)") +
        bar("Meeting Booked", clinic_meeting, total_clinics, "var(--amber)", dim=True) +
        bar("Partnership", 0, total_clinics, "var(--green)", dim=True) +
        bar("Declined", clinic_declined, total_clinics, "var(--red)")
    )

    email_total = len(email_pages)

    parts = []
    parts.append("<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append("<title>Axolt Seeding Dashboard</title>")
    parts.append("<style>" + CSS + "</style></head><body>")
    parts.append("<div class='tabs'>")
    parts.append("  <div class='tab active' onclick=\"show('inf',this)\">Influencers</div>")
    parts.append("  <div class='tab' onclick=\"show('cli',this)\">UK Clinics</div>")
    parts.append("</div>")

    parts.append("<div class='page active' id='inf'>")
    parts.append("  <div class='alert'>Auto-refreshed daily at 11:00 Prague time &middot; Last update: " + today_short + "</div>")
    parts.append("  <div class='ch-header'><div class='ch-title'>&#128231; Email Outreach</div>")
    parts.append("  <div class='ch-pill pill-e'>" + str(email_contacted) + " contacted &middot; " + str(email_total) + " total</div></div>")
    parts.append("  <div class='stat-row g4'>")
    parts.append("    <div class='stat'><div class='stat-label'>Total in DB</div><div class='stat-value' style='color:var(--purple)'>" + str(email_total) + "</div><div class='stat-sub'>" + str(email_contacted) + " contacted</div></div>")
    parts.append("    <div class='stat'><div class='stat-label'>Intake Filled</div><div class='stat-value' style='color:var(--amber)'>" + str(email_intake) + "</div></div>")
    parts.append("    <div class='stat'><div class='stat-label'>Product Delivered</div><div class='stat-value' style='color:var(--green)'>" + str(email_delivered) + "</div></div>")
    parts.append("    <div class='stat'><div class='stat-label'>Declined</div><div class='stat-value' style='color:var(--red)'>" + str(email_declined) + "</div></div>")
    parts.append("  </div>")
    parts.append("  <div class='slbl'>Email Funnel</div>")
    parts.append("  <div class='funnel'>" + email_funnel + "</div>")
    parts.append("  <div class='slbl'>Seeded Influencers (" + str(len(seeded)) + ")</div>")
    parts.append("  " + seeded_section)
    parts.append("  <div class='div'></div>")
    parts.append("  <div class='ch-header'><div class='ch-title'>&#128247; Instagram DM</div>")
    parts.append("  <div class='ch-pill pill-d'>" + str(dm_sent) + " DMs &middot; " + str(resp_rate) + "% responded</div></div>")
    parts.append("  <div class='stat-row g4'>")
    parts.append("    <div class='stat'><div class='stat-label'>DMs Sent</div><div class='stat-value' style='color:var(--pink)'>" + str(dm_sent) + "</div></div>")
    parts.append("    <div class='stat'
