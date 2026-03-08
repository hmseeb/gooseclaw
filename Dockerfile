FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# minimal runtime deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl git python3 ca-certificates jq cron && \
    rm -rf /var/lib/apt/lists/*

# install goose (prebuilt binary via official script)
RUN curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh \
    | CONFIGURE=false GOOSE_BIN_DIR=/usr/local/bin bash

# create non-root user
RUN useradd -m -s /bin/bash nix

# persistent data directory (Railway volume mounts here)
RUN mkdir -p /data && chown nix:nix /data
VOLUME ["/data"]

# copy application files
COPY --chown=nix:nix . /home/nix/app/

# make scripts executable
RUN chmod +x /home/nix/app/docker/entrypoint.sh \
             /home/nix/app/scripts/persist.sh

USER nix
WORKDIR /home/nix/app

CMD ["/home/nix/app/docker/entrypoint.sh"]
