"""Which repositories are worth lastcall's time, decided without any model call (DESIGN.md, section 8).

Points: recent activity (30 within a month, 15 within three months, 5 within a year), tests (20), CI (5), an
environment lastcall can verify in (15: Python or Node.js, a .lastcall.yml, a devcontainer, go.mod, Cargo.toml or
pom.xml) and open issues (1 each, up to 10). Large repositories lose 15, tiny ones 10. Archived, forked
and empty repositories are left out. A repository is suggested from 40 points.

Reasons are catalog keys with their values, so the web UI says them in the owner's language and the command line in
English.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lastcall.i18n import MESSAGES, plural, text

SUGGEST_FROM = 40
SUPPORTED = {"Python", "JavaScript", "TypeScript"}  # verified in the runner itself (section 10)
# Files that give a project its own environment (lastcall.environment)
ENVIRONMENT_FILES = re.compile(
    r"\.lastcall\.ya?ml|\.devcontainer\.json|\.devcontainer/([^/]+/)?devcontainer\.json|go\.mod|Cargo\.toml|pom\.xml"
)
REVIEWER_FILES = re.compile(r"\.lastcall/reviewers/[^/]+\.md")  # a project's own reviewers (lastcall.recipes.audit)
SKIP = re.compile(
    r"(^|/)(vendor|node_modules|dist|build)/|\.min\.(js|css)$|(\.lock|lock\.json)$|"
    r"\.(png|jpe?g|gif|ico|webp|svg|pdf|zip|gz|db|sqlite3?|woff2?|ttf|mp[34]|csv|parquet|map)$"
)
TESTS = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$")
CI = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$|^\.gitlab-ci\.yml$|^\.circleci/")
MAX_FILE = 120_000  # larger files do not go into the context pack
BIG = 400_000  # tokens of code
Reason = tuple[str, dict[str, Any]]  # a key under "suggest." in the catalogs, and its values


@dataclass(frozen=True)
class Suggestion:
    repo: str
    score: int
    suggested: bool
    language: str | None
    code_tokens: int  # about 4 bytes per token, for the files a context pack would include
    why: tuple[Reason, ...]

    @property
    def reasons(self) -> tuple[str, ...]:
        """The reasons in English, as the command line prints them."""
        return tuple(explain("en", r) for r in self.why)


def explain(lang: str, reason: Reason) -> str:
    key, values = f"suggest.{reason[0]}", reason[1]
    if f"{key}.one" in MESSAGES["en"]:
        return plural(lang, key, int(values["count"]))
    return text(lang, key, **values)


def suggest(
    repo: Mapping[str, Any], files: Sequence[tuple[str, int]], now: float, truncated: bool = False
) -> Suggestion:
    name, language = str(repo["full_name"]), repo.get("language")
    left_out = "archived" if repo.get("archived") else "fork" if repo.get("fork") else None
    if left_out is None and not files:
        left_out = "empty"
    tokens = sum(size for path, size in files if size <= MAX_FILE and not SKIP.search(path)) // 4
    if left_out:
        return Suggestion(name, 0, False, language, tokens, ((left_out, {}),))

    score, why = 0, list[Reason]()
    pushed = repo.get("pushed_at")
    days = (now - datetime.fromisoformat(str(pushed)).timestamp()) / 86_400 if pushed else 10_000.0
    score += 30 if days <= 30 else 15 if days <= 90 else 5 if days <= 365 else 0
    why.append(("push", {"count": round(days)}))
    if any(TESTS.search(path) for path, _ in files):
        score += 20
        why.append(("tests", {}))
    else:
        why.append(("no-tests", {}))
    if any(CI.search(path) for path, _ in files):
        score += 5
        why.append(("ci", {}))
    environment = next((path for path, _ in files if ENVIRONMENT_FILES.fullmatch(path)), None)
    if language in SUPPORTED or environment:
        score += 15
        if environment and language not in SUPPORTED:
            why.append(("env-file", {"file": environment}))
    else:
        why.append(("environment", {"language": language}) if language else ("no-language", {}))
    reviewers = sum(1 for path, _ in files if REVIEWER_FILES.fullmatch(path))
    if reviewers:
        why.append(("reviewers", {"count": reviewers}))
    issues = int(repo.get("open_issues_count") or 0)
    if issues:
        score += min(10, issues)
        why.append(("issues", {"count": issues}))
    if tokens > BIG:
        score -= 15
        why.append(("large", {}))
    elif tokens < 2_000:
        score -= 10
        why.append(("tiny", {}))
    if truncated:
        why.append(("truncated", {}))
    return Suggestion(name, score, score >= SUGGEST_FROM, language, tokens, tuple(why))
