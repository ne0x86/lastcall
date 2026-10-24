# Supervisor: the only container with the Docker socket. It starts one runner container per step.
FROM docker:28-cli AS docker-cli

FROM python:3.12-slim-bookworm AS base
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Integration tests: they play the service's part against a running supervisor.
FROM base AS test
RUN pip install --no-cache-dir "pytest>=8.3"
COPY tests ./tests

FROM base AS supervisor
CMD ["python", "-m", "lastcall.supervisor"]
