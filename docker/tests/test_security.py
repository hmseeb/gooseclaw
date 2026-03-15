import os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSecretShInjection:
    """SEC-01: secret.sh uses os.environ, not string interpolation."""

    def test_secret_sh_no_vault_file_interpolation(self, secret_sh_source):
        """No '$VAULT_FILE' inside python3 -c strings."""
        # Find all python3 -c blocks and check for $VAULT_FILE interpolation
        assert "'$VAULT_FILE'" not in secret_sh_source, \
            "secret.sh must not interpolate $VAULT_FILE into python3 -c strings"

    def test_secret_sh_no_dotpath_interpolation(self, secret_sh_source):
        """No '$DOTPATH' inside python3 -c strings."""
        assert "'$DOTPATH'" not in secret_sh_source, \
            "secret.sh must not interpolate $DOTPATH into python3 -c strings"

    def test_secret_sh_no_value_interpolation(self, secret_sh_source):
        """No '''$VALUE''' inside python3 -c strings."""
        assert "'''$VALUE'''" not in secret_sh_source and "'$VALUE'" not in secret_sh_source, \
            "secret.sh must not interpolate $VALUE into python3 -c strings"

    def test_secret_sh_uses_os_environ(self, secret_sh_source):
        """All python3 -c blocks must use os.environ for data access."""
        assert "os.environ" in secret_sh_source, \
            "secret.sh must use os.environ to pass data to inline Python"


class TestEntrypointInjection:
    """SEC-02: entrypoint.sh password reset uses os.environ."""

    def test_entrypoint_no_password_interpolation(self, entrypoint_source):
        """No '$GOOSECLAW_RESET_PASSWORD' inside python3 -c strings."""
        assert "'$GOOSECLAW_RESET_PASSWORD'" not in entrypoint_source, \
            "entrypoint.sh must not interpolate password into python3 -c strings"

    def test_entrypoint_no_data_dir_interpolation(self, entrypoint_source):
        """No '$DATA_DIR' inside python3 -c strings (in quotes context)."""
        assert "'$DATA_DIR'" not in entrypoint_source, \
            "entrypoint.sh must not interpolate $DATA_DIR into python3 -c strings"

    def test_entrypoint_uses_os_environ(self, entrypoint_source):
        """Password reset block must use os.environ."""
        assert "os.environ" in entrypoint_source, \
            "entrypoint.sh must use os.environ for data passing"


class TestRunScriptInjection:
    """SEC-03: gateway.py _run_script uses list args, not shell=True."""

    def test_run_script_no_shell_true(self, gateway_source):
        """_run_script must not use shell=True."""
        # Find the _run_script function and check it doesn't use shell=True
        run_script_match = re.search(
            r'def _run_script\(.*?\n(.*?)(?=\ndef |\nclass |\Z)',
            gateway_source, re.DOTALL
        )
        assert run_script_match, "_run_script function must exist"
        func_body = run_script_match.group(1)
        assert "shell=True" not in func_body, \
            "_run_script must not use shell=True"

    def test_run_script_uses_explicit_shell(self, gateway_source):
        """_run_script must use ['/bin/sh', '-c', command] pattern."""
        assert '"/bin/sh"' in gateway_source or "'/bin/sh'" in gateway_source, \
            "_run_script must use explicit /bin/sh invocation"


class TestRecoverySecretLeak:
    """SEC-06: Recovery secret not printed to container stdout."""

    def test_recovery_secret_not_leaked(self, entrypoint_source):
        """entrypoint.sh must not echo the recovery secret value."""
        # Check no line echoes the secret variable
        for line in entrypoint_source.splitlines():
            if "echo" in line and "$RECOVERY_SECRET" in line:
                # Allow "echo ... saved to file" but not "echo ... =$RECOVERY_SECRET"
                assert "GOOSECLAW_RECOVERY_SECRET=$RECOVERY_SECRET" not in line, \
                    f"Line leaks recovery secret to stdout: {line.strip()}"


class TestBodySizeLimit:
    """SEC-07: _read_body rejects >1MB with 413."""

    def test_max_body_size_defined(self, gateway_source):
        """MAX_BODY_SIZE constant must exist."""
        assert "MAX_BODY_SIZE" in gateway_source, \
            "gateway.py must define MAX_BODY_SIZE constant"

    def test_body_size_limit(self, gateway_source):
        """_read_body must check Content-Length against MAX_BODY_SIZE."""
        assert "MAX_BODY_SIZE" in gateway_source and "413" in gateway_source, \
            "_read_body must reject oversized requests with 413"

    def test_read_body_returns_none_on_oversize(self, gateway_source):
        """_read_body must return None when body exceeds limit."""
        assert "body is None" in gateway_source, \
            "Call sites must handle None return from _read_body (413 already sent)"


class TestSecurityHeaders:
    """HARD-04: All required security headers present."""

    def test_security_headers_complete(self, gateway_source):
        """SECURITY_HEADERS must include Cross-Origin-Opener-Policy."""
        assert "Cross-Origin-Opener-Policy" in gateway_source, \
            "SECURITY_HEADERS must include Cross-Origin-Opener-Policy"
