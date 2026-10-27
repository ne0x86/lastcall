"""Merge on approve (DESIGN.md, section 11): a project can let lastcall merge its own pull requests once the owner
approves them on GitHub.

lastcall merges a pull request only when:
- someone who may write to the repository (its owner, a member or a collaborator) approved its latest commit, and
  nobody who may write asks for changes;
- GitHub says that it merges, and no check fails or is still running;
- the merge names that commit, so that a push after the checks stops it.

Reading the checks takes two permissions of the GitHub App, Checks and Commit statuses (read). An App created before
they were asked for lacks them: lastcall finds out from the installation, says what to do (`ChecksUnreadable`) and
does not ask for the checks.

A pull request that no longer merges cleanly is closed with a comment, and its findings or ideas go back to pending:
a later run makes the change again on the current code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from lastcall import i18n
from lastcall.github import GitHub, GitHubError, lacking

TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}  # GitHub's author_association of people who may write
FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
NAMES = {"checks": "Checks", "statuses": "Commit statuses"}  # as the App's permissions page on GitHub calls them
Outcome = Literal["merged", "closed", "waiting"]


class ChecksUnreadable(RuntimeError):
    """The GitHub App cannot read the checks of a commit: it has no read access to `missing`, of "checks" and
    "statuses". The message says what is missing and what to do, for the log."""

    def __init__(self, missing: Sequence[str]) -> None:
        names = " and ".join(NAMES[name] for name in missing)
        super().__init__(
            f"lastcall cannot read the checks: its GitHub App has no read access to {names}. Open the App's "
            f"permissions on GitHub, set {names} to Read-only and save, then accept the new permissions on the "
            "App's installation. The Projects page has the link."
        )


def approved(reviews: Sequence[Mapping[str, Any]], head: str) -> bool:
    """Whether someone who may write approved `head`, and nobody who may write asks for changes. Each person counts
    with their latest review."""
    latest: dict[str, Mapping[str, Any]] = {}
    for review in reviews:
        if review.get("author_association") in TRUSTED and review.get("state") in (
            "APPROVED",
            "CHANGES_REQUESTED",
            "DISMISSED",
        ):
            latest[str((review.get("user") or {}).get("login"))] = review
    states = list(latest.values())
    approval = any(r["state"] == "APPROVED" and r.get("commit_id") == head for r in states)
    return approval and not any(r["state"] == "CHANGES_REQUESTED" for r in states)


def ensure_readable(client: GitHub, repo: str) -> None:
    """Raise ChecksUnreadable when GitHub says that the App's installation on `repo` has no read access to the checks
    of a commit. When it does not say, go on: `read_checks` tells a refusal."""
    try:
        granted = client.granted(repo)
    except GitHubError:
        return  # the request for the checks meets the same error, and is handled as it always was
    if granted is not None and (missing := lacking(granted)):
        raise ChecksUnreadable(missing)


def read_checks(client: GitHub, token: str, path: str, permission: str) -> Any:
    """What GitHub says at `path`, which needs `permission` (read) of the App: a 403 means that it lacks it, unless it
    is a rate limit."""
    try:
        return client.request("GET", path, token)
    except GitHubError as e:
        if e.status == 403 and "rate limit" not in str(e).lower():
            raise ChecksUnreadable([permission]) from e
        raise


def check_runs(client: GitHub, token: str, repo: str, sha: str) -> list[dict[str, Any]]:
    """The check runs of a commit: GitHub Actions jobs and other apps' checks. They need Checks (read)."""
    found = read_checks(client, token, f"/repos/{repo}/commits/{sha}/check-runs?per_page=100", "checks")
    return list(found.get("check_runs") or [])


def checks(client: GitHub, token: str, repo: str, sha: str) -> Literal["passed", "pending", "failed"]:
    """The checks of a commit, from check runs and from commit statuses."""
    runs = check_runs(client, token, repo, sha)
    if any(r.get("conclusion") in FAILED for r in runs):
        return "failed"
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    status = read_checks(client, token, f"/repos/{repo}/commits/{sha}/status", "statuses")  # Commit statuses (read)
    if status.get("total_count"):
        if status.get("state") in ("failure", "error"):
            return "failed"
        if status.get("state") == "pending":
            return "pending"
    return "passed"


def failing(client: GitHub, token: str, repo: str, sha: str) -> list[str]:
    """The names of the check runs that failed on a commit."""
    return [str(r.get("name")) for r in check_runs(client, token, repo, sha) if r.get("conclusion") in FAILED]


def method(meta: Mapping[str, Any]) -> str:
    """How the repository lets pull requests merge: squash first, since lastcall's pull requests are small."""
    if meta.get("allow_squash_merge", True):
        return "squash"
    return "merge" if meta.get("allow_merge_commit", True) else "rebase"


def land(client: GitHub, token: str, repo: str, pr: Mapping[str, Any], language: str) -> tuple[Outcome, str]:
    """What merge on approve does with an open lastcall pull request, as GitHub returns it: ("merged", the merge
    commit), ("closed", why) when it no longer merges cleanly, or ("waiting", why)."""
    number, head = pr["number"], str(pr["head"]["sha"])
    reviews = client.request("GET", f"/repos/{repo}/pulls/{number}/reviews?per_page=100", token)
    if not approved(reviews, head):
        return "waiting", "not approved on its latest commit"
    if pr.get("mergeable") is None:
        return "waiting", "GitHub is still working out whether it merges"
    if pr.get("mergeable") is False or pr.get("mergeable_state") == "dirty":
        body = i18n.text(language, "pr.conflict", branch=str(pr["base"]["ref"]))
        client.request("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
        client.request("PATCH", f"/repos/{repo}/pulls/{number}", token, {"state": "closed"})
        return "closed", "it no longer merges cleanly"
    if pr.get("mergeable_state") not in ("clean", "unstable", "has_hooks"):
        return "waiting", f"GitHub does not let it merge yet ({pr.get('mergeable_state')})"
    ensure_readable(client, repo)
    state = checks(client, token, repo, head)
    if state != "passed":
        return "waiting", f"its checks {'failed' if state == 'failed' else 'are still running'}"
    try:
        merged = client.request(
            "PUT",
            f"/repos/{repo}/pulls/{number}/merge",
            token,
            {"merge_method": method(client.repository(token, repo)), "sha": head},
        )
    except GitHubError as e:  # branch protection, or a push after the checks
        return "waiting", f"GitHub did not merge it: {e}"
    return "merged", str(merged.get("sha") or "")
