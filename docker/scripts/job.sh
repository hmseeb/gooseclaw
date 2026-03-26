#!/usr/bin/env bash
# job.sh — unified job system (reminders + scripts) via the gateway API.
#
# usage:
#   job create "briefing" --run "cmd" --weekdays 09:00       # Mon-Fri at 9am
#   job create "digest"   --run "cmd" --daily 12:00          # every day at noon
#   job create "report"   --run "cmd" --weekly mon,fri 10:00 # Mon+Fri at 10am
#   job create "invoice"  --run "cmd" --monthly 1 09:00      # 1st of month at 9am
#   job create "check"    --run "cmd" --every 1h             # every hour
#   job create "alert"    --run "cmd" --in 5m                # one-shot in 5 min
#   job create "health"   --run "cmd" --cron "0 9 * * 1-5"   # raw cron (advanced)
#   job list                                                 # list active jobs
#   job cancel <id>                                          # cancel by ID
#   job run <id>                                             # trigger immediately
#
# prefer --weekdays/--daily/--weekly/--monthly over --cron. clearer, less error-prone.
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

    schedule = j.get('cron_human', '')
    if not schedule and j.get('cron'):
        schedule = j['cron']
    elif not schedule and j.get('fires_at_human'):
        schedule = f'fires {j[\"fires_at_human\"]}'
    if j.get('recurring_seconds'):
        rs = j['recurring_seconds']
        if rs >= 3600: schedule += f' (every {rs//3600}h)'
        elif rs >= 60: schedule += f' (every {rs//60}m)'
        else: schedule += f' (every {rs}s)'

    # next run
    runs = j.get('next_runs', [])
    next_str = ''
    if runs:
        next_str = f' | next: {runs[0].get(\"relative\", runs[0].get(\"iso\", \"\"))}'

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
    print(f'  [{jid}] {tag} {name} — {schedule}{next_str}{override} [{status}]')
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
    local notify_channel=""
    # structured schedule
    local sched_frequency=""
    local sched_time=""
    local sched_days=""
    local sched_dom=""

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
            --weekdays)
                sched_frequency="weekdays"
                shift
                sched_time="$1"
                ;;
            --daily)
                sched_frequency="daily"
                shift
                sched_time="$1"
                ;;
            --weekends)
                sched_frequency="weekends"
                shift
                sched_time="$1"
                ;;
            --weekly)
                sched_frequency="weekly"
                shift
                sched_days="$1"
                shift
                sched_time="$1"
                ;;
            --monthly)
                sched_frequency="monthly"
                shift
                sched_dom="$1"
                shift
                sched_time="$1"
                ;;
            --until)
                shift
                expires_at=$(_parse_until "$1")
                ;;
            --notify-channel)
                shift
                notify_channel="$1"
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

    if [[ -z "$delay_seconds" ]] && [[ -z "$fire_at" ]] && [[ -z "$recurring_seconds" ]] && [[ -z "$cron_expr" ]] && [[ -z "$sched_frequency" ]]; then
        echo "error: must specify a schedule: --weekdays, --daily, --weekly, --monthly, --every, --in, --at, or --cron" >&2
        exit 1
    fi

    # if --every but no --in/--at, fire first one after the interval
    if [[ -n "$recurring_seconds" ]] && [[ -z "$delay_seconds" ]] && [[ -z "$fire_at" ]] && [[ -z "$cron_expr" ]] && [[ -z "$sched_frequency" ]]; then
        delay_seconds="$recurring_seconds"
    fi

    PAYLOAD=$(_NAME="$name" _CMD="$command" _DELAY="$delay_seconds" _FIREAT="$fire_at" _RECUR="$recurring_seconds" _CRON="$cron_expr" _MODEL="$model" _PROVIDER="$provider" _EXPIRES="$expires_at" _NOTIFY_CH="$notify_channel" _SCHED_FREQ="$sched_frequency" _SCHED_TIME="$sched_time" _SCHED_DAYS="$sched_days" _SCHED_DOM="$sched_dom" python3 -c "
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
nc = os.environ.get('_NOTIFY_CH', '')
sf = os.environ.get('_SCHED_FREQ', '')
st = os.environ.get('_SCHED_TIME', '')
sd = os.environ.get('_SCHED_DAYS', '')
sm = os.environ.get('_SCHED_DOM', '')
if sf:
    sched = {'frequency': sf, 'time': st}
    if sf == 'weekly' and sd:
        sched['days'] = [d.strip() for d in sd.split(',')]
    if sf == 'monthly' and sm:
        sched['day_of_month'] = int(sm)
    d['schedule'] = sched
elif ds:
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
if nc:
    d['notify_channel'] = nc
print(json.dumps(d))
")

    RESPONSE=$(curl -s -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null)

    CREATED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('created', False))" 2>/dev/null || echo "False")

    if [[ "$CREATED" == "True" ]]; then
        # show human-readable summary from API response
        echo "$RESPONSE" | python3 -c "
import sys, json
r = json.load(sys.stdin)
jid = r['job']['id'][:8]
name = r['job'].get('name', jid)
sched = r.get('cron_human', '')
if not sched:
    cron = r['job'].get('cron', '')
    if cron:
        sched = cron
    elif r.get('fires_at_human'):
        sched = 'fires ' + r['fires_at_human']
runs = r.get('next_runs', [])
run_str = ''
if runs:
    run_str = ' | next: ' + runs[0].get('relative', runs[0].get('iso', ''))
print(f'[job] created: \"{name}\" — {sched}{run_str} [{jid}]')
"
    else
        echo "[job] failed: $RESPONSE" >&2
        exit 1
    fi
}

cmd_edit() {
    local jid="$1"
    shift

    # resolve short ID
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

    # parse flags (same as create but all optional)
    local command=""
    local name=""
    local cron_expr=""
    local recurring_seconds=""
    local model=""
    local provider=""
    local expires_at=""
    local notify_channel=""
    local sched_frequency=""
    local sched_time=""
    local sched_days=""
    local sched_dom=""
    local enabled=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --run)      shift; command="$1" ;;
            --name)     shift; name="$1" ;;
            --cron)     shift; cron_expr="$1" ;;
            --every)    shift; recurring_seconds=$(parse_duration "$1") ;;
            --weekdays) sched_frequency="weekdays"; shift; sched_time="$1" ;;
            --daily)    sched_frequency="daily"; shift; sched_time="$1" ;;
            --weekends) sched_frequency="weekends"; shift; sched_time="$1" ;;
            --weekly)   sched_frequency="weekly"; shift; sched_days="$1"; shift; sched_time="$1" ;;
            --monthly)  sched_frequency="monthly"; shift; sched_dom="$1"; shift; sched_time="$1" ;;
            --until)    shift; expires_at=$(_parse_until "$1") ;;
            --notify-channel) shift; notify_channel="$1" ;;
            --model)    shift; model="$1" ;;
            --provider) shift; provider="$1" ;;
            --enable)   enabled="true" ;;
            --disable)  enabled="false" ;;
            *)
                echo "error: unknown flag '$1'" >&2
                exit 1
                ;;
        esac
        shift
    done

    PAYLOAD=$(_CMD="$command" _NAME="$name" _CRON="$cron_expr" _RECUR="$recurring_seconds" _MODEL="$model" _PROVIDER="$provider" _EXPIRES="$expires_at" _NOTIFY_CH="$notify_channel" _SCHED_FREQ="$sched_frequency" _SCHED_TIME="$sched_time" _SCHED_DAYS="$sched_days" _SCHED_DOM="$sched_dom" _ENABLED="$enabled" python3 -c "
import json, os
d = {}
cmd = os.environ.get('_CMD', '')
name = os.environ.get('_NAME', '')
cr = os.environ.get('_CRON', '')
rs = os.environ.get('_RECUR', '')
ml = os.environ.get('_MODEL', '')
pv = os.environ.get('_PROVIDER', '')
ex = os.environ.get('_EXPIRES', '')
nc = os.environ.get('_NOTIFY_CH', '')
sf = os.environ.get('_SCHED_FREQ', '')
st = os.environ.get('_SCHED_TIME', '')
sd = os.environ.get('_SCHED_DAYS', '')
sm = os.environ.get('_SCHED_DOM', '')
en = os.environ.get('_ENABLED', '')
if cmd: d['command'] = cmd
if name: d['name'] = name
if cr: d['cron'] = cr
if rs: d['recurring_seconds'] = int(rs)
if ml: d['model'] = ml
if pv: d['provider'] = pv
if ex: d['expires_at'] = float(ex)
if nc: d['notify_channel'] = nc
if en == 'true': d['enabled'] = True
if en == 'false': d['enabled'] = False
if sf:
    sched = {'frequency': sf, 'time': st}
    if sf == 'weekly' and sd:
        sched['days'] = [x.strip() for x in sd.split(',')]
    if sf == 'monthly' and sm:
        sched['day_of_month'] = int(sm)
    d['schedule'] = sched
if not d:
    print('{}')
else:
    print(json.dumps(d))
")

    if [[ "$PAYLOAD" == "{}" ]]; then
        echo "error: no changes specified. use flags like --weekdays 09:00, --run \"cmd\", --name \"x\"" >&2
        exit 1
    fi

    RESPONSE=$(curl -s -X PUT "$API_URL/$jid" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null)

    UPDATED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('updated', False))" 2>/dev/null || echo "False")

    if [[ "$UPDATED" == "True" ]]; then
        echo "$RESPONSE" | python3 -c "
import sys, json
r = json.load(sys.stdin)
j = r['job']
jid = j['id'][:8]
name = j.get('name', jid)
sched = r.get('cron_human', '')
if not sched and j.get('cron'):
    sched = j['cron']
runs = r.get('next_runs', [])
run_str = ''
if runs:
    run_str = ' | next: ' + runs[0].get('relative', runs[0].get('iso', ''))
print(f'[job] updated: \"{name}\" — {sched}{run_str} [{jid}]')
"
    else
        echo "[job] failed: $RESPONSE" >&2
        exit 1
    fi
}

# ── main ─────────────────────────────────────────────────────────────────────

if [[ $# -eq 0 ]]; then
    echo "usage:"
    echo "  job create \"name\" --run \"cmd\" --weekdays 09:00        # Mon-Fri at 9am"
    echo "  job create \"name\" --run \"cmd\" --daily 12:00           # every day at noon"
    echo "  job create \"name\" --run \"cmd\" --weekly mon,fri 10:00  # specific days"
    echo "  job create \"name\" --run \"cmd\" --monthly 1 09:00       # 1st of month"
    echo "  job create \"name\" --run \"cmd\" --weekends 10:00        # Sat+Sun"
    echo "  job create \"name\" --run \"cmd\" --every 1h              # every hour"
    echo "  job create \"name\" --run \"cmd\" --in 5m                 # one-shot delay"
    echo "  job create \"name\" --run \"cmd\" --at 09:00              # one-shot at time"
    echo "  job create \"name\" --run \"cmd\" --cron \"0 9 * * 1-5\"    # raw cron (advanced)"
    echo "  job edit <id> --weekdays 10:00                         # change schedule"
    echo "  job edit <id> --run \"new-cmd\"                          # change command"
    echo "  job edit <id> --name \"new-name\" --daily 08:00          # change multiple"
    echo "  job edit <id> --disable / --enable                     # toggle on/off"
    echo "  job list                                               # list active jobs"
    echo "  job cancel <id>                                        # cancel by ID"
    echo "  job run <id>                                           # trigger immediately"
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
    edit)
        if [[ -z "${2:-}" ]]; then
            echo "usage: job edit <id> --weekdays 09:00  (change schedule, command, name, etc.)" >&2
            exit 1
        fi
        jid="$2"
        shift 2
        cmd_edit "$jid" "$@"
        ;;
    *)
        echo "error: unknown command '$1'. use: create, edit, list, cancel, run" >&2
        exit 1
        ;;
esac
