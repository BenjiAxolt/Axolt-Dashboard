import os
import requests
from datetime import datetime, timezone

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
INFLUENCER_DB = "f07a187424e64bc7b1b992ceced311c5"
CLINIC_DB = "cb01c955a4664a1eb0d66c1f835f1243"
DASHBOARD_URL = "https://benjiaxolt.github.io/Axolt-Dashboard"

HEADERS = {
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
            headers=HEADERS,
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


def make_bar(label, count, total, color, dim=False):
    pct = max(4, round(count / max(total, 1) * 100))
    opacity = "opacity:0.4;" if (dim and count == 0) else ""
    txt_color = color if count else "#44444f"
    return (
        "<div class=f-row>"
        "<div class=f-lbl>" + label + "</div>"
        "<div class=f-track>"
        "<div class=f-bar style='width:" + str(pct) + "%;background:" + color + ";" + opacity + "min-width:28px'>" + str(count) + "</div>"
        "</div>"
        "<div class=f-n style='color:" + txt_color + "'>" + str(count) + "</div>"
        "</div>"
    )


def make_dm_badge(p):
    decision = get_prop(p, "DM Decision") or ""
    stage = get_prop(p, "Stage") or ""
    active = ["Intake Survey Filled", "Product Delivered", "14-Day Survey Sent",
              "14-Day Survey Filled", "30-Day Survey Sent", "30-Day Survey Filled"]
    if stage in active:
        return ("Converted", "#C026A8", "rgba(192,38,168,.15)", "#C026A8")
    if decision == "Interested":
        return ("Interested", "#639922", "rgba(99,153,34,.15)", "#639922")
    if decision == "Not Now":
        return ("Not Now", "#BA7517", "rgba(186,117,23,.15)", "#BA7517")
    if decision == "Declined":
        return ("Declined", "#E24B4A", "rgba(226,75,74,.15)", "#E24B4A")
    return ("No Response", "#888780", "rgba(136,135,128,.15)", "#2a2a34")


def build_dashboard(inf_pages, clinic_pages):
    today = datetime.now(timezone.utc).strftime("%a %-d %b %Y")

    inf_pages = [p for p in inf_pages if get_prop(p, "Stage") != "Duplicate"]
    clinic_pages = [p for p in clinic_pages if get_prop(p, "Stage") != "Duplicate"]

    dm_pages = [p for p in inf_pages if get_prop(p, "DM Outreach") is True]
    email_pages = [p for p in inf_pages if not (get_prop(p, "DM Outreach") is True)]

    delivered = ["Product Delivered", "14-Day Survey Sent", "14-Day Survey Filled",
                 "30-Day Survey Sent", "30-Day Survey Filled"]
    intake_plus = ["Intake Survey Filled"] + delivered

    ec = sum(1 for p in email_pages if get_prop(p, "Stage") not in [None, "Lead"])
    ei = sum(1 for p in email_pages if get_prop(p, "Stage") in intake_plus)
    ed = sum(1 for p in email_pages if get_prop(p, "Stage") in delivered)
    e14 = sum(1 for p in email_pages if get_prop(p, "Stage") in ["14-Day Survey Sent", "14-Day Survey Filled"])
    e30 = sum(1 for p in email_pages if get_prop(p, "Stage") in ["30-Day Survey Sent", "30-Day Survey Filled"])
    edecl = sum(1 for p in email_pages if get_prop(p, "Stage") == "Declined")

    ds = len(dm_pages)
    dr = sum(1 for p in dm_pages if get_prop(p, "DM Response") is True)
    di = sum(1 for p in dm_pages if get_prop(p, "DM Decision") == "Interested")
    dc = sum(1 for p in dm_pages if get_prop(p, "Stage") in intake_plus)
    dnr = sum(1 for p in dm_pages if get_prop(p, "DM Decision") in ["No Response", None])
    rr = round(dr / max(ds, 1) * 100)

    tc = len(clinic_pages)
    cm = sum(1 for p in clinic_pages if get_prop(p, "Stage") in ["Meeting Booked", "Meeting Held", "Replied"])
    cdecl = sum(1 for p in clinic_pages if get_prop(p, "Stage") == "Declined")
    creplied = sum(1 for p in clinic_pages if get_prop(p, "Stage") not in [None, "Lead", "Contacted"])

    seeded = [p for p in inf_pages if get_prop(p, "Stage") in delivered]

    # Build seeded rows
    seed_html = ""
    for p in seeded:
        name = get_prop(p, "Name") or "Unknown"
        handle = get_prop(p, "Handle") or ""
        stage = get_prop(p, "Stage") or ""
        del_date = fmt_date(get_prop(p, "Product Delivered"))
        foll = fmt_followers(get_prop(p, "Followers"))
        cat = ", ".join(get_prop(p, "Category") or [])
        if "30-Day" in stage:
            sc, sb = "#534AB7", "rgba(83,74,183,0.15)"
        elif "14-Day" in stage:
            sc, sb = "#7F77DD", "rgba(127,119,221,0.15)"
        else:
            sc, sb = "#639922", "rgba(99,153,34,0.15)"
        badge = "<span style='font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;background:" + sb + ";color:" + sc + "'>" + stage + "</span>"
        seed_html += (
            "<div class=seed-row>"
            "<div class=seed-name>" + name + "</div>"
            "<div class=seed-handle>" + handle + "</div>"
            "<div class=seed-foll>" + foll + "</div>"
            "<div class=seed-cat>" + cat + "</div>"
            "<div class=seed-del>" + del_date + "</div>"
            "<div class=seed-stage>" + badge + "</div>"
            "</div>"
        )

    if seed_html:
        seeded_block = (
            "<div class=seed-header>"
            "<div style='min-width:148px'>Name</div>"
            "<div style='min-width:130px'>Handle</div>"
            "<div style='min-width:52px'>Followers</div>"
            "<div style='flex:1'>Category</div>"
            "<div style='min-width:56px;text-align:center'>Delivered</div>"
            "<div style='min-width:160px;text-align:right'>Stage</div>"
            "</div>"
            "<div class=seed-list>" + seed_html + "</div>"
        )
    else:
        seeded_block = "<div class=empty>No influencers seeded yet.</div>"

    # Build DM rows
    dm_html = ""
    for p in dm_pages:
        name = get_prop(p, "Name") or "Unknown"
        handle = get_prop(p, "Handle") or ""
        note = (get_prop(p, "Note") or "")[:90]
        label, color, bg, border = make_dm_badge(p)
        badge = "<span style='font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;background:" + bg + ";color:" + color + "'>" + label + "</span>"
        dm_html += (
            "<div class=dm-row style='border-left:3px solid " + border + "'>"
            "<div class=dm-name>" + name + "</div>"
            "<div class=dm-handle>" + handle + "</div>"
            "<div class=dm-badge>" + badge + "</div>"
            "<div class=dm-note>" + note + "</div>"
            "</div>"
        )
    if not dm_html:
        dm_html = "<div class=empty>No DMs recorded yet.</div>"

    # Funnels
    ef = (
        make_bar("Contacted", ec, ec, "#7F77DD") +
        make_bar("Intake Filled", ei, ec, "#BA7517") +
        make_bar("Product Delivered", ed, ec, "#639922") +
        make_bar("14-Day Survey", e14, ec, "#534AB7", dim=True) +
        make_bar("30-Day Survey", e30, ec, "#534AB7", dim=True) +
        make_bar("Declined", edecl, ec, "#E24B4A")
    )
    cf = (
        make_bar("Contacted", tc, tc, "#1D9E75") +
        make_bar("Replied", creplied, tc, "#1D9E75") +
        make_bar("Meeting Booked", cm, tc, "#BA7517", dim=True) +
        make_bar("Partnership", 0, tc, "#639922", dim=True) +
        make_bar("Declined", cdecl, tc, "#E24B4A")
    )

    et = len(email_pages)

    html = """<!DOCTYPE html>
<html lang=en>
<head>
<meta charset=UTF-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Axolt Seeding Dashboard</title>
<style>
:root{--p:#7F77DD;--t:#1D9E75;--a:#BA7517;--g:#639922;--dp:#534AB7;--pk:#C026A8;--r:#E24B4A;--gr:#888780;--bg:#0f0f12;--s:#1a1a20;--s2:#22222a;--b:#2a2a34;--tx:#e8e8f0;--mu:#7a7a88;--d:#44444f}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;font-size:13px;line-height:1.5}
.tabs{display:flex;background:var(--s);border-bottom:1px solid var(--b);position:sticky;top:0;z-index:100}
.tab{padding:13px 24px;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--mu);border-bottom:2px solid transparent;margin-bottom:-1px;user-select:none}
.tab.active{color:var(--p);border-bottom-color:var(--p)}
.tab:nth-child(2).active{color:var(--t);border-bottom-color:var(--t)}
.page{display:none;padding:24px;max-width:980px;margin:0 auto}
.page.active{display:block}
.ch-hd{display:flex;align-items:center;gap:10px;padding:10px 0 14px;border-bottom:1px solid var(--b);margin-bottom:16px}
.ch-ti{font-size:14px;font-weight:700}
.pill{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;border-radius:20px;padding:3px 10px}
.pe{background:rgba(127,119,221,.15);color:var(--p)}
.pd{background:rgba(192,38,168,.15);color:var(--pk)}
.pc{background:rgba(29,158,117,.15);color:var(--t)}
.sg{display:grid;gap:8px;margin-bottom:16px}
.g4{grid-template-columns:repeat(4,1fr)}
.g2{grid-template-columns:repeat(2,1fr)}
.card{background:var(--s);border:1px solid var(--b);border-radius:8px;padding:13px 15px}
.cl{font-size:10px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--mu);margin-bottom:5px}
.cv{font-size:24px;font-weight:700;line-height:1}
.cs{font-size:10px;color:var(--mu);margin-top:4px}
.funnel{display:flex;flex-direction:column;gap:4px;margin-bottom:20px}
.f-row{display:flex;align-items:center;gap:10px}
.f-lbl{width:148px;font-size:11px;color:var(--mu);text-align:right;flex-shrink:0}
.f-track{flex:1;background:var(--s2);border-radius:3px;height:18px;overflow:hidden}
.f-bar{height:100%;border-radius:3px;display:flex;align-items:center;padding:0 8px;font-size:10px;font-weight:700;color:rgba(255,255,255,.85)}
.f-n{width:24px;font-size:12px;font-weight:700;text-align:right;flex-shrink:0}
.dm-list{display:flex;flex-direction:column;gap:5px;margin-bottom:16px}
.dm-row{display:flex;align-items:center;gap:10px;background:var(--s);border:1px solid var(--b);border-radius:6px;padding:9px 13px}
.dm-name{font-weight:600;font-size:12px;min-width:148px}
.dm-handle{font-size:10px;color:var(--mu);min-width:150px}
.dm-badge{min-width:110px;flex-shrink:0}
.dm-note{font-size:10px;color:var(--mu);flex:1}
.seed-header{display:flex;gap:10px;padding:6px 13px;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--d);margin-bottom:4px}
.seed-list{display:flex;flex-direction:column;gap:4px;margin-bottom:16px}
.seed-row{display:flex;align-items:center;gap:10px;background:var(--s);border:1px solid var(--b);border-left:3px solid var(--g);border-radius:6px;padding:9px 13px}
.seed-name{font-weight:600;font-size:12px;min-width:148px}
.seed-handle{font-size:10px;color:var(--mu);min-width:130px}
.seed-foll{font-size:11px;font-weight:600;color:var(--p);min-width:52px}
.seed-cat{font-size:10px;color:var(--mu);flex:1}
.seed-del{font-size:10px;color:var(--a);min-width:56px;text-align:center}
.seed-stage{min-width:160px;text-align:right}
.div{height:1px;background:var(--b);margin:22px 0}
.slbl{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--d);margin-bottom:10px}
.alert{background:rgba(186,117,23,.08);border:1px solid rgba(186,117,23,.2);border-radius:6px;padding:9px 14px;font-size:11px;color:var(--a);margin-bottom:20px}
.footer{text-align:center;font-size:10px;color:var(--d);padding:18px 0 2px;margin-top:20px;border-top:1px solid var(--b)}
.empty{font-size:11px;color:var(--d);font-style:italic;padding:12px 14px}
</style>
</head>
<body>
<div class=tabs>
<div class="tab active" onclick="show('inf',this)">Influencers</div>
<div class=tab onclick="show('cli',this)">UK Clinics</div>
</div>
"""

    html += "<div class='page active' id=inf>"
    html += "<div class=alert>Auto-refreshed daily at 11:00 Prague time &middot; Last update: " + today + "</div>"
    html += "<div class=ch-hd><div class=ch-ti>&#128231; Email Outreach</div><div class='pill pe'>" + str(ec) + " contacted &middot; " + str(et) + " total</div></div>"
    html += "<div class='sg g4'>"
    html += "<div class=card><div class=cl>Total in DB</div><div class=cv style='color:#7F77DD'>" + str(et) + "</div><div class=cs>" + str(ec) + " contacted</div></div>"
    html += "<div class=card><div class=cl>Intake Filled</div><div class=cv style='color:#BA7517'>" + str(ei) + "</div></div>"
    html += "<div class=card><div class=cl>Product Delivered</div><div class=cv style='color:#639922'>" + str(ed) + "</div></div>"
    html += "<div class=card><div class=cl>Declined</div><div class=cv style='color:#E24B4A'>" + str(edecl) + "</div></div>"
    html += "</div>"
    html += "<div class=slbl>Email Funnel</div><div class=funnel>" + ef + "</div>"
    html += "<div class=slbl>Seeded Influencers (" + str(len(seeded)) + ")</div>"
    html += seeded_block
    html += "<div class=div></div>"
    html += "<div class=ch-hd><div class=ch-ti>&#128247; Instagram DM</div><div class='pill pd'>" + str(ds) + " DMs &middot; " + str(rr) + "% responded</div></div>"
    html += "<div class='sg g4'>"
    html += "<div class=card><div class=cl>DMs Sent</div><div class=cv style='color:#C026A8'>" + str(ds) + "</div></div>"
    html += "<div class=card><div class=cl>Responded</div><div class=cv style='color:#1D9E75'>" + str(dr) + "</div><div class=cs>" + str(rr) + "% rate</div></div>"
    html += "<div class=card><div class=cl>Interested</div><div class=cv style='color:#639922'>" + str(di) + "</div><div class=cs>" + str(dc) + " converted</div></div>"
    html += "<div class=card><div class=cl>No Response</div><div class=cv style='color:#888780'>" + str(dnr) + "</div></div>"
    html += "</div>"
    html += "<div class=slbl>DM Tracker</div><div class=dm-list>" + dm_html + "</div>"
    html += "<div class=footer>Last updated: " + today + " &middot; " + DASHBOARD_URL + "</div>"
    html += "</div>"

    html += "<div class=page id=cli>"
    html += "<div class=ch-hd><div class=ch-ti>&#127973; UK Clinics</div><div class='pill pc'>" + str(tc) + " contacted &middot; 2 batches</div></div>"
    html += "<div class='sg g4'>"
    html += "<div class=card><div class=cl>Total in DB</div><div class=cv style='color:#1D9E75'>" + str(tc) + "</div></div>"
    html += "<div class=card><div class=cl>Outreach Sent</div><div class=cv style='color:#1D9E75'>" + str(tc) + "</div></div>"
    html += "<div class=card><div class=cl>Meeting Booked</div><div class=cv style='color:#BA7517'>" + str(cm) + "</div></div>"
    html += "<div class=card><div class=cl>Declined</div><div class=cv style='color:#E24B4A'>" + str(cdecl) + "</div></div>"
    html += "</div>"
    html += "<div class=slbl>Clinic Funnel</div><div class=funnel>" + cf + "</div>"
    html += "<div class='sg g2'>"
    html += "<div class=card style='border-left:3px solid #1D9E75'><div class=cl>Batch 1 - 21-22 Jun</div><div class=cv style='color:#1D9E75'>25</div><div class=cs>Standard subject line</div></div>"
    html += "<div class=card style='border-left:3px solid #534AB7'><div class=cl>Batch 2 - 25 Jun - A/B Test</div><div class=cv style='color:#534AB7'>20</div><div class=cs>Subject: Strange ask</div></div>"
    html += "</div>"
    html += "<div class=footer>Last updated: " + today + " &middot; " + DASHBOARD_URL + "</div>"
    html += "</div>"

    html += """
<script>
function show(id,el){
document.querySelectorAll('.page').forEach(function(p){p.classList.remove('active')});
document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active')});
document.getElementById(id).classList.add('active');
el.classList.add('active');
}
</script>
</body></html>"""

    return html


def post_slack(webhook_url):
    today = datetime.now(timezone.utc).strftime("%-d %B %Y")
    requests.post(webhook_url, json={
        "text": "Axolt Seeding Dashboard - " + today + "\n" + DASHBOARD_URL
    })


if __name__ == "__main__":
    print("Fetching Notion data...")
    inf_pages = query_db(INFLUENCER_DB)
    clinic_pages = query_db(CLINIC_DB)
    print("Influencers: " + str(len(inf_pages)) + ", Clinics: " + str(len(clinic_pages)))
    html = build_dashboard(inf_pages, clinic_pages)
    with open("index.html", "w") as f:
        f.write(html)
    print("index.html written.")
    slack = os.environ.get("SLACK_WEBHOOK_URL", "")
    if slack:
        post_slack(slack)
        print("Slack notified.")
