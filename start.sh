#!/bin/bash
# Railpack fallback — railway.toml specifies builder=DOCKERFILE,
# but Railpack occasionally runs instead. This prevents the
# "start.sh not found" build failure.
exec /app/docker/entrypoint.sh
