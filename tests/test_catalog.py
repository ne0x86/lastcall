"""models.dev's catalog, as the web UI reads it: which models a provider offers that a step can run on. The tests
never ask models.dev: `ModelsDev` is a `fetch` that answers from a small catalog of the shape that it has."""

import gzip
import json
import urllib.request
import zlib
from collections.abc import Mapping
from typing import Any

import pytest

from lastcall import __version__, catalog, engines
from lastcall.accounts import MODEL
from lastcall.engines.base import Keyed
from lastcall.github import Fetch, Response


def model(**fields: Any) -> dict[str, Any]:
    """One model of a provider: it calls tools and has a price, until a field says otherwise."""
    return {"name": "A model", "tool_call": True, "cost": {"input": 1.5, "output": 7.5}, **fields}


CATALOG: dict[str, Any] = {
    "openrouter": {
        "id": "openrouter",
        "name": "OpenRouter",
        "models": {
            "qwen/qwen3-coder": model(),
            "Qwen/Qwen3-coder-flash": model(),  # the order is alphabetical whatever the case
            "qwen/qwen3-coder:free": model(cost={"input": 0, "output": 0}),  # free is a price
            "anthropic/claude-sonnet-5.5": model(),
            "~anthropic/claude-sonnet-latest": model(),  # an alias that an account cannot name: it starts with ~
            "legacy/old-model": model(status="deprecated"),
            "beta/new-model": model(status="beta"),  # not deprecated: it stays
            "writer/no-tools": model(tool_call=False),
            "writer/tools-unknown": {"cost": {"input": 1, "output": 2}},
            "writer/no-price": {"tool_call": True},
            "writer/half-price": model(cost={"input": 1}),
            "writer/odd-price": model(cost={"input": "1", "output": True}),
        },
    },
    "anthropic": {"id": "anthropic", "models": {"claude-sonnet-5-5": model(), "claude-opus-5-5": model()}},
    "openai": {"id": "openai", "models": {"gpt-6.1-sol": model(), "gpt-image-2": model(tool_call=False)}},
    "ollama-cloud": {"id": "ollama-cloud", "models": {"kimi-k3": model()}},  # not a provider that lastcall asks about
}
URL = "https://models.dev/api.json"
Answer = Mapping[str, Any] | Exception | tuple[int, bytes]


class ModelsDev:
    """A `fetch` that answers what models.dev would: the catalog (gzipped, as it is when it is asked for gzip), or the
    exception that a network that fails raises, or a status and a body. It records every request it gets."""

    def __init__(self, answer: Answer = CATALOG, packed: bool = True) -> None:
        self.answer, self.packed = answer, packed
        self.asked: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request) -> Response:
        self.asked.append(req)
        assert req.full_url == URL, req.full_url
        if isinstance(self.answer, Exception):
            raise self.answer
        if isinstance(self.answer, tuple):
            status, body = self.answer
            return status, {}, body
        body = json.dumps(self.answer).encode()
        if self.packed:
            return 200, {"Content-Type": "application/json", "Content-Encoding": "gzip"}, gzip.compress(body)
        return 200, {"Content-Type": "application/json"}, body


def says(body: bytes, headers: Mapping[str, str] | None = None) -> Fetch:
    """A `fetch` that answers 200 with this body and these headers, whatever it is asked."""

    def fetch(req: urllib.request.Request) -> Response:
        return 200, dict(headers or {}), body

    return fetch


def listed(provider: str) -> tuple[str, ...]:
    return catalog.models(ModelsDev(), [provider], 5)[provider]


def test_it_keeps_the_models_that_a_step_can_run_on() -> None:
    assert listed("openrouter") == (
        "anthropic/claude-sonnet-5.5",
        "beta/new-model",
        "qwen/qwen3-coder",
        "Qwen/Qwen3-coder-flash",
        "qwen/qwen3-coder:free",
    )
    assert listed("anthropic") == ("claude-opus-5-5", "claude-sonnet-5-5")
    assert listed("openai") == ("gpt-6.1-sol",)


def test_every_name_that_it_keeps_is_one_that_an_account_takes() -> None:
    assert all(MODEL.fullmatch(name) for name in listed("openrouter"))
    assert "~anthropic/claude-sonnet-latest" not in listed("openrouter")


def test_one_request_answers_for_every_provider_and_a_missing_provider_has_none() -> None:
    fetch = ModelsDev()
    found = catalog.models(fetch, ["openrouter", "anthropic", "openai", "nobody"], 5)
    assert len(fetch.asked) == 1
    assert found["nobody"] == () and found["anthropic"] == ("claude-opus-5-5", "claude-sonnet-5-5")
    assert "ollama-cloud" not in found  # it keeps only the providers that it is asked about


def test_the_request_says_who_asks_wants_gzip_and_waits_5_seconds_and_carries_nothing_of_the_owner() -> None:
    fetch = ModelsDev()
    catalog.models(fetch, ["openai"], 5)
    (req,) = fetch.asked
    assert req.full_url == "https://models.dev/api.json" and req.get_method() == "GET" and req.data is None
    assert req.get_header("User-agent") == f"lastcall/{__version__} (+https://github.com/ne0x86/lastcall)"
    assert req.get_header("Accept-encoding") == "gzip" and getattr(req, "timeout", None) == 5
    assert not req.has_header("Authorization") and not req.has_header("Cookie")


@pytest.mark.parametrize("packed", [True, False])
def test_a_gzipped_answer_and_a_plain_one_read_the_same(packed: bool) -> None:
    found = catalog.models(ModelsDev(packed=packed), ["anthropic"], 5)
    assert found == {"anthropic": ("claude-opus-5-5", "claude-sonnet-5-5")}


def test_the_header_that_says_gzip_is_read_whatever_its_case() -> None:
    fetch = says(gzip.compress(json.dumps(CATALOG).encode()), {"content-encoding": " GZip "})
    assert catalog.models(fetch, ["openai"], 5) == {"openai": ("gpt-6.1-sol",)}


@pytest.mark.parametrize(
    "answer",
    [
        (404, b"{}"),
        (403, gzip.compress(b"<html>Forbidden</html>")),  # what models.dev says to a request with no User-Agent
        (500, b""),
        (200, b""),
        (200, b"<html>a captive portal</html>"),
        (200, b'["not", "the", "catalog"]'),
        (200, b"null"),
        OSError("no route to host"),
        TimeoutError("timed out"),
    ],
)
def test_an_answer_that_is_not_the_catalog_is_an_error(answer: Answer) -> None:
    with pytest.raises((OSError, ValueError)):
        catalog.models(ModelsDev(answer), ["openrouter"], 5)


def test_a_gzip_that_is_broken_is_an_error() -> None:
    with pytest.raises(zlib.error):
        catalog.models(says(b"this is not gzip", {"Content-Encoding": "gzip"}), ["openrouter"], 5)


@pytest.mark.parametrize(
    "entry",
    [None, [], "openrouter", 3, {}, {"models": None}, {"models": []}, {"models": "x"}, {"models": {"a/b": "text"}}],
)
def test_a_provider_that_does_not_look_like_one_has_no_models(entry: object) -> None:
    assert catalog.models(ModelsDev({"p": entry}), ["p"], 5) == {"p": ()}


def test_what_else_the_catalog_says_about_a_model_does_not_matter() -> None:
    odd = {"p": {"models": {"a/b": model(cost={"input": 1, "output": 2, "cache_read": None, "tiers": [{"x": 1}]})}}}
    assert catalog.models(ModelsDev(odd), ["p"], 5) == {"p": ("a/b",)}


def test_a_provider_keeps_at_most_max_models_the_first_in_alphabetical_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "MAX_MODELS", 3)
    many = {"p": {"models": {f"vendor/m{n:02}": model() for n in range(20, 0, -1)}}}
    assert catalog.models(ModelsDev(many), ["p"], 5) == {"p": ("vendor/m01", "vendor/m02", "vendor/m03")}


def test_an_answer_over_the_limit_is_refused_packed_or_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "MAX_BYTES", 1000)
    big = {"p": {"models": {f"vendor/model-{n}": model() for n in range(100)}}}
    for packed in (True, False):
        with pytest.raises(ValueError, match="too big"):
            catalog.models(ModelsDev(big, packed=packed), ["p"], 5)


def test_a_few_kilobytes_that_unpack_to_megabytes_are_refused_while_they_unpack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "MAX_BYTES", 1024 * 1024)
    bomb = gzip.compress(b" " * (20 * 1024 * 1024))  # 20 MB of spaces, 20 KB packed: under the limit as it comes
    assert len(bomb) < 100 * 1024
    with pytest.raises(ValueError, match="too big"):
        catalog.models(says(bomb, {"Content-Encoding": "gzip"}), ["openrouter"], 5)


def test_an_answer_of_exactly_the_limit_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(CATALOG).encode()
    monkeypatch.setattr(catalog, "MAX_BYTES", len(body))  # its size, plain or unpacked
    assert catalog.models(says(body), ["openai"], 5) == {"openai": ("gpt-6.1-sol",)}
    packed = says(gzip.compress(body), {"Content-Encoding": "gzip"})
    assert catalog.models(packed, ["openai"], 5) == {"openai": ("gpt-6.1-sol",)}


def test_each_api_provider_of_opencode_says_its_id_in_models_dev_and_a_local_server_has_none() -> None:
    engine = engines.get("opencode")
    assert isinstance(engine, Keyed)
    found = {p.name: p.catalog for p in engine.providers}
    assert found == {"openrouter": "openrouter", "anthropic": "anthropic", "openai": "openai", "ollama": ""}
    assert all(p.keys for p in engine.providers if p.catalog)  # an API takes a key; a local server has none
