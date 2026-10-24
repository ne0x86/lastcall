"""The egress allowlist: the domains runners may reach through the proxy (DESIGN.md, section 5).

It is built from the enabled engines and the package ecosystems of the projects. A domain written `*.example.com`
also allows its subdomains. The output is the tinyproxy filter format: one extended regular expression per line.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from lastcall.engines.base import Engine

REGISTRIES = {
    "npm": ("registry.npmjs.org",),
    "pypi": ("pypi.org", "files.pythonhosted.org"),
}
DOMAIN = re.compile(r"(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+")


def allowlist(engines: Iterable[Engine], ecosystems: Iterable[str]) -> list[str]:
    domains = {d for e in engines for d in e.egress} | {d for eco in ecosystems for d in REGISTRIES[eco]}
    return [pattern(d) for d in sorted(domains, key=lambda d: d.removeprefix("*."))]


def pattern(domain: str) -> str:
    if not DOMAIN.fullmatch(domain):
        raise ValueError(f"not a domain: {domain!r}")
    if domain.startswith("*."):
        return r"^(.+\.)?" + domain[2:].replace(".", r"\.") + "$"
    return "^" + domain.replace(".", r"\.") + "$"


def render(engines: Iterable[Engine], ecosystems: Iterable[str]) -> str:
    return "".join(f"{p}\n" for p in allowlist(engines, ecosystems))
