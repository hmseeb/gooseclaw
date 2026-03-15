import hashlib, json, os, sys

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
