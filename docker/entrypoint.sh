#!/bin/bash
set -e

echo "========================================"
echo "  gooseclaw — personal AI agent"
echo "========================================"
echo ""

APP_DIR="/app"
DATA_DIR="/data"
CONFIG_DIR="/data/config"
IDENTITY_DIR="/data/identity"
HOME_DIR="${HOME:-/root}"

export APP_DIR DATA_DIR CONFIG_DIR IDENTITY_DIR

# ensure ~/.local/bin is in PATH (claude CLI installs there)
export PATH="$HOME_DIR/.local/bin:$PATH"

# ─── first boot: copy template files to volume ─────────────────────────────

if [ ! -f "$DATA_DIR/.initialized" ]; then
    echo "[init] first boot detected. setting up /data..."
    mkdir -p "$IDENTITY_DIR/journal" "$CONFIG_DIR" "$DATA_DIR/sessions" "$DATA_DIR/recipes" "$DATA_DIR/secrets"
    chmod 700 "$DATA_DIR/secrets"
    touch "$DATA_DIR/secrets/vault.yaml"
    chmod 600 "$DATA_DIR/secrets/vault.yaml"

    # copy template identity files
    cp -r "$APP_DIR/identity/"* "$IDENTITY_DIR/"
    echo "[init] identity files copied to $IDENTITY_DIR/"

    touch "$DATA_DIR/.initialized"
    echo "[init] first boot setup complete"
else
    echo "[init] existing data found at /data. using it."
fi

# ─── goose config ───────────────────────────────────────────────────────────

# symlink goose config directory to volume
mkdir -p "$HOME_DIR/.config"
rm -rf "$HOME_DIR/.config/goose"
ln -sf "$CONFIG_DIR" "$HOME_DIR/.config/goose"

# symlink sessions db to volume for persistence across deploys
mkdir -p "$HOME_DIR/.local/share/goose/sessions"
if [ -f "$DATA_DIR/sessions/sessions.db" ]; then
    ln -sf "$DATA_DIR/sessions/sessions.db" "$HOME_DIR/.local/share/goose/sessions/sessions.db"
fi

# preserve gateway state (pairings, configs, pending codes) across restarts
# — goose writes these to config.yaml at runtime, and we'd lose them on regen
GATEWAY_STATE=""
if [ -f "$CONFIG_DIR/config.yaml" ]; then
    GATEWAY_STATE=$(python3 -c "
lines = open('$CONFIG_DIR/config.yaml').readlines()
in_gw = False
buf = []
gw_keys = ('gateway_pairings:', 'gateway_configs:', 'gateway_pending_codes:')
for line in lines:
    if any(line.startswith(k) for k in gw_keys):
        in_gw = True
        buf.append(line)
    elif in_gw:
        if line and not line[0].isspace() and not line.strip().startswith('-'):
            in_gw = False
            if any(line.startswith(k) for k in gw_keys):
                in_gw = True
                buf.append(line)
        else:
            buf.append(line)
print(''.join(buf), end='')
" 2>/dev/null || true)
fi

# generate base config.yaml (provider may be added by env vars or setup wizard)
cat > "$CONFIG_DIR/config.yaml" << YAML
keyring: false
GOOSE_MODE: auto
GOOSE_CONTEXT_STRATEGY: summarize
GOOSE_MAX_TURNS: 50
GOOSE_DISABLE_SESSION_NAMING: true
YAML

# ─── provider setup (env vars — optional, setup wizard is the alternative) ─

PROVIDER_CONFIGURED=false

if [ -n "${CLAUDE_SETUP_TOKEN:-}" ]; then
    echo "[provider] claude subscription (setup-token)"

    # install claude CLI if not present
    if command -v claude &>/dev/null; then
        echo "[provider] claude CLI already installed"
    else
        echo "[provider] installing claude CLI..."
        curl -fsSL https://claude.ai/install.sh | bash 2>/dev/null || {
            echo "[provider] native install failed, trying npm..."
            apt-get update -qq && apt-get install -y -qq nodejs npm >/dev/null 2>&1
            npm install -g @anthropic-ai/claude-code 2>/dev/null || {
                echo "[provider] ERROR: could not install claude CLI"
                exit 1
            }
        }
    fi

    export CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_SETUP_TOKEN"
    mkdir -p "$HOME_DIR/.claude"
    cat > "$HOME_DIR/.claude.json" << CJSON
{
    "hasCompletedOnboarding": true
}
CJSON
    echo "GOOSE_PROVIDER: claude-code" >> "$CONFIG_DIR/config.yaml"
    PROVIDER_CONFIGURED=true

elif [ -n "${GOOSE_API_KEY:-}" ]; then
    PROVIDER="${GOOSE_PROVIDER:-anthropic}"
    echo "[provider] API key ($PROVIDER)"
    echo "GOOSE_PROVIDER: $PROVIDER" >> "$CONFIG_DIR/config.yaml"

    case "$PROVIDER" in
        anthropic)  export ANTHROPIC_API_KEY="$GOOSE_API_KEY" ;;
        openai)     export OPENAI_API_KEY="$GOOSE_API_KEY" ;;
        google)     export GOOGLE_API_KEY="$GOOSE_API_KEY" ;;
        groq)       export GROQ_API_KEY="$GOOSE_API_KEY" ;;
        openrouter) export OPENROUTER_API_KEY="$GOOSE_API_KEY" ;;
        *)          export GOOSE_PROVIDER__API_KEY="$GOOSE_API_KEY" ;;
    esac
    PROVIDER_CONFIGURED=true

elif [ -n "${CUSTOM_PROVIDER_URL:-}" ]; then
    echo "[provider] custom provider ($CUSTOM_PROVIDER_URL)"
    mkdir -p "$CONFIG_DIR/custom_providers"
    cat > "$CONFIG_DIR/custom_providers/custom.json" << CPJSON
{
    "name": "custom",
    "provider_type": "openai",
    "host": "$CUSTOM_PROVIDER_URL",
    "model": "${CUSTOM_PROVIDER_MODEL:-gpt-4}",
    "api_key": "${CUSTOM_PROVIDER_KEY:-}"
}
CPJSON
    echo "GOOSE_PROVIDER: custom" >> "$CONFIG_DIR/config.yaml"
    PROVIDER_CONFIGURED=true

elif [ -f "$CONFIG_DIR/setup.json" ]; then
    echo "[provider] configured via setup wizard"
    PROVIDER_CONFIGURED=true

    # re-hydrate env vars from setup.json (needed after container restart)
    # env vars set via Railway/Docker take priority over stored values
    REHYDRATE_FILE=$(mktemp /tmp/rehydrate.XXXXXX)
    python3 -c "
import json, os, shlex
c = json.load(open('$CONFIG_DIR/setup.json'))
pt = c.get('provider_type', '')
ak = c.get('api_key', '')
# single-key providers: provider -> env var name
env_map = {
    'anthropic': 'ANTHROPIC_API_KEY',
    'openai': 'OPENAI_API_KEY',
    'google': 'GOOGLE_API_KEY',
    'groq': 'GROQ_API_KEY',
    'openrouter': 'OPENROUTER_API_KEY',
    'mistral': 'MISTRAL_API_KEY',
    'xai': 'XAI_API_KEY',
    'deepseek': 'DEEPSEEK_API_KEY',
    'together': 'TOGETHER_API_KEY',
    'cerebras': 'CEREBRAS_API_KEY',
    'perplexity': 'PERPLEXITY_API_KEY',
    'avian': 'AVIAN_API_KEY',
    'venice': 'VENICE_API_KEY',
    'ovhcloud': 'OVH_AI_ENDPOINTS_ACCESS_TOKEN',
    'litellm': 'LITELLM_API_KEY',
}
if pt == 'claude-code' and c.get('claude_setup_token'):
    if not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'):
        print(f'export CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(c[\"claude_setup_token\"])}')
elif pt == 'azure-openai':
    # setup.json stores azure_key and azure_endpoint (not api_key)
    azure_key = c.get('azure_key', '')
    azure_endpoint = c.get('azure_endpoint', '')
    if azure_key and not os.environ.get('AZURE_OPENAI_API_KEY'):
        print(f'export AZURE_OPENAI_API_KEY={shlex.quote(azure_key)}')
    if azure_endpoint and not os.environ.get('AZURE_OPENAI_ENDPOINT'):
        print(f'export AZURE_OPENAI_ENDPOINT={shlex.quote(azure_endpoint)}')
elif pt == 'litellm':
    # export api_key as LITELLM_API_KEY (host is not stored via setup wizard)
    if ak and not os.environ.get('LITELLM_API_KEY'):
        print(f'export LITELLM_API_KEY={shlex.quote(ak)}')
    litellm_host = c.get('litellm_host', '')
    if litellm_host and not os.environ.get('LITELLM_HOST'):
        print(f'export LITELLM_HOST={shlex.quote(litellm_host)}')
elif pt == 'github-copilot':
    # github-copilot: export GITHUB_TOKEN from api_key if stored
    if ak and not os.environ.get('GITHUB_TOKEN'):
        print(f'export GITHUB_TOKEN={shlex.quote(ak)}')
elif pt in ('ollama', 'lm-studio', 'docker-model-runner', 'ramalama'):
    # local providers: export OLLAMA_HOST if a host URL was stored
    ollama_host = c.get('ollama_host', '')
    if ollama_host and not os.environ.get('OLLAMA_HOST'):
        print(f'export OLLAMA_HOST={shlex.quote(ollama_host)}')
elif pt in env_map and ak:
    if not os.environ.get(env_map[pt]):
        print(f'export {env_map[pt]}={shlex.quote(ak)}')
tg = c.get('telegram_bot_token', '')
if tg and not os.environ.get('TELEGRAM_BOT_TOKEN'):
    print(f'export TELEGRAM_BOT_TOKEN={shlex.quote(tg)}')
tz = c.get('timezone', '')
if tz and not os.environ.get('TZ'):
    print(f'export TZ={shlex.quote(tz)}')
# lead/worker multi-model settings -> export to env for config.yaml
lp = c.get('lead_provider', '')
lm = c.get('lead_model', '')
ltc = c.get('lead_turn_count', '')
if lp:
    print(f'export GOOSE_LEAD_PROVIDER={shlex.quote(lp)}')
if lm:
    print(f'export GOOSE_LEAD_MODEL={shlex.quote(lm)}')
if ltc:
    print(f'export GOOSE_LEAD_TURN_COUNT={shlex.quote(str(ltc))}')
" > "$REHYDRATE_FILE" 2>/dev/null
    # source is safe because values are shlex.quote'd by the Python script
    . "$REHYDRATE_FILE"
    rm -f "$REHYDRATE_FILE"
fi

if [ "$PROVIDER_CONFIGURED" = false ]; then
    echo "[provider] no provider configured yet. setup wizard will handle it."
fi

# model override
if [ -n "${GOOSE_MODEL:-}" ]; then
    echo "GOOSE_MODEL: $GOOSE_MODEL" >> "$CONFIG_DIR/config.yaml"
fi

# lead/worker multi-model settings
if [ -n "${GOOSE_LEAD_PROVIDER:-}" ]; then
    echo "GOOSE_LEAD_PROVIDER: $GOOSE_LEAD_PROVIDER" >> "$CONFIG_DIR/config.yaml"
fi
if [ -n "${GOOSE_LEAD_MODEL:-}" ]; then
    echo "GOOSE_LEAD_MODEL: $GOOSE_LEAD_MODEL" >> "$CONFIG_DIR/config.yaml"
fi
if [ -n "${GOOSE_LEAD_TURN_COUNT:-}" ]; then
    echo "GOOSE_LEAD_TURN_COUNT: $GOOSE_LEAD_TURN_COUNT" >> "$CONFIG_DIR/config.yaml"
fi

# ─── vault hydration (export stored credentials as env vars) ─────────────

VAULT_FILE="$DATA_DIR/secrets/vault.yaml"
if [ -f "$VAULT_FILE" ] && [ -s "$VAULT_FILE" ]; then
    echo "[vault] hydrating credentials from vault..."
    VAULT_REHYDRATE_FILE=$(mktemp /tmp/vault_rehydrate.XXXXXX)
    python3 -c "
import yaml, sys, os, re, shlex
try:
    with open('$VAULT_FILE') as f:
        data = yaml.safe_load(f) or {}
    # known env var mappings for common integrations
    env_map = {
        'anthropic.api_key': 'ANTHROPIC_API_KEY',
        'openai.api_key': 'OPENAI_API_KEY',
        'google.api_key': 'GOOGLE_API_KEY',
        'groq.api_key': 'GROQ_API_KEY',
        'openrouter.api_key': 'OPENROUTER_API_KEY',
        'fireflies.api_key': 'FIREFLIES_API_KEY',
        'browserbase.api_key': 'BROWSERBASE_API_KEY',
        'exa.api_key': 'EXA_API_KEY',
        'brave.api_key': 'BRAVE_API_KEY',
        'supabase.url': 'SUPABASE_URL',
        'supabase.key': 'SUPABASE_KEY',
        'github.pat': 'GITHUB_PAT',
    }
    for dotpath, env_var in env_map.items():
        keys = dotpath.split('.')
        val = data
        try:
            for k in keys:
                val = val[k]
            # only export if not already set (env vars take priority)
            if not os.environ.get(env_var):
                print(f'export {env_var}={shlex.quote(str(val))}')
        except (KeyError, TypeError):
            pass
    # also export any custom keys as GOOSECLAW_<SERVICE>_<KEY>
    for service, values in data.items():
        if isinstance(values, dict):
            for key, val in values.items():
                env_name = f'GOOSECLAW_{service.upper()}_{key.upper()}'
                # sanitize: replace any non-alphanumeric/underscore with underscore
                env_name = re.sub(r'[^A-Z0-9_]', '_', env_name)
                if not os.environ.get(env_name):
                    print(f'export {env_name}={shlex.quote(str(val))}')
except Exception as e:
    print(f'echo \"[vault] WARN: {e}\"', file=sys.stderr)
" > "$VAULT_REHYDRATE_FILE" 2>/dev/null
    # source is safe because values are shlex.quote'd by the Python script
    . "$VAULT_REHYDRATE_FILE"
    rm -f "$VAULT_REHYDRATE_FILE"
    echo "[vault] done"
else
    echo "[vault] no credentials stored yet"
fi

# ─── default MCP extensions (Context7 + Exa, no API keys needed) ─────────

echo "[mcp] configuring default extensions (Context7, Exa)..."
cat >> "$CONFIG_DIR/config.yaml" << 'EXTENSIONS'
extensions:
  developer:
    enabled: true
    type: platform
    name: developer
    description: Write and edit files, and execute shell commands
    display_name: Developer
    bundled: true
    available_tools: []
  tom:
    enabled: true
    type: platform
    name: tom
    description: Inject custom context into every turn via GOOSE_MOIM_MESSAGE_TEXT and GOOSE_MOIM_MESSAGE_FILE environment variables
    display_name: Top Of Mind
    bundled: true
    available_tools: []
  todo:
    enabled: true
    type: platform
    name: todo
    description: Enable a todo list for goose so it can keep track of what it is doing
    display_name: Todo
    bundled: true
    available_tools: []
  memory:
    enabled: true
    type: builtin
    name: memory
    description: Teach goose your preferences as you go.
    display_name: Memory
    timeout: 300
    bundled: true
    available_tools: []
  context7:
    enabled: true
    type: stdio
    name: Context7
    description: Up-to-date code documentation for LLMs and AI code editors
    cmd: npx
    args:
      - -y
      - '@upstash/context7-mcp'
    envs: {}
    env_keys: []
    timeout: 300
    bundled: null
    available_tools: []
  exa:
    enabled: true
    type: streamable_http
    name: Exa
    description: Exa MCP for web search and web crawling
    uri: https://mcp.exa.ai/mcp
    envs: {}
    env_keys: []
    headers: {}
    timeout: 300
    bundled: null
    available_tools: []
EXTENSIONS

# restore preserved gateway state (pairings, sessions, pending codes)
if [ -n "$GATEWAY_STATE" ]; then
    echo "$GATEWAY_STATE" >> "$CONFIG_DIR/config.yaml"
    echo "[init] gateway pairings preserved across restart"
fi

# ─── template version tracking ────────────────────────────────────────────

TEMPLATE_VERSION_FILE="$APP_DIR/VERSION"
DATA_VERSION_FILE="$DATA_DIR/VERSION"
if [ -f "$TEMPLATE_VERSION_FILE" ]; then
    TEMPLATE_VER=$(cat "$TEMPLATE_VERSION_FILE")
    DATA_VER=$(cat "$DATA_VERSION_FILE" 2>/dev/null || echo "0.0.0")
    if [ "$TEMPLATE_VER" != "$DATA_VER" ]; then
        echo "[upgrade] template updated: $DATA_VER -> $TEMPLATE_VER"
        # update system files (tools.md, persistent-instructions.md)
        # but NEVER overwrite user files (soul.md, user.md, memory.md, heartbeat.md)
        for f in tools.md persistent-instructions.md; do
            if [ -f "$APP_DIR/identity/$f" ]; then
                cp "$APP_DIR/identity/$f" "$IDENTITY_DIR/$f"
                echo "[upgrade] updated $f"
            fi
        done
        echo "$TEMPLATE_VER" > "$DATA_VERSION_FILE"
        echo "[upgrade] done"
    fi
fi

# ─── MOIM (persistent instructions injected every turn) ────────────────────

export GOOSE_MOIM_MESSAGE_FILE="$IDENTITY_DIR/persistent-instructions.md"

# ─── git persistence setup ─────────────────────────────────────────────────

if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
    echo "[persist] git persistence enabled (${GITHUB_REPO})"
    export GIT_USER_NAME="${GIT_USER_NAME:-gooseclaw-agent}"
    export GIT_USER_EMAIL="${GIT_USER_EMAIL:-gooseclaw@users.noreply.github.com}"
    export PERSIST_BRANCH="${PERSIST_BRANCH:-main}"

    git config --global user.name "$GIT_USER_NAME"
    git config --global user.email "$GIT_USER_EMAIL"
else
    echo "[persist] git persistence disabled (no GITHUB_PAT/GITHUB_REPO)"
    echo "[persist] state persists on Railway volume only"
fi

# ─── prepare non-root runtime ─────────────────────────────────────────────
# claude CLI refuses --dangerously-skip-permissions as root (security feature),
# but goose requires that flag for headless operation. solution: run all goose
# processes as the gooseclaw user.

GCLAW_HOME="/home/gooseclaw"

# claude CLI: copy/symlink to gooseclaw's path
if [ -f "/root/.local/bin/claude" ] || [ -L "/root/.local/bin/claude" ]; then
    CLAUDE_REAL=$(readlink -f /root/.local/bin/claude 2>/dev/null || echo /root/.local/bin/claude)
    mkdir -p "$GCLAW_HOME/.local/bin"
    # symlink the actual binary (could be large, avoid copying)
    ln -sf "$CLAUDE_REAL" "$GCLAW_HOME/.local/bin/claude"
    echo "[runtime] claude CLI linked to $GCLAW_HOME/.local/bin/"
fi

# claude config files
mkdir -p "$GCLAW_HOME/.claude"
cp /root/.claude.json "$GCLAW_HOME/.claude.json" 2>/dev/null || true
cp -a /root/.claude/* "$GCLAW_HOME/.claude/" 2>/dev/null || true

# goose config: symlink to shared volume
mkdir -p "$GCLAW_HOME/.config"
rm -rf "$GCLAW_HOME/.config/goose"
ln -sf "$CONFIG_DIR" "$GCLAW_HOME/.config/goose"

# goose data directories
mkdir -p "$GCLAW_HOME/.local/share/goose/sessions" "$GCLAW_HOME/.local/state/goose/logs"
mkdir -p "$DATA_DIR/sessions"
# always symlink so goose writes sessions.db to the persistent volume
ln -sf "$DATA_DIR/sessions/sessions.db" "$GCLAW_HOME/.local/share/goose/sessions/sessions.db"

# fix ownership: gooseclaw needs write access to /data and its own home
chown -R gooseclaw:gooseclaw "$DATA_DIR" "$GCLAW_HOME"

# set HOME for all child processes
export HOME="$GCLAW_HOME"

echo "[runtime] non-root user prepared (gooseclaw)"

# ─── start gateway (setup wizard + reverse proxy to goose web) ────────────

echo "[gateway] starting gateway on port ${PORT:-8080}..."
runuser -u gooseclaw -- python3 "$APP_DIR/docker/gateway.py" &
GATEWAY_PY_PID=$!

# ─── telegram gateway ─────────────────────────────────────────────────────
# NOTE: telegram lifecycle is managed by gateway.py (start_telegram_gateway).
# entrypoint does NOT start telegram directly to avoid duplicate processes
# (two goose gateway instances = pairing loop bug).
# gateway.py reads TELEGRAM_BOT_TOKEN from env or setup.json on boot.

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "[telegram] TELEGRAM_BOT_TOKEN set. gateway.py will start the bot."
else
    echo "[telegram] TELEGRAM_BOT_TOKEN not set. configure via setup wizard."
fi
TELEGRAM_PID=""

# ─── start persist loop (if git enabled) ───────────────────────────────────

if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
    PERSIST_INTERVAL="${PERSIST_INTERVAL:-300}"
    echo "[persist] loop started (every ${PERSIST_INTERVAL}s)"

    while true; do
        sleep "$PERSIST_INTERVAL"
        "$APP_DIR/scripts/persist.sh" 2>&1 | while read -r line; do
            echo "[persist] $line"
        done
    done &
    PERSIST_PID=$!
fi

# ─── watchdog (restart crashed processes) ──────────────────────────────────

(
    sleep 30  # initial grace period
    while true; do
        sleep 60

        # check gateway
        if ! kill -0 "$GATEWAY_PY_PID" 2>/dev/null; then
            echo "[watchdog] gateway crashed, restarting..."
            runuser -u gooseclaw -- python3 "$APP_DIR/docker/gateway.py" &
            GATEWAY_PY_PID=$!
            echo "[watchdog] gateway restarted (pid $GATEWAY_PY_PID)"
        fi

        # telegram lifecycle managed by gateway.py — no watchdog needed here
    done
) &
WATCHDOG_PID=$!

# ─── SIGTERM handler (graceful shutdown) ────────────────────────────────────

shutdown() {
    echo "[shutdown] SIGTERM received"

    if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
        echo "[shutdown] persisting final state..."
        "$APP_DIR/scripts/persist.sh" 2>&1 || true
    fi

    # send SIGTERM to gateway (it handles its own children) and wait for it
    if kill -0 "$GATEWAY_PY_PID" 2>/dev/null; then
        kill -TERM "$GATEWAY_PY_PID"
        wait "$GATEWAY_PY_PID" 2>/dev/null || true
    fi
    [ -n "${PERSIST_PID:-}" ] && kill -TERM "$PERSIST_PID" 2>/dev/null || true
    [ -n "${WATCHDOG_PID:-}" ] && kill -TERM "$WATCHDOG_PID" 2>/dev/null || true

    echo "[shutdown] done"
    exit 0
}
trap shutdown SIGTERM SIGINT

echo "[gooseclaw] agent is live!"
echo ""

# wait for any process to exit
wait -n "$GATEWAY_PY_PID"
echo "[gooseclaw] process exited unexpectedly, shutting down..."
exit 1
