import os
import subprocess
from pathlib import Path

import pytest

from lastcall import pack


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "README.md").write_text("# demo\n\nSends the morning report.\n", encoding="utf-8")
    (root / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (root / "node_modules" / "x" / "index.js").write_text("vendored()\n", encoding="utf-8")
    (root / "data.bin").write_bytes(b"\0\1\2binary")
    (root / "big.py").write_text("x = 1\n" * 30_000, encoding="utf-8")
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "First version"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)
    return root


def test_pack_holds_what_reviewers_read(repo: Path, tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    p = pack.build(repo, run, "ne0x86/demo")
    text = p.path.read_text(encoding="utf-8")
    assert p.files == 2  # README.md and app.py
    assert "### app.py\n```\n1| def main():\n2|     return 1\n```" in text
    assert "Sends the morning report." in text
    assert "First version" in text
    assert "node_modules" not in text
    assert "data.bin" not in text
    assert "big.py" not in text
    assert (p.src / "app.py").exists()
    assert not (p.src / ".git").exists()
    assert len(p.sha) == 7


def test_code_over_the_budget_is_listed_not_included(repo: Path, tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    p = pack.build(repo, run, "ne0x86/demo", budget=1)
    text = p.path.read_text(encoding="utf-8")
    assert p.files == 0
    assert p.omitted == ["README.md", "app.py"]
    assert "Not in this pack (over the size budget): read them from /work" in text
