import string

from lastcall.i18n import MESSAGES, code, number, plural, text
from lastcall.recipes.audit import AuditReport, issue_body, issue_title, pr_footer
from lastcall.verify import describe


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def test_catalogs_say_the_same_things() -> None:
    english = MESSAGES["en"]
    for lang, catalog in MESSAGES.items():
        assert set(catalog) == set(english), lang
        for key, template in catalog.items():
            assert fields(template) == fields(english[key]), f"{lang}:{key}"


def test_language_names() -> None:
    assert code("Spanish") == code("español") == code("es") == "es"
    assert code(" English ") == "en"
    assert code("Klingon") == "en"  # unknown languages fall back to English
    assert text("Klingon", "issue.one") == "decision"


def test_published_texts_follow_the_run_language() -> None:
    report = AuditReport("ne0x86/autopost", "a2051bd", "claude-code", "Spanish")
    assert issue_title(report, 1) == "lastcall: 1 decisión en autopost (auditoría de a2051bd)"
    assert issue_title(report, 2).startswith("lastcall: 2 decisiones en autopost")
    checked = describe("tests", "7 passed in 0.13s", "Spanish")
    assert checked == "tests repetidos en un contenedor limpio: `7 passed in 0.13s`"
    footer = pr_footer(report, ["B1", "B3"], "opus", "high", checked)
    assert footer.startswith("lastcall: auditoría de `a2051bd` con claude-code; hallazgos B1, B3;")
    assert footer.endswith("escaneo de secretos: limpio.")
    finding = {"title": "T", "file": "a.py", "lines": "1", "why": "w", "fix": "f"}
    body = issue_body(report, [{"id": "B4", "severity": "low", "reason": "r"}], {"B4": finding})
    assert body.startswith("Hallazgos válidos que lastcall no ha convertido en pull request")
    assert "*Arreglo propuesto:* f" in body and "*Por qué necesita tu decisión:* r" in body


def test_numbers_and_counts() -> None:
    assert (number("en", 1.5), number("es", 1.5), number("es", 62.04)) == ("1.5", "1,5", "62")
    assert plural("Spanish", "web.time.days", 1) == "1 día"
    assert plural("Spanish", "web.time.days", 3) == "3 días"
    keys = set(MESSAGES["en"])
    for key in keys:  # a key with a count has both forms
        if key.endswith(".one"):
            assert key.removesuffix(".one") + ".other" in keys, key
