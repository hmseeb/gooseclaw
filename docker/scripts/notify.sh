#!/usr/bin/env bash
# notify.sh — send a message to all paired telegram users via the gateway API.
#
# usage:
#   echo "hello" | notify.sh
#   notify.sh "hello world"
#   notify.sh --file /path/to/message.txt
#
# uses the local gateway's /api/notify endpoint.
# no external config needed — the gateway handles bot tokens and chat IDs.

set -euo pipefail

GATEWAY_PORT="${PORT:-8080}"
GATEWAY_URL="http://127.0.0.1:${GATEWAY_PORT}/api/notify"

# read message
MSG=""
if [[ "${1:-}" == "--file" ]] && [[ -n "${2:-}" ]]; then
    MSG=$(cat "$2")
elif [[ -n "${1:-}" ]]; then
    MSG="$1"
elif [[ ! -t 0 ]]; then
    MSG=$(cat)
fi

if [[ -z "$MSG" ]]; then
    echo "usage: notify.sh \"message\" | notify.sh --file path | echo msg | notify.sh" >&2
    exit 1
fi

# send via gateway
PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'text': sys.stdin.read()}))" <<< "$MSG")
RESPONSE=$(curl -s -X POST "$GATEWAY_URL" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>/dev/null)

SENT=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sent', False))" 2>/dev/null || echo "False")

if [[ "$SENT" == "True" ]]; then
    exit 0
else
    echo "[notify] failed: $RESPONSE" >&2
    exit 1
fi
