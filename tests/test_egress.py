from pathlib import Path

import pytest

from lastcall import egress
from lastcall.engines import BUILTIN


def test_patterns() -> None:
    assert egress.pattern("api.openai.com") == r"^api\.openai\.com$"
    assert egress.pattern("*.chatgpt.com") == r"^(.+\.)?chatgpt\.com$"
    with pytest.raises(ValueError, match="not a domain"):
        egress.pattern("example.com$|.*")


def test_default_filter_matches_the_builtin_engines() -> None:
    """egress/filter is the allowlist for the built-in engines and every package registry. Regenerate it when they
    change: python -c "from lastcall import egress, engines; print(egress.render([f() for f in
    engines.BUILTIN.values()], egress.REGISTRIES), end='')" > egress/filter"""
    expected = egress.render([make() for make in BUILTIN.values()], egress.REGISTRIES)
    assert (Path(__file__).parents[1] / "egress" / "filter").read_text(encoding="utf-8") == expected
