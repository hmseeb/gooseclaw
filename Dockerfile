FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# minimal runtime deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl git python3 ca-certificates jq && \
    rm -rf /var/lib/apt/lists/*

# install goose (prebuilt binary via official script)
RUN curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh \
    | CONFIGURE=false GOOSE_BIN_DIR=/usr/local/bin bash

# app directory
WORKDIR /app
COPY . /app/

# make scripts executable
RUN chmod +x /app/docker/entrypoint.sh /app/scripts/persist.sh

# persistent data directory (Railway volume mounts here)
RUN mkdir -p /data
VOLUME ["/data"]

CMD ["/app/docker/entrypoint.sh"]
