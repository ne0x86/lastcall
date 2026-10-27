import string

from lastcall.i18n import MESSAGES, code, number, plural, span, text
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


def test_a_span_keeps_its_number_and_unit_together() -> None:
    assert span("en", 46 * 3600) == "46\u00a0h" and span("es", 46 * 3600) == "46\u00a0h"
    assert span("en", 25 * 60) == "25\u00a0min" and span("en", 1) == "1\u00a0min"
    assert (span("en", 3 * 86_400), span("es", 3 * 86_400), span("es", 2 * 86_400)) == (
        "3\u00a0days",
        "3\u00a0días",
        "2\u00a0días",
    )
    assert text("es", "web.time.ago", span=span("es", 46 * 3600)) == "hace 46\u00a0h"
    assert " " not in span("en", 5 * 86_400)


def test_numbers_and_counts() -> None:
    assert (number("en", 1.5), number("es", 1.5), number("es", 62.04)) == ("1.5", "1,5", "62")
    assert plural("Spanish", "web.time.days", 1) == "1 día"
    assert plural("Spanish", "web.time.days", 3) == "3 días"
    keys = set(MESSAGES["en"])
    for key in keys:  # a key with a count has both forms
        if key.endswith(".one"):
            assert key.removesuffix(".one") + ".other" in keys, key


def test_a_count_and_its_unit_never_part_in_the_texts_that_show_them() -> None:
    for lang in ("English", "Spanish"):
        assert "\u00a0" in plural(lang, "suggest.push", 1) and "\u00a0" in plural(lang, "suggest.push", 5)
    assert plural("Spanish", "suggest.push", 5) == "último push hace 5\u00a0días"
    for key in ("suggest.issues", "suggest.reviewers"):  # the same line of reasons, joined with " · "
        for lang in ("English", "Spanish"):
            for count in (1, 5):
                assert "\u00a0" in plural(lang, key, count), (key, lang, count)
    for lang in ("en", "es"):  # the chip "active · priority 2x" is one piece
        assert "\u00a0\u00d72" in text(lang, "web.projects.active", weight="2")


def test_the_loki_label_hint_lists_what_the_code_tries() -> None:
    from lastcall import loki

    for lang in ("en", "es"):
        hint = text(lang, "web.loki.label_hint")
        assert ", ".join(loki.NAMES) in hint.replace(" or ", ", ").replace(" o ", ", ")


def test_the_fan_out_hint_quotes_the_labels_where_the_option_really_is() -> None:
    for lang in ("en", "es"):  # the option of the Account select, in the "Run now" group, used with "Audit now"
        hint = text(lang, "web.runs.fanout_hint")
        for key in ("web.project.all", "web.project.account", "web.project.run_now", "web.project.audit"):
            assert text(lang, key) in hint, (lang, key)


def test_the_texts_about_the_checks_call_the_two_settings_as_the_projects_page_does() -> None:
    for lang in ("en", "es"):
        merge, watch = text(lang, "web.project.merge_on_approve"), text(lang, "web.project.watch")
        lead = text(lang, "web.project.checks.lead", merge=merge, watch=watch)
        permissions = text(lang, "web.help.github.permissions")  # what the manifest asks for, and why
        for label in (merge, watch):
            assert label in lead and label in permissions, (lang, label)


def test_the_help_of_a_projects_language_quotes_the_option_where_it_really_is() -> None:
    for lang in ("en", "es"):
        assert text(lang, "web.project.language.account") in text(lang, "web.help.projects.language"), lang


def test_the_help_of_the_turns_calls_the_two_boxes_and_the_two_buttons_as_the_page_does() -> None:
    for lang in ("en", "es"):
        turns = text(lang, "web.help.projects.turns")
        for key in ("web.project.tests", "web.project.deps", "web.project.tests_now", "web.project.deps_now"):
            assert text(lang, key) in turns, (lang, key)


def test_the_help_of_the_watch_calls_the_active_box_as_the_page_does() -> None:
    for lang in ("en", "es"):
        assert text(lang, "web.project.active") in text(lang, "web.help.projects.watch"), lang


def test_the_hint_and_the_help_of_the_watch_both_say_that_logs_do_not_take_part() -> None:
    said = {
        "en": ("not your logs or error tracker", "take no part in the watch"),
        "es": ("no tus logs ni tu tracker de errores", "no intervienen en la vigilancia"),
    }
    for lang, (hint, bubble) in said.items():
        assert hint in text(lang, "web.project.watch_hint"), lang
        assert bubble in text(lang, "web.help.projects.watch"), lang


def test_the_help_about_effort_names_no_default_so_that_the_data_file_alone_decides_them() -> None:
    for lang, catalog in MESSAGES.items():
        for key, template in catalog.items():
            if key.startswith(("web.tiers.", "web.help.accounts.")):
                assert "xhigh" not in template, (lang, key)


def test_the_suggestions_say_where_they_come_from_in_both_languages() -> None:
    for lang in ("en", "es"):
        assert "models.dev" in text(lang, "web.tiers.suggest.catalog", provider="OpenRouter"), lang
        assert "Codex" in text(lang, "web.tiers.suggest.cli", tool="Codex"), lang
        assert "qwen/qwen3-coder" in text(lang, "web.accounts.model_suggest"), lang


def test_the_default_language_of_a_project_fits_a_closed_select_on_a_phone() -> None:
    """A closed select cuts its text short: about 30 characters fit in a phone's, and the field is cut at 24 to leave
    room for the narrower ones."""
    for lang in ("en", "es"):
        assert len(text(lang, "web.project.language.account")) <= 24, lang
