"""Watch after the merge (DESIGN.md, section 11): for a day after a lastcall pull request is merged, in a project
that asks for it, lastcall looks at the checks of the merged commit and at the project's health address, if it has
one. When the checks fail where they passed before the merge, or the address stops answering 3 times in a row, it
opens a pull request that reverts the change, pauses the project and tells the owner. It never merges the revert:
the owner does, if that change is the cause.

Reading the checks needs the GitHub App's Checks and Commit statuses permissions (read). While the App lacks them,
lastcall says so in the log and looks only at the health address.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lastcall import git, i18n
from lastcall.approval import ChecksUnreadable, ensure_readable, failing
from lastcall.github import GitHub, GitHubError, GitHubForge
from lastcall.store import ProjectRow, Store

if TYPE_CHECKING:
    from lastcall.notify import Notifier

WATCH_HOURS = 24.0
HEALTH_FAILURES = 3  # in a row: the service syncs every 10 minutes
HEALTH_TIMEOUT = 10.0
Log = Callable[[str], None]
Revert = Callable[[GitHub, str, Mapping[str, Any], str, str], str | None]  # (client, repo, pr, title, body) -> url


def answers(url: str) -> bool:
    """Whether the health address answers, with a status below 400, within 10 seconds."""
    request = urllib.request.Request(url, headers={"User-Agent": "lastcall"})
    try:
        with urllib.request.urlopen(request, timeout=HEALTH_TIMEOUT) as response:
            return 200 <= int(response.status) < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def open_revert(client: GitHub, repo: str, pr: Mapping[str, Any], title: str, body: str) -> str | None:
    """A pull request that reverts the merged change, on a new branch from the default branch. None when the change
    does not revert cleanly any more."""
    forge = GitHubForge.connect(client, repo)
    work = Path(tempfile.mkdtemp(prefix="lastcall-revert-"))
    try:
        git.clone(repo, work / "clone", client.repository_token(repo), forge.base)
        branch = f"lastcall/revert-{pr['number']}"
        if not git.revert(work / "clone", str(pr["merge_sha"]), branch, forge.author):
            return None
        forge.push(work / "clone", branch)
        return forge.open_pull_request(branch, title, body)
    finally:
        git.remove(work)


def broken(client: GitHub, token: str, repo: str, sha: str) -> list[str]:
    """The names of the checks that the merge broke: they fail on the merged commit and passed on the one before."""
    ensure_readable(client, repo)
    failed = failing(client, token, repo, sha)
    if failed:
        parent = client.request("GET", f"/repos/{repo}/commits/{sha}", token)["parents"][0]["sha"]
        if not failing(client, token, repo, parent):
            return failed
    return []


def cause(
    client: GitHub,
    token: str,
    store: Store,
    pr: Mapping[str, Any],
    project: ProjectRow,
    health: Callable[[str], bool],
    log: Log = print,
) -> str | None:
    """Why the merged change should be reverted, in the project's language; None while all is well. When the App
    cannot read the checks, `log` says so and the health address is still looked at."""
    language, repo, sha = project.language or "English", str(pr["repo"]), str(pr["merge_sha"])
    try:
        failed = broken(client, token, repo, sha)
    except ChecksUnreadable as e:
        log(f"watch {repo}: {e}")
        failed = []
    if failed:
        return i18n.text(language, "watch.checks", names=", ".join(failed))
    if project.health_url:
        failures = store.health(str(pr["url"]), health(project.health_url))
        if failures >= HEALTH_FAILURES:
            return i18n.text(language, "watch.health", address=project.health_url, count=str(failures))
    return None


def watch(
    store: Store,
    client: GitHub,
    log: Log = print,
    notify: Notifier | None = None,
    now: float | None = None,
    health: Callable[[str], bool] = answers,
    revert: Revert = open_revert,
) -> int:
    """Look at the merged pull requests under watch. Returns how many lastcall proposed to revert."""
    reverted = 0
    told: set[str] = set()

    def once(line: str) -> None:  # several merged pull requests of a project wait on the same permission
        if line not in told:
            told.add(line)
            log(line)

    for pr in store.watched(time.time() if now is None else now):
        repo = str(pr["repo"])
        project = store.project(repo)
        if project is None or not project.watch:
            continue
        try:
            why = cause(client, client.repository_token(repo), store, pr, project, health, once)
        except GitHubError as e:
            log(f"watch {pr['url']}: {e}")
            continue
        if why is None:
            continue
        language = project.language or "English"
        title = i18n.text(language, "revert.title", title=str(pr["title"]))
        body = i18n.text(language, "revert.body", url=str(pr["url"]), reason=why, sha=str(pr["merge_sha"])[:7])
        try:
            url = revert(client, repo, pr, title, body)
        except (GitHubError, subprocess.CalledProcessError, OSError) as e:
            log(f"revert of {pr['url']}: {e}")
            url = None
        store.record_revert(str(pr["url"]), url)
        store.pause_project(repo)
        reverted += 1
        log(f"{pr['url']}: {why}; " + (f"revert proposed: {url}" if url else "it does not revert cleanly"))
        if notify:
            values = {"repo": repo, "title": str(pr["title"]), "reason": why}
            if url:
                notify.notify("reverted", url=url, **values)
            else:
                notify.notify("reverted", "revert-failed", url=str(pr["url"]), **values)
    return reverted
