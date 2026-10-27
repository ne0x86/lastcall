"""Errors in the logs of a project's containers: which containers belong to a project (the owner's choice or a label),
what the supervisor lists and reads, and how lastcall groups the lines into kinds of errors, masked."""

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
from lastcall.supervisor import MAX_LOG_SECONDS, Invalid, LogRequest, list_containers, read_logs

T = "2026-10-26T09:52:52.123456789Z "
LATER = "2026-10-26T09:52:53.000000000Z "
FILES = {"shop/pricing.py", "shop/views.py", "src/api/orders.js"}


def kinds(lines: list[str], **container: Any) -> tuple[list[logs.Container], list[logs.Kind]]:
    found = {"name": "shop-web", "status": "running", "restarts": 0, "stderr": [T + line for line in lines]}
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
    quiet = logs.errors([{"name": "db", "status": "exited", "exit_code": 1, "stdout": [], "stderr": []}])
    assert (
        logs.section(*quiet)
        == "Containers: db (exited; exited with code 1).\nNo errors in their logs of the last 24 hours."
    )
    assert logs.section([], []) == ""


# the supervisor: the list of containers, and the logs of those that lastcall names


def test_a_logs_request_names_containers_and_a_time() -> None:
    assert LogRequest.parse({"names": ["shop-web-1"], "since": 3600}) == LogRequest(("shop-web-1",), 3600)
    bad: tuple[dict[str, Any], ...] = (
        {"names": []},
        {"names": ["../etc"]},
        {"names": ["lastcall-step-abc123"]},  # a step's container: what its agent printed
        {"names": ["a"] * 11},
        {"names": ["a"], "since": MAX_LOG_SECONDS + 1},
    )
    for body in bad:
        with pytest.raises(Invalid):
            LogRequest.parse(body)


def test_the_supervisor_lists_the_containers_and_reads_the_named_ones() -> None:
    asked: list[list[str]] = []

    def docker(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        asked.append(argv)
        out = b""
        if argv[0] == "ps":
            rows = [
                {"Names": "shop-web-1", "Image": "ghcr.io/acme/shop:1.2", "State": "running",
                 "Labels": "com.docker.compose.project=shop,com.docker.compose.service=web"},
                {"Names": "blog", "Image": "nginx:1.27", "State": "exited", "Labels": "lastcall.project=acme/blog"},
                {"Names": "lastcall-step-abc123", "Image": "ghcr.io/ne0x86/lastcall-runner", "State": "running"},
            ]  # fmt: skip
            out = "\n".join(json.dumps(r) for r in rows).encode()
        elif argv[0] == "inspect":
            out = json.dumps([{"Name": "/shop-web-1", "RestartCount": 2, "State": {"Status": "running"}}]).encode()
        elif argv[0] == "logs":  # Docker stamped the line on stderr before the end of the traceback on stdout
            traceback = (
                f'{T}Traceback (most recent call last):\n{T}  File "/app/shop/pricing.py", line 3, in average\n'
            )
            traceback += f"{LATER}ZeroDivisionError: division by zero\n"
            return subprocess.CompletedProcess(argv, 0, traceback.encode(), f"{T}ERROR payment failed\n".encode())
        return subprocess.CompletedProcess(argv, 0, out, b"")

    listed = list_containers(docker)["containers"]
    assert listed == [
        {"name": "blog", "image": "nginx:1.27", "state": "exited", "compose_project": "", "compose_service": "",
         "label": "acme/blog"},
        {"name": "shop-web-1", "image": "ghcr.io/acme/shop:1.2", "state": "running", "compose_project": "shop",
         "compose_service": "web", "label": ""},
    ]  # fmt: skip
    found = read_logs(LogRequest(("shop-web-1",), 3600), docker)["containers"]
    assert [c["name"] for c in found] == ["shop-web-1"] and found[0]["restarts"] == 2
    assert asked[-1] == ["logs", "--since", "3600s", "--timestamps", "--tail", "5000", "shop-web-1"]
    assert (len(found[0]["stdout"]), len(found[0]["stderr"])) == (3, 1)  # each stream in its own order
    _, kinds = logs.errors(found, FILES)
    assert {(k.sample, k.frames) for k in kinds} == {
        ("ZeroDivisionError: division by zero", ("shop/pricing.py:3 in average",)),
        ("ERROR payment failed", ()),
    }


# which containers belong to which project


REPOS = ["acme/shop", "acme/blog", "other/shop"]


def container(name: str, image: str = "", compose: str = "", label: str = "") -> dict[str, str]:
    return {"name": name, "image": image, "compose_project": compose, "label": label}


def test_lastcall_suggests_a_project_from_the_image_the_compose_project_or_the_name() -> None:
    assert logs.suggest(container("x", "ghcr.io/acme/shop:1.2"), REPOS) == "acme/shop"  # the image names the repo
    assert logs.suggest(container("x", "registry.local:5000/acme/blog"), REPOS) == "acme/blog"  # a port, not a tag
    assert logs.suggest(container("x", "acme/blog@sha256:abc"), REPOS) == "acme/blog"
    assert logs.suggest(container("x", "postgres:17", compose="blog"), REPOS) == "acme/blog"
    assert logs.suggest(container("blog-worker-1", "python:3.12"), REPOS) == "acme/blog"
    assert logs.suggest(container("shop-web-1", "python:3.12"), REPOS) == ""  # two repos are called shop
    assert logs.suggest(container("redis", "redis:7"), REPOS) == ""
    assert logs.suggest(container("Blog_Worker.1"), REPOS) == "acme/blog"  # hyphens, underscores and dots alike
    assert logs.suggest(container("web_blog_v2"), REPOS) == "acme/blog"  # its name between other words
    assert logs.suggest(container("x", compose="infra_blog"), REPOS) == "acme/blog"
    assert logs.suggest(container("bot_api"), ["acme/api"]) == ""  # too short a name to find between words


def test_a_container_belongs_to_a_project_by_its_label_or_the_owners_choice() -> None:
    listed = [container("a", label="ACME/Shop"), container("b"), container("c", label="acme/blog")]
    choices = {"b": "acme/shop", "c": "acme/shop"}  # a label wins over a choice
    assert logs.names_for("acme/shop", listed, choices) == ["a", "b"]
    assert logs.names_for("acme/blog", listed, choices) == ["c"]


# the pack and the ranking


class Box:
    def __init__(self, listed: list[dict[str, Any]], found: list[dict[str, Any]] | Exception) -> None:
        self.listed, self.found, self.asked = listed, found, list[tuple[list[str], int]]()

    def containers(self) -> list[dict[str, Any]]:
        return self.listed

    def logs(self, names: list[str], since: int) -> list[dict[str, Any]]:
        self.asked.append((list(names), since))
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


def test_the_pack_and_the_ranking_read_the_containers_of_the_project(
    tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]
) -> None:
    from lastcall.service import Service

    clone = make_repo(tmp_path / "clone", {"shop/pricing.py": ""})
    store = Store(tmp_path / "lastcall.db")
    store.add_project("acme/shop")
    found = [{"name": "shop-web", "status": "running", "stderr": [T + "ERROR boom", T + "FATAL out of disk"]}]
    box = Box([container("shop-web", "ghcr.io/acme/shop")], found)
    inst = Instance(cast(Sandbox, box), cast(GitHub, None), store)
    assert production_errors(inst, "acme/shop", clone) == {} and box.asked == []  # a suggestion is not a choice
    logs.choose(store, {"shop-web": "acme/shop"})
    section = production_errors(inst, "acme/shop", clone)[logs.TITLE]
    assert "2 kinds of errors in the last 24 hours" in section and box.asked == [(["shop-web"], 86_400)]
    svc = Service(inst, start=lambda work: None, log=lambda m: None)
    svc.read_signals(0)
    assert svc.errors == {"acme/shop": 2}
    inst = Instance(cast(Sandbox, Box([container("shop-web")], SandboxError("gone"))), cast(GitHub, None), store)
    assert production_errors(inst, "acme/shop", clone) == {}  # nothing read: no section, and the run goes on


def test_the_command_line_chooses_a_containers_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LASTCALL_STATE", str(tmp_path / "state"))
    with pytest.raises(SystemExit):  # only one of the projects
        cli.main(["logs", "set", "shop-web-1", "acme/shop"])
    with pytest.raises(SystemExit):
        cli.main(["logs", "set", "../etc", "-"])
    Store(tmp_path / "state" / "lastcall.db").add_project("acme/shop")
    assert cli.main(["logs", "set", "shop-web-1", "acme/shop"]) == 0
    assert logs.chosen(Store(tmp_path / "state" / "lastcall.db")) == {"shop-web-1": "acme/shop"}
    assert cli.main(["logs", "set", "shop-web-1", "-"]) == 0  # a no, so that lastcall stops suggesting one
    assert logs.chosen(Store(tmp_path / "state" / "lastcall.db")) == {"shop-web-1": ""}


def test_the_settings_page_shows_a_choice_as_its_project_is_written_and_keeps_apart_one_that_is_not_a_project() -> (
    None
):
    repos = ["acme/shop", "acme/Blog"]
    assert logs.shown("acme/shop", repos) == ("acme/shop", "")
    assert logs.shown("ACME/BLOG", repos) == ("acme/Blog", "")  # the command line takes the project in any case
    assert logs.shown("acme/old", repos) == ("", "acme/old")  # the owner removed that project: kept, and shown apart
    assert logs.shown("", repos) == ("", "")  # no choice, or a no


# the settings page's lists: containers in three parts, and what its forms ask


def test_lastcalls_own_containers_are_those_of_its_compose_project_or_named_lastcall() -> None:
    assert logs.is_own(container("lastcall-lastcall-1", compose="lastcall"))
    assert logs.is_own(container("lastcall-supervisor-1"))  # by its name
    assert logs.is_own(container("proxy-1", compose="Lastcall"))  # by its Compose project, in any case
    assert logs.is_own({"name": "LASTCALL-egress-1"})
    assert not logs.is_own(container("lastcall", compose="shop"))  # an app that is called so is not lastcall's
    assert not logs.is_own(container("shop-lastcall-1", compose="shop"))
    assert not logs.is_own({})


def test_containers_come_in_three_parts_and_lastcalls_own_are_in_none() -> None:
    repos = ["acme/shop", "acme/Blog", "acme/api"]
    listed = [
        container("shop-web-1", "ghcr.io/acme/shop:1.2", "shop"),  # a suggestion: it is not a choice
        container("blog", label="ACME/blog"),  # read by its label, as the Projects page writes the project
        container("site", label="acme/missing"),  # a label that names no project: read once it is one
        container("api-1", "x"),  # chosen, in another case
        container("redis", "redis:7"),  # nothing fits
        container("old", "x"),  # chosen for a project that was removed
        container("nope", "ghcr.io/acme/shop"),  # the owner said no to its suggestion
        container("lastcall-lastcall-1", "ghcr.io/ne0x86/lastcall-service:0.7.4", "lastcall"),
        container("proxy-1", compose="lastcall"),
    ]
    choices = {"api-1": "ACME/API", "old": "acme/gone", "nope": "", "blog": "acme/shop"}  # a label wins over a choice
    found = logs.parts(listed, repos, choices)
    assert [(r["name"], r["project"], r["by"], r["unknown"]) for r in found.read if r["by"] == "label"] == [
        ("blog", "acme/Blog", "label", False),
        ("site", "acme/missing", "label", True),
    ]
    assert [(r["name"], r["project"], r["by"]) for r in found.read if r["by"] == "choice"] == [
        ("api-1", "acme/api", "choice")
    ]
    assert [(s["name"], s["suggested"]) for s in found.suggested] == [("shop-web-1", "acme/shop")]
    assert [(o["name"], o["suggested"], o["gone"]) for o in found.others] == [
        ("redis", "", ""),
        ("old", "", "acme/gone"),  # the choice is kept, and shown apart
        ("nope", "", ""),
    ]
    assert found.count == 7 and [o["name"] for o in found.others_named("O")] == ["old", "nope"]
    assert [o["name"] for o in found.others_named("rEd")] == ["redis"] and found.others_named("zzz") == []
    assert found.others_named("") == found.others
    # sources in Loki have no label: the same function sorts them
    sources = [{"name": "shop-web-1", "compose_project": "shop", "hosts": ["vps-1"]}, {"name": "job-1", "hosts": []}]
    assert [s["name"] for s in logs.parts(sources, repos, {}).suggested] == ["shop-web-1"]


def test_a_project_is_the_one_that_the_projects_page_writes_whatever_the_case_typed() -> None:
    repos = ["acme/shop", "acme/Blog"]
    assert (
        logs.project_named("acme/blog", repos) == "acme/Blog"
        and logs.project_named(" ACME/SHOP ", repos) == "acme/shop"
    )
    assert logs.project_named("acme/blo", repos) is None and logs.project_named("", repos) is None
    assert logs.project_named("shop", repos) is None  # the whole name: no guessing


def form_choices(
    form: dict[str, str], ticked: tuple[str, ...] = (), choices: dict[str, str] | None = None
) -> dict[str, str]:
    """What `logs.apply_form` makes of a form for containers that are named by letters and digits only, with the
    projects `acme/shop` and `acme/Blog`."""
    return logs.apply_form(form, ticked, choices or {}, ["acme/shop", "acme/Blog"], lambda name: name.isalnum())


def test_a_form_of_the_settings_page_changes_the_choices_that_its_button_asks_for() -> None:
    rows = {"name-1": "web", "project-1": "acme/shop", "name-2": "worker", "project-2": "acme/blog"}
    kept = {"cache": "acme/shop", "old": "acme/gone"}
    # the ticked ones, or all of them, for the project that the page showed, written as the Projects page writes it
    assert form_choices({**rows, "pick": "selected"}, ("2",), kept) == {**kept, "worker": "acme/Blog"}
    assert form_choices({**rows, "pick": "suggested"}, ("1",), kept) == {
        **kept,
        "web": "acme/shop",
        "worker": "acme/Blog",
    }
    # a no to the ticked ones, or to the row of Stop reading: the project is of no matter
    assert form_choices({**rows, "pick": "skip"}, ("1",), kept) == {**kept, "web": ""}
    assert form_choices({"name-1": "cache", "pick": "stop"}, (), kept) == {"cache": "", "old": "acme/gone"}
    assert kept == {"cache": "acme/shop", "old": "acme/gone"}  # what it was given is not changed
    # the others: a project typed next to a container, in any case; an empty field changes nothing, so that saving
    # never erases a choice that the page shows apart
    typed = {"name-1": "web", "project-1": " ACME/BLOG ", "name-2": "cache", "project-2": "", "name-3": "old"}
    assert form_choices({**typed, "pick": "others"}, (), kept) == {**kept, "web": "acme/Blog"}
    assert form_choices(typed, (), kept) == {**kept, "web": "acme/Blog"}  # a button that says nothing: the same
    assert form_choices({}, (), kept) == kept and form_choices({"pick": "others"}) == {}


def refused(form: dict[str, str], ticked: tuple[str, ...] = ()) -> logs.Refused:
    with pytest.raises(logs.Refused) as raised:
        form_choices(form, ticked, {"cache": "acme/shop"})
    return raised.value


def test_a_form_of_the_settings_page_is_refused_whole_when_part_of_it_is_not_acceptable() -> None:
    rows = {"name-1": "web", "project-1": "acme/shop", "name-2": "worker", "project-2": "acme/blog"}
    assert refused({**rows, "name-3": "../etc", "pick": "suggested"}).reason == "form"  # not a container's name
    assert refused({**rows, "project-2": "acme/other", "pick": "suggested"}).reason == "form"  # forged: not a project
    assert refused({**rows, "pick": "selected"}).reason == "none"  # nothing ticked
    assert refused({**rows, "pick": "selected"}, ("7",)).reason == "none"  # a row that the form does not have
    assert refused({"pick": "stop"}).reason == "none" and refused({"pick": "suggested"}).reason == "none"
    wrong = refused({"name-1": "web", "project-1": "acme/shp", "name-2": "worker", "project-2": "ACME/shop"})
    assert (wrong.reason, wrong.wrong) == ("project", "acme/shp")
    assert wrong.typed == {"web": "acme/shp", "worker": "ACME/shop"}  # what was typed, to give back to the owner
