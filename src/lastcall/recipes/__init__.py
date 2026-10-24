"""Recipes: fixed shapes with configurable slots, run with the same guarantees (DESIGN.md, section 9)."""

from __future__ import annotations

import json
import string
from importlib import resources
from typing import Any


def prompt(name: str) -> str:
    return resources.files("lastcall.prompts").joinpath(name).read_text(encoding="utf-8")


def render(template: str, **values: str) -> str:
    """Fill ${name} placeholders. Values go in as they are: they are never read as placeholders."""
    return string.Template(template).substitute(values)


def schema(name: str) -> dict[str, Any]:
    value = json.loads(resources.files("lastcall.schemas").joinpath(f"{name}.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"schema {name} is not a JSON object")
    return value


def front_matter(text: str) -> tuple[dict[str, str], str]:
    """`key: value` lines between two `---` lines at the start, then the body. Enough for reviewers and recipes."""
    if not text.startswith("---\n"):
        return {}, text.strip()
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise ValueError("front matter without its closing ---")
    meta: dict[str, str] = {}
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, colon, value = line.partition(":")
        if not colon:
            raise ValueError(f"front matter lines are `key: value`: {line!r}")
        meta[key.strip()] = value.strip()
    return meta, body.strip()
