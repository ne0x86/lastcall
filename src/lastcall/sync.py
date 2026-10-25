"""GitHub to lastcall: what the owner did with lastcall's pull requests, decisions issues and improvement plans.

A merged pull request marks its findings and ideas `merged`; one closed without merging marks them `rejected`, and
they are not proposed again. A decisions issue closed as completed marks what was left `resolved`; closed as not
planned, deleted or out of the App's reach, `dismissed`. In an open improvement plan, a ticked idea becomes `wanted`,
and a run builds it; a closed plan dismisses the ideas that were not ticked.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from lastcall.github import GitHub, GitHubError
from lastcall.recipes.improve import ticked
from lastcall.store import Store

REF = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/(\d+)")


def ref(url: str) -> tuple[str, int]:
    match = REF.match(url)
    if not match:
        raise ValueError(f"not a GitHub issue or pull request URL: {url}")
    return match.group(1), int(match.group(2))


def sync(store: Store, client: GitHub, log: Callable[[str], None] = print) -> int:
    """Read the state of every open lastcall pull request, decisions issue and improvement plan. Returns how many
    changed."""
    tokens: dict[str, str] = {}
    changed = 0

    def fetch(url: str, kind: str) -> dict[str, Any] | None:
        repo, number = ref(url)
        try:
            if repo not in tokens:
                tokens[repo] = client.repository_token(repo)
            return dict(client.request("GET", f"/repos/{repo}/{kind}/{number}", tokens[repo]))
        except GitHubError as e:
            if e.status in (404, 410):  # deleted, or the App was removed from the repository
                return {"state": "closed", "merged_at": None, "state_reason": "not_planned"}
            log(f"sync {url}: {e}")
            return None

    for pr in store.open_prs():
        data = fetch(pr["url"], "pulls")
        if data and data.get("state") == "closed":
            merged = bool(data.get("merged_at"))
            store.close_pr(pr["url"], merged)
            changed += 1
            log(f"{pr['url']}: {'merged' if merged else 'closed without merging'}")
    for url in store.open_issues():
        data = fetch(url, "issues")
        if data and data.get("state") == "closed":
            completed = data.get("state_reason") == "completed"
            changed += store.close_issue(url, completed)
            log(f"{url}: closed as {'completed' if completed else 'not planned'}")
    for url in store.open_plans():
        data = fetch(url, "issues")
        if data is None:
            continue
        if data.get("state") == "closed":
            changed += store.close_plan(url, data.get("state_reason") == "completed")
            log(f"{url}: plan closed")
        else:
            ticks = store.tick_ideas(url, ticked(str(data.get("body") or "")))
            if ticks:
                log(f"{url}: {ticks} idea(s) ticked or unticked")
            changed += ticks
    return changed
