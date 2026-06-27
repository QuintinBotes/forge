# Forge sandbox image — Node (F19). Node.js + corepack (pnpm) on the locked-down
# base. Pinned in env as FORGE_SANDBOX_IMAGE_NODE.
ARG FORGE_SANDBOX_BASE=forge-sandbox-base:latest
FROM ${FORGE_SANDBOX_BASE}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm \
 && corepack enable || true \
 && rm -rf /var/lib/apt/lists/*

USER 10001:10001
ENV npm_config_cache=/tmp/npm-cache
