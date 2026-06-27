# syntax=docker/dockerfile:1
# Forge MCP gateway image (plan Task 0.6 substrate; built + verified in Phase 2 Task 2.1).
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

EXPOSE 8001
CMD ["uvicorn", "forge_mcp_gateway.app:app", "--host", "0.0.0.0", "--port", "8001"]
