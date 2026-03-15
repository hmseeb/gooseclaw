"""End-to-end integration tests for the GooseClaw Docker container.

These tests build the real Docker image, boot a container, exercise the
setup wizard via HTTP, and verify the health endpoint reports a working
system.  They are the capstone validation for v4.0 production hardening.

Run selectively:  pytest -m e2e -v --timeout=300
"""

import time

import pytest

requests = pytest.importorskip("requests")

pytestmark = pytest.mark.e2e


class TestE2EContainer:
    """Full lifecycle tests against a real GooseClaw Docker container."""

    def test_01_container_boots_and_health_returns_200(self, docker_container):
        """Container builds, boots, and health endpoint responds."""
        resp = requests.get(f"{docker_container['base_url']}/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "gooseclaw"

    def test_02_setup_wizard_saves_config(self, docker_container):
        """Setup wizard accepts provider config and password via HTTP POST."""
        resp = requests.post(
            f"{docker_container['base_url']}/api/setup/save",
            json={
                "provider_type": "openai",
                "api_key": "sk-fake-test-key-not-real",
                "web_auth_token": "e2e-test-password-123",
            },
            timeout=15,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True

    def test_03_can_login_after_setup(self, docker_container):
        """Auth login works with the password set during setup."""
        # First complete setup so there is a password to login with
        requests.post(
            f"{docker_container['base_url']}/api/setup/save",
            json={
                "provider_type": "openai",
                "api_key": "sk-fake-test-key-not-real",
                "web_auth_token": "e2e-test-password-123",
            },
            timeout=15,
        )

        resp = requests.post(
            f"{docker_container['base_url']}/api/auth/login",
            json={"password": "e2e-test-password-123"},
            timeout=10,
        )
        assert resp.status_code == 200
        # Session cookie should be set
        assert "Set-Cookie" in resp.headers or "set-cookie" in resp.headers

    def test_04_health_shows_goosed_status_after_setup(self, docker_container):
        """After setup, health endpoint includes goosed lifecycle field.

        With a fake API key goosed may fail to fully start.  That is OK.
        The test proves the system TRIED to start goosed after setup
        completed.  The ``goosed`` key must exist in the health JSON
        regardless of its value.
        """
        # Complete setup to trigger goosed lifecycle
        requests.post(
            f"{docker_container['base_url']}/api/setup/save",
            json={
                "provider_type": "openai",
                "api_key": "sk-fake-test-key-not-real",
                "web_auth_token": "e2e-test-password-123",
            },
            timeout=15,
        )

        # Poll health for up to 30 seconds waiting for goosed field
        deadline = time.time() + 30
        goosed_found = False
        last_data = {}

        while time.time() < deadline:
            resp = requests.get(
                f"{docker_container['base_url']}/api/health",
                timeout=5,
            )
            if resp.status_code == 200:
                last_data = resp.json()
                if "goosed" in last_data:
                    goosed_found = True
                    break
            time.sleep(3)

        assert goosed_found, (
            f"Health endpoint never included 'goosed' key within 30s. "
            f"Last response: {last_data}"
        )
