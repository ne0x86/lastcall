"""How a change is verified: the project's check command, detected from its files, run outside the agent.

The kind says how strong the check is: `tests` runs the suite; `build` only checks that the code compiles, because
the project has no tests; `none` means nothing could be checked. Pull requests say which one applied.

Python dependencies come from requirements files (at the top or one folder down), from pyproject.toml, and from
the packages the project's Dockerfiles install with pip: many projects declare them only there.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Literal

from lastcall import git
from lastcall.i18n import text

Kind = Literal["tests", "build", "none"]
PYTEST = "python -m pytest -q -p no:cacheprovider"
NPM_DEFAULT_TEST = "no test specified"
PIP_INSTALL = re.compile(r"(?:python3? -m )?pip3? install\s+(.*?)(?=&&|\|\||;|$)")
PIP_VALUE_OPTIONS = {
    *("-r", "--requirement", "-c", "--constraint", "-i", "--index-url", "--extra-index-url"),
    *("-f", "--find-links", "-t", "--target", "--prefix", "--root"),
}
NOT_DEPENDENCIES = {"pip", "setuptools", "wheel", "."}


def has_python_tests(root: Path) -> bool:
    return any(root.glob("tests/**/*.py")) or any(root.rglob("test_*.py")) or any(root.rglob("*_test.py"))


def dockerfile_packages(text: str) -> list[str]:
    """Packages that `RUN pip install` lines add, without options, local paths or pip's own tooling."""
    packages: list[str] = []
    for line in re.sub(r"\\\r?\n", " ", text).splitlines():
        if not line.strip().upper().startswith("RUN"):
            continue
        for match in PIP_INSTALL.finditer(line):
            try:
                tokens = shlex.split(match.group(1))
            except ValueError:
                continue
            skip = False
            for token in tokens:
                if skip:
                    skip = False
                elif token in PIP_VALUE_OPTIONS:
                    skip = True
                elif not token.startswith(("-", ".", "/")) and token not in NOT_DEPENDENCIES:
                    packages.append(token)
    return packages


def python_dependencies(root: Path) -> list[str]:
    """pip arguments that install what the project needs to run its tests."""
    requirements = sorted([*root.glob("requirements*.txt"), *root.glob("*/requirements*.txt")])
    packages: list[str] = []
    for dockerfile in sorted([*root.glob("Dockerfile*"), *root.glob("*/Dockerfile*")]):
        if git.repo_file(root, dockerfile.relative_to(root)):
            packages += dockerfile_packages(dockerfile.read_text(errors="replace"))
    files = [arg for path in requirements for arg in ("-r", path.relative_to(root).as_posix())]
    return files + list(dict.fromkeys(packages))


def check_command(root: Path, assume_tests: bool = False) -> tuple[str, Kind]:
    """The command that verifies a change. With `assume_tests`, the command that would run tests the implementer
    adds to a project that has none yet."""
    py = any(root.rglob("*.py"))
    if py and (assume_tests or has_python_tests(root)):
        found = git.repo_file(root, "pyproject.toml")
        pyproject = found.read_text(errors="replace") if found else ""
        targets: list[str] = []
        if "[project]" in pyproject:
            dev = "[project.optional-dependencies]" in pyproject and "dev" in pyproject
            targets.append(".[dev]" if dev else ".")
        targets += python_dependencies(root)
        install = " ".join(shlex.quote(t) for t in [*targets, "pytest"])
        return f"pip install -q --user --no-warn-script-location {install} && {PYTEST}", "tests"
    package = git.repo_file(root, "package.json")
    if package:
        try:
            scripts = json.loads(package.read_text(errors="replace")).get("scripts") or {}
        except ValueError:
            scripts = {}
        test = scripts.get("test") if isinstance(scripts, dict) else None
        if isinstance(test, str) and NPM_DEFAULT_TEST not in test:
            return "npm install --no-audit --no-fund --ignore-scripts && npm test", "tests"
    if py:
        return "python -m compileall -q .", "build"
    return "true", "none"


SUMMARY = re.compile(r"\b\d+ (passed|failed|errors?|skipped|xfailed)\b|^Tests?:\s")


def summary_line(output: str) -> str:
    """The line that sums up a test run (pytest, jest...), or the last line when none does."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return next((line for line in reversed(lines) if SUMMARY.search(line)), lines[-1] if lines else "")


def describe(kind: Kind, last_line: str, language: str = "English", image: str | None = None) -> str:
    """One line for the pull request, in the run's language: what the clean-container check proved, and where."""
    if image and kind == "tests":
        return text(language, "check.tests.image", line=last_line or "passed", image=image)
    return text(language, f"check.{kind}", line=last_line or "passed")
