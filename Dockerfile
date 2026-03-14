FROM ubuntu:22.04

LABEL maintainer="gooseclaw" \
      description="GooseClaw personal AI agent" \
      org.opencontainers.image.source="https://github.com/gooseclaw/gooseclaw" \
      org.opencontainers.image.title="GooseClaw" \
      org.opencontainers.image.base.name="ubuntu:22.04"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# minimal runtime deps
# python3-yaml pinned via apt; see docker/requirements.txt for pip-based version tracking
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl git python3 python3-yaml ca-certificates jq bzip2 libgomp1 tzdata libssl3 && \
    rm -rf /var/lib/apt/lists/*

# install node 20 LTS (ubuntu 22.04 apt ships v12, MCP tools need 18+)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# install goosed (extracted from desktop app .deb — no pre-built binary published separately)
ARG GOOSE_VERSION=1.27.2
RUN curl -fsSL -o /tmp/goose.deb \
      "https://github.com/block/goose/releases/download/v${GOOSE_VERSION}/goose_${GOOSE_VERSION}_amd64.deb" && \
    apt-get update && apt-get install -y --no-install-recommends zstd binutils && \
    cd /tmp && ar x goose.deb data.tar.zst && \
    zstd -d data.tar.zst -o data.tar && \
    tar xf data.tar ./usr/lib/goose/resources/bin/goosed && \
    mv ./usr/lib/goose/resources/bin/goosed /usr/local/bin/goosed && \
    chmod +x /usr/local/bin/goosed && \
    rm -rf /tmp/goose.deb /tmp/data.tar* /tmp/usr && \
    apt-get purge -y zstd binutils && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
# also install goose CLI for non-server commands (configure, etc.)
RUN curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh \
    | CONFIGURE=false GOOSE_BIN_DIR=/usr/local/bin bash

# create non-root user for runtime processes
# entrypoint.sh runs initial setup as root, then drops to gooseclaw user
# for gateway.py and all goose/claude processes. This is required because
# claude CLI refuses --dangerously-skip-permissions when running as root.
RUN groupadd -r gooseclaw && \
    useradd -r -g gooseclaw -m -d /home/gooseclaw -s /bin/sh gooseclaw

# app directory
WORKDIR /app

# copy dependencies first for better layer caching (these change rarely)
COPY docker/requirements.txt /app/docker/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/docker/requirements.txt

# copy application files using specific paths (avoids wildcard COPY)
COPY docker/ /app/docker/
COPY scripts/ /app/scripts/
COPY identity/ /app/identity/
COPY .goosehints /app/.goosehints
COPY VERSION /app/VERSION

# make scripts executable
RUN chmod +x /app/docker/entrypoint.sh /app/docker/gateway.py /app/scripts/persist.sh /app/docker/scripts/notify.sh /app/docker/scripts/secret.sh /app/docker/scripts/remind.sh /app/docker/scripts/job.sh
# put helper scripts on PATH
RUN ln -sf /app/docker/scripts/notify.sh /usr/local/bin/notify && \
    ln -sf /app/docker/scripts/secret.sh /usr/local/bin/secret && \
    ln -sf /app/docker/scripts/remind.sh /usr/local/bin/remind && \
    ln -sf /app/docker/scripts/job.sh /usr/local/bin/job

# persistent data directory (Railway volume mounts at /data)
RUN mkdir -p /data

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8080/api/health || exit 1

CMD ["/app/docker/entrypoint.sh"]
