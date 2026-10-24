"""Notification destinations as the owner knows them: a service and its few fields. lastcall turns them into the
Apprise URL that it keeps in the secret store, so nobody writes an Apprise URL by hand. "Other" still takes any
Apprise URL, for the services not listed here.
"""

from __future__ import annotations

import json
import re
import secrets
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

from lastcall.github import Fetch, urlopen
from lastcall.notify import describe


@dataclass(frozen=True)
class Field:
    name: str
    kind: str = "text"  # the input's type: text, password, url or email
    required: bool = True


SERVICES: dict[str, tuple[Field, ...]] = {
    "telegram": (Field("token", "password"), Field("chat")),
    "ntfy": (Field("topic"), Field("server", "url", required=False)),
    "discord": (Field("webhook", "url"),),
    "slack": (Field("webhook", "url"),),
    "email": (
        Field("address", "email"),
        Field("password", "password"),
        Field("to", "email", required=False),
        Field("smtp", required=False),
    ),
    "apprise": (Field("url"),),
}
NTFY = "https://ntfy.sh"
TELEGRAM_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
TELEGRAM_CHAT = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,}")
TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")
DISCORD = re.compile(r"https://(?:discord|discordapp)\.com/api/webhooks/\d+/[A-Za-z0-9_-]+/?")
SLACK = re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
HOST = re.compile(r"[A-Za-z0-9.-]+(?::\d{1,5})?")


class FormError(ValueError):
    """What the owner must fix: `key` is the catalog key that says it, `field` the field it is about."""

    def __init__(self, field: str, key: str, **values: object) -> None:
        super().__init__(key)
        self.field, self.key, self.values = field, key, values


def apprise_url(service: str, form: Mapping[str, str]) -> str:
    """The Apprise URL of a destination, from its form. Raises FormError for the first field that is wrong."""
    if service not in SERVICES:
        raise FormError("service", "web.error.form")
    values = {f.name: form.get(f.name, "").strip() for f in SERVICES[service]}
    for f in SERVICES[service]:
        if f.required and not values[f.name]:
            raise FormError(f.name, "web.dest.required")
    url = _build(service, values)
    if describe(url) is None:
        raise FormError(SERVICES[service][0].name, "web.notify.bad_url")
    return url


def _build(service: str, v: Mapping[str, str]) -> str:
    match service:
        case "telegram":
            if not TELEGRAM_TOKEN.fullmatch(v["token"]):
                raise FormError("token", "web.dest.telegram.bad_token")
            if not TELEGRAM_CHAT.fullmatch(v["chat"]):
                raise FormError("chat", "web.dest.telegram.bad_chat")
            return f"tgram://{v['token']}/{v['chat']}"
        case "ntfy":
            if not TOPIC.fullmatch(v["topic"]):
                raise FormError("topic", "web.dest.ntfy.bad_topic")
            server = urllib.parse.urlsplit(v["server"] or NTFY)
            if server.scheme not in ("http", "https") or not HOST.fullmatch(server.netloc):
                raise FormError("server", "web.dest.bad_server")
            if server.netloc == "ntfy.sh" and server.scheme == "https":
                return f"ntfys://{v['topic']}"
            return f"{'ntfys' if server.scheme == 'https' else 'ntfy'}://{server.netloc}/{v['topic']}"
        case "discord":
            if not DISCORD.fullmatch(v["webhook"]):
                raise FormError("webhook", "web.dest.discord.bad")
            return v["webhook"]
        case "slack":
            if not SLACK.fullmatch(v["webhook"]):
                raise FormError("webhook", "web.dest.slack.bad")
            return v["webhook"]
        case "email":
            for name in ("address", "to"):
                if v[name] and not EMAIL.fullmatch(v[name]):
                    raise FormError(name, "web.dest.email.bad")
            if v["smtp"] and not HOST.fullmatch(v["smtp"]):
                raise FormError("smtp", "web.dest.bad_smtp")
            user, domain = v["address"].rsplit("@", 1)
            query = {k: v[k] for k in ("to", "smtp") if v[k]}
            secret = urllib.parse.quote(v["password"], safe="")
            url = f"mailtos://{urllib.parse.quote(user, safe='')}:{secret}@{domain}"
            return url + (f"?{urllib.parse.urlencode(query)}" if query else "")
    return v["url"]


def random_topic() -> str:
    """A topic nobody guesses: on a public ntfy server, whoever knows a topic's name can read it."""
    return "lastcall-" + "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(16))


def telegram_chats(token: str, fetch: Fetch = urlopen) -> list[tuple[str, str]]:
    """The chats that wrote to the bot lately, as (chat id, name), from Telegram's getUpdates. It does not mark the
    updates as read."""
    token = token.strip()
    if not TELEGRAM_TOKEN.fullmatch(token):
        raise FormError("token", "web.dest.telegram.bad_token")
    try:
        status, _, raw = fetch(urllib.request.Request(f"https://api.telegram.org/bot{token}/getUpdates"))
    except OSError as e:
        raise FormError("token", "web.dest.telegram.unreachable", error=e) from e
    if status in (401, 403, 404):
        raise FormError("token", "web.dest.telegram.bad_token")
    if status == 409:
        raise FormError("chat", "web.dest.telegram.webhook")
    try:
        updates = json.loads(raw).get("result") or []
    except (ValueError, AttributeError) as e:
        raise FormError("token", "web.dest.telegram.unreachable", error=f"HTTP {status}") from e
    chats: dict[str, str] = {}
    for update in updates:
        for kind in ("message", "edited_message", "channel_post", "edited_channel_post", "my_chat_member"):
            chat = (update.get(kind) or {}).get("chat") or {}
            if "id" in chat:
                person = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
                name = chat.get("title") or person or (f"@{chat['username']}" if chat.get("username") else "")
                chats[str(chat["id"])] = f"{name} ({chat.get('type', 'chat')})" if name else str(chat["id"])
    return list(chats.items())
