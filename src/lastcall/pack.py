"""The context pack of a project, built without any model call: what the project is, its recent history, a map and
its code with line numbers. Every step of a run shares it, and providers cache it (DESIGN.md, section 7).

Scanners, production signals and the memory of earlier runs join the pack in later steps of v0.1.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from lastcall import git
from lastcall.suggest import MAX_FILE, SKIP

CODE_BUDGET = 150_000  # tokens of code in the pack; reviewers read the rest from /work
ABOUT_LINES = 150


@dataclass(frozen=True)
class Pack:
    sha: str
    path: Path  # the shared file: pack and code
    src: Path  # the snapshot that steps get as their workspace
    files: int
    omitted: list[str]
    tokens: int


def code_files(root: Path) -> Iterator[tuple[str, str]]:
    """(relative path, text) of the files worth reading: no vendored, generated, binary or huge files."""
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):  # same order on every OS
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or path.is_symlink() or SKIP.search(rel) or path.stat().st_size > MAX_FILE:
            continue
        raw = path.read_bytes()
        if b"\0" in raw[:4096]:
            continue
        yield rel, raw.decode(errors="replace")


def numbered(text: str) -> str:
    lines = text.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i:>{width}}| {line}" for i, line in enumerate(lines, 1))


def build(clone: Path, run_dir: Path, project: str, budget: int = CODE_BUDGET) -> Pack:
    src = run_dir / "src"
    git.snapshot(clone, src)
    sha = git.head(clone)[:7]
    code, rows, omitted, used = [], [], [], 0
    for rel, text in code_files(src):
        tokens = len(text) // 4
        rows.append(f"- {rel} ({len(text.splitlines())} lines)")
        if used + tokens > budget:
            omitted.append(rel)
            continue
        used += tokens
        code.append(f"### {rel}\n```\n{numbered(text)}\n```\n")
    readme = next((p for p in sorted(src.glob("README*")) if p.is_file()), None)
    about = "\n".join(readme.read_text(errors="replace").splitlines()[:ABOUT_LINES]) if readme else "no README"
    sections = {
        "What the project says about itself (README)": about,
        "Recent history (where the owner is taking it)": git.history(clone),
        "Map (files a review reads)": "\n".join(rows) or "no code files",
    }
    if omitted:
        sections["Not in this pack (over the size budget): read them from /work"] = "\n".join(omitted)
    head = (
        f"# Context pack: {project} @ {sha}\n\nBuilt {time.strftime('%Y-%m-%d %H:%M')}. The repository is in /work, "
        f"read-only. The code with line numbers comes after the pack.\n"
    )
    body = head + "".join(f"\n## {title}\n\n{text}\n" for title, text in sections.items())
    path = run_dir / "shared.md"
    path.write_text(body + "\n# Code (with line numbers)\n\n" + "\n".join(code), encoding="utf-8")
    return Pack(sha, path, src, len(code), omitted, used)
