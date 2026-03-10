FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# minimal runtime deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl git python3 python3-yaml ca-certificates jq bzip2 libgomp1 nodejs npm && \
    rm -rf /var/lib/apt/lists/*

# install goose (prebuilt binary via official script)
RUN curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh \
    | CONFIGURE=false GOOSE_BIN_DIR=/usr/local/bin bash

# app directory
WORKDIR /app
COPY . /app/

# make scripts executable
RUN chmod +x /app/docker/entrypoint.sh /app/docker/gateway.py /app/scripts/persist.sh /app/docker/scripts/notify.sh /app/docker/scripts/secret.sh
# put helper scripts on PATH
RUN ln -sf /app/docker/scripts/notify.sh /usr/local/bin/notify && \
    ln -sf /app/docker/scripts/secret.sh /usr/local/bin/secret

# persistent data directory (Railway volume mounts at /data)
RUN mkdir -p /data

CMD ["/app/docker/entrypoint.sh"]
