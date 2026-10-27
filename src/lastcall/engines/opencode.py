"""OpenCode (`opencode run`), for API keys (OpenRouter, Anthropic, OpenAI) and for local models on an
OpenAI-compatible server such as Ollama. Pinned to OpenCode 1, the npm package `opencode-ai`: OpenCode 2 is another
program.

- Its whole configuration comes in OPENCODE_CONFIG_CONTENT, which ranks above any `opencode.json` in the repository,
  and the project's own configuration and instruction files are off (OPENCODE_DISABLE_PROJECT_CONFIG).
- Reviewers and the judge get read-only tools (read, grep, glob): every other tool is denied, so the run never
  stops to ask. The implementer gets edit and bash too, with `--auto`.
- The API key reaches the step as a read-only file in /ctx, which the configuration reads with `{file:...}`.
- `--pure` keeps it from installing plugins at run time, and `--title` skips the call that names the session.
- OpenCode has no structured output: when a step has a schema, the prompt carries it, and the step's output is the
  JSON of its last text.
- Its events are JSON lines: step_start, tool_use, step_finish (tokens and cost), text and error.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from lastcall.engines.base import (
    CONTEXT_DIR,
    WORKDIR,
    Engine,
    Event,
    EventKind,
    Invocation,
    Provider,
    StepResult,
    StepSpec,
    count_tools,
    json_line,
)

KEY_FILE = f"{CONTEXT_DIR}/key"
LOCAL = "ollama"  # local models: an OpenAI-compatible server, by default the compose service `ollama`
LOCAL_URL = "http://ollama:11434/v1"
PROVIDERS = (  # the models.dev id of each API is its name; a local model server has no catalog to ask
    Provider(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/settings/keys",
        "https://openrouter.ai/models",
        "openrouter",
    ),
    Provider(
        "anthropic",
        "Anthropic API",
        "https://platform.claude.com/settings/keys",
        "https://platform.claude.com/docs/en/models/overview",
        "anthropic",
    ),
    Provider(
        "openai",
        "OpenAI API",
        "https://platform.openai.com/api-keys",
        "https://developers.openai.com/api/docs/models",
        "openai",
    ),
    Provider(LOCAL, "Ollama", "", "https://ollama.com/library"),
)
DENIED = ("webfetch", "websearch", "task", "skill", "todowrite", "question")  # never needed: the run must not ask
FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class OpenCode:
    name: str = "opencode"
    title: str = "OpenCode"
    provider: str = "API keys and local models"
    capabilities: frozenset[str] = frozenset({"effort"})
    # the providers' APIs, and models.dev, where OpenCode reads what each model costs
    egress: tuple[str, ...] = ("openrouter.ai", "api.anthropic.com", "api.openai.com", "models.dev")
    home: str = "/home/runner/.local/share"
    home_env: str = "XDG_DATA_HOME"
    login: tuple[str, ...] = ()  # no login of its own: lastcall holds the account's API key
    providers: tuple[Provider, ...] = PROVIDERS

    def __init__(self, provider: str = "openrouter", key: str | None = None, base_url: str | None = None) -> None:
        names = [p.name for p in PROVIDERS]
        if provider not in names:
            raise ValueError(f"unknown provider {provider!r}; available: {', '.join(names)}")
        self.via, self.key, self.base_url = provider, key, base_url

    def with_key(self, provider: str, key: str | None, base_url: str | None = None) -> Engine:
        return OpenCode(provider, key, base_url)

    def config(self, step: StepSpec) -> dict[str, Any]:
        permission = dict.fromkeys(DENIED, "deny")
        if not step.writable:
            permission |= {"edit": "deny", "bash": "deny"}
        if self.via == LOCAL:
            options: dict[str, Any] = {"baseURL": self.base_url or LOCAL_URL}
            provider = {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Local models",
                "options": options,
                "models": {step.model: {"name": step.model}},
            }
        else:
            options = {"apiKey": "{file:" + KEY_FILE + "}"} if self.key else {}
            if self.base_url:
                options["baseURL"] = self.base_url
            provider = {"options": options}
        model = f"{self.via}/{step.model}"
        return {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "model": model,
            "small_model": model,
            "provider": {self.via: provider},
            "permission": permission,
        }

    def invocation(self, step: StepSpec) -> Invocation:
        argv = ["opencode", "--pure", "run", "--format", "json", "--title", "lastcall"]
        argv += ["-m", f"{self.via}/{step.model}", "--dir", WORKDIR]
        if step.effort:
            argv += ["--variant", step.effort]
        if step.writable:
            argv.append("--auto")
        prompt = step.prompt
        if step.shared_context is not None:
            prompt = step.shared_context.read_text(encoding="utf-8") + "\n\n---\n\n" + prompt
        if step.schema is not None:
            prompt += (
                "\n\n## Output format\n\nAnswer with one JSON object and nothing else: no prose before or after it, "
                "and no code fence. It must match this JSON schema:\n\n" + json.dumps(step.schema)
            )
        env = {
            "OPENCODE_CONFIG_CONTENT": json.dumps(self.config(step)),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        }
        files = {KEY_FILE: self.key} if self.key and self.via != LOCAL else {}
        return Invocation(tuple(argv), stdin=prompt, env=env, files=files)

    def parse(self, line: str) -> Event | None:
        ev = json_line(line)
        if ev is None:
            return None
        kind, part = ev.get("type"), ev.get("part") or {}
        if kind == "tool_use":
            return Event(EventKind.TOOL, {"count": 1})
        if kind == "text":
            return Event(EventKind.MESSAGE, {"text": part.get("text") or ""})
        if kind == "step_finish":
            return Event(EventKind.USAGE, {"usage": part.get("tokens"), "cost": part.get("cost")})
        if kind == "error":
            return Event(EventKind.ERROR, {"detail": json.dumps(ev.get("error"))[:500]})
        return None

    def result(self, step: StepSpec, events: Sequence[Event]) -> StepResult:
        texts = [str(e.data.get("text") or "") for e in events if e.kind is EventKind.MESSAGE]
        steps = [e.data for e in events if e.kind is EventKind.USAGE]
        errors = [str(e.data.get("detail")) for e in events if e.kind is EventKind.ERROR]
        text = next((t for t in reversed(texts) if t.strip()), None)
        output = text if step.schema is None else _json(text)
        ok = output is not None and not errors
        error = None
        if not ok:
            error = "; ".join(errors) or (f"output is not valid JSON: {text[:300]}" if text else "no answer")
        costs = [float(s["cost"]) for s in steps if isinstance(s.get("cost"), int | float)]
        return StepResult(
            ok,
            output if ok else None,
            error,
            tool_uses=count_tools(events),
            turns=len(steps) or None,
            cost_usd=sum(costs) if costs else None,
            usage=_tokens(steps),
        )


def _json(text: str | None) -> object | None:
    """The JSON object of an answer, also when the model wrapped it in a code fence."""
    if not text:
        return None
    match = FENCE.match(text.strip())
    try:
        value: object = json.loads(match.group(1) if match else text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _tokens(steps: Sequence[Mapping[str, Any]]) -> dict[str, int] | None:
    """The tokens of every model call of the step, added up."""
    total = {"input": 0, "output": 0, "cache_read": 0}
    for s in steps:
        tokens = s.get("usage") or {}
        total["input"] += int(tokens.get("input") or 0)
        total["output"] += int(tokens.get("output") or 0)
        total["cache_read"] += int((tokens.get("cache") or {}).get("read") or 0)
    return total if steps else None
