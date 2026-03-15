"""HTTP-level tests for setup wizard endpoints."""

import json
import os
import requests


def _setup_auth(gateway_module, password="testpassword"):
    """Write setup.json with PBKDF2 password and provider config."""
    gw = gateway_module
    hashed = gw.hash_token(password)
    setup = {
        "web_auth_token_hash": hashed,
        "setup_complete": True,
        "provider_type": "openai",
    }
    os.makedirs(os.path.dirname(gw.SETUP_FILE), exist_ok=True)
    with open(gw.SETUP_FILE, "w") as f:
        json.dump(setup, f, indent=2)


class TestSetupConfig:
    """GET /api/setup/config tests."""

    def test_get_config_returns_200(self, live_gateway, auth_session):
        resp = requests.get(
            f"{live_gateway}/api/setup/config",
            headers=auth_session,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "configured" in data or "config" in data

    def test_get_config_requires_auth(self, live_gateway, gateway_module):
        _setup_auth(gateway_module)
        resp = requests.get(f"{live_gateway}/api/setup/config")
        assert resp.status_code == 401

    def test_get_config_masks_secrets(self, live_gateway, auth_session, gateway_module):
        # Write setup with an API key
        gw = gateway_module
        setup = json.load(open(gw.SETUP_FILE))
        setup["api_key"] = "sk-secret-key-12345"
        with open(gw.SETUP_FILE, "w") as f:
            json.dump(setup, f, indent=2)

        resp = requests.get(
            f"{live_gateway}/api/setup/config",
            headers=auth_session,
        )
        assert resp.status_code == 200
        data = resp.json()
        config = data.get("config", data)
        # api_key should be redacted
        if "api_key" in config:
            assert config["api_key"] == "***REDACTED***"


class TestSetupStatus:
    """GET /api/setup/status tests."""

    def test_get_status_returns_200(self, live_gateway):
        resp = requests.get(f"{live_gateway}/api/setup/status")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_status_shows_state(self, live_gateway):
        resp = requests.get(f"{live_gateway}/api/setup/status")
        data = resp.json()
        # goosed_startup_state has a "state" field
        assert "state" in data


class TestSetupValidate:
    """POST /api/setup/validate tests."""

    def test_validate_missing_provider(self, live_gateway):
        resp = requests.post(
            f"{live_gateway}/api/setup/validate",
            json={},
        )
        # Should respond (not crash), 200 with validation result or 400/500
        assert resp.status_code in (200, 400, 500)

    def test_validate_returns_json(self, live_gateway):
        resp = requests.post(
            f"{live_gateway}/api/setup/validate",
            json={"provider_type": "openai", "credentials": {"api_key": "sk-test"}},
        )
        assert resp.status_code in (200, 500)
        data = resp.json()
        assert isinstance(data, dict)


class TestSetupSave:
    """POST /api/setup/save tests."""

    def test_save_requires_auth(self, live_gateway, gateway_module):
        _setup_auth(gateway_module)
        resp = requests.post(
            f"{live_gateway}/api/setup/save",
            json={"provider_type": "openai"},
        )
        assert resp.status_code == 401

    def test_save_provider_config(self, live_gateway, auth_session, gateway_module):
        gw = gateway_module
        resp = requests.post(
            f"{live_gateway}/api/setup/save",
            json={
                "provider_type": "openai",
                "api_key": "sk-test-key",
                "web_auth_token": "testpassword",
            },
            headers=auth_session,
        )
        # May be 200 (success) or 400 (validation error depending on required fields)
        assert resp.status_code in (200, 400)
        if resp.status_code == 200:
            data = resp.json()
            assert data.get("success") is True
            # Verify config was persisted
            with open(gw.SETUP_FILE) as f:
                saved = json.load(f)
            assert saved.get("provider_type") == "openai"
