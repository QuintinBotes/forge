# syntax=docker/dockerfile:1
# Forge API image (plan Task 0.6 substrate; built + verified in Phase 2 Task 2.1).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:7cf77f594be8042dab6daa9fe326f90962252268b4f120a7f5dccce4d947e6c1

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Workspace metadata first for layer caching, then sources.
COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages
COPY apps ./apps
RUN uv sync --frozen --no-dev --no-editable

# Run as a non-root user (uid/gid 1000 matches the compose `user:` directive).
RUN groupadd -g 1000 forge && useradd -u 1000 -g forge -m forge \
    && chown -R forge:forge /app
# Spec-engine store. Created here, owned by `forge`, so that when compose mounts
# a named volume over it Docker seeds the volume from this directory and the
# non-root process can write. A volume mounted onto a path absent from the image
# is created root-owned, and the engine then fails to write any spec.
RUN mkdir -p /srv/forge/specs && chown -R forge:forge /srv/forge
USER forge

EXPOSE 8000
CMD ["uvicorn", "forge_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
