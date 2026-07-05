import re
import os
import requests

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
TEMPLATES_DB = "6a7be0bdce01429cba2bdd5f4643667e"

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
    return None


def _chunk_text(text, size=1900):
    text = text or ""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _page_to_template(page):
    return {
        "id": page["id"],
        "name": _get_prop(page, "Name") or "",
        "key": _get_prop(page, "Key") or "",
        "content": _get_prop(page, "Content") or "",
    }


def list_templates():
    templates, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            "https://api.notion.com/v1/databases/" + TEMPLATES_DB + "/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        data = r.json()
        templates.extend(_page_to_template(p) for p in data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return templates


def get_template(template_id):
    r = requests.get("https://api.notion.com/v1/pages/" + template_id, headers=NOTION_HEADERS)
    if r.status_code != 200:
        return None
    return _page_to_template(r.json())


def get_template_by_key(key):
    r = requests.post(
        "https://api.notion.com/v1/databases/" + TEMPLATES_DB + "/query",
        headers=NOTION_HEADERS,
        json={"filter": {"property": "Key", "rich_text": {"equals": key}}},
    )
    results = r.json().get("results", [])
    return _page_to_template(results[0]) if results else None


def create_template(name, content="", key=""):
    props = {
        "Name": {"title": [{"text": {"content": name}}]},
        "Content": {"rich_text": [{"text": {"content": c}} for c in _chunk_text(content)]},
    }
    if key:
        props["Key"] = {"rich_text": [{"text": {"content": key}}]}
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": TEMPLATES_DB}, "properties": props},
    )
    return r.json().get("id")


def update_template(template_id, name=None, content=None):
    props = {}
    if name is not None:
        props["Name"] = {"title": [{"text": {"content": name}}]}
    if content is not None:
        props["Content"] = {"rich_text": [{"text": {"content": c}} for c in _chunk_text(content)]}
    if props:
        requests.patch(
            "https://api.notion.com/v1/pages/" + template_id,
            headers=NOTION_HEADERS,
            json={"properties": props},
        )


def delete_template(template_id):
    requests.patch(
        "https://api.notion.com/v1/pages/" + template_id,
        headers=NOTION_HEADERS,
        json={"archived": True},
    )


def html_to_text(html):
    """Strips HTML tags for feeding template content into Claude prompts."""
    text = re.sub(r"<(br|/p|/div)\s*/?>", "\n", html or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    return text.strip()
