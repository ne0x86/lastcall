"""Where a project's checks run, and what its owner says about it (DESIGN.md, section 10).

lastcall looks, in this order, at:
1. `.lastcall.yml` in the repository: the test command, the environment (an image and the setup before the tests),
   the paths the context pack leaves out, and what matters most;
2. a devcontainer (`.devcontainer/devcontainer.json`): its image and its create commands;
3. the project's own files: Go (go.mod), Rust (Cargo.toml) and Java with Maven (pom.xml) run in their official
   images; Python and Node.js run in the runner, with the commands of lastcall.verify.

The setup and the tests run in a container of that image with the isolation of a step: user 1000, no capabilities,
the sandbox network with the package registries, and never an account's credentials.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SETTINGS = (".lastcall.yml", ".lastcall.yaml")
DEVCONTAINERS = (".devcontainer/devcontainer.json", ".devcontainer.json")
CREATE_COMMANDS = ("onCreateCommand", "updateContentCommand", "postCreateCommand")
IMAGE = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::\d{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?(?:@sha256:[a-f0-9]{64})?"
)
GO_VERSION = re.compile(r"^go\s+(\d+\.\d+)", re.MULTILINE)
RUST_VERSION = re.compile(r'^rust-version\s*=\s*"(\d+\.\d+)', re.MULTILINE)
JAVA_VERSION = re.compile(r"<(?:maven\.compiler\.release|maven\.compiler\.target|java\.version)>\s*(?:1\.)?(\d+)\s*<")
# Writable homes and caches for tools that run as user 1000 in images built for root.
TOOL_ENV = {
    "HOME": "/tmp/home",
    "GOPATH": "/tmp/go",
    "GOCACHE": "/tmp/go-build",
    "CARGO_HOME": "/tmp/cargo",
    "GRADLE_USER_HOME": "/tmp/gradle",
}
# Maven reads its proxy only from settings.xml: this writes one from HTTPS_PROXY, which the sandbox always sets.
MAVEN_PROXY = (
    'p="${HTTPS_PROXY#http://}"; p="${p%/}"; mkdir -p "$HOME/.m2" && printf \''
    "<settings><proxies>"
    "<proxy><id>https</id><active>true</active><protocol>https</protocol><host>%s</host><port>%s</port></proxy>"
    "<proxy><id>http</id><active>true</active><protocol>http</protocol><host>%s</host><port>%s</port></proxy>"
    '</proxies></settings>\' "${p%:*}" "${p##*:}" "${p%:*}" "${p##*:}" > "$HOME/.m2/settings.xml"'
)


class ConfigError(ValueError):
    """A `.lastcall.yml` that lastcall cannot use. The run stops before spending anything and says why."""


@dataclass(frozen=True)
class Settings:
    """What `.lastcall.yml` says."""

    test: str | None = None
    image: str | None = None
    setup: str = ""
    exclude: tuple[str, ...] = ()
    focus: str = ""


@dataclass(frozen=True)
class Environment:
    image: str | None  # None: the runner image
    setup: str  # runs before the tests; "" when there is nothing to prepare
    test: str | None  # None: lastcall.verify works it out, in the runner
    source: str  # what decided it: ".lastcall.yml", a devcontainer, "go.mod"... or "runner"
    note: str = ""  # what lastcall saw but could not use

    @property
    def name(self) -> str:
        return self.image or "runner"

    def script(self, command: str) -> str:
        """The setup and then `command`, as one shell script that stops at the first failure."""
        return f"set -e\n{self.setup}\n{command}" if self.setup else command


RUNNER = Environment(None, "", None, "runner")


def settings(root: Path) -> Settings | None:
    """The repository's `.lastcall.yml`, or None when it has none. Raises ConfigError when it is wrong."""
    path = next((root / name for name in SETTINGS if (root / name).is_file()), None)
    if path is None:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name} is not valid YAML: {e}") from e
    if data is None:
        return Settings()
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a mapping of keys, such as `test: pytest -q`")
    known = {"test", "environment", "exclude", "focus"}
    unknown = sorted(str(k) for k in data if k not in known)
    if unknown:
        raise ConfigError(f"unknown key in {path.name}: {', '.join(unknown)} (known: {', '.join(sorted(known))})")
    env = data.get("environment") or {}
    if not isinstance(env, dict) or set(env) - {"image", "setup"}:
        raise ConfigError(f"`environment` in {path.name} takes `image` and `setup`")
    image = _string(env.get("image"), "environment.image")
    if image is not None and not IMAGE.fullmatch(image):
        raise ConfigError(f"`environment.image` is not an image reference: {image!r}")
    exclude = data.get("exclude") or []
    if isinstance(exclude, str):
        exclude = [exclude]
    if not isinstance(exclude, list) or not all(isinstance(e, str) and e.strip() for e in exclude):
        raise ConfigError("`exclude` is a list of paths, such as `[vendor/, testdata/]`")
    return Settings(
        test=_string(data.get("test"), "test"),
        image=image,
        setup=_commands(env.get("setup"), "environment.setup"),
        exclude=tuple(e.strip() for e in exclude),
        focus=_string(data.get("focus"), "focus") or "",
    )


def detect(root: Path, own: Settings | None = None) -> Environment:
    """Where the project's checks run."""
    if own and (own.image or own.test or own.setup):
        return Environment(own.image, own.setup, own.test, ".lastcall.yml")
    container = devcontainer(root)
    if container and container.image:
        return container
    found = _by_files(root) or RUNNER
    return Environment(found.image, found.setup, found.test, found.source, container.note if container else "")


def devcontainer(root: Path) -> Environment | None:
    path = next((root / name for name in DEVCONTAINERS if (root / name).is_file()), None)
    if path is None:
        path = next(iter(sorted(root.glob(".devcontainer/*/devcontainer.json"))), None)
    if path is None:
        return None
    source = path.relative_to(root).as_posix()
    try:
        data = jsonc(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return Environment(None, "", None, source, note=f"{source} is not valid JSON")
    if not isinstance(data, dict):
        return Environment(None, "", None, source, note=f"{source} is not a JSON object")
    image = data.get("image")
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        note = (
            f"{source} builds its own image, and lastcall cannot build images yet: "
            "set `environment.image` in .lastcall.yml"
        )
        return Environment(None, "", None, source, note=note)
    setup = " && ".join(c for name in CREATE_COMMANDS if (c := _devcontainer_command(data.get(name))))
    note = f"{source}: its features are not installed" if data.get("features") else ""
    return Environment(image, setup, None, source, note)


def _by_files(root: Path) -> Environment | None:
    if (root / "go.mod").is_file():
        version = GO_VERSION.search((root / "go.mod").read_text(errors="replace"))
        image = f"golang:{version.group(1)}" if version else "golang:1"
        return Environment(image, "go mod download", "go test ./...", "go.mod")
    if (root / "Cargo.toml").is_file():
        version = RUST_VERSION.search((root / "Cargo.toml").read_text(errors="replace"))
        return Environment(f"rust:{version.group(1) if version else '1'}", "cargo fetch", "cargo test", "Cargo.toml")
    if (root / "pom.xml").is_file():
        version = JAVA_VERSION.search((root / "pom.xml").read_text(errors="replace"))
        java = version.group(1) if version and int(version.group(1)) >= 8 else "21"
        return Environment(f"maven:3-eclipse-temurin-{java}", MAVEN_PROXY, "mvn -B -q test", "pom.xml")
    return None


def _string(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str | int | float) or isinstance(value, bool) or not str(value).strip():
        raise ConfigError(f"`{key}` must be text")
    return str(value).strip()


def _commands(value: Any, key: str) -> str:
    """One command, or a list of them run in order."""
    if value is None:
        return ""
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return " && ".join(v.strip() for v in value if v.strip())
    return _string(value, key) or ""


def _devcontainer_command(value: Any) -> str:
    """A devcontainer command: a shell string, an argv list, or named commands that run in parallel there."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return shlex.join(value)
    if isinstance(value, dict):
        return " && ".join(c for v in value.values() if (c := _devcontainer_command(v)))
    return ""


def jsonc(text: str) -> Any:
    """JSON with comments and trailing commas, as devcontainer.json allows."""
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            in_string = c != '"'
            i += 1
        elif c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            if c in "]}":
                while out and out[-1].isspace():
                    out.pop()
                if out and out[-1] == ",":
                    out.pop()
            out.append(c)
            i += 1
    return json.loads("".join(out))
