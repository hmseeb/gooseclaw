#!/usr/bin/env bash
# job.sh — unified job system (reminders + scripts) via the gateway API.
#
# usage:
#   job create "cost-check" --run "curl -s api/costs | notify" --every 1h
#   job create "health" --run "curl -s api/health" --cron "0 9 * * 1-5"
#   job create "deploy-check" --run "check-deploy.sh" --in 5m
#   job list                        # list active jobs
#   job cancel <id>                 # cancel/delete a job by ID
#   job run <id>                    # trigger a job immediately
#
# the gateway handles scheduling and delivery. no goose sessions involved.

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

_parse_until() {
    # accepts: YYYY-MM-DD, YYYY-MM-DD HH:MM, or a duration like 7d/2w
    local input="$1"
    # try date format first
    python3 -c "
import time, sys, re
s = '''$input'''.strip()
# try YYYY-MM-DD HH:MM
for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
    try:
        t = time.mktime(time.strptime(s, fmt))
        if t <= time.time():
            print('error: --until date must be in the future', file=sys.stderr)
            sys.exit(1)
        print(int(t))
        sys.exit(0)
    except ValueError:
        pass
# try duration: Nd or Nw
m = re.match(r'^(\d+)([dwDW])$', s)
if m:
    n, unit = int(m.group(1)), m.group(2).lower()
    secs = n * (86400 if unit == 'd' else 604800)
    print(int(time.time() + secs))
    sys.exit(0)
print(f'error: could not parse --until \"{s}\". use YYYY-MM-DD, YYYY-MM-DD HH:MM, Nd, or Nw', file=sys.stderr)
sys.exit(1)
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

    provider = j.get('provider', '')
    model = j.get('model', '')
    override = ''
    if provider and model:
        override = f' [{provider}/{model}]'
    elif provider:
        override = f' [{provider}]'
    elif model:
        override = f' [{model}]'
    status = j.get('last_status', '-')
    print(f'  [{jid}] {tag} {name} — {schedule}{override} [{status}]')
"
}

cmd_cancel() {
    local jid="$1"
    # if short ID provided, try to match against full IDs
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
        echo "[job] deleted"
    else
        echo "[job] failed: $RESPONSE" >&2
        exit 1
    fi
}

cmd_run() {
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

    RESPONSE=$(curl -s -X POST "$API_URL/$jid/run" 2>/dev/null)
    STARTED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('started', False))" 2>/dev/null || echo "False")

    if [[ "$STARTED" == "True" ]]; then
        echo "[job] triggered"
    else
        echo "[job] failed: $RESPONSE" >&2
        exit 1
    fi
}

cmd_create() {
    local name=""
    local command=""
    local delay_seconds=""
    local fire_at=""
    local recurring_seconds=""
    local cron_expr=""
    local model=""
    local provider=""
    local expires_at=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --run)
                shift
                command="$1"
                ;;
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
                ;;
            --cron)
                shift
                cron_expr="$1"
                ;;
            --until)
                shift
                expires_at=$(_parse_until "$1")
                ;;
            --model)
                shift
                model="$1"
                ;;
            --provider)
                shift
                provider="$1"
                ;;
            *)
                if [[ -z "$name" ]]; then
                    name="$1"
                else
                    echo "error: unknown flag '$1'" >&2
                    exit 1
                fi
                ;;
        esac
        shift
    done

    if [[ -z "$name" ]]; then
        echo "error: job name is required" >&2
        exit 1
    fi

    if [[ -z "$command" ]]; then
        echo "error: --run <command> is required" >&2
        exit 1
    fi

    if [[ -z "$delay_seconds" ]] && [[ -z "$fire_at" ]] && [[ -z "$recurring_seconds" ]] && [[ -z "$cron_expr" ]]; then
        echo "error: must specify a schedule: --in, --at, --every, or --cron" >&2
        exit 1
    fi

    # if --every but no --in/--at, fire first one after the interval
    if [[ -n "$recurring_seconds" ]] && [[ -z "$delay_seconds" ]] && [[ -z "$fire_at" ]] && [[ -z "$cron_expr" ]]; then
        delay_seconds="$recurring_seconds"
    fi

    PAYLOAD=$(_NAME="$name" _CMD="$command" _DELAY="$delay_seconds" _FIREAT="$fire_at" _RECUR="$recurring_seconds" _CRON="$cron_expr" _MODEL="$model" _PROVIDER="$provider" _EXPIRES="$expires_at" python3 -c "
import json, os
d = {
    'type': 'script',
    'name': os.environ['_NAME'],
    'command': os.environ['_CMD'],
}
ds = os.environ.get('_DELAY', '')
fa = os.environ.get('_FIREAT', '')
rs = os.environ.get('_RECUR', '')
cr = os.environ.get('_CRON', '')
ml = os.environ.get('_MODEL', '')
pv = os.environ.get('_PROVIDER', '')
ex = os.environ.get('_EXPIRES', '')
if ds:
    d['delay_seconds'] = int(ds)
elif fa:
    d['fire_at'] = float(fa)
if rs:
    d['recurring_seconds'] = int(rs)
if cr:
    d['cron'] = cr
if ml:
    d['model'] = ml
if pv:
    d['provider'] = pv
if ex:
    d['expires_at'] = float(ex)
print(json.dumps(d))
")

    RESPONSE=$(curl -s -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null)

    CREATED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('created', False))" 2>/dev/null || echo "False")

    if [[ "$CREATED" == "True" ]]; then
        JID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['job']['id'][:8])" 2>/dev/null)
        SCHED=""
        if [[ -n "$cron_expr" ]]; then
            SCHED="cron=$cron_expr"
        else
            FIRES_AT=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('fires_at_human', '?'))" 2>/dev/null)
            FIRES_IN=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('fires_in_seconds', '?'))" 2>/dev/null)
            SCHED="fires $FIRES_AT (in ${FIRES_IN}s)"
        fi
        if [[ -n "$recurring_seconds" ]]; then
            SCHED="$SCHED, repeats every ${recurring_seconds}s"
        fi
        echo "[job] created: \"$name\" — $SCHED [$JID]"
    else
        echo "[job] failed: $RESPONSE" >&2
        exit 1
    fi
}

# ── main ─────────────────────────────────────────────────────────────────────

if [[ $# -eq 0 ]]; then
    echo "usage:"
    echo "  job create \"name\" --run \"command\" --every 1h    # recurring script"
    echo "  job create \"name\" --run \"command\" --cron \"0 9 * * 1-5\"  # cron schedule"
    echo "  job create \"name\" --run \"command\" --in 5m       # one-shot in 5 minutes"
    echo "  job create \"name\" --run \"command\" --at 09:00    # one-shot at time"
    echo "  job create \"name\" --run \"cmd\" --every 1d --until 2026-03-30  # auto-expires"
    echo "  job create \"name\" --run \"cmd\" --every 1d --provider openrouter --model mistral-7b"
    echo "  job list                                         # list active jobs"
    echo "  job cancel <id>                                  # cancel by ID (first 8 chars ok)"
    echo "  job run <id>                                     # trigger immediately"
    exit 0
fi

case "$1" in
    list)
        cmd_list
        ;;
    cancel)
        if [[ -z "${2:-}" ]]; then
            echo "usage: job cancel <id>" >&2
            exit 1
        fi
        cmd_cancel "$2"
        ;;
    run)
        if [[ -z "${2:-}" ]]; then
            echo "usage: job run <id>" >&2
            exit 1
        fi
        cmd_run "$2"
        ;;
    create)
        shift
        cmd_create "$@"
        ;;
    *)
        echo "error: unknown command '$1'. use: create, list, cancel, run" >&2
        exit 1
        ;;
esac
