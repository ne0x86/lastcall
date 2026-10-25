import shutil
import subprocess
from pathlib import Path

import pytest

from lastcall.environment import MAVEN_PROXY, RUNNER, ConfigError, Environment, Settings, detect, jsonc, settings


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def test_no_settings_file(tmp_path: Path) -> None:
    assert settings(tmp_path) is None
    assert settings(write(tmp_path, {".lastcall.yml": ""})) == Settings()


def test_settings(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            ".lastcall.yml": (
                "test: go test ./...\n"
                "environment:\n"
                "  image: golang:1.23\n"
                "  setup:\n"
                "    - go mod download\n"
                "    - go generate ./...\n"
                "exclude: vendor/\n"
                "focus: The HTTP handlers matter most.\n"
            )
        },
    )
    assert settings(tmp_path) == Settings(
        test="go test ./...",
        image="golang:1.23",
        setup="go mod download && go generate ./...",
        exclude=("vendor/",),
        focus="The HTTP handlers matter most.",
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("tests: pytest\n", "unknown key in .lastcall.yml: tests"),
        ("- test\n", "must be a mapping"),
        ("test: [a, b]\n", "`test` must be text"),
        ("environment:\n  image: golang\n  run: x\n", "takes `image` and `setup`"),
        ("environment:\n  image: '--privileged'\n", "not an image reference"),
        ("environment:\n  image: Golang:1\n", "not an image reference"),
        ("exclude: [1, 2]\n", "`exclude` is a list of paths"),
        ("reviewers: bugs\n", "`reviewers` is a list of reviewer names"),
        ("test: [unclosed\n", "not valid YAML"),
    ],
)
def test_settings_that_are_wrong(tmp_path: Path, text: str, message: str) -> None:
    write(tmp_path, {".lastcall.yml": text})
    with pytest.raises(ConfigError, match=message):
        settings(tmp_path)


def test_the_settings_file_decides_first(tmp_path: Path) -> None:
    write(tmp_path, {"go.mod": "module x\n\ngo 1.22\n", ".devcontainer/devcontainer.json": '{"image": "debian:12"}'})
    own = Settings(test="make test", image="debian:12")
    assert detect(tmp_path, own) == Environment("debian:12", "", "make test", ".lastcall.yml")
    # settings with only a focus or exclusions leave the environment to the next source
    assert detect(tmp_path, Settings(focus="x")).source == ".devcontainer/devcontainer.json"


def test_a_devcontainer_with_an_image(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            ".devcontainer/devcontainer.json": """{
  // the Go image of the dev containers project
  "image": "mcr.microsoft.com/devcontainers/go:1-1.22", /* pinned */
  "onCreateCommand": "go mod download",
  "postCreateCommand": ["go", "install", "golang.org/x/tools/gopls@latest"],
  "features": {"ghcr.io/devcontainers/features/node:1": {}},
}"""
        },
    )
    env = detect(tmp_path)
    assert env.image == "mcr.microsoft.com/devcontainers/go:1-1.22"
    assert env.setup == "go mod download && go install golang.org/x/tools/gopls@latest"
    assert env.test is None and env.source == ".devcontainer/devcontainer.json"
    assert "features are not installed" in env.note


def test_a_devcontainer_that_builds_its_image_falls_back(tmp_path: Path) -> None:
    write(tmp_path, {".devcontainer.json": '{"build": {"dockerfile": "Dockerfile"}}', "Cargo.toml": "[package]\n"})
    env = detect(tmp_path)
    assert (env.image, env.test, env.source) == ("rust:1", "cargo test", "Cargo.toml")
    assert "cannot build images yet" in env.note


@pytest.mark.parametrize(
    ("files", "image", "test"),
    [
        ({"go.mod": "module x\n\ngo 1.22.3\n"}, "golang:1.22", "go test ./..."),
        ({"go.mod": "module x\n"}, "golang:1", "go test ./..."),
        ({"Cargo.toml": '[package]\nrust-version = "1.79"\n'}, "rust:1.79", "cargo test"),
        (
            {"pom.xml": "<project><properties><maven.compiler.release>17</maven.compiler.release></properties>"},
            "maven:3-eclipse-temurin-17",
            "mvn -B -q test",
        ),
        ({"pom.xml": "<project/>"}, "maven:3-eclipse-temurin-21", "mvn -B -q test"),
    ],
)
def test_known_projects_get_their_official_image(
    tmp_path: Path, files: dict[str, str], image: str, test: str
) -> None:
    env = detect(write(tmp_path, files))
    assert (env.image, env.test) == (image, test)


def test_python_and_node_stay_in_the_runner(tmp_path: Path) -> None:
    assert detect(write(tmp_path, {"app.py": "", "package.json": "{}"})) == RUNNER


def test_scripts() -> None:
    assert RUNNER.script("pytest") == "pytest"
    env = Environment("golang:1", "go mod download", "go test ./...", "go.mod")
    assert env.script("go test ./...") == "set -e\ngo mod download\ngo test ./..."


def test_jsonc() -> None:
    text = '{"a": "keep // this", "b": "and /* this */", "c": "say \\"hi\\" // still a string",\n "d": [1, 2,],}'
    assert jsonc(text) == {"a": "keep // this", "b": "and /* this */", "c": 'say "hi" // still a string', "d": [1, 2]}


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_maven_reads_the_sandbox_proxy(tmp_path: Path) -> None:
    env = {"HTTPS_PROXY": "http://egress:8888", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    subprocess.run(["sh", "-c", MAVEN_PROXY], env=env, check=True)
    xml = (tmp_path / ".m2" / "settings.xml").read_text(encoding="utf-8")
    assert "<protocol>https</protocol><host>egress</host><port>8888</port>" in xml
    assert "<protocol>http</protocol><host>egress</host><port>8888</port>" in xml


def test_settings_choose_reviewers(tmp_path: Path) -> None:
    found = settings(write(tmp_path, {".lastcall.yml": "reviewers: [bugs, security]\n"}))
    assert found == Settings(reviewers=("bugs", "security"))
