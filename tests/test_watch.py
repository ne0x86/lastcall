"""Watch after the merge, and merge on approve as sync runs it: what lastcall merges, watches and proposes to
revert."""

import os
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from lastcall import git
from lastcall.github import GitHub
from lastcall.recipes.audit import AuditReport
from lastcall.store import Store
from lastcall.sync import sync
from lastcall.watch import HEALTH_FAILURES, WATCH_HOURS, watch

REPO = "acme/api"
PR = "https://github.com/acme/api/pull/5"
NOW = time.time()  # merged pull requests are watched from the real clock


class FakeGitHub:
    def __init__(self, answers: dict[tuple[str, str], Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, Any]] = []

    def repository_token(self, repo: str) -> str:
        return "t"

    def repository(self, token: str, repo: str) -> dict[str, Any]:
        return {"allow_squash_merge": True}

    def ensure_label(self, token: str, repo: str, label: str, description: str) -> None:
        pass

    def labelled_issues(self, token: str, repo: str, label: str) -> list[dict[str, Any]]:
        return []

    def request(self, method: str, path: str, token: str, body: object = None) -> Any:
        self.calls.append((method, path, body))
        return self.answers[(method, path)]


def runs(*conclusions: str) -> dict[str, Any]:
    return {
        "check_runs": [{"name": f"job{n}", "status": "completed", "conclusion": c} for n, c in enumerate(conclusions)]
    }


@pytest.fixture
def store(tmp_path: Path) -> Store:
    st = Store(tmp_path / "lastcall.db")
    st.add_project(REPO)
    report = AuditReport(REPO, "abc1234", "claude-code", "English")
    finding = {"title": "T", "severity": "high", "file": "a.py", "lines": "1", "why": "w", "fix": "f"}
    report.findings = [{**finding, "id": "B1"}]
    report.verdicts = [{"id": "B1", "verdict": "valid", "decision": "pr"}]
    report.deliveries = [{"status": "opened", "url": PR, "title": "Guard the input", "ids": ["B1"]}]
    st.record_findings("run1", report)
    st.record_deliveries("run1", report)
    return st


def watched(store: Store, health_url: str | None = None) -> None:
    store.update_project(REPO, active=True, weight=1, language=None, max_prs=3, watch=True, health_url=health_url)
    store.close_pr(PR, True, "merged1", WATCH_HOURS)


def status(store: Store) -> str:
    return str(store.q("SELECT status FROM findings")[0]["status"])


class Reverts:
    def __init__(self, url: str | None = "https://github.com/acme/api/pull/6") -> None:
        self.url = url
        self.asked: list[tuple[str, str, str]] = []

    def __call__(self, client: GitHub, repo: str, pr: Mapping[str, Any], title: str, body: str) -> str | None:
        self.asked.append((repo, title, body))
        return self.url


def test_failing_checks_after_the_merge_bring_a_revert(store: Store) -> None:
    watched(store)
    client = FakeGitHub(
        {
            ("GET", f"/repos/{REPO}/commits/merged1/check-runs?per_page=100"): runs("success", "failure"),
            ("GET", f"/repos/{REPO}/commits/merged1"): {"parents": [{"sha": "before1"}]},
            ("GET", f"/repos/{REPO}/commits/before1/check-runs?per_page=100"): runs("success", "success"),
        }
    )
    reverts = Reverts()
    assert watch(store, cast(GitHub, client), lambda m: None, now=NOW, revert=reverts) == 1
    [(repo, title, body)] = reverts.asked
    assert repo == REPO
    assert title == 'Revert "Guard the input"'
    assert "the checks of the merged commit failed (job1), where they passed before it" in body
    assert store.project(REPO) is not None and not store.project(REPO).active  # type: ignore[union-attr]
    assert [p["url"] for p in store.open_prs(REPO)] == ["https://github.com/acme/api/pull/6"]  # the revert to review
    assert store.watched(NOW) == []  # watched once
    store.close_pr("https://github.com/acme/api/pull/6", True)
    assert status(store) == "reverted"  # the judge will know


def test_checks_that_failed_before_the_merge_are_not_the_changes_fault(store: Store) -> None:
    watched(store)
    client = FakeGitHub(
        {
            ("GET", f"/repos/{REPO}/commits/merged1/check-runs?per_page=100"): runs("failure"),
            ("GET", f"/repos/{REPO}/commits/merged1"): {"parents": [{"sha": "before1"}]},
            ("GET", f"/repos/{REPO}/commits/before1/check-runs?per_page=100"): runs("failure"),
        }
    )
    reverts = Reverts()
    assert watch(store, cast(GitHub, client), lambda m: None, now=NOW, revert=reverts) == 0
    assert reverts.asked == [] and len(store.watched(NOW)) == 1


def test_a_health_address_that_stops_answering(store: Store) -> None:
    watched(store, "https://api.acme.example/health")
    client = FakeGitHub({("GET", f"/repos/{REPO}/commits/merged1/check-runs?per_page=100"): runs("success")})
    reverts = Reverts(url=None)  # it no longer reverts cleanly
    told: list[tuple[str, str | None, dict[str, Any]]] = []

    class Notifier:
        def notify(self, event: str, key: str | None = None, **values: Any) -> bool:
            told.append((event, key, values))
            return True

    for _ in range(1, HEALTH_FAILURES):
        assert (
            watch(store, cast(GitHub, client), lambda m: None, cast(Any, Notifier()), NOW, lambda u: False, reverts)
            == 0
        )
    assert (
        watch(store, cast(GitHub, client), lambda m: None, cast(Any, Notifier()), NOW, lambda u: True, reverts) == 0
    )
    for _ in range(HEALTH_FAILURES):  # an answer resets the count
        found = watch(
            store, cast(GitHub, client), lambda m: None, cast(Any, Notifier()), NOW, lambda u: False, reverts
        )
    assert found == 1
    assert "https://api.acme.example/health stopped answering (3 checks in a row)" in reverts.asked[0][2]
    assert told == [
        (
            "reverted",
            "revert-failed",
            {
                "url": PR,
                "repo": REPO,
                "title": "Guard the input",
                "reason": "https://api.acme.example/health stopped answering (3 checks in a row)",
            },
        )
    ]
    assert store.open_prs(REPO) == [] and store.watched(NOW) == []


def test_nothing_is_watched_after_its_window(store: Store) -> None:
    watched(store)
    assert store.watched(NOW + (WATCH_HOURS + 1) * 3600 + 10**10) == []


def test_sync_merges_what_the_owner_approved_and_watches_it(store: Store) -> None:
    store.update_project(REPO, active=True, weight=1, language=None, max_prs=3, merge_on_approve=True, watch=True)
    head = {
        "number": 5,
        "head": {"sha": "h1"},
        "base": {"ref": "main"},
        "mergeable": True,
        "mergeable_state": "clean",
    }
    client = FakeGitHub(
        {
            ("GET", f"/repos/{REPO}/pulls/5"): {"state": "open", **head},
            ("GET", f"/repos/{REPO}/pulls/5/reviews?per_page=100"): [
                {"user": {"login": "acme"}, "state": "APPROVED", "author_association": "OWNER", "commit_id": "h1"}
            ],
            ("GET", f"/repos/{REPO}/commits/h1/check-runs?per_page=100"): runs("success"),
            ("GET", f"/repos/{REPO}/commits/h1/status"): {"total_count": 0},
            ("PUT", f"/repos/{REPO}/pulls/5/merge"): {"merged": True, "sha": "merged1"},
            ("GET", f"/repos/{REPO}/commits/merged1/check-runs?per_page=100"): runs(),
        }
    )
    told: list[str] = []

    class Notifier:
        def notify(self, event: str, key: str | None = None, **values: Any) -> bool:
            told.append(event)
            return True

    assert sync(store, cast(GitHub, client), lambda m: None, cast(Any, Notifier())) == 1
    assert status(store) == "merged" and told == ["merged"]
    [under_watch] = store.watched(NOW)
    assert under_watch["merge_sha"] == "merged1"


def test_sync_reopens_what_no_longer_merges(store: Store) -> None:
    store.update_project(REPO, active=True, weight=1, language=None, max_prs=3, merge_on_approve=True)
    head = {
        "number": 5,
        "head": {"sha": "h1"},
        "base": {"ref": "main"},
        "mergeable": False,
        "mergeable_state": "dirty",
    }
    client = FakeGitHub(
        {
            ("GET", f"/repos/{REPO}/pulls/5"): {"state": "open", **head},
            ("GET", f"/repos/{REPO}/pulls/5/reviews?per_page=100"): [
                {"user": {"login": "acme"}, "state": "APPROVED", "author_association": "OWNER", "commit_id": "h1"}
            ],
            ("POST", f"/repos/{REPO}/issues/5/comments"): {},
            ("PATCH", f"/repos/{REPO}/pulls/5"): {},
        }
    )
    assert sync(store, cast(GitHub, client), lambda m: None) == 1
    assert status(store) == "pending" and store.open_prs(REPO) == []  # a later run makes the change again


def test_without_merge_on_approve_sync_only_reads(store: Store) -> None:
    client = FakeGitHub({("GET", f"/repos/{REPO}/pulls/5"): {"state": "open", "number": 5}})
    assert sync(store, cast(GitHub, client), lambda m: None) == 0
    assert [c[0] for c in client.calls] == ["GET"]


# the revert itself, in a real repository

GIT = dict(
    os.environ,
    GIT_AUTHOR_NAME="t",
    GIT_AUTHOR_EMAIL="t@example.com",
    GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@example.com",
)
BOT = ("lastcall-acme[bot]", "1+lastcall-acme[bot]@users.noreply.github.com")


def run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=GIT).stdout


def test_git_reverts_a_squashed_change_and_a_merge_commit(
    tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]
) -> None:
    repo = make_repo(tmp_path / "repo", {"app.py": "one\n"})
    (repo / "app.py").write_text("two\n", encoding="utf-8")
    run(repo, "commit", "-qam", "Squashed change")
    squashed = run(repo, "rev-parse", "HEAD").strip()
    assert git.revert(repo, squashed, "lastcall/revert-1", BOT)
    assert (repo / "app.py").read_text(encoding="utf-8") == "one\n"
    assert run(repo, "log", "-1", "--format=%an").strip() == BOT[0]

    run(repo, "switch", "-q", "main")
    run(repo, "switch", "-q", "-c", "feature")
    (repo / "lib.py").write_text("new\n", encoding="utf-8")
    run(repo, "add", "lib.py")
    run(repo, "commit", "-qm", "Feature")
    run(repo, "switch", "-q", "main")
    run(repo, "merge", "-q", "--no-ff", "-m", "Merge the feature", "feature")
    merge = run(repo, "rev-parse", "HEAD").strip()
    assert git.revert(repo, merge, "lastcall/revert-2", BOT)
    assert not (repo / "lib.py").exists()


def test_git_reports_a_change_that_no_longer_reverts(
    tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]
) -> None:
    repo = make_repo(tmp_path / "repo", {"app.py": "one\n"})
    (repo / "app.py").write_text("two\n", encoding="utf-8")
    run(repo, "commit", "-qam", "Change")
    change = run(repo, "rev-parse", "HEAD").strip()
    (repo / "app.py").write_text("three\n", encoding="utf-8")
    run(repo, "commit", "-qam", "Later change on the same line")
    assert not git.revert(repo, change, "lastcall/revert-1", BOT)
    assert (repo / "app.py").read_text(encoding="utf-8") == "three\n"
    assert run(repo, "status", "--porcelain") == ""
