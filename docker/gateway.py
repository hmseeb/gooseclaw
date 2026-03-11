#!/usr/bin/env python3
"""
gooseclaw gateway — setup wizard + notification bus + reverse proxy to goose web.

Runs on $PORT. Serves /setup directly, proxies everything else to goose web
on an internal port. Manages the goose web subprocess lifecycle.

API:
  GET  /api/health           -> health check
  GET  /api/setup/config     -> current provider config (masked)
  GET  /api/setup/status     -> goose web startup state (idle/starting/ready/error)
  POST /api/setup/validate   -> validate provider credentials
  POST /api/setup/save       -> save provider config and restart
  POST /api/notify           -> send message to all paired telegram users
  GET  /api/notify/status    -> check if telegram delivery is available
  GET  /api/telegram/status  -> telegram gateway status, paired users, pairing code
  POST /api/telegram/pair    -> generate a new telegram pairing code
  POST /api/auth/recover     -> reset auth token using GOOSECLAW_RECOVERY_SECRET
"""

import base64
import collections
import hashlib
import http.client
import http.server
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
telegram_process = None
telegram_lock = threading.Lock()
telegram_pair_code = None
telegram_pair_lock = threading.Lock()

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


def send_telegram_message(bot_token, chat_id, text):
    """Send a message via telegram bot API. Returns (ok, error)."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # split long messages (telegram limit: 4096 chars)
    chunks = []
    if len(text) <= 4000:
        chunks = [text]
    else:
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > 4000:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

    for chunk in chunks:
        try:
            payload = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(url, data=payload)
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    # retry without markdown
                    payload = urllib.parse.urlencode({
                        "chat_id": chat_id,
                        "text": chunk,
                        "disable_web_page_preview": "true",
                    }).encode()
                    req = urllib.request.Request(url, data=payload)
                    urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError:
            # markdown parse failed, retry plain
            try:
                payload = urllib.parse.urlencode({
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": "true",
                }).encode()
                req = urllib.request.Request(url, data=payload)
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                return False, str(e)
        except Exception as e:
            return False, str(e)
    return True, ""


def notify_all(text):
    """Send a message to all paired telegram users. Returns summary dict."""
    token = get_bot_token()
    if not token:
        return {"sent": False, "error": "no bot token configured"}
    chat_ids = get_paired_chat_ids()
    if not chat_ids:
        return {"sent": False, "error": "no paired telegram users found"}
    results = []
    for cid in chat_ids:
        ok, err = send_telegram_message(token, cid, text)
        results.append({"chat_id": cid, "sent": ok, "error": err})
    return {"sent": all(r["sent"] for r in results), "recipients": results}


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


def check_auth(handler):
    """Check HTTP Basic Auth. Returns True if authorized."""
    stored, is_hashed = get_auth_token()
    if not stored:
        return True

    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            _, provided = decoded.split(":", 1)
            if is_hashed:
                return verify_token(provided, stored)
            return provided == stored
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

    # check if already installed
    if subprocess.run(["which", "claude"], capture_output=True).returncode == 0:
        print("[gateway] claude CLI already installed")
    else:
        print("[gateway] installing claude CLI...")
        try:
            subprocess.run(
                ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
                check=True, timeout=120,
            )
        except Exception:
            print("[gateway] native install failed, trying npm...")
            try:
                subprocess.run(
                    ["bash", "-c", "apt-get update -qq && apt-get install -y -qq nodejs npm >/dev/null 2>&1 && npm install -g @anthropic-ai/claude-code 2>/dev/null"],
                    check=True, timeout=180,
                )
            except Exception as e:
                print(f"[gateway] ERROR: could not install claude CLI: {e}")
                return

    # create ~/.claude.json if missing
    claude_json = os.path.join(home, ".claude.json")
    if not os.path.exists(claude_json):
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(claude_json, "w") as f:
            json.dump({"hasCompletedOnboarding": True}, f)
        print("[gateway] created ~/.claude.json")


def apply_config(config):
    """Write goose config.yaml and set env vars from setup config."""
    provider_type = config.get("provider_type", "")
    api_key = config.get("api_key", "")
    model = config.get("model", "")
    tz = config.get("timezone", "UTC")

    # set timezone
    os.environ["TZ"] = tz

    # base config
    config_path = os.path.join(CONFIG_DIR, "config.yaml")
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

    with open(config_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    # telegram — set env var AND start gateway if not already running
    tg_token = config.get("telegram_bot_token", "")
    if tg_token:
        os.environ["TELEGRAM_BOT_TOKEN"] = tg_token
        start_telegram_gateway(tg_token)


def _is_goose_gateway_running():
    """Check if a goose gateway process is already running (from entrypoint or previous start)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "goose gateway start.*telegram"],
            capture_output=True, text=True, timeout=5
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        return len(pids) > 0, pids
    except Exception:
        return False, []


def _generate_and_store_pair_code():
    """Generate a telegram pairing code and store it in the global."""
    global telegram_pair_code
    try:
        result = subprocess.run(
            ["goose", "gateway", "pair", "telegram"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        import re
        match = re.search(r'[A-Z0-9]{6}', output)
        if match:
            code = match.group()
            with telegram_pair_lock:
                telegram_pair_code = code
            print(f"[gateway] telegram pairing code: {code}")
            return code
        else:
            print(f"[gateway] telegram pair output: {output.strip()}")
    except Exception as e:
        print(f"[gateway] could not generate pair code: {e}")
    return None


def start_telegram_gateway(bot_token):
    """Start the telegram gateway process if not already running."""
    global telegram_process

    with telegram_lock:
        # check our tracked process first
        if telegram_process and telegram_process.poll() is None:
            print("[gateway] telegram gateway already running (tracked pid)")
            return

    # check for any goose gateway process (may have been started by entrypoint or previous run)
    running, pids = _is_goose_gateway_running()
    if running:
        print(f"[gateway] telegram gateway already running (external pids: {pids})")
        return

    _check_stale_pid("telegram")
    print("[gateway] starting telegram gateway...")
    try:
        proc = subprocess.Popen(
            ["goose", "gateway", "start", "--bot-token", bot_token, "telegram"],
            stdout=sys.stdout, stderr=sys.stderr
        )
        with telegram_lock:
            telegram_process = proc
        _write_pid("telegram", proc.pid)
        # generate pairing code after gateway has time to initialize
        def _delayed_pair():
            time.sleep(8)
            _generate_and_store_pair_code()
        threading.Thread(target=_delayed_pair, daemon=True).start()
    except Exception as e:
        print(f"[gateway] failed to start telegram: {e}")


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
        else:
            self.proxy_to_goose()

    def do_PUT(self):
        self.proxy_to_goose()

    def do_DELETE(self):
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
        if not check_auth(self):
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
        running = False
        with telegram_lock:
            tproc = telegram_process
        if tproc and tproc.poll() is None:
            running = True
        else:
            ext_running, _ = _is_goose_gateway_running()
            running = ext_running

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

        # check if telegram is running
        running = False
        with telegram_lock:
            tproc = telegram_process
        if tproc and tproc.poll() is None:
            running = True
        else:
            ext_running, _ = _is_goose_gateway_running()
            running = ext_running

        if not running:
            # try to start telegram first
            token = get_bot_token()
            if token:
                start_telegram_gateway(token)
                # wait for it to initialize
                time.sleep(10)
            else:
                self.send_json(400, {"error": "no telegram bot token configured", "code": None})
                return

        # generate new code
        code = _generate_and_store_pair_code()
        if code:
            self.send_json(200, {"code": code, "message": "send this code to your telegram bot"})
        else:
            self.send_json(500, {"error": "could not generate pairing code. check logs.", "code": None})

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
        print("[gateway] shutting down...")
        # stop accepting new connections first
        threading.Thread(target=server.shutdown, daemon=True).start()
        # terminate goose web and clean up PID
        stop_goose_web()
        _remove_pid("goose_web")
        # terminate telegram and clean up PID
        with telegram_lock:
            tproc = telegram_process
        if tproc and tproc.poll() is None:
            tproc.terminate()
            try:
                tproc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tproc.kill()
        _remove_pid("telegram")
        print("[gateway] shutdown complete")

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.serve_forever()
    sys.exit(0)


if __name__ == "__main__":
    main()
