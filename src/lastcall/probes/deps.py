"""List a clone's dependencies with their latest versions, for the deps recipe, and print two JSON lines:
{"tool": "osv-scanner", ...} with the known vulnerabilities, and {"tool": "versions", ...} with the direct
dependencies: [{"ecosystem", "name", "version", "latest", "deprecated", "source"}].

osv-scanner reads the dependency files and lists every package (`--all-packages`). A package is direct when a
manifest of the repository names it; deps.dev gives its latest version and whether its authors deprecated it.
Runs inside the runner container, read-only on the clone, through the egress proxy. Standard library only.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".venv", "venv"}
MANIFESTS = re.compile(
    r"^(package\.json|requirements.*\.txt|pyproject\.toml|Pipfile|setup\.cfg|setup\.py|go\.mod|Cargo\.toml|Gemfile"
    r"|.*\.gemspec|.*\.csproj|pom\.xml|build\.gradle(\.kts)?|dockerfile-requirements\.txt)$"
)
SYSTEMS = {"PyPI": "pypi", "npm": "npm", "Go": "go", "crates.io": "cargo", "Maven": "maven", "RubyGems": "rubygems"}
SYSTEMS |= {"NuGet": "nuget"}
MAX_PACKAGES = 80  # each one is a request to deps.dev
DEPS_DEV = "https://api.deps.dev/v3/systems/{system}/packages/{name}"


def run(argv: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout, cwd="/tmp")
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout} s"
    except FileNotFoundError as e:
        return -1, "", str(e)
    return p.returncode, p.stdout, p.stderr


def emit(tool: str, data: Any = None, error: str | None = None) -> None:
    print(json.dumps({"tool": tool, "ok": error is None, "data": data, "error": error}), flush=True)


def manifests(*roots: Path) -> str:
    """The text of every manifest under the roots, to tell the direct dependencies from the others."""
    texts = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            parts = set(path.relative_to(root).parts)
            if (
                path.is_file()
                and not parts & SKIP_DIRS
                and MANIFESTS.match(path.name)
                and path.stat().st_size < 2**20
            ):
                texts.append(path.read_text(errors="replace"))
    return "\n".join(texts)


def named(text: str, name: str) -> bool:
    """Whether a manifest names the package, as a whole word: `requests`, not `requests-oauthlib`. Dashes, dots and
    underscores stand for each other, as in Python's package names."""
    if ":" in name:  # Maven: group:artifact, written as two elements
        name = name.split(":", 1)[1]
    pattern = re.sub(r"\\-|\\\.|_", "[-_.]", re.escape(name))  # escaped "-" and ".", and "_"
    return re.search(rf"(?<![\w.-]){pattern}(?![\w.-])", text, re.IGNORECASE) is not None


def latest(package: dict[str, str]) -> dict[str, Any]:
    url = DEPS_DEV.format(system=package["system"], name=urllib.parse.quote(package["name"], safe=""))
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            versions = json.load(response).get("versions") or []
    except (OSError, ValueError) as e:
        return {**package, "latest": None, "error": str(e)[:200]}
    default = next((v for v in versions if v.get("isDefault")), None)
    current = next((v for v in versions if (v.get("versionKey") or {}).get("version") == package["version"]), None)
    return {
        **package,
        "latest": (default or {}).get("versionKey", {}).get("version"),
        "deprecated": bool((current or {}).get("isDeprecated") or (default or {}).get("isDeprecated")),
        "reason": str((current or default or {}).get("deprecatedReason") or "")[:200],
    }


def main(work: Path, extra: Path | None) -> None:
    dirs = [str(work)] + ([str(extra)] if extra and extra.is_dir() else [])
    code, out, err = run(["osv-scanner", "scan", "source", "-r", *dirs, "--all-packages", "--format", "json"], 600)
    if code == 128:  # no package sources found
        emit("osv-scanner", {"results": []})
        emit("versions", [])
        return
    try:
        found = json.loads(out)  # exit code 1 only means vulnerabilities were found
    except ValueError:
        emit("osv-scanner", error=(err or out)[-500:] or f"exit code {code}")
        emit("versions", error="osv-scanner did not list the packages")
        return
    emit("osv-scanner", found)
    text = manifests(work, *([extra] if extra and extra.is_dir() else []))
    packages: dict[tuple[str, str], dict[str, str]] = {}
    for result in found.get("results") or []:
        source = str((result.get("source") or {}).get("path") or "").removeprefix(f"{work}/")
        for item in result.get("packages") or []:
            info = item.get("package") or {}
            system = SYSTEMS.get(str(info.get("ecosystem")))
            name, version = str(info.get("name") or ""), str(info.get("version") or "")
            if system and name and version and named(text, name):
                found_here = {"system": system, "name": name, "version": version, "source": source}
                packages.setdefault((system, name), found_here)
    with ThreadPoolExecutor(max_workers=8) as pool:
        emit("versions", list(pool.map(latest, list(packages.values())[:MAX_PACKAGES])))


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/work"), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
