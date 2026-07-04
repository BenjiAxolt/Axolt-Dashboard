import os
import secrets
import string
from datetime import datetime, timedelta, timezone

import requests
from werkzeug.security import generate_password_hash, check_password_hash

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
USERS_DB = "5a8314c2dc1c4b19bde629980bbef5f7"

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
    if t == "checkbox":
        return p.get("checkbox", False)
    if t == "email":
        return p.get("email")
    if t == "date":
        d = p.get("date") or {}
        return d.get("start")
    return None


def _page_to_user(page):
    return {
        "id": page["id"],
        "username": _get_prop(page, "Username"),
        "password_hash": _get_prop(page, "Password Hash"),
        "role": _get_prop(page, "Role") or "User",
        "must_reset": bool(_get_prop(page, "Must Reset Password")),
        "email": _get_prop(page, "Email") or "",
        "reset_token": _get_prop(page, "Reset Token") or "",
        "reset_token_expires": _get_prop(page, "Reset Token Expires"),
    }


def find_user(username):
    r = requests.post(
        "https://api.notion.com/v1/databases/" + USERS_DB + "/query",
        headers=NOTION_HEADERS,
        json={"filter": {"property": "Username", "title": {"equals": username}}},
    )
    results = r.json().get("results", [])
    return _page_to_user(results[0]) if results else None


def find_user_by_email(email):
    r = requests.post(
        "https://api.notion.com/v1/databases/" + USERS_DB + "/query",
        headers=NOTION_HEADERS,
        json={"filter": {"property": "Email", "email": {"equals": email}}},
    )
    results = r.json().get("results", [])
    return _page_to_user(results[0]) if results else None


def find_user_by_reset_token(token):
    r = requests.post(
        "https://api.notion.com/v1/databases/" + USERS_DB + "/query",
        headers=NOTION_HEADERS,
        json={"filter": {"property": "Reset Token", "rich_text": {"equals": token}}},
    )
    results = r.json().get("results", [])
    user = _page_to_user(results[0]) if results else None
    if not user or not user["reset_token_expires"]:
        return None
    expires = datetime.fromisoformat(user["reset_token_expires"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires:
        return None
    return user


def create_reset_token(page_id):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {
            "Reset Token": {"rich_text": [{"text": {"content": token}}]},
            "Reset Token Expires": {"date": {"start": expires}},
        }},
    )
    return token


def clear_reset_token(page_id):
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {
            "Reset Token": {"rich_text": []},
            "Reset Token Expires": {"date": None},
        }},
    )


def list_users():
    users, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            "https://api.notion.com/v1/databases/" + USERS_DB + "/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        data = r.json()
        users.extend(_page_to_user(p) for p in data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return users


def generate_password(length=12):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_user(username, role="User", email=""):
    password = generate_password()
    props = {
        "Username": {"title": [{"text": {"content": username}}]},
        "Password Hash": {"rich_text": [{"text": {"content": generate_password_hash(password)}}]},
        "Role": {"select": {"name": role}},
        "Must Reset Password": {"checkbox": True},
    }
    if email:
        props["Email"] = {"email": email}
    requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": USERS_DB}, "properties": props},
    )
    return password


def set_password(page_id, new_password, must_reset=False):
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"properties": {
            "Password Hash": {"rich_text": [{"text": {"content": generate_password_hash(new_password)}}]},
            "Must Reset Password": {"checkbox": must_reset},
            "Reset Token": {"rich_text": []},
            "Reset Token Expires": {"date": None},
        }},
    )


def delete_user(page_id):
    requests.patch(
        "https://api.notion.com/v1/pages/" + page_id,
        headers=NOTION_HEADERS,
        json={"archived": True},
    )


def verify_password(password, password_hash):
    return check_password_hash(password_hash, password)
