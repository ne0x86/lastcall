"""Which repositories are worth lastcall's time, decided without any model call (DESIGN.md, section 8).

Points: recent activity (30 within a month, 15 within three months, 5 within a year), tests (20), CI (5), a language
v0.1 can verify (15) and open issues (1 each, up to 10). Large repositories lose 15, tiny ones 10. Archived, forked
and empty repositories are left out. A repository is suggested from 40 points.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SUGGEST_FROM = 40
SUPPORTED = {"Python", "JavaScript", "TypeScript"}  # the default environments of v0.1 (section 10)
SKIP = re.compile(
    r"(^|/)(vendor|node_modules|dist|build)/|\.min\.(js|css)$|(\.lock|lock\.json)$|"
    r"\.(png|jpe?g|gif|ico|webp|svg|pdf|zip|gz|db|sqlite3?|woff2?|ttf|mp[34]|csv|parquet|map)$"
)
TESTS = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$")
CI = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$|^\.gitlab-ci\.yml$|^\.circleci/")
MAX_FILE = 120_000  # larger files do not go into the context pack
BIG = 400_000  # tokens of code


@dataclass(frozen=True)
class Suggestion:
    repo: str
    score: int
    suggested: bool
    language: str | None
    code_tokens: int  # about 4 bytes per token, for the files a context pack would include
    reasons: tuple[str, ...]


def suggest(
    repo: Mapping[str, Any], files: Sequence[tuple[str, int]], now: float, truncated: bool = False
) -> Suggestion:
    name, language = str(repo["full_name"]), repo.get("language")
    left_out = "archived" if repo.get("archived") else "fork" if repo.get("fork") else None
    if left_out is None and not files:
        left_out = "empty"
    tokens = sum(size for path, size in files if size <= MAX_FILE and not SKIP.search(path)) // 4
    if left_out:
        return Suggestion(name, 0, False, language, tokens, (f"left out: {left_out}",))

    score, reasons = 0, []
    pushed = repo.get("pushed_at")
    days = (now - datetime.fromisoformat(str(pushed)).timestamp()) / 86_400 if pushed else 10_000.0
    score += 30 if days <= 30 else 15 if days <= 90 else 5 if days <= 365 else 0
    reasons.append(f"last push {days:.0f} days ago")
    if any(TESTS.search(path) for path, _ in files):
        score += 20
        reasons.append("tests")
    else:
        reasons.append("no tests: fixes can only be checked to build")
    if any(CI.search(path) for path, _ in files):
        score += 5
        reasons.append("CI")
    if language in SUPPORTED:
        score += 15
    else:
        reasons.append(f"{language or 'no main language'}: needs a project environment (devcontainer, v0.2)")
    issues = int(repo.get("open_issues_count") or 0)
    if issues:
        score += min(10, issues)
        reasons.append(f"{issues} open issues")
    if tokens > BIG:
        score -= 15
        reasons.append("large: an audit covers part of the code")
    elif tokens < 2_000:
        score -= 10
        reasons.append("tiny")
    if truncated:
        reasons.append("very many files: size is a lower bound")
    return Suggestion(name, score, score >= SUGGEST_FROM, language, tokens, tuple(reasons))
