"""Tiers: what a step asks for (fast, balanced, deep), and the model and effort each engine uses for it."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, Literal

Tier = Literal["fast", "balanced", "deep"]
TIERS: tuple[Tier, ...] = ("fast", "balanced", "deep")


def defaults(engine: str) -> dict[str, tuple[str, str | None]]:
    """tier -> (model, effort), from lastcall/data/tiers.json."""
    data = _data()
    if engine not in data or engine == "efforts":
        raise ValueError(f"no default tiers for engine {engine!r}")
    return {tier: (str(model), str(effort) if effort else None) for tier, (model, effort) in data[engine].items()}


def efforts(engine: str) -> tuple[str, ...]:
    """The effort levels that an engine's CLI takes, the lowest first; none for an engine that takes none."""
    return tuple(str(level) for level in _data()["efforts"].get(engine, ()))


def _data() -> dict[str, Any]:
    found: dict[str, Any] = json.loads(
        resources.files("lastcall.data").joinpath("tiers.json").read_text(encoding="utf-8")
    )
    return found
