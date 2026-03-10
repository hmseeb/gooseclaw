#!/usr/bin/env python3
"""
gooseclaw gateway — setup wizard + notification bus + reverse proxy to goose web.

Runs on $PORT. Serves /setup directly, proxies everything else to goose web
on an internal port. Manages the goose web subprocess lifecycle.

API:
  GET  /api/health           -> health check
  GET  /api/setup/config     -> current provider config (masked)
  POST /api/setup/validate   -> validate provider credentials
  POST /api/setup/save       -> save provider config and restart
  POST /api/notify           -> send message to all paired telegram users
  GET  /api/notify/status    -> check if telegram delivery is available
  GET  /api/telegram/status  -> telegram gateway status, paired users, pairing code
  POST /api/telegram/pair    -> generate a new telegram pairing code
"""

import base64
import http.client
import http.server
import json
import os
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

# ── config ──────────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_DIR = os.path.join(DATA_DIR, "config")
SETUP_FILE = os.path.join(CONFIG_DIR, "setup.json")
APP_DIR = os.environ.get("APP_DIR", "/app")
SETUP_HTML = os.path.join(APP_DIR, "docker", "setup.html")
PORT = int(os.environ.get("PORT", 8080))
GOOSE_WEB_PORT = 3001

goose_process = None
goose_lock = threading.Lock()
telegram_process = None
telegram_pair_code = None
telegram_pair_lock = threading.Lock()


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

def load_setup():
    if os.path.exists(SETUP_FILE):
        with open(SETUP_FILE) as f:
            return json.load(f)
    return None


def save_setup(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETUP_FILE, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(SETUP_FILE, 0o600)


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


def get_auth_token():
    """Get the active auth token from env var or setup file."""
    token = os.environ.get("GOOSE_WEB_AUTH_TOKEN", "")
    if token:
        return token
    setup = load_setup()
    if setup:
        return setup.get("web_auth_token", "")
    return ""


def check_auth(handler):
    """Check HTTP Basic Auth. Returns True if authorized."""
    token = get_auth_token()
    if not token:
        return True

    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            _, provided = decoded.split(":", 1)
            return provided == token
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
        return {"valid": True, "message": "Claude Code uses OAuth. Validation must be done manually.", "skip_validation": True}
    if provider == "github-copilot":
        return {"valid": True, "message": "GitHub Copilot token validation is not supported remotely.", "skip_validation": True}

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
        endpoint = credentials.get("AZURE_OPENAI_ENDPOINT") or credentials.get("endpoint", "")
        if not key or not endpoint:
            return {"valid": False, "error": "Both API key and endpoint are required."}
        return validate_azure_openai(key, endpoint)

    # LiteLLM
    if provider == "litellm":
        key = credentials.get("LITELLM_API_KEY") or credentials.get("api_key", "")
        host = credentials.get("LITELLM_HOST") or credentials.get("host", "")
        return validate_litellm(key, host)

    # Local providers
    if provider in ("ollama", "lm-studio", "docker-model-runner", "ramalama"):
        host = credentials.get("OLLAMA_HOST") or credentials.get("host") or credentials.get("url")
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
        # set env vars for the provider from the module-level registry
        for env_var in env_map.get(provider_type, []):
            val = config.get(env_var.lower(), "") or api_key
            if val:
                os.environ[env_var] = val
        lines.append(f"GOOSE_PROVIDER: {provider_type}")

    # default models per provider if none specified (from module-level registry)
    if not model:
        model = default_models.get(provider_type, "")

    if model:
        lines.append(f"GOOSE_MODEL: {model}")

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

    # check our tracked process first
    if telegram_process and telegram_process.poll() is None:
        print("[gateway] telegram gateway already running (tracked pid)")
        return

    # check for any goose gateway process (may have been started by entrypoint or previous run)
    running, pids = _is_goose_gateway_running()
    if running:
        print(f"[gateway] telegram gateway already running (external pids: {pids})")
        return

    print("[gateway] starting telegram gateway...")
    try:
        telegram_process = subprocess.Popen(
            ["goose", "gateway", "start", "--bot-token", bot_token, "telegram"],
            stdout=sys.stdout, stderr=sys.stderr
        )
        # generate pairing code after gateway has time to initialize
        def _delayed_pair():
            time.sleep(8)
            _generate_and_store_pair_code()
        threading.Thread(target=_delayed_pair, daemon=True).start()
    except Exception as e:
        print(f"[gateway] failed to start telegram: {e}")


def start_goose_web():
    global goose_process
    with goose_lock:
        if goose_process and goose_process.poll() is None:
            goose_process.terminate()
            try:
                goose_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                goose_process.kill()

        token = get_auth_token()
        cmd = ["goose", "web", "--host", "127.0.0.1", "--port", str(GOOSE_WEB_PORT)]
        if token:
            cmd += ["--auth-token", token]
        else:
            cmd += ["--no-auth"]

        print(f"[gateway] starting goose web on 127.0.0.1:{GOOSE_WEB_PORT}")
        print(f"[gateway] cmd: {' '.join(cmd)}")
        goose_process = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)

        # wait for it to listen
        for _ in range(30):
            time.sleep(1)
            try:
                conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=2)
                conn.request("GET", "/api/health")
                resp = conn.getresponse()
                if resp.status == 200:
                    print("[gateway] goose web is ready")
                    return True
                conn.close()
            except Exception:
                pass

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


# ── HTTP handler ────────────────────────────────────────────────────────────

class GatewayHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # quiet unless error
        if args and str(args[0]).startswith("5"):
            print(f"[gateway] {format % args}")

    # ── routing ──

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            self.send_json(200, {"status": "ok", "service": "gooseclaw"})
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
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/setup/save":
            self.handle_save()
        elif path == "/api/setup/validate":
            self.handle_validate()
        elif path == "/api/notify":
            self.handle_notify()
        elif path == "/api/telegram/pair":
            self.handle_telegram_pair()
        else:
            self.proxy_to_goose()

    def do_PUT(self):
        self.proxy_to_goose()

    def do_DELETE(self):
        self.proxy_to_goose()

    def do_OPTIONS(self):
        self.proxy_to_goose()

    def do_PATCH(self):
        self.proxy_to_goose()

    # ── setup endpoints ──

    def handle_setup_page(self):
        # first boot (no setup.json) = open. after that = require auth.
        if load_setup() and not check_auth(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="gooseclaw setup"')
            self.send_header("Content-Length", "23")
            self.end_headers()
            self.wfile.write(b"Authentication required")
            return
        try:
            with open(SETUP_HTML, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
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
            # mask top-level secrets
            for key in ("api_key", "claude_setup_token", "custom_key", "web_auth_token"):
                val = safe.get(key, "")
                if val and len(val) > 12:
                    safe[key] = val[:6] + "..." + val[-4:]
                elif val:
                    safe[key] = "***"
            # mask values inside saved_keys dict
            if "saved_keys" in safe and isinstance(safe["saved_keys"], dict):
                masked_keys = {}
                for provider_id, val in safe["saved_keys"].items():
                    if isinstance(val, str) and len(val) > 12:
                        masked_keys[provider_id] = val[:6] + "..." + val[-4:]
                    elif isinstance(val, str) and val:
                        masked_keys[provider_id] = "***"
                    else:
                        masked_keys[provider_id] = val
                safe["saved_keys"] = masked_keys
            self.send_json(200, {"configured": True, "config": safe})
        else:
            self.send_json(200, {"configured": False})

    def handle_save(self):
        if load_setup() and not check_auth(self):
            self.send_response(401)
            self.end_headers()
            return
        body = self._read_body()
        try:
            config = json.loads(body)

            # auto-generate auth token if not provided
            if not config.get("web_auth_token") and not os.environ.get("GOOSE_WEB_AUTH_TOKEN"):
                config["web_auth_token"] = secrets.token_urlsafe(24)

            save_setup(config)
            apply_config(config)

            # restart goose web in background
            def _restart():
                time.sleep(1)
                start_goose_web()
            threading.Thread(target=_restart, daemon=True).start()

            resp = {"success": True, "message": "saved. agent is restarting..."}
            if config.get("web_auth_token"):
                resp["auth_token"] = config["web_auth_token"]
            self.send_json(200, resp)

        except json.JSONDecodeError:
            self.send_json(400, {"success": False, "error": "invalid JSON"})
        except Exception as e:
            self.send_json(500, {"success": False, "error": str(e)})

    def handle_validate(self):
        body = self._read_body()
        try:
            data = json.loads(body)
            provider = data.get("provider_type") or data.get("provider", "")
            credentials = data.get("credentials", data)
            result = dispatch_validation(provider, credentials)
            self.send_json(200, result)
        except Exception as e:
            self.send_json(500, {"valid": False, "error": str(e)})

    # ── notify endpoints ──

    def handle_notify(self):
        """POST /api/notify — send a message to all paired telegram users."""
        body = self._read_body()
        try:
            data = json.loads(body)
            text = data.get("text", "")
            if not text:
                self.send_json(400, {"sent": False, "error": "text field is required"})
                return
            result = notify_all(text)
            status_code = 200 if result["sent"] else 502
            self.send_json(status_code, result)
        except json.JSONDecodeError:
            self.send_json(400, {"sent": False, "error": "invalid JSON"})
        except Exception as e:
            self.send_json(500, {"sent": False, "error": str(e)})

    def handle_notify_status(self):
        """GET /api/notify/status — check if notification delivery is available."""
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
        token = get_bot_token()
        running = False
        if telegram_process and telegram_process.poll() is None:
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
        if load_setup() and not check_auth(self):
            self.send_response(401)
            self.end_headers()
            return

        # check if telegram is running
        running = False
        if telegram_process and telegram_process.poll() is None:
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

    # ── reverse proxy to goose web ──

    def proxy_to_goose(self):
        if not is_configured():
            self.send_response(302)
            self.send_header("Location", "/setup")
            self.end_headers()
            return

        if goose_process is None or goose_process.poll() is not None:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain")
            body = b"Agent is starting up. Refresh in a few seconds."
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "5")
            self.end_headers()
            self.wfile.write(body)
            return

        try:
            conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=300)

            # forward headers
            headers = {}
            for key in self.headers:
                if key.lower() not in ("host", "transfer-encoding", "connection"):
                    headers[key] = self.headers[key]
            headers["Host"] = f"127.0.0.1:{GOOSE_WEB_PORT}"
            headers["Connection"] = "close"

            # read body
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()

            # send response status and headers
            self.send_response(resp.status)
            is_sse = False
            for key, val in resp.getheaders():
                lower = key.lower()
                if lower in ("transfer-encoding", "connection"):
                    continue
                # rewrite location headers
                if lower == "location":
                    val = val.replace(f"http://127.0.0.1:{GOOSE_WEB_PORT}", "")
                self.send_header(key, val)
                if lower == "content-type" and "text/event-stream" in val:
                    is_sse = True
            self.end_headers()

            # stream the response body
            if is_sse:
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
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                body = b"Agent is starting up. Refresh in a few seconds."
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", "5")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass  # client disconnected
        except Exception as e:
            try:
                self.send_error(502, f"Proxy error: {e}")
            except Exception:
                pass

    # ── helpers ──

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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

        # start telegram if token is available but apply_config didn't handle it
        # (env-var-only deployments without setup.json)
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if tg_token:
            start_telegram_gateway(tg_token)
    else:
        print("[gateway] no provider configured. serving setup wizard.")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), GatewayHandler)

    def shutdown(_sig, _frame):
        print("[gateway] shutting down...")
        stop_goose_web()
        if telegram_process and telegram_process.poll() is None:
            telegram_process.terminate()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.serve_forever()
    sys.exit(0)


if __name__ == "__main__":
    main()
