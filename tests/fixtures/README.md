# Fixtures

Event streams for the engine parsers, one JSON object per line.

These first fixtures are **synthetic**: they follow the event shapes that the prototype parsed from Claude Code
2.1.x (`--output-format stream-json`) and Codex 0.16x (`exec --json`), reduced to the fields lastcall reads.
Replace them with streams recorded from real runs (secrets and code removed) when the runner exists, and record
again whenever the pinned CLI version changes.

`opencode_success.jsonl` is recorded: opencode-ai 1.19.4 (`opencode run --format json`) against a stand-in for an
OpenAI-compatible API, with its local paths replaced by the runner's.
