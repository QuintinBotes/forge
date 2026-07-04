# Forge sandbox image — Go (F19). Go toolchain on the locked-down base.
# Pinned in env as FORGE_SANDBOX_IMAGE_GO.
ARG FORGE_SANDBOX_BASE=forge-sandbox-base:latest
FROM ${FORGE_SANDBOX_BASE}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends golang \
 && rm -rf /var/lib/apt/lists/*

USER 10001:10001
ENV GOCACHE=/tmp/go-cache \
    GOPATH=/tmp/go
