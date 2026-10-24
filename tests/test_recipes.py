from typing import Any

import pytest

from lastcall.recipes import front_matter, render, schema
from lastcall.recipes.audit import reviewers
from lastcall.tiers import defaults


def test_front_matter() -> None:
    meta, body = front_matter("---\nname: bugs\n# a comment\nprefix: B\n---\nFind bugs.\n")
    assert meta == {"name": "bugs", "prefix": "B"}
    assert body == "Find bugs."
    assert front_matter("No front matter.\n") == ({}, "No front matter.")
    with pytest.raises(ValueError, match="closing"):
        front_matter("---\nname: x\nbody")
    with pytest.raises(ValueError, match="key: value"):
        front_matter("---\nnot a pair\n---\nbody")


def test_render_never_reads_values_as_placeholders() -> None:
    assert render("a ${x} b", x="${y} $z {w}") == "a ${y} $z {w} b"
    with pytest.raises(KeyError):
        render("${missing}")


def test_builtin_reviewers() -> None:
    team = reviewers()
    assert [(r.name, r.prefix, r.tier) for r in team] == [
        ("bugs", "B", "balanced"),
        ("hygiene", "H", "balanced"),
        ("performance", "P", "balanced"),
        ("security", "S", "balanced"),
    ]
    assert all(r.focus for r in team)


def strict(node: dict[str, Any], where: str) -> None:
    """What Codex structured outputs need: every object closed, every property required, typed enums."""
    if "enum" in node:
        assert node.get("type") == "string", where
    if node.get("type") == "object":
        assert node.get("additionalProperties") is False, where
        assert set(node["required"]) == set(node["properties"]), where
        for key, child in node["properties"].items():
            strict(child, f"{where}.{key}")
    elif node.get("type") == "array":
        strict(node["items"], f"{where}[]")


@pytest.mark.parametrize("name", ["findings", "verdicts"])
def test_schemas_are_strict(name: str) -> None:
    strict(schema(name), name)


def test_default_tiers() -> None:
    assert defaults("claude-code")["deep"] == ("opus", "high")
    assert set(defaults("codex")) == {"fast", "balanced", "deep"}
    with pytest.raises(ValueError, match="no default tiers"):
        defaults("scripted")
