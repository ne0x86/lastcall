"""The data file of tiers: the default model and effort of each tier, the model names that each CLI takes, and the
effort levels that it takes."""

import json
from importlib import resources

import pytest

from lastcall.accounts import MODEL, Account
from lastcall.tiers import SECTIONS, TIERS, defaults, efforts, models

ENGINES = ("claude-code", "codex")  # the engines on a CLI login: the ones with default tiers


def test_the_default_effort_follows_the_work() -> None:
    for engine in ENGINES:
        assert {tier: effort for tier, (_, effort) in defaults(engine).items()} == {
            "fast": "medium",  # only a repository's own reviewers ask for it
            "balanced": "high",  # the reviewers
            "deep": "xhigh",  # the judge, the implementers and the improvement plans
        }


def test_the_default_models_are_the_ones_of_each_engine() -> None:
    assert {tier: model for tier, (model, _) in defaults("claude-code").items()} == {
        "fast": "haiku",
        "balanced": "sonnet",
        "deep": "opus",
    }
    assert {tier: model for tier, (model, _) in defaults("codex").items()} == {
        "fast": "gpt-6-luna",
        "balanced": "gpt-6.1-sol",
        "deep": "gpt-6.1-sol",
    }


def test_every_default_effort_is_one_that_the_cli_takes() -> None:
    for engine in ENGINES:
        for tier, (_, effort) in defaults(engine).items():
            assert effort in efforts(engine), (engine, tier)
    assert "xhigh" in efforts("claude-code") and "xhigh" in efforts("codex")


def test_the_efforts_of_an_engine_go_from_the_lowest_up() -> None:
    for engine in ("claude-code", "codex", "opencode"):
        levels = efforts(engine)
        assert levels == tuple(sorted(levels, key=("minimal", "low", "medium", "high", "xhigh", "max").index)), engine


def test_the_cli_of_an_account_gets_the_default_effort_of_each_of_its_tiers() -> None:
    assert Account("max", "claude-code").tiers() == {
        "fast": ("haiku", "medium"),
        "balanced": ("sonnet", "high"),
        "deep": ("opus", "xhigh"),
    }
    assert Account("chatgpt", "codex").tiers() == {
        "fast": ("gpt-6-luna", "medium"),
        "balanced": ("gpt-6.1-sol", "high"),
        "deep": ("gpt-6.1-sol", "xhigh"),
    }
    own = Account("chatgpt", "codex", models={"deep": "gpt-6-astra"}, efforts={"fast": "low"}).tiers()
    assert own["deep"] == ("gpt-6-astra", "xhigh") and own["fast"] == (
        "gpt-6-luna",
        "low",
    )  # a model keeps its tier's


def test_models_lists_what_each_cli_takes() -> None:
    assert models("claude-code") == ("haiku", "sonnet", "opus")
    assert models("codex") == ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna")


def test_every_default_model_is_one_that_it_suggests() -> None:
    for engine in ENGINES:
        for tier, (model, _) in defaults(engine).items():
            assert model in models(engine), (engine, tier)


def test_every_suggestion_is_a_name_that_an_account_takes_and_none_is_there_twice() -> None:
    for engine in ENGINES:
        assert all(MODEL.fullmatch(name) for name in models(engine)), engine
        assert len(set(models(engine))) == len(models(engine)), engine


def test_an_engine_with_no_list_suggests_nothing() -> None:
    # an API key's models come from models.dev, a local server's are the owner's, and a stand-in has none
    for engine in ("opencode", "scripted", "nothing", "_about", "models", "efforts"):
        assert models(engine) == (), engine


def test_the_sections_of_the_file_are_not_engines() -> None:
    for section in SECTIONS:
        with pytest.raises(ValueError, match="no default tiers"):
            defaults(section)
    data = json.loads(resources.files("lastcall.data").joinpath("tiers.json").read_text(encoding="utf-8"))
    for engine in set(data) - set(SECTIONS):
        assert set(data[engine]) == set(TIERS), engine  # what is left is an engine, with its three tiers
    assert set(data["models"]) - {"_about"} <= set(data) - set(SECTIONS)  # a list only for an engine that has tiers
    assert all(data[section]["_about"] for section in ("models", "efforts"))  # each section says what it is
