# syntax=docker/dockerfile:1
# Forge Celery worker image (plan Task 0.6 substrate; built in Phase 2).
#
# PARKED: `docker compose build` is not runnable in the overnight sandbox (no
# network). Phase 2 (Task 2.1) verifies the build.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY pyproject.toml ./
COPY packages ./packages
COPY apps ./apps
RUN uv sync --no-dev --no-editable

RUN groupadd -g 1000 forge && useradd -u 1000 -g forge -m forge \
    && chown -R forge:forge /app
USER forge

CMD ["celery", "-A", "forge_worker", "worker", "--loglevel=info"]
