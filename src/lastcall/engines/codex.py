"""Codex (`codex exec`), with the owner's ChatGPT login, a Codex access token or an OpenAI API key.

- The shared context goes at the start of the message: OpenAI caches a common prompt prefix on its own.
- Codex runs with approvals and its own sandbox turned off. Its sandbox does not work inside a container, and the
  runner is the sandbox anyway: read-only mount for reviewers, egress proxy. Subagents stay off.
- The CLI prints JSON events. The step's output is the text of the last agent message.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from lastcall.engines.base import (
    CONTEXT_DIR,
    WORKDIR,
    Event,
    EventKind,
    Invocation,
    StepResult,
    StepSpec,
    count_tools,
    json_line,
    last,
)

TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


class Codex:
    name: str = "codex"
    capabilities: frozenset[str] = frozenset({"json_schema", "effort"})
    egress: tuple[str, ...] = ("api.openai.com", "auth.openai.com", "*.chatgpt.com")
    home: str = "/home/runner/.codex"

    def invocation(self, step: StepSpec) -> Invocation:
        argv = ["codex", "exec", "-m", step.model, "-c", "agents.enabled=false"]
        if step.effort:
            argv += ["-c", f"model_reasoning_effort={step.effort}"]
        files: dict[str, str] = {}
        if step.schema is not None:
            files[f"{CONTEXT_DIR}/schema.json"] = json.dumps(step.schema)
            argv += ["--output-schema", f"{CONTEXT_DIR}/schema.json"]
        argv += ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-C", WORKDIR]
        argv.append("-")  # the prompt comes on stdin
        prompt = step.prompt
        if step.shared_context is not None:
            prompt = step.shared_context.read_text(encoding="utf-8") + "\n\n---\n\n" + prompt
        return Invocation(tuple(argv), stdin=prompt, env={"CODEX_HOME": self.home}, files=files)

    def parse(self, line: str) -> Event | None:
        ev = json_line(line)
        if ev is None:
            return None
        kind, item = ev.get("type"), ev.get("item") or {}
        if kind == "item.completed" and item.get("type") == "agent_message":
            return Event(EventKind.MESSAGE, {"text": item.get("text") or ""})
        if kind == "item.completed" and item.get("type") in TOOL_ITEMS:
            return Event(EventKind.TOOL, {"count": 1})
        if kind == "turn.completed":
            return Event(EventKind.USAGE, {"usage": ev.get("usage")})
        if kind in ("error", "turn.failed"):
            return Event(EventKind.ERROR, {"detail": json.dumps(ev)[:500]})
        return None

    def result(self, step: StepSpec, events: Sequence[Event]) -> StepResult:
        message = last(events, EventKind.MESSAGE)
        text = message.get("text") if message else None
        usage = last(events, EventKind.USAGE)
        errors = [str(e.data.get("detail")) for e in events if e.kind is EventKind.ERROR]
        output = text if step.schema is None else _loads(text)
        ok = output is not None
        error = None
        if not ok:
            error = "; ".join(errors) or (f"output is not valid JSON: {text[:300]}" if text else None)
        return StepResult(
            ok, output, error, tool_uses=count_tools(events), usage=usage.get("usage") if usage else None
        )


def _loads(text: str | None) -> object | None:
    if not text:
        return None
    try:
        value: object = json.loads(text)
    except ValueError:
        return None
    return value
