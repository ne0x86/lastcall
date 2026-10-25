from pathlib import Path

import pytest

from lastcall.accounts import Account, login_argv
from lastcall.cli import describe
from lastcall.engines.claude_code import ClaudeCode
from lastcall.engines.codex import Codex
from lastcall.quota import HOUR, Reading, Window

NOW = 1_800_000_000.0


def values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_account_volume() -> None:
    assert Account("claude-max", "claude-code").volume == "lastcall-account-claude-max"


@pytest.mark.parametrize("account_id", ["Claude", "-max", "a_b", "x" * 41, ""])
def test_account_ids_are_volume_safe(account_id: str) -> None:
    with pytest.raises(ValueError, match="account ids"):
        Account(account_id, "claude-code")


def test_unknown_engine() -> None:
    with pytest.raises(ValueError, match="unknown engine"):
        Account("main", "gemini")


def test_login_runs_the_official_cli_in_the_sandbox() -> None:
    argv = login_argv(Account("claude-max", "claude-code"), ClaudeCode())
    assert argv[:4] == ["docker", "run", "--rm", "-it"]
    assert values(argv, "--network") == ["lastcall-sandbox"]
    assert values(argv, "--user") == ["1000:1000"]
    assert values(argv, "--cap-drop") == ["ALL"]
    assert values(argv, "--security-opt") == ["no-new-privileges"]
    assert "HTTPS_PROXY=http://egress:8888" in values(argv, "-e")
    assert "CLAUDE_CONFIG_DIR=/home/runner/.claude" in values(argv, "-e")
    assert values(argv, "--mount") == ["type=volume,src=lastcall-account-claude-max,dst=/home/runner/.claude"]
    assert argv[-4:] == ["lastcall-runner:dev", "claude", "auth", "login"]


def test_codex_login_uses_the_device_code() -> None:
    argv = login_argv(Account("chatgpt", "codex"), Codex())
    assert "CODEX_HOME=/home/runner/.codex" in values(argv, "-e")
    assert argv[-3:] == ["codex", "login", "--device-auth"]


def test_describe_a_reading() -> None:
    reading = Reading(
        "claude-max",
        (
            Window("5h", 12, NOW + 2 * HOUR, "throttle", 5.0),
            Window("week", 36, NOW + 100 * HOUR, "expiring", 168.0),
        ),
        False,
        "exact",
        NOW,
    )
    text = describe(reading, NOW)
    assert "week   36.0% used, resets Tue 19 12:00 UTC" in text
    assert "paid usage: off" in text
    assert "burst: no (burst in" in text


def test_the_login_script_knows_every_engine() -> None:
    """compose.yaml's `login` service runs runner/login.sh: each engine must have its login there, with its own
    home variable pointed at the account volume."""
    from lastcall import engines

    script = (Path(__file__).parents[1] / "runner" / "login.sh").read_text(encoding="utf-8")
    for name in engines.BUILTIN:
        engine = engines.get(name)
        branch = script[script.index(f"  {name})") :].split(";;")[0]
        assert f'export {engine.home_env}="$account"' in branch, name
        assert f"exec {' '.join(engine.login)}" in branch, name


def test_describe_a_reading_with_the_window_of_a_model() -> None:
    reading = Reading(
        "claude-max",
        (
            Window("week", 36, NOW + 100 * HOUR, "expiring", 168.0),
            Window("week", 95, NOW + 100 * HOUR, "expiring", 168.0, "Opus"),
        ),
        False,
        "exact",
        NOW,
    )
    text = describe(reading, NOW, ["sonnet", "opus"])
    assert "week       36.0% used" in text and "week Opus  95.0% used" in text  # the labels line up
    assert "burst: no (week Opus window at 95% (reserve 10%))" in text
