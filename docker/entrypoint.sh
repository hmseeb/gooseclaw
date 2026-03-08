#!/bin/bash
set -e

echo "========================================"
echo "  nix — personal AI agent on railway"
echo "========================================"
echo ""

APP_DIR="/home/nix/app"
DATA_DIR="/data"
CONFIG_DIR="/data/config"
IDENTITY_DIR="/data/identity"

# ─── first boot: copy template files to volume ─────────────────────────────

if [ ! -f "$DATA_DIR/.initialized" ]; then
    echo "[init] first boot detected. setting up /data..."
    mkdir -p "$IDENTITY_DIR/journal" "$CONFIG_DIR" "$DATA_DIR/recipes" "$DATA_DIR/sessions"

    # copy template identity files
    cp -r "$APP_DIR/identity/"* "$IDENTITY_DIR/"
    echo "[init] identity files copied to $IDENTITY_DIR/"

    # copy recipes
    cp -r "$APP_DIR/recipes/"* "$DATA_DIR/recipes/" 2>/dev/null || true
    echo "[init] recipes copied to $DATA_DIR/recipes/"

    # copy persistent instructions (MOIM file)
    cp "$APP_DIR/identity/persistent-instructions.md" "$IDENTITY_DIR/persistent-instructions.md"

    touch "$DATA_DIR/.initialized"
    echo "[init] first boot setup complete"
else
    echo "[init] existing data found at /data. using it."
fi

# ─── goose config ───────────────────────────────────────────────────────────

# symlink goose config directory
mkdir -p /home/nix/.config
rm -rf /home/nix/.config/goose
ln -sf "$CONFIG_DIR" /home/nix/.config/goose

# symlink sessions db to volume for persistence
mkdir -p /home/nix/.local/share/goose/sessions
if [ -f "$DATA_DIR/sessions/sessions.db" ]; then
    ln -sf "$DATA_DIR/sessions/sessions.db" /home/nix/.local/share/goose/sessions/sessions.db
fi

# generate config.yaml
cat > "$CONFIG_DIR/config.yaml" << YAML
keyring: false
GOOSE_MODE: auto
GOOSE_CONTEXT_STRATEGY: summarize
GOOSE_MAX_TURNS: 50
GOOSE_DISABLE_SESSION_NAMING: true
YAML

# ─── provider setup ────────────────────────────────────────────────────────

if [ -n "${CLAUDE_SETUP_TOKEN:-}" ]; then
    echo "[provider] tier 1: claude subscription (setup-token)"

    # install claude CLI
    if command -v claude &>/dev/null; then
        echo "[provider] claude CLI already installed"
    else
        echo "[provider] installing claude CLI..."
        curl -fsSL https://claude.ai/install.sh | bash 2>/dev/null || {
            echo "[provider] native install failed, trying npm..."
            npm install -g @anthropic-ai/claude-code 2>/dev/null || {
                echo "[provider] ERROR: could not install claude CLI"
                exit 1
            }
        }
    fi

    # authenticate with setup token
    export CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_SETUP_TOKEN"

    # create minimal claude config to skip onboarding
    mkdir -p /home/nix/.claude
    cat > /home/nix/.claude.json << CJSON
{
    "hasCompletedOnboarding": true
}
CJSON

    echo "GOOSE_PROVIDER: claude-code" >> "$CONFIG_DIR/config.yaml"

elif [ -n "${GOOSE_API_KEY:-}" ]; then
    PROVIDER="${GOOSE_PROVIDER:-anthropic}"
    echo "[provider] tier 2: API key ($PROVIDER)"
    echo "GOOSE_PROVIDER: $PROVIDER" >> "$CONFIG_DIR/config.yaml"

    # set the appropriate env var for the provider
    case "$PROVIDER" in
        anthropic)  export ANTHROPIC_API_KEY="$GOOSE_API_KEY" ;;
        openai)     export OPENAI_API_KEY="$GOOSE_API_KEY" ;;
        google)     export GOOGLE_API_KEY="$GOOSE_API_KEY" ;;
        groq)       export GROQ_API_KEY="$GOOSE_API_KEY" ;;
        openrouter) export OPENROUTER_API_KEY="$GOOSE_API_KEY" ;;
        *)          export GOOSE_PROVIDER__API_KEY="$GOOSE_API_KEY" ;;
    esac

elif [ -n "${CUSTOM_PROVIDER_URL:-}" ]; then
    echo "[provider] tier 3: custom provider ($CUSTOM_PROVIDER_URL)"
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

else
    echo ""
    echo "ERROR: no LLM provider configured."
    echo ""
    echo "set ONE of these in Railway environment variables:"
    echo "  CLAUDE_SETUP_TOKEN  — claude subscription (run 'claude setup-token' locally)"
    echo "  GOOSE_API_KEY       — API key (set GOOSE_PROVIDER too: anthropic, openai, google, etc.)"
    echo "  CUSTOM_PROVIDER_URL — any OpenAI-compatible endpoint"
    echo ""
    exit 1
fi

# model override
if [ -n "${GOOSE_MODEL:-}" ]; then
    echo "GOOSE_MODEL: $GOOSE_MODEL" >> "$CONFIG_DIR/config.yaml"
fi

# ─── MOIM (persistent instructions injected every turn) ────────────────────

export GOOSE_MOIM_MESSAGE_FILE="$IDENTITY_DIR/persistent-instructions.md"

# ─── git persistence setup ─────────────────────────────────────────────────

if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
    echo "[persist] git persistence enabled (${GITHUB_REPO})"
    export GIT_USER_NAME="${GIT_USER_NAME:-nix-agent}"
    export GIT_USER_EMAIL="${GIT_USER_EMAIL:-nix-agent@users.noreply.github.com}"
    export PERSIST_BRANCH="${PERSIST_BRANCH:-main}"

    git config --global user.name "$GIT_USER_NAME"
    git config --global user.email "$GIT_USER_EMAIL"
else
    echo "[persist] git persistence disabled (no GITHUB_PAT/GITHUB_REPO set)"
    echo "[persist] identity state will persist on Railway volume only"
fi

# ─── start health check server ─────────────────────────────────────────────

echo "[health] starting on port ${PORT:-8080}..."
python3 "$APP_DIR/docker/health.py" &
HEALTH_PID=$!

# ─── start telegram gateway ────────────────────────────────────────────────

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "[gateway] starting telegram gateway..."

    # construct platform config JSON
    PLATFORM_CONFIG=$(jq -n --arg token "$TELEGRAM_BOT_TOKEN" '{"bot_token": $token}')

    # start gateway in background
    goose gateway start telegram --config "$PLATFORM_CONFIG" &
    GATEWAY_PID=$!

    # wait for gateway to initialize
    sleep 5

    # generate pairing code for user
    echo ""
    echo "========================================"
    echo "  TELEGRAM PAIRING"
    echo "========================================"
    echo ""
    echo "  your bot is running! to connect:"
    echo ""
    echo "  1. open telegram"
    echo "  2. find your bot (the one you created with @BotFather)"
    echo "  3. send /start"
    echo "  4. the bot will ask for a pairing code"
    echo ""

    # try to generate a code via CLI
    PAIR_OUTPUT=$(goose gateway pair telegram 2>&1) || true
    if echo "$PAIR_OUTPUT" | grep -qE '[A-Z0-9]{6}'; then
        PAIR_CODE=$(echo "$PAIR_OUTPUT" | grep -oE '[A-Z0-9]{6}' | head -1)
        echo "  YOUR PAIRING CODE: $PAIR_CODE"
        echo ""
        echo "  send this code to your bot on telegram."
        echo "  pairing is one-time. it persists across restarts."
    else
        echo "  run 'goose gateway pair telegram' to generate a code"
        echo "  (check Railway logs after gateway is fully started)"
    fi
    echo ""
    echo "========================================"
    echo ""
else
    echo "[gateway] TELEGRAM_BOT_TOKEN not set, skipping telegram"
    GATEWAY_PID=""
fi

# ─── start persist loop (if git enabled) ───────────────────────────────────

if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
    PERSIST_INTERVAL="${PERSIST_INTERVAL:-300}"
    echo "[persist] starting persist loop (every ${PERSIST_INTERVAL}s)"

    while true; do
        sleep "$PERSIST_INTERVAL"
        "$APP_DIR/scripts/persist.sh" 2>&1 | while read -r line; do
            echo "[persist] $line"
        done
    done &
    PERSIST_PID=$!
fi

# ─── SIGTERM handler (graceful shutdown) ────────────────────────────────────

shutdown() {
    echo "[shutdown] SIGTERM received"

    # persist final state
    if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
        echo "[shutdown] persisting final state..."
        "$APP_DIR/scripts/persist.sh" 2>&1 || true
    fi

    # stop processes
    kill "$HEALTH_PID" 2>/dev/null || true
    [ -n "${GATEWAY_PID:-}" ] && kill "$GATEWAY_PID" 2>/dev/null || true
    [ -n "${PERSIST_PID:-}" ] && kill "$PERSIST_PID" 2>/dev/null || true

    echo "[shutdown] done"
    exit 0
}
trap shutdown SIGTERM SIGINT

echo "[nix] agent is live!"
echo ""

# wait for any process to exit
wait -n "$HEALTH_PID" ${GATEWAY_PID:+"$GATEWAY_PID"}
echo "[nix] process exited unexpectedly. shutting down..."
exit 1
