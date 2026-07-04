# syntax=docker/dockerfile:1
# Forge web (Next.js) image — standalone production build (HARD-07).
#
# Build stage installs the pnpm workspace and runs `next build` with
# `output: "standalone"` (apps/web/next.config.mjs); the runtime stage ships
# ONLY the traced standalone server + static assets and runs `node server.js`
# as the non-root `node` user (uid 1000 — matches the compose `user:` line).
FROM node:22-bookworm-slim@sha256:813a7480f28fdadac1f7f5c824bcdad435b5bc1322a5968bbbdef8d058f9dff4 AS build

ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable
WORKDIR /app

# Workspace manifests first for caching.
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web ./apps/web
RUN pnpm install --frozen-lockfile
RUN pnpm --filter ./apps/web build

FROM node:22-bookworm-slim@sha256:813a7480f28fdadac1f7f5c824bcdad435b5bc1322a5968bbbdef8d058f9dff4 AS runtime
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

# Standalone output preserves the monorepo layout: the traced server lands at
# .next/standalone/apps/web/server.js with its minimal node_modules alongside.
COPY --from=build --chown=node:node /app/apps/web/.next/standalone ./
COPY --from=build --chown=node:node /app/apps/web/.next/static ./apps/web/.next/static
COPY --from=build --chown=node:node /app/apps/web/public ./apps/web/public

# Non-root runtime (the base image ships a `node` user, uid 1000).
USER node

EXPOSE 3000
CMD ["node", "apps/web/server.js"]
