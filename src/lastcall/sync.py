"""GitHub to lastcall: what the owner did with lastcall's pull requests, decisions issues and improvement plans.

A merged pull request marks its findings and ideas `merged`; one closed without merging marks them `rejected`, and
they are not proposed again. A decisions issue closed as completed marks what was left `resolved`; closed as not
planned, deleted or out of the App's reach, `dismissed`. In an open improvement plan, a ticked idea becomes `wanted`,
and a run builds it; a closed plan dismisses the ideas that were not ticked.

In a project with merge on approve, an open pull request that the owner approved is merged here
(lastcall.approval). In a project that is watched, merged pull requests are watched here (lastcall.watch).

In active projects, the open issues labelled `lastcall` wait for a run (lastcall.recipes.issues); lastcall creates
the label once, and takes it off the issues it gave back: those it could not do, and those whose pull request the
owner closed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from lastcall.approval import land
from lastcall.github import GitHub, GitHubError
from lastcall.recipes.improve import ticked
from lastcall.recipes.issues import LABEL
from lastcall.store import Store
from lastcall.watch import WATCH_HOURS, watch

if TYPE_CHECKING:
    from lastcall.notify import Notifier

LABEL_ABOUT = "lastcall works on this issue with leftover AI quota"
REF = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/(\d+)")


def ref(url: str) -> tuple[str, int]:
    match = REF.match(url)
    if not match:
        raise ValueError(f"not a GitHub issue or pull request URL: {url}")
    return match.group(1), int(match.group(2))


def sync(store: Store, client: GitHub, log: Callable[[str], None] = print, notify: Notifier | None = None) -> int:
    """Read the state of every open lastcall pull request, decisions issue and improvement plan, and the issues
    labelled `lastcall`; merge what the owner approved where the project asks for it, and watch what was merged.
    Returns how many changed."""
    tokens: dict[str, str] = {}
    changed = 0

    def token(repo: str) -> str:
        if repo not in tokens:
            tokens[repo] = client.repository_token(repo)
        return tokens[repo]

    def fetch(url: str, kind: str) -> dict[str, Any] | None:
        repo, number = ref(url)
        try:
            auth = token(repo)
            return dict(client.request("GET", f"/repos/{repo}/{kind}/{number}", auth))
        except GitHubError as e:
            if e.status in (404, 410):  # deleted, or the App was removed from the repository
                return {"state": "closed", "merged_at": None, "state_reason": "not_planned"}
            log(f"sync {url}: {e}")
            return None

    for pr in store.open_prs():
        data = fetch(pr["url"], "pulls")
        if data is None:
            continue
        project = store.project(pr["repo"])
        hours = WATCH_HOURS if project and project.watch and pr["kind"] == "change" else 0
        if data.get("state") == "closed":
            merged = bool(data.get("merged_at"))
            store.close_pr(pr["url"], merged, data.get("merge_commit_sha"), hours)
            changed += 1
            log(f"{pr['url']}: {'merged' if merged else 'closed without merging'}")
        elif project and project.merge_on_approve and pr["kind"] == "change":
            try:
                outcome, detail = land(client, token(pr["repo"]), pr["repo"], data, project.language or "English")
            except GitHubError as e:
                log(f"merge on approve {pr['url']}: {e}")
                continue
            if outcome == "merged":
                store.close_pr(pr["url"], True, detail, hours)
                changed += 1
                log(f"{pr['url']}: merged after the owner's approval")
                if notify:
                    notify.notify("merged", repo=pr["repo"], title=pr["title"], url=pr["url"])
            elif outcome == "closed":
                store.reopen(pr["url"])
                changed += 1
                log(f"{pr['url']}: closed, {detail}; its changes go back to pending")
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
    for project in store.projects(active_only=True):
        try:
            auth = token(project.repo)
            if not store.setting(f"label {project.repo}"):
                client.ensure_label(auth, project.repo, LABEL, LABEL_ABOUT)
                store.set_setting(f"label {project.repo}", "yes")
            changed += store.sync_tickets(project.repo, client.labelled_issues(auth, project.repo, LABEL))
        except GitHubError as e:
            log(f"labelled issues of {project.repo}: {e}")
    for ticket in store.to_unlabel():
        try:
            client.unlabel(token(ticket["repo"]), ticket["repo"], int(ticket["number"]), LABEL)
            store.unlabeled(int(ticket["id"]))
        except GitHubError as e:
            log(f"{ticket['url']}: {e}")
    return changed + watch(store, client, log, notify)
