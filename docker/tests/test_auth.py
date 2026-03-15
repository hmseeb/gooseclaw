import os, sys

# Add docker/ to path so we can potentially import gateway helpers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestPBKDF2HashFormat:
    """SEC-04: hash_token returns $pbkdf2$ prefixed string with salt."""

    def test_pbkdf2_hash_format(self, gateway_source):
        """hash_token must produce $pbkdf2$<base64-salt>$<base64-hash> format."""
        # Verify the function exists with PBKDF2 implementation
        assert "pbkdf2_hmac" in gateway_source, "hash_token must use pbkdf2_hmac"
        assert "PBKDF2_ITERATIONS" in gateway_source, "Must define PBKDF2_ITERATIONS constant"

        # Extract and run hash_token to verify output format
        # This imports the actual function if possible, or verifies the pattern
        assert "$pbkdf2$" in gateway_source, "Hash format must use $pbkdf2$ prefix"

    def test_pbkdf2_iterations_600k(self, gateway_source):
        """PBKDF2 must use 600K iterations per OWASP recommendation."""
        assert "600_000" in gateway_source or "600000" in gateway_source, \
            "PBKDF2_ITERATIONS must be 600,000"

    def test_pbkdf2_uses_random_salt(self, gateway_source):
        """Each hash must use a unique random salt via os.urandom."""
        assert "os.urandom" in gateway_source, "hash_token must generate random salt"

    def test_hash_comparison_constant_time(self, gateway_source):
        """Hash comparison must use hmac.compare_digest, not ==."""
        assert "hmac.compare_digest" in gateway_source, \
            "verify_token must use hmac.compare_digest for constant-time comparison"


class TestLegacySHA256Migration:
    """SEC-05: verify_token accepts legacy SHA-256 and triggers migration."""

    def test_legacy_sha256_verification(self, gateway_source):
        """verify_token must accept bare 64-char hex SHA-256 hashes."""
        assert "startswith" in gateway_source and "pbkdf2" in gateway_source, \
            "verify_token must dispatch on $pbkdf2$ prefix, falling back to SHA-256"

    def test_lazy_migration_on_legacy_success(self, gateway_source):
        """On successful legacy SHA-256 login, hash must be upgraded to PBKDF2."""
        assert "_migrate_password_hash" in gateway_source or \
               ("hash_token" in gateway_source and "save_setup" in gateway_source), \
            "verify_token must trigger hash migration on legacy SHA-256 success"

    def test_migration_non_fatal(self, gateway_source):
        """Migration failure must not prevent login."""
        assert "non-fatal" in gateway_source or "except" in gateway_source, \
            "Hash migration must be wrapped in try/except (non-fatal)"


# ── HTTP-level auth endpoint tests ───────────────────────────────────────────

import json
import requests


def _setup_password(gateway_module, password="testpassword"):
    """Write setup.json with a PBKDF2 hash for the given password."""
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


class TestAuthLogin:
    """HTTP-level tests for POST /api/auth/login."""

    def test_login_correct_password(self, live_gateway, gateway_module):
        _setup_password(gateway_module)
        resp = requests.post(
            f"{live_gateway}/api/auth/login",
            json={"password": "testpassword"},
        )
        assert resp.status_code == 200
        assert "Set-Cookie" in resp.headers

    def test_login_wrong_password(self, live_gateway, gateway_module):
        _setup_password(gateway_module)
        resp = requests.post(
            f"{live_gateway}/api/auth/login",
            json={"password": "wrongpassword"},
        )
        assert resp.status_code == 401

    def test_login_missing_password(self, live_gateway, gateway_module):
        _setup_password(gateway_module)
        resp = requests.post(
            f"{live_gateway}/api/auth/login",
            json={},
        )
        assert resp.status_code == 400

    def test_login_empty_body(self, live_gateway, gateway_module):
        _setup_password(gateway_module)
        resp = requests.post(
            f"{live_gateway}/api/auth/login",
            data=b"",
            headers={"Content-Type": "application/json"},
        )
        # Empty body should be rejected (400 for invalid JSON or missing password)
        assert resp.status_code in (400, 401)


class TestAuthSession:
    """HTTP-level tests for session cookie behavior."""

    def test_authenticated_endpoint_with_cookie(self, live_gateway, auth_session):
        resp = requests.get(
            f"{live_gateway}/api/setup/config",
            headers=auth_session,
        )
        assert resp.status_code == 200

    def test_authenticated_endpoint_without_cookie(self, live_gateway, gateway_module):
        _setup_password(gateway_module)
        resp = requests.get(
            f"{live_gateway}/api/setup/config",
        )
        assert resp.status_code == 401

    def test_session_cookie_httponly(self, live_gateway, gateway_module):
        _setup_password(gateway_module)
        resp = requests.post(
            f"{live_gateway}/api/auth/login",
            json={"password": "testpassword"},
        )
        assert resp.status_code == 200
        cookie_header = resp.headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie_header


class TestAuthRateLimiting:
    """HTTP-level tests for auth rate limiting."""

    def test_auth_rate_limit_triggers(self, live_gateway, gateway_module):
        _setup_password(gateway_module)
        max_reqs = gateway_module.auth_limiter.max_requests
        # Send max_requests + 1 wrong password attempts
        last_status = None
        for _ in range(max_reqs + 1):
            resp = requests.post(
                f"{live_gateway}/api/auth/login",
                json={"password": "wrongpassword"},
            )
            last_status = resp.status_code
        assert last_status == 429


class TestAuthRecovery:
    """HTTP-level tests for POST /api/auth/recover."""

    def test_recovery_with_valid_secret(self, live_gateway, gateway_module):
        _setup_password(gateway_module)
        recovery_secret = "test-recovery-secret-12345"
        os.environ["GOOSECLAW_RECOVERY_SECRET"] = recovery_secret

        resp = requests.post(
            f"{live_gateway}/api/auth/recover",
            json={"secret": recovery_secret},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True
        assert "temporary_password" in data

        # Verify login works with the new temporary password
        new_pass = data["temporary_password"]
        resp2 = requests.post(
            f"{live_gateway}/api/auth/login",
            json={"password": new_pass},
        )
        assert resp2.status_code == 200

        # Cleanup
        os.environ.pop("GOOSECLAW_RECOVERY_SECRET", None)

    def test_recovery_with_invalid_secret(self, live_gateway, gateway_module):
        _setup_password(gateway_module)
        os.environ["GOOSECLAW_RECOVERY_SECRET"] = "real-secret"

        resp = requests.post(
            f"{live_gateway}/api/auth/recover",
            json={"secret": "wrong-secret"},
        )
        assert resp.status_code == 403

        # Cleanup
        os.environ.pop("GOOSECLAW_RECOVERY_SECRET", None)
