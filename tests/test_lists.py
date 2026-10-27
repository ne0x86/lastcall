"""Long lists in the web UI. Settings shows the containers of the host and the sources in Loki in three parts, so that
dozens of them stay short: what lastcall reads now, what it suggests, and the rest in a fold with a filter by name and
one field to give each a project. The Projects page gets a search by name, filters with their counts, and, with many
projects, a Run now that starts folded."""

import html
import re
from pathlib import Path
from typing import Any

from lastcall import logs, loki
from lastcall.recipes.audit import AuditReport
from lastcall.recipes.improve import ImproveReport
from lastcall.web import project_views
from test_loki import FakeLoki
from test_projects import FakeGitHub
from test_web import NOW, OTHERS, SITE, Web, help_buttons_inside_a_paragraph, suggest_many

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ("acme/shop", "acme/blog", "acme/api", "acme/Worker")


# the containers of a host

NOT_MATCHED = "redis:7"


def box(name: str, image: str = NOT_MATCHED, compose: str = "", label: str = "") -> dict[str, str]:
    return {"name": name, "image": image, "state": "running", "compose_project": compose, "label": label}


def host() -> list[dict[str, str]]:
    """A host with dozens of containers: lastcall's own, one with a label, three that match a project, and 30 more."""
    return [
        box("lastcall-lastcall-1", "ghcr.io/ne0x86/lastcall-service:0.7.4", "lastcall"),
        box("lastcall-supervisor-1", "ghcr.io/ne0x86/lastcall-supervisor:0.7.4", "lastcall"),
        box("proxy-1", "tinyproxy", "lastcall"),  # in lastcall's Compose project, whatever its name
        box("blog", "nginx:1.27", label="ACME/blog"),
        box("shop-web-1", "ghcr.io/acme/shop:1.2", "shop"),
        box("shop-worker-1", "python:3.12", "shop"),
        box("api-1", "ghcr.io/acme/api:3"),
        *(box(f"cache-{n:02d}") for n in range(1, 31)),
    ]


def settings_web(tmp_path: Path, listed: list[dict[str, str]] | None = None, **options: Any) -> Web:
    """A signed-in web UI whose host lists `listed`, with the four projects."""
    found = host() if listed is None else listed
    web = Web(tmp_path, containers=lambda: found, **options)
    web.set_up()
    for repo in PROJECTS:
        web.store.add_project(repo)
    return web


def section(page: str, name: str) -> str:
    """The card of a section of the Settings page: "logs" or "loki"."""
    return page.split(f'id="{name}">')[1].split("</section>")[0]


def part(page: str, kind: str, which: str) -> str:
    """One of the three parts of a list in the settings: "read", "suggested" or "others"; "" when it is not there."""
    body = section(page, kind).split("Or with a label in the compose file")[0]  # what follows the lists is not theirs
    marks = [
        ("read", f'id="{kind}-read-title"'),
        ("suggested", f'id="{kind}-suggested-title"'),
        ("others", '<details class="fold others-fold"'),
    ]
    found = [(name, body.index(mark)) for name, mark in marks if mark in body]
    for number, (name, start) in enumerate(found):
        if name == which:
            return body[start : found[number + 1][1] if number + 1 < len(found) else len(body)]
    return ""


def names(piece: str) -> list[str]:
    """The containers or sources that a part lists: those with a row or a tile of their own."""
    found = re.findall(r'<strong class="mono">([^<]+)</strong>|class="suggestion-name">([^<]+)</label>', piece)
    return [row or tile for row, tile in found]


def hidden(form: str) -> dict[str, str]:
    """What a form sends without the owner touching it: its hidden fields."""
    found = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)
    return {name: html.unescape(value) for name, value in found}


def row_of(form: dict[str, str], name: str) -> str:
    """The number of the row that a form of the page gives to a container or a source."""
    return next(key for key, value in form.items() if value == name).removeprefix("name-")


def form_of(piece: str, css: str) -> str:
    found = re.search(rf'<form method="post" action="[^"]+" class="{css}">(.*?)</form>', piece, re.S)
    assert found, css
    return found.group(1)


def get(web: Web, path: str = "/settings", lang: str = "en") -> str:
    return web.client.get(path, headers={"Accept-Language": lang}).get_data(as_text=True)


def post(web: Web, path: str, **fields: Any) -> Any:
    return web.client.post(path, data={"csrf": web.csrf(), **fields}, headers=SITE)


def helps(page: str, topic: str) -> int:
    """How many "?" buttons open the bubble of `topic` (its close button has a popovertarget too)."""
    return len(re.findall(rf'popovertarget="help-projects-{topic}"(?! popovertargetaction)', page))


def clean(page: str) -> None:
    """What the content security policy would block, and a help button where it must not be."""
    assert " style=" not in page and "<script" not in page and not re.search(r"\son[a-z]+=", page)
    assert help_buttons_inside_a_paragraph(page) == []


def test_container_logs_come_in_three_parts_and_never_list_lastcalls_own(tmp_path: Path) -> None:
    web = settings_web(tmp_path)
    logs.choose(web.store, {"cache-01": "ACME/Api", "cache-02": ""})  # chosen, in another case; and a no
    page = get(web)
    card = section(page, "logs")
    for own in ("lastcall-lastcall-1", "lastcall-supervisor-1", "proxy-1"):
        assert own not in page  # not by its name, not by its Compose project
    read, suggested, others = (part(page, "logs", which) for which in ("read", "suggested", "others"))
    # read now: the label and the choice, with how each was chosen; only a chosen one can stop being read
    assert names(read) == ["blog", "cache-01"] and ">Read now</h4>" in read
    assert '<span class="mono">acme/blog</span> <span class="muted">· its label decides</span>' in read
    assert '<span class="mono">acme/api</span> <span class="muted">· chosen here</span>' in read
    assert read.count('class="row-actions"') == 1 and 'aria-label="Stop reading cache-01"' in read
    assert "Stop reading blog" not in read
    # suggested: a checkbox with the container, and the project that fits
    assert names(suggested) == ["shop-web-1", "shop-worker-1", "api-1"]
    assert suggested.count("for acme/shop") == 2 and suggested.count("for acme/api") == 1
    assert re.findall(r'type="checkbox" name="tick" value="(\d)"', suggested) == ["1", "2", "3"]
    # the buttons for many at once come first; only "Read the ticked" stays in the bar at the bottom of the screen
    top, bar = suggested.split('<div class="pick-all">')[1], suggested.split('<div class="pick-bar">')[1]
    assert 'value="suggested"' in top.split("</div>")[0] and 'value="skip"' in top.split("</div>")[0]
    assert re.findall(r'name="pick" value="(\w+)"', bar.split("</div>")[0]) == ["selected"]
    assert ">Read the 3 suggested</button>" in suggested and ">Read the ticked</button>" in suggested
    # the rest, in a fold that says how many: 29 caches (cache-01 is read), with the one that was said no to
    assert "<summary>29 other containers</summary>" in others and '<details class="fold others-fold">' in others
    assert names(others) == [f"cache-{n:02d}" for n in range(2, 31)]
    assert others.count('list="logs-projects"') == 29 and 'name="logs_q"' in others  # many: a filter by name
    # one list of projects for the whole section, not one in each row
    assert card.count("<option") == len(PROJECTS) and "<select" not in card
    assert card.count('<datalist id="logs-projects">') == 1
    clean(page)
    # nothing is read until the owner chooses it: a suggestion is no choice
    listed = host()
    assert logs.names_for("acme/shop", listed, logs.chosen(web.store)) == []
    assert logs.names_for("acme/blog", listed, logs.chosen(web.store)) == ["blog"]
    assert logs.names_for("acme/api", listed, logs.chosen(web.store)) == ["cache-01"]
    spanish = get(web, lang="es")
    assert ">Se lee ahora</h4>" in spanish and ">Sugeridos</h4>" in spanish
    assert "<summary>29 otros contenedores</summary>" in spanish and "· lo decide su etiqueta" in spanish
    assert ">Leer los 3 sugeridos</button>" in spanish and ">Dejar de leer</button>" in spanish


def test_the_parts_that_have_nothing_to_show_are_short(tmp_path: Path) -> None:
    web = settings_web(tmp_path, [box("cache-01"), box("cache-02")])
    page = get(web)
    assert "Nothing is read yet." in part(page, "logs", "read")
    assert part(page, "logs", "suggested") == "" and "<summary>2 other containers</summary>" in page
    assert 'name="logs_q"' not in page  # two: no filter to look for them
    nothing = settings_web(tmp_path / "none", [box("lastcall-lastcall-1", compose="lastcall")])
    assert "No containers on this machine besides lastcall&#39;s own." in get(nothing)
    assert 'class="parts"' not in get(nothing)
    only = settings_web(tmp_path / "one", [box("shop-web-1", "ghcr.io/acme/shop")])
    assert ">Read the suggested one</button>" in get(only)
    assert '<details class="fold others-fold"' not in get(only)  # none left over: no fold


def test_the_ticked_suggestions_are_read_or_all_of_them_for_the_project_that_the_page_showed(tmp_path: Path) -> None:
    web = settings_web(tmp_path)
    form = hidden(form_of(part(get(web), "logs", "suggested"), "pick"))
    assert (form["name-1"], form["project-1"], form["name-3"], form["project-3"]) == (
        "shop-web-1",
        "acme/shop",
        "api-1",
        "acme/api",
    )
    response = post(web, "/settings/logs", **{**form, "tick": ["1", "3"], "pick": "selected"})
    assert response.headers["Location"] == "/settings?done=logs#logs"
    assert logs.chosen(web.store) == {"shop-web-1": "acme/shop", "api-1": "acme/api"}
    page = get(web)
    assert names(part(page, "logs", "read")) == ["blog", "shop-web-1", "api-1"]  # read now, and not suggested again
    assert names(part(page, "logs", "suggested")) == ["shop-worker-1"]
    # "all" does not look at what was ticked
    form = hidden(form_of(part(page, "logs", "suggested"), "pick"))
    assert post(web, "/settings/logs", **{**form, "tick": ["9"], "pick": "suggested"}).status_code == 302
    assert logs.chosen(web.store) == {"shop-web-1": "acme/shop", "api-1": "acme/api", "shop-worker-1": "acme/shop"}
    assert 'id="logs-suggested-title"' not in get(web)  # nothing left to suggest: no such part
    assert "Saved. lastcall reads the logs of the containers that have a project." in get(web, "/settings?done=logs")


def test_ticking_nothing_says_so_and_changes_nothing(tmp_path: Path) -> None:
    web = settings_web(tmp_path)
    form = hidden(form_of(part(get(web), "logs", "suggested"), "pick"))
    for tick in ([], ["7"]):  # none, or a row that the form does not have
        response = post(web, "/settings/logs", **{**form, "tick": tick, "pick": "selected"})
        page = response.get_data(as_text=True)
        assert response.status_code == 400 and "Tick at least one container." in section(page, "logs")
        assert 'role="alert"' in section(page, "logs") and logs.chosen(web.store) == {}
    spanish = web.client.post(
        "/settings/logs",
        data={"csrf": web.csrf(), **form, "pick": "selected"},
        headers={**SITE, "Accept-Language": "es"},
    )
    assert "Marca al menos un contenedor." in spanish.get_data(as_text=True)


def test_skipping_a_suggestion_or_stopping_a_container_says_no_so_that_it_is_not_suggested_again(
    tmp_path: Path,
) -> None:
    web = settings_web(tmp_path)
    logs.choose(web.store, {"cache-01": "acme/api"})
    page = get(web)
    form = hidden(form_of(part(page, "logs", "suggested"), "pick"))
    assert post(web, "/settings/logs", **{**form, "tick": ["2"], "pick": "skip"}).status_code == 302
    assert logs.chosen(web.store) == {"cache-01": "acme/api", "shop-worker-1": ""}
    page = get(web)
    assert names(part(page, "logs", "suggested")) == ["shop-web-1", "api-1"]
    assert "shop-worker-1" in names(part(page, "logs", "others")) and "shop-worker-1" not in part(
        page, "logs", "read"
    )
    # Stop reading is a small form of its own: it posts the container and nothing else but the button
    stop = hidden(form_of(part(page, "logs", "read"), "row-actions"))
    assert stop == {"csrf": stop["csrf"], "pick": "stop", "name-1": "cache-01"}
    assert post(web, "/settings/logs", **{k: v for k, v in stop.items() if k != "csrf"}).status_code == 302
    assert logs.chosen(web.store) == {"cache-01": "", "shop-worker-1": ""}
    page = get(web)
    assert names(part(page, "logs", "read")) == ["blog"] and "cache-01" in names(part(page, "logs", "others"))
    assert logs.names_for("acme/api", host(), logs.chosen(web.store)) == []


def test_a_project_typed_for_a_container_is_checked_against_the_projects_whatever_the_case(tmp_path: Path) -> None:
    web = settings_web(tmp_path)
    form = hidden(form_of(part(get(web), "logs", "others"), "others-form"))
    assert form["name-1"] == "cache-01" and form["name-30"] == "cache-30"
    # the page offers the projects once, to type against; the field says which container it is for
    assert '<datalist id="logs-projects">' in get(web) and 'aria-label="Project for cache-01"' in get(web)
    typed = {"project-1": " ACME/blog ", "project-2": "acme/WORKER", "project-3": ""}
    response = post(web, "/settings/logs", **{**form, **typed, "pick": "others"})
    assert response.headers["Location"] == "/settings?done=logs#logs"
    assert logs.chosen(web.store) == {
        "cache-01": "acme/blog",
        "cache-02": "acme/Worker",
    }  # as the Projects page writes them
    # one that is not a project: nothing is saved, the page says which, and keeps what was typed, with the fold open
    before = logs.chosen(web.store)
    wrong = post(
        web, "/settings/logs", **{**form, "project-4": "acme/shp", "project-5": "acme/API", "pick": "others"}
    )
    page = wrong.get_data(as_text=True)
    assert wrong.status_code == 400 and logs.chosen(web.store) == before
    assert "“acme/shp” is not one of your projects." in section(page, "logs")
    fold = part(page, "logs", "others")
    assert fold.startswith('<details class="fold others-fold" open>') and 'value="acme/shp"' in fold
    assert 'value="acme/API"' in fold  # what was typed in the others is back: the owner fixes one
    clean(page)
    spanish = web.client.post(
        "/settings/logs",
        data={"csrf": web.csrf(), **form, "project-4": "acme/shp", "pick": "others"},
        headers={**SITE, "Accept-Language": "es"},
    )
    assert "«acme/shp» no es uno de tus proyectos." in spanish.get_data(as_text=True)
    # what the owner typed is the owner's own: escaped, and given back no longer than a repository's name
    odd = post(web, "/settings/logs", **{**form, "project-6": "<b>x</b>", "project-7": "x" * 300, "pick": "others"})
    page = odd.get_data(as_text=True)
    assert odd.status_code == 400 and "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page
    assert f'value="{"x" * 140}"' in page and "x" * 141 not in page
    clean(page)


def test_the_other_containers_are_filtered_by_name_from_the_address(tmp_path: Path) -> None:
    web = settings_web(tmp_path)
    page = get(web, "/settings?logs_q=CACHE-1&loki_q=zz")  # whatever the case
    fold = part(page, "logs", "others")
    assert fold.startswith('<details class="fold others-fold" open>')  # a filter that is set: no match is hidden
    assert names(fold) == [f"cache-{n}" for n in range(10, 20)]
    assert "10 of 30 match “CACHE-1”." in fold and '<a href="/settings?loki_q=zz#logs">Show all</a>' in fold
    assert '<form method="get" action="/settings#logs" class="filter" role="search">' in fold
    assert 'name="logs_q" type="search" value="CACHE-1"' in fold
    assert '<input type="hidden" name="loki_q" value="zz">' in fold  # the other list's filter stays
    assert "<summary>30 other containers</summary>" in fold  # it says how many there are, not how many match
    # the save form holds only the rows that show, so that saving cannot touch the others
    assert hidden(form_of(fold, "others-form")) == {
        "csrf": hidden(form_of(fold, "others-form"))["csrf"],
        **{f"name-{n + 1}": f"cache-{10 + n}" for n in range(10)},
    }
    nothing = get(web, "/settings?logs_q=zzz")
    assert "0 of 30 match “zzz”." in nothing and 'class="others-form"' not in nothing
    assert 'name="logs_q" type="search" value="zzz"' in nothing  # the filter stays, to be cleared
    trimmed = get(web, "/settings?logs_q=%20%20cache-3%20")
    assert "1 of 30 match “cache-3”." in trimmed and 'value="cache-3"' in trimmed
    long = get(web, "/settings?logs_q=" + "x" * 300)
    assert f'value="{"x" * 100}"' in long
    odd = get(web, "/settings?logs_q=%3Cb%3Ex%3C/b%3E")
    assert "&lt;b&gt;x&lt;/b&gt;" in odd and "<b>x</b>" not in odd
    assert "10 de 30 coinciden con «CACHE-1»." in get(web, "/settings?logs_q=CACHE-1", "es")
    assert "1 of 30 matches" not in get(web)  # no filter, no note


def test_the_filter_shows_from_the_seventh_other_container_or_while_one_is_set(tmp_path: Path) -> None:
    few = settings_web(tmp_path, [box(f"cache-{n}") for n in range(1, 7)])
    assert 'name="logs_q"' not in get(few)  # six
    assert 'name="logs_q" type="search" value="1"' in get(few, "/settings?logs_q=1")  # set: it shows, to be cleared
    seven = settings_web(tmp_path / "seven", [box(f"cache-{n}") for n in range(1, 8)])
    assert 'name="logs_q"' in get(seven)


def test_a_choice_for_a_removed_project_survives_every_save_and_comes_back_with_the_project(tmp_path: Path) -> None:
    web = settings_web(tmp_path)
    logs.choose(web.store, {"cache-01": "acme/gone", "shop-web-1": "acme/shop"})
    note = "It was chosen for acme/gone, which is not a project now: add acme/gone again and it is read again."
    page = get(web)
    assert part(page, "logs", "others").count(note) == 1 and "cache-01" in names(part(page, "logs", "others"))
    assert "cache-01" not in part(page, "logs", "read") and 'value="acme/gone"' not in page
    # saving the others as the page sends them (nothing typed) changes nothing; typing for another one keeps it too
    form = hidden(form_of(part(page, "logs", "others"), "others-form"))
    assert post(web, "/settings/logs", **{**form, "pick": "others"}).status_code == 302
    assert logs.chosen(web.store) == {"cache-01": "acme/gone", "shop-web-1": "acme/shop"}
    row = next(k for k, v in form.items() if v == "cache-05").removeprefix("name-")
    assert post(web, "/settings/logs", **{**form, f"project-{row}": "acme/blog", "pick": "others"}).status_code == 302
    assert logs.chosen(web.store) == {"cache-01": "acme/gone", "cache-05": "acme/blog", "shop-web-1": "acme/shop"}
    # the other forms never touch it either
    assert post(web, "/settings/logs", **{"name-1": "shop-web-1", "pick": "stop"}).status_code == 302
    assert logs.chosen(web.store)["cache-01"] == "acme/gone"
    # the project is back: so is the container, read now, with nothing to say about it
    web.store.add_project("acme/gone")
    page = get(web)
    assert "cache-01" in names(part(page, "logs", "read")) and "is not a project now" not in page
    assert '<span class="mono">acme/gone</span> <span class="muted">· chosen here</span>' in page
    assert logs.names_for("acme/gone", host(), logs.chosen(web.store)) == ["cache-01"]


def test_a_label_wins_over_a_choice_and_may_name_a_project_that_does_not_exist_yet(tmp_path: Path) -> None:
    web = settings_web(tmp_path, [box("blog", label="ACME/blog"), box("site", label="acme/missing")])
    logs.choose(web.store, {"blog": "acme/shop", "site": "acme/shop"})  # the label decides, not this
    page = get(web)
    read = part(page, "logs", "read")
    assert names(read) == ["blog", "site"] and read.count("its label decides") == 2
    assert '<span class="mono">acme/blog</span>' in read and "acme/shop" not in read  # as the Projects page writes it
    assert "Its label names acme/missing, which is not a project: add it on the Projects page and it is read." in read
    assert read.count('class="row-actions"') == 0  # nothing to stop: the label is in the app's compose file
    assert part(page, "logs", "suggested") == "" and "other containers" not in page
    assert logs.names_for("acme/blog", [box("blog", label="ACME/blog")], {"blog": "acme/shop"}) == ["blog"]


def test_the_forms_of_the_container_list_refuse_what_the_page_never_sends(tmp_path: Path) -> None:
    web = settings_web(tmp_path)
    form = hidden(form_of(part(get(web), "logs", "suggested"), "pick"))
    forged: list[dict[str, Any]] = [
        {"name-1": "../etc/passwd", "pick": "stop"},  # not a container's name
        {"name-1": "lastcall-step-abc123", "pick": "stop"},  # what an agent printed: never read
        {
            **form,
            "project-1": "someone/else",
            "tick": ["1"],
            "pick": "selected",
        },  # a suggestion that is not a project
        {"name-1": "cache-01", "project-1": "acme/blog", "name-2": "a b", "pick": "others"},
    ]
    for fields in forged:
        response = post(web, "/settings/logs", **fields)
        assert response.status_code == 400 and "Some values in this form are not valid" in response.get_data(
            as_text=True
        )
        assert logs.chosen(web.store) == {}
    plain = {k: v for k, v in form.items() if k != "csrf"}
    unsigned = web.client.post("/settings/logs", data={**plain, "tick": ["1"], "pick": "selected"}, headers=SITE)
    assert unsigned.status_code == 403  # no CSRF token
    elsewhere = web.client.post(
        "/settings/logs",
        data={"csrf": web.csrf(), **form, "tick": ["1"], "pick": "selected"},
        headers={"Origin": "https://evil.example"},
    )
    assert elsewhere.status_code == 403 and logs.chosen(web.store) == {}
    stranger = web.app.test_client().post("/settings/logs", data={**form, "pick": "suggested"}, headers=SITE)
    assert stranger.status_code == 302 and stranger.headers["Location"] == "/login"
    assert logs.chosen(web.store) == {}


# the sources in Loki

SERIES = [
    {"container": "lastcall-lastcall-1", "compose_project": "lastcall", "host": "vps-1"},  # lastcall's own
    {"container": "shop-web-1", "compose_project": "shop", "host": "vps-1"},
    {"container": "shop-web-1", "compose_project": "shop", "host": "vps-2"},
    {"container": "api-1", "compose_project": "api", "host": "vps-1"},
    {"container": "blog-1", "compose_project": "blog", "host": "vps-3"},
    *({"container": f"job-{n:02d}", "host": "vps-3"} for n in range(1, 11)),
]


def loki_web(tmp_path: Path, series: list[dict[str, str]] | None = None) -> Web:
    """A signed-in web UI connected to a Loki that has `series`, with the four projects."""
    web = Web(tmp_path, loki_fetch=FakeLoki(SERIES if series is None else series))
    web.set_up()
    loki.Loki("http://loki.example.com:3100").save(web.secrets)
    web.store.set_setting(loki.LABEL, "container")
    for repo in PROJECTS:
        web.store.add_project(repo)
    return web


def test_sources_in_loki_come_in_the_same_three_parts_with_their_hosts_and_compose_project(tmp_path: Path) -> None:
    web = loki_web(tmp_path)
    loki.choose(web.store, {"blog-1": "acme/blog"})
    page = get(web)
    card = section(page, "loki")
    assert "lastcall-lastcall-1" not in card  # lastcall's own, by its Compose project and by its name
    read, suggested, others = (part(page, "loki", which) for which in ("read", "suggested", "others"))
    assert names(read) == ["blog-1"] and '<span class="chip">vps-3</span>' in read
    assert "Compose project: blog" in read and 'aria-label="Stop reading blog-1"' in read
    assert names(suggested) == ["api-1", "shop-web-1"]  # Loki's sources come sorted by name
    assert '<span class="chip">vps-1</span> <span class="chip">vps-2</span>' in suggested  # where it ran
    assert "Compose project: shop" in suggested and "for acme/shop" in suggested and "for acme/api" in suggested
    assert "<summary>10 other sources</summary>" in others and names(others) == [f"job-{n:02d}" for n in range(1, 11)]
    assert others.count('list="loki-projects"') == 10 and 'name="loki_q"' in others
    # the label that names the sources stays, and every form says it: the choices belong to it
    assert '<option value="container" selected>container</option>' in card
    for piece in (read, suggested, others):
        assert re.search(r'<input type="hidden" name="label" value="container">', piece)
    assert card.count("<option") == len(PROJECTS) + 3  # the projects, once, and the 3 labels that Loki has
    assert card.count("<select") == 1  # only the label's own: no select of projects in any row
    clean(page)
    spanish = get(web, lang="es")
    assert "<summary>10 otras fuentes</summary>" in spanish and "para acme/shop" in spanish


def test_the_forms_of_the_sources_read_the_ticked_or_all_skip_stop_and_check_the_project(tmp_path: Path) -> None:
    web = loki_web(tmp_path)
    form = hidden(form_of(part(get(web), "loki", "suggested"), "pick"))
    assert form["label"] == "container" and form["name-1"] == "api-1" and form["project-1"] == "acme/api"
    shop = row_of(form, "shop-web-1")
    assert form[f"project-{shop}"] == "acme/shop"
    response = post(web, "/settings/loki/sources", **{**form, "tick": [shop], "pick": "selected"})
    assert response.headers["Location"] == "/settings?done=loki_sources#loki"
    assert (web.store.setting(loki.LABEL), loki.chosen(web.store)) == ("container", {"shop-web-1": "acme/shop"})
    form = hidden(form_of(part(get(web), "loki", "suggested"), "pick"))
    assert post(web, "/settings/loki/sources", **{**form, "pick": "suggested"}).status_code == 302  # api-1 and blog-1
    assert loki.chosen(web.store) == {"api-1": "acme/api", "blog-1": "acme/blog", "shop-web-1": "acme/shop"}
    assert names(part(get(web), "loki", "read")) == ["api-1", "blog-1", "shop-web-1"]
    stop = {k: v for k, v in hidden(form_of(part(get(web), "loki", "read"), "row-actions")).items() if k != "csrf"}
    assert stop["pick"] == "stop" and stop["label"] == "container" and stop["name-1"] == "api-1"
    assert post(web, "/settings/loki/sources", **stop).status_code == 302
    assert loki.chosen(web.store) == {"api-1": "", "blog-1": "acme/blog", "shop-web-1": "acme/shop"}
    # a project typed in the others, in any case
    others = hidden(form_of(part(get(web), "loki", "others"), "others-form"))
    first, second = row_of(others, "job-01"), row_of(others, "job-02")
    typed = {**others, f"project-{first}": "ACME/worker", "pick": "others"}
    assert post(web, "/settings/loki/sources", **typed).status_code == 302
    assert loki.chosen(web.store)["job-01"] == "acme/Worker"
    wrong = post(web, "/settings/loki/sources", **{**others, f"project-{second}": "acme/nope", "pick": "others"})
    page = wrong.get_data(as_text=True)
    assert wrong.status_code == 400 and "“acme/nope” is not one of your projects." in section(page, "loki")
    assert part(page, "loki", "others").startswith('<details class="fold others-fold" open>')
    assert 'value="acme/nope"' in part(page, "loki", "others") and "job-02" not in loki.chosen(web.store)
    none = post(web, "/settings/loki/sources", **{**others, "tick": [], "pick": "selected"})
    assert none.status_code == 400 and "Tick at least one source." in none.get_data(as_text=True)
    # the label that the form says is the one that its choices belong to: another starts again
    assert post(web, "/settings/loki/sources", **{**others, "label": "job", "pick": "others"}).status_code == 302
    assert (web.store.setting(loki.LABEL), loki.chosen(web.store)) == ("job", {})


def test_the_forms_of_the_sources_refuse_what_the_page_never_sends(tmp_path: Path) -> None:
    web = loki_web(tmp_path)
    form = hidden(form_of(part(get(web), "loki", "suggested"), "pick"))
    for bad in (
        {"name-1": "lastcall-step-abc123"},  # a step of lastcall's
        {"project-1": "someone/else", "tick": ["1"], "pick": "selected"},
        {"label": "a b"},
        {"label": ""},
    ):
        response = post(web, "/settings/loki/sources", **{**form, "tick": ["1"], "pick": "selected", **bad})
        assert response.status_code == 400, bad
    assert loki.chosen(web.store) == {}


def test_the_sources_are_filtered_by_name_too(tmp_path: Path) -> None:
    web = loki_web(tmp_path)
    page = get(web, "/settings?loki_q=JOB-0&logs_q=zz")
    fold = part(page, "loki", "others")
    assert fold.startswith('<details class="fold others-fold" open>') and len(names(fold)) == 9
    assert "9 of 10 match “JOB-0”." in fold and '<a href="/settings?logs_q=zz#loki">Show all</a>' in fold
    assert '<input type="hidden" name="logs_q" value="zz">' in fold
    assert 'name="loki_q" type="search" value="JOB-0"' in fold
    assert "9 de 10 coinciden con «JOB-0»." in get(web, "/settings?loki_q=JOB-0", "es")


def test_a_loki_with_nothing_but_lastcalls_own_sources_says_it_saw_none(tmp_path: Path) -> None:
    web = loki_web(tmp_path, [{"container": "lastcall-lastcall-1", "compose_project": "lastcall", "host": "vps-1"}])
    page = get(web)
    assert "Loki saw no streams with this label in the last 7 days." in page and 'class="parts"' not in page


# the Projects page


def projects_web(tmp_path: Path) -> Web:
    """Nine projects, with the GitHub App and an account: three are off, and three wait for the owner (a pull request
    to review, a decision in an issue, ideas to choose in a plan)."""
    web = Web(tmp_path, github=FakeGitHub())
    web.set_up()
    web.store.add_account("claude", "claude-code")
    for name in ("api-gateway", "web", "worker", "billing", "Admin", "reports"):
        web.store.add_project(f"acme/{name}")
    for name in ("api-docs", "legacy", "mobile"):
        web.store.add_project(f"acme/{name}", active=False)
    finding = {"title": "T", "severity": "high", "file": "a.py", "lines": "1", "why": "w", "fix": "f"}
    report = AuditReport("acme/web", "a2051bd", "claude-code", "English")
    report.findings = [{**finding, "id": "B1"}]
    report.verdicts = [{"id": "B1", "verdict": "valid", "decision": "pr"}]
    report.deliveries = [
        {"status": "opened", "url": "https://github.com/acme/web/pull/7", "title": "T", "ids": ["B1"]}
    ]
    web.store.start_run("r-web", "acme/web", "claude", "burst")
    web.store.record_findings("r-web", report)
    web.store.record_deliveries("r-web", report)
    web.store.finish_run("r-web", "ok", "a2051bd")
    report = AuditReport("acme/billing", "b3162ce", "claude-code", "English")
    report.findings = [{**finding, "id": "B2"}]
    report.verdicts = [{"id": "B2", "verdict": "valid", "decision": "issue"}]
    report.issue_url = "https://github.com/acme/billing/issues/3"
    web.store.start_run("r-billing", "acme/billing", "claude", "burst")
    web.store.record_findings("r-billing", report)
    web.store.record_deliveries("r-billing", report)
    web.store.finish_run("r-billing", "ok", "b3162ce")
    plan = ImproveReport("acme/mobile", "abc", "claude-code", "English")
    plan.ideas = [{"id": "I1", "title": "A", "kind": "feature", "change": "c"}]
    plan.verdicts = [{"id": "I1", "verdict": "keep"}]
    plan.plan, plan.issue_url = ["I1"], "https://github.com/acme/mobile/issues/4"
    web.store.record_ideas("r-mobile", plan)
    return web


def cards(page: str) -> list[str]:
    """The projects whose cards the page shows, in order."""
    return re.findall(r'<article class="card project(?: is-off)?" id="project-([^"]+)"', page)


def filters(page: str) -> list[tuple[str, str, str, bool]]:
    """The filter links: where each goes, its name, its count and whether it is marked as the current one."""
    found = re.findall(
        r'<li><a href="([^"]*)"( aria-current="true")?>([^<]+) <span class="count">(\d+)</span></a></li>', page
    )
    return [(html.unescape(href), name, count, bool(current)) for href, current, name, count in found]


def test_the_projects_page_searches_by_name_and_filters_with_counts(tmp_path: Path) -> None:
    web = projects_web(tmp_path)
    page = get(web, "/projects")
    assert len(cards(page)) == 9 and 'role="search"' in page and 'for="search">Search by name</label>' in page
    assert filters(page) == [
        ("/projects#find", "All", "9", True),
        ("/projects?show=active#find", "Active", "6", False),
        ("/projects?show=inactive#find", "Inactive", "3", False),
        ("/projects?show=waiting#find", "Waiting for you", "3", False),
    ]
    assert '<nav class="find-links" aria-label="Show projects">' in page and "Showing" not in page
    assert '<form method="get" action="/projects#find" class="filter" role="search">' in page
    assert 'id="search" name="search" type="search" value=""' in page and ">Search</button>" in page
    # the filters, each with the projects that it leaves
    for show, found in {
        "active": ["api-gateway", "web", "worker", "billing", "Admin", "reports"],
        "inactive": ["api-docs", "legacy", "mobile"],
        "waiting": ["web", "billing", "mobile"],  # a pull request, a decision, ideas
    }.items():
        shown = cards(get(web, f"/projects?show={show}"))
        assert sorted(shown, key=str.lower) == sorted((f"acme--{n}" for n in found), key=str.lower), show
    page = get(web, "/projects?show=waiting")
    assert [f[3] for f in filters(page)] == [False, False, False, True] and "Showing 3 of 9 projects." in page
    # the search looks in the name, whatever the case, and the counts are those of the search
    page = get(web, "/projects?search=API")
    assert sorted(cards(page)) == ["acme--api-docs", "acme--api-gateway"]
    assert [(f[1], f[2]) for f in filters(page)] == [
        ("All", "2"),
        ("Active", "1"),
        ("Inactive", "1"),
        ("Waiting for you", "0"),
    ]
    assert 'name="search" type="search" value="API"' in page and "Showing 2 of 9 projects." in page
    assert cards(get(web, "/projects?search=acme/ADMIN")) == ["acme--Admin"]  # the whole name, owner included
    # search and filter combine, and each link keeps the search
    page = get(web, "/projects?search=api&show=active")
    assert cards(page) == ["acme--api-gateway"] and "Showing 1 of 9 projects." in page
    assert filters(page) == [
        ("/projects?search=api#find", "All", "2", False),
        ("/projects?show=active&search=api#find", "Active", "1", True),
        ("/projects?show=inactive&search=api#find", "Inactive", "1", False),
        ("/projects?show=waiting&search=api#find", "Waiting for you", "0", False),
    ]
    assert '<input type="hidden" name="show" value="active">' in page  # a new search keeps the filter
    # both stay at the cards: the forms and the links go back to the bar, and nothing is a script or a style
    clean(page)
    spanish = get(web, "/projects?show=waiting&search=a", "es")
    assert 'for="search">Buscar por nombre</label>' in spanish and 'aria-label="Mostrar proyectos"' in spanish
    assert "Pendientes de ti" in spanish and "Mostrando 3 de 9 proyectos." in spanish
    assert [f[1] for f in filters(spanish)] == ["Todos", "Activos", "Inactivos", "Pendientes de ti"]


def test_no_project_matching_has_its_own_message_and_the_page_goes_on(tmp_path: Path) -> None:
    web = projects_web(tmp_path)
    page = get(web, "/projects?search=zzz")
    assert cards(page) == [] and "No project matches this search and filter." in page
    assert '<a href="/projects#find">Show all projects</a>' in page
    assert "No projects yet." not in page and 'id="add-title"' in page  # it is not an empty page: the Add part stays
    assert 'name="search" type="search" value="zzz"' in page and "Showing 0 of 9 projects." in page
    empty = get(web, "/projects?show=waiting&search=reports")  # a project that waits for nothing
    assert cards(empty) == [] and "No project matches" in empty and "Showing 0 of 9" in empty
    assert "Ningún proyecto coincide con esta búsqueda y este filtro." in get(web, "/projects?search=zzz", "es")
    assert "No project matches" not in get(web, "/projects")
    none = Web(tmp_path / "none", github=FakeGitHub())
    none.set_up()
    assert "No projects yet." in get(none, "/projects") and "No project matches" not in get(
        none, "/projects?search=a"
    )
    assert 'role="search"' not in get(none, "/projects")


def test_what_waits_for_the_owner_is_what_the_dashboard_lists(tmp_path: Path) -> None:
    web = projects_web(tmp_path)
    assert project_views.waiting_for_the_owner(web.store) == {"acme/web", "acme/billing", "acme/mobile"}
    web.store.close_pr("https://github.com/acme/web/pull/7", merged=True)  # reviewed
    web.store.close_issue("https://github.com/acme/billing/issues/3", completed=True)  # decided
    assert project_views.waiting_for_the_owner(web.store) == {"acme/mobile"}
    assert cards(get(web, "/projects?show=waiting")) == ["acme--mobile"]
    assert [(f[1], f[2]) for f in filters(get(web, "/projects"))][3] == ("Waiting for you", "1")


def test_the_search_and_the_filters_show_from_the_seventh_project_or_while_one_is_set(tmp_path: Path) -> None:
    web = Web(tmp_path, github=FakeGitHub())
    web.set_up()
    for name in "abcdef":
        web.store.add_project(f"acme/{name}")
    page = get(web, "/projects")
    assert len(cards(page)) == 6 and 'id="find"' not in page and "find-links" not in page  # six: no bar
    assert 'id="find"' in get(web, "/projects?search=a") and 'id="find"' in get(web, "/projects?show=active")
    assert 'id="find"' not in get(web, "/projects?show=bogus")  # a value that is none of them is the default
    web.store.add_project("acme/g")
    page = get(web, "/projects")
    assert len(cards(page)) == 7 and 'id="find"' in page and filters(page)[0][3] is True
    odd = get(web, "/projects?show=%3Cb%3E&search=%3Cb%3Ex%3C/b%3E")
    assert "&lt;b&gt;x&lt;/b&gt;" in odd and "<b>x</b>" not in odd and filters(odd)[0][3] is True
    assert f'value="{"x" * 100}"' in get(web, "/projects?search=" + "x" * 300)


def get_form(page: str, action: str) -> str:
    """The inside of the GET form that goes to `action` (a filter or a search)."""
    return page.split(f'<form method="get" action="{action}"')[1].split("</form>")[0]


def test_the_filter_of_the_suggestions_and_the_search_of_the_cards_keep_each_other(tmp_path: Path) -> None:
    web = projects_web(tmp_path)
    # acme/web and acme/worker are projects already: seven suggestions are left, so that their filter shows
    suggest_many(web.store, others=(*OTHERS, "acme/extra", "acme/more"))
    plain = get(web, "/projects")
    assert "hidden" not in get_form(plain, "/projects#find") and "hidden" not in get_form(
        plain, "/projects#suggestions"
    )
    assert '<a href="/projects#suggestions">Show all</a>' not in plain  # no filter, no note
    page = get(web, "/projects?search=api&show=active&q=web")
    # a new search keeps the filter of the suggestions, and the new filter of the suggestions keeps the search and the
    # filter of the cards
    assert '<input type="hidden" name="q" value="web">' in get_form(page, "/projects#find")
    assert '<input type="hidden" name="show" value="active">' in get_form(page, "/projects#find")
    assert (
        '<input type="hidden" name="search" value="api"><input type="hidden" name="show" value="active">'
        in get_form(page, "/projects#suggestions")
    )
    assert filters(page) == [
        ("/projects?search=api&q=web#find", "All", "2", False),
        ("/projects?show=active&search=api&q=web#find", "Active", "1", True),
        ("/projects?show=inactive&search=api&q=web#find", "Inactive", "1", False),
        ("/projects?show=waiting&search=api&q=web#find", "Waiting for you", "0", False),
    ]
    assert '<a href="/projects?search=api&amp;show=active#suggestions">Show all</a>' in page
    only_cards = get(web, "/projects?show=waiting")
    assert '<a href="/projects?show=waiting#find"' in only_cards and "q=" not in "".join(
        f[0] for f in filters(only_cards)
    )
    only_suggestions = get(web, "/projects?q=web")
    assert '<a href="/projects#suggestions">Show all</a>' in only_suggestions
    assert next(f[0] for f in filters(only_suggestions)) == "/projects?q=web#find"


def test_with_more_than_six_projects_each_cards_run_now_starts_folded(tmp_path: Path) -> None:
    web = projects_web(tmp_path)
    page = get(web, "/projects")
    assert page.count('<details class="fold run-fold">') == 9  # closed: no open attribute
    card = page.split('id="project-acme--web"')[1].split("</article>")[0]
    fold = card.split('<details class="fold run-fold">')[1].split("</details>")[0]
    assert fold.startswith("\n        <summary>Run now</summary>")  # its title is what opens it
    assert 'action="/projects/acme/web/audit"' in fold and ">Audit now</button>" in fold  # the form is inside
    assert 'action="/projects/acme/web/improve"' in fold and "<legend>Run now</legend>" not in card
    assert 'aria-label="Run now"' in fold and "Starts a run right away, on the account you choose." in fold
    # the card keeps its Settings fold, after the Run now one
    assert card.index("run-fold") < card.index("<summary>Settings</summary>")
    # a "?" inside a fold repeats, so that each card reaches its help; each bubble is there once
    for bubble in ("run_now", "account", "improve"):
        assert helps(page, bubble) == 9, bubble
        assert page.count(f'id="help-projects-{bubble}" popover') == 1, bubble
    clean(page)
    assert "<summary>Ejecutar ahora</summary>" in get(web, "/projects", "es")
    # an order that waits shows its message, as before, instead of a form to fold
    web.store.add_order("audit", "acme/web", None, NOW)
    page = get(web, "/projects")
    assert page.count('<details class="fold run-fold">') == 8 and "Audit queued: it starts as soon as" in page


def test_with_six_projects_or_fewer_run_now_is_open_as_it_was(tmp_path: Path) -> None:
    web = Web(tmp_path, github=FakeGitHub())
    web.set_up()
    web.store.add_account("claude", "claude-code")
    for name in "abcdef":
        web.store.add_project(f"acme/{name}")
    page = get(web, "/projects")
    assert "run-fold" not in page and page.count("<legend>Run now</legend>") == 6
    assert page.count('<fieldset class="run-now has-help">') == 6
    assert helps(page, "run_now") == 1  # once for the page, on the first card
    clean(page)
    web.store.add_project("acme/g")
    assert get(web, "/projects").count('<details class="fold run-fold">') == 7


# the stylesheet and the documents


def test_the_new_rules_sit_in_their_own_blocks_of_the_stylesheet() -> None:
    css = (ROOT / "src/lastcall/web/static/style.css").read_text(encoding="utf-8")
    projects = css.split("Projects: the page header")[1].split("/* ---------- Sign-in pages")[0]
    for rule in (".find {", ".find-links a[aria-current]", ".find-links .count", ".run-fold > .run-form"):
        assert rule in projects, rule
    rows = css.split("/* ---------- Lists of rows")[1].split("/* ---------- Settings ----------")[0]
    for rule in (".parts {", "ul.reading > li", ".read-main {", "ul.others > li", ".others-form {", ".part-title {"):
        assert rule in rows, rule
    # the rows of a section are flex or grid items that wrap: never wider than a phone's screen
    assert "ul.reading > li { display: flex; flex-wrap: wrap;" in rows
    assert not re.search(
        r"\sstyle=|<style", (ROOT / "src/lastcall/web/templates/_lists.html").read_text(encoding="utf-8")
    )


def test_the_documents_describe_the_lists_that_scale() -> None:
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    design = " ".join((ROOT / "DESIGN.md").read_text(encoding="utf-8").split())
    logs_text = readme.split("**Container logs.**")[1].split("**Logs in Loki.**")[0]
    loki_text = readme.split("**Logs in Loki.**")[1].split("**After the merge.**")[0]
    for text in (logs_text, loki_text):
        assert "Read now" in text and "suggested" in text and "fold" in text
    assert "lastcall's own containers never show" in logs_text
    web_ui = design.split("## 13. Web UI and CLI")[1].split("## 14. Extensibility")[0]
    for words in (
        "`Read now`",
        "`POST /settings/logs`",
        "`POST /settings/loki/sources`",
        "`logs_q`",
        "`loki_q`",
        "`<datalist>`",
        "ignoring case",
        "`#find`",
        "`search`",
        "`show`",
        "starts folded",
    ):
        assert words in web_ui, words
    # the parameters and the anchor that they name exist
    source = (ROOT / "src/lastcall/web/__init__.py").read_text(encoding="utf-8")
    assert all(f'"{name}"' in source for name in ("logs_q", "loki_q", "search", "show"))
    assert 'id="find"' in (ROOT / "src/lastcall/web/templates/projects.html").read_text(encoding="utf-8")
    assert '"/settings/logs"' in source and '"/settings/loki/sources"' in source
