# Runner: one ephemeral container per step, started by the supervisor. It holds the official agent CLIs at pinned
# versions, the scanners of the context pack, Python and Node.js for the projects' tests, and read-only tools. No
# Docker socket and no keys: only the workspace, /ctx and the account's credential volume are mounted.
FROM node:26-bookworm-slim AS node
FROM zricethezav/gitleaks:v8.30.1 AS gitleaks
FROM ghcr.io/google/osv-scanner:v2.7.0 AS osv
FROM hadolint/hadolint:v2.15.1 AS hadolint

FROM python:3.12-slim-bookworm
ARG CLAUDE_CODE_VERSION=2.1.285
ARG CODEX_VERSION=0.163.0
ARG SEMGREP_VERSION=1.182.1

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# ca-certificates is required: without it, Codex fails the login as if the token were wrong.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Scanners for the context pack, at pinned versions. semgrep gets its own virtual environment, so that its many
# dependencies never mix with what a project installs to run its tests.
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=osv /osv-scanner /usr/local/bin/osv-scanner
COPY --from=hadolint /bin/hadolint /usr/local/bin/hadolint
RUN python -m venv /opt/semgrep \
    && /opt/semgrep/bin/pip install --no-cache-dir "semgrep==${SEMGREP_VERSION}" \
    && ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep

# Versions change only on purpose: the quota readers use experimental CLI interfaces.
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SEMGREP_ENABLE_VERSION_CHECK=0 \
    SEMGREP_SEND_METRICS=off

# The CLI homes exist in the image so that a new account volume starts owned by the runner user.
RUN useradd -m -u 1000 runner \
    && mkdir -p /home/runner/.account /home/runner/.claude /home/runner/.codex \
    && chown -R runner:runner /home/runner
USER runner
WORKDIR /home/runner
