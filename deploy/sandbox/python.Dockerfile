# Forge sandbox image — Python (F19). cpython + uv on top of the locked-down base.
# Built FROM the curated base so the hardening (non-root 10001, tini, timeout) is
# inherited. Pinned in env as FORGE_SANDBOX_IMAGE_PYTHON and on the workspace
# FORGE_SANDBOX_ALLOWED_IMAGES allowlist.
ARG FORGE_SANDBOX_BASE=forge-sandbox-base:latest
FROM ${FORGE_SANDBOX_BASE}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
# uv installed to a system path readable by uid 10001.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

USER 10001:10001
ENV UV_CACHE_DIR=/tmp/uv-cache \
    PIP_NO_INPUT=1
