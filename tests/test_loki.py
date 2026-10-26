"""Errors in the logs that Grafana Loki keeps: the connection, the sources and their suggestions, what lastcall asks
Loki, and how it puts the pieces of a stream back together. The answers have the shape that Loki 3.7 gives."""

import base64
import io
import json
import re
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from lastcall import cli, logs, loki
from lastcall.github import GitHub
from lastcall.runs import Instance, production_errors
from lastcall.sandbox import Sandbox
from lastcall.secretstore import SecretStore
from lastcall.store import Store

NOW = 1_800_000_000.0  # 2027-01-15 08:00 UTC
LOKI = loki.Loki("http://loki.example.com:3100")
SERIES = [
    {"container": "shop-web-1", "compose_project": "shop", "host": "vps-1", "service_name": "shop-web-1"},
    {"container": "shop-web-1", "compose_project": "shop", "host": "vps-2", "__stream_shard__": "1"},
    {"container": "web_blog_v2", "host": "vps-1", "service_name": "web_blog_v2"},
    {"container": "lastcall-step-abc123", "host": "vps-1"},  # a step of a lastcall whose host Loki also reads
    {"service_name": "unknown_service", "host": "vps-1"},
]


def ns(seconds_before: float) -> str:
    return str(int((NOW - seconds_before) * 10**9))


class FakeLoki:
    """Answers as Loki does, and keeps what lastcall asked."""

    def __init__(self, series: Sequence[dict[str, str]] = (), result: Sequence[dict[str, Any]] = ()) -> None:
        self.series, self.result = list(series), list(result)
        self.asked: list[tuple[str, dict[str, str], Mapping[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> Any:
        parts = urllib.parse.urlsplit(url)
        path, params = parts.path.removeprefix("/loki/api/v1/"), dict(urllib.parse.parse_qsl(parts.query))
        self.asked.append((path, params, headers))
        data: Any = {"resultType": "streams", "result": self.result}
        if path == "labels":
            data = sorted({name for s in self.series for name in s} | {"__stream_shard__"})
        elif path == "series":
            data = self.series
        return {"status": "success", "data": data}


def test_a_connection_logs_in_as_loki_asks(tmp_path: Path) -> None:
    flags = {"Accept": "application/json", "X-Loki-Response-Encoding-Flags": "categorize-labels"}
    assert LOKI.headers() == flags  # a Loki with no login
    cloud = loki.Loki("https://logs-prod-012.grafana.net", "123456", "glc_t0ken")
    assert cloud.headers()["Authorization"] == "Basic " + base64.b64encode(b"123456:glc_t0ken").decode()
    tenant = loki.Loki("http://loki:3100", token="t0ken", tenant="team-a")
    assert tenant.headers() == {**flags, "Authorization": "Bearer t0ken", "X-Scope-OrgID": "team-a"}
    secrets = SecretStore(tmp_path)
    cloud.save(secrets)
    assert loki.Loki.load(secrets) == cloud


def test_loki_says_what_is_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "http://loki.example.com:3100/loki/api/v1/labels"
    answers: list[Any] = [
        urllib.error.HTTPError(url, 401, "Unauthorized", {}, None),  # type: ignore[arg-type]
        urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b"parse error at line 1, col 2")),  # type: ignore[arg-type]
        urllib.error.URLError("Connection refused"),
        b"<html>a proxy's page</html>",
        b'{"status": "error"}',
    ]

    def urlopen(request: Any, timeout: float) -> io.BytesIO:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(loki.LokiError, match="the user or the token is not valid"):
        loki.labels(LOKI, NOW)
    with pytest.raises(loki.LokiError, match="parse error at line 1"):
        loki.labels(LOKI, NOW)
    with pytest.raises(loki.LokiError, match=re.escape("no answer from http://loki.example.com:3100")):
        loki.labels(LOKI, NOW)
    for _ in range(2):
        with pytest.raises(loki.LokiError, match="did not answer as Loki does"):
            loki.labels(LOKI, NOW)


KEPT = [
    "Traceback (most recent call last):",
    '  File "/app/shop/pricing.py", line 3, in average',
    "ZeroDivisionError: division by zero",
    "httpx.ReadTimeout: timed out",
    "KeyboardInterrupt",
    "During handling of the above exception, another exception occurred:",
    "2026-10-26 09:52:53,001 ERROR payment failed",
    'level=error msg="db timeout"',
    '{"level":"error","msg":"db timeout"}',
    "2026/10/26 09:55:00 [error] 29#29: upstream timed out",
    "TypeError: Cannot read properties of undefined (reading 'id')",
    "    at handler (/srv/web/src/api/orders.js:42:17)",
    "\tat com.acme.Shop.pay(Shop.java:88)",
    "panic: runtime error: index out of range",
]
DROPPED = [
    'INFO:     172.18.0.1:52312 - "GET /health HTTP/1.1" 200 OK',  # uvicorn's
    "INFO:     Application startup complete.",
    "Date: Mon Oct 26 09:00:00 CET 2026",
    "level=info msg=ok",
    "    return total / len(prices)",
    "INFO 0 errors in the last batch",
    "Started",
]


def test_loki_keeps_only_the_lines_that_may_be_part_of_an_error() -> None:
    kept = re.compile(loki.FILTER)  # Loki's RE2 reads these as Python does
    assert [line for line in KEPT if not kept.search(line)] == []
    assert [line for line in DROPPED if kept.search(line)] == []
    assert loki.query("container", 'shop "web"') == '{container="shop \\"web\\""} |~ `' + loki.FILTER + "`"


def test_lastcall_lists_the_sources_that_loki_saw(tmp_path: Path) -> None:
    store, fake = Store(tmp_path / "lastcall.db"), FakeLoki(SERIES)
    names = loki.labels(LOKI, NOW, fake)
    assert names == ["compose_project", "container", "host", "service_name"]  # not Loki's own
    assert loki.label_for(store, names) == "container"  # before service_name
    store.set_setting(loki.LABEL, "service_name")
    assert loki.label_for(store, names) == "service_name"  # the label of the owner's choices
    assert loki.label_for(store, ["host"]) == ""
    listed = loki.sources(LOKI, "container", NOW, fake)
    assert listed == [
        {"name": "shop-web-1", "image": "", "compose_project": "shop", "hosts": ["vps-1", "vps-2"]},
        {"name": "web_blog_v2", "image": "", "compose_project": "", "hosts": ["vps-1"]},
    ]
    path, params, _ = fake.asked[-1]
    assert (path, params["match[]"]) == ("series", '{container=~".+"}')
    assert int(params["end"]) - int(params["start"]) == 7 * 86_400 * 10**9
    # its Compose project; its name, between other words
    assert [logs.suggest(s, ["acme/shop", "acme/blog"]) for s in listed] == ["acme/shop", "acme/blog"]
    with pytest.raises(loki.LokiError, match="not a label"):
        loki.sources(LOKI, 'container="x"} or {a', NOW, fake)


def test_reading_a_source_puts_its_stream_together_again(tmp_path: Path) -> None:
    """Loki gives the newest lines first, and the pieces of a stream apart: the shards of a busy one, and, in an
    older Loki, a piece for each level that it detects. Here a traceback crosses two shards."""
    store = Store(tmp_path / "lastcall.db")
    store.set_setting(loki.LABEL, "container")
    loki.choose(store, {"shop-web-1": "acme/shop", "shop-worker-1": "", "blog": "acme/blog"})

    def entry(before: float, line: str) -> list[Any]:
        return [ns(before), line, {"structuredMetadata": {"detected_level": "unknown"}}]

    tags = {"container": "shop-web-1", "host": "vps-1"}
    first = [entry(1, "ZeroDivisionError: division by zero"), entry(3, "Traceback (most recent call last):")]
    second = [entry(2, '  File "/app/shop/pricing.py", line 3, in average')]
    elsewhere = [entry(5, "ERROR payment failed for maria@example.com\nand a second line")]
    result = [
        {"stream": {**tags, "__stream_shard__": "0"}, "values": first},
        {"stream": {**tags, "__stream_shard__": "1"}, "values": second},
        {"stream": {**tags, "host": "vps-2", "detected_level": "error"}, "values": elsewhere},
    ]
    fake = FakeLoki(result=result)
    sources, kinds = loki.read(LOKI, store, "acme/shop", {"shop/pricing.py"}, NOW, fake)
    assert [s.name for s in sources] == ["shop-web-1"]
    assert {(k.sample, k.frames) for k in kinds} == {
        ("ZeroDivisionError: division by zero", ("shop/pricing.py:3 in average",)),
        ("ERROR payment failed for <email>", ()),
    }
    path, params, headers = fake.asked[-1]
    assert (path, params["query"]) == ("query_range", loki.query("container", "shop-web-1"))
    assert (params["limit"], params["direction"]) == ("5000", "backward")
    assert int(params["end"]) - int(params["start"]) == 86_400 * 10**9
    assert headers["X-Loki-Response-Encoding-Flags"] == "categorize-labels"
    text = loki.section(sources, kinds, "container")
    assert text.startswith("Sources in Loki, by their label container: shop-web-1.\n2 kinds of errors in the last 24")
    assert loki.read(LOKI, store, "acme/other", (), NOW, fake) == ([], [])
    assert loki.names_for("ACME/Shop", loki.chosen(store)) == ["shop-web-1"]


def test_the_pack_and_the_ranking_read_the_sources_in_loki(
    tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]
) -> None:
    from lastcall.service import Service

    clone = make_repo(tmp_path / "clone", {"shop/pricing.py": ""})
    store, secrets = Store(tmp_path / "lastcall.db"), SecretStore(tmp_path / "secrets")
    store.add_project("acme/shop")
    lines = [[ns(1), "ERROR boom"], [ns(2), "FATAL out of disk"]]
    fake = FakeLoki(result=[{"stream": {"container": "shop-web-1"}, "values": lines}])
    inst = Instance(cast(Sandbox, None), cast(GitHub, None), store, secrets=secrets, loki_fetch=fake)
    assert production_errors(inst, "acme/shop", clone) == {}  # Loki is not connected
    LOKI.save(secrets)
    assert production_errors(inst, "acme/shop", clone) == {} and fake.asked == []  # and no source is chosen
    store.set_setting(loki.LABEL, "container")
    loki.choose(store, {"shop-web-1": "acme/shop"})
    assert "2 kinds of errors in the last 24 hours" in production_errors(inst, "acme/shop", clone)[loki.TITLE]
    svc = Service(inst, start=lambda work: None, log=lambda m: None)
    svc.read_signals(NOW)
    assert svc.errors == {"acme/shop": 2}

    def down(url: str, headers: Mapping[str, str]) -> Any:
        raise loki.LokiError("no answer from http://loki.example.com:3100")

    inst = Instance(cast(Sandbox, None), cast(GitHub, None), store, secrets=secrets, loki_fetch=down)
    note = "Not read: no answer from http://loki.example.com:3100"
    assert production_errors(inst, "acme/shop", clone) == {loki.TITLE: note}  # and the run goes on


def test_the_command_line_connects_loki_and_chooses_its_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LASTCALL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("LASTCALL_SECRETS", str(tmp_path / "secrets"))
    fake = FakeLoki(SERIES, [{"stream": {"container": "shop-web-1"}, "values": [[ns(1), "ERROR boom"]]}])

    def urlopen(request: Any, timeout: float) -> io.BytesIO:
        return io.BytesIO(json.dumps(fake(request.full_url, dict(request.header_items()))).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert cli.main(["loki", "connect", "--url", "http://loki.example.com:3100/"]) == 0
    assert loki.Loki.load(SecretStore(tmp_path / "secrets")) == LOKI
    store = Store(tmp_path / "state" / "lastcall.db")
    store.add_project("acme/shop")
    assert cli.main(["loki", "list"]) == 0
    assert "- (suggested: acme/shop; `lastcall loki set shop-web-1 acme/shop`)" in capsys.readouterr().out
    with pytest.raises(SystemExit):  # never a step of lastcall's
        cli.main(["loki", "set", "lastcall-step-abc123", "acme/shop"])
    assert cli.main(["loki", "set", "shop-web-1", "acme/shop"]) == 0
    assert (store.setting(loki.LABEL), loki.chosen(store)) == ("container", {"shop-web-1": "acme/shop"})
    assert cli.main(["loki", "show", "acme/shop"]) == 0
    assert "ERROR boom (once, last 2027-01-15 07:59 UTC)" in capsys.readouterr().out
