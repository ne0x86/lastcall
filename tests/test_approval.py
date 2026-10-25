"""Merge on approve: who may approve, what must pass, and what lastcall does with a pull request that no longer
merges."""

from typing import Any, cast

from lastcall.approval import approved, land, method
from lastcall.github import GitHub, GitHubError

REPO = "acme/api"
HEAD = "c0ffee1"


def review(login: str, state: str, association: str = "OWNER", commit: str = HEAD) -> dict[str, Any]:
    return {"user": {"login": login}, "state": state, "author_association": association, "commit_id": commit}


class FakeGitHub:
    """Answers by (method, path); records what lastcall asked for."""

    def __init__(self, answers: dict[tuple[str, str], Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, Any]] = []

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
) -> FakeGitHub:
    answers: dict[tuple[str, str], Any] = {
        ("GET", f"/repos/{REPO}/pulls/7/reviews?per_page=100"): reviews,
        ("GET", f"/repos/{REPO}/commits/{HEAD}/check-runs?per_page=100"): {"check_runs": runs or []},
        ("GET", f"/repos/{REPO}/commits/{HEAD}/status"): {"total_count": 0, "state": "pending"},
        ("PUT", f"/repos/{REPO}/pulls/7/merge"): {"merged": True, "sha": "merged1"},
        ("POST", f"/repos/{REPO}/issues/7/comments"): {},
        ("PATCH", f"/repos/{REPO}/pulls/7"): {},
    }
    return FakeGitHub({**answers, **(more or {})})


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
