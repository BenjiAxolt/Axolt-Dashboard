import os
import requests

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
FLAGS_DB = "6b19ecafd07741f08667c623e4505e13"

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
    if t == "select":
        s = p.get("select") or {}
        return s.get("name")
    return None


def _page_to_flag(page):
    return {
        "id": page["id"],
        "username": _get_prop(page, "Username") or "",
        "type": _get_prop(page, "Type") or "",
        "description": _get_prop(page, "Description") or "",
        "status": _get_prop(page, "Status") or "Open",
        "created_time": page.get("created_time", ""),
    }


def create_flag(username, flag_type, description=""):
    title = flag_type + " — " + (username or "unknown")
    requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={
            "parent": {"database_id": FLAGS_DB},
            "properties": {
                "Name": {"title": [{"text": {"content": title}}]},
                "Username": {"rich_text": [{"text": {"content": username or ""}}]},
                "Type": {"select": {"name": flag_type}},
                "Description": {"rich_text": [{"text": {"content": description}}]},
                "Status": {"select": {"name": "Open"}},
            },
        },
    )


def list_open_flags():
    flags, cursor = [], None
    while True:
        body = {
            "page_size": 100,
            "filter": {"property": "Status", "select": {"equals": "Open"}},
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        }
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            "https://api.notion.com/v1/databases/" + FLAGS_DB + "/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        data = r.json()
        flags.extend(_page_to_flag(p) for p in data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return flags


def resolve_flag(page_id):
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {"Status": {"select": {"name": "Resolved"}}}},
    )
