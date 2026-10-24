---
name: hygiene
prefix: H
tier: balanced
---
Dockerfile and compose files (base image, non-root user, healthcheck, layers, .dockerignore), dependencies that are
unpinned, outdated or unused, files under version control that should not be, dead code, tests (gaps in what is
critical, tests that do not test what they say) and configuration (dangerous defaults, settings not validated at
start-up).
