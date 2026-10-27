"""The public documents say what the code does: line width, the redirect anchors, the default models, the roadmap."""

import json
import re
from pathlib import Path

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
