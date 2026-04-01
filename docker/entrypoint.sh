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
    mkdir -p "$IDENTITY_DIR/journal" "$IDENTITY_DIR/learnings" "$CONFIG_DIR" "$DATA_DIR/sessions" "$DATA_DIR/recipes" "$DATA_DIR/secrets" "$DATA_DIR/plugins"
    chmod 700 "$DATA_DIR/secrets"
    touch "$DATA_DIR/secrets/vault.yaml"
    chmod 600 "$DATA_DIR/secrets/vault.yaml"

    # copy template identity files
    cp -r "$APP_DIR/identity/"* "$IDENTITY_DIR/"
    echo "[init] identity files copied to $IDENTITY_DIR/"

    # generate recovery secret for password reset
    RECOVERY_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    echo "$RECOVERY_SECRET" > "$DATA_DIR/.recovery_secret"
    chmod 600 "$DATA_DIR/.recovery_secret"
    echo "[init] recovery secret generated and saved to /data/.recovery_secret"
    echo "[init] TIP: copy this to Railway env vars as GOOSECLAW_RECOVERY_SECRET for easy access"
    echo "[init] recovery secret saved to /data/.recovery_secret (cat /data/.recovery_secret to retrieve)"

    touch "$DATA_DIR/.initialized"
    echo "[init] first boot setup complete"
else
    echo "[init] existing data found at /data. using it."
fi

# ─── recovery secret ──────────────────────────────────────────────────────────

# load recovery secret from persistent storage if env var not set
if [ -z "$GOOSECLAW_RECOVERY_SECRET" ] && [ -f "$DATA_DIR/.recovery_secret" ]; then
    export GOOSECLAW_RECOVERY_SECRET=$(cat "$DATA_DIR/.recovery_secret")
    echo "[init] recovery secret loaded from /data"
fi

# ─── emergency password reset via env var ─────────────────────────────────────

if [ -n "$GOOSECLAW_RESET_PASSWORD" ]; then
    echo "[init] GOOSECLAW_RESET_PASSWORD detected, resetting password..."
    _DATA_DIR="$DATA_DIR" _RESET_PW="$GOOSECLAW_RESET_PASSWORD" python3 -c "
import json, hashlib, os, base64
setup_path = os.path.join(os.environ['_DATA_DIR'], 'config', 'setup.json')
if os.path.exists(setup_path):
    with open(setup_path) as f:
        setup = json.load(f)
    pw = os.environ['_RESET_PW']
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 600_000)
    salt_b64 = base64.b64encode(salt).decode()
    dk_b64 = base64.b64encode(dk).decode()
    setup['web_auth_token_hash'] = '\$pbkdf2\$' + salt_b64 + '\$' + dk_b64
    setup.pop('web_auth_token', None)
    with open(setup_path, 'w') as f:
        json.dump(setup, f, indent=2)
    print('[init] password reset to value of GOOSECLAW_RESET_PASSWORD (PBKDF2)')
else:
    print('[init] no setup.json found, skipping password reset')
"
    echo "[init] IMPORTANT: remove GOOSECLAW_RESET_PASSWORD from Railway env vars after login"
fi

# ─── persistent runtime installs ─────────────────────────────────────────────
# anything installed at runtime (pip, npm, binaries, apt, models, etc.) lives
# on the /data volume so it survives container rebuilds/deploys.

mkdir -p /data/bin /data/pip-packages/bin /data/npm-global/bin /data/lib

# pip → /data/pip-packages
# PYTHONPATH appends (not prepends) so system packages always win.
# prevents user-installed packages from breaking gateway dependencies.
export PIP_TARGET="/data/pip-packages"
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}/data/pip-packages"

# npm → /data/npm-global (global installs only)
# NPM_CONFIG_PREFIX only affects `npm install -g`. npx cache is separate
# (~/.npm/_npx) so MCP tools (context7, exa) are unaffected.
export NPM_CONFIG_PREFIX="/data/npm-global"

# all persistent dirs on PATH
export PATH="/data/bin:/data/pip-packages/bin:/data/npm-global/bin:$PATH"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}/data/lib"

echo "[init] runtime installs persist to /data (pip, npm, bin, lib)"

# ─── boot setup script (re-run user installs after deploy) ───────────────────
# /data/boot-setup.sh is a user/bot editable script that runs on every boot.
# the bot can append commands like "apt-get install -y ffmpeg" or
# "curl -L ... -o /data/bin/sometool" to make them survive deploys.
# each line runs independently so one failure doesn't block the rest.

if [ -f /data/boot-setup.sh ]; then
    echo "[init] running /data/boot-setup.sh..."
    chmod +x /data/boot-setup.sh
    if bash /data/boot-setup.sh 2>&1 | while IFS= read -r out; do echo "[boot-setup] $out"; done; then
        echo "[init] boot-setup complete"
    else
        echo "[init] boot-setup FAILED (exit $?) — continuing anyway"
    fi
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
GATEWAY_STATE_FILE=$(mktemp /tmp/gateway_state.XXXXXX.json)
EXTENSIONS_STATE_FILE=$(mktemp /tmp/extensions_state.XXXXXX.json)
if [ -f "$CONFIG_DIR/config.yaml" ]; then
    python3 -c "
import yaml, json, sys
try:
    with open('$CONFIG_DIR/config.yaml') as f:
        data = yaml.safe_load(f) or {}
    gw_keys = ('gateway_pairings', 'gateway_configs', 'gateway_pending_codes')
    state = {k: data[k] for k in gw_keys if k in data}
    if state:
        with open('$GATEWAY_STATE_FILE', 'w') as out:
            json.dump(state, out)
    # preserve user-customized extensions across reboots
    if 'extensions' in data and data['extensions']:
        with open('$EXTENSIONS_STATE_FILE', 'w') as out:
            json.dump({'extensions': data['extensions']}, out)
except Exception:
    pass
" 2>/dev/null || true
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
gek = c.get('groq_extraction_key', '')
if gek and not os.environ.get('GROQ_API_KEY'):
    print(f'export GROQ_API_KEY={shlex.quote(gek)}')
m0p = c.get('mem0_provider', '')
m0m = c.get('mem0_model', '')
if m0p:
    print(f'export MEM0_PROVIDER={shlex.quote(m0p)}')
if m0m:
    print(f'export MEM0_MODEL={shlex.quote(m0m)}')
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
    print(f'export GOOSE_LEAD_TURNS={shlex.quote(str(ltc))}')
lft = c.get('lead_failure_threshold', '')
lfbt = c.get('lead_fallback_turns', '')
lcl = c.get('lead_context_limit', '')
if lft:
    print(f'export GOOSE_LEAD_FAILURE_THRESHOLD={shlex.quote(str(lft))}')
if lfbt:
    print(f'export GOOSE_LEAD_FALLBACK_TURNS={shlex.quote(str(lfbt))}')
if lcl:
    print(f'export GOOSE_LEAD_CONTEXT_LIMIT={shlex.quote(str(lcl))}')
# fallback provider config -> export as JSON env vars for gateway.py
fb = c.get('fallback_providers', [])
if fb and isinstance(fb, list):
    print(f'export FALLBACK_PROVIDERS={shlex.quote(json.dumps(fb))}')
m0fb = c.get('mem0_fallback_providers', [])
if m0fb and isinstance(m0fb, list):
    print(f'export MEM0_FALLBACK_PROVIDERS={shlex.quote(json.dumps(m0fb))}')
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
if [ -n "${GOOSE_LEAD_TURNS:-}" ]; then
    echo "GOOSE_LEAD_TURNS: $GOOSE_LEAD_TURNS" >> "$CONFIG_DIR/config.yaml"
fi
if [ -n "${GOOSE_LEAD_FAILURE_THRESHOLD:-}" ]; then
    echo "GOOSE_LEAD_FAILURE_THRESHOLD: $GOOSE_LEAD_FAILURE_THRESHOLD" >> "$CONFIG_DIR/config.yaml"
fi
if [ -n "${GOOSE_LEAD_FALLBACK_TURNS:-}" ]; then
    echo "GOOSE_LEAD_FALLBACK_TURNS: $GOOSE_LEAD_FALLBACK_TURNS" >> "$CONFIG_DIR/config.yaml"
fi
if [ -n "${GOOSE_LEAD_CONTEXT_LIMIT:-}" ]; then
    echo "GOOSE_LEAD_CONTEXT_LIMIT: $GOOSE_LEAD_CONTEXT_LIMIT" >> "$CONFIG_DIR/config.yaml"
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

# ─── MCP extensions (preserve user customizations, defaults on first boot) ─

if [ -s "$EXTENSIONS_STATE_FILE" ]; then
    echo "[mcp] restoring user-customized extensions..."
    python3 -c "
import yaml, json, sys
try:
    with open('$EXTENSIONS_STATE_FILE') as f:
        ext_state = json.load(f)
    with open('$CONFIG_DIR/config.yaml') as f:
        config = yaml.safe_load(f) or {}
    config.update(ext_state)
    with open('$CONFIG_DIR/config.yaml', 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
except Exception as e:
    print(f'[mcp] WARN: could not restore extensions: {e}', file=sys.stderr)
" 2>/dev/null || true
else
    echo "[mcp] first boot — writing default extensions (Context7, Exa)..."
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
  knowledge:
    enabled: true
    type: stdio
    name: Knowledge
    description: Semantic knowledge base for procedures, integrations, and facts
    cmd: python3
    args:
      - /app/docker/knowledge/server.py
    envs:
      KNOWLEDGE_DB_PATH: /data/knowledge/chroma
    env_keys: []
    timeout: 300
    bundled: null
    available_tools: []
  mem0-memory:
    enabled: true
    type: stdio
    name: mem0-memory
    description: Long-term memory with semantic search and contradiction resolution
    cmd: python3
    args:
      - /app/docker/memory/server.py
    envs:
      MEM0_USER_ID: default
      MEM0_TELEMETRY: "false"
      OPENBLAS_NUM_THREADS: "1"
      HF_HUB_OFFLINE: "0"
      HF_HOME: /data/hf_cache
      TOKENIZERS_PARALLELISM: "false"
      MEM0_ENABLE_GRAPH: "true"
      MEM0_KUZU_PATH: /data/knowledge/kuzu
      MEM0_CHROMA_PATH: /data/mem0/chroma
      CONFIG_DIR: /data/config
      DATA_DIR: /data
    env_keys: []
    timeout: 300
    bundled: null
    available_tools: []
EXTENSIONS
fi
rm -f "$EXTENSIONS_STATE_FILE"

# --- auto-generated extensions (from registry.json) ----------------------------------------
REGISTRY_FILE="/data/extensions/registry.json"
if [ -f "$REGISTRY_FILE" ]; then
    echo "[mcp] loading auto-generated extensions from registry..."
    python3 -c "
import yaml, json, os, sys
try:
    with open('$REGISTRY_FILE') as f:
        registry = json.load(f)
    with open('$CONFIG_DIR/config.yaml') as f:
        config = yaml.safe_load(f) or {}
    exts = config.setdefault('extensions', {})
    added = []
    skipped = []
    for name, meta in registry.get('extensions', {}).items():
        if not meta.get('enabled', True):
            skipped.append(f'{name} (disabled)')
            continue
        sp = meta.get('server_path', '')
        if not os.path.isfile(sp):
            skipped.append(f'{name} (server.py missing)')
            continue
        exts[name] = {
            'enabled': True,
            'type': 'stdio',
            'name': name,
            'description': meta.get('description', f'Auto-generated {name} extension'),
            'cmd': 'python3',
            'args': [sp],
            'envs': {},
            'env_keys': [],
            'timeout': 300,
            'bundled': None,
            'available_tools': [],
        }
        added.append(name)
    if added:
        config['extensions'] = exts
        with open('$CONFIG_DIR/config.yaml', 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        print(f'[mcp] auto-generated extensions loaded: {\", \".join(added)}')
    if skipped:
        print(f'[mcp] auto-generated extensions skipped: {\", \".join(skipped)}', file=sys.stderr)
except Exception as e:
    print(f'[mcp] WARN: registry load failed: {e}', file=sys.stderr)
" 2>/dev/null || true
fi

# ─── patch auto-generated extensions for direct MCP support ──────────────────
# Existing generated server.py files may have unconditional sys.stdout redirect
# which breaks direct MCP stdio communication from gateway. Patch them.
python3 -c "
import glob, os
for f in glob.glob('/data/extensions/*/server.py'):
    try:
        content = open(f).read()
        if 'sys.stdout = sys.stderr' in content and 'MCP_DIRECT' not in content:
            content = content.replace(
                'sys.stdout = sys.stderr',
                'if not os.environ.get(\"MCP_DIRECT\"):\n    sys.stdout = sys.stderr'
            )
            # Ensure os is imported
            if 'import os' not in content:
                content = content.replace('import sys', 'import sys\nimport os', 1)
            open(f, 'w').write(content)
            print(f'[mcp] patched {f} for direct MCP support')
    except Exception as e:
        print(f'[mcp] WARN: failed to patch {f}: {e}')
" 2>&1 || true

# ─── sync new template extensions into existing configs ──────────────────────
# On upgrades, new extensions (like mem0-memory) need to be added to existing
# config.yaml files. This merges any missing template extensions without
# overwriting user customizations.

python3 -c "
import yaml, sys
try:
    # template extensions we require (add new ones here)
    required = {
        'mem0-memory': {
            'enabled': True, 'type': 'stdio', 'name': 'mem0-memory',
            'description': 'Long-term memory with semantic search and contradiction resolution',
            'cmd': 'python3', 'args': ['/app/docker/memory/server.py'],
            'envs': {'MEM0_USER_ID': 'default', 'MEM0_TELEMETRY': 'false', 'OPENBLAS_NUM_THREADS': '1', 'HF_HUB_OFFLINE': '0', 'HF_HOME': '/data/hf_cache', 'TOKENIZERS_PARALLELISM': 'false', 'MEM0_ENABLE_GRAPH': 'true', 'MEM0_KUZU_PATH': '/data/knowledge/kuzu', 'MEM0_CHROMA_PATH': '/data/mem0/chroma', 'CONFIG_DIR': '/data/config', 'DATA_DIR': '/data'},
            'env_keys': [],
            'timeout': 300, 'bundled': None, 'available_tools': [],
        },
    }
    with open('$CONFIG_DIR/config.yaml') as f:
        config = yaml.safe_load(f) or {}
    exts = config.get('extensions', {})
    updated = []
    for name, defn in required.items():
        if name not in exts:
            exts[name] = defn
            updated.append(f'{name} (added)')
        else:
            # force-update envs and env_keys on existing extensions
            # so config changes (HF_HOME, NEO4J_PASSWORD, etc.) propagate
            changed = False
            if exts[name].get('envs') != defn.get('envs'):
                exts[name]['envs'] = defn['envs']
                changed = True
            if exts[name].get('env_keys') != defn.get('env_keys'):
                exts[name]['env_keys'] = defn['env_keys']
                changed = True
            if changed:
                updated.append(f'{name} (envs updated)')
    # inject mem0 provider API key into mem0 extension envs
    import json as _json
    try:
        with open('$CONFIG_DIR/setup.json') as _sf:
            _setup = _json.load(_sf)
        import os as _os
        _m0p = _setup.get('mem0_provider', '') or 'groq'
        _key_env_map = {
            'groq': 'GROQ_API_KEY',
            'openai': 'OPENAI_API_KEY',
            'anthropic': 'ANTHROPIC_API_KEY',
            'google': 'GOOGLE_API_KEY',
            'deepseek': 'DEEPSEEK_API_KEY',
            'together': 'TOGETHER_API_KEY',
        }
        _vault_key_map = {
            'groq': ['groq_api_key', 'GROQ_API_KEY'],
            'openai': ['openai_api_key', 'OPENAI_API_KEY'],
            'anthropic': ['ANTHROPIC_SECRET_KEY', 'ANTHROPIC_API_KEY', 'anthropic_api_key'],
            'google': ['google_api_key', 'GOOGLE_API_KEY'],
            'deepseek': ['deepseek_api_key', 'DEEPSEEK_API_KEY'],
            'together': ['together_api_key', 'TOGETHER_API_KEY'],
        }
        _target_env = _key_env_map.get(_m0p, 'GROQ_API_KEY')
        _mk = ''
        # 1. legacy groq_extraction_key
        if _m0p == 'groq':
            _mk = _setup.get('groq_extraction_key', '')
        # 2. vault
        if not _mk:
            _vault_path = _os.path.join('$DATA_DIR', 'secrets', 'vault.yaml')
            if _os.path.exists(_vault_path):
                with open(_vault_path) as _vf:
                    _vault = yaml.safe_load(_vf) or {}
                for _vk in _vault_key_map.get(_m0p, []):
                    _mk = _vault.get(_vk, '')
                    if _mk:
                        break
        # 3. env var
        if not _mk:
            _mk = _os.environ.get(_target_env, '')
        # 4. saved_keys
        if not _mk:
            _saved = _setup.get('saved_keys', {})
            if isinstance(_saved, dict):
                _mk = _saved.get(_m0p, '')
        if _mk and 'mem0-memory' in exts:
            exts['mem0-memory'].setdefault('envs', {})[_target_env] = _mk
            updated.append(f'mem0-memory ({_target_env} injected)')
    except Exception:
        pass
    if updated:
        config['extensions'] = exts
        with open('$CONFIG_DIR/config.yaml', 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        print(f'[mcp] synced extensions: {\", \".join(updated)}')
except Exception as e:
    print(f'[mcp] WARN: extension sync failed: {e}', file=sys.stderr)
" 2>/dev/null || true

# restore preserved gateway state (pairings, sessions, pending codes)
if [ -s "$GATEWAY_STATE_FILE" ]; then
    python3 -c "
import yaml, json, sys
try:
    with open('$GATEWAY_STATE_FILE') as f:
        state = json.load(f)
    if not state:
        sys.exit(0)
    with open('$CONFIG_DIR/config.yaml') as f:
        config = yaml.safe_load(f) or {}
    config.update(state)
    with open('$CONFIG_DIR/config.yaml', 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
except Exception as e:
    print(f'[init] WARN: could not restore gateway state: {e}', file=sys.stderr)
" 2>/dev/null || true
    echo "[init] gateway pairings preserved across restart"
fi
rm -f "$GATEWAY_STATE_FILE"

# ─── system file sync (every boot) ────────────────────────────────────────
# system files always match the image. user files (soul.md, user.md, memory.md)
# are never touched here — they live on the volume and belong to the user.

for f in system.md system-core.md turn-rules.md onboarding.md; do
    if [ -f "$APP_DIR/identity/$f" ]; then
        cp "$APP_DIR/identity/$f" "$IDENTITY_DIR/$f"
    fi
done
if [ -d "$APP_DIR/identity/schemas" ]; then
    mkdir -p "$IDENTITY_DIR/schemas"
    cp "$APP_DIR/identity/schemas/"*.schema.md "$IDENTITY_DIR/schemas/"
fi
echo "[init] system files synced"

# track version for informational purposes
if [ -f "$APP_DIR/VERSION" ]; then
    cp "$APP_DIR/VERSION" "$DATA_DIR/VERSION"
fi

# ─── knowledge base (vector search for system docs) ──────────────────────
# Re-indexes system namespace on every boot (system.md, onboarding.md, schemas/).
# Runtime namespace (user facts, integrations) is never wiped.

# one-time nuke of ALL chroma stores (compactor + format corruption)
if [ ! -f /data/.chroma_reset_v2 ]; then
    echo "[chroma] resetting all chroma stores for clean 1.5.5 init..."
    rm -rf /data/mem0/chroma /data/knowledge/chroma
    touch /data/.chroma_reset_v2
fi

# chroma + cache dirs. kuzu creates its own db dir, so do NOT mkdir it
mkdir -p /data/knowledge/chroma /data/mem0/chroma /data/hf_cache /data/chroma_cache
# remove empty kuzu dir if pre-created (kuzu rejects existing directories)
[ -d /data/knowledge/kuzu ] && [ -z "$(ls -A /data/knowledge/kuzu 2>/dev/null)" ] && rmdir /data/knowledge/kuzu 2>/dev/null || true
# persist chromadb model cache on volume (prevents re-download on every restart)
ln -sfn /data/chroma_cache /home/gooseclaw/.cache/chroma 2>/dev/null || true
chown -R gooseclaw:gooseclaw /data/knowledge /data/mem0 /data/hf_cache /data/chroma_cache

# Pre-download sentence-transformers model so MCP subprocess finds it cached.
# Runs once, persists on volume across deploys.
if [ ! -d "/data/hf_cache/hub/models--sentence-transformers--all-MiniLM-L6-v2" ]; then
    echo "[hf] downloading sentence-transformers model to persistent cache..."
    runuser -u gooseclaw -- env HF_HOME=/data/hf_cache python3 -c "
from sentence_transformers import SentenceTransformer
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
" 2>/dev/null && echo "[hf] model cached" || echo "[hf] WARNING: model download failed"
else
    echo "[hf] sentence-transformers model already cached"
fi

echo "[knowledge] indexing system knowledge base..."
if runuser -u gooseclaw -- env PYTHONPATH=/app/docker python3 /app/docker/knowledge/indexer.py; then
    echo "[knowledge] indexing complete"
else
    echo "[knowledge] WARNING: indexing failed (non-fatal, gateway will still start)"
fi

if [ -f "$IDENTITY_DIR/memory.md" ] && [ ! -f "$DATA_DIR/knowledge/.memory_migrated" ]; then
    echo "[knowledge] migrating memory.md to vector store..."
    if runuser -u gooseclaw -- env PYTHONPATH=/app/docker python3 /app/docker/knowledge/migrate_memory.py; then
        touch "$DATA_DIR/knowledge/.memory_migrated"
        echo "[knowledge] memory migration complete"
    else
        echo "[knowledge] WARNING: memory migration failed (non-fatal)"
    fi
fi

if [ ! -f "$DATA_DIR/knowledge/.mem0_migrated" ]; then
    echo "[mem0-migrate] migrating runtime memories to mem0..."
    if runuser -u gooseclaw -- env PYTHONPATH=/app/docker MEM0_USER_ID=default MEM0_TELEMETRY=false MEM0_CHROMA_PATH=/data/mem0/chroma HF_HOME=/data/hf_cache python3 /app/docker/knowledge/migrate_to_mem0.py; then
        echo "[mem0-migrate] migration complete"
    else
        echo "[mem0-migrate] WARNING: migration failed (non-fatal)"
    fi
fi

export KNOWLEDGE_DB_PATH="/data/knowledge/chroma"
export MEM0_CHROMA_PATH="/data/mem0/chroma"

# ---- graph memory (Kuzu persistent mode) ----
# NOTE: MEM0_ENABLE_GRAPH is set ONLY in the MCP server envs (not exported here)
# to prevent the gateway's mem0 instance from locking the kuzu db file.
# kuzu only allows one process to hold the lock at a time.
echo "[graph] graph memory enabled via MCP server (kuzu @ /data/knowledge/kuzu)"

# ─── MOIM (critical rules injected every turn, slim ~100 lines) ────────────
# Full session context (onboarding, procedures, docs) loads via .goosehints
# at session start. Only critical per-turn rules go through MOIM.

# ensure GOOSE_MODE is an env var (not just a YAML key) so claude-code
# provider receives --dangerously-skip-permissions flag
export GOOSE_MODE="${GOOSE_MODE:-auto}"

export GOOSE_MOIM_MESSAGE_FILE="$IDENTITY_DIR/turn-rules.md"
export GOOSE_MOIM_MESSAGE_TEXT="NON-NEGOTIABLE RULES — EVERY SINGLE TURN:
(1) RECALL FIRST: You are not starting from zero. Before you respond, call memory_search. Could a previous session have covered this? If maybe, search. Do not wait for the user to ask. Do not assume the current session has the full picture. NEVER ask for information you might already have.
(2) SAVE ALWAYS: Every conversation teaches you something. When you learn it, memory_add it. Silently, same turn. The test: if this session vanished right now, would you lose something you can't re-derive? If yes, save it. Emotional context counts. Do not ask. Do not announce. Just save.
(3) NEVER use CronCreate or CronDelete. Use job/remind bash CLI only."

# ─── .goosehints (session-start context, loads identity files) ─────────────
# goosed reads .goosehints from its working directory (/app).
# @file syntax in .goosehints only supports RELATIVE paths, so we symlink
# /data/identity into /app/identity-data so @identity-data/soul.md works.

ln -sfn /data/identity /app/identity-data
echo "[init] symlinked /app/identity-data -> /data/identity"

# goosed sessions use /data as working_dir, so .goosehints must be there too
ln -sfn /app/.goosehints /data/.goosehints
ln -sfn /data/identity /data/identity-data
echo "[init] linked .goosehints + identity-data into /data"

if [ ! -f "$IDENTITY_DIR/turn-rules.md" ]; then
    cp /app/identity/turn-rules.md "$IDENTITY_DIR/turn-rules.md"
fi

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
    # also symlink to /usr/bin so goosed extensions can find it
    ln -sf "$CLAUDE_REAL" /usr/bin/claude 2>/dev/null || true
    chmod +x "$CLAUDE_REAL" 2>/dev/null || true
    echo "[runtime] claude CLI linked to $GCLAW_HOME/.local/bin/ and /usr/bin/"
fi

# claude config files
mkdir -p "$GCLAW_HOME/.claude"
cp /root/.claude.json "$GCLAW_HOME/.claude.json" 2>/dev/null || true
cp -a /root/.claude/* "$GCLAW_HOME/.claude/" 2>/dev/null || true

# goose config: symlink to shared volume
mkdir -p "$GCLAW_HOME/.config"
rm -rf "$GCLAW_HOME/.config/goose"
ln -sf "$CONFIG_DIR" "$GCLAW_HOME/.config/goose"

# goose data directories — persist everything to the Railway volume
mkdir -p "$DATA_DIR/goose_data/sessions" "$DATA_DIR/goose_data/scheduled_recipes" "$DATA_DIR/plugins" "$IDENTITY_DIR/learnings"
mkdir -p "$GCLAW_HOME/.local/state/goose/logs"

# ─── migrate /data/channels → /data/plugins (one-time) ──────────────────────
if [ -d "$DATA_DIR/channels" ] && [ ! -L "$DATA_DIR/channels" ]; then
    # existing install: move plugin files to new dir, symlink old path
    if [ "$(ls -A "$DATA_DIR/channels" 2>/dev/null)" ]; then
        cp -a "$DATA_DIR/channels/"* "$DATA_DIR/plugins/" 2>/dev/null || true
    fi
    rm -rf "$DATA_DIR/channels"
    ln -sf "$DATA_DIR/plugins" "$DATA_DIR/channels"
    echo "[init] migrated /data/channels → /data/plugins (symlinked for compat)"
elif [ ! -e "$DATA_DIR/channels" ]; then
    # fresh install or already migrated: ensure symlink exists
    ln -sf "$DATA_DIR/plugins" "$DATA_DIR/channels"
fi

# symlink the entire goose share directory to the persistent volume
# this covers sessions.db, schedule.json, and scheduled_recipes/
rm -rf "$GCLAW_HOME/.local/share/goose"
mkdir -p "$GCLAW_HOME/.local/share"
ln -sf "$DATA_DIR/goose_data" "$GCLAW_HOME/.local/share/goose"

# fix ownership: gooseclaw needs write access to /data and its own home
chown -R gooseclaw:gooseclaw "$DATA_DIR" "$GCLAW_HOME"

# set HOME for all child processes
export HOME="$GCLAW_HOME"

echo "[runtime] non-root user prepared (gooseclaw)"

# ─── user-space boot services ────────────────────────────────────────────
# /data/boot-services.sh runs as gooseclaw (non-root) on every boot.
# Use this for background processes (bridges, daemons, watchers) that the
# gateway needs to be able to restart/kill at runtime.
# boot-setup.sh = root (installs). boot-services.sh = gooseclaw (processes).

if [ -f /data/boot-services.sh ]; then
    echo "[init] running /data/boot-services.sh as gooseclaw..."
    chmod +x /data/boot-services.sh
    if runuser -u gooseclaw -- bash /data/boot-services.sh 2>&1 | while IFS= read -r out; do echo "[boot-services] $out"; done; then
        echo "[init] boot-services complete"
    else
        echo "[init] boot-services FAILED (exit $?) — continuing anyway"
    fi
fi

# ─── start voice WebSocket server (websockets library, async) ─────────────

echo "[voice] starting voice server on port 8765..."
runuser -u gooseclaw -- python3 "$APP_DIR/docker/voice_test_server.py" &
VOICE_SERVER_PID=$!

# ─── start gateway (setup wizard + reverse proxy to goosed) ──────────────

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

        # check voice server
        if ! kill -0 "$VOICE_SERVER_PID" 2>/dev/null; then
            echo "[watchdog] voice server crashed, restarting..."
            runuser -u gooseclaw -- python3 "$APP_DIR/docker/voice_test_server.py" &
            VOICE_SERVER_PID=$!
            echo "[watchdog] voice server restarted (pid $VOICE_SERVER_PID)"
        fi

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
