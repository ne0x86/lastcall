"""lastcall against a real Loki: compose.dev.yaml's loki. The test pushes the lines of two containers into it, as
Grafana Alloy would, and reads them back as lastcall does: the sources, and the errors of one of them."""

import json
import os
import time
import urllib.request
from pathlib import Path

import pytest

from lastcall import logs, loki
from lastcall.store import Store

URL = os.environ.get("LASTCALL_LOKI", "")
LINES = [
    "INFO started",
    "Traceback (most recent call last):",
    '  File "/app/shop/pricing.py", line 3, in average',
    "    return total / len(prices)",
    "ZeroDivisionError: division by zero",
    "ERROR payment failed for maria@example.com",
]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="needs the test Loki: docker compose --profile test run --rm it"),
]


def test_lastcall_reads_the_errors_of_a_source_in_a_real_loki(tmp_path: Path) -> None:
    for _ in range(120):  # Loki takes some seconds to be ready
        try:
            with urllib.request.urlopen(URL + "/ready", timeout=2):
                break
        except OSError:
            time.sleep(1)
    now = time.time_ns()
    values = [[str(now - (len(LINES) - n) * 1_000_000), line] for n, line in enumerate(LINES)]
    streams = [
        {"stream": {"container": "shop-web-1", "compose_project": "shop", "host": "it"}, "values": values},
        {"stream": {"container": "redis", "host": "it"}, "values": [[str(now), "INFO ready"]]},
    ]
    push = urllib.request.Request(
        URL + "/loki/api/v1/push", json.dumps({"streams": streams}).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(push, timeout=10) as answer:
        assert answer.status == 204
    connection, store = loki.Loki(URL), Store(tmp_path / "lastcall.db")
    for _ in range(30):  # what Loki takes in reaches its queries in a moment
        names = loki.labels(connection, time.time())
        if "container" in names:
            break
        time.sleep(1)
    assert loki.label_for(store, names) == "container"
    listed = loki.sources(connection, "container", time.time())
    assert [(s["name"], s["compose_project"], s["hosts"]) for s in listed] == [
        ("redis", "", ["it"]),
        ("shop-web-1", "shop", ["it"]),
    ]
    assert logs.suggest(listed[1], ["acme/shop"]) == "acme/shop"
    store.set_setting(loki.LABEL, "container")
    loki.choose(store, {"shop-web-1": "acme/shop"})
    sources, kinds = loki.read(connection, store, "acme/shop", {"shop/pricing.py"}, time.time())
    assert [s.name for s in sources] == ["shop-web-1"]
    assert {(k.sample, k.frames) for k in kinds} == {
        ("ZeroDivisionError: division by zero", ("shop/pricing.py:3 in average",)),
        ("ERROR payment failed for <email>", ()),
    }
    params = {
        "query": loki.query("container", "shop-web-1"),
        "start": str(now - 60 * 10**9),
        "end": str(time.time_ns()),
    }
    kept = [
        line.split(" ", 1)[1]
        for stream in loki.streams(loki.get(connection, "query_range", params))
        for line in stream
    ]
    assert kept == [LINES[1], LINES[2], LINES[4], LINES[5]]  # Loki's own filter left the others out
