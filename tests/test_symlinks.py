"""What lastcall reads of a repository on its own side never goes through a symbolic link: a link in a repository
could point at the host's files, such as lastcall's secrets. Inside the sandbox, links resolve in the container."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lastcall import environment, pack, verify
from lastcall.environment import RUNNER, ConfigError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")

SECRET = "-----BEGIN PRIVATE KEY-----\nnot for any model\n"


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A file outside the repository, where the host keeps its secrets."""
    secret = tmp_path / "secrets" / "github-app"
    secret.parent.mkdir()
    secret.write_text(SECRET, encoding="utf-8")
    return secret


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def commit(root: Path) -> None:
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "First version"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def test_a_link_out_of_the_repository_neither_fails_the_pack_nor_enters_it(
    repo: Path, host: Path, tmp_path: Path
) -> None:
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    os.symlink(host, repo / "README.md")
    commit(repo)
    run = tmp_path / "run"
    run.mkdir()
    built = pack.build(repo, run, "ne0x86/demo")
    text = built.path.read_text(encoding="utf-8")
    assert "not for any model" not in text
    assert "no README" in text
    assert not (built.src / "README.md").exists()  # the snapshot leaves the link out
    assert (built.src / "app.py").exists()


def test_a_readme_linked_inside_the_repository_is_read(repo: Path, tmp_path: Path) -> None:
    (repo / "docs").mkdir()
    (repo / "docs" / "about.md").write_text("# demo\n\nSends the morning report.\n", encoding="utf-8")
    os.symlink("docs/about.md", repo / "README.md")
    commit(repo)
    run = tmp_path / "run"
    run.mkdir()
    assert "Sends the morning report." in pack.build(repo, run, "ne0x86/demo").path.read_text(encoding="utf-8")


def test_a_settings_file_linked_out_stops_the_run(repo: Path, host: Path) -> None:
    os.symlink(host, repo / ".lastcall.yml")
    with pytest.raises(ConfigError, match="does not lead to a file of the repository"):
        environment.settings(repo)


def test_linked_project_files_are_not_read(repo: Path, host: Path) -> None:
    os.symlink(host, repo / "go.mod")
    (repo / ".devcontainer").mkdir()
    os.symlink(host, repo / ".devcontainer" / "devcontainer.json")
    assert environment.detect(repo) == RUNNER


def test_linked_files_do_not_shape_the_check(repo: Path, host: Path) -> None:
    host.write_text('{"scripts": {"test": "jest"}}\n[project]\nRUN pip install leaked==1.0\n', encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_it():\n    pass\n", encoding="utf-8")
    for name in ("pyproject.toml", "package.json", "Dockerfile"):
        os.symlink(host, repo / name)
    command, kind = verify.check_command(repo)
    assert kind == "tests"
    assert "leaked" not in command and ".[dev]" not in command and "'.'" not in command
