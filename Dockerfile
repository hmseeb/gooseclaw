FROM ubuntu:22.04

LABEL maintainer="gooseclaw" \
      description="GooseClaw personal AI agent" \
      org.opencontainers.image.source="https://github.com/gooseclaw/gooseclaw" \
      org.opencontainers.image.title="GooseClaw" \
      org.opencontainers.image.base.name="ubuntu:22.04"

ENV DEBIAN_FRONTEND=noninteractive

# minimal runtime deps
# python3-yaml pinned via apt; see docker/requirements.txt for pip-based version tracking
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl git python3 python3-yaml ca-certificates jq bzip2 libgomp1 nodejs npm && \
    rm -rf /var/lib/apt/lists/*

# install goose (prebuilt binary via official script)
RUN curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh \
    | CONFIGURE=false GOOSE_BIN_DIR=/usr/local/bin bash

# create non-root user for potential non-root deployments
# NOTE: Container runs as root by default because entrypoint.sh may install
# the claude CLI via apt at startup. For deployments that do not need the
# claude CLI installed at runtime, override with: --user gooseclaw
# Railway sets RAILWAY_RUN_UID=0 (root) which is the expected default here.
RUN groupadd -r gooseclaw && \
    useradd -r -g gooseclaw -d /app -s /sbin/nologin gooseclaw

# app directory
WORKDIR /app

# copy dependencies first for better layer caching (these change rarely)
COPY docker/requirements.txt /app/docker/requirements.txt

# copy application files using specific paths (avoids wildcard COPY)
COPY docker/ /app/docker/
COPY scripts/ /app/scripts/
COPY identity/ /app/identity/
COPY VERSION /app/VERSION

# make scripts executable
RUN chmod +x /app/docker/entrypoint.sh /app/docker/gateway.py /app/scripts/persist.sh /app/docker/scripts/notify.sh /app/docker/scripts/secret.sh
# put helper scripts on PATH
RUN ln -sf /app/docker/scripts/notify.sh /usr/local/bin/notify && \
    ln -sf /app/docker/scripts/secret.sh /usr/local/bin/secret

# persistent data directory (Railway volume mounts at /data)
RUN mkdir -p /data

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8080/api/health || exit 1

CMD ["/app/docker/entrypoint.sh"]
