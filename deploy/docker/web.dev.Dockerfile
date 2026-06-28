# syntax=docker/dockerfile:1
# Forge web (Next.js) — LOCAL DEV image.
#
# Unlike deploy/docker/web.Dockerfile (production: build + `next start`), this
# image installs the workspace deps and runs the Next dev server, so the single
# `docker compose -f deploy/docker-compose.dev.yml up` command serves the UI
# reliably without a brittle production prerender step.
FROM node:22-bookworm-slim

ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    NODE_ENV=development \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable
WORKDIR /app

# Workspace manifests first for layer caching, then the web source.
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web ./apps/web
RUN pnpm install --frozen-lockfile

WORKDIR /app/apps/web
EXPOSE 3000
# Bind to 0.0.0.0 so the port is reachable from the host and via Caddy.
CMD ["pnpm", "exec", "next", "dev", "-H", "0.0.0.0", "-p", "3000"]
