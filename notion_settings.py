import os
import requests

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
SETTINGS_DB = "e0decafd38e04e059e16fb4d9b356458"

NOTION_HEADERS = {
    "Authorization": "Bearer " + NOTION_TOKEN,
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def chunk_text(text, size=1900):
    text = text or ""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def find_setting_page(key):
    r = requests.post(
        "https://api.notion.com/v1/databases/" + SETTINGS_DB + "/query",
        headers=NOTION_HEADERS,
        json={"filter": {"property": "Key", "rich_text": {"equals": key}}},
    )
    results = r.json().get("results", [])
    return results[0] if results else None


def get_setting(key, default=""):
    page = find_setting_page(key)
    if not page:
        return default
    value_prop = page.get("properties", {}).get("Value", {})
    text = "".join(i.get("plain_text", "") for i in value_prop.get("rich_text", []))
    return text or default


def set_setting(key, value):
    props = {
        "Name": {"title": [{"text": {"content": key}}]},
        "Key": {"rich_text": [{"text": {"content": key}}]},
        "Value": {"rich_text": [{"text": {"content": c}} for c in chunk_text(value)]},
    }
    page = find_setting_page(key)
    if page:
        requests.patch(
            "https://api.notion.com/v1/pages/" + page["id"],
            headers=NOTION_HEADERS,
            json={"properties": props},
        )
    else:
        requests.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={"parent": {"database_id": SETTINGS_DB}, "properties": props},
        )
