"""Merge on approve: who may approve, what must pass, and what lastcall does with a pull request that no longer
merges."""

from collections.abc import Mapping
from typing import Any, cast

import pytest

from lastcall.approval import ChecksUnreadable, approved, land, method
from lastcall.github import GitHub, GitHubError

REPO = "acme/api"
HEAD = "c0ffee1"
CHECKS = f"/repos/{REPO}/commits/{HEAD}/check-runs?per_page=100"
STATUS = f"/repos/{REPO}/commits/{HEAD}/status"
READS = {"contents": "write", "checks": "read", "statuses": "read"}  # what an App made by v0.7.2 or later was granted


def review(login: str, state: str, association: str = "OWNER", commit: str = HEAD) -> dict[str, Any]:
    return {"user": {"login": login}, "state": state, "author_association": association, "commit_id": commit}


class FakeGitHub:
    """Answers by (method, path); records what lastcall asked for. `permissions` is what the installation was
    granted: None when GitHub does not say, or an error when it cannot be asked."""

    def __init__(
        self, answers: dict[tuple[str, str], Any], permissions: Mapping[str, str] | Exception | None = READS
    ) -> None:
        self.answers = answers
        self.permissions = permissions
        self.calls: list[tuple[str, str, Any]] = []

    def granted(self, repo: str) -> dict[str, str] | None:
        if isinstance(self.permissions, Exception):
            raise self.permissions
        return None if self.permissions is None else dict(self.permissions)

    def request(self, method: str, path: str, token: str, body: object = None) -> Any:
        self.calls.append((method, path, body))
        answer = self.answers[(method, path)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def repository(self, token: str, repo: str) -> dict[str, Any]:
        return {"allow_squash_merge": True}


def pr(**changes: Any) -> dict[str, Any]:
    base = {
        "number": 7,
        "head": {"sha": HEAD},
        "base": {"ref": "main"},
        "mergeable": True,
        "mergeable_state": "clean",
    }
    return {**base, **changes}


def github(
    reviews: list[dict[str, Any]],
    runs: list[dict[str, Any]] | None = None,
    more: dict[tuple[str, str], Any] | None = None,
    permissions: Mapping[str, str] | Exception | None = READS,
) -> FakeGitHub:
    answers: dict[tuple[str, str], Any] = {
        ("GET", f"/repos/{REPO}/pulls/7/reviews?per_page=100"): reviews,
        ("GET", f"/repos/{REPO}/commits/{HEAD}/check-runs?per_page=100"): {"check_runs": runs or []},
        ("GET", f"/repos/{REPO}/commits/{HEAD}/status"): {"total_count": 0, "state": "pending"},
        ("PUT", f"/repos/{REPO}/pulls/7/merge"): {"merged": True, "sha": "merged1"},
        ("POST", f"/repos/{REPO}/issues/7/comments"): {},
        ("PATCH", f"/repos/{REPO}/pulls/7"): {},
    }
    return FakeGitHub({**answers, **(more or {})}, permissions)


def test_who_may_approve() -> None:
    assert approved([review("ne0x86", "APPROVED")], HEAD)
    assert approved([review("teammate", "APPROVED", "COLLABORATOR")], HEAD)
    assert not approved([review("stranger", "APPROVED", "NONE")], HEAD)  # anyone may review a public repository
    assert not approved([review("ne0x86", "APPROVED", commit="0ld")], HEAD)  # a push after the approval
    assert not approved([review("ne0x86", "APPROVED"), review("teammate", "CHANGES_REQUESTED", "MEMBER")], HEAD)
    # each person counts with their latest review
    assert approved(
        [review("teammate", "CHANGES_REQUESTED", "MEMBER"), review("teammate", "APPROVED", "MEMBER")], HEAD
    )
    assert not approved([review("ne0x86", "APPROVED"), review("ne0x86", "DISMISSED")], HEAD)


def test_an_approved_pull_request_with_green_checks_is_merged() -> None:
    client = github(
        [review("ne0x86", "APPROVED")], [{"name": "test", "status": "completed", "conclusion": "success"}]
    )
    assert land(cast(GitHub, client), "t", REPO, pr(), "English") == ("merged", "merged1")
    assert ("PUT", f"/repos/{REPO}/pulls/7/merge", {"merge_method": "squash", "sha": HEAD}) in client.calls


def test_it_waits_for_the_approval_github_and_the_checks() -> None:
    def outcome(client: FakeGitHub, data: dict[str, Any]) -> str:
        found, why = land(cast(GitHub, client), "t", REPO, data, "English")
        assert found == "waiting" and not any(c[0] == "PUT" for c in client.calls)
        return why

    assert outcome(github([]), pr()) == "not approved on its latest commit"
    approval = [review("ne0x86", "APPROVED")]
    assert "still working out" in outcome(github(approval), pr(mergeable=None))
    assert "(blocked)" in outcome(github(approval), pr(mergeable_state="blocked"))
    running = [{"name": "test", "status": "in_progress", "conclusion": None}]
    assert outcome(github(approval, running), pr()) == "its checks are still running"
    failed = [{"name": "test", "status": "completed", "conclusion": "failure"}]
    assert outcome(github(approval, failed), pr()) == "its checks failed"
    status = {("GET", f"/repos/{REPO}/commits/{HEAD}/status"): {"total_count": 1, "state": "failure"}}
    assert outcome(github(approval, None, status), pr()) == "its checks failed"
    refused = {("PUT", f"/repos/{REPO}/pulls/7/merge"): GitHubError(405, "Base branch was modified")}
    assert "did not merge it" in land(cast(GitHub, github(approval, None, refused)), "t", REPO, pr(), "English")[1]


def test_a_pull_request_that_no_longer_merges_is_closed_with_a_word() -> None:
    client = github([review("ne0x86", "APPROVED")])
    assert (
        land(cast(GitHub, client), "t", REPO, pr(mergeable=False, mergeable_state="dirty"), "Spanish")[0] == "closed"
    )
    [comment] = [c for c in client.calls if c[0] == "POST"]
    assert "ya no se fusiona limpio en main" in comment[2]["body"]
    assert ("PATCH", f"/repos/{REPO}/pulls/7", {"state": "closed"}) in client.calls


def test_the_merge_method_follows_the_repository() -> None:
    assert method({"allow_squash_merge": True}) == "squash"
    assert method({"allow_squash_merge": False, "allow_merge_commit": True}) == "merge"
    assert method({"allow_squash_merge": False, "allow_merge_commit": False}) == "rebase"


# an App made before v0.7.2 was not granted Checks and Commit statuses

APPROVAL = [review("ne0x86", "APPROVED")]
GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]


def asked(client: FakeGitHub, *words: str) -> bool:
    """Whether lastcall asked GitHub for a path that has one of the words in it."""
    return any(word in path for _, path, _ in client.calls for word in words)


@pytest.mark.parametrize(
    ("granted", "missing"),
    [
        ({"contents": "write", "metadata": "read"}, "Checks and Commit statuses"),
        ({}, "Checks and Commit statuses"),
        ({"checks": "read"}, "Commit statuses"),
        ({"statuses": "read"}, "Checks"),
    ],
)
def test_an_installation_without_the_checks_is_not_asked_for_them(granted: dict[str, str], missing: str) -> None:
    client = github(APPROVAL, GREEN, permissions=granted)
    with pytest.raises(ChecksUnreadable) as error:
        land(cast(GitHub, client), "t", REPO, pr(), "English")
    said = str(error.value)
    assert f"its GitHub App has no read access to {missing}." in said
    assert f"set {missing} to Read-only and save, then accept the new permissions on the App's installation" in said
    assert said.endswith("The Projects page has the link.")
    assert not asked(client, "check-runs", "/status") and not any(call[0] == "PUT" for call in client.calls)


def test_read_is_enough_and_so_is_write() -> None:
    for granted in ({"checks": "read", "statuses": "read"}, {"checks": "write", "statuses": "write"}):
        client = github(APPROVAL, GREEN, permissions=granted)
        assert land(cast(GitHub, client), "t", REPO, pr(), "English") == ("merged", "merged1")


@pytest.mark.parametrize("permissions", [None, GitHubError(502, "Bad Gateway")])
def test_when_github_does_not_say_what_was_granted_the_checks_are_asked_for(
    permissions: Exception | None,
) -> None:
    client = github(APPROVAL, GREEN, permissions=permissions)
    assert land(cast(GitHub, client), "t", REPO, pr(), "English") == ("merged", "merged1")
    assert asked(client, "check-runs", "/status")


def test_what_comes_before_the_checks_does_not_need_them() -> None:
    bare = {"contents": "write"}
    assert land(cast(GitHub, github([], permissions=bare)), "t", REPO, pr(), "English") == (
        "waiting",
        "not approved on its latest commit",
    )
    client = github(APPROVAL, permissions=bare)
    assert (
        land(cast(GitHub, client), "t", REPO, pr(mergeable=False, mergeable_state="dirty"), "English")[0] == "closed"
    )


@pytest.mark.parametrize(("path", "missing"), [(CHECKS, "Checks"), (STATUS, "Commit statuses")])
@pytest.mark.parametrize("permissions", [READS, None])
def test_a_refusal_names_the_permission_that_the_endpoint_needs(
    path: str, missing: str, permissions: dict[str, str] | None
) -> None:
    refused = {("GET", path): GitHubError(403, "Resource not accessible by integration")}
    client = github(APPROVAL, GREEN, refused, permissions)  # GitHub answers 403 whatever the installation says
    with pytest.raises(ChecksUnreadable, match=rf"no read access to {missing}\. Open the App's permissions"):
        land(cast(GitHub, client), "t", REPO, pr(), "English")
    assert not any(call[0] == "PUT" for call in client.calls)


@pytest.mark.parametrize(
    "error", [GitHubError(403, "API rate limit exceeded for installation ID 7."), GitHubError(502, "Bad Gateway")]
)
def test_any_other_refusal_stays_what_github_said(error: GitHubError) -> None:
    client = github(APPROVAL, GREEN, {("GET", CHECKS): error})
    with pytest.raises(GitHubError) as caught:
        land(cast(GitHub, client), "t", REPO, pr(), "English")
    assert caught.value is error
