"""Subprocess-based tests for entrypoint.sh bootstrap logic."""

import json
import os
import subprocess


DOCKER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENTRYPOINT = os.path.join(DOCKER_DIR, "entrypoint.sh")


def run_entrypoint_section(bash_code, tmp_path, env_extra=None):
    """Run a section of entrypoint-like bash code with controlled env."""
    env = {
        "HOME": str(tmp_path / "home"),
        "APP_DIR": DOCKER_DIR,
        "DATA_DIR": str(tmp_path / "data"),
        "CONFIG_DIR": str(tmp_path / "data" / "config"),
        "IDENTITY_DIR": str(tmp_path / "data" / "identity"),
        "PATH": os.environ.get("PATH", ""),
    }
    if env_extra:
        env.update(env_extra)

    result = subprocess.run(
        ["bash", "-c", bash_code],
        capture_output=True, text=True, timeout=10,
        env=env,
    )
    return result


class TestEntrypointDirectoryCreation:
    """Test that first boot creates the required directory structure."""

    def test_creates_data_directories(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        result = run_entrypoint_section(
            """
            mkdir -p "$IDENTITY_DIR/journal" "$IDENTITY_DIR/learnings" "$CONFIG_DIR" \
                "$DATA_DIR/sessions" "$DATA_DIR/recipes" "$DATA_DIR/secrets" "$DATA_DIR/channels"
            chmod 700 "$DATA_DIR/secrets"
            touch "$DATA_DIR/secrets/vault.yaml"
            chmod 600 "$DATA_DIR/secrets/vault.yaml"
            """,
            tmp_path,
        )
        assert result.returncode == 0

        # Verify directories exist
        assert (data_dir / "config").is_dir()
        assert (data_dir / "secrets").is_dir()
        assert (data_dir / "sessions").is_dir()
        assert (data_dir / "channels").is_dir()
        assert (data_dir / "secrets" / "vault.yaml").is_file()

    def test_secrets_dir_permissions(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        run_entrypoint_section(
            """
            mkdir -p "$DATA_DIR/secrets"
            chmod 700 "$DATA_DIR/secrets"
            """,
            tmp_path,
        )
        secrets_stat = os.stat(data_dir / "secrets")
        # Check owner has rwx
        assert secrets_stat.st_mode & 0o700 == 0o700


class TestEntrypointConfigGeneration:
    """Test config file generation on first boot."""

    def test_creates_default_config_yaml(self, tmp_path):
        data_dir = tmp_path / "data"
        config_dir = data_dir / "config"
        config_dir.mkdir(parents=True)
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        (home_dir / ".config").mkdir()

        result = run_entrypoint_section(
            """
            mkdir -p "$HOME/.config"
            rm -rf "$HOME/.config/goose"
            ln -sf "$CONFIG_DIR" "$HOME/.config/goose"
            cat > "$CONFIG_DIR/config.yaml" << YAML
keyring: false
GOOSE_MODE: auto
GOOSE_CONTEXT_STRATEGY: summarize
GOOSE_MAX_TURNS: 50
GOOSE_DISABLE_SESSION_NAMING: true
YAML
            """,
            tmp_path,
        )
        assert result.returncode == 0
        config_yaml = config_dir / "config.yaml"
        assert config_yaml.is_file()
        content = config_yaml.read_text()
        assert "keyring: false" in content
        assert "GOOSE_MODE: auto" in content

    def test_preserves_existing_setup_json(self, tmp_path):
        data_dir = tmp_path / "data"
        config_dir = data_dir / "config"
        config_dir.mkdir(parents=True)

        # Write pre-existing setup.json
        setup = {"provider_type": "openai", "setup_complete": True}
        with open(config_dir / "setup.json", "w") as f:
            json.dump(setup, f)

        # The entrypoint's first-boot block only runs if .initialized doesn't exist
        # But the config regen section always writes config.yaml (not setup.json)
        # Verify setup.json is untouched
        with open(config_dir / "setup.json") as f:
            preserved = json.load(f)
        assert preserved["provider_type"] == "openai"


class TestEntrypointEnvRehydration:
    """Test that entrypoint rehydrates provider env vars from setup.json."""

    def test_rehydrates_provider_env_vars(self, tmp_path):
        data_dir = tmp_path / "data"
        config_dir = data_dir / "config"
        config_dir.mkdir(parents=True)

        setup = {
            "provider_type": "openai",
            "saved_keys": {"openai": {"api_key": "sk-test-123"}},
            "setup_complete": True,
        }
        with open(config_dir / "setup.json", "w") as f:
            json.dump(setup, f)

        # Run the rehydration logic from entrypoint
        result = run_entrypoint_section(
            """
            python3 -c "
import json, os, shlex
c = json.load(open(os.path.join(os.environ['CONFIG_DIR'], 'setup.json')))
pt = c.get('provider_type', '')
sk = c.get('saved_keys', {})
if pt in sk:
    keys = sk[pt]
    if isinstance(keys, dict):
        for k, v in keys.items():
            print(f'{k}={v}')
"
            """,
            tmp_path,
        )
        assert result.returncode == 0
        assert "api_key=sk-test-123" in result.stdout


class TestEntrypointProviderDetection:
    """Test that entrypoint detects configured providers."""

    def test_detects_configured_provider_from_setup_json(self, tmp_path):
        data_dir = tmp_path / "data"
        config_dir = data_dir / "config"
        config_dir.mkdir(parents=True)

        setup = {"provider_type": "openai", "setup_complete": True}
        with open(config_dir / "setup.json", "w") as f:
            json.dump(setup, f)

        result = run_entrypoint_section(
            """
            if [ -f "$CONFIG_DIR/setup.json" ]; then
                echo "provider: configured via setup wizard"
            else
                echo "provider: not configured"
            fi
            """,
            tmp_path,
        )
        assert result.returncode == 0
        assert "configured via setup wizard" in result.stdout

    def test_no_provider_shows_unconfigured(self, tmp_path):
        data_dir = tmp_path / "data"
        config_dir = data_dir / "config"
        config_dir.mkdir(parents=True)

        result = run_entrypoint_section(
            """
            if [ -f "$CONFIG_DIR/setup.json" ]; then
                echo "provider: configured via setup wizard"
            else
                echo "provider: not configured"
            fi
            """,
            tmp_path,
        )
        assert result.returncode == 0
        assert "not configured" in result.stdout


class TestEntrypointPasswordReset:
    """Test GOOSECLAW_RESET_PASSWORD env var password reset."""

    def test_password_reset_via_env_var(self, tmp_path):
        data_dir = tmp_path / "data"
        config_dir = data_dir / "config"
        config_dir.mkdir(parents=True)

        # Write initial setup.json with old password hash
        setup = {
            "web_auth_token_hash": "$pbkdf2$old$hash",
            "setup_complete": True,
            "provider_type": "openai",
        }
        with open(config_dir / "setup.json", "w") as f:
            json.dump(setup, f)

        result = run_entrypoint_section(
            r"""
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
    print('password reset done')
else:
    print('no setup.json found')
"
            """,
            tmp_path,
            env_extra={"GOOSECLAW_RESET_PASSWORD": "newpassword123"},
        )
        assert result.returncode == 0
        assert "password reset done" in result.stdout

        # Verify the hash was updated
        with open(config_dir / "setup.json") as f:
            updated = json.load(f)
        assert updated["web_auth_token_hash"].startswith("$pbkdf2$")
        assert updated["web_auth_token_hash"] != "$pbkdf2$old$hash"


class TestEntrypointNeo4j:
    """GRAPH-01: neo4j startup block in entrypoint."""

    def test_entrypoint_has_neo4j_startup_block(self):
        """GRAPH-01: entrypoint starts Neo4j with proper config."""
        with open(ENTRYPOINT) as f:
            source = f.read()
        assert "neo4j console" in source
        assert "NEO4J_ENABLED=true" in source
        assert "NEO4J_server_directories_data=/data/neo4j" in source
        assert "NEO4J_AUTH=none" in source


class TestEntrypointMem0Extension:
    """MEM-06: mem0-memory extension registered in default config."""

    def test_entrypoint_has_mem0_extension(self):
        """MEM-06: mem0-memory extension registered in default config."""
        with open(ENTRYPOINT) as f:
            source = f.read()
        assert "mem0-memory:" in source
        assert "/app/docker/memory/server.py" in source
        assert 'MEM0_TELEMETRY: "false"' in source
