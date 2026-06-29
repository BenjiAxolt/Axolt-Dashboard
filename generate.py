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


ARROW_SVG = "<svg width='8' height='8' viewBox='0 0 8 8'><path d='M4 0v6M1 4l3 3 3-3' stroke='#44444f' stroke-width='1.2' fill='none'/></svg>"


def conv_pct(a, b):
    if not b:
        return ""
    return str(round(a / b * 100)) + "%"


def drop_row(pct_str, label, color):
    if not pct_str:
        return ""
    return (
        "<div class=f-drop>"
        "<div class=f-drop-inner>"
        + ARROW_SVG +
        "<span style='color:" + color + "'>" + pct_str + " " + label + "</span>"
        "</div>"
        "</div>"
    )


def make_bar(label, count, total, color, faded=False):
    pct = max(4, round(count / max(total, 1) * 100))
    opacity = "opacity:0.35;" if faded and count == 0 else ""
    lbl_color = "#44444f" if (faded and count == 0) else color
    return (
        "<div class=f-row>"
        "<div class='f-lbl' style='color:" + lbl_color + "'>" + label + "</div>"
        "<div class=f-track>"
        "<div class=f-bar style='width:" + str(pct) + "%;background:" + color + ";" + opacity + "min-width:36px'>" + str(count) + "</div>"
        "</div>"
        "</div>"
    )


def build_dashboard(inf_pages, clinic_pages):
    today = datetime.now(timezone.utc).strftime("%a %-d %b %Y")

    # Filter duplicates
    inf_pages = [p for p in inf_pages if get_prop(p, "Stage") != "Duplicate"]
    clinic_pages = [p for p in clinic_pages if get_prop(p, "Stage") != "Duplicate"]

    # Stage buckets
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

    # Seeded list
    seeded = [p for p in inf_pages if get_prop(p, "Stage") in DELIVERED]
    seed_rows = ""
    for p in seeded:
        name    = get_prop(p, "Name") or "Unknown"
        handle  = get_prop(p, "Handle") or ""
        stage   = get_prop(p, "Stage") or ""
        del_dt  = fmt_date(get_prop(p, "Product Delivered"))
        foll    = fmt_followers(get_prop(p, "Followers"))
        cat     = ", ".join(get_prop(p, "Category") or [])
        if "30-Day" in stage:
            sc, sb = "#534AB7", "rgba(83,74,183,0.15)"
        elif "14-Day" in stage:
            sc, sb = "#7F77DD", "rgba(127,119,221,0.15)"
        else:
            sc, sb = "#639922", "rgba(99,153,34,0.15)"
        badge = "<span style='font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;background:" + sb + ";color:" + sc + "'>" + stage + "</span>"
        seed_rows += (
            "<div class=seed-row>"
            "<div class=sn>" + name + "</div>"
            "<div class=sh>" + handle + "</div>"
            "<div class=sf>" + foll + "</div>"
            "<div class=sc>" + cat + "</div>"
            "<div class=sd>" + del_dt + "</div>"
            "<div class=ss>" + badge + "</div>"
            "</div>"
        )

    if seed_rows:
        seeded_block = (
            "<div class=seed-hd>"
            "<div style='min-width:140px'>Name</div>"
            "<div style='min-width:140px'>Handle</div>"
            "<div style='min-width:50px'>Followers</div>"
            "<div style='flex:1'>Category</div>"
            "<div style='min-width:54px;text-align:center'>Delivered</div>"
            "<div style='min-width:150px;text-align:right'>Stage</div>"
            "</div>"
            "<div class=seed-list>" + seed_rows + "</div>"
        )
    else:
        seeded_block = "<div class=empty>No influencers seeded yet.</div>"

    # Influencer funnel
    inf_funnel = (
        make_bar("Total in DB",       total_inf,   total_inf, "#534AB7") +
        drop_row(conv_pct(contacted,  total_inf),  "contacted",        "#7F77DD") +
        make_bar("Contacted",         contacted,   total_inf, "#7F77DD") +
        drop_row(conv_pct(intake,     contacted),  "filled intake",    "#BA7517") +
        make_bar("Intake Filled",     intake,      total_inf, "#BA7517") +
        drop_row(conv_pct(delivered,  intake),     "delivered",        "#639922") +
        make_bar("Product Delivered", delivered,   total_inf, "#639922") +
        "<div style='height:6px'></div>" +
        make_bar("14-Day Survey",     survey_14,   total_inf, "#534AB7", faded=True) +
        make_bar("30-Day Survey",     survey_30,   total_inf, "#534AB7", faded=True) +
        "<div style='height:6px'></div>" +
        drop_row(conv_pct(declined_inf, contacted), "of contacted declined", "#E24B4A") +
        make_bar("Declined",          declined_inf, total_inf, "#E24B4A")
    )

    # Clinic stats
    total_cli   = len(clinic_pages)
    cli_replied = sum(1 for p in clinic_pages if get_prop(p, "Stage") not in [None, "Lead", "Contacted"])
    cli_meeting = sum(1 for p in clinic_pages if get_prop(p, "Stage") in ["Meeting Booked", "Meeting Held"])
    cli_partner = sum(1 for p in clinic_pages if get_prop(p, "Stage") in ["Partnership Agreed", "Active"])
    cli_declined= sum(1 for p in clinic_pages if get_prop(p, "Stage") == "Declined")

    cli_funnel = (
        make_bar("Contacted",          total_cli,    total_cli, "#1D9E75") +
        drop_row(conv_pct(cli_replied, total_cli),   "replied",            "#1D9E75") +
        make_bar("Replied",            cli_replied,  total_cli, "#1D9E75") +
        drop_row(conv_pct(cli_meeting, cli_replied), "meeting booked",     "#BA7517") +
        make_bar("Meeting Booked",     cli_meeting,  total_cli, "#BA7517", faded=True) +
        drop_row(conv_pct(cli_partner, cli_meeting), "partnership agreed", "#639922") +
        make_bar("Partnership Agreed", cli_partner,  total_cli, "#639922", faded=True) +
        "<div style='height:6px'></div>" +
        drop_row(conv_pct(cli_declined, total_cli), "of contacted declined", "#E24B4A") +
        make_bar("Declined",           cli_declined, total_cli, "#E24B4A")
    )

    # Build HTML
    html = """<!DOCTYPE html>
<html lang=en>
<head>
<meta charset=UTF-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Axolt Seeding Dashboard</title>
<style>
:root{--p:#7F77DD;--t:#1D9E75;--a:#BA7517;--g:#639922;--dp:#534AB7;--r:#E24B4A;--bg:#0f0f12;--s:#1a1a20;--s2:#22222a;--b:#2a2a34;--tx:#e8e8f0;--mu:#7a7a88;--d:#44444f}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;font-size:13px;line-height:1.5}
.tabs{display:flex;background:var(--s);border-bottom:1px solid var(--b);position:sticky;top:0;z-index:100}
.tab{padding:13px 24px;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--mu);border-bottom:2px solid transparent;margin-bottom:-1px;user-select:none}
.tab.active{color:var(--p);border-bottom-color:var(--p)}
.tab:nth-child(2).active{color:var(--t);border-bottom-color:var(--t)}
.page{display:none;padding:24px;max-width:960px;margin:0 auto}
.page.active{display:block}
.sg{display:grid;gap:8px;margin-bottom:20px}
.g5{grid-template-columns:repeat(5,1fr)}
.g4{grid-template-columns:repeat(4,1fr)}
.card{background:var(--s);border:1px solid var(--b);border-radius:8px;padding:13px 15px}
.cl{font-size:10px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--mu);margin-bottom:5px}
.cv{font-size:24px;font-weight:700;line-height:1}
.cs{font-size:10px;color:var(--mu);margin-top:4px}
.slbl{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--d);margin-bottom:10px}
.funnel{display:flex;flex-direction:column;gap:3px;margin-bottom:24px}
.f-row{display:flex;align-items:center;gap:10px}
.f-lbl{width:160px;font-size:11px;color:var(--mu);text-align:right;flex-shrink:0}
.f-track{flex:1;background:var(--s2);border-radius:3px;height:22px;overflow:hidden}
.f-bar{height:100%;border-radius:3px;display:flex;align-items:center;padding:0 10px;font-size:10px;font-weight:700;color:rgba(255,255,255,.9)}
.f-drop{display:flex;align-items:center;gap:10px;height:16px}
.f-drop-inner{display:flex;align-items:center;gap:4px;font-size:9px;font-weight:600;margin-left:170px}
.seed-hd{display:flex;gap:10px;padding:5px 13px;font-size:9px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--d);margin-bottom:4px}
.seed-list{display:flex;flex-direction:column;gap:4px;margin-bottom:8px}
.seed-row{display:flex;align-items:center;gap:10px;background:var(--s);border:1px solid var(--b);border-left:3px solid var(--g);border-radius:6px;padding:10px 13px}
.sn{font-weight:600;font-size:12px;min-width:140px}
.sh{font-size:10px;color:var(--mu);min-width:140px}
.sf{font-size:11px;font-weight:600;color:var(--p);min-width:50px}
.sc{font-size:10px;color:var(--mu);flex:1}
.sd{font-size:10px;color:var(--a);min-width:54px;text-align:center}
.ss{min-width:150px;text-align:right}
.empty{font-size:11px;color:var(--d);font-style:italic;padding:10px 0}
.footer{text-align:center;font-size:10px;color:var(--d);padding:20px 0 2px;margin-top:20px;border-top:1px solid var(--b)}
</style>
</head>
<body>
<div class=tabs>
<div class='tab active' onclick='show("inf",this)'>Influencers</div>
<div class=tab onclick='show("cli",this)'>UK Clinics</div>
</div>
"""

    # Influencers page
    html += "<div class='page active' id=inf>"
    html += "<div class='sg g5' style='margin-top:18px'>"
    html += "<div class=card><div class=cl>Total in DB</div><div class=cv style='color:#534AB7'>" + str(total_inf) + "</div></div>"
    html += "<div class=card><div class=cl>Contacted</div><div class=cv style='color:#7F77DD'>" + str(contacted) + "</div></div>"
    html += "<div class=card><div class=cl>Intake Filled</div><div class=cv style='color:#BA7517'>" + str(intake) + "</div></div>"
    html += "<div class=card><div class=cl>Product Delivered</div><div class=cv style='color:#639922'>" + str(delivered) + "</div></div>"
    html += "<div class=card><div class=cl>Declined</div><div class=cv style='color:#E24B4A'>" + str(declined_inf) + "</div></div>"
    html += "</div>"
    html += "<div class=slbl>Pipeline Funnel</div>"
    html += "<div class=funnel>" + inf_funnel + "</div>"
    html += "<div class=slbl>Seeded (" + str(len(seeded)) + ")</div>"
    html += seeded_block
    html += "<div class=footer>Auto-refreshed daily 11:00 Prague &middot; " + DASHBOARD_URL + "</div>"
    html += "</div>"

    # Clinics page
    html += "<div class=page id=cli>"
    html += "<div class='sg g4' style='margin-top:18px'>"
    html += "<div class=card><div class=cl>Total in DB</div><div class=cv style='color:#1D9E75'>" + str(total_cli) + "</div></div>"
    html += "<div class=card><div class=cl>Outreach Sent</div><div class=cv style='color:#1D9E75'>" + str(total_cli) + "</div></div>"
    html += "<div class=card><div class=cl>Meeting Booked</div><div class=cv style='color:#BA7517'>" + str(cli_meeting) + "</div></div>"
    html += "<div class=card><div class=cl>Declined</div><div class=cv style='color:#E24B4A'>" + str(cli_declined) + "</div></div>"
    html += "</div>"
    html += "<div class=slbl>Pipeline Funnel</div>"
    html += "<div class=funnel>" + cli_funnel + "</div>"
    html += "<div class=footer>Auto-refreshed daily 11:00 Prague &middot; " + DASHBOARD_URL + "</div>"
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
