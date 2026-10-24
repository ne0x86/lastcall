"""Projects: what lastcall suggests among the installed repositories, and adding one by its URL (DESIGN.md,
section 8). The suggestions need many GitHub requests, so the store keeps them: the service refreshes them every few
hours, and the owner can ask for it from the web UI or with `lastcall github repos`.
"""

from __future__ import annotations

import re

from lastcall.github import GitHub
from lastcall.store import Store
from lastcall.suggest import Suggestion, suggest

REFRESH_EVERY = 6 * 3600.0
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
GITHUB_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)


def parse_repo(text: str) -> str | None:
    """owner/name from what the owner pastes: owner/name, or a github.com address of the repository or of a page in
    it, with or without .git. None when it is not one."""
    value = GITHUB_URL.sub("", text.strip()).split("?")[0].split("#")[0]
    parts = value.strip("/").split("/")
    if len(parts) < 2:
        return None
    repo = f"{parts[0]}/{parts[1].removesuffix('.git')}"
    return repo if REPO.fullmatch(repo) and not repo.endswith(("/.", "/..")) else None


def refresh(store: Store, client: GitHub, now: float) -> list[Suggestion]:
    """Suggestions for every repository the App is installed on, saved in the store."""
    found = []
    for installation in client.installations():
        token = client.installation_token(int(installation["id"]))
        for repo in client.repositories(token):
            files, truncated = client.files(token, repo["full_name"], repo["default_branch"])
            found.append(suggest(repo, files, now, truncated))
    store.save_suggestions(found, now)
    return found


def installed_name(client: GitHub, repo: str) -> str:
    """The repository's name as GitHub writes it. Raises GitHubError (404) when the App is not installed on it."""
    token = client.repository_token(repo)
    return str(client.repository(token, repo)["full_name"])
