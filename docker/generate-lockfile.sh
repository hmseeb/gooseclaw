#!/usr/bin/env bash
# Generate requirements.lock with hash-pinned dependencies.
# Must run on Python 3.10+ (matching Docker image target).
#
# Usage:
#   ./docker/generate-lockfile.sh           # if Python 3.10+ available locally
#   ./docker/generate-lockfile.sh --docker  # generate inside Docker container
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REQ_IN="$SCRIPT_DIR/requirements.txt"
REQ_OUT="$SCRIPT_DIR/requirements.lock"

if [[ "${1:-}" == "--docker" ]]; then
    echo "Generating requirements.lock inside Docker (python:3.10-slim)..."
    docker run --rm \
        -v "$SCRIPT_DIR:/work" \
        python:3.10-slim \
        bash -c "pip install pip-tools && pip-compile --generate-hashes --allow-unsafe --output-file=/work/requirements.lock /work/requirements.txt"
else
    echo "Generating requirements.lock locally..."
    pip install pip-tools 2>/dev/null
    pip-compile --generate-hashes --allow-unsafe --output-file="$REQ_OUT" "$REQ_IN"
fi

echo "Done: $REQ_OUT"
echo "Packages pinned: $(grep -c '==' "$REQ_OUT" || true)"
