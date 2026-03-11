#!/usr/bin/env bash
# remind.sh — lightweight reminder/timer system via the gateway API.
#
# Bypasses goose's scheduler entirely. Fires via direct telegram notification.
#
# usage:
#   remind "drink water" --in 5m          # one-shot, fires in 5 minutes
#   remind "drink water" --in 30s         # one-shot, fires in 30 seconds
#   remind "drink water" --in 2h          # one-shot, fires in 2 hours
#   remind "standup" --at "09:00"         # one-shot, fires at next 09:00
#   remind "drink water" --every 1h       # recurring every hour
#   remind "stretch" --every 30m          # recurring every 30 minutes
#   remind list                           # list active reminders
#   remind cancel <id>                    # cancel a reminder by ID
#
# the gateway handles delivery via telegram. no goose sessions involved.

set -euo pipefail

GATEWAY_PORT="${PORT:-8080}"
API_URL="http://127.0.0.1:${GATEWAY_PORT}/api/reminders"

# ── helpers ──────────────────────────────────────────────────────────────────

parse_duration() {
    # convert human duration (5m, 2h, 30s, 1h30m) to seconds
    local input="$1"
    local total=0
    local remaining="$input"

    # extract hours
    if [[ "$remaining" =~ ([0-9]+)h ]]; then
        total=$((total + ${BASH_REMATCH[1]} * 3600))
        remaining="${remaining//${BASH_REMATCH[0]}/}"
    fi
    # extract minutes
    if [[ "$remaining" =~ ([0-9]+)m ]]; then
        total=$((total + ${BASH_REMATCH[1]} * 60))
        remaining="${remaining//${BASH_REMATCH[0]}/}"
    fi
    # extract seconds
    if [[ "$remaining" =~ ([0-9]+)s ]]; then
        total=$((total + ${BASH_REMATCH[1]}))
        remaining="${remaining//${BASH_REMATCH[0]}/}"
    fi
    # plain number = seconds
    if [[ "$total" -eq 0 ]] && [[ "$input" =~ ^[0-9]+$ ]]; then
        total="$input"
    fi

    if [[ "$total" -eq 0 ]]; then
        echo "error: could not parse duration '$input'. use e.g. 5m, 2h, 30s, 1h30m" >&2
        exit 1
    fi
    echo "$total"
}

parse_time() {
    # convert HH:MM to unix timestamp (next occurrence)
    local time_str="$1"
    if [[ ! "$time_str" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
        echo "error: invalid time format '$time_str'. use HH:MM" >&2
        exit 1
    fi
    # use python for reliable timezone-aware calculation
    python3 -c "
import time, sys
h, m = ${BASH_REMATCH[1]}, ${BASH_REMATCH[2]}
now = time.time()
lt = time.localtime(now)
target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, lt.tm_isdst))
if target <= now:
    target += 86400  # next day
print(int(target))
"
}

# ── commands ─────────────────────────────────────────────────────────────────

cmd_list() {
    RESPONSE=$(curl -s "$API_URL" 2>/dev/null)
    COUNT=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null || echo 0)

    if [[ "$COUNT" == "0" ]]; then
        echo "no active reminders"
        return
    fi

    echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for r in data.get('reminders', []):
    recurring = f' (every {r.get(\"recurring_seconds\", 0)}s)' if r.get('recurring_seconds') else ''
    fires = r.get('fires_at_human', '?')
    secs = r.get('fires_in_seconds', 0)
    rid = r['id'][:8]
    print(f'  [{rid}] \"{r[\"text\"]}\" — fires {fires} (in {secs}s){recurring}')
"
}

cmd_cancel() {
    local rid="$1"
    # if short ID provided, try to match against full IDs
    if [[ ${#rid} -lt 36 ]]; then
        # fetch full list and find matching ID
        FULL_ID=$(curl -s "$API_URL" 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
prefix = '$rid'
for r in data.get('reminders', []):
    if r['id'].startswith(prefix):
        print(r['id'])
        break
" 2>/dev/null)
        if [[ -z "$FULL_ID" ]]; then
            echo "error: no reminder found matching '$rid'" >&2
            exit 1
        fi
        rid="$FULL_ID"
    fi

    RESPONSE=$(curl -s -X DELETE "$API_URL/$rid" 2>/dev/null)
    CANCELLED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cancelled', False))" 2>/dev/null || echo "False")

    if [[ "$CANCELLED" == "True" ]]; then
        echo "[remind] cancelled"
    else
        echo "[remind] failed: $RESPONSE" >&2
        exit 1
    fi
}

cmd_create() {
    local text="$1"
    shift

    local delay_seconds=""
    local fire_at=""
    local recurring_seconds=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --in)
                shift
                delay_seconds=$(parse_duration "$1")
                ;;
            --at)
                shift
                fire_at=$(parse_time "$1")
                ;;
            --every)
                shift
                recurring_seconds=$(parse_duration "$1")
                # if no --in or --at specified, fire first one after the interval
                if [[ -z "$delay_seconds" ]] && [[ -z "$fire_at" ]]; then
                    delay_seconds="$recurring_seconds"
                fi
                ;;
            *)
                echo "error: unknown flag '$1'" >&2
                exit 1
                ;;
        esac
        shift
    done

    if [[ -z "$delay_seconds" ]] && [[ -z "$fire_at" ]]; then
        echo "error: must specify --in <duration>, --at <HH:MM>, or --every <interval>" >&2
        exit 1
    fi

    # build JSON payload (text via stdin to avoid shell injection)
    PAYLOAD=$(_DELAY="$delay_seconds" _FIREAT="$fire_at" _RECUR="$recurring_seconds" python3 -c "
import json, sys, os
text = sys.stdin.read().strip()
d = {'text': text}
ds = os.environ.get('_DELAY', '')
fa = os.environ.get('_FIREAT', '')
rs = os.environ.get('_RECUR', '')
if ds:
    d['delay_seconds'] = int(ds)
elif fa:
    d['fire_at'] = float(fa)
if rs:
    d['recurring_seconds'] = int(rs)
print(json.dumps(d))
" <<< "$text")

    RESPONSE=$(curl -s -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null)

    CREATED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('created', False))" 2>/dev/null || echo "False")

    if [[ "$CREATED" == "True" ]]; then
        FIRES_AT=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('fires_at_human', '?'))" 2>/dev/null)
        FIRES_IN=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('fires_in_seconds', '?'))" 2>/dev/null)
        RID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['reminder']['id'][:8])" 2>/dev/null)
        if [[ -n "$recurring_seconds" ]]; then
            echo "[remind] recurring reminder set: \"$text\" — first fires $FIRES_AT (in ${FIRES_IN}s), repeats every ${recurring_seconds}s [$RID]"
        else
            echo "[remind] reminder set: \"$text\" — fires $FIRES_AT (in ${FIRES_IN}s) [$RID]"
        fi
    else
        echo "[remind] failed: $RESPONSE" >&2
        exit 1
    fi
}

# ── main ─────────────────────────────────────────────────────────────────────

if [[ $# -eq 0 ]]; then
    echo "usage:"
    echo "  remind \"message\" --in 5m         # fire in 5 minutes"
    echo "  remind \"message\" --at 09:00      # fire at next 09:00"
    echo "  remind \"message\" --every 1h      # recurring every hour"
    echo "  remind list                       # list active reminders"
    echo "  remind cancel <id>                # cancel by ID (first 8 chars ok)"
    exit 0
fi

case "$1" in
    list)
        cmd_list
        ;;
    cancel)
        if [[ -z "${2:-}" ]]; then
            echo "usage: remind cancel <id>" >&2
            exit 1
        fi
        cmd_cancel "$2"
        ;;
    *)
        if [[ $# -lt 2 ]]; then
            echo "error: need a message and a time flag (--in, --at, or --every)" >&2
            exit 1
        fi
        cmd_create "$@"
        ;;
esac
