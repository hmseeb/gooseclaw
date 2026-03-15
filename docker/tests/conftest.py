import pytest, json, hashlib, os, sys, threading, time, tempfile

# ── existing source-inspection fixtures (Phase 18 compat) ────────────────────


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temp data dir with config/setup.json containing a SHA-256 hash."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    sha256_hash = hashlib.sha256(b"testpassword").hexdigest()
    setup = {
        "web_auth_token_hash": sha256_hash,
        "setup_complete": True,
    }
    setup_path = config_dir / "setup.json"
    setup_path.write_text(json.dumps(setup, indent=2))
    return tmp_path


@pytest.fixture
def gateway_source():
    """Return gateway.py source code as string for inspection tests."""
    gateway_path = os.path.join(os.path.dirname(__file__), "..", "gateway.py")
    with open(gateway_path) as f:
        return f.read()


@pytest.fixture
def entrypoint_source():
    """Return entrypoint.sh source code as string for inspection tests."""
    ep_path = os.path.join(os.path.dirname(__file__), "..", "entrypoint.sh")
    with open(ep_path) as f:
        return f.read()


@pytest.fixture
def secret_sh_source():
    """Return secret.sh source code as string for inspection tests."""
    ss_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "secret.sh")
    with open(ss_path) as f:
        return f.read()


# ── HTTP-level test fixtures ─────────────────────────────────────────────────


@pytest.fixture(scope="session")
def gateway_module():
    """Import gateway module with controlled env vars. Session-scoped to avoid re-import."""
    # Set env vars BEFORE importing gateway (module-level side effects)
    tmp = tempfile.mkdtemp(prefix="gw_test_")
    os.environ["DATA_DIR"] = tmp
    os.environ["PORT"] = "0"  # will be overridden per-server
    os.environ["APP_DIR"] = os.path.join(os.path.dirname(__file__), "..")

    # Add docker/ to sys.path so gateway can be imported
    docker_dir = os.path.join(os.path.dirname(__file__), "..")
    if docker_dir not in sys.path:
        sys.path.insert(0, docker_dir)

    import gateway
    return gateway


@pytest.fixture(scope="module")
def live_gateway(gateway_module, tmp_path_factory):
    """Start a real GatewayHandler on a random port. Returns base_url string."""
    from http.server import ThreadingHTTPServer

    gw = gateway_module

    # Create isolated data directories for this test module
    tmp = tmp_path_factory.mktemp("gateway_data")
    data_dir = str(tmp)
    config_dir = os.path.join(data_dir, "config")
    secrets_dir = os.path.join(data_dir, "secrets")
    sessions_dir = os.path.join(data_dir, "sessions")
    channels_dir = os.path.join(data_dir, "channels")

    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(secrets_dir, mode=0o700, exist_ok=True)
    os.makedirs(sessions_dir, exist_ok=True)
    os.makedirs(channels_dir, exist_ok=True)

    # Create empty vault.yaml
    vault_path = os.path.join(secrets_dir, "vault.yaml")
    with open(vault_path, "w") as f:
        f.write("")

    # Save and patch module globals
    orig_data_dir = gw.DATA_DIR
    orig_config_dir = gw.CONFIG_DIR
    orig_setup_file = gw.SETUP_FILE

    gw.DATA_DIR = data_dir
    gw.CONFIG_DIR = config_dir
    gw.SETUP_FILE = os.path.join(config_dir, "setup.json")

    # Start server on random port
    server = ThreadingHTTPServer(("127.0.0.1", 0), gw.GatewayHandler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Wait for server to be ready
    import urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{base_url}/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.05)

    yield base_url

    # Teardown
    server.shutdown()
    gw.DATA_DIR = orig_data_dir
    gw.CONFIG_DIR = orig_config_dir
    gw.SETUP_FILE = orig_setup_file


@pytest.fixture
def auth_session(live_gateway, gateway_module):
    """Set up a password and login, returning dict with Cookie header for authenticated requests."""
    import requests as req

    gw = gateway_module

    # Write setup.json with PBKDF2 hashed password
    password = "testpassword"
    hashed = gw.hash_token(password)
    setup = {
        "web_auth_token_hash": hashed,
        "setup_complete": True,
        "provider_type": "openai",
    }
    setup_path = gw.SETUP_FILE
    os.makedirs(os.path.dirname(setup_path), exist_ok=True)
    with open(setup_path, "w") as f:
        json.dump(setup, f, indent=2)

    # Login to get session cookie
    resp = req.post(
        f"{live_gateway}/api/auth/login",
        json={"password": password},
    )
    assert resp.status_code == 200, f"Auth login failed: {resp.status_code} {resp.text}"

    cookie = resp.headers.get("Set-Cookie", "")
    # Extract just the cookie value for the Cookie header
    cookie_parts = cookie.split(";")[0] if cookie else ""
    return {"Cookie": cookie_parts}


@pytest.fixture(autouse=True)
def reset_rate_limiters(gateway_module):
    """Clear rate limiter state before each test."""
    gw = gateway_module
    if hasattr(gw, "auth_limiter"):
        gw.auth_limiter._requests.clear()
    if hasattr(gw, "api_limiter"):
        gw.api_limiter._requests.clear()
    if hasattr(gw, "notify_limiter"):
        gw.notify_limiter._requests.clear()
