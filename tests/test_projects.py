from pathlib import Path
from typing import Any, cast

import pytest

from lastcall.github import App, GitHub, GitHubError
from lastcall.projects import installed_name, parse_repo, refresh
from lastcall.store import Store
from lastcall.suggest import explain

NOW = 1_800_000_000.0
APP = App(1, "lastcall-test", "lastcall-test", "acme", "https://github.com/apps/lastcall-test", "not a key")


class FakeGitHub:
    """Two installed repositories; anything else is not installed."""

    def __init__(self) -> None:
        self.app = APP
        self.repos = {
            "acme/api": {
                "full_name": "acme/api",
                "default_branch": "main",
                "language": "Python",
                "pushed_at": "2027-01-10T10:00:00Z",
                "open_issues_count": 1,
            },
            "acme/old-site": {
                "full_name": "acme/old-site",
                "default_branch": "main",
                "language": "Go",
                "archived": True,
            },
        }
        self.files_of = {
            "acme/api": [("src/app.py", 40_000), ("tests/test_app.py", 4_000), (".github/workflows/ci.yml", 300)],
            "acme/old-site": [("main.go", 9_000)],
        }

    def installations(self) -> list[dict[str, Any]]:
        return [{"id": 7}]

    def installation_token(self, installation_id: int) -> str:
        return "token"

    def repositories(self, token: str) -> list[dict[str, Any]]:
        return list(self.repos.values())

    def files(self, token: str, repo: str, ref: str) -> tuple[list[tuple[str, int]], bool]:
        return self.files_of[repo], False

    def repository_token(self, repo: str) -> str:
        if repo.lower() not in self.repos:
            raise GitHubError(404, f"not installed on {repo}")
        return "token"

    def repository(self, token: str, repo: str) -> dict[str, Any]:
        return self.repos[repo.lower()]


@pytest.mark.parametrize(
    ("text", "repo"),
    [
        ("acme/api", "acme/api"),
        ("  https://github.com/acme/api  ", "acme/api"),
        ("github.com/acme/api.git", "acme/api"),
        ("https://www.github.com/acme/api/pull/15?tab=files#x", "acme/api"),
        ("http://GitHub.com/Acme-1/api_v2/", "Acme-1/api_v2"),
        ("https://gitlab.com/acme/api", None),
        ("gitlab.com/acme/api", None),
        ("acme", None),
        ("acme/..", None),
        ("../etc/passwd", None),
        ("", None),
    ],
)
def test_parse_repo(text: str, repo: str | None) -> None:
    assert parse_repo(text) == repo


def test_refresh_keeps_the_suggestions_in_the_store(tmp_path: Path) -> None:
    store = Store(tmp_path / "lastcall.db")
    found = refresh(store, cast(GitHub, FakeGitHub()), NOW)
    assert [s.repo for s in found] == ["acme/api", "acme/old-site"]
    kept = store.suggestions()
    assert kept == sorted(found, key=lambda s: (-s.score, s.repo))
    api = kept[0]
    assert api.suggested and "tests" in api.reasons and "CI" in api.reasons
    assert kept[1].reasons == ("left out: archived",)
    assert store.setting("suggestions_refreshed") == f"{NOW:.0f}"
    refresh(store, cast(GitHub, FakeGitHub()), NOW + 60)  # a refresh replaces the list
    assert len(store.suggestions()) == 2


def test_reasons_in_spanish(tmp_path: Path) -> None:
    store = Store(tmp_path / "lastcall.db")
    refresh(store, cast(GitHub, FakeGitHub()), NOW)
    api = store.suggestions()[0]
    spanish = [explain("es", r) for r in api.why]
    assert spanish[0] == "último push hace 5\u00a0días"
    assert "1 issue abierto" in spanish
    assert explain("es", ("environment", {"language": "Go"})) == (
        "Go: añade un .lastcall.yml con su entorno, o un devcontainer"
    )


def test_installed_name() -> None:
    client = cast(GitHub, FakeGitHub())
    assert installed_name(client, "ACME/API") == "acme/api"
    with pytest.raises(GitHubError):
        installed_name(client, "acme/other")
