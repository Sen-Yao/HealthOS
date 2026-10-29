# HealthOS

HealthOS is a standard-library Python service backed by SQLite and a mounted
filesystem. The deployable service, tests, Dockerfile, release scripts, and
production runbook live under [`deploy/healthos`](deploy/healthos/).

## Local development

See [`LOCAL_RUN.md`](LOCAL_RUN.md) for the verified Windows development command,
local database paths, health checks, and the optional Ironbook read-only preview.

## Container release

Production uses one Docker container. Docker build and release instructions are
documented in [`deploy/healthos/README.md`](deploy/healthos/README.md); Docker
Compose is not part of the production release path.

## Repository hygiene

Local databases, virtual environments, browser artifacts, caches, and secrets
are excluded by [`.gitignore`](.gitignore).
