"""Tiers: what a step asks for (fast, balanced, deep), and the model and effort each engine uses for it."""

from __future__ import annotations

import json
from importlib import resources
from typing import Literal

Tier = Literal["fast", "balanced", "deep"]
TIERS: tuple[Tier, ...] = ("fast", "balanced", "deep")


def defaults(engine: str) -> dict[str, tuple[str, str | None]]:
    """tier -> (model, effort), from lastcall/data/tiers.json."""
    data = json.loads(resources.files("lastcall.data").joinpath("tiers.json").read_text(encoding="utf-8"))
    if engine not in data:
        raise ValueError(f"no default tiers for engine {engine!r}")
    return {tier: (str(model), str(effort) if effort else None) for tier, (model, effort) in data[engine].items()}
