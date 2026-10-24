"""Quota readers: the probe scripts against fake CLIs, and the engines' parsing of their answers."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import lastcall.probes
from lastcall.engines.claude_code import ClaudeCode
from lastcall.engines.codex import Codex
from lastcall.quota import QuotaError, epoch

PROBES = Path(lastcall.probes.__file__).parent
FAKES = Path(__file__).parent / "fakes"
NOW = 1_792_800_000.0


def probe(script: str, fake: str, *flags: str) -> tuple[int, list[str]]:
    command = [sys.executable, str(PROBES / script), sys.executable, str(FAKES / fake), *flags]
    p = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    return p.returncode, p.stdout.splitlines()


def control(subtype: str, **rest: object) -> str:
    return json.dumps({"type": "control_response", "response": {"subtype": subtype, "request_id": "x", **rest}})


# Claude Code


def test_claude_probe_lets_the_cli_fetch_the_plan_usage() -> None:
    """With CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 (the runner default) get_usage answers rate_limits: null."""
    env = ClaudeCode().quota_invocation().env
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == ""
    assert env["DISABLE_TELEMETRY"] == "1"
    assert env["DISABLE_ERROR_REPORTING"] == "1"


def test_claude_probe_and_reading() -> None:
    code, lines = probe("claude_usage.py", "fake_claude.py")
    assert code == 0
    assert len(lines) == 1
    reading = ClaudeCode().quota_reading("claude-max", lines, NOW)
    five, week = reading.windows
    assert (five.name, five.used_pct, five.role, five.duration_hours) == ("5h", 12.0, "throttle", 5.0)
    assert (week.name, week.used_pct, week.role) == ("week", 36.0, "expiring")
    assert week.resets_at == epoch("2026-10-26T12:00:00+00:00")
    assert not reading.paid_usage
    assert reading.fidelity == "exact"


def test_claude_probe_reports_the_login_error() -> None:
    code, lines = probe("claude_usage.py", "fake_claude.py", "--not-logged-in")
    assert code == 0
    with pytest.raises(QuotaError, match="Not logged in"):
        ClaudeCode().quota_reading("claude-max", lines, NOW)


def test_claude_probe_when_the_cli_ends_without_answer() -> None:
    code, lines = probe("claude_usage.py", "fake_claude.py", "--silent")
    assert code == 1
    with pytest.raises(QuotaError, match="no usage answer"):
        ClaudeCode().quota_reading("claude-max", lines, NOW)


def test_claude_extra_usage_is_paid_usage() -> None:
    body = {"rate_limits": {"seven_day": {"utilization": 5}}, "extra_usage": {"is_enabled": True}}
    assert ClaudeCode().quota_reading("a", [control("success", response=body)], NOW).paid_usage


def test_claude_api_key_login_has_no_plan_limits() -> None:
    body = {"rate_limits_available": False, "rate_limits": {}}
    with pytest.raises(QuotaError, match="no plan limits"):
        ClaudeCode().quota_reading("a", [control("success", response=body)], NOW)


def test_claude_answer_without_weekly_usage() -> None:
    body = {"rate_limits": {"five_hour": {"utilization": 5}}}
    with pytest.raises(QuotaError, match="no weekly usage"):
        ClaudeCode().quota_reading("a", [control("success", response=body)], NOW)


# Codex


def test_codex_probe_and_reading() -> None:
    code, lines = probe("codex_rate_limits.py", "fake_codex.py")
    assert code == 0
    reading = Codex().quota_reading("chatgpt", lines, NOW)
    five, week = reading.windows
    assert (five.used_pct, five.resets_at, five.duration_hours) == (18.0, 1_793_000_000.0, 5.0)
    assert (week.used_pct, week.role, week.duration_hours) == (41.0, "expiring", 168.0)
    assert not reading.paid_usage


def test_codex_probe_reports_the_login_error() -> None:
    code, lines = probe("codex_rate_limits.py", "fake_codex.py", "--not-logged-in")
    assert code == 0
    with pytest.raises(QuotaError, match="not logged in"):
        Codex().quota_reading("chatgpt", lines, NOW)


def test_codex_credits_are_paid_usage() -> None:
    limits = {"secondary": {"usedPercent": 1}, "credits": {"hasCredits": True}}
    line = json.dumps({"id": 1, "result": {"rateLimits": limits}})
    assert Codex().quota_reading("a", [line], NOW).paid_usage


def test_codex_without_answer() -> None:
    with pytest.raises(QuotaError, match="no rate limits answer"):
        Codex().quota_reading("a", ["not json", json.dumps({"method": "something"})], NOW)


def test_epoch() -> None:
    assert epoch(None) is None
    assert epoch(1_800_000_000) == 1_800_000_000.0
    assert epoch("2026-10-26T12:00:00Z") == epoch("2026-10-26T12:00:00+00:00")
    with pytest.raises(QuotaError):
        epoch(["not", "a", "time"])
