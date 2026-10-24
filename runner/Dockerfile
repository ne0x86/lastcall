# Runner: one ephemeral container per step, started by the supervisor. It holds the official agent CLIs at pinned
# versions, Python and Node.js for the projects' tests, and read-only tools. No Docker socket and no keys: only the
# workspace, /ctx and the account's credential volume are mounted.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm
ARG CLAUDE_CODE_VERSION=2.1.285
ARG CODEX_VERSION=0.163.0

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# ca-certificates is required: without it, Codex fails the login as if the token were wrong.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Versions change only on purpose: the quota readers use experimental CLI interfaces.
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The CLI homes exist in the image so that a new account volume starts owned by the runner user.
RUN useradd -m -u 1000 runner \
    && mkdir -p /home/runner/.claude /home/runner/.codex \
    && chown -R runner:runner /home/runner
USER runner
WORKDIR /home/runner
