import json
from pathlib import Path

import pytest

from lastcall import engines
from lastcall.engines.base import Engine, Event, Keyed, StepSpec
from lastcall.engines.claude_code import ClaudeCode
from lastcall.engines.codex import Codex
from lastcall.engines.opencode import OpenCode

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


# OpenCode


def test_opencode_reviewer_has_read_only_tools_and_the_schema_in_its_prompt(tmp_path: Path) -> None:
    shared = tmp_path / "shared.md"
    shared.write_text("context", encoding="utf-8")
    step = StepSpec("reviewer:bugs", "qwen/qwen3-coder", "Review the code.", "high", shared, SCHEMA)
    inv = OpenCode("openrouter", "sk-or-test").invocation(step)
    assert inv.argv[:3] == ("opencode", "--pure", "run")
    assert value_after(inv.argv, "-m") == "openrouter/qwen/qwen3-coder"
    assert value_after(inv.argv, "--dir") == "/work"
    assert value_after(inv.argv, "--variant") == "high"
    assert "--auto" not in inv.argv
    assert inv.stdin.startswith("context")
    assert "Review the code." in inv.stdin and json.dumps(SCHEMA) in inv.stdin
    config = json.loads(inv.env["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"]["edit"] == config["permission"]["bash"] == config["permission"]["webfetch"] == "deny"
    assert config["provider"]["openrouter"]["options"] == {"apiKey": "{file:/ctx/key}"}
    assert inv.env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert inv.files == {"/ctx/key": "sk-or-test"}  # the key travels only as a read-only file
    assert "sk-or-test" not in " ".join([*inv.argv, *inv.env.values(), inv.stdin])


def test_opencode_implementer_works_alone_and_a_local_model_needs_no_key() -> None:
    inv = OpenCode("ollama").invocation(StepSpec("implementer", "qwen3-coder", "Fix it.", writable=True))
    assert "--auto" in inv.argv
    assert "--variant" not in inv.argv
    config = json.loads(inv.env["OPENCODE_CONFIG_CONTENT"])
    assert "edit" not in config["permission"] and config["permission"]["task"] == "deny"
    local = config["provider"]["ollama"]
    assert local["npm"] == "@ai-sdk/openai-compatible"
    assert local["options"] == {"baseURL": "http://ollama:11434/v1"}
    assert local["models"] == {"qwen3-coder": {"name": "qwen3-coder"}}
    assert inv.files == {}
    assert "Output format" not in inv.stdin


def test_opencode_success() -> None:
    """Recorded from opencode-ai 1.19.4, against a stand-in for the provider's API."""
    engine = OpenCode()
    res = engine.result(reviewer(), events(engine, "opencode_success.jsonl"))
    assert res.ok
    assert res.output == {"summary": "nothing to report", "findings": []}
    assert res.tool_uses == 1
    assert res.turns == 2
    assert res.cost_usd == 0
    assert res.usage == {"input": 2468, "output": 112, "cache_read": 0}


def test_opencode_answers_in_a_fence_in_prose_and_errors() -> None:
    engine = OpenCode()

    def said(*lines: dict[str, object]) -> list[Event]:
        return [ev for line in lines if (ev := engine.parse(json.dumps(line))) is not None]

    fenced = said({"type": "text", "part": {"text": '```json\n{"summary": "x", "findings": []}\n```'}})
    assert engine.result(reviewer(), fenced).output == {"summary": "x", "findings": []}
    prose = said({"type": "text", "part": {"text": "I found nothing."}})
    res = engine.result(reviewer(), prose)
    assert not res.ok and "not valid JSON" in (res.error or "")
    assert engine.result(StepSpec("implementer", "m", "Fix it.", writable=True), prose).output == "I found nothing."
    failed = engine.result(reviewer(), said({"type": "error", "error": {"name": "ProviderAuthError"}}))
    assert not failed.ok and "ProviderAuthError" in (failed.error or "")


def test_opencode_providers() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        OpenCode("nope")
    engine = engines.get("opencode")
    assert isinstance(engine, Keyed) and not engine.login
    assert [p.name for p in engine.providers] == ["openrouter", "anthropic", "openai", "ollama"]
    assert [p.name for p in engine.providers if not p.keys] == ["ollama"]  # a local server needs no key


# registry


def test_builtin_engines() -> None:
    assert engines.get("claude-code").name == "claude-code"
    assert engines.get("codex").name == "codex"
    with pytest.raises(ValueError, match="unknown engine"):
        engines.get("nope")
