# lastcall

**Last call before your AI quota resets.**

lastcall watches the usage limits of the AI coding subscriptions you already pay for, starting with Claude Code and
Codex. When a reset is coming and capacity is left over, it spends that capacity on your repositories. It audits
them, fixes what it can in small pull requests verified by your own tests, and leaves the rest as issues for you to
decide. Nothing reaches your default branch without your approval.

> **Status: design phase.** There is nothing to install yet. The design is in [DESIGN.md](DESIGN.md) and open for
> discussion.

## Why

Subscription plans come with rolling and weekly limits. Whatever you do not use before the reset is gone. lastcall
turns that leftover into the maintenance work that never gets done: real bugs, security issues, outdated
dependencies, improvement plans.

## Principles

- **Only leftover capacity.** A reserve stays for you, and lastcall stops before any paid usage.
- **Small, verified changes.** lastcall runs the tests again in a clean container, outside the agent, before it
  opens a pull request.
- **You approve everything.** Agents never push, merge or deploy.
- **Official tools only.** lastcall runs each vendor's official, unmodified CLI with your own login. It never
  handles your subscription credentials. See
  [provider terms and responsible use](DESIGN.md#3-provider-terms-and-responsible-use).
- **Self-hosted.** It runs on your machine with Docker. There is no hosted version.

## Development

You need [uv](https://docs.astral.sh/uv/).

```sh
uv sync                      # create the environment
uv run pytest                # tests
uv run ruff check && uv run ruff format --check && uv run mypy
```

The sandbox (egress proxy, supervisor and runner image) needs Docker 26 or later:

```sh
docker compose --profile build --profile cli --profile test build
docker compose --profile test run --rm it   # sandbox tests against the real containers
```

Connect an account and read its quota. The login is the CLI's own; lastcall never sees it:

```sh
docker compose up -d                                          # egress proxy and supervisor
uv run lastcall login claude-max --engine claude-code         # or: lastcall login chatgpt --engine codex
docker compose run --rm cli quota claude-max --engine claude-code
```

## License

[Apache-2.0](LICENSE)
