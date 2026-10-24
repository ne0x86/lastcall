"""GitHub to lastcall: what the owner did with lastcall's pull requests and decisions issues.

A merged pull request marks its findings `merged`; one closed without merging marks them `rejected`, and the judge
will not propose them again. A decisions issue closed as completed marks what was left `resolved`; closed as not
planned, deleted or out of the App's reach, `dismissed`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from lastcall.github import GitHub, GitHubError
from lastcall.store import Store

REF = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/(\d+)")


def ref(url: str) -> tuple[str, int]:
    match = REF.match(url)
    if not match:
        raise ValueError(f"not a GitHub issue or pull request URL: {url}")
    return match.group(1), int(match.group(2))


def sync(store: Store, client: GitHub, log: Callable[[str], None] = print) -> int:
    """Read the state of every open lastcall pull request and decisions issue. Returns how many changed."""
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
    return changed
