import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="t",
    GIT_AUTHOR_EMAIL="t@example.com",
    GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@example.com",
)


@pytest.fixture
def make_repo() -> Callable[[Path, dict[str, str]], Path]:
    """A real git repository with one commit holding `files` (path -> text)."""

    def make(root: Path, files: dict[str, str]) -> Path:
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "First version"]):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=GIT_ENV)
        return root

    return make
