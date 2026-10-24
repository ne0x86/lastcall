from collections.abc import Sequence
from pathlib import Path

import pytest

from lastcall.notify import DEFAULT_EVENTS, SECRET, Notifier, describe
from lastcall.secretstore import SecretStore
from lastcall.store import Store

NTFY = "ntfy://ntfy.sh/lastcall-topic"
TELEGRAM = "tgram://123456789:AAHabcdefghijklmnopqrstuvwxyz/12345"


class Outbox:
    def __init__(self, ok: bool = True) -> None:
        self.sent: list[tuple[list[str], str, str]] = []
        self.ok = ok

    def __call__(self, urls: Sequence[str], title: str, body: str) -> tuple[bool, str]:
        self.sent.append((list(urls), title, body))
        return self.ok, "" if self.ok else "There were not Telegram chat_ids to notify."


@pytest.fixture
def outbox() -> Outbox:
    return Outbox()


@pytest.fixture
def notifier(tmp_path: Path, outbox: Outbox) -> Notifier:
    return Notifier(SecretStore(tmp_path / "secrets"), Store(tmp_path / "lastcall.db"), send=outbox, log=print)


def test_destinations_are_apprise_urls_kept_as_secrets(notifier: Notifier) -> None:
    assert notifier.add_url(f"  {NTFY} ")
    assert notifier.add_url(TELEGRAM)
    assert notifier.add_url(NTFY)  # the same one again: kept once
    assert not notifier.add_url("not a url")
    assert not notifier.add_url("")
    assert notifier.urls() == [NTFY, TELEGRAM]
    assert notifier.secrets.get_json(SECRET) == {"urls": [NTFY, TELEGRAM]}
    assert notifier.store.setting("notify_urls") == ""  # never in the database
    notifier.remove_url(0)
    notifier.remove_url(5)
    assert notifier.urls() == [TELEGRAM]


def test_the_web_ui_shows_destinations_without_their_secrets() -> None:
    shown = describe(TELEGRAM) or ""
    assert shown.startswith("Telegram: tgram://") and "AAHabcdefghijklmnopqrstuvwxyz" not in shown
    assert describe("nonsense://x") is None


def test_events_and_language(notifier: Notifier, outbox: Outbox) -> None:
    assert notifier.events() == set(DEFAULT_EVENTS)
    assert not notifier.notify("pr", repo="acme/api", title="T", url="https://github.com/acme/api/pull/1")
    assert outbox.sent == []  # no destination yet
    notifier.add_url(NTFY)
    notifier.choose(["decisions", "burst", "unknown"], "es")
    assert notifier.events() == {"decisions", "burst"}
    assert not notifier.notify("pr", repo="acme/api", title="T", url="u")  # not chosen
    url = "https://github.com/acme/api/issues/2"
    assert notifier.notify("decisions", repo="acme/api", count=2, url=url)
    assert outbox.sent == [([NTFY], "lastcall: decisiones en acme/api", f"2 hallazgos necesitan tu decisión: {url}")]


def test_an_early_stop_is_a_failed_event_with_its_own_words(notifier: Notifier, outbox: Outbox) -> None:
    notifier.add_url(NTFY)
    notifier.notify("failed", "partial", repo="acme/api", reason="week window at 90% (reserve 10%)")
    assert outbox.sent[-1][1:] == (
        "lastcall: the audit of acme/api stopped early",
        "week window at 90% (reserve 10%)",
    )


def test_a_failing_notification_never_stops_lastcall(tmp_path: Path) -> None:
    logs: list[str] = []

    def broken(urls: Sequence[str], title: str, body: str) -> tuple[bool, str]:
        raise OSError("network down")

    notifier = Notifier(SecretStore(tmp_path / "s"), Store(tmp_path / "db"), send=broken, log=logs.append)
    notifier.add_url(NTFY)
    assert notifier.test() == (False, "network down")
    assert logs == ["notification test: network down"]
    quiet = Notifier(SecretStore(tmp_path / "s"), Store(tmp_path / "db"), send=Outbox(ok=False), log=logs.append)
    assert quiet.test() == (False, "There were not Telegram chat_ids to notify.")
    assert (
        logs[-1]
        == "notification test: not delivered to every destination: There were not Telegram chat_ids to notify."
    )
    assert quiet.test(NTFY) == (False, "There were not Telegram chat_ids to notify.")  # one destination, saved or not
