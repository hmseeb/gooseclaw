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

# ─── first boot: copy template files to volume ─────────────────────────────

if [ ! -f "$DATA_DIR/.initialized" ]; then
    echo "[init] first boot detected. setting up /data..."
    mkdir -p "$IDENTITY_DIR/journal" "$CONFIG_DIR" "$DATA_DIR/sessions" "$DATA_DIR/recipes"

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
fi

if [ "$PROVIDER_CONFIGURED" = false ]; then
    echo "[provider] no provider configured yet. setup wizard will handle it."
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
    export GIT_USER_NAME="${GIT_USER_NAME:-gooseclaw-agent}"
    export GIT_USER_EMAIL="${GIT_USER_EMAIL:-gooseclaw@users.noreply.github.com}"
    export PERSIST_BRANCH="${PERSIST_BRANCH:-main}"

    git config --global user.name "$GIT_USER_NAME"
    git config --global user.email "$GIT_USER_EMAIL"
else
    echo "[persist] git persistence disabled (no GITHUB_PAT/GITHUB_REPO)"
    echo "[persist] state persists on Railway volume only"
fi

# ─── start gateway (setup wizard + reverse proxy to goose web) ────────────

echo "[gateway] starting gateway on port ${PORT:-8080}..."
python3 "$APP_DIR/docker/gateway.py" &
GATEWAY_PY_PID=$!

# ─── start telegram gateway ────────────────────────────────────────────────

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "[telegram] starting telegram gateway..."

    goose gateway start --bot-token "$TELEGRAM_BOT_TOKEN" telegram &
    TELEGRAM_PID=$!

    sleep 8

    echo ""
    echo "========================================"
    echo "  TELEGRAM PAIRING"
    echo "========================================"
    echo ""
    echo "  your bot is running! to pair:"
    echo ""
    echo "  1. open Telegram, find your bot"
    echo "  2. send /start or any message"
    echo "  3. it will ask for a pairing code"
    echo ""

    PAIR_OUTPUT=$(goose gateway pair telegram 2>&1) || true
    if echo "$PAIR_OUTPUT" | grep -qE '[A-Z0-9]{6}'; then
        PAIR_CODE=$(echo "$PAIR_OUTPUT" | grep -oE '[A-Z0-9]{6}' | head -1)
        echo "  >>> PAIRING CODE: $PAIR_CODE <<<"
        echo ""
        echo "  send this code to your bot."
        echo "  pairing is one-time. persists across restarts."
    else
        echo "  could not auto-generate code."
        echo "  check logs after gateway is fully started."
        echo "  output: $PAIR_OUTPUT"
    fi
    echo ""
    echo "========================================"
    echo ""
else
    echo "[telegram] TELEGRAM_BOT_TOKEN not set, skipping telegram"
    TELEGRAM_PID=""
fi

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

# ─── SIGTERM handler (graceful shutdown) ────────────────────────────────────

shutdown() {
    echo "[shutdown] SIGTERM received"

    if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
        echo "[shutdown] persisting final state..."
        "$APP_DIR/scripts/persist.sh" 2>&1 || true
    fi

    kill "$GATEWAY_PY_PID" 2>/dev/null || true
    [ -n "${TELEGRAM_PID:-}" ] && kill "$TELEGRAM_PID" 2>/dev/null || true
    [ -n "${PERSIST_PID:-}" ] && kill "$PERSIST_PID" 2>/dev/null || true

    echo "[shutdown] done"
    exit 0
}
trap shutdown SIGTERM SIGINT

echo "[gooseclaw] agent is live!"
echo ""

# wait for any process to exit
wait -n "$GATEWAY_PY_PID" ${TELEGRAM_PID:+"$TELEGRAM_PID"}
echo "[gooseclaw] process exited unexpectedly, shutting down..."
exit 1
