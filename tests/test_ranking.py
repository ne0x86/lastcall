from typing import Any

from lastcall.ranking import DAY, rank
from lastcall.store import ProjectRow

NOW = 1_800_000_000.0


def project(repo: str, weight: float = 1, active: bool = True) -> ProjectRow:
    return ProjectRow(repo, active, weight, None, 3)


def ranked(
    projects: list[ProjectRow],
    runs: dict[str, dict[str, Any]] | None = None,
    prs: dict[str, int] | None = None,
    pending: dict[str, int] | None = None,
    commits: dict[str, int] | None = None,
    busy: set[str] | None = None,
) -> list[tuple[str, float]]:
    found = rank(
        projects,
        last_run=lambda r: (runs or {}).get(r),
        open_prs=lambda r: (prs or {}).get(r, 0),
        pending=lambda r: (pending or {}).get(r, 0),
        new_commits=lambda r, sha: (commits or {}).get(r),
        busy=busy or set(),
        now=NOW,
    )
    return [(c.repo, c.score) for c in found]


def test_never_audited_goes_first() -> None:
    runs = {"o/old": {"started": NOW - 10 * DAY, "sha": "abc1234"}}
    assert ranked([project("o/old"), project("o/new")], runs=runs) == [("o/new", 100.0), ("o/old", 20.0)]


def test_recent_audits_busy_projects_and_review_backlogs_wait() -> None:
    runs = {"o/recent": {"started": NOW - 2 * DAY, "sha": "a"}}
    projects = [project("o/recent"), project("o/busy"), project("o/backlog"), project("o/off", active=False)]
    assert ranked(projects, runs=runs, prs={"o/backlog": 2}, busy={"o/busy"}) == []


def test_commits_pending_findings_and_weight() -> None:
    runs = {"o/a": {"started": NOW - 30 * DAY, "sha": "a"}, "o/b": {"started": NOW - 30 * DAY, "sha": "b"}}
    result = ranked(
        [project("o/a"), project("o/b", weight=2)],
        runs=runs,
        pending={"o/a": 10},
        commits={"o/a": 4, "o/b": 20},
        prs={"o/b": 1},
    )
    # o/a: 30 (days, capped) + 12 (commits) + 25 (pending, capped) = 67
    # o/b: (30 + 30 (commits, capped) - 20 (one PR to review)) x 2 = 80
    assert result == [("o/b", 80.0), ("o/a", 67.0)]


def test_weight_zero_is_never() -> None:
    assert ranked([project("o/a", weight=0)]) == []


def test_ideas_the_owner_chose_go_first() -> None:
    runs = {"o/chosen": {"started": NOW - 2 * DAY, "sha": "a"}, "o/other": {"started": NOW - 30 * DAY, "sha": "b"}}
    found = rank(
        [project("o/chosen"), project("o/other")],
        last_run=lambda r: runs.get(r),
        open_prs=lambda r: 0,
        pending=lambda r: 0,
        new_commits=lambda r, sha: None,
        busy=set(),
        now=NOW,
        wanted=lambda r: 2 if r == "o/chosen" else 0,
    )
    assert [c.repo for c in found] == ["o/chosen", "o/other"]  # audited 2 days ago, and it still goes first
    assert "2 asked for by the owner" in found[0].reasons
