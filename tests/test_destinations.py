from typing import Any

import pytest

from lastcall.destinations import FormError, apprise_url, random_topic, telegram_chats
from lastcall.notify import describe

BOT = "123456789:AAHabcdefghijklmnopqrstuvwxyz0123456"


@pytest.mark.parametrize(
    ("service", "form", "url"),
    [
        ("telegram", {"token": BOT, "chat": "987654321"}, f"tgram://{BOT}/987654321"),
        ("telegram", {"token": f" {BOT} ", "chat": "-100123"}, f"tgram://{BOT}/-100123"),
        ("ntfy", {"topic": "lastcall-abc"}, "ntfys://lastcall-abc"),
        ("ntfy", {"topic": "alerts", "server": "https://ntfy.example.com"}, "ntfys://ntfy.example.com/alerts"),
        ("ntfy", {"topic": "alerts", "server": "http://10.0.0.5:8080"}, "ntfy://10.0.0.5:8080/alerts"),
        (
            "discord",
            {"webhook": "https://discord.com/api/webhooks/123/abc-DEF_9"},
            "https://discord.com/api/webhooks/123/abc-DEF_9",
        ),
        (
            "slack",
            {"webhook": "https://hooks.slack.com/services/T000/B000/XXXX"},
            "https://hooks.slack.com/services/T000/B000/XXXX",
        ),
        ("email", {"address": "me@gmail.com", "password": "ab cd/ef"}, "mailtos://me:ab%20cd%2Fef@gmail.com"),
        (
            "email",
            {"address": "me@example.com", "password": "x", "to": "you@example.com", "smtp": "smtp.example.com"},
            "mailtos://me:x@example.com?to=you%40example.com&smtp=smtp.example.com",
        ),
        ("apprise", {"url": "pover://user@token"}, "pover://user@token"),
    ],
)
def test_each_service_becomes_an_apprise_url(service: str, form: dict[str, str], url: str) -> None:
    assert apprise_url(service, form) == url
    assert describe(url) is not None  # Apprise understands it


@pytest.mark.parametrize(
    ("service", "form", "field", "key"),
    [
        ("telegram", {"token": "12345", "chat": "1"}, "token", "web.dest.telegram.bad_token"),
        ("telegram", {"token": BOT, "chat": "Ana"}, "chat", "web.dest.telegram.bad_chat"),
        ("telegram", {"token": BOT}, "chat", "web.dest.required"),
        ("ntfy", {"topic": "a b"}, "topic", "web.dest.ntfy.bad_topic"),
        ("ntfy", {"topic": "ok", "server": "ftp://x"}, "server", "web.dest.bad_server"),
        ("discord", {"webhook": "https://evil.example/api/webhooks/1/x"}, "webhook", "web.dest.discord.bad"),
        ("slack", {"webhook": "https://hooks.slack.com.evil/services/x"}, "webhook", "web.dest.slack.bad"),
        ("email", {"address": "nobody", "password": "x"}, "address", "web.dest.email.bad"),
        (
            "email",
            {"address": "me@example.com", "password": "x", "smtp": "smtp example"},
            "smtp",
            "web.dest.bad_smtp",
        ),
        ("apprise", {"url": "nonsense"}, "url", "web.notify.bad_url"),
        ("pigeon", {}, "service", "web.error.form"),
    ],
)
def test_what_is_wrong_is_said_per_field(service: str, form: dict[str, str], field: str, key: str) -> None:
    with pytest.raises(FormError) as wrong:
        apprise_url(service, form)
    assert (wrong.value.field, wrong.value.key) == (field, key)


def test_random_topics() -> None:
    first, second = random_topic(), random_topic()
    assert first != second and first.startswith("lastcall-") and len(first) == 25


def test_telegram_chats() -> None:
    updates = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {"chat": {"id": 1, "type": "private", "first_name": "Ana", "last_name": "B"}},
            },
            {
                "update_id": 2,
                "message": {"chat": {"id": 1, "type": "private", "first_name": "Ana", "last_name": "B"}},
            },
            {"update_id": 3, "my_chat_member": {"chat": {"id": -100, "type": "group", "title": "Team"}}},
            {"update_id": 4, "edited_message": {"chat": {"id": 7, "type": "private", "username": "cid"}}},
            {"update_id": 5, "poll": {"id": "x"}},
        ],
    }
    import json

    def fetch(request: Any) -> tuple[int, dict[str, str], bytes]:
        return 200, {}, json.dumps(updates).encode()

    assert telegram_chats(BOT, fetch) == [("1", "Ana B (private)"), ("-100", "Team (group)"), ("7", "@cid (private)")]

    def webhook(request: Any) -> tuple[int, dict[str, str], bytes]:
        return 409, {}, b'{"ok": false}'

    with pytest.raises(FormError) as wrong:
        telegram_chats(BOT, webhook)
    assert wrong.value.key == "web.dest.telegram.webhook"

    def offline(request: Any) -> tuple[int, dict[str, str], bytes]:
        raise OSError("no route to host")

    with pytest.raises(FormError) as wrong:
        telegram_chats(BOT, offline)
    assert (
        wrong.value.key == "web.dest.telegram.unreachable" and str(wrong.value.values["error"]) == "no route to host"
    )
