"""Errors in the logs of a project's containers: what the supervisor reads, only from the containers labelled for the
project, and how lastcall groups the lines into kinds of errors, masked."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from lastcall import cli, logs
from lastcall.github import GitHub
from lastcall.runs import Instance, production_errors
from lastcall.sandbox import Sandbox, SandboxError
from lastcall.store import Store
from lastcall.supervisor import MAX_LOG_SECONDS, Invalid, LogRequest, read_logs

T = "2026-10-26T09:52:52.123456789Z "
FILES = {"shop/pricing.py", "shop/views.py", "src/api/orders.js"}


def kinds(lines: list[str], **container: Any) -> tuple[list[logs.Container], list[logs.Kind]]:
    found = {"name": "shop-web", "status": "running", "restarts": 0, "lines": [T + line for line in lines]}
    return logs.errors([{**found, **container}], FILES)


def test_a_python_traceback_is_its_exception_at_its_place_in_the_code() -> None:
    traceback = [
        "Traceback (most recent call last):",
        '  File "/app/shop/views.py", line 21, in checkout',
        "    average(prices)",
        '  File "/usr/lib/python3.12/statistics.py", line 9, in mean',
        '  File "/app/shop/pricing.py", line 3, in average',
        "    return total / len(prices)",
        "ZeroDivisionError: division by zero",
    ]
    chained = [
        "Traceback (most recent call last):",
        '  File "/app/shop/pricing.py", line 9, in parse',
        "KeyError: 'amount'",
        "",
        "During handling of the above exception, another exception occurred:",
        "",
        *traceback,
    ]
    _, found = kinds(["INFO starting", *traceback, *chained])
    [kind] = found  # the same exception at the same place, twice
    assert (kind.count, kind.sample) == (2, "ZeroDivisionError: division by zero")
    assert kind.frames == ("shop/views.py:21 in checkout", "shop/pricing.py:3 in average")  # the project's own


def test_lines_with_an_error_level_group_without_their_numbers_and_personal_data() -> None:
    _, found = kinds(
        [
            "2026-10-26 09:52:53,001 ERROR payment 4411 failed for maria@example.com (order 'A-17')",
            "2026-10-26 09:53:10,002 ERROR payment 9932 failed for juan@example.com (order 'B-99')",
            '{"level":"error","msg":"db timeout after 5000ms"}',
            "2026/10/26 09:55:00 [error] 29#29: *1 upstream timed out, client: 10.0.0.12",
            "INFO 0 errors in the last batch",  # the word, not a level: not an error
            "level=info msg=ok",
        ]
    )
    assert [(k.count, k.sample) for k in found] == [
        (2, "ERROR payment 9932 failed for <email> (order 'B-99')"),
        (1, '{"level":"error","msg":"db timeout after 5000ms"}'),
        (1, "[error] 29#29: *1 upstream timed out, client: <ip>"),
    ]


def test_stacks_of_node_and_java_keep_their_frames_innermost_last() -> None:
    _, found = kinds(
        [
            "TypeError: Cannot read properties of undefined (reading 'id')",
            "    at handler (/srv/web/src/api/orders.js:42:17)",
            "    at processTicksAndRejections (node:internal/process/task_queues:95:5)",
            'Exception in thread "main" java.lang.IllegalStateException: closed',
            "\tat com.acme.Shop.pay(Shop.java:88)",
            "\tat com.acme.Main.main(Main.java:12)",
        ]
    )
    assert found[0].frames == ("src/api/orders.js:42 in handler",)
    assert found[1].frames == ("Main.java:12 in com.acme.Main.main", "Shop.java:88 in com.acme.Shop.pay")


def test_the_section_says_how_the_containers_are() -> None:
    containers, found = kinds(["ERROR boom"], restarts=3, oom_killed=True)
    text = logs.section(containers, found)
    assert text.startswith("Containers: shop-web (running; restarted 3 times; the last stop was the kernel killing")
    assert "- [shop-web] ERROR boom (once, last 2026-10-26 09:52 UTC)" in text
    quiet = logs.errors([{"name": "db", "status": "exited", "exit_code": 1, "lines": []}])
    assert (
        logs.section(*quiet)
        == "Containers: db (exited; exited with code 1).\nNo errors in their logs of the last 24 hours."
    )
    assert logs.section([], []) == ""


# the supervisor: only the labelled containers


def test_a_logs_request_names_a_project_and_a_time() -> None:
    assert LogRequest.parse({"project": "acme/shop", "since": 3600}) == LogRequest("acme/shop", 3600)
    for body in ({"project": "acme"}, {"project": "../etc"}, {"project": "acme/shop", "since": MAX_LOG_SECONDS + 1}):
        with pytest.raises(Invalid):
            LogRequest.parse(body)


def test_the_supervisor_reads_only_the_containers_labelled_for_the_project() -> None:
    asked: list[list[str]] = []
    shop = "a" * 64

    def docker(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        asked.append(argv)
        out, err = b"", b""
        if argv[0] == "ps":
            listed = [
                f"{shop}\tcom.docker.compose.service=web,lastcall.project=ACME/Shop",
                f"{'b' * 64}\tlastcall.project=acme/blog",
                "not-an-id\tlastcall.project=acme/shop",
            ]
            out = "\n".join(listed).encode()
        elif argv[0] == "inspect":
            out = json.dumps([{"Name": "/shop-web", "RestartCount": 2, "State": {"Status": "running"}}]).encode()
        elif argv[0] == "logs":
            out = f'{T}first\n{T}Traceback (most recent call last):\n{T}  File "/app/x.py", line 1, in f\n'.encode()
            err = f"2026-10-26T09:52:51.000000000Z earlier\n{T}KeyError: 'x'\n".encode()
        return subprocess.CompletedProcess(argv, 0, out, err)

    found = read_logs(LogRequest("acme/shop", 3600), docker)["containers"]
    assert [c["name"] for c in found] == ["shop-web"] and found[0][
        "restarts"
    ] == 2  # the label's case does not matter
    assert [argv[0] for argv in asked] == ["ps", "inspect", "logs"]
    assert asked[2] == ["logs", "--since", "3600s", "--timestamps", "--tail", "5000", shop]
    assert [line.split(" ", 1)[1] for line in found[0]["lines"]] == [
        "earlier",
        "first",
        "Traceback (most recent call last):",
        '  File "/app/x.py", line 1, in f',
        "KeyError: 'x'",
    ]  # the two streams merge by time, and lines of the same time keep their order


# the pack, the ranking and the setting


class Box:
    def __init__(self, found: list[dict[str, Any]] | Exception) -> None:
        self.found, self.asked = found, list[tuple[str, int]]()

    def logs(self, project: str, since: int) -> list[dict[str, Any]]:
        self.asked.append((project, since))
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


def test_the_pack_and_the_ranking_read_the_logs_when_the_owner_wants(
    tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]
) -> None:
    from lastcall.service import Service

    clone = make_repo(tmp_path / "clone", {"shop/pricing.py": ""})
    store = Store(tmp_path / "lastcall.db")
    store.add_project("acme/shop")
    found = [{"name": "shop-web", "status": "running", "lines": [T + "ERROR boom", T + "FATAL out of disk"]}]
    box = Box(found)
    inst = Instance(cast(Sandbox, box), cast(GitHub, None), store)
    assert production_errors(inst, "acme/shop", clone) == {} and box.asked == []  # off until the owner turns it on
    store.set_setting(logs.SETTING, "yes")
    section = production_errors(inst, "acme/shop", clone)[logs.TITLE]
    assert "2 kinds of errors in the last 24 hours" in section and box.asked == [("acme/shop", 86_400)]
    svc = Service(inst, start=lambda work: None, log=lambda m: None)
    svc.read_signals(0)
    assert svc.errors == {"acme/shop": 2}
    inst = Instance(cast(Sandbox, Box(SandboxError("no supervisor"))), cast(GitHub, None), store)
    assert production_errors(inst, "acme/shop", clone) == {}  # nothing read: no section, and the run goes on


def test_the_command_line_turns_them_on_and_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LASTCALL_STATE", str(tmp_path / "state"))
    assert cli.main(["logs", "on"]) == 0
    assert Store(tmp_path / "state" / "lastcall.db").setting(logs.SETTING) == "yes"
    assert cli.main(["logs", "off"]) == 0
    assert Store(tmp_path / "state" / "lastcall.db").setting(logs.SETTING) == "no"
