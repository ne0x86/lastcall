import json
from pathlib import Path

import pytest

from lastcall import engines
from lastcall.engines.base import Engine, Event, StepSpec
from lastcall.engines.claude_code import ClaudeCode
from lastcall.engines.codex import Codex

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = {"type": "object", "required": ["summary", "findings"]}


def events(engine: Engine, fixture: str) -> list[Event]:
    lines = (FIXTURES / fixture).read_text(encoding="utf-8").splitlines()
    return [ev for line in lines if (ev := engine.parse(line)) is not None]


def value_after(argv: tuple[str, ...], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def reviewer(shared: Path | None = None) -> StepSpec:
    return StepSpec("reviewer:bugs", "sonnet", "Review the code.", "high", shared, SCHEMA)


# Claude Code


def test_claude_reviewer_is_read_only_and_shares_the_cached_context(tmp_path: Path) -> None:
    shared = tmp_path / "shared.md"
    shared.write_text("context", encoding="utf-8")
    inv = ClaudeCode().invocation(reviewer(shared))
    assert inv.argv[:2] == ("claude", "-p")
    assert value_after(inv.argv, "--tools") == "Read,Grep,Glob"
    assert "--dangerously-skip-permissions" not in inv.argv
    assert value_after(inv.argv, "--append-system-prompt-file") == "/ctx/shared.md"
    assert inv.mounts == {"/ctx/shared.md": shared}
    assert json.loads(value_after(inv.argv, "--json-schema")) == SCHEMA
    assert value_after(inv.argv, "--effort") == "high"
    assert inv.stdin == "Review the code."


def test_claude_implementer_can_edit() -> None:
    inv = ClaudeCode().invocation(StepSpec("implementer", "opus", "Fix it.", writable=True))
    assert value_after(inv.argv, "--tools") == "Read,Grep,Glob,Edit,Write,Bash"
    assert "--dangerously-skip-permissions" in inv.argv
    assert "--json-schema" not in inv.argv


def test_claude_success() -> None:
    engine = ClaudeCode()
    res = engine.result(reviewer(), events(engine, "claude_code_success.jsonl"))
    assert res.ok
    assert res.output["findings"][0]["title"] == "Retry without backoff"
    assert res.tool_uses == 3
    assert res.turns == 4
    assert res.cost_usd == 0.42
    assert res.rate_limits["seven_day"]["utilization"] == 40


def test_claude_error() -> None:
    engine = ClaudeCode()
    res = engine.result(reviewer(), events(engine, "claude_code_error.jsonl"))
    assert not res.ok
    assert res.output is None
    assert res.error == "Reached maximum number of turns"
    assert res.tool_uses == 1


def test_claude_without_result_event() -> None:
    assert not ClaudeCode().result(reviewer(), []).ok


# Codex


def test_codex_puts_the_shared_context_first_and_reads_stdin(tmp_path: Path) -> None:
    shared = tmp_path / "shared.md"
    shared.write_text("context", encoding="utf-8")
    inv = Codex().invocation(reviewer(shared))
    assert inv.argv[:2] == ("codex", "exec")
    assert inv.argv[-1] == "-"
    assert inv.stdin.startswith("context")
    assert inv.stdin.endswith("Review the code.")
    assert json.loads(inv.files[value_after(inv.argv, "--output-schema")]) == SCHEMA
    assert "model_reasoning_effort=high" in inv.argv
    assert "agents.enabled=false" in inv.argv


def test_codex_success() -> None:
    engine = Codex()
    res = engine.result(reviewer(), events(engine, "codex_success.jsonl"))
    assert res.ok
    assert res.output == {"summary": "One bug.", "findings": [{"title": "Retry without backoff"}]}
    assert res.tool_uses == 2
    assert res.usage["cached_input_tokens"] == 4000


def test_codex_output_that_is_not_json() -> None:
    engine = Codex()
    res = engine.result(reviewer(), events(engine, "codex_bad_output.jsonl"))
    assert not res.ok
    assert res.error is not None
    assert "not valid JSON" in res.error


# registry


def test_builtin_engines() -> None:
    assert engines.get("claude-code").name == "claude-code"
    assert engines.get("codex").name == "codex"
    with pytest.raises(ValueError, match="unknown engine"):
        engines.get("nope")
