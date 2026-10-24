"""git for lastcall's own side: clone with an installation token that never lands in argv or .git/config."""

from __future__ import annotations

import base64
import io
import os
import shutil
import stat
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path


def env(token: str | None) -> dict[str, str]:
    """git's environment: the token travels as an HTTP header in variables, and git never prompts."""
    variables = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        variables |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }
    return variables


def clone(repo: str, dest: Path, token: str | None, branch: str, depth: int = 100) -> str:
    """Clone `branch` of owner/name with recent history. Returns the commit."""
    url = f"https://github.com/{repo}.git"
    subprocess.run(
        ["git", "clone", "-q", "--depth", str(depth), "--branch", branch, url, str(dest)],
        env=env(token),
        check=True,
        capture_output=True,
    )
    return head(dest)


def head(repo: Path) -> str:
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def snapshot(repo: Path, dest: Path) -> None:
    """The versioned files at HEAD, without .git: what reviewers read."""
    data = subprocess.check_output(["git", "-C", str(repo), "archive", "HEAD"])
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(dest, filter="data")


def remove(path: Path) -> None:
    """Delete a working copy. git marks its objects read-only, and Windows refuses to delete those as they are."""

    def writable_then_retry(func: Callable[..., object], target: str, _exc: BaseException) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=writable_then_retry)


def branch_from(clone_dir: Path, dest: Path, branch: str) -> None:
    """A fresh working copy of a local clone, on a new branch: one per pull request."""
    subprocess.run(["git", "clone", "-q", str(clone_dir), str(dest)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(dest), "switch", "-q", "-c", branch], check=True, capture_output=True)


def stage_all(repo: Path) -> str:
    """Stage every change, new files included, and return the diff a commit would carry."""
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached"], check=True, capture_output=True, text=True, errors="replace"
    ).stdout


def files(repo: Path) -> list[str]:
    """Tracked and new files, without what .gitignore excludes: what a clean verification copy gets."""
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-co", "--exclude-standard", "-z"], check=True, capture_output=True
    ).stdout
    return [f for f in out.decode(errors="replace").split("\0") if f]


def commit(repo: Path, message: str, author: tuple[str, str]) -> str:
    name, email = author
    subprocess.run(
        ["git", "-C", str(repo), "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-F", "-"],
        input=message.encode(),
        check=True,
        capture_output=True,
    )
    return head(repo)


def push(repo_dir: Path, repo: str, branch: str, token: str) -> None:
    """Push HEAD to a new branch on GitHub. Only lastcall's side does this; agents never hold a token."""
    url = f"https://github.com/{repo}.git"
    subprocess.run(
        ["git", "-C", str(repo_dir), "push", "-q", url, f"HEAD:refs/heads/{branch}"],
        env=env(token),
        check=True,
        capture_output=True,
    )


def history(repo: Path, n: int = 40) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "log", f"-n{n}", "--date=short", "--format=%ad %s"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return out.strip() or "no history"
