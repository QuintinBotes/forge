# syntax=docker/dockerfile:1
# Forge self-hosted REFERENCE MCP server (HARD-05 integration substrate).
#
# A tiny, dependency-free MCP server that speaks the real JSON-RPC 2.0 wire
# protocol (MCP revision 2025-06-18) over Streamable-HTTP. It is the integration
# substrate for the `mcp-integration` CI lane and `docker compose -f
# deploy/docker-compose.integration.yml up mcp-reference` — NO external SaaS or
# credential. It serves the same fixture corpus the security tests trust,
# including one resource with a planted FAKE secret to exercise redaction.
#
# NOT for production traffic: it is a test double, not a real knowledge source.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:7cf77f594be8042dab6daa9fe326f90962252268b4f120a7f5dccce4d947e6c1

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages
COPY apps ./apps
# The reference server lives in the forge_mcp SDK (no extra deps); sync it only.
RUN uv sync --frozen --no-dev --no-editable --package forge-mcp

RUN groupadd -g 1000 forge && useradd -u 1000 -g forge -m forge \
    && chown -R forge:forge /app
USER forge

EXPOSE 8901
# Bind all interfaces inside the container network; the compose overlay maps it.
CMD ["python", "-m", "forge_mcp.reference_server", "--http", "--host", "0.0.0.0", "--port", "8901"]
