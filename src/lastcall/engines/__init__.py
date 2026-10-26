"""Engines available out of the box. The scripted engine takes a script, so it is built directly in tests."""

from __future__ import annotations

from collections.abc import Callable

from lastcall.engines.base import Engine
from lastcall.engines.claude_code import ClaudeCode
from lastcall.engines.codex import Codex
from lastcall.engines.opencode import OpenCode

BUILTIN: dict[str, Callable[[], Engine]] = {"claude-code": ClaudeCode, "codex": Codex, "opencode": OpenCode}


def get(name: str) -> Engine:
    try:
        return BUILTIN[name]()
    except KeyError:
        raise ValueError(f"unknown engine {name!r}; available: {', '.join(sorted(BUILTIN))}") from None
