# Contributing to lastcall

Thank you for helping. lastcall is young: issues and small pull requests are the best way in. Everyone who takes
part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Before you start

- **A bug:** open an issue with what you did, what you expected and what happened. The service's log helps
  (`docker compose logs lastcall`); remove anything secret first.
- **A feature:** open an issue first, so that we agree on its shape before you write code.
  [DESIGN.md](DESIGN.md) explains how the parts fit together.
- **A security problem:** do not open an issue. See [SECURITY.md](SECURITY.md).

## Development

You need [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync                                     # the environment
uv run pytest                               # unit tests
uv run ruff check && uv run ruff format --check && uv run mypy
```

The sandbox tests run against the real containers, built from your checkout (on Windows the separator is `;`):

```sh
export COMPOSE_FILE=compose.yaml:compose.dev.yaml
docker compose --profile build --profile cli --profile test build
docker compose --profile test run --rm it
```

They use the same volume and network names as an installed lastcall: do not run them on a machine where your own
lastcall is running.

## Pull requests

- **One topic per pull request**, small enough to review in minutes.
- **Tests for every change in behaviour.** The CI runs ruff, mypy (strict) and pytest on Python 3.12 and 3.14, and
  the sandbox tests.
- **Keep the documentation true.** A change that README.md or DESIGN.md describes updates them in the same pull
  request.
- **Write code that reads like the code around it.** Comments and documentation are in English, in short, plain
  sentences.
- **User-facing texts go in both catalogs**, `src/lastcall/locales/en.json` and `es.json`. A test checks that they
  have the same keys.

## Adding an engine

An engine drives one official command-line tool. The rest of lastcall only knows the engine contract, so a new
engine is one module and a few small changes:

1. **Write `src/lastcall/engines/<name>.py`**, a class with the attributes and methods of `Engine` in
   `src/lastcall/engines/base.py`:
   - its id, its title and its provider;
   - what it supports;
   - the domains its CLI needs (`egress`);
   - where the CLI keeps its login (`home`), the variable that points it there (`home_env`), and its own login
     command. An engine that runs on API keys or on a local model server has no login: it implements `Keyed`
     instead, its `providers` (each with where to create a key, where its models are listed and, when models.dev
     has them, its id there, so that the Accounts page suggests its models) and `with_key`, which returns the
     engine on an account's key. The key reaches the step only as a file in `/ctx` (`Invocation.files`), never on
     the command line or in the environment;
   - how a step becomes a command line (`invocation`), and how the output becomes events and a result (`parse` and
     `result`).
2. **If the CLI can read the account's quota without calling the model,** add `quota_invocation` and
   `quota_reading` (`QuotaProbe`). Without them, lastcall cannot plan bursts for that engine's accounts, unless the
   engine is `Keyed`: then lastcall's ledger reads what its steps cost, so `result` must report `cost_usd`. A
   window that limits only some models names the model in `Window.model`.
3. **Register it** in `src/lastcall/engines/__init__.py`.
4. **Pick its models** for each tier (fast, balanced, deep) in `src/lastcall/data/tiers.json`, and the names that
   its CLI takes under `models`, which the Accounts page suggests. An engine on API keys may have none there:
   each of its accounts names its models.
5. **Install its CLI**, at a pinned version, in `runner/Dockerfile`, and add its login to `runner/login.sh`, or, for
   an engine with no login, a branch that says so. A test checks that every engine has one there.
6. **Regenerate `egress/filter`.** The command is in `tests/test_egress.py`.
7. **Test it:**
   - with event streams recorded from the real CLI, in `tests/fixtures`, as `tests/test_engines.py` does for the
     existing engines;
   - its quota reader with a fake CLI, in `tests/fakes`;
   - if you can, the real CLI in the sandbox, against a stand-in for the provider, as
     `tests/integration/test_sandbox.py` does for opencode.
8. **Add it to the engines tables** in README.md and DESIGN.md (section 6), and to the tools that the README's
   introduction names.

## License

By contributing, you agree that your contributions are licensed under the Apache License 2.0, like the rest of
lastcall.
