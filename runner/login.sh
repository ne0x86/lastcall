#!/bin/sh
# The official login of an engine's CLI, kept in the account's volume, which compose.yaml mounts at
# /home/runner/.account:
#
#   ACCOUNT=<id> ENGINE=<engine> docker compose run --rm login
#
# Each engine points its CLI at that folder through its own variable; agent steps later mount the same volume
# where the engine expects it. tests/test_accounts.py checks that every engine of lastcall.engines is here.
set -eu
account=/home/runner/.account
case "${LASTCALL_ENGINE:-}" in
  claude-code)
    export CLAUDE_CONFIG_DIR="$account"
    exec claude auth login
    ;;
  codex)
    export CODEX_HOME="$account"
    exec codex login --device-auth
    ;;
  opencode)
    echo "An opencode account runs on an API key or a local model, which you give on the Accounts page: it has no login." >&2
    exit 2
    ;;
  *)
    echo "Set ENGINE to an engine with a login: claude-code or codex (it was '${LASTCALL_ENGINE:-}')." >&2
    exit 2
    ;;
esac
