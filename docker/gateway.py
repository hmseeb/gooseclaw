#!/usr/bin/env python3
"""
gooseclaw gateway — setup wizard + notification bus + reverse proxy to goose web.

Runs on $PORT. Serves /setup directly, proxies everything else to goose web
on an internal port. Manages the goose web subprocess lifecycle.

Architecture:
  - notification bus: channel-agnostic delivery. telegram/slack/whatsapp register
    handlers via register_notification_handler(). scheduler, reminders, and session
    watcher all deliver through notify_all() without knowing which channels are active.
  - cron scheduler: reads goose schedule.json, fires jobs in isolated goose web
    sessions, delivers output via notify_all(). replaces goose's built-in scheduler
    which only runs inside `goose gateway` (not `goose web`).
  - reminder engine: lightweight timers (no AI). 10s tick, direct delivery.
  - session watcher: polls goose web for scheduled session output, forwards via notify.

API:
  GET  /api/health           -> health check
  GET  /api/setup/config     -> current provider config (masked)
  GET  /api/setup/status     -> goose web startup state (idle/starting/ready/error)
  POST /api/setup/validate   -> validate provider credentials
  POST /api/setup/save       -> save provider config and restart
  POST /api/notify           -> send message to all registered notification channels
  GET  /api/notify/status    -> check if notification delivery is available
  GET  /api/telegram/status  -> telegram gateway status, paired users, pairing code
  POST /api/telegram/pair    -> generate a new telegram pairing code
  POST /api/auth/recover     -> reset auth token using GOOSECLAW_RECOVERY_SECRET
  GET  /api/reminders         -> list active reminders
  POST /api/reminders         -> create a new reminder (delay_seconds or fire_at)
  DELETE /api/reminders/<id>  -> cancel a reminder
  GET  /api/channels          -> list loaded channel plugins
  POST /api/channels/reload   -> hot-reload channel plugins from /data/channels/
"""

import base64
import collections
import glob
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

# ── rate limiting ────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple per-IP sliding window rate limiter using stdlib only."""

    def __init__(self, max_requests=60, window_seconds=60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests = collections.defaultdict(list)  # ip -> [timestamps]
        self._lock = threading.Lock()

    def is_allowed(self, ip):
        """Check if request from IP is allowed. Cleans old entries."""
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            timestamps = self._requests[ip]
            # remove expired entries
            self._requests[ip] = [t for t in timestamps if t > cutoff]
            if len(self._requests[ip]) >= self.max_requests:
                return False
            self._requests[ip].append(now)
            return True

    def cleanup(self):
        """Periodic cleanup of stale IPs (call from a timer)."""
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            stale = [ip for ip, ts in self._requests.items() if not ts or ts[-1] < cutoff]
            for ip in stale:
                del self._requests[ip]


# module-level rate limiter instances
api_limiter = RateLimiter(max_requests=60, window_seconds=60)    # 1 req/sec sustained
auth_limiter = RateLimiter(max_requests=5, window_seconds=60)    # auth-sensitive endpoints
notify_limiter = RateLimiter(max_requests=10, window_seconds=60)  # notify endpoint


# ── security headers ─────────────────────────────────────────────────────────

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# ── config ──────────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_DIR = os.path.join(DATA_DIR, "config")
SETUP_FILE = os.path.join(CONFIG_DIR, "setup.json")
APP_DIR = os.environ.get("APP_DIR", "/app")
SETUP_HTML = os.path.join(APP_DIR, "docker", "setup.html")
PORT = int(os.environ.get("PORT", 8080))
GOOSE_WEB_PORT = 3001
PROXY_TIMEOUT = int(os.environ.get("GOOSECLAW_PROXY_TIMEOUT", "60"))

goose_process = None
goose_lock = threading.Lock()
telegram_process = None  # kept for backwards compat; no longer a subprocess
telegram_lock = threading.Lock()
telegram_pair_code = None
telegram_pair_lock = threading.Lock()
_telegram_running = False  # True while the Python polling thread is active
_telegram_sessions_file = os.path.join(DATA_DIR, "telegram_sessions.json")
_telegram_sessions = {}  # chat_id (str) -> session_id (str)
_telegram_sessions_lock = threading.Lock()

# ── notification bus (channel-agnostic delivery) ─────────────────────────────
#
# Any channel (telegram, slack, whatsapp, etc.) registers a handler via
# register_notification_handler(). All delivery goes through notify_all().
# The scheduler, reminder engine, and session watcher don't know or care
# which channels are active.
#
# Handler signature: handler_fn(text) -> {"sent": bool, "error": str}

_notification_handlers = []    # [{"name": str, "handler": callable}, ...]
_notification_handlers_lock = threading.Lock()


def register_notification_handler(name, handler_fn):
    """Register a delivery channel. handler_fn(text) -> {"sent": bool, "error": str}."""
    with _notification_handlers_lock:
        # avoid double-registration
        for h in _notification_handlers:
            if h["name"] == name:
                h["handler"] = handler_fn
                print(f"[notify] updated handler: {name}")
                return
        _notification_handlers.append({"name": name, "handler": handler_fn})
    print(f"[notify] registered handler: {name}")


# ── channel plugin system state ───────────────────────────────────────────────

CHANNELS_DIR = os.path.join(DATA_DIR, "channels")
_loaded_channels = {}       # name -> {"module": mod, "channel": CHANNEL dict, "creds": dict}
_channel_threads = {}       # name -> Thread
_channel_stop_events = {}   # name -> threading.Event
_channels_lock = threading.Lock()

# ── session watcher state (auto-forward scheduled output to telegram) ───────
_session_watcher_running = False
_session_watcher_state_file = os.path.join(DATA_DIR, "session_watcher_state.json")
_session_watcher_state = {}   # session_id -> {"forwarded_count": int, "schedule_id": str}
_session_watcher_lock = threading.Lock()

# ── reminder engine state ──────────────────────────────────────────────────
_reminders_file = os.path.join(DATA_DIR, "reminders.json")
_reminders = []        # list of reminder dicts
_reminders_lock = threading.Lock()
_reminder_engine_running = False

# ── goose web startup state ─────────────────────────────────────────────────
goose_startup_state = {
    "state": "idle",        # idle | starting | ready | error
    "message": "",          # human-readable status message
    "error": "",            # stderr output when state=error
    "timestamp": 0,         # time.time() of last state change
}
_startup_state_lock = threading.Lock()
_stderr_buffer = collections.deque(maxlen=50)  # last 50 lines of stderr
_stderr_lock = threading.Lock()

# internal token used for gateway -> goose web communication (never exposed to users)
_INTERNAL_GOOSE_TOKEN = None


def _set_startup_state(state, message="", error=""):
    """Update goose web startup state under lock."""
    with _startup_state_lock:
        goose_startup_state["state"] = state
        goose_startup_state["message"] = message
        goose_startup_state["error"] = error
        goose_startup_state["timestamp"] = time.time()


def _append_stderr(line):
    """Append a line to the stderr ring buffer under lock."""
    with _stderr_lock:
        _stderr_buffer.append(line)


def _get_recent_stderr(n=20):
    """Return the last n lines from the stderr buffer as a single string."""
    with _stderr_lock:
        lines = list(_stderr_buffer)[-n:]
    return "\n".join(lines)


def _stderr_reader(proc):
    """Read proc.stderr line by line, log with prefix, and buffer lines."""
    try:
        for raw_line in proc.stderr:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            print(f"[goose-web] {line}", file=sys.stderr)
            _append_stderr(line)
    except Exception:
        pass  # process exited or pipe closed


# ── PID file management ──────────────────────────────────────────────────────

def _write_pid(name, pid):
    """Write a PID file for a managed subprocess."""
    try:
        with open(os.path.join(CONFIG_DIR, f"{name}.pid"), "w") as f:
            f.write(str(pid))
    except Exception:
        pass


def _remove_pid(name):
    """Remove a PID file for a managed subprocess."""
    try:
        os.unlink(os.path.join(CONFIG_DIR, f"{name}.pid"))
    except OSError:
        pass


def _check_stale_pid(name):
    """Check if a PID file exists for a dead process and clean it up."""
    pid_file = os.path.join(CONFIG_DIR, f"{name}.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # check if process exists (raises if not)
        except (ProcessLookupError, ValueError):
            _remove_pid(name)
        except PermissionError:
            pass  # process exists but we can't signal it


# ── auth token hashing ───────────────────────────────────────────────────────

def hash_token(token):
    """Hash an auth token using SHA-256 for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(provided, stored_hash):
    """Verify a provided token against a stored SHA-256 hash."""
    return hashlib.sha256(provided.encode()).hexdigest() == stored_hash


# ── provider registry ────────────────────────────────────────────────────────

env_map = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "google": ["GOOGLE_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "xai": ["XAI_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "together": ["TOGETHER_API_KEY"],
    "cerebras": ["CEREBRAS_API_KEY"],
    "perplexity": ["PERPLEXITY_API_KEY"],
    "avian": ["AVIAN_API_KEY"],
    "litellm": ["LITELLM_API_KEY", "LITELLM_HOST"],
    "venice": ["VENICE_API_KEY"],
    "ovhcloud": ["OVH_AI_ENDPOINTS_ACCESS_TOKEN"],
    "claude-code": ["CLAUDE_CODE_OAUTH_TOKEN"],
    "github-copilot": ["GITHUB_TOKEN"],
    "ollama": ["OLLAMA_HOST"],
    "lm-studio": [],
    "docker-model-runner": [],
    "ramalama": [],
    "azure-openai": ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"],
    "custom": [],
}

default_models = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o",
    "google": "gemini-2.0-flash",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "anthropic/claude-3.5-sonnet",
    "mistral": "mistral-large-latest",
    "xai": "grok-2-1212",
    "deepseek": "deepseek-chat",
    "together": "meta-llama/Llama-3-70b-chat-hf",
    "cerebras": "llama3.1-70b",
    "perplexity": "llama-3.1-sonar-large-128k-online",
    "avian": "gpt-4o",
    "litellm": "gpt-4o",
    "venice": "llama-3.3-70b",
    "ovhcloud": "Meta-Llama-3.1-70B-Instruct",
    "claude-code": "claude-sonnet-4-5",
    "github-copilot": "gpt-4o",
    "ollama": "llama3.2",
    "lm-studio": "local-model",
    "docker-model-runner": "ai/llama3.2",
    "ramalama": "llama3.2",
    "azure-openai": "gpt-4o",
    "custom": "custom-model",
}

provider_names = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google AI (Gemini)",
    "groq": "Groq",
    "openrouter": "OpenRouter",
    "mistral": "Mistral AI",
    "xai": "xAI (Grok)",
    "deepseek": "DeepSeek",
    "together": "Together AI",
    "cerebras": "Cerebras",
    "perplexity": "Perplexity AI",
    "avian": "Avian",
    "litellm": "LiteLLM",
    "venice": "Venice AI",
    "ovhcloud": "OVHcloud AI",
    "claude-code": "Claude Code",
    "github-copilot": "GitHub Copilot",
    "ollama": "Ollama",
    "lm-studio": "LM Studio",
    "docker-model-runner": "Docker Model Runner",
    "ramalama": "Ramalama",
    "azure-openai": "Azure OpenAI",
    "custom": "Custom Provider",
}

key_urls = {
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
    "google": "https://aistudio.google.com/app/apikey",
    "groq": "https://console.groq.com/keys",
    "openrouter": "https://openrouter.ai/settings/keys",
    "mistral": "https://console.mistral.ai/api-keys",
    "xai": "https://console.x.ai/",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "together": "https://api.together.xyz/settings/api-keys",
    "cerebras": "https://cloud.cerebras.ai/platform",
    "perplexity": "https://www.perplexity.ai/settings/api",
    "avian": "https://avian.io/",
    "litellm": "https://docs.litellm.ai/",
    "venice": "https://venice.ai/settings/api",
    "ovhcloud": "https://endpoints.ai.cloud.ovh.net/",
}


# ── telegram notification ────────────────────────────────────────────────────

GOOSE_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yaml")


def get_bot_token():
    """Get telegram bot token from env, setup.json, or goose config."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        return token
    setup = None
    if os.path.exists(SETUP_FILE):
        with open(SETUP_FILE) as f:
            setup = json.load(f)
    if setup and setup.get("telegram_bot_token"):
        return setup["telegram_bot_token"]
    return ""


def get_paired_chat_ids():
    """Read paired telegram chat IDs from goose config.yaml."""
    chat_ids = []
    if not os.path.exists(GOOSE_CONFIG_PATH):
        return chat_ids
    try:
        with open(GOOSE_CONFIG_PATH) as f:
            content = f.read()
        # lightweight yaml parse: find gateway_pairings entries with platform: telegram
        # goose config uses simple yaml, so we can parse with basic string matching
        in_pairings = False
        current_entry = {}
        for line in content.split("\n"):
            stripped = line.strip()
            if line.startswith("gateway_pairings:"):
                in_pairings = True
                continue
            if in_pairings:
                if line and not line[0].isspace() and not stripped.startswith("-"):
                    break  # left the pairings block
                if stripped.startswith("- platform:"):
                    if current_entry.get("platform") == "telegram" and current_entry.get("user_id"):
                        chat_ids.append(current_entry["user_id"])
                    current_entry = {"platform": stripped.split(":", 1)[1].strip()}
                elif stripped.startswith("user_id:"):
                    val = stripped.split(":", 1)[1].strip().strip("'\"")
                    current_entry["user_id"] = val
                elif stripped.startswith("state:") and "paired" in stripped:
                    current_entry["paired"] = True
        # catch last entry
        if current_entry.get("platform") == "telegram" and current_entry.get("user_id"):
            chat_ids.append(current_entry["user_id"])
    except Exception as e:
        print(f"[gateway] warn: could not read pairings: {e}")
    return chat_ids


def _markdown_to_telegram_html(text):
    """Convert standard markdown to Telegram-compatible HTML.

    Telegram HTML supports: <b>, <i>, <code>, <pre>, <a>, <s>, <blockquote>, <u>.
    This is far more reliable than Telegram's legacy Markdown or MarkdownV2 modes.
    """
    # -- Step 1: extract code blocks so they don't get mangled --
    code_blocks = []
    def _save_code_block(m):
        code = m.group(2)
        code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        idx = len(code_blocks)
        code_blocks.append(f"<pre>{code}</pre>")
        return f"\x00CB{idx}\x00"
    text = re.sub(r'```(\w*)\n(.*?)```', _save_code_block, text, flags=re.DOTALL)
    # also handle ``` without newline after lang
    text = re.sub(r'```(\w*)(.*?)```', _save_code_block, text, flags=re.DOTALL)

    # -- Step 2: extract inline code --
    inline_codes = []
    def _save_inline(m):
        code = m.group(1)
        code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        idx = len(inline_codes)
        inline_codes.append(f"<code>{code}</code>")
        return f"\x00IC{idx}\x00"
    text = re.sub(r'`([^`\n]+)`', _save_inline, text)

    # -- Step 3: escape HTML entities in remaining text --
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # -- Step 4: headers → bold --
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # -- Step 5: bold (**text** or __text__) --
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # -- Step 6: italic (*text* or _text_) — avoid matching mid-word underscores --
    text = re.sub(r'(?<!\w)\*([^*\n]+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_\n]+?)_(?!\w)', r'<i>\1</i>', text)

    # -- Step 7: strikethrough ~~text~~ --
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # -- Step 8: links [text](url) --
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # -- Step 9: blockquotes (> line) --
    def _convert_blockquotes(txt):
        lines = txt.split('\n')
        out = []
        bq_buf = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith('&gt; '):  # was '> ' before HTML escape
                bq_buf.append(stripped[5:])
            elif stripped == '&gt;':
                bq_buf.append('')
            else:
                if bq_buf:
                    out.append('<blockquote>' + '\n'.join(bq_buf) + '</blockquote>')
                    bq_buf = []
                out.append(line)
        if bq_buf:
            out.append('<blockquote>' + '\n'.join(bq_buf) + '</blockquote>')
        return '\n'.join(out)
    text = _convert_blockquotes(text)

    # -- Step 10: tables → preformatted block --
    lines = text.split('\n')
    result = []
    table_buf = []
    for line in lines:
        stripped = line.strip()
        if re.match(r'^\|.+\|$', stripped):
            # skip separator rows  |---|---|
            if re.match(r'^\|[\s\-:|\+]+\|$', stripped):
                continue
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            table_buf.append('  '.join(cells))
        else:
            if table_buf:
                result.append('<pre>' + '\n'.join(table_buf) + '</pre>')
                table_buf = []
            result.append(line)
    if table_buf:
        result.append('<pre>' + '\n'.join(table_buf) + '</pre>')
    text = '\n'.join(result)

    # -- Step 11: horizontal rules --
    text = re.sub(r'^-{3,}$', '─' * 20, text, flags=re.MULTILINE)

    # -- Step 12: restore protected blocks --
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", code)

    return text


def _strip_html(text):
    """Strip HTML tags for plain-text fallback."""
    return re.sub(r'<[^>]+>', '', text)


def send_telegram_message(bot_token, chat_id, text):
    """Send a message via telegram bot API. Returns (ok, error).

    Converts markdown to Telegram HTML first. Falls back to plain text if
    HTML parse fails.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    html_text = _markdown_to_telegram_html(text)

    # split long messages (telegram limit: 4096 chars)
    def _chunk(t, limit=4000):
        if len(t) <= limit:
            return [t]
        chunks = []
        current = ""
        for line in t.split("\n"):
            if len(current) + len(line) + 1 > limit:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        return chunks

    html_chunks = _chunk(html_text)
    plain_chunks = _chunk(text)  # original text for fallback

    for i, chunk in enumerate(html_chunks):
        try:
            payload = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(url, data=payload)
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    raise ValueError("telegram returned ok=false")
        except Exception:
            # HTML failed — fall back to plain text for this chunk
            try:
                fallback = plain_chunks[i] if i < len(plain_chunks) else _strip_html(chunk)
                payload = urllib.parse.urlencode({
                    "chat_id": chat_id,
                    "text": fallback,
                    "disable_web_page_preview": "true",
                }).encode()
                req = urllib.request.Request(url, data=payload)
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                return False, str(e)
    return True, ""


def notify_all(text):
    """Send a message to all registered notification channels.

    Channel-agnostic: telegram, slack, whatsapp, etc. each register via
    register_notification_handler(). This function just calls all of them.
    """
    with _notification_handlers_lock:
        handlers = list(_notification_handlers)
    if not handlers:
        return {"sent": False, "error": "no notification channels registered"}
    results = []
    for h in handlers:
        try:
            result = h["handler"](text)
            results.append({"channel": h["name"], **result})
        except Exception as e:
            results.append({"channel": h["name"], "sent": False, "error": str(e)})
    return {"sent": any(r.get("sent") for r in results), "channels": results}


def _telegram_notify_handler(text):
    """Telegram notification handler — registered with the notification bus."""
    token = get_bot_token()
    if not token:
        return {"sent": False, "error": "no bot token configured"}
    chat_ids = get_paired_chat_ids()
    if not chat_ids:
        return {"sent": False, "error": "no paired telegram users"}
    ok_all = True
    for cid in chat_ids:
        ok, err = send_telegram_message(token, cid, text)
        if not ok:
            ok_all = False
    return {"sent": ok_all, "error": "" if ok_all else "some deliveries failed"}


# ── setup config management ─────────────────────────────────────────────────

def validate_setup_config(config):
    """Validate setup config schema. Returns (valid, errors) tuple."""
    errors = []
    if not isinstance(config, dict):
        return False, ["config must be a JSON object"]

    provider = config.get("provider_type", "")
    if not provider:
        errors.append("provider_type is required")
    elif provider not in env_map:
        errors.append(f"unknown provider_type: {provider!r}")

    # provider-specific credential validation (skip local/no-key providers)
    local_providers = ("ollama", "lm-studio", "docker-model-runner", "ramalama")
    if provider in env_map and provider not in local_providers:
        if provider != "custom" and not config.get("api_key") and not config.get("claude_setup_token"):
            # check if any provider-specific env var key is provided in config
            has_cred = False
            for env_var in env_map.get(provider, []):
                if config.get(env_var.lower()):
                    has_cred = True
                    break
            if not has_cred:
                errors.append(f"api_key or provider credentials required for {provider}")

    # telegram token format check (if provided)
    tg = config.get("telegram_bot_token", "")
    if tg and ":" not in tg:
        errors.append("telegram_bot_token must be in format digits:alphanumeric")

    # timezone format check (if provided)
    tz = config.get("timezone", "")
    if tz and "/" not in tz and tz != "UTC":
        errors.append(f"timezone should be in Region/City format (got {tz!r})")

    # string field max-length guard (prevent absurdly large values)
    for field in ("api_key", "claude_setup_token", "custom_key", "custom_url", "model",
                  "lead_provider", "lead_model"):
        val = config.get(field, "")
        if isinstance(val, str) and len(val) > 2000:
            errors.append(f"{field} exceeds maximum length (2000 chars)")

    # lead/worker multi-model validation
    lead_provider = config.get("lead_provider", "")
    if lead_provider and lead_provider not in env_map:
        errors.append(f"unknown lead_provider: {lead_provider!r}")
    lead_turn_count = config.get("lead_turn_count", "")
    if lead_turn_count:
        try:
            tc = int(lead_turn_count)
            if tc < 1 or tc > 50:
                errors.append("lead_turn_count must be between 1 and 50")
        except (ValueError, TypeError):
            errors.append("lead_turn_count must be an integer")

    return len(errors) == 0, errors


def load_setup():
    if os.path.exists(SETUP_FILE):
        with open(SETUP_FILE) as f:
            return json.load(f)
    return None


def save_setup(config):
    """Atomically write config to setup.json (write tmp, then rename)."""
    import shutil
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # back up existing config before overwrite
    if os.path.exists(SETUP_FILE):
        try:
            shutil.copy2(SETUP_FILE, SETUP_FILE + ".bak")
        except Exception:
            pass  # non-fatal
    tmp_path = SETUP_FILE + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(config, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, SETUP_FILE)  # atomic on same filesystem
    except Exception:
        # clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def is_configured():
    """Check if an LLM provider is configured (env vars OR setup file)."""
    if os.environ.get("CLAUDE_SETUP_TOKEN"):
        return True
    if os.environ.get("GOOSE_API_KEY"):
        return True
    if os.environ.get("CUSTOM_PROVIDER_URL"):
        return True
    setup = load_setup()
    return setup is not None and setup.get("provider_type")


def _is_first_boot():
    """Return True when no setup has been completed AND no env-var provider is configured.

    During first boot, only setup-related endpoints should be accessible. All other
    API endpoints (notify, telegram, etc.) make no sense before configuration and must
    return 403 to prevent unauthenticated access to a partially-initialised system.
    """
    if os.environ.get("CLAUDE_SETUP_TOKEN"):
        return False
    if os.environ.get("GOOSE_API_KEY"):
        return False
    if os.environ.get("CUSTOM_PROVIDER_URL"):
        return False
    return load_setup() is None


def get_auth_token():
    """Get the active auth token. Returns (token_or_hash, is_hashed) tuple.

    - env var GOOSE_WEB_AUTH_TOKEN -> (plaintext, False)
    - setup.json web_auth_token_hash (new format) -> (hash, True)
    - setup.json web_auth_token (legacy plaintext) -> (plaintext, False)
    """
    token = os.environ.get("GOOSE_WEB_AUTH_TOKEN", "")
    if token:
        return token, False
    setup = load_setup()
    if setup:
        stored_hash = setup.get("web_auth_token_hash", "")
        if stored_hash:
            return stored_hash, True
        legacy = setup.get("web_auth_token", "")
        if legacy:
            return legacy, False
    return "", False


def _make_session_cookie(token):
    """Create an HMAC-based session cookie value from the auth token."""
    return hashlib.sha256(f"gooseclaw-session:{token}".encode()).hexdigest()


def check_auth(handler):
    """Check HTTP Basic Auth or session cookie. Returns True if authorized."""
    stored, is_hashed = get_auth_token()
    if not stored:
        return True

    # check session cookie first (avoids re-prompting Basic Auth)
    cookie_header = handler.headers.get("Cookie", "")
    if cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("gooseclaw_session="):
                cookie_val = part.split("=", 1)[1]
                # verify cookie matches current token
                expected = _make_session_cookie(stored)
                if secrets.compare_digest(cookie_val, expected):
                    return True

    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            _, provided = decoded.split(":", 1)
            if is_hashed:
                if verify_token(provided, stored):
                    handler._set_session_cookie = True
                    return True
            elif provided == stored:
                handler._set_session_cookie = True
                return True
        except Exception:
            pass
    return False


# ── provider validation ─────────────────────────────────────────────────────

def http_get(url, headers=None, timeout=10):
    """Perform a GET request with timeout. Returns (status_code, body_text)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach {url}: {e.reason}") from e
    except Exception as e:
        raise ConnectionError(f"Request failed: {e}") from e


def validate_openai_compatible(provider_id, api_key, base_url):
    """Validate an OpenAI-compatible provider via GET /v1/models."""
    name = provider_names.get(provider_id, provider_id)
    key_url = key_urls.get(provider_id, "the provider dashboard")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        status, body = http_get(f"{base_url}/v1/models", headers=headers)
        if status == 200:
            try:
                data = json.loads(body)
                count = len(data.get("data", []))
            except (json.JSONDecodeError, KeyError):
                count = 0
            return {"valid": True, "message": f"Connected to {name}. Found {count} available models."}
        elif status in (401, 403):
            return {"valid": False, "error": f"Invalid API key for {name}. Check your key at {key_url}."}
        else:
            return {"valid": False, "error": f"Unexpected response from {name} API (HTTP {status})."}
    except ConnectionError as e:
        return {"valid": False, "error": f"Cannot reach {name} API. Check your network."}


def validate_anthropic(api_key):
    """Validate Anthropic key via GET /v1/models with x-api-key header."""
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    try:
        status, _body = http_get("https://api.anthropic.com/v1/models", headers=headers)
        if status == 200:
            return {"valid": True, "message": "Connected to Anthropic. API key is valid."}
        elif status in (401, 403):
            return {"valid": False, "error": "Invalid Anthropic API key."}
        else:
            return {"valid": False, "error": f"Unexpected response from Anthropic (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": "Cannot reach Anthropic API. Check your network."}


def validate_google(api_key):
    """Validate Google AI key via generativelanguage.googleapis.com."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={urllib.parse.quote(api_key)}"
    try:
        status, body = http_get(url)
        if status == 200:
            try:
                count = len(json.loads(body).get("models", []))
            except (json.JSONDecodeError, KeyError):
                count = 0
            return {"valid": True, "message": f"Connected to Google AI (Gemini). Found {count} models."}
        elif status in (400, 401, 403):
            return {"valid": False, "error": "Invalid Google API key."}
        else:
            return {"valid": False, "error": f"Unexpected response from Google AI (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": "Cannot reach Google AI API. Check your network."}


def validate_perplexity(api_key):
    """Validate Perplexity via a minimal chat completions test."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = json.dumps({
        "model": "llama-3.1-sonar-small-128k-online",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()
    req = urllib.request.Request(
        "https://api.perplexity.ai/chat/completions",
        data=payload, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"valid": True, "message": "Connected to Perplexity AI. API key is valid."}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"valid": False, "error": "Invalid Perplexity API key."}
        if e.code == 400:
            return {"valid": True, "message": "Connected to Perplexity AI. API key appears valid."}
        return {"valid": False, "error": f"Unexpected response from Perplexity AI (HTTP {e.code})."}
    except urllib.error.URLError:
        return {"valid": False, "error": "Cannot reach Perplexity AI. Check your network."}


def validate_azure_openai(api_key, endpoint):
    """Validate Azure OpenAI with key + endpoint."""
    if not endpoint or not endpoint.startswith("https://"):
        return {"valid": False, "error": "Azure OpenAI endpoint must start with 'https://'."}
    url = f"{endpoint.rstrip('/')}/openai/models?api-version=2024-02-01"
    try:
        status, _body = http_get(url, headers={"api-key": api_key})
        if status == 200:
            return {"valid": True, "message": "Connected to Azure OpenAI. Credentials are valid."}
        elif status in (401, 403):
            return {"valid": False, "error": "Invalid Azure OpenAI API key or endpoint."}
        else:
            return {"valid": False, "error": f"Unexpected response from Azure OpenAI (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": "Cannot reach Azure OpenAI endpoint."}


def validate_litellm(api_key, host):
    """Validate LiteLLM proxy via GET /v1/models."""
    if not host:
        return {"valid": False, "error": "LiteLLM host URL is required. Set LITELLM_HOST."}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        status, _body = http_get(f"{host.rstrip('/')}/v1/models", headers=headers)
        if status == 200:
            return {"valid": True, "message": "Connected to LiteLLM proxy."}
        elif status in (401, 403):
            return {"valid": False, "error": "LiteLLM proxy returned auth error. Check LITELLM_API_KEY."}
        else:
            return {"valid": False, "error": f"Unexpected response from LiteLLM (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": f"Cannot reach LiteLLM at {host}."}


def validate_local_provider(provider_id, host=None):
    """Validate a local provider (ollama, lm-studio, docker-model-runner, ramalama)."""
    name = provider_names.get(provider_id, provider_id)
    defaults = {
        "ollama": "http://localhost:11434",
        "lm-studio": "http://localhost:1234",
        "docker-model-runner": "http://localhost:12434",
        "ramalama": "http://localhost:8080",
    }
    if not host:
        return {"valid": True, "message": f"No host URL configured. Default: {defaults.get(provider_id, 'http://localhost:8080')}"}
    check_url = f"{host.rstrip('/')}/api/tags" if provider_id == "ollama" else f"{host.rstrip('/')}/v1/models"
    try:
        status, body = http_get(check_url)
        if status == 200:
            count = 0
            try:
                data = json.loads(body)
                count = len(data.get("models" if provider_id == "ollama" else "data", []))
            except (json.JSONDecodeError, KeyError):
                pass
            return {"valid": True, "message": f"Connected to {name}. {count} models available."}
        else:
            return {"valid": False, "error": f"Cannot reach {name} at {host} (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": f"Cannot reach {host}."}


def dispatch_validation(provider, credentials):
    """Route validation to the correct handler for the given provider."""
    # Special / skip-validation providers
    if provider == "claude-code":
        return {
            "valid": True,
            "message": "Claude Code uses OAuth authentication. Run 'claude setup-token' in your local terminal to get the token. Validation must be done manually after saving.",
            "skip_validation": True,
        }
    if provider == "github-copilot":
        token = credentials.get("GITHUB_TOKEN") or credentials.get("api_key", "")
        if token:
            headers = {"Authorization": f"Bearer {token}"}
            try:
                status, _ = http_get("https://api.github.com/copilot_internal/v2/token", headers=headers)
                if status == 200:
                    return {"valid": True, "message": "GitHub Copilot token is valid."}
                elif status in (401, 403):
                    return {"valid": False, "error": "Invalid GitHub token. Check your token or Copilot subscription."}
            except ConnectionError:
                pass
        return {"valid": True, "message": "GitHub Copilot uses device flow authentication. No pre-validation needed.", "skip_validation": True}

    # Anthropic
    if provider == "anthropic":
        key = credentials.get("ANTHROPIC_API_KEY") or credentials.get("api_key", "")
        return validate_anthropic(key) if key else {"valid": False, "error": "API key is required."}

    # Google
    if provider == "google":
        key = credentials.get("GOOGLE_API_KEY") or credentials.get("api_key", "")
        return validate_google(key) if key else {"valid": False, "error": "API key is required."}

    # Perplexity
    if provider == "perplexity":
        key = credentials.get("PERPLEXITY_API_KEY") or credentials.get("api_key", "")
        return validate_perplexity(key) if key else {"valid": False, "error": "API key is required."}

    # Avian (format-only)
    if provider == "avian":
        key = credentials.get("AVIAN_API_KEY") or credentials.get("api_key", "")
        if not key:
            return {"valid": False, "error": "API key is required."}
        if key.startswith("avian-"):
            return {"valid": True, "message": "Avian API key format is valid."}
        return {"valid": False, "error": "Avian keys must start with 'avian-'."}

    # OVHcloud (length-only)
    if provider == "ovhcloud":
        key = credentials.get("OVH_AI_ENDPOINTS_ACCESS_TOKEN") or credentials.get("api_key", "")
        if not key:
            return {"valid": False, "error": "Access token is required."}
        if len(key) > 20:
            return {"valid": True, "message": "OVHcloud AI Endpoints token appears valid."}
        return {"valid": False, "error": "OVHcloud token appears too short."}

    # Azure OpenAI
    if provider == "azure-openai":
        key = credentials.get("AZURE_OPENAI_API_KEY") or credentials.get("api_key", "")
        endpoint = credentials.get("AZURE_OPENAI_ENDPOINT") or credentials.get("azure_endpoint") or credentials.get("endpoint", "")
        if not key or not endpoint:
            return {"valid": False, "error": "Both API key and endpoint are required."}
        return validate_azure_openai(key, endpoint)

    # LiteLLM
    if provider == "litellm":
        key = credentials.get("LITELLM_API_KEY") or credentials.get("api_key", "")
        host = credentials.get("LITELLM_HOST") or credentials.get("litellm_host") or credentials.get("host", "")
        return validate_litellm(key, host)

    # Local providers
    if provider in ("ollama", "lm-studio", "docker-model-runner", "ramalama"):
        host = credentials.get("OLLAMA_HOST") or credentials.get("ollama_host") or credentials.get("host") or credentials.get("url")
        return validate_local_provider(provider, host)

    # Custom provider
    if provider == "custom":
        key = credentials.get("api_key") or credentials.get("custom_key", "")
        url = credentials.get("url") or credentials.get("custom_url", "")
        if not url:
            return {"valid": False, "error": "Custom provider URL is required."}
        return validate_openai_compatible("custom", key, url.rstrip("/")) if key else {"valid": True, "message": f"Connected to {url} (no auth)."}

    # OpenAI-compatible providers
    openai_compat = {
        "openai": "https://api.openai.com",
        "groq": "https://api.groq.com/openai",
        "openrouter": "https://openrouter.ai/api",
        "mistral": "https://api.mistral.ai",
        "xai": "https://api.x.ai",
        "deepseek": "https://api.deepseek.com",
        "together": "https://api.together.xyz",
        "cerebras": "https://api.cerebras.ai",
        "venice": "https://api.venice.ai/api",
    }
    if provider in openai_compat:
        key = credentials.get(env_map[provider][0]) or credentials.get("api_key", "")
        if not key:
            return {"valid": False, "error": "API key is required."}
        return validate_openai_compatible(provider, key, openai_compat[provider])

    return {"valid": False, "error": f"Unknown provider: {provider!r}"}


# ── goose web subprocess management ─────────────────────────────────────────

def _setup_claude_cli():
    """Install claude CLI and create config if needed (for claude-code provider)."""
    home = os.environ.get("HOME", "/root")

    # ensure ~/.local/bin is in PATH
    local_bin = os.path.join(home, ".local", "bin")
    if local_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{local_bin}:{os.environ.get('PATH', '')}"

    # also check /root/.local/bin (claude may have been installed as root during entrypoint)
    root_local_bin = "/root/.local/bin"
    if root_local_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{os.environ.get('PATH', '')}:{root_local_bin}"

    # check if already installed
    if subprocess.run(["which", "claude"], capture_output=True).returncode == 0:
        print("[gateway] claude CLI already installed")
    else:
        print("[gateway] installing claude CLI...")
        is_root = os.getuid() == 0
        try:
            subprocess.run(
                ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
                check=True, timeout=120,
            )
        except Exception:
            if is_root:
                print("[gateway] native install failed, trying npm...")
                try:
                    subprocess.run(
                        ["bash", "-c", "apt-get update -qq && apt-get install -y -qq nodejs npm >/dev/null 2>&1 && npm install -g @anthropic-ai/claude-code 2>/dev/null"],
                        check=True, timeout=180,
                    )
                except Exception as e:
                    print(f"[gateway] ERROR: could not install claude CLI: {e}")
                    return
            else:
                print("[gateway] ERROR: claude CLI install failed (running as non-root, apt not available)")
                print("[gateway] claude CLI should be pre-installed by entrypoint.sh")
                return

    # create ~/.claude.json if missing
    claude_json = os.path.join(home, ".claude.json")
    if not os.path.exists(claude_json):
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(claude_json, "w") as f:
            json.dump({"hasCompletedOnboarding": True}, f)
        print("[gateway] created ~/.claude.json")


def _extract_yaml_sections(content, section_keys):
    """Extract multi-line YAML sections from config content.

    Returns a string containing all matched top-level sections (key + nested lines).
    Used to preserve extensions: and gateway_* sections when rewriting config.yaml.
    """
    lines = content.split("\n")
    buf = []
    in_section = False
    for line in lines:
        if any(line.startswith(k) for k in section_keys):
            in_section = True
            buf.append(line)
        elif in_section:
            # still inside the section if the line is indented or a YAML list item
            if line and (line[0].isspace() or line.strip().startswith("-")):
                buf.append(line)
            else:
                in_section = False
                # check if the new line starts another section we want
                if any(line.startswith(k) for k in section_keys):
                    in_section = True
                    buf.append(line)
    return "\n".join(buf) + "\n" if buf else ""


def apply_config(config):
    """Write goose config.yaml and set env vars from setup config."""
    provider_type = config.get("provider_type", "")
    api_key = config.get("api_key", "")
    model = config.get("model", "")
    tz = config.get("timezone", "UTC")

    # set timezone
    os.environ["TZ"] = tz

    config_path = os.path.join(CONFIG_DIR, "config.yaml")

    # ── preserve existing extensions and gateway state ──
    # goose re-reads config.yaml from disk on every API call. If we strip the
    # extensions: section, the gateway detects "extensions changed" on every
    # telegram message, evicts the agent, and the session loses continuity.
    # Similarly, gateway_pairings must survive reconfiguration.
    preserved = ""
    try:
        with open(config_path) as f:
            old_content = f.read()
        preserved = _extract_yaml_sections(old_content, [
            "extensions:",
            "gateway_pairings:",
            "gateway_configs:",
            "gateway_pending_codes:",
        ])
    except FileNotFoundError:
        pass

    # base config
    lines = [
        "keyring: false",
        "GOOSE_MODE: auto",
        "GOOSE_CONTEXT_STRATEGY: summarize",
        "GOOSE_MAX_TURNS: 50",
        "GOOSE_DISABLE_SESSION_NAMING: true",
    ]

    if provider_type == "claude-code":
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = config.get("claude_setup_token", "")
        lines.append("GOOSE_PROVIDER: claude-code")
        # ensure claude CLI is installed and configured
        _setup_claude_cli()
    elif provider_type == "custom":
        url = config.get("custom_url", "")
        custom_model = config.get("custom_model", "gpt-4")
        custom_key = config.get("custom_key", "")
        # write custom provider json
        cp_dir = os.path.join(CONFIG_DIR, "custom_providers")
        os.makedirs(cp_dir, exist_ok=True)
        with open(os.path.join(cp_dir, "custom.json"), "w") as f:
            json.dump({
                "name": "custom",
                "provider_type": "openai",
                "host": url,
                "model": custom_model,
                "api_key": custom_key,
            }, f)
        lines.append("GOOSE_PROVIDER: custom")
    elif provider_type in env_map:
        # map env var names to their setup.json field names for non-standard providers
        field_map = {
            'AZURE_OPENAI_API_KEY': 'azure_key',
            'AZURE_OPENAI_ENDPOINT': 'azure_endpoint',
            'LITELLM_HOST': 'litellm_host',
            'OLLAMA_HOST': 'ollama_host',
            'GITHUB_TOKEN': 'api_key',
        }
        # set env vars for the provider from the module-level registry
        for env_var in env_map.get(provider_type, []):
            mapped_field = field_map.get(env_var, env_var.lower())
            val = config.get(mapped_field, "") or api_key
            if val:
                os.environ[env_var] = val
        lines.append(f"GOOSE_PROVIDER: {provider_type}")

    # default models per provider if none specified (from module-level registry)
    if not model:
        model = default_models.get(provider_type, "")

    if model:
        lines.append(f"GOOSE_MODEL: {model}")

    # lead/worker multi-model settings
    lead_provider = config.get("lead_provider", "")
    lead_model = config.get("lead_model", "")
    lead_turn_count = config.get("lead_turn_count", "")
    if lead_provider:
        lines.append(f"GOOSE_LEAD_PROVIDER: {lead_provider}")
        if lead_model:
            lines.append(f"GOOSE_LEAD_MODEL: {lead_model}")
        if lead_turn_count:
            lines.append(f"GOOSE_LEAD_TURN_COUNT: {lead_turn_count}")

    # write base config + preserved sections atomically
    content = "\n".join(lines) + "\n"
    if preserved:
        content += preserved
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, config_path)

    # telegram — set env var AND start gateway if not already running
    tg_token = config.get("telegram_bot_token", "")
    if tg_token:
        os.environ["TELEGRAM_BOT_TOKEN"] = tg_token
        start_telegram_gateway(tg_token)


def _is_goose_gateway_running():
    """Check if the Python telegram polling thread is running."""
    return _telegram_running, []


# ── telegram session persistence ────────────────────────────────────────────

def _load_telegram_sessions():
    """Load telegram session mapping from disk."""
    global _telegram_sessions
    try:
        if os.path.exists(_telegram_sessions_file):
            with open(_telegram_sessions_file) as f:
                data = json.load(f)
            if isinstance(data, dict):
                with _telegram_sessions_lock:
                    _telegram_sessions = data
                print(f"[telegram] loaded {len(data)} session(s) from {_telegram_sessions_file}")
    except Exception as e:
        print(f"[telegram] warn: could not load sessions: {e}")


def _save_telegram_sessions():
    """Persist telegram session mapping to disk."""
    with _telegram_sessions_lock:
        data = dict(_telegram_sessions)
    try:
        os.makedirs(os.path.dirname(_telegram_sessions_file), exist_ok=True)
        tmp = _telegram_sessions_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _telegram_sessions_file)
    except Exception as e:
        print(f"[telegram] warn: could not save sessions: {e}")


# ── reminder engine ──────────────────────────────────────────────────────────
#
# Lightweight timer system that bypasses goose's scheduler entirely.
# Background thread checks every 10s, fires notify_all() when due.
# Supports one-shot and recurring reminders. Persists to /data/reminders.json.
#
# Reminder dict shape:
#   {
#     "id": str (uuid4),
#     "text": str,
#     "fire_at": float (unix timestamp),
#     "created_at": float,
#     "recurring_seconds": int or None,
#     "fired": bool,
#   }

def _load_reminders():
    """Load reminders from disk."""
    global _reminders
    try:
        if os.path.exists(_reminders_file):
            with open(_reminders_file) as f:
                data = json.load(f)
            if isinstance(data, list):
                with _reminders_lock:
                    _reminders = data
                active = sum(1 for r in data if not r.get("fired"))
                print(f"[remind] loaded {len(data)} reminder(s) ({active} active)")
    except Exception as e:
        print(f"[remind] warn: could not load reminders: {e}")


def _save_reminders():
    """Persist reminders to disk (atomic write)."""
    with _reminders_lock:
        data = list(_reminders)
    try:
        os.makedirs(os.path.dirname(_reminders_file), exist_ok=True)
        tmp = _reminders_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _reminders_file)
    except Exception as e:
        print(f"[remind] warn: could not save reminders: {e}")


def create_reminder(text, fire_at, recurring_seconds=None):
    """Create a new reminder. Returns the reminder dict."""
    reminder = {
        "id": str(uuid.uuid4()),
        "text": text,
        "fire_at": fire_at,
        "created_at": time.time(),
        "recurring_seconds": recurring_seconds,
        "fired": False,
    }
    with _reminders_lock:
        _reminders.append(reminder)
    _save_reminders()
    print(f"[remind] created: '{text}' fires at {time.strftime('%H:%M:%S', time.localtime(fire_at))}"
          + (f" (every {recurring_seconds}s)" if recurring_seconds else " (one-shot)"))
    return reminder


def cancel_reminder(reminder_id):
    """Cancel a reminder by ID. Returns True if found and cancelled."""
    with _reminders_lock:
        for r in _reminders:
            if r["id"] == reminder_id and not r.get("fired"):
                r["fired"] = True
                _save_reminders()
                print(f"[remind] cancelled: {reminder_id}")
                return True
    return False


def list_active_reminders():
    """Return list of active (unfired) reminders."""
    with _reminders_lock:
        return [r for r in _reminders if not r.get("fired")]


def _reminder_engine_loop():
    """Background loop: check reminders every 10s, fire when due."""
    global _reminder_engine_running
    _reminder_engine_running = True
    print("[remind] engine started (10s tick)")

    while _reminder_engine_running:
        now = time.time()
        to_fire = []
        changed = False

        with _reminders_lock:
            for r in _reminders:
                if r.get("fired"):
                    continue
                if r["fire_at"] <= now:
                    to_fire.append(dict(r))
                    if r.get("recurring_seconds"):
                        # advance to next fire time (skip missed ticks)
                        interval = r["recurring_seconds"]
                        while r["fire_at"] <= now:
                            r["fire_at"] += interval
                        changed = True
                    else:
                        r["fired"] = True
                        changed = True

        # fire outside the lock
        for r in to_fire:
            try:
                emoji = "🔔" if not r.get("recurring_seconds") else "🔁"
                msg = f"{emoji} Reminder: {r['text']}"
                result = notify_all(msg)
                if result.get("sent"):
                    print(f"[remind] fired: '{r['text']}'")
                else:
                    print(f"[remind] delivery failed: {result.get('error', '?')}")
            except Exception as e:
                print(f"[remind] error firing reminder: {e}")

        if changed:
            _save_reminders()

        # prune old fired reminders (> 24h)
        cutoff = now - 86400
        with _reminders_lock:
            before = len(_reminders)
            _reminders[:] = [
                r for r in _reminders
                if not r.get("fired") or r.get("fire_at", 0) > cutoff
            ]
            if len(_reminders) < before:
                changed = True
        if changed:
            _save_reminders()

        # sleep 10s, checking shutdown every 2s
        for _ in range(5):
            if not _reminder_engine_running:
                break
            time.sleep(2)

    print("[remind] engine stopped")


def start_reminder_engine():
    """Start the reminder engine daemon thread."""
    global _reminder_engine_running
    if _reminder_engine_running:
        return
    _load_reminders()
    threading.Thread(target=_reminder_engine_loop, daemon=True).start()


# ── cron scheduler (channel-agnostic, reads goose schedule.json) ─────────────
#
# Replaces goose's built-in scheduler (which only runs inside `goose gateway`,
# not `goose web`). Reads the same schedule.json that `goose schedule add`
# writes to, so existing CLI commands work transparently.
#
# Architecture (mirrors OpenClaw's approach):
#   - scheduler loop runs inside gateway.py (not the LLM)
#   - each job fires in an isolated goose web session (fresh session per run)
#   - output delivered via notify_all() (channel-agnostic bus)
#   - anyone building a slack/whatsapp/discord gateway just registers a handler
#
# On each tick (30s):
#   1. read schedule.json
#   2. for each job where now >= next_run: fire it
#   3. firing = read recipe YAML -> relay instructions to goose web -> notify_all()
#   4. update last_run, write schedule.json back

_cron_scheduler_running = False
_GOOSE_SHARE_DIR = os.path.join(
    os.environ.get("HOME", "/home/gooseclaw"), ".local", "share", "goose"
)
_SCHEDULE_FILE = os.path.join(_GOOSE_SHARE_DIR, "schedule.json")
_CRON_TICK_SECONDS = 30


def _parse_cron_field(field, min_val, max_val):
    """Parse a single cron field into a set of valid integers."""
    values = set()
    for part in field.split(","):
        part = part.strip()
        # handle */N (step)
        if part.startswith("*/"):
            step = int(part[2:])
            values.update(range(min_val, max_val + 1, step))
        elif part == "*":
            values.update(range(min_val, max_val + 1))
        elif "-" in part:
            # range: 1-5
            lo, hi = part.split("-", 1)
            values.update(range(int(lo), int(hi) + 1))
        else:
            values.add(int(part))
    return values


def _cron_matches_now(cron_expr, now=None):
    """Check if a 5-field cron expression matches the current time.

    Fields: minute hour day-of-month month day-of-week
    """
    if now is None:
        now = time.localtime()
    fields = cron_expr.strip().split()
    if len(fields) == 6:
        # 6-field: drop seconds field (first position, goose uses this)
        fields = fields[1:]
    if len(fields) != 5:
        return False
    try:
        minutes = _parse_cron_field(fields[0], 0, 59)
        hours = _parse_cron_field(fields[1], 0, 23)
        days = _parse_cron_field(fields[2], 1, 31)
        months = _parse_cron_field(fields[3], 1, 12)
        weekdays = _parse_cron_field(fields[4], 0, 6)
    except (ValueError, IndexError):
        return False

    # convert Python weekday (0=Mon) to cron weekday (0=Sun)
    cron_wday = (now.tm_wday + 1) % 7
    return (
        now.tm_min in minutes
        and now.tm_hour in hours
        and now.tm_mday in days
        and now.tm_mon in months
        and cron_wday in weekdays
    )


def _load_schedule():
    """Read schedule.json. Returns list of job dicts."""
    try:
        if os.path.exists(_SCHEDULE_FILE):
            with open(_SCHEDULE_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f"[cron] warn: could not load schedule.json: {e}")
    return []


def _save_schedule(jobs):
    """Write schedule.json atomically."""
    try:
        os.makedirs(os.path.dirname(_SCHEDULE_FILE), exist_ok=True)
        tmp = _SCHEDULE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(jobs, f, indent=2)
        os.replace(tmp, _SCHEDULE_FILE)
    except Exception as e:
        print(f"[cron] warn: could not save schedule.json: {e}")


def _load_recipe(source_path):
    """Read a recipe YAML file and extract the instructions field.

    Uses a minimal parser (no PyYAML dependency) — reads the 'instructions: |'
    block which is the only field we need.
    """
    try:
        if not os.path.exists(source_path):
            return None
        with open(source_path) as f:
            content = f.read()

        # extract 'instructions: |' block (YAML literal block scalar)
        # find the line starting with 'instructions:'
        lines = content.split("\n")
        capture = False
        indent = 0
        instruction_lines = []

        for line in lines:
            if line.strip().startswith("instructions:"):
                # check if it's a block scalar (ends with |)
                rest = line.split("instructions:", 1)[1].strip()
                if rest == "|":
                    capture = True
                    continue
                elif rest:
                    # inline value
                    return rest
            elif capture:
                if line.strip() == "" and not instruction_lines:
                    continue  # skip leading blank
                # detect indent of first content line
                if not instruction_lines and line.strip():
                    indent = len(line) - len(line.lstrip())
                # block ends when we hit a line with less/equal indent that's not blank
                if line.strip() and (len(line) - len(line.lstrip())) < indent and instruction_lines:
                    break
                # strip the indent prefix
                if len(line) >= indent:
                    instruction_lines.append(line[indent:])
                else:
                    instruction_lines.append(line.lstrip())

        if instruction_lines:
            return "\n".join(instruction_lines).strip()
        return None
    except Exception as e:
        print(f"[cron] warn: could not read recipe {source_path}: {e}")
        return None


def _fire_cron_job(job):
    """Execute a cron job: relay recipe instructions to goose web, deliver output.

    Runs in a fresh isolated session (like OpenClaw's sessionTarget: "isolated").
    """
    job_id = job.get("id", "unknown")
    source = job.get("source", "")
    print(f"[cron] firing job: {job_id}")

    instructions = _load_recipe(source)
    if not instructions:
        print(f"[cron] skip {job_id}: no instructions found in {source}")
        return

    # create an isolated session ID for this run
    session_id = f"cron_{job_id}_{time.strftime('%Y%m%d_%H%M%S')}"

    # prefix with job context so the agent knows it's a cron run
    prompt = (
        f"[cron: {job_id}]\n\n"
        f"You are running as a scheduled cron job. "
        f"Complete the task below and deliver the output using the `notify` command "
        f"(pipe your output into it: echo \"YOUR_OUTPUT\" | notify). "
        f"Be concise.\n\n"
        f"{instructions}"
    )

    # relay to goose web
    response_text, error = _do_ws_relay(prompt, session_id)

    if error:
        print(f"[cron] job {job_id} failed: {error}")
        # notify about the failure
        notify_all(f"[cron:{job_id}] failed: {error}")
        return

    # if the response contains useful output, deliver it
    # (the recipe may have already called notify via shell, but we deliver
    # the response too in case it didn't)
    if response_text and response_text != "(No response from goose)":
        # check if the agent already called notify (contains "notify" tool output)
        # if so, the output was already delivered. deliver anyway as fallback
        # since double-delivery is better than no delivery.
        formatted = f"[{job_id}]\n\n{response_text}"
        if len(formatted) > 4000:
            formatted = formatted[:3997] + "..."
        notify_all(formatted)

    print(f"[cron] job {job_id} completed")


def _cron_scheduler_loop():
    """Background loop: check schedule.json every 30s, fire due jobs."""
    global _cron_scheduler_running
    _cron_scheduler_running = True
    print(f"[cron] scheduler started ({_CRON_TICK_SECONDS}s tick)")

    while _cron_scheduler_running:
        try:
            # wait for goose web to be ready
            with _startup_state_lock:
                ready = goose_startup_state["state"] == "ready"
            if not ready:
                time.sleep(10)
                continue

            jobs = _load_schedule()
            now = time.localtime()
            save_needed = False

            for job in jobs:
                if job.get("paused"):
                    continue
                if job.get("currently_running"):
                    continue

                cron_expr = job.get("cron", "")
                if not cron_expr:
                    continue

                # check if this job matches the current minute
                if not _cron_matches_now(cron_expr, now):
                    continue

                # check last_run to avoid double-firing within the same minute
                last_run = job.get("last_run", "")
                if last_run:
                    try:
                        if "T" in last_run:
                            lr_time = last_run.split("T")[1][:5]  # HH:MM
                            now_time = time.strftime("%H:%M", now)
                            if lr_time == now_time:
                                continue
                    except Exception:
                        pass

                # fire it in a thread so we don't block other jobs
                job["currently_running"] = True
                save_needed = True

                def _run_job(j, all_jobs):
                    try:
                        _fire_cron_job(j)
                    finally:
                        j["currently_running"] = False
                        j["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
                        j["current_session_id"] = None
                        _save_schedule(all_jobs)

                threading.Thread(target=_run_job, args=(job, jobs), daemon=True).start()

            if save_needed:
                _save_schedule(jobs)

        except Exception as e:
            print(f"[cron] error: {e}")

        # sleep 30s, checking shutdown every 5s
        for _ in range(6):
            if not _cron_scheduler_running:
                break
            time.sleep(5)

    print("[cron] scheduler stopped")


def start_cron_scheduler():
    """Start the cron scheduler daemon thread."""
    global _cron_scheduler_running
    if _cron_scheduler_running:
        return
    threading.Thread(target=_cron_scheduler_loop, daemon=True).start()


# ── channel plugin system ─────────────────────────────────────────────────────
#
# Each channel is a .py file in /data/channels/ with a CHANNEL dict:
#   CHANNEL = {
#       "name": "slack",              # REQUIRED
#       "version": 1,                 # REQUIRED
#       "send": send_fn,              # REQUIRED: (text) -> {"sent": bool, "error": str}
#       "poll": poll_fn,              # OPTIONAL: (relay_fn, stop_event, creds) -> None (blocking)
#       "setup": setup_fn,            # OPTIONAL: (creds) -> {"ok": bool, "error": str}
#       "teardown": teardown_fn,      # OPTIONAL: () -> None
#       "credentials": ["TOKEN"],     # OPTIONAL: keys resolved from env then sidecar JSON
#   }
#
# Credentials sidecar: /data/channels/<name>.json -> {"TOKEN": "value"}
# Files prefixed with _ are skipped (use for templates like _example.py).
# Hot-reload via POST /api/channels/reload.

def get_paired_user_ids(platform):
    """Read paired user IDs from config.yaml filtered by platform."""
    user_ids = []
    if not os.path.exists(GOOSE_CONFIG_PATH):
        return user_ids
    try:
        with open(GOOSE_CONFIG_PATH) as f:
            content = f.read()
        in_pairings = False
        current_entry = {}
        for line in content.split("\n"):
            stripped = line.strip()
            if line.startswith("gateway_pairings:"):
                in_pairings = True
                continue
            if in_pairings:
                if line and not line[0].isspace() and not stripped.startswith("-"):
                    break
                if stripped.startswith("- platform:"):
                    if current_entry.get("platform") == platform and current_entry.get("user_id"):
                        user_ids.append(current_entry["user_id"])
                    current_entry = {"platform": stripped.split(":", 1)[1].strip()}
                elif stripped.startswith("user_id:"):
                    val = stripped.split(":", 1)[1].strip().strip("'\"")
                    current_entry["user_id"] = val
        if current_entry.get("platform") == platform and current_entry.get("user_id"):
            user_ids.append(current_entry["user_id"])
    except Exception as e:
        print(f"[channels] warn: could not read pairings for {platform}: {e}")
    return user_ids


def _resolve_channel_creds(name, cred_keys):
    """Resolve credential keys: check os.environ first, then /data/channels/<name>.json."""
    creds = {}
    sidecar = {}
    sidecar_path = os.path.join(CHANNELS_DIR, f"{name}.json")
    if os.path.exists(sidecar_path):
        try:
            with open(sidecar_path) as f:
                sidecar = json.load(f)
        except Exception as e:
            print(f"[channels] warn: could not read {sidecar_path}: {e}")
    for key in cred_keys:
        val = os.environ.get(key, "") or sidecar.get(key, "")
        creds[key] = val
    return creds


class ChannelRelay:
    """Relay function wrapper for channel plugins. Manages per-channel sessions."""

    def __init__(self, channel_name):
        self._name = channel_name
        self._sessions_file = os.path.join(DATA_DIR, f"channel_sessions_{channel_name}.json")
        self._sessions = {}
        self._lock = threading.Lock()
        # load existing sessions
        try:
            if os.path.exists(self._sessions_file):
                with open(self._sessions_file) as f:
                    self._sessions.update(json.load(f))
        except Exception:
            pass

    def _save(self):
        try:
            with self._lock:
                data = dict(self._sessions)
            tmp = self._sessions_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._sessions_file)
        except Exception as e:
            print(f"[channels] warn: could not save sessions for {self._name}: {e}")

    def __call__(self, user_id, text):
        """Relay a message from channel user to goose web. Returns response text."""
        user_key = str(user_id)
        with self._lock:
            session_id = self._sessions.get(user_key)

        if not session_id:
            session_id = f"{self._name}_{user_key}_{time.strftime('%Y%m%d_%H%M%S')}"
            with self._lock:
                self._sessions[user_key] = session_id
            self._save()

        response_text, error = _do_ws_relay(text, session_id)
        if error:
            # try new session on failure
            session_id = f"{self._name}_{user_key}_{time.strftime('%Y%m%d_%H%M%S')}"
            with self._lock:
                self._sessions[user_key] = session_id
            self._save()
            response_text, error = _do_ws_relay(text, session_id)

        if error:
            return f"Error: {error}"
        return response_text

    def reset_session(self, user_id):
        """Reset a user's session (for /clear, /newsession commands)."""
        with self._lock:
            self._sessions.pop(str(user_id), None)
        self._save()


def _deregister_notification_handler(name):
    """Remove a handler from the notification bus by name."""
    with _notification_handlers_lock:
        _notification_handlers[:] = [h for h in _notification_handlers if h["name"] != name]


def _load_channel(filepath):
    """Load a single channel plugin from a .py file."""
    basename = os.path.basename(filepath)
    mod_name = basename[:-3]  # strip .py

    print(f"[channels] loading {basename}...")

    try:
        spec = importlib.util.spec_from_file_location(f"channel_{mod_name}", filepath)
        if not spec or not spec.loader:
            print(f"[channels] skip {basename}: could not create module spec")
            return False

        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        channel = getattr(mod, "CHANNEL", None)
        if not isinstance(channel, dict):
            print(f"[channels] skip {basename}: no CHANNEL dict found")
            return False

        name = channel.get("name")
        if not name or not isinstance(name, str):
            print(f"[channels] skip {basename}: CHANNEL.name is required")
            return False

        send_fn = channel.get("send")
        if not callable(send_fn):
            print(f"[channels] skip {basename}: CHANNEL.send must be callable")
            return False

        # resolve credentials
        cred_keys = channel.get("credentials", [])
        creds = _resolve_channel_creds(name, cred_keys) if cred_keys else {}

        # check required creds are present
        missing = [k for k in cred_keys if not creds.get(k)]
        if missing:
            print(f"[channels] skip {name}: missing credentials: {', '.join(missing)}")
            return False

        # call setup() if provided
        setup_fn = channel.get("setup")
        if callable(setup_fn):
            try:
                result = setup_fn(creds)
                if isinstance(result, dict) and not result.get("ok", True):
                    print(f"[channels] skip {name}: setup failed: {result.get('error', '?')}")
                    return False
            except Exception as e:
                print(f"[channels] skip {name}: setup() raised: {e}")
                return False

        # register notification handler (wraps send_fn)
        def _make_handler(fn):
            def handler(text):
                try:
                    return fn(text)
                except Exception as e:
                    return {"sent": False, "error": str(e)}
            return handler

        register_notification_handler(f"channel:{name}", _make_handler(send_fn))

        # start poll thread if provided
        poll_fn = channel.get("poll")
        stop_event = threading.Event()
        poll_thread = None

        if callable(poll_fn):
            relay_fn = ChannelRelay(name)

            def _poll_wrapper(_fn, _relay, _stop, _creds):
                try:
                    _fn(_relay, _stop, _creds)
                except Exception as e:
                    print(f"[channels] {name} poll() crashed: {e}")

            poll_thread = threading.Thread(
                target=_poll_wrapper,
                args=(poll_fn, relay_fn, stop_event, creds),
                daemon=True,
            )
            poll_thread.start()

        with _channels_lock:
            _loaded_channels[name] = {"module": mod, "channel": channel, "creds": creds}
            _channel_stop_events[name] = stop_event
            if poll_thread:
                _channel_threads[name] = poll_thread

        has_poll = "poll" if callable(poll_fn) else "send-only"
        print(f"[channels] loaded: {name} v{channel.get('version', '?')} ({has_poll})")
        return True

    except Exception as e:
        print(f"[channels] error loading {basename}: {e}")
        return False


def _unload_channel(name):
    """Unload a channel plugin: teardown, stop poll, deregister."""
    with _channels_lock:
        entry = _loaded_channels.pop(name, None)
        stop_event = _channel_stop_events.pop(name, None)
        thread = _channel_threads.pop(name, None)

    if not entry:
        return

    # call teardown() if provided
    teardown_fn = entry["channel"].get("teardown")
    if callable(teardown_fn):
        try:
            teardown_fn()
        except Exception as e:
            print(f"[channels] {name} teardown() error: {e}")

    # stop poll thread
    if stop_event:
        stop_event.set()
    if thread:
        thread.join(timeout=5)

    # deregister from notification bus
    _deregister_notification_handler(f"channel:{name}")

    print(f"[channels] unloaded: {name}")


def _load_all_channels():
    """Discover and load all channel plugins from /data/channels/."""
    os.makedirs(CHANNELS_DIR, exist_ok=True)
    plugins = sorted(glob.glob(os.path.join(CHANNELS_DIR, "*.py")))
    loaded = 0
    for filepath in plugins:
        basename = os.path.basename(filepath)
        if basename.startswith("_"):
            continue
        try:
            if _load_channel(filepath):
                loaded += 1
        except Exception as e:
            print(f"[channels] error loading {basename}: {e}")
    if loaded:
        print(f"[channels] {loaded} channel(s) loaded")
    else:
        print("[channels] no channel plugins found")


def _reload_channels():
    """Unload all channels and reload from disk. Returns list of loaded names."""
    with _channels_lock:
        names = list(_loaded_channels.keys())
    for name in names:
        _unload_channel(name)
    _load_all_channels()
    with _channels_lock:
        return list(_loaded_channels.keys())


# ── session watcher: persistence + API helpers + loop ────────────────────────

def _load_watcher_state():
    """Load session watcher state from disk."""
    global _session_watcher_state
    try:
        if os.path.exists(_session_watcher_state_file):
            with open(_session_watcher_state_file) as f:
                data = json.load(f)
            if isinstance(data, dict):
                with _session_watcher_lock:
                    _session_watcher_state = data
                print(f"[watcher] loaded {len(data)} tracked session(s)")
    except Exception as e:
        print(f"[watcher] warn: could not load state: {e}")


def _save_watcher_state():
    """Persist session watcher state to disk."""
    with _session_watcher_lock:
        data = dict(_session_watcher_state)
    try:
        os.makedirs(os.path.dirname(_session_watcher_state_file), exist_ok=True)
        tmp = _session_watcher_state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _session_watcher_state_file)
    except Exception as e:
        print(f"[watcher] warn: could not save state: {e}")


def _fetch_scheduled_sessions():
    """Fetch sessions from goose web and return only scheduled ones."""
    if not _INTERNAL_GOOSE_TOKEN:
        return []
    try:
        conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=10)
        conn.request("GET", "/api/sessions", headers={
            "Authorization": f"Bearer {_INTERNAL_GOOSE_TOKEN}",
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        if resp.status != 200:
            return []
        data = json.loads(body)
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        return [s for s in sessions if s.get("schedule_id")]
    except Exception as e:
        print(f"[watcher] error fetching sessions: {e}")
        return []


def _fetch_session_messages(session_id):
    """Fetch full conversation from a goose web session. Returns list of message dicts."""
    if not _INTERNAL_GOOSE_TOKEN:
        return []
    try:
        conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=15)
        conn.request("GET", f"/api/sessions/{urllib.parse.quote(str(session_id))}", headers={
            "Authorization": f"Bearer {_INTERNAL_GOOSE_TOKEN}",
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        if resp.status != 200:
            return []
        session = json.loads(body)
        conversation = session.get("conversation") or []
        if isinstance(conversation, dict):
            conversation = conversation.get("messages", [])
        messages = []
        for msg in conversation:
            role = msg.get("role", "")
            content_items = msg.get("content", [])
            text_parts = []
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    text_parts.append(item)
            if text_parts:
                messages.append({"role": role, "text": "\n".join(text_parts)})
        return messages
    except Exception as e:
        print(f"[watcher] error fetching session {session_id}: {e}")
        return []


def _session_watcher_loop():
    """Poll goose web for scheduled session output and auto-forward to telegram."""
    global _session_watcher_running
    _session_watcher_running = True
    print("[watcher] session watcher started")

    while _session_watcher_running:
        try:
            # wait for goose web to be ready
            with _startup_state_lock:
                ready = goose_startup_state["state"] == "ready"
            if not ready:
                time.sleep(10)
                continue

            scheduled = _fetch_scheduled_sessions()
            state_changed = False

            for session in scheduled:
                sid = session.get("id", "")
                schedule_id = session.get("schedule_id", "unknown")
                msg_count = session.get("message_count", 0) or 0
                if not sid:
                    continue

                with _session_watcher_lock:
                    tracked = _session_watcher_state.get(sid, {})
                    forwarded = tracked.get("forwarded_count", 0)

                if msg_count <= forwarded:
                    continue  # no new messages

                # fetch full conversation
                messages = _fetch_session_messages(sid)
                if not messages:
                    continue

                # extract new assistant messages beyond what we already forwarded
                for msg in messages[forwarded:]:
                    if msg["role"] == "assistant" and msg["text"].strip():
                        text = msg["text"].strip()
                        formatted = f"[{schedule_id}]\n\n{text}"
                        if len(formatted) > 4000:
                            formatted = formatted[:3997] + "..."
                        result = notify_all(formatted)
                        if result.get("sent"):
                            print(f"[watcher] forwarded output from {schedule_id}")
                        else:
                            print(f"[watcher] delivery failed for {schedule_id}: {result.get('error', '?')}")

                # update tracking
                with _session_watcher_lock:
                    _session_watcher_state[sid] = {
                        "forwarded_count": len(messages),
                        "schedule_id": schedule_id,
                        "last_seen": time.time(),
                    }
                state_changed = True

            # prune stale entries (> 7 days, no longer in session list)
            active_ids = {s.get("id") for s in scheduled}
            cutoff = time.time() - 7 * 86400
            with _session_watcher_lock:
                stale = [
                    sid for sid, info in _session_watcher_state.items()
                    if sid not in active_ids and info.get("last_seen", 0) < cutoff
                ]
                for sid in stale:
                    del _session_watcher_state[sid]
                    state_changed = True

            if state_changed:
                _save_watcher_state()

        except Exception as e:
            print(f"[watcher] error: {e}")

        # sleep 30s, checking shutdown flag every 5s
        for _ in range(6):
            if not _session_watcher_running:
                break
            time.sleep(5)

    print("[watcher] session watcher stopped")


def start_session_watcher():
    """Start the session watcher daemon thread."""
    global _session_watcher_running
    if _session_watcher_running:
        return
    _load_watcher_state()
    threading.Thread(target=_session_watcher_loop, daemon=True).start()


# ── telegram session management ─────────────────────────────────────────────

def _get_session_id(chat_id):
    """Get or create a session_id for a telegram chat_id.

    For new chats, calls POST /agent/start on goose web to get a real session_id.
    For existing chats, returns the stored session_id.
    """
    chat_key = str(chat_id)
    with _telegram_sessions_lock:
        sid = _telegram_sessions.get(chat_key)
    if sid:
        return sid

    # create a new agent session via goose web
    sid = _create_goose_session()
    if not sid:
        # fallback to random UUID if goose web is unavailable
        sid = str(uuid.uuid4())
        print(f"[telegram] warn: could not start agent, using random session {sid}")

    with _telegram_sessions_lock:
        _telegram_sessions[chat_key] = sid
    _save_telegram_sessions()
    print(f"[telegram] new session {sid} for chat {chat_key}")
    return sid


def _create_goose_session():
    """Create a new session via GET / on goose web (follows redirect to get session_id).

    Returns the session_id string, or None on failure.
    """
    if not _INTERNAL_GOOSE_TOKEN:
        return None

    try:
        conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=10)
        conn.request("GET", "/", headers={
            "Authorization": f"Bearer {_INTERNAL_GOOSE_TOKEN}",
        })
        resp = conn.getresponse()
        resp.read()  # consume body

        if resp.status in (301, 302, 303, 307, 308):
            location = resp.getheader("Location", "")
            # location is like /session/20260311_170000
            if "/session/" in location:
                sid = location.split("/session/")[-1].strip("/")
                conn.close()
                print(f"[telegram] created session via redirect: {sid}")
                return sid

        # fallback: try GET /api/sessions to find the latest
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=10)
        conn.request("GET", "/api/sessions", headers={
            "Authorization": f"Bearer {_INTERNAL_GOOSE_TOKEN}",
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()

        if resp.status == 200:
            sessions = json.loads(body)
            if isinstance(sessions, list) and sessions:
                sid = sessions[-1].get("id") or sessions[-1].get("session_id")
                if sid:
                    print(f"[telegram] using latest session from /api/sessions: {sid}")
                    return str(sid)

        print(f"[telegram] could not create session: GET / returned {resp.status}")
        return None

    except Exception as e:
        print(f"[telegram] session creation failed: {e}")
        return None


# ── minimal WebSocket client (stdlib only, no external deps) ────────────────

def _ws_connect(host, port, path, auth_token=None):
    """Open a WebSocket connection. Returns the raw socket or raises."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(120)
    sock.connect((host, port))

    # generate a random 16-byte key for the handshake
    ws_key = base64.b64encode(os.urandom(16)).decode()

    # build auth headers — try both Bearer and Basic (goose web accepts both)
    auth_headers = ""
    if auth_token:
        auth_headers = f"Authorization: Bearer {auth_token}\r\n"

    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"{auth_headers}"
        f"\r\n"
    )
    sock.sendall(request.encode())

    # read the HTTP response (until \r\n\r\n)
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise ConnectionError("WebSocket handshake failed: connection closed")
        response += chunk

    status_line = response.split(b"\r\n")[0].decode()
    if "101" not in status_line:
        sock.close()
        raise ConnectionError(f"WebSocket handshake failed: {status_line}")

    return sock


def _ws_send_text(sock, text):
    """Send a text frame over WebSocket."""
    payload = text.encode("utf-8")
    mask_key = os.urandom(4)

    # build frame header
    header = bytearray()
    header.append(0x81)  # FIN=1, opcode=1 (text)

    length = len(payload)
    if length < 126:
        header.append(0x80 | length)  # MASK=1
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", length))

    header.extend(mask_key)

    # mask the payload
    masked = bytearray(len(payload))
    for i in range(len(payload)):
        masked[i] = payload[i] ^ mask_key[i % 4]

    sock.sendall(bytes(header) + bytes(masked))


def _ws_recv_frame(sock):
    """Read one WebSocket frame. Returns (opcode, payload_bytes) or raises."""
    def _recv_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            buf += chunk
        return buf

    header = _recv_exact(2)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F

    if length == 126:
        length = struct.unpack(">H", _recv_exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(8))[0]

    if masked:
        mask_key = _recv_exact(4)
        payload = bytearray(_recv_exact(length))
        for i in range(length):
            payload[i] ^= mask_key[i % 4]
        return opcode, bytes(payload)

    return opcode, _recv_exact(length)


def _ws_recv_text(sock):
    """Read text frames, handling pings/close. Returns text string or None on close."""
    while True:
        opcode, payload = _ws_recv_frame(sock)
        if opcode == 0x1:  # text frame
            return payload.decode("utf-8", errors="replace")
        elif opcode == 0x9:  # ping
            # send pong
            pong = bytearray([0x8A, 0x80 | len(payload)])
            mask_key = os.urandom(4)
            pong.extend(mask_key)
            masked = bytearray(len(payload))
            for i in range(len(payload)):
                masked[i] = payload[i] ^ mask_key[i % 4]
            pong.extend(masked)
            sock.sendall(bytes(pong))
        elif opcode == 0x8:  # close
            return None
        # ignore other frames (continuation, binary, pong)


# ── telegram bot API helpers ────────────────────────────────────────────────

def _send_typing_action(bot_token, chat_id):
    """Send 'typing' chat action to telegram."""
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendChatAction"
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "action": "typing",
        }).encode()
        req = urllib.request.Request(url, data=payload)
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # non-critical


def _relay_to_goose_web(user_text, session_id, chat_id=None):
    """Send a user message to goose web via WebSocket and return the assistant's text.

    Returns (response_text, error_string). On success error_string is empty.
    If chat_id is provided and the session is stale, creates a new session and retries.
    """
    if not _INTERNAL_GOOSE_TOKEN:
        return "", "Goose is not ready yet (no internal token). Please try again in a moment."

    text, err = _do_ws_relay(user_text, session_id)

    # if connection or session error, try creating a new session
    if err and chat_id:
        print(f"[telegram] relay failed ({err}), creating new session")
        new_sid = _create_goose_session()
        if new_sid:
            with _telegram_sessions_lock:
                _telegram_sessions[str(chat_id)] = new_sid
            _save_telegram_sessions()
            print(f"[telegram] retrying with new session {new_sid}")
            return _do_ws_relay(user_text, new_sid)

    return text, err


def _do_ws_relay(user_text, session_id):
    """Connect to goose web via WebSocket, send a message, collect the response.

    Returns (response_text, error_string).
    """
    ws_path = f"/ws?token={urllib.parse.quote(str(_INTERNAL_GOOSE_TOKEN))}"
    t0 = time.time()
    print(f"[relay] start session={session_id} text={user_text[:50]!r}")

    sock = None
    try:
        sock = _ws_connect("127.0.0.1", GOOSE_WEB_PORT, ws_path, auth_token=_INTERNAL_GOOSE_TOKEN)
        t_connect = time.time()
        print(f"[relay] ws connected in {t_connect - t0:.1f}s")

        # send the user message
        msg = json.dumps({
            "type": "message",
            "content": user_text,
            "session_id": session_id,
            "timestamp": int(time.time() * 1000),
        })
        _ws_send_text(sock, msg)

        # collect response chunks until "complete"
        collected = []
        first_chunk_time = None
        while True:
            frame_text = _ws_recv_text(sock)
            if frame_text is None:
                # connection closed
                break

            try:
                event = json.loads(frame_text)
            except (json.JSONDecodeError, ValueError):
                continue

            etype = event.get("type", "")

            if etype == "response":
                content = event.get("content", "")
                if content:
                    if first_chunk_time is None:
                        first_chunk_time = time.time()
                        print(f"[relay] first chunk in {first_chunk_time - t0:.1f}s (TTFB)")
                    collected.append(content)
            elif etype == "error":
                err_msg = event.get("message", "Unknown error")
                print(f"[relay] error event after {time.time() - t0:.1f}s: {err_msg}")
                sock.close()
                return "", f"Goose error: {err_msg}"
            elif etype == "complete":
                break
            # ignore: thinking, tool_request, tool_confirmation, tool_response, cancelled

        sock.close()
        elapsed = time.time() - t0
        full_text = "".join(collected).strip()
        print(f"[relay] done in {elapsed:.1f}s ({len(full_text)} chars) session={session_id}")
        if not full_text:
            return "(No response from goose)", ""
        return full_text, ""

    except ConnectionError as e:
        print(f"[relay] connection error after {time.time() - t0:.1f}s: {e}")
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        return "", f"WebSocket error: {e}"
    except Exception as e:
        print(f"[relay] error after {time.time() - t0:.1f}s: {e}")
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        return "", f"Error communicating with goose: {e}"


# ── pairing helpers (self-contained, no Rust subprocess) ────────────────────

def _add_pairing_to_config(chat_id):
    """Add a telegram pairing entry to goose config.yaml (gateway_pairings section)."""
    config_path = GOOSE_CONFIG_PATH
    chat_str = str(chat_id)
    try:
        content = ""
        if os.path.exists(config_path):
            with open(config_path) as f:
                content = f.read()

        # check if already paired
        if chat_str in content:
            # crude check — good enough since chat IDs are unique numeric strings
            return

        pairing_entry = (
            f"  - platform: telegram\n"
            f"    user_id: '{chat_str}'\n"
            f"    state: paired\n"
        )
        if "gateway_pairings:" in content:
            # append to existing section
            content = content.replace("gateway_pairings:\n", "gateway_pairings:\n" + pairing_entry, 1)
        else:
            content = content.rstrip("\n") + "\ngateway_pairings:\n" + pairing_entry

        tmp = config_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, config_path)
        print(f"[telegram] paired chat_id {chat_str}")
    except Exception as e:
        print(f"[telegram] warn: could not write pairing: {e}")


def _generate_and_store_pair_code():
    """Generate a random 6-char alphanumeric pairing code and store globally."""
    global telegram_pair_code
    # generate a 6-character uppercase alphanumeric code
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    code = "".join(secrets.choice(alphabet) for _ in range(6))
    with telegram_pair_lock:
        telegram_pair_code = code
    print(f"[telegram] pairing code: {code}")
    return code


# ── telegram polling loop ───────────────────────────────────────────────────

def _telegram_poll_loop(bot_token):
    """Long-poll telegram for updates and relay messages to goose web.

    Runs in a daemon thread. Handles pairing and message relay.
    """
    global _telegram_running, telegram_pair_code
    offset = 0
    _telegram_running = True
    print("[telegram] polling loop started")

    while _telegram_running:
        try:
            url = (
                f"https://api.telegram.org/bot{bot_token}/getUpdates"
                f"?offset={offset}&timeout=30&allowed_updates=[\"message\"]"
            )
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.loads(resp.read())

            if not data.get("ok"):
                print(f"[telegram] getUpdates not ok: {data}")
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg:
                    continue

                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()
                if not chat_id or not text:
                    continue

                paired_ids = get_paired_chat_ids()

                if chat_id in paired_ids:
                    # ── handle local slash commands before relaying ──
                    lower = text.lower()

                    if lower == "/help":
                        help_text = (
                            "🪿 *GooseClaw Commands*\n\n"
                            "*Session:*\n"
                            "/clear — wipe conversation and start fresh\n"
                            "/newsession — same as /clear\n"
                            "/compact — summarize history to save tokens\n\n"
                            "*MCP Prompts:*\n"
                            "/prompts — list available extension prompts\n"
                            "/prompt <name> — run a prompt\n\n"
                            "/help — this message"
                        )
                        send_telegram_message(bot_token, chat_id, help_text)
                        continue

                    if lower in ("/newsession", "/clear"):
                        with _telegram_sessions_lock:
                            old = _telegram_sessions.pop(chat_id, None)
                        # generate a fresh session ID directly — goose web auto-creates
                        # sessions on first message. _create_goose_session() unreliably
                        # returns the existing session via GET / redirect, so we bypass it.
                        new_sid = time.strftime("%Y%m%d_%H%M%S")
                        with _telegram_sessions_lock:
                            _telegram_sessions[chat_id] = new_sid
                        _save_telegram_sessions()
                        label = "cleared" if lower == "/clear" else "started"
                        send_telegram_message(
                            bot_token, chat_id,
                            f"🔄 Session {label}. Conversation history is fresh."
                        )
                        print(f"[telegram] session reset for chat {chat_id} (old: {old}, new: {new_sid})")
                        continue

                    if lower == "/compact":
                        # relay /compact to goose as a regular message — it handles summarization
                        _send_typing_action(bot_token, chat_id)
                        session_id = _get_session_id(chat_id)
                        response_text, error = _relay_to_goose_web(
                            "Please summarize our conversation so far into key points, "
                            "then we can continue from this summary. Be concise.",
                            session_id, chat_id=chat_id
                        )
                        if error:
                            send_telegram_message(bot_token, chat_id, f"Error: {error}")
                        else:
                            send_telegram_message(bot_token, chat_id, f"📝 Compacted:\n\n{response_text}")
                        continue

                    # ── relay to goose web ──
                    _send_typing_action(bot_token, chat_id)
                    session_id = _get_session_id(chat_id)

                    # send typing indicator periodically in a background thread
                    typing_stop = threading.Event()

                    def _typing_loop(_bt=bot_token, _cid=chat_id):
                        while not typing_stop.is_set():
                            _send_typing_action(_bt, _cid)
                            typing_stop.wait(4)

                    typing_thread = threading.Thread(target=_typing_loop, daemon=True)
                    typing_thread.start()

                    try:
                        response_text, error = _relay_to_goose_web(text, session_id, chat_id=chat_id)
                    finally:
                        typing_stop.set()
                        typing_thread.join(timeout=2)

                    if error:
                        send_telegram_message(bot_token, chat_id, f"Error: {error}")
                    else:
                        send_telegram_message(bot_token, chat_id, response_text)
                else:
                    # unpaired user — check if this is a pairing code
                    with telegram_pair_lock:
                        current_code = telegram_pair_code

                    if current_code and text.upper() == current_code.upper():
                        # valid pairing code — pair this chat
                        _add_pairing_to_config(chat_id)
                        with telegram_pair_lock:
                            # consume the code so it can't be reused
                            telegram_pair_code = None
                        send_telegram_message(
                            bot_token, chat_id,
                            "Paired successfully! You can now send messages to goose through this chat."
                        )
                        print(f"[telegram] chat {chat_id} paired via code {current_code}")
                    else:
                        send_telegram_message(
                            bot_token, chat_id,
                            "You are not paired with this goose instance. "
                            "Please enter a valid pairing code from the web dashboard."
                        )

        except urllib.error.HTTPError as e:
            if e.code == 409:
                # conflict — another getUpdates call is running; back off
                print("[telegram] conflict (409), backing off 10s")
                time.sleep(10)
            elif e.code == 401:
                print("[telegram] FATAL: invalid bot token (401). Stopping poll loop.")
                _telegram_running = False
                return
            else:
                print(f"[telegram] HTTP error {e.code}, retrying in 5s")
                time.sleep(5)
        except urllib.error.URLError as e:
            print(f"[telegram] network error: {e.reason}, retrying in 5s")
            time.sleep(5)
        except Exception as e:
            print(f"[telegram] poll error: {e}, retrying in 5s")
            time.sleep(5)

    print("[telegram] polling loop stopped")


def start_telegram_gateway(bot_token):
    """Start the Python telegram polling thread if not already running."""
    global _telegram_running

    if _telegram_running:
        print("[telegram] polling already running")
        return

    _load_telegram_sessions()

    # register telegram with the notification bus
    register_notification_handler("telegram", _telegram_notify_handler)

    # generate an initial pairing code
    _generate_and_store_pair_code()

    thread = threading.Thread(target=_telegram_poll_loop, args=(bot_token,), daemon=True)
    thread.start()
    print("[telegram] polling thread started")


def start_goose_web():
    global goose_process, _INTERNAL_GOOSE_TOKEN
    _check_stale_pid("goose_web")
    _set_startup_state("starting", "Starting goose web...")
    with goose_lock:
        if goose_process and goose_process.poll() is None:
            goose_process.terminate()
            try:
                goose_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                goose_process.kill()

        # Generate a random internal token for goose web communication.
        # This token is never exposed to users -- gateway handles all user auth.
        # Users authenticate against the stored hash; gateway then proxies
        # requests to goose web using this internal token.
        _INTERNAL_GOOSE_TOKEN = secrets.token_urlsafe(32)
        cmd = ["goose", "web", "--host", "127.0.0.1", "--port", str(GOOSE_WEB_PORT)]
        cmd += ["--auth-token", _INTERNAL_GOOSE_TOKEN]

        print(f"[gateway] starting goose web on 127.0.0.1:{GOOSE_WEB_PORT}")
        print(f"[gateway] cmd: goose web --host 127.0.0.1 --port {GOOSE_WEB_PORT} --auth-token [internal]")
        goose_process = subprocess.Popen(cmd, stdout=sys.stdout, stderr=subprocess.PIPE)
        _write_pid("goose_web", goose_process.pid)

        # Start daemon thread to read stderr line-by-line, forward to sys.stderr,
        # and buffer lines for the startup status API.
        threading.Thread(target=_stderr_reader, args=(goose_process,), daemon=True).start()

        # wait for it to listen
        for i in range(30):
            time.sleep(1)
            # check if process exited prematurely
            if goose_process.poll() is not None:
                exit_code = goose_process.returncode
                _set_startup_state("error", f"goose web exited with code {exit_code}", error=_get_recent_stderr(20))
                print(f"[gateway] goose web exited during startup with code {exit_code}")
                return False
            try:
                conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=2)
                conn.request("GET", "/api/health")
                resp = conn.getresponse()
                if resp.status == 200:
                    _set_startup_state("ready", "goose web is running")
                    print("[gateway] goose web is ready")
                    return True
                conn.close()
            except Exception:
                pass

        _set_startup_state("error", "goose web did not become ready in 30s", error=_get_recent_stderr(20))
        print("[gateway] WARN: goose web did not become ready in 30s")
        return False


def stop_goose_web():
    global goose_process
    with goose_lock:
        if goose_process and goose_process.poll() is None:
            goose_process.terminate()
            try:
                goose_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                goose_process.kill()
        goose_process = None
    _remove_pid("goose_web")


def goose_health_monitor():
    """Monitor goose web subprocess and auto-restart on crash with backoff."""
    backoff = 5  # initial backoff seconds
    max_backoff = 120
    consecutive_failures = 0

    while True:
        time.sleep(15)  # check every 15 seconds
        if not is_configured():
            continue

        with goose_lock:
            proc = goose_process
        if proc is None:
            continue

        if proc.poll() is not None:
            # process has exited
            exit_code = proc.returncode
            consecutive_failures += 1
            wait_time = min(backoff * (2 ** (consecutive_failures - 1)), max_backoff)
            _set_startup_state("starting", f"Restarting goose web (attempt #{consecutive_failures})...")
            print(f"[health] goose web exited (code {exit_code}). "
                  f"Restart #{consecutive_failures} in {wait_time}s...")
            _remove_pid("goose_web")
            time.sleep(wait_time)
            try:
                start_goose_web()
                print(f"[health] goose web restarted after failure #{consecutive_failures}")
            except Exception as e:
                print(f"[health] restart failed: {e}")
        else:
            # process is running, reset backoff on sustained health
            if consecutive_failures > 0:
                # verify it's actually responding
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=3)
                    conn.request("GET", "/api/health")
                    resp = conn.getresponse()
                    conn.close()
                    if resp.status == 200:
                        consecutive_failures = 0
                except Exception:
                    pass  # will catch on next cycle if it dies


# ── input sanitization ───────────────────────────────────────────────────────

def _sanitize_string(value, max_length=2000):
    """Sanitize a string value: strip whitespace, truncate, remove control characters."""
    if not isinstance(value, str):
        return value
    # strip leading/trailing whitespace
    value = value.strip()
    # truncate to max length
    value = value[:max_length]
    # remove control characters (except newline \n=0x0a and tab \t=0x09)
    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)
    return value


# ── HTTP handler ────────────────────────────────────────────────────────────

class GatewayHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Structured request logging with timestamp and format string."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if args:
            print(f"[gateway] {timestamp} {format % args}")
        else:
            print(f"[gateway] {timestamp} {format}")

    def log_request(self, code="-", size="-"):
        """Override to log request with duration."""
        duration_ms = int((time.time() - getattr(self, "_request_start", time.time())) * 1000)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"[gateway] {timestamp} {self.command} {self.path} {code} {duration_ms}ms")

    def _check_rate_limit(self, limiter):
        """Return True if request is allowed; send 429 and return False if over limit."""
        ip = self.client_address[0]
        if not limiter.is_allowed(ip):
            self.send_json(429, {"error": "Too many requests. Try again later.", "code": "RATE_LIMITED"})
            return False
        return True

    # ── routing ──

    def do_GET(self):
        self._request_start = time.time()
        path = urllib.parse.urlparse(self.path).path
        # rate-limit all /api/* requests; skip static /setup pages and proxy
        if path.startswith("/api/") and not self._check_rate_limit(api_limiter):
            return
        if path == "/api/health":
            self.handle_health()
        elif path == "/api/health/ready":
            self.handle_health_ready()
        elif path == "/api/setup/status":
            self.handle_startup_status()
        elif path == "/api/version":
            self.handle_version()
        elif path.rstrip("/") == "/setup" or path.startswith("/setup/"):
            self.handle_setup_page()
        elif path == "/api/setup/config":
            self.handle_get_config()
        elif path == "/api/notify/status":
            self.handle_notify_status()
        elif path == "/api/telegram/status":
            self.handle_telegram_status()
        elif path == "/api/reminders":
            self.handle_list_reminders()
        elif path == "/api/channels":
            self.handle_list_channels()
        elif not is_configured():
            self.send_response(302)
            self.send_header("Location", "/setup")
            self.end_headers()
        else:
            self.proxy_to_goose()

    def do_POST(self):
        self._request_start = time.time()
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/setup/save":
            self.handle_save()
        elif path == "/api/setup/validate":
            self.handle_validate()
        elif path == "/api/notify":
            self.handle_notify()
        elif path == "/api/telegram/pair":
            self.handle_telegram_pair()
        elif path == "/api/auth/recover":
            self.handle_auth_recover()
        elif path == "/api/reminders":
            self.handle_create_reminder()
        elif path == "/api/channels/reload":
            self.handle_reload_channels()
        else:
            self.proxy_to_goose()

    def do_PUT(self):
        self.proxy_to_goose()

    def do_DELETE(self):
        self._request_start = time.time()
        path = urllib.parse.urlparse(self.path).path
        # DELETE /api/reminders/<id>
        if path.startswith("/api/reminders/"):
            reminder_id = path[len("/api/reminders/"):]
            if reminder_id:
                self.handle_cancel_reminder(reminder_id)
                return
        self.proxy_to_goose()

    def do_OPTIONS(self):
        # Handle CORS preflight for /api/* paths without proxying to goose.
        # Only echo Origin back if it is same-host; otherwise omit CORS headers
        # so the browser blocks the cross-origin request.
        if self.path.startswith("/api/"):
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            self.send_response(200)
            if origin and host and (
                origin == f"http://{host}" or origin == f"https://{host}"
            ):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.proxy_to_goose()

    def do_PATCH(self):
        self.proxy_to_goose()

    # ── health endpoints ──

    def _ping_goose_web(self):
        """Try to ping goose web subprocess. Returns 'healthy', 'unhealthy (HTTP N)', or 'unreachable'."""
        try:
            conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=2)
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            conn.close()
            return "healthy" if resp.status == 200 else f"unhealthy (HTTP {resp.status})"
        except Exception:
            return "unreachable"

    def handle_health(self):
        """GET /api/health — deep health check: liveness + goose web subprocess status."""
        status = {"service": "gooseclaw", "configured": is_configured()}

        if goose_process and goose_process.poll() is None:
            # process is alive — probe it
            status["goose_web"] = self._ping_goose_web()
        else:
            status["goose_web"] = "not running" if is_configured() else "not started (unconfigured)"

        if not is_configured():
            status["status"] = "setup_required"
        elif status.get("goose_web") == "healthy":
            status["status"] = "ok"
        else:
            status["status"] = "degraded"

        # 200 for ok/setup_required (healthy enough to serve traffic), 503 for degraded
        code = 200 if status["status"] in ("ok", "setup_required") else 503
        self.send_json(code, status)

    def handle_health_ready(self):
        """GET /api/health/ready — readiness probe: 200 only when goose web is up and responding."""
        if goose_process and goose_process.poll() is None:
            result = self._ping_goose_web()
            if result == "healthy":
                self.send_json(200, {"ready": True, "goose_web": "healthy"})
                return
            self.send_json(503, {"ready": False, "goose_web": result})
        else:
            reason = "not started (unconfigured)" if not is_configured() else "not running"
            self.send_json(503, {"ready": False, "goose_web": reason})

    def handle_version(self):
        """GET /api/version — return the deployed version from VERSION file."""
        version = "unknown"
        version_file = os.path.join(APP_DIR, "VERSION")
        if os.path.exists(version_file):
            try:
                with open(version_file) as f:
                    version = f.read().strip()
            except Exception:
                pass
        self.send_json(200, {"version": version, "service": "gooseclaw"})

    # ── startup status endpoint ──

    def handle_startup_status(self):
        """GET /api/setup/status — goose web startup state (no auth required)."""
        with _startup_state_lock:
            state_copy = dict(goose_startup_state)
        self.send_json(200, state_copy)

    # ── setup endpoints ──

    def handle_setup_page(self):
        # first boot (no setup.json) = open. after that = require auth.
        # allow unauthenticated access to recovery page
        query = urllib.parse.urlparse(self.path).query
        is_recovery = "recover" in urllib.parse.parse_qs(query)
        if load_setup() and not is_recovery and not check_auth(self):
            body = b"Authentication required. Lost your token? Visit /setup?recover"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="gooseclaw setup"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            with open(SETUP_HTML, "rb") as f:
                content = f.read()
            mtime = os.path.getmtime(SETUP_HTML)
            etag = f'"{int(mtime)}"'
            # conditional request support
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            # security headers for HTML response
            for header, value in SECURITY_HEADERS.items():
                self.send_header(header, value)
            # override X-Frame-Options already covered by CSP frame-ancestors
            # Content-Security-Policy for setup.html
            # unsafe-inline for script-src is required because setup.html has inline JS
            csp = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src https://fonts.gstatic.com; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
            self.send_header("Content-Security-Policy", csp)
            if os.environ.get("RAILWAY_ENVIRONMENT"):
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            # persist auth via session cookie after successful Basic Auth
            self._inject_session_cookie()
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "Setup page not found")

    def handle_get_config(self):
        if load_setup() and not check_auth(self):
            self.send_response(401)
            self.end_headers()
            return
        setup = load_setup()
        if setup:
            safe = {**setup}

            # ── Top-level secret masking ─────────────────────────────────────
            # Replace secret values with a fixed placeholder ("********").
            # This reveals NOTHING about the key (no partial leakage).
            # Also add boolean companion fields (_set) so the frontend can
            # display "key already set" placeholders without knowing the value.
            # telegram_bot_token is removed entirely — frontend only needs bool.
            SECRET_FIELDS = (
                "api_key",
                "claude_setup_token",
                "custom_key",
                "web_auth_token",
                "web_auth_token_hash",
            )
            for key in SECRET_FIELDS:
                val = safe.get(key, "")
                safe[f"{key}_set"] = bool(val)
                if val:
                    safe[key] = "********"
                else:
                    safe.pop(key, None)

            # telegram_bot_token: only expose whether it is set, never the value
            tbt = safe.pop("telegram_bot_token", "")
            safe["telegram_bot_token_set"] = bool(tbt)

            # ── saved_keys masking ───────────────────────────────────────────
            # Return "********" as the masked value so typeof val === 'string'
            # still holds in setup.html's updateDashboardCredField().
            # Also add a saved_keys_set dict with booleans for smarter UI hints.
            if "saved_keys" in safe and isinstance(safe["saved_keys"], dict):
                masked_keys = {}
                set_indicators = {}
                for provider_id, val in safe["saved_keys"].items():
                    if isinstance(val, str) and val:
                        masked_keys[provider_id] = "********"
                        set_indicators[provider_id] = True
                    elif isinstance(val, dict) and val:
                        # complex value (e.g. azure key+endpoint dict) — mask
                        # each string sub-field
                        masked_sub = {}
                        for sub_key, sub_val in val.items():
                            masked_sub[sub_key] = "********" if sub_val else sub_val
                        masked_keys[provider_id] = masked_sub
                        set_indicators[provider_id] = True
                    else:
                        masked_keys[provider_id] = val
                        set_indicators[provider_id] = False
                safe["saved_keys"] = masked_keys
                safe["saved_keys_set"] = set_indicators

            self.send_json(200, {"configured": True, "config": safe})
        else:
            self.send_json(200, {"configured": False})

    def handle_save(self):
        if not self._check_rate_limit(auth_limiter):
            return
        if load_setup() and not check_auth(self):
            self.send_response(401)
            self.end_headers()
            return
        body = self._read_body()
        try:
            config = json.loads(body)

            # sanitize all string fields before validation
            for key, val in list(config.items()):
                if isinstance(val, str):
                    config[key] = _sanitize_string(val)

            # validate config schema before accepting
            valid, errors = validate_setup_config(config)
            if not valid:
                self.send_json(400, {"success": False, "errors": errors})
                return

            # auto-generate auth token if not provided
            plaintext_token = config.get("web_auth_token", "")
            if not plaintext_token and not os.environ.get("GOOSE_WEB_AUTH_TOKEN"):
                plaintext_token = secrets.token_urlsafe(24)

            # hash the token before storage -- plaintext never hits disk
            if plaintext_token:
                config["web_auth_token_hash"] = hash_token(plaintext_token)
                # remove plaintext from config dict before saving
                config.pop("web_auth_token", None)

            save_setup(config)
            apply_config(config)

            # restart goose web in background
            def _restart():
                time.sleep(1)
                start_goose_web()
                start_session_watcher()
                start_reminder_engine()
                start_cron_scheduler()
            threading.Thread(target=_restart, daemon=True).start()

            resp = {"success": True, "message": "saved. agent is restarting..."}
            if plaintext_token:
                # one-time display to user -- not stored in setup.json
                resp["auth_token"] = plaintext_token
            self.send_json(200, resp)

        except json.JSONDecodeError:
            self.send_json(400, {"success": False, "error": "invalid JSON", "code": "INVALID_CONFIG"})
        except Exception as e:
            print(f"[gateway] ERROR (handle_save): {e}", file=sys.stderr)
            self.send_json(500, {"success": False, "error": "Internal server error. Check server logs.", "code": "INTERNAL_ERROR"})

    def handle_validate(self):
        if not self._check_rate_limit(auth_limiter):
            return
        body = self._read_body()
        try:
            data = json.loads(body)
            provider = _sanitize_string(data.get("provider_type") or data.get("provider", ""))
            credentials = data.get("credentials", data)
            # sanitize credential string values
            if isinstance(credentials, dict):
                credentials = {k: _sanitize_string(v) for k, v in credentials.items()}
            result = dispatch_validation(provider, credentials)
            self.send_json(200, result)
        except Exception as e:
            print(f"[gateway] ERROR (handle_validate): {e}", file=sys.stderr)
            self.send_json(500, {"valid": False, "error": "Internal server error. Check server logs.", "code": "INTERNAL_ERROR"})

    # ── notify endpoints ──

    def handle_notify(self):
        """POST /api/notify — send a message to all paired telegram users."""
        if not self._check_rate_limit(notify_limiter):
            return
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        # allow unauthenticated calls from localhost (e.g. notify.sh, cron jobs)
        client_ip = self.client_address[0] if self.client_address else ""
        is_local = client_ip in ("127.0.0.1", "::1", "localhost")
        if not is_local and not check_auth(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="gooseclaw"')
            self.end_headers()
            return
        body = self._read_body()
        try:
            data = json.loads(body)
            text = _sanitize_string(data.get("text", ""), max_length=4000)
            if not text:
                self.send_json(400, {"sent": False, "error": "text field is required"})
                return
            result = notify_all(text)
            status_code = 200 if result["sent"] else 502
            self.send_json(status_code, result)
        except json.JSONDecodeError:
            self.send_json(400, {"sent": False, "error": "invalid JSON", "code": "INVALID_CONFIG"})
        except Exception as e:
            print(f"[gateway] ERROR (handle_notify): {e}", file=sys.stderr)
            self.send_json(500, {"sent": False, "error": "Internal server error. Check server logs.", "code": "INTERNAL_ERROR"})

    def handle_notify_status(self):
        """GET /api/notify/status — check if notification delivery is available."""
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        token = get_bot_token()
        chat_ids = get_paired_chat_ids()
        self.send_json(200, {
            "available": bool(token and chat_ids),
            "bot_configured": bool(token),
            "paired_users": len(chat_ids),
        })

    # ── telegram pairing endpoints ──

    def handle_telegram_status(self):
        """GET /api/telegram/status — telegram gateway status, paired users, pairing code."""
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        token = get_bot_token()
        running = _telegram_running

        chat_ids = get_paired_chat_ids()
        with telegram_pair_lock:
            code = telegram_pair_code

        self.send_json(200, {
            "running": running,
            "bot_configured": bool(token),
            "paired_users": len(chat_ids),
            "paired_chat_ids": chat_ids,
            "pairing_code": code,
        })

    def handle_telegram_pair(self):
        """POST /api/telegram/pair — generate a new pairing code."""
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        if not check_auth(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="gooseclaw"')
            self.end_headers()
            return

        if not _telegram_running:
            # try to start telegram first
            token = get_bot_token()
            if token:
                start_telegram_gateway(token)
                # give the thread a moment to start
                time.sleep(2)
            else:
                self.send_json(400, {"error": "no telegram bot token configured", "code": None})
                return

        # generate new code
        code = _generate_and_store_pair_code()
        if code:
            self.send_json(200, {"code": code, "message": "send this code to your telegram bot"})
        else:
            self.send_json(500, {"error": "could not generate pairing code. check logs.", "code": None})

    # ── reminder endpoints ──

    def _check_local_or_auth(self):
        """Allow localhost without auth, require auth for remote. Returns True if allowed."""
        client_ip = self.client_address[0] if self.client_address else ""
        is_local = client_ip in ("127.0.0.1", "::1", "localhost")
        if is_local:
            return True
        if not check_auth(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="gooseclaw"')
            self.end_headers()
            return False
        return True

    def handle_create_reminder(self):
        """POST /api/reminders — create a new reminder.

        JSON body:
          text: str (required) — the reminder message
          delay_seconds: int — fire after N seconds from now
          fire_at: float — unix timestamp to fire at
          recurring_seconds: int — repeat interval (0 or null = one-shot)

        Must provide either delay_seconds or fire_at (delay_seconds takes priority).
        """
        if not self._check_rate_limit(api_limiter):
            return
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        if not self._check_local_or_auth():
            return
        body = self._read_body()
        try:
            data = json.loads(body)
            text = _sanitize_string(data.get("text", ""), max_length=500)
            if not text:
                self.send_json(400, {"error": "text field is required"})
                return

            delay = data.get("delay_seconds")
            fire_at = data.get("fire_at")
            recurring = data.get("recurring_seconds")

            if delay is not None:
                try:
                    delay = int(delay)
                    if delay < 1:
                        self.send_json(400, {"error": "delay_seconds must be >= 1"})
                        return
                    fire_at = time.time() + delay
                except (ValueError, TypeError):
                    self.send_json(400, {"error": "delay_seconds must be an integer"})
                    return
            elif fire_at is not None:
                try:
                    fire_at = float(fire_at)
                    if fire_at <= time.time():
                        self.send_json(400, {"error": "fire_at must be in the future"})
                        return
                except (ValueError, TypeError):
                    self.send_json(400, {"error": "fire_at must be a unix timestamp"})
                    return
            else:
                self.send_json(400, {"error": "provide either delay_seconds or fire_at"})
                return

            if recurring is not None:
                try:
                    recurring = int(recurring)
                    if recurring < 30:
                        self.send_json(400, {"error": "recurring_seconds must be >= 30 (minimum 30s interval)"})
                        return
                except (ValueError, TypeError):
                    self.send_json(400, {"error": "recurring_seconds must be an integer"})
                    return
            else:
                recurring = None

            reminder = create_reminder(text, fire_at, recurring_seconds=recurring)
            self.send_json(201, {
                "created": True,
                "reminder": reminder,
                "fires_in_seconds": round(fire_at - time.time()),
                "fires_at_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fire_at)),
            })
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
        except Exception as e:
            self._internal_error(e, "handle_create_reminder")

    def handle_list_reminders(self):
        """GET /api/reminders — list active reminders."""
        if not self._check_rate_limit(api_limiter):
            return
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        if not self._check_local_or_auth():
            return
        active = list_active_reminders()
        now = time.time()
        for r in active:
            r["fires_in_seconds"] = round(r["fire_at"] - now)
            r["fires_at_human"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["fire_at"]))
        self.send_json(200, {"reminders": active, "count": len(active)})

    def handle_cancel_reminder(self, reminder_id):
        """DELETE /api/reminders/<id> — cancel a reminder."""
        if not self._check_rate_limit(api_limiter):
            return
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        if not self._check_local_or_auth():
            return
        if cancel_reminder(reminder_id):
            self.send_json(200, {"cancelled": True, "id": reminder_id})
        else:
            self.send_json(404, {"error": "reminder not found or already fired"})

    # ── channel plugin endpoints ──

    def handle_list_channels(self):
        """GET /api/channels — list loaded channel plugins (localhost only)."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        with _channels_lock:
            channels = []
            for name, entry in _loaded_channels.items():
                ch = entry["channel"]
                channels.append({
                    "name": name,
                    "version": ch.get("version", 0),
                    "has_poll": callable(ch.get("poll")),
                    "has_setup": callable(ch.get("setup")),
                    "credentials": ch.get("credentials", []),
                })
        self.send_json(200, {"channels": channels, "count": len(channels)})

    def handle_reload_channels(self):
        """POST /api/channels/reload — hot-reload all channel plugins (localhost only)."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        names = _reload_channels()
        self.send_json(200, {"reloaded": True, "channels": names, "count": len(names)})

    # ── auth recovery endpoint ──

    def handle_auth_recover(self):
        """POST /api/auth/recover — reset auth token using recovery secret."""
        if not self._check_rate_limit(auth_limiter):
            return
        recovery_secret = os.environ.get("GOOSECLAW_RECOVERY_SECRET", "")
        if not recovery_secret:
            self.send_json(404, {"error": "auth recovery not configured. Set GOOSECLAW_RECOVERY_SECRET env var."})
            return
        body = self._read_body()
        try:
            data = json.loads(body)
            provided = _sanitize_string(data.get("secret", ""))
            if not provided:
                self.send_json(400, {"error": "secret field is required"})
                return
            if not secrets.compare_digest(provided, recovery_secret):
                self.send_json(403, {"error": "invalid recovery secret"})
                return
            # generate new auth token
            new_token = secrets.token_urlsafe(24)
            new_hash = hash_token(new_token)
            # update setup.json
            setup = load_setup()
            if not setup:
                self.send_json(400, {"error": "no setup configuration found"})
                return
            setup["web_auth_token_hash"] = new_hash
            setup.pop("web_auth_token", None)  # remove legacy plaintext
            save_setup(setup)
            self.send_json(200, {
                "success": True,
                "auth_token": new_token,
                "message": "Auth token reset. Save this token — it won't be shown again."
            })
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
        except Exception as e:
            self._internal_error(e, "handle_auth_recover")

    # ── reverse proxy to goose web ──

    def proxy_to_goose(self):
        if not is_configured():
            self.send_response(302)
            self.send_header("Location", "/setup")
            self.end_headers()
            return

        with goose_lock:
            gproc = goose_process
        if gproc is None or gproc.poll() is not None:
            with _startup_state_lock:
                state_copy = dict(goose_startup_state)
            stderr_tail = _get_recent_stderr(10)
            error_detail = {
                "status": state_copy["state"],
                "message": state_copy.get("message", "Agent is starting up"),
                "error": stderr_tail if state_copy["state"] == "error" else "",
                "retry_after": 5,
            }
            body = json.dumps(error_detail).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "5")
            self.end_headers()
            self.wfile.write(body)
            return

        try:
            conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=PROXY_TIMEOUT)

            # forward headers
            headers = {}
            for key in self.headers:
                if key.lower() not in ("host", "transfer-encoding", "connection"):
                    headers[key] = self.headers[key]
            headers["Host"] = f"127.0.0.1:{GOOSE_WEB_PORT}"
            headers["Connection"] = "close"

            # replace user Authorization with internal token
            # gateway already authenticated the user in do_GET/do_POST
            if _INTERNAL_GOOSE_TOKEN:
                auth_value = base64.b64encode(
                    f"user:{_INTERNAL_GOOSE_TOKEN}".encode()
                ).decode()
                headers["Authorization"] = f"Basic {auth_value}"

            # read body
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()

            # send response status and headers
            self.send_response(resp.status)
            is_sse = False
            proxied_headers = set()
            for key, val in resp.getheaders():
                lower = key.lower()
                if lower in ("transfer-encoding", "connection"):
                    continue
                # rewrite location headers
                if lower == "location":
                    val = val.replace(f"http://127.0.0.1:{GOOSE_WEB_PORT}", "")
                self.send_header(key, val)
                proxied_headers.add(lower)
                if lower == "content-type" and "text/event-stream" in val:
                    is_sse = True
            # inject security headers into proxied responses (don't overwrite if already set)
            for header, value in SECURITY_HEADERS.items():
                if header.lower() not in proxied_headers:
                    self.send_header(header, value)
            if os.environ.get("RAILWAY_ENVIRONMENT"):
                if "strict-transport-security" not in proxied_headers:
                    self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            # persist auth via session cookie after successful Basic Auth
            self._inject_session_cookie()
            self.end_headers()

            # stream the response body
            if is_sse:
                # SSE connections are long-lived — remove socket timeout so they
                # don't get killed by PROXY_TIMEOUT during an active stream
                try:
                    conn.sock.settimeout(None)
                except Exception:
                    pass
                while True:
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                self.wfile.flush()

            conn.close()

        except OSError:
            try:
                with _startup_state_lock:
                    state_copy = dict(goose_startup_state)
                stderr_tail = _get_recent_stderr(10)
                error_detail = {
                    "status": state_copy["state"],
                    "message": state_copy.get("message", "Agent is starting up"),
                    "error": stderr_tail if state_copy["state"] == "error" else "",
                    "retry_after": 5,
                }
                body = json.dumps(error_detail).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", "5")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass  # client disconnected
        except Exception as e:
            print(f"[gateway] proxy error: {e}", file=sys.stderr)
            try:
                self.send_error(502, "Gateway error")
            except Exception:
                pass

    # ── helpers ──

    def _internal_error(self, e, context=""):
        """Log real error to stderr, return sanitized response to client."""
        print(f"[gateway] ERROR ({context}): {e}", file=sys.stderr)
        self.send_json(500, {"error": "Internal server error. Check server logs.", "code": "INTERNAL_ERROR"})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _inject_session_cookie(self):
        """If Basic Auth succeeded this request, set a session cookie so the
        browser won't re-prompt on subsequent requests."""
        if getattr(self, "_set_session_cookie", False):
            stored, _ = get_auth_token()
            if stored:
                cookie_val = _make_session_cookie(stored)
                secure_flag = "; Secure" if os.environ.get("RAILWAY_ENVIRONMENT") else ""
                self.send_header(
                    "Set-Cookie",
                    f"gooseclaw_session={cookie_val}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000{secure_flag}",
                )
            self._set_session_cookie = False

    def send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # security headers on every JSON response
        for header, value in SECURITY_HEADERS.items():
            self.send_header(header, value)
        # add HSTS only when running on Railway (which terminates TLS)
        if os.environ.get("RAILWAY_ENVIRONMENT"):
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Origin-aware CORS: only allow same-host origins, never wildcard.
        # Same-host means the Origin header matches the request Host header
        # (accounting for http/https scheme). Requests with no Origin header
        # (same-origin or server-to-server) need no CORS header at all.
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if origin and host and (
            origin == f"http://{host}" or origin == f"https://{host}"
        ):
            self.send_header("Access-Control-Allow-Origin", origin)
        # persist auth via session cookie after successful Basic Auth
        self._inject_session_cookie()
        self.end_headers()
        self.wfile.write(body)


# ── main ────────────────────────────────────────────────────────────────────

def main():
    print(f"[gateway] gooseclaw gateway starting on 0.0.0.0:{PORT}")

    if is_configured():
        # re-apply config from setup.json (env vars lost on container restart)
        setup = load_setup()
        if setup:
            apply_config(setup)
        print("[gateway] provider configured. starting goose web...")
        start_goose_web()

        # start health monitor to auto-restart goose web on crash
        health_thread = threading.Thread(target=goose_health_monitor, daemon=True)
        health_thread.start()

        # start session watcher to auto-forward scheduled output to telegram
        start_session_watcher()

        # start reminder engine (lightweight timers, bypasses goose scheduler)
        start_reminder_engine()

        # start cron scheduler (reads goose schedule.json, fires jobs via goose web)
        start_cron_scheduler()

        # load channel plugins from /data/channels/
        _load_all_channels()

        # start telegram if token is available but apply_config didn't handle it
        # (env-var-only deployments without setup.json)
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if tg_token:
            start_telegram_gateway(tg_token)
    else:
        print("[gateway] no provider configured. serving setup wizard.")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), GatewayHandler)

    # periodic rate limiter cleanup (every 5 minutes) to free stale IP entries
    def _rate_limiter_cleanup():
        while True:
            time.sleep(300)
            api_limiter.cleanup()
            auth_limiter.cleanup()
            notify_limiter.cleanup()

    threading.Thread(target=_rate_limiter_cleanup, daemon=True).start()

    def shutdown(_sig, _frame):
        global _telegram_running, _reminder_engine_running, _cron_scheduler_running
        print("[gateway] shutting down...")
        # stop accepting new connections first
        threading.Thread(target=server.shutdown, daemon=True).start()
        # unload all channel plugins (stop threads, call teardown)
        with _channels_lock:
            channel_names = list(_loaded_channels.keys())
        for ch_name in channel_names:
            _unload_channel(ch_name)
        # terminate goose web and clean up PID
        stop_goose_web()
        _remove_pid("goose_web")
        # stop telegram polling thread, session watcher, reminder engine, cron scheduler
        _telegram_running = False
        _session_watcher_running = False
        _reminder_engine_running = False
        _cron_scheduler_running = False
        _remove_pid("telegram")
        print("[gateway] shutdown complete")

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.serve_forever()
    sys.exit(0)


if __name__ == "__main__":
    main()
