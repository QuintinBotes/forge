# Forge sandbox base image (F19) — the locked-down rootfs every per-task
# container runs as. Non-root uid/gid 10001 (must match FORGE_SANDBOX_RUN_UID/GID
# and the worktree ownership), tini as PID 1, coreutils for `timeout`, git +
# ca-certificates. Deliberately minimal: NO docker client, NO build tooling
# beyond what a language image adds on top.
#
# Build:  docker build -f deploy/sandbox/base.Dockerfile -t forge-sandbox-base .
# Production pins by @sha256 digest (PARKED: digest resolution needs registry
# network, unavailable in the offline sandbox).
FROM debian:12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      coreutils \
      git \
      tini \
 && rm -rf /var/lib/apt/lists/*

# Fixed, non-root identity shared with the host worker (uid/gid 10001) so files
# written in-container (e.g. coverage.xml) are readable back on the host.
RUN groupadd --gid 10001 forge \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/forge --shell /usr/sbin/nologin forge

WORKDIR /workspace
USER 10001:10001

ENTRYPOINT ["tini", "--"]
CMD ["sleep", "infinity"]
