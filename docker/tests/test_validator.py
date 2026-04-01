"""Unit tests for the extension validator module."""

import json
import os
import sys
import time

import pytest

# Ensure docker/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import extensions.registry as registry
import extensions.validator as validator


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    """Set up isolated registry path for tests."""
    registry_path = str(tmp_path / "registry.json")
    monkeypatch.setattr(registry, "REGISTRY_PATH", registry_path)
    return registry_path


class TestValidateSyntax:
    def test_valid_python_passes(self, tmp_path):
        """Valid Python file passes syntax check."""
        py_file = tmp_path / "good.py"
        py_file.write_text("def hello():\n    return 'world'\n")

        ok, msg = validator.validate_syntax(str(py_file))
        assert ok is True
        assert msg == ""

    def test_syntax_error_fails(self, tmp_path):
        """File with syntax error fails validation."""
        py_file = tmp_path / "bad.py"
        py_file.write_text("def broken(\n")

        ok, msg = validator.validate_syntax(str(py_file))
        assert ok is False
        assert "SyntaxError" in msg

    def test_missing_file_fails(self):
        """Nonexistent file fails validation."""
        ok, msg = validator.validate_syntax("/nonexistent/path.py")
        assert ok is False
        assert "not found" in msg.lower() or "No such file" in msg


class TestHealthCheck:
    def test_healthy_mcp_server(self, tmp_path):
        """A minimal MCP server that responds to initialize passes health check."""
        server_script = tmp_path / "healthy_server.py"
        server_script.write_text(
            'import sys, json\n'
            'line = sys.stdin.readline()\n'
            'req = json.loads(line)\n'
            'resp = {"jsonrpc": "2.0", "id": req["id"], "result": {'
            '"protocolVersion": "2024-11-05", "capabilities": {}, '
            '"serverInfo": {"name": "test", "version": "1.0"}}}\n'
            'sys.stdout.write(json.dumps(resp) + "\\n")\n'
            'sys.stdout.flush()\n'
        )

        ok, msg = validator.health_check(str(server_script), timeout=10)
        assert ok is True
        assert msg == ""

    def test_crashing_server_fails(self, tmp_path):
        """A server that exits immediately fails health check."""
        server_script = tmp_path / "crash_server.py"
        server_script.write_text("import sys; sys.exit(1)\n")

        ok, msg = validator.health_check(str(server_script), timeout=5)
        assert ok is False
        assert msg != ""

    def test_timeout_server_fails(self, tmp_path):
        """A server that hangs fails with timeout."""
        server_script = tmp_path / "slow_server.py"
        server_script.write_text("import time; time.sleep(999)\n")

        ok, msg = validator.health_check(str(server_script), timeout=2)
        assert ok is False
        assert "timeout" in msg.lower() or "Timeout" in msg


class TestFailureTracking:
    def test_record_failure_increments(self, registry_env):
        """record_failure increments consecutive_failures each call."""
        registry.register(
            name="test_ext",
            template="rest_api",
            vault_prefix="test",
            vault_keys=["test.key"],
            server_path="/data/extensions/test_ext/server.py",
        )

        count1 = validator.record_failure("test_ext")
        count2 = validator.record_failure("test_ext")
        count3 = validator.record_failure("test_ext")

        assert count1 == 1
        assert count2 == 2
        assert count3 == 3

    def test_clear_failures_resets(self, registry_env):
        """clear_failures resets counter, next record_failure starts at 1."""
        registry.register(
            name="test_ext",
            template="rest_api",
            vault_prefix="test",
            vault_keys=["test.key"],
            server_path="/data/extensions/test_ext/server.py",
        )

        validator.record_failure("test_ext")
        validator.record_failure("test_ext")
        validator.clear_failures("test_ext")

        count = validator.record_failure("test_ext")
        assert count == 1

    def test_check_and_disable_at_threshold(self, registry_env):
        """check_and_disable disables extension at max_failures threshold."""
        registry.register(
            name="test_ext",
            template="rest_api",
            vault_prefix="test",
            vault_keys=["test.key"],
            server_path="/data/extensions/test_ext/server.py",
        )

        # First two failures: below threshold
        disabled1 = validator.check_and_disable("test_ext", max_failures=3)
        disabled2 = validator.check_and_disable("test_ext", max_failures=3)
        assert disabled1 is False
        assert disabled2 is False

        # Third failure: at threshold, should disable
        disabled3 = validator.check_and_disable("test_ext", max_failures=3)
        assert disabled3 is True

        # Verify extension is disabled in registry
        exts = registry.list_extensions()
        assert exts["test_ext"]["enabled"] is False

    def test_check_and_disable_nonexistent(self, registry_env):
        """check_and_disable on nonexistent extension does not raise."""
        result = validator.check_and_disable("nonexistent")
        assert result is False
