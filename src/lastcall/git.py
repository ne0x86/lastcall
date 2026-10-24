"""git for lastcall's own side: clone with an installation token that never lands in argv or .git/config."""

from __future__ import annotations

import base64
import io
import os
import subprocess
import tarfile
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


def history(repo: Path, n: int = 40) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "log", f"-n{n}", "--date=short", "--format=%ad %s"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return out.strip() or "no history"
