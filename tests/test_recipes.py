import os
import sys
from pathlib import Path
from typing import Any

import pytest

from lastcall.environment import ConfigError
from lastcall.recipes import front_matter, render, schema
from lastcall.recipes.audit import project_reviewers, reviewers, team
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


# a project's own reviewers

ACCESSIBILITY = "---\nname: accessibility\nprefix: A\ntier: fast\n---\nFind WCAG 2.2 AA failures in the templates.\n"


def own(root: Path, files: dict[str, str]) -> Path:
    folder = root / ".lastcall" / "reviewers"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    return root


def test_a_project_brings_its_own_reviewers(tmp_path: Path) -> None:
    root = own(tmp_path, {"accessibility.md": ACCESSIBILITY, "notes.txt": "not a reviewer"})
    [mine] = project_reviewers(root)
    assert (mine.name, mine.prefix, mine.tier) == ("accessibility", "A", "fast")
    assert mine.focus == "Find WCAG 2.2 AA failures in the templates."
    assert [rv.name for rv in team(root)] == ["bugs", "hygiene", "performance", "security", "accessibility"]
    assert project_reviewers(tmp_path / "elsewhere") == []


def test_settings_choose_the_reviewers(tmp_path: Path) -> None:
    root = own(tmp_path, {"accessibility.md": ACCESSIBILITY})
    assert [rv.name for rv in team(root, ["security", "accessibility", "security"])] == ["security", "accessibility"]
    with pytest.raises(ConfigError, match="names style, and the reviewers are bugs, "):
        team(root, ["security", "style"])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Find things.\n", "`name` takes lowercase"),
        ("---\nname: Docs\nprefix: D\ntier: fast\n---\nx\n", "`name` takes lowercase"),
        ("---\nname: docs\nprefix: d1\ntier: fast\n---\nx\n", "`prefix` takes one to three capital letters"),
        ("---\nname: docs\nprefix: D\ntier: huge\n---\nx\n", "`tier` is one of fast, balanced, deep"),
        ("---\nname: docs\nprefix: D\ntier: fast\n---\n", "focus goes after the front matter"),
        ("---\nname: docs\nprefix: D\n", "closing ---"),
    ],
)
def test_a_wrong_reviewer_stops_the_run(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        project_reviewers(own(tmp_path, {"docs.md": text}))


def test_reviewers_need_their_own_name_and_prefix(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="own name, and two have security"):
        team(own(tmp_path / "a", {"s.md": "---\nname: security\nprefix: Z\ntier: fast\n---\nx\n"}))
    with pytest.raises(ConfigError, match="own prefix, and two have B"):
        team(own(tmp_path / "b", {"s.md": "---\nname: builds\nprefix: B\ntier: fast\n---\nx\n"}))


def test_limits_on_a_projects_reviewers(tmp_path: Path) -> None:
    many = {f"r{i}.md": f"---\nname: r{i}\nprefix: R{chr(65 + i)}\ntier: fast\n---\nx\n" for i in range(5)}
    with pytest.raises(ConfigError, match="has 5 reviewers, and a run takes 4"):
        project_reviewers(own(tmp_path / "many", many))
    with pytest.raises(ConfigError, match="over 8 KB"):
        project_reviewers(own(tmp_path / "big", {"big.md": ACCESSIBILITY + "x" * 9_000}))


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_reviewer_linked_out_of_the_repository_stops_the_run(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.md"
    outside.write_text(ACCESSIBILITY, encoding="utf-8")
    root = own(tmp_path / "repo", {})
    os.symlink(outside, root / ".lastcall" / "reviewers" / "a.md")
    with pytest.raises(ConfigError, match="does not lead to a file of the repository"):
        project_reviewers(root)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_reviewers_folder_linked_out_of_the_repository_stops_the_run(tmp_path: Path) -> None:
    outside = own(tmp_path / "elsewhere", {"a.md": ACCESSIBILITY}) / ".lastcall" / "reviewers"
    root = tmp_path / "repo"
    (root / ".lastcall").mkdir(parents=True)
    os.symlink(outside, root / ".lastcall" / "reviewers")
    with pytest.raises(ConfigError, match="is a link out of the repository"):
        project_reviewers(root)
