#!/usr/bin/env bash
# remind.sh — convenience wrapper for text reminders via the job engine.
#
# usage:
#   remind "drink water" --in 5m          # one-shot, fires in 5 minutes
#   remind "standup" --at "09:00"         # one-shot, fires at next 09:00
#   remind "drink water" --every 1h       # recurring every hour
#   remind list                           # list active jobs (all types)
#   remind cancel <id>                    # cancel a job by ID
#
# thin wrapper over `job`. creates reminder-type jobs via /api/jobs.

set -euo pipefail

GATEWAY_PORT="${PORT:-8080}"
API_URL="http://127.0.0.1:${GATEWAY_PORT}/api/jobs"

# ── helpers ──────────────────────────────────────────────────────────────────

parse_duration() {
    local input="$1"
    local total=0
    local remaining="$input"

    if [[ "$remaining" =~ ([0-9]+)h ]]; then
        total=$((total + ${BASH_REMATCH[1]} * 3600))
        remaining="${remaining//${BASH_REMATCH[0]}/}"
    fi
    if [[ "$remaining" =~ ([0-9]+)m ]]; then
        total=$((total + ${BASH_REMATCH[1]} * 60))
        remaining="${remaining//${BASH_REMATCH[0]}/}"
    fi
    if [[ "$remaining" =~ ([0-9]+)s ]]; then
        total=$((total + ${BASH_REMATCH[1]}))
        remaining="${remaining//${BASH_REMATCH[0]}/}"
    fi
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
    local time_str="$1"
    if [[ ! "$time_str" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
        echo "error: invalid time format '$time_str'. use HH:MM" >&2
        exit 1
    fi
    python3 -c "
import time, sys
h, m = ${BASH_REMATCH[1]}, ${BASH_REMATCH[2]}
now = time.time()
lt = time.localtime(now)
target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, lt.tm_isdst))
if target <= now:
    target += 86400
print(int(target))
"
}

# ── commands ─────────────────────────────────────────────────────────────────

cmd_list() {
    RESPONSE=$(curl -s "$API_URL" 2>/dev/null)
    COUNT=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null || echo 0)

    if [[ "$COUNT" == "0" ]]; then
        echo "no active jobs"
        return
    fi

    echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for j in data.get('jobs', []):
    jid = j['id'][:8]
    name = j.get('name', jid)
    jtype = j.get('type', 'script')
    tag = '[R]' if jtype == 'reminder' else '[S]'

    schedule = ''
    if j.get('cron'):
        schedule = f'cron={j[\"cron\"]}'
    elif j.get('fires_at_human'):
        schedule = f'fires {j[\"fires_at_human\"]}'
        if j.get('fires_in_seconds') is not None:
            schedule += f' (in {j[\"fires_in_seconds\"]}s)'
    if j.get('recurring_seconds'):
        schedule += f' (every {j[\"recurring_seconds\"]}s)'

    status = j.get('last_status', '-')
    print(f'  [{jid}] {tag} {name} — {schedule} [{status}]')
"
}

cmd_cancel() {
    local jid="$1"
    if [[ ${#jid} -lt 36 ]]; then
        FULL_ID=$(curl -s "$API_URL" 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
prefix = '$jid'
for j in data.get('jobs', []):
    if j['id'].startswith(prefix):
        print(j['id'])
        break
" 2>/dev/null)
        if [[ -z "$FULL_ID" ]]; then
            echo "error: no job found matching '$jid'" >&2
            exit 1
        fi
        jid="$FULL_ID"
    fi

    RESPONSE=$(curl -s -X DELETE "$API_URL/$jid" 2>/dev/null)
    DELETED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('deleted', False))" 2>/dev/null || echo "False")

    if [[ "$DELETED" == "True" ]]; then
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
    local notify_channel=""

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
                if [[ -z "$delay_seconds" ]] && [[ -z "$fire_at" ]]; then
                    delay_seconds="$recurring_seconds"
                fi
                ;;
            --notify-channel)
                shift
                notify_channel="$1"
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

    PAYLOAD=$(_TEXT="$text" _DELAY="$delay_seconds" _FIREAT="$fire_at" _RECUR="$recurring_seconds" _NOTIFY_CH="$notify_channel" python3 -c "
import json, os
text = os.environ['_TEXT']
d = {'type': 'reminder', 'text': text, 'name': text[:80]}
ds = os.environ.get('_DELAY', '')
fa = os.environ.get('_FIREAT', '')
rs = os.environ.get('_RECUR', '')
nc = os.environ.get('_NOTIFY_CH', '')
if ds:
    d['delay_seconds'] = int(ds)
elif fa:
    d['fire_at'] = float(fa)
if rs:
    d['recurring_seconds'] = int(rs)
if nc:
    d['notify_channel'] = nc
print(json.dumps(d))
")

    RESPONSE=$(curl -s -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null)

    CREATED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('created', False))" 2>/dev/null || echo "False")

    if [[ "$CREATED" == "True" ]]; then
        FIRES_AT=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('fires_at_human', '?'))" 2>/dev/null)
        FIRES_IN=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('fires_in_seconds', '?'))" 2>/dev/null)
        RID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['job']['id'][:8])" 2>/dev/null)
        if [[ -n "$recurring_seconds" ]]; then
            echo "[remind] recurring: \"$text\" — first fires $FIRES_AT (in ${FIRES_IN}s), repeats every ${recurring_seconds}s [$RID]"
        else
            echo "[remind] set: \"$text\" — fires $FIRES_AT (in ${FIRES_IN}s) [$RID]"
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
    echo "  remind \"message\" --in 5m --notify-channel telegram"
    echo "  remind list                       # list all jobs"
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
