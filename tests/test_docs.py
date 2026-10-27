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
