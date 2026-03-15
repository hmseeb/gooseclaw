import pytest, json, hashlib, tempfile, os


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
