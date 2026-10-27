"""models.dev's public catalog of models (https://models.dev/api.json), where OpenCode reads what each model costs.
The web UI reads from it which models an API key's provider offers, to suggest them in the model fields
(DESIGN.md, section 6).

The catalog is one JSON object, `{provider id: {"models": {model id: {...}}}}`. Of each model lastcall looks at three
things: `tool_call`, since a step runs on tools; `cost`, since the monthly budget counts what each step costs, so a
model with no price cannot run; and `status`, since a deprecated model is not worth suggesting. It suggests only the
ids that an account's model field takes (`accounts.MODEL`). Anything else in the catalog is ignored, and an answer
that does not look like this counts as no models.

The answer weighs 6 MB, or 0.6 MB gzipped, so the request asks for gzip. The reading is bounded: neither the answer
nor what it unpacks to may pass `MAX_BYTES`, and a provider keeps `MAX_MODELS` models at most.
"""

from __future__ import annotations

import json
import urllib.request
import zlib
from collections.abc import Iterable

from lastcall import __version__
from lastcall.accounts import MODEL
from lastcall.github import Fetch

URL = "https://models.dev/api.json"
AGENT = f"lastcall/{__version__} (+https://github.com/ne0x86/lastcall)"  # without a User-Agent, models.dev says 403
MAX_BYTES = 32 * 1024 * 1024  # the most that the answer may weigh, packed or unpacked
MAX_MODELS = 1000  # the most that one provider keeps: OpenRouter lists the most, about 300


def request(timeout: float) -> urllib.request.Request:
    req = urllib.request.Request(URL)
    req.add_header("User-Agent", AGENT)
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Encoding", "gzip")
    req.timeout = timeout  # read by github.urlopen; a fake fetch ignores it
    return req


def models(fetch: Fetch, providers: Iterable[str], timeout: float) -> dict[str, tuple[str, ...]]:
    """The models that models.dev lists for each of `providers`, sorted, from one request to `fetch`: the ones that a
    step can run on, `MAX_MODELS` at most. A provider that the catalog does not list gets none. Raises what `fetch`
    raises, or ValueError, or zlib.error, when the answer cannot be read."""
    status, headers, raw = fetch(request(timeout))
    if status != 200:
        raise ValueError(f"models.dev answered {status}")
    if len(raw) > MAX_BYTES:
        raise ValueError("the answer of models.dev is too big")
    if any(name.lower() == "content-encoding" and value.strip().lower() == "gzip" for name, value in headers.items()):
        raw = gunzip(raw)
    catalog = json.loads(raw)
    if not isinstance(catalog, dict):
        raise ValueError("models.dev did not answer with its catalog")
    return {name: suggestions(catalog.get(name)) for name in providers}


def gunzip(packed: bytes) -> bytes:
    """What a gzipped answer unpacks to. A few KB can unpack to gigabytes, so more than `MAX_BYTES` is refused."""
    inflater = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    unpacked = inflater.decompress(packed, MAX_BYTES + 1)
    if len(unpacked) > MAX_BYTES:
        raise ValueError("the answer of models.dev is too big")
    return unpacked


def suggestions(provider: object) -> tuple[str, ...]:
    """The ids of the models in a provider's entry of the catalog that a step can run on, in alphabetical order."""
    listed = provider.get("models") if isinstance(provider, dict) else None
    if not isinstance(listed, dict):
        return ()
    found = [name for name, model in listed.items() if isinstance(name, str) and usable(name, model)]
    return tuple(sorted(found, key=lambda name: (name.casefold(), name))[:MAX_MODELS])


def usable(name: str, model: object) -> bool:
    """Whether a step can run on this model and an account may name it: it calls tools, it has a price (free is a
    price) and it is not deprecated."""
    if not isinstance(model, dict) or not MODEL.fullmatch(name):
        return False
    return model.get("tool_call") is True and model.get("status") != "deprecated" and priced(model.get("cost"))


def priced(cost: object) -> bool:
    """Whether a cost entry of the catalog has the price of what goes in and of what comes out."""
    if not isinstance(cost, dict):
        return False
    prices = [cost.get("input"), cost.get("output")]
    return all(isinstance(price, int | float) and not isinstance(price, bool) for price in prices)
