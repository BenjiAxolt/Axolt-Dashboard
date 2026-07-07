import os
import requests

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
CALENDAR_DB = "352ffbdab212449aa5fdce03adbfe1b1"

NOTION_HEADERS = {
    "Authorization": "Bearer " + NOTION_TOKEN,
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def _get_prop(page, name):
    p = page.get("properties", {}).get(name, {})
    t = p.get("type")
    if t == "title":
        return "".join(i.get("plain_text", "") for i in p.get("title", [])).strip()
    if t == "rich_text":
        return "".join(i.get("plain_text", "") for i in p.get("rich_text", [])).strip()
    if t == "date":
        d = p.get("date") or {}
        return d.get("start")
    return None


def _page_to_event(page):
    return {
        "id": page["id"],
        "name": _get_prop(page, "Name") or "",
        "date": _get_prop(page, "Date") or "",
        "description": _get_prop(page, "Description") or "",
    }


def list_events(start_iso, end_iso):
    """start_iso/end_iso: 'YYYY-MM-DD' bounds, inclusive."""
    events, cursor = [], None
    while True:
        body = {
            "page_size": 100,
            "filter": {
                "and": [
                    {"property": "Date", "date": {"on_or_after": start_iso}},
                    {"property": "Date", "date": {"on_or_before": end_iso}},
                ]
            },
            "sorts": [{"property": "Date", "direction": "ascending"}],
        }
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            "https://api.notion.com/v1/databases/" + CALENDAR_DB + "/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        data = r.json()
        events.extend(_page_to_event(p) for p in data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return events


def get_event(event_id):
    r = requests.get("https://api.notion.com/v1/pages/" + event_id, headers=NOTION_HEADERS)
    if r.status_code != 200:
        return None
    return _page_to_event(r.json())


def create_event(name, date_iso, description=""):
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={
            "parent": {"database_id": CALENDAR_DB},
            "properties": {
                "Name": {"title": [{"text": {"content": name}}]},
                "Date": {"date": {"start": date_iso}},
                "Description": {"rich_text": [{"text": {"content": description[:2000]}}]},
            },
        },
    )
    return r.json().get("id")


def delete_event(event_id):
    requests.patch(
        "https://api.notion.com/v1/pages/" + event_id,
        headers=NOTION_HEADERS,
        json={"archived": True},
    )
