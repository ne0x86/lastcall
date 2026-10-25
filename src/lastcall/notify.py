"""Notifications through Apprise: Telegram, ntfy, Discord, Slack, email and many more (DESIGN.md, section 4).

They only inform: the owner acts on GitHub or in the web UI. The owner chooses the destinations (Apprise URLs), the
events and the language in the web UI. The URLs often hold credentials (bot tokens, webhooks), so they live in the
secret store; the events and the language live in the settings. A notification that fails is logged and never stops
lastcall.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from typing import Any

import apprise

from lastcall.i18n import CATALOGS, MESSAGES, plural, text
from lastcall.secretstore import SecretStore
from lastcall.store import Store

SECRET = "notify"
EVENTS = ("pr", "decisions", "merged", "reverted", "failed", "paid", "quota", "burst")
DEFAULT_EVENTS = ("pr", "decisions", "merged", "reverted", "failed", "paid", "quota")
Send = Callable[[Sequence[str], str, str], tuple[bool, str]]  # (delivered everywhere, what Apprise said)


def apprise_send(urls: Sequence[str], title: str, body: str) -> tuple[bool, str]:
    sender = apprise.Apprise()
    for url in urls:
        sender.add(url)
    capture = apprise.LogCapture(level=logging.WARNING, fmt="%(message)s")  # type: ignore[no-untyped-call]
    with capture as said:
        ok = bool(sender.notify(body=body, title=title))
    return ok, said.getvalue().strip()


def describe(url: str) -> str | None:
    """The service and the URL with its secrets hidden, as Apprise shows them; None when Apprise does not know it."""
    plugin = apprise.Apprise.instantiate(url)
    if plugin is None:
        return None
    masked = str(plugin.url(privacy=True))  # type: ignore[no-untyped-call]  # Apprise leaves url() untyped
    return f"{plugin.service_name}: {masked.split('?')[0]}"


class Notifier:
    def __init__(
        self, secrets: SecretStore, store: Store, *, send: Send = apprise_send, log: Callable[[str], None] = print
    ) -> None:
        self.secrets, self.store, self.send, self.log = secrets, store, send, log
        self.lock = threading.Lock()

    # what the owner set

    def urls(self) -> list[str]:
        data = self.secrets.get_json(SECRET) or {}
        return [str(u) for u in data.get("urls") or []]

    def add_url(self, url: str) -> bool:
        """Keep a destination, if Apprise understands it."""
        url = url.strip()
        if not url or describe(url) is None:
            return False
        with self.lock:
            urls = self.urls()
            if url not in urls:
                self.secrets.set_json(SECRET, {"urls": [*urls, url]})
        return True

    def remove_url(self, index: int) -> None:
        with self.lock:
            urls = self.urls()
            if 0 <= index < len(urls):
                del urls[index]
                self.secrets.set_json(SECRET, {"urls": urls})

    def events(self) -> set[str]:
        chosen = self.store.setting("notify_events", ",".join(DEFAULT_EVENTS))
        return {e for e in chosen.split(",") if e in EVENTS}

    def language(self) -> str:
        chosen = self.store.setting("notify_language", "en")
        return chosen if chosen in CATALOGS else "en"

    def choose(self, events: Sequence[str], language: str) -> None:
        self.store.set_setting("notify_events", ",".join(e for e in EVENTS if e in events))
        self.store.set_setting("notify_language", language if language in CATALOGS else "en")

    # sending

    def notify(self, event: str, key: str | None = None, **values: Any) -> bool:
        """Send the notification of an event (with the texts of `key`, by default the event's), if the owner wants
        it. Never raises."""
        if event not in self.events():
            return False
        return self._send(key or event, values)[0]

    def test(self, url: str | None = None) -> tuple[bool, str]:
        """The test notification, to one destination (saved or not yet) or to all of them: whether it got through,
        and what Apprise said when it did not."""
        return self._send("test", {}, [url] if url else None)

    def _send(self, key: str, values: dict[str, Any], urls: list[str] | None = None) -> tuple[bool, str]:
        urls = urls or self.urls()
        if not urls:
            return False, ""
        lang = self.language()
        try:
            title = say(lang, f"notify.{key}.title", values)
            body = say(lang, f"notify.{key}.body", values)
            ok, said = self.send(urls, title, body)
        except Exception as e:  # a notification never stops lastcall
            self.log(f"notification {key}: {e}")
            return False, str(e)
        if not ok:
            self.log(f"notification {key}: not delivered to every destination" + (f": {said}" if said else ""))
        return ok, said


def say(lang: str, key: str, values: dict[str, Any]) -> str:
    if f"{key}.one" in MESSAGES["en"]:
        return plural(lang, key, int(values["count"]), **{k: v for k, v in values.items() if k != "count"})
    return text(lang, key, **values)
