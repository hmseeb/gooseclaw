#!/usr/bin/env python3
"""
gooseclaw gateway — setup wizard + reverse proxy to goose web.

Runs on $PORT. Serves /setup directly, proxies everything else to goose web
on an internal port. Manages the goose web subprocess lifecycle.
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

def validate_provider(provider_type, credentials):
    """Test if credentials work by making a minimal API call."""
    try:
        if provider_type == "anthropic":
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps({
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}]
                }).encode(),
                headers={
                    "x-api-key": credentials.get("api_key", ""),
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            urllib.request.urlopen(req, timeout=15)
            return {"valid": True}

        elif provider_type == "openai":
            req = urllib.request.Request(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {credentials.get('api_key', '')}"},
            )
            urllib.request.urlopen(req, timeout=15)
            return {"valid": True}

        elif provider_type == "google":
            key = credentials.get("api_key", "")
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
            )
            urllib.request.urlopen(req, timeout=15)
            return {"valid": True}

        elif provider_type == "groq":
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {credentials.get('api_key', '')}"},
            )
            urllib.request.urlopen(req, timeout=15)
            return {"valid": True}

        elif provider_type == "openrouter":
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {credentials.get('api_key', '')}"},
            )
            urllib.request.urlopen(req, timeout=15)
            return {"valid": True}

        elif provider_type == "custom":
            url = credentials.get("url", "").rstrip("/") + "/models"
            headers = {}
            if credentials.get("api_key"):
                headers["Authorization"] = f"Bearer {credentials['api_key']}"
            req = urllib.request.Request(url, headers=headers)
            urllib.request.urlopen(req, timeout=15)
            return {"valid": True}

        elif provider_type == "claude-code":
            return {"valid": True, "note": "Claude setup tokens cannot be validated remotely. Save and test."}

        return {"valid": True, "note": "Validation not available for this provider."}

    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"valid": False, "error": "Invalid or unauthorized API key."}
        return {"valid": True, "note": f"Got HTTP {e.code} but key format looks correct."}
    except Exception as e:
        return {"valid": False, "error": str(e)}


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
    elif provider_type in ("anthropic", "openai", "google", "groq", "openrouter"):
        env_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "google": "GOOGLE_API_KEY",
            "groq": "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        os.environ[env_map[provider_type]] = api_key
        lines.append(f"GOOSE_PROVIDER: {provider_type}")

    # default models per provider if none specified
    if not model:
        default_models = {
            "claude-code": "default",
            "anthropic": "claude-sonnet-4-20250514",
            "openrouter": "anthropic/claude-sonnet-4-20250514",
            "openai": "gpt-4o",
            "google": "gemini-2.0-flash",
            "groq": "llama-3.3-70b-versatile",
        }
        model = default_models.get(provider_type, "")

    if model:
        lines.append(f"GOOSE_MODEL: {model}")

    with open(config_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    # telegram
    tg_token = config.get("telegram_bot_token", "")
    if tg_token:
        os.environ["TELEGRAM_BOT_TOKEN"] = tg_token


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
            # mask secrets
            for key in ("api_key", "claude_setup_token", "custom_key", "web_auth_token"):
                val = safe.get(key, "")
                if val and len(val) > 12:
                    safe[key] = val[:6] + "..." + val[-4:]
                elif val:
                    safe[key] = "***"
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
            result = validate_provider(data.get("provider_type", ""), data)
            self.send_json(200, result)
        except Exception as e:
            self.send_json(500, {"valid": False, "error": str(e)})

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
    else:
        print("[gateway] no provider configured. serving setup wizard.")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), GatewayHandler)

    def shutdown(_sig, _frame):
        print("[gateway] shutting down...")
        stop_goose_web()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.serve_forever()
    sys.exit(0)


if __name__ == "__main__":
    main()
