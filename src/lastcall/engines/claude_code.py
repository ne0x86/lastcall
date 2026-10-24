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
    probe_source,
)
from lastcall.quota import QuotaError, Reading, Window, epoch

READ_TOOLS = "Read,Grep,Glob"
WRITE_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"


class ClaudeCode:
    name: str = "claude-code"
    capabilities: frozenset[str] = frozenset({"json_schema", "system_prompt_file", "effort"})
    egress: tuple[str, ...] = ("api.anthropic.com", "platform.claude.com")  # inference; login token exchange
    home: str = "/home/runner/.claude"
    home_env: str = "CLAUDE_CONFIG_DIR"
    login: tuple[str, ...] = ("claude", "auth", "login")  # Claude subscription; the code is pasted back

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
        return Invocation(tuple(argv), stdin=step.prompt, env={self.home_env: self.home}, mounts=mounts)

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

    def quota_invocation(self) -> Invocation:
        """The get_usage control request: plan usage without calling the model (experimental in the CLI)."""
        probe = f"{CONTEXT_DIR}/probe.py"
        env = {
            self.home_env: self.home,
            "NO_COLOR": "1",
            # For the CLI, fetching the plan usage is "nonessential traffic": with the runner's default
            # CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1, get_usage answers rate_limits: null. Telemetry stays off.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
        return Invocation(("python3", probe), env=env, files={probe: probe_source("claude_usage")})

    def quota_reading(self, account: str, lines: Sequence[str], now: float) -> Reading:
        msg = next(
            (m for line in reversed(lines) if (m := json_line(line)) and m.get("type") == "control_response"), None
        )
        if msg is None:
            raise QuotaError("Claude Code gave no usage answer")
        response = msg.get("response") or {}
        if response.get("subtype") != "success":
            raise QuotaError(f"the usage request failed: {response.get('error')}")
        body = response.get("response") or {}
        if body.get("rate_limits_available") is False:  # not signed in, or signed in with an API key
            raise QuotaError("no plan limits: sign in with a Claude subscription (lastcall login), not an API key")
        limits = body.get("rate_limits") or {}
        five, week = limits.get("five_hour") or {}, limits.get("seven_day") or {}
        if week.get("utilization") is None:
            raise QuotaError("the answer has no weekly usage")
        extra = body.get("extra_usage") or limits.get("extra_usage") or {}
        windows = (
            Window("5h", float(five.get("utilization") or 0.0), epoch(five.get("resets_at")), "throttle", 5.0),
            Window("week", float(week["utilization"]), epoch(week.get("resets_at")), "expiring", 168.0),
        )
        return Reading(account, windows, bool(extra.get("is_enabled")), "exact", now)
