"""The public documents say what the code does: line width, the redirect anchors, the default models, the roadmap."""

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
WIDTH = 120


def prose_lines(text: str) -> list[tuple[int, str]]:
    """The lines that wrap: not tables, not code, not the badges, whose addresses cannot be split."""
    found, fence = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence and not line.startswith(("|", "[![")):
            found.append((number, line))
    return found


def test_prose_wraps_at_120_columns() -> None:
    for name, text in (("README.md", README), ("DESIGN.md", DESIGN)):
        long = [f"{name}:{n}" for n, line in prose_lines(text) if len(line) > WIDTH]
        assert not long, long


def test_design_lists_every_anchor_that_a_save_returns_to() -> None:
    source = (ROOT / "src/lastcall/web/__init__.py").read_text(encoding="utf-8")
    anchors = set(re.findall(r'\+ "#([a-z]+)"', source))
    assert {"signals", "logs", "loki", "runs", "notify"} <= anchors, anchors
    missing = [a for a in sorted(anchors) if f"`#{a}`" not in DESIGN]
    assert not missing, missing


def test_design_shows_the_default_models_of_data_tiers_json() -> None:
    defaults = json.loads((ROOT / "src/lastcall/data/tiers.json").read_text(encoding="utf-8"))
    for engine in ("claude-code", "codex"):
        shown = re.search(rf"engine: {engine}\n(?:.*\n)*?\s+tiers: \{{([^}}]*)\}}", DESIGN)
        assert shown, engine
        tiers = dict(pair.strip().split(": ") for pair in shown.group(1).split(","))
        for tier, (model, effort) in ((t, defaults[engine][t]) for t in ("fast", "balanced", "deep")):
            assert tiers[tier] == f"{model}/{effort}", (engine, tier)


def test_every_release_is_in_both_roadmaps() -> None:
    for release in ("v0.6", "v0.7"):
        assert f"**{release}" in README, release
        assert f"**{release}:" in DESIGN, release


def test_the_web_address_and_the_port_are_explained() -> None:
    assert "Web UI on" in README and "LASTCALL_PORT" in README
    assert "macOS" in README and "not been tested" in README
    assert "macOS" in DESIGN and "nothing has run on it" in DESIGN


def test_the_design_lists_every_permission_that_the_manifest_asks_for() -> None:
    from lastcall.github import PERMISSIONS

    onboarding = DESIGN.split("### Onboarding")[1].split("2. **Suggestions.**")[0]
    missing = [name for name in PERMISSIONS if name.replace("_", " ") not in onboarding]
    assert not missing, missing


def test_the_readme_says_what_the_two_after_the_merge_settings_need() -> None:
    after = README.split("**After the merge.**")[1].split("\n## ")[0]
    assert "need the GitHub App to read checks and commit statuses" in after and "older App" in after


def test_the_documents_say_how_the_first_steps_go() -> None:
    steps = " ".join(README.split("**3. Follow the dashboard.**")[1].split("That is all.")[0].split())
    assert "GitHub brings you back to lastcall when you do" in steps
    assert "Tick the ones you want, or add the suggested ones or all of them at once, with a filter" in steps
    onboarding = DESIGN.split("### Onboarding")[1].split("2. **Suggestions.**")[0]
    assert all(word in onboarding for word in ("`setup_url`", "`setup_on_update`", "/github/installed", "`returns`"))
    web_ui = DESIGN.split("## 13. Web UI and CLI")[1].split("## 14. Extensibility")[0]
    assert "`POST /projects/add-many`" in web_ui and "`#suggestions`" in web_ui and "hidden field (`q`)" in web_ui
    # the routes and the anchor that they name exist
    source = (ROOT / "src/lastcall/web/__init__.py").read_text(encoding="utf-8")
    assert '"/github/installed"' in source and '"/projects/add-many"' in source
    assert 'id="suggestions"' in (ROOT / "src/lastcall/web/templates/projects.html").read_text(encoding="utf-8")


def test_the_documents_describe_the_state_chip_and_the_trash_button() -> None:
    steps = " ".join(README.split("**3. Follow the dashboard.**")[1].split("That is all.")[0].split())
    assert "A click on a project's state turns its bursts on or off, and its trash button removes it" in steps
    assert "from lastcall but keeps its history" in steps
    web_ui = " ".join(DESIGN.split("## 13. Web UI and CLI")[1].split("## 14. Extensibility")[0].split())
    for words in (
        "`POST /projects/<owner>/<name>/active`",
        "`POST /projects/<owner>/<name>/remove`",
        "also gives it weight 1",
        "`#project-owner--name`",
        "Nothing else goes: runs, findings, ideas, labelled issues and pull requests are keyed by the repository's",
        "`lastcall project remove <owner/name>`",
    ):
        assert words in web_ui, words
    # the routes and the command that they name exist
    source = (ROOT / "src/lastcall/web/__init__.py").read_text(encoding="utf-8")
    assert '"/projects/<owner>/<name>/active"' in source and '"/projects/<owner>/<name>/remove"' in source
    cli = (ROOT / "src/lastcall/cli.py").read_text(encoding="utf-8")
    assert "lastcall project remove <owner/name>" in cli and "cli project remove <owner/name>" in README


def tiers_json() -> dict[str, Any]:
    found: dict[str, Any] = json.loads((ROOT / "src/lastcall/data/tiers.json").read_text(encoding="utf-8"))
    return found


def test_the_readme_example_of_a_default_is_the_one_in_data_tiers_json() -> None:
    model, effort = tiers_json()["claude-code"]["deep"]
    assert f"such as {model} at `{effort}` effort for Claude Code's *deep*" in " ".join(README.split())


def test_the_documents_say_why_the_efforts_differ_and_name_the_ones_of_data_tiers_json() -> None:
    data = tiers_json()
    efforts = {tier: data["claude-code"][tier][1] for tier in ("fast", "balanced", "deep")}
    assert efforts == {tier: data["codex"][tier][1] for tier in efforts}  # one reason for both engines
    assert len(set(efforts.values())) == 3  # they differ
    readme = " ".join(README.split())
    assert "The efforts differ because the work does" in readme
    assert f"*deep* runs at `{efforts['deep']}`" in readme and f"*balanced* at `{efforts['balanced']}`" in readme
    assert f"at `{efforts['fast']}`." in readme
    section = " ".join(DESIGN.split("### Tiers and models")[1].split("### Engines")[0].split())
    assert "The default efforts differ by tier because the work does" in section
    assert f"so it takes `{efforts['fast']}`" in section and f"at `{efforts['balanced']}`" in section
    assert f"at `{efforts['deep']}`" in section and "where quality matters most" in section


def test_the_documents_say_where_the_suggested_models_come_from() -> None:
    import lastcall.web as site
    from lastcall import catalog

    section = " ".join(DESIGN.split("### Tiers and models")[1].split("### Engines")[0].split())
    for words in (
        "**Suggested models.**",
        "`<datalist>`",
        "the `models` of its engine in the data file",
        "https://models.dev/api.json",
        "`Provider.catalog`",
        "A local model server (Ollama) gets no suggestions",
        "`fable` is left out on purpose",
    ):
        assert words in section, words
    for name in tiers_json()["models"]["claude-code"]:
        assert f"`{name}`" in section, name  # the aliases that the data file lists
    # the numbers that it gives are the code's
    assert (site.CATALOG_TIMEOUT, site.CATALOG_SECONDS, site.CATALOG_RETRY_SECONDS) == (5, 86_400, 600)
    assert "waits 5 seconds at most" in section and "stays in memory for a day" in section
    assert "A failure is kept for 10 minutes" in section
    assert catalog.MAX_MODELS == 1000 and "at most 1000 models" in section
    assert "ask [models.dev](https://models.dev), about once a day, for the names of the models" in " ".join(
        README.split()
    )


def test_the_documents_say_that_only_the_checks_and_the_health_address_take_part_in_the_watch() -> None:
    after = " ".join(README.split("**After the merge.**")[1].split("\n## ")[0].split())
    assert (
        "Nothing else takes part in the watch: container logs, Loki and your error tracker feed the audits" in after
    )
    deployment = " ".join(DESIGN.split("## 11. Delivery, approval and after the merge")[1].split("## 12.")[0].split())
    assert (
        "Errors in production feed the audits since v0.5, and container logs and their state since v0.6" in deployment
    )
