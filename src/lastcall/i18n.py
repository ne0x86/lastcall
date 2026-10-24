"""The texts lastcall writes itself, in the owner's language: the few sentences in what it publishes (issues, pull
request footers) and the web UI. Agents already write findings, titles and bodies in the language of the run.

The catalogs are JSON files in lastcall/locales. Unknown languages fall back to English. A key with a count has two
forms, `<key>.one` and `<key>.other`, which is enough for English and Spanish.
"""

from __future__ import annotations

import json
from importlib import resources

LANGUAGES = {"en": "en", "english": "en", "inglés": "en", "es": "es", "spanish": "es", "español": "es"}
CATALOGS = ("en", "es")

MESSAGES: dict[str, dict[str, str]] = {
    lang: json.loads(resources.files("lastcall.locales").joinpath(f"{lang}.json").read_text(encoding="utf-8"))
    for lang in CATALOGS
}


def code(language: str) -> str:
    """Catalog code for a language name as runs carry it ("Spanish", "es"...); English when unknown."""
    return LANGUAGES.get(language.strip().lower(), "en")


def text(language: str, key: str, **values: object) -> str:
    return MESSAGES[code(language)][key].format(**values)


def plural(language: str, key: str, count: int, **values: object) -> str:
    return text(language, f"{key}.{'one' if count == 1 else 'other'}", count=count, **values)


def number(language: str, value: float) -> str:
    """A number with at most one decimal, with the language's decimal mark: 12, 1.5 or 1,5."""
    rounded = round(value, 1)
    out = f"{rounded:.0f}" if rounded == int(rounded) else f"{rounded:.1f}"
    return out.replace(".", ",") if code(language) == "es" else out
