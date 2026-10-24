"""Claude Code (`claude -p`), with the owner's Claude login or an Anthropic API key.

- The shared context goes through --append-system-prompt-file. The provider caches the system prompt, so the
  steps of a run after the first one read the context from the cache (61% cheaper reviewers in the prototype).
- Reviewers and the judge get read-only tools; the implementer gets edit tools and Bash. The runner enforces the
  rest: read-only mount, egress proxy.
- The CLI prints stream-json events. With a schema, the step's JSON arrives in the result event as
  structured_output.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from lastcall.engines.base import (
    CONTEXT_DIR,
    Event,
    EventKind,
    Invocation,
    StepResult,
    StepSpec,
    count_tools,
    json_line,
    last,
)

READ_TOOLS = "Read,Grep,Glob"
WRITE_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"


class ClaudeCode:
    name: str = "claude-code"
    capabilities: frozenset[str] = frozenset({"json_schema", "system_prompt_file", "effort"})
    egress: tuple[str, ...] = ("api.anthropic.com", "platform.claude.com")  # inference; login token exchange
    home: str = "/home/runner/.claude"

    def invocation(self, step: StepSpec) -> Invocation:
        argv = ["claude", "-p", "--model", step.model, "--output-format", "stream-json", "--verbose"]
        argv += ["--strict-mcp-config"]  # no MCP servers unless a recipe asks for them
        if step.effort:
            argv += ["--effort", step.effort]
        if step.writable:
            argv += ["--dangerously-skip-permissions", "--tools", WRITE_TOOLS]
        else:
            argv += ["--restricted", "--tools", READ_TOOLS]
        mounts: dict[str, Path] = {}
        if step.shared_context is not None:
            mounts[f"{CONTEXT_DIR}/shared.md"] = step.shared_context
            argv += ["--append-system-prompt-file", f"{CONTEXT_DIR}/shared.md"]
        if step.schema is not None:
            argv += ["--json-schema", json.dumps(step.schema)]
        return Invocation(tuple(argv), stdin=step.prompt, env={"CLAUDE_CONFIG_DIR": self.home}, mounts=mounts)

    def parse(self, line: str) -> Event | None:
        ev = json_line(line)
        if ev is None:
            return None
        kind = ev.get("type")
        if kind == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            tools = sum(1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use")
            return Event(EventKind.TOOL, {"count": tools}) if tools else None
        if kind == "rate_limit_event":
            return Event(EventKind.RATE_LIMIT, {"info": (ev.get("rate_limit_info") or {}).get("unifiedWindows")})
        if kind == "result":
            return Event(EventKind.RESULT, ev)
        return None

    def result(self, step: StepSpec, events: Sequence[Event]) -> StepResult:
        res = last(events, EventKind.RESULT)
        limits = last(events, EventKind.RATE_LIMIT)
        if res is None:
            return StepResult(False, tool_uses=count_tools(events))
        output = res.get("structured_output") if step.schema is not None else res.get("result")
        ok = res.get("subtype") == "success" and not res.get("is_error") and output is not None
        error = None if ok else str(res.get("result") or res.get("subtype") or "no output")
        return StepResult(
            ok,
            output if ok else None,
            error,
            tool_uses=count_tools(events),
            turns=res.get("num_turns"),
            cost_usd=res.get("total_cost_usd"),
            usage=res.get("modelUsage") or res.get("usage"),
            rate_limits=limits.get("info") if limits else None,
        )
