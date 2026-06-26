# syntax=docker/dockerfile:1
# Forge web (Next.js) image (plan Task 0.6 substrate; built in Phase 2).
#
# PARKED: `docker compose build` is not runnable in the overnight sandbox (no
# network to pull base images / fetch pnpm deps). Phase 2 (Task 2.1) verifies it.
FROM node:22-bookworm-slim AS build

ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH
RUN corepack enable
WORKDIR /app

# Workspace manifests first for caching.
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web ./apps/web
COPY packages/ui-kit ./packages/ui-kit
RUN pnpm install --frozen-lockfile
RUN pnpm --filter ./apps/web build

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production \
    PATH=/pnpm:$PATH
RUN corepack enable
WORKDIR /app/apps/web
COPY --from=build /app /app

# Non-root runtime (the base image ships a `node` user, uid 1000).
USER node

EXPOSE 3000
CMD ["pnpm", "start"]
