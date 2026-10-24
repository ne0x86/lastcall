# lastcall images. Targets:
#   supervisor  the only container with the Docker socket; starts one runner container per step
#   service     lastcall itself, as user 1000 like the runners (for now: the command line)
#   test        the service image plus the integration tests
FROM docker:28-cli AS docker-cli

FROM python:3.12-slim-bookworm AS app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

FROM app AS service
RUN useradd -m -u 1000 lastcall
USER 1000:1000
ENTRYPOINT ["python", "-m", "lastcall"]

FROM service AS test
USER root
RUN pip install --no-cache-dir "pytest>=8.3"
COPY tests ./tests
USER 1000:1000
ENTRYPOINT []

FROM app AS supervisor
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
CMD ["python", "-m", "lastcall.supervisor"]
