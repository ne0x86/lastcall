"""The few sentences lastcall writes itself in what it publishes (issues, pull request footers), in the owner's
language. Agents already write findings, titles and bodies in the language of the run. Unknown languages fall back
to English; the web UI will use the same catalogs (DESIGN.md, section 13).
"""

from __future__ import annotations

LANGUAGES = {"en": "en", "english": "en", "inglés": "en", "es": "es", "spanish": "es", "español": "es"}

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "issue.title": "lastcall: {count} {noun} for {project} (audit of {sha})",
        "issue.one": "decision",
        "issue.many": "decisions",
        "issue.intro": (
            "Valid findings that lastcall did not turn into a pull request: they need your decision, or they are too "
            "large for one. Close this issue when you have decided."
        ),
        "issue.fix": "Proposed fix",
        "issue.why": "Why it needs you",
        "issue.footer": "lastcall: audit of `{sha}` with {engine}",
        "pr.footer": (
            "lastcall: audit of `{sha}` with {engine}; findings {ids}; implemented with {model} ({effort}); "
            "{checked}; secret scan: clean."
        ),
        "check.tests": "tests run again in a clean container: `{line}`",
        "check.build": "the project has no tests: a clean container only checked that the code compiles",
        "check.none": "no automatic verification was possible",
    },
    "es": {
        "issue.title": "lastcall: {count} {noun} en {project} (auditoría de {sha})",
        "issue.one": "decisión",
        "issue.many": "decisiones",
        "issue.intro": (
            "Hallazgos válidos que lastcall no ha convertido en pull request: necesitan tu decisión o son demasiado "
            "grandes para uno. Cierra este issue cuando lo hayas decidido."
        ),
        "issue.fix": "Arreglo propuesto",
        "issue.why": "Por qué necesita tu decisión",
        "issue.footer": "lastcall: auditoría de `{sha}` con {engine}",
        "pr.footer": (
            "lastcall: auditoría de `{sha}` con {engine}; hallazgos {ids}; implementado con {model} ({effort}); "
            "{checked}; escaneo de secretos: limpio."
        ),
        "check.tests": "tests repetidos en un contenedor limpio: `{line}`",
        "check.build": "el proyecto no tiene tests: un contenedor limpio solo comprobó que el código compila",
        "check.none": "no fue posible ninguna verificación automática",
    },
}


def code(language: str) -> str:
    """Catalog code for a language name as runs carry it ("Spanish", "es"...); English when unknown."""
    return LANGUAGES.get(language.strip().lower(), "en")


def text(language: str, key: str, **values: object) -> str:
    return MESSAGES[code(language)][key].format(**values)
