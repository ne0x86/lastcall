import os
import sys
from pathlib import Path
from typing import Any

import pytest

from lastcall.supervisor import Config, Invalid, StepRequest, docker_argv


@pytest.fixture
def config(tmp_path: Path) -> Config:
    (tmp_path / "runs" / "r1" / "src").mkdir(parents=True)
    (tmp_path / "runs" / "r1" / "shared.md").write_text("context", encoding="utf-8")
    return Config(data_root=tmp_path)


def body(**changes: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "engine": "claude-code",
        "argv": ["claude", "-p"],
        "stdin": "Review the code.",
        "env": {"CLAUDE_CONFIG_DIR": "/home/runner/.claude"},
        "files": {"/ctx/schema.json": "{}"},
        "copies": {"/ctx/shared.md": "runs/r1/shared.md"},
        "workspace": "runs/r1/src",
        "writable": False,
        "account_volume": "lastcall-account-claude-max",
        "home": "/home/runner/.claude",
        "timeout": 600,
    }
    return {**request, **changes}


def values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_valid_request(config: Config) -> None:
    req = StepRequest.parse(body(), config)
    assert req.workspace == "runs/r1/src"
    assert req.copies == {"/ctx/shared.md": "runs/r1/shared.md"}
    assert req.timeout == 600.0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"engine": "docker"}, "unknown engine"),
        ({"argv": []}, "argv"),
        ({"stdin": 1}, "stdin"),
        ({"env": {"HTTPS_PROXY": "http://elsewhere:3128"}}, "not allowed"),
        ({"env": {"lower": "x"}}, "not allowed"),
        ({"files": {"/etc/passwd": "x"}}, "under /ctx"),
        ({"files": {"/ctx/../etc/passwd": "x"}}, "under /ctx"),
        ({"files": {"/ctx": "x"}}, "under /ctx"),
        ({"copies": {"/ctx/a": "steps/x/ctx/a"}}, "inside runs/"),
        ({"copies": {"/ctx/a": "runs/r1/missing.md"}}, "no such file"),
        ({"workspace": "runs"}, "inside runs/"),
        ({"workspace": "../etc"}, "inside runs/"),
        ({"workspace": "/data/runs/r1/src"}, "inside runs/"),
        ({"workspace": "runs/r1/src,readonly=false"}, "inside runs/"),  # a comma would add mount options
        ({"workspace": "runs/r1/missing"}, "no such directory"),
        ({"workspace": "runs/r1/shared.md"}, "no such directory"),
        ({"writable": "yes"}, "writable"),
        ({"account_volume": "lastcall-data"}, "not an account volume"),
        ({"account_volume": None}, "go together"),
        ({"home": "/etc"}, "under /home/runner"),
        ({"timeout": 0}, "timeout"),
        ({"timeout": 99_999}, "timeout"),
        ({"timeout": True}, "timeout"),
    ],
)
def test_invalid_requests(config: Config, changes: dict[str, Any], message: str) -> None:
    with pytest.raises(Invalid, match=message):
        StepRequest.parse(body(**changes), config)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_out_of_runs_is_refused(config: Config, tmp_path: Path) -> None:
    (tmp_path / "steps").mkdir()
    os.symlink(tmp_path / "steps", tmp_path / "runs" / "r1" / "escape")
    with pytest.raises(Invalid, match="leaves runs/"):
        StepRequest.parse(body(workspace="runs/r1/escape"), config)


def test_docker_argv_applies_the_isolation(config: Config) -> None:
    argv = docker_argv(StepRequest.parse(body(), config), "abc123", config)
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    assert values(argv, "--name") == ["lastcall-step-abc123"]
    assert values(argv, "--network") == ["lastcall-sandbox"]
    assert values(argv, "--user") == ["1000:1000"]
    assert values(argv, "--cap-drop") == ["ALL"]
    assert values(argv, "--security-opt") == ["no-new-privileges"]
    assert "HTTPS_PROXY=http://egress:8888" in values(argv, "-e")
    assert values(argv, "--mount") == [
        "type=volume,src=lastcall-data,dst=/work,volume-subpath=runs/r1/src,readonly",
        "type=volume,src=lastcall-data,dst=/ctx,volume-subpath=steps/abc123/ctx,readonly",
        "type=volume,src=lastcall-account-claude-max,dst=/home/runner/.claude",
    ]
    assert argv[-3:] == ["lastcall-runner:dev", "claude", "-p"]


def test_implementer_gets_a_writable_workspace(config: Config) -> None:
    argv = docker_argv(StepRequest.parse(body(writable=True), config), "abc123", config)
    assert "type=volume,src=lastcall-data,dst=/work,volume-subpath=runs/r1/src" in values(argv, "--mount")


def test_step_without_account(config: Config) -> None:
    argv = docker_argv(StepRequest.parse(body(account_volume=None, home=None), config), "abc123", config)
    assert len(values(argv, "--mount")) == 2


def test_a_check_may_run_in_the_projects_image(config: Config) -> None:
    req = StepRequest.parse(body(image="golang:1.23", account_volume=None, home=None, env={}), config)
    argv = docker_argv(req, "abc", config)
    assert argv[argv.index("-w") + 2] == "golang:1.23"  # the image comes right after the working directory
    assert values(argv, "--user") == ["1000:1000"] and "--cap-drop" in argv  # the same isolation as any step
    runner = docker_argv(StepRequest.parse(body(), config), "abc", config)
    assert runner[runner.index("-w") + 2] == "lastcall-runner:dev"


@pytest.mark.parametrize("image", ["--privileged", "Golang:1", "golang:1 sh", "a/../b", "", 7])
def test_bad_image_references(config: Config, image: object) -> None:
    with pytest.raises(Invalid, match="not an image reference"):
        StepRequest.parse(body(image=image, account_volume=None, home=None), config)


def test_an_environment_image_never_gets_an_account(config: Config) -> None:
    with pytest.raises(Invalid, match="never gets an account volume"):
        StepRequest.parse(body(image="golang:1.23"), config)


def test_every_engine_runs_on_the_runner_image_unless_set_apart() -> None:
    from lastcall import engines

    config = Config.from_env({"LASTCALL_RUNNER_IMAGE": "runner:1", "LASTCALL_RUNNER_IMAGE_CODEX": "runner-codex:1"})
    assert set(config.images) == set(engines.BUILTIN)
    assert config.images["claude-code"] == "runner:1" and config.images["codex"] == "runner-codex:1"
