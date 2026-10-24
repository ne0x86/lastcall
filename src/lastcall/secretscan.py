"""A secret scan for everything lastcall publishes: the lines a pull request adds, its title and body, and issue
texts. High-precision rules only; a hit blocks the publication and names the rule, never the value.

It also covers an agent copying its own CLI credentials into its output (DESIGN.md, section 12).
"""

from __future__ import annotations

import re

RULES = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"),
    "Anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI key": re.compile(r"\bsk-(proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "JSON Web Token": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
}


def scan(text: str) -> list[str]:
    """Names of the rules that match: empty when the text is clean."""
    return [name for name, rule in RULES.items() if rule.search(text)]


def added_lines(diff: str) -> str:
    """The lines a unified diff adds: what a pull request would publish."""
    return "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
