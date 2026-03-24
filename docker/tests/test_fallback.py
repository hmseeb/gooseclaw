"""Tests for the fallback provider system (Phase 26)."""

import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Ensure docker/ is on sys.path for gateway imports
docker_dir = os.path.join(os.path.dirname(__file__), "..")
if docker_dir not in sys.path:
    sys.path.insert(0, docker_dir)


class TestErrorClassification:
    """Test _is_retriable_provider_error correctly classifies errors."""

    def test_retriable_429_rate_limit(self, gateway_module):
        assert gateway_module._is_retriable_provider_error("429 rate limit exceeded") is True

    def test_retriable_500_server_error(self, gateway_module):
        for code in ("500", "502", "503", "504", "529"):
            assert gateway_module._is_retriable_provider_error(f"HTTP {code} server error") is True

    def test_retriable_timeout(self, gateway_module):
        for msg in ("connection timeout", "request timed out", "took too long to respond"):
            assert gateway_module._is_retriable_provider_error(msg) is True

    def test_retriable_connection_error(self, gateway_module):
        for msg in ("connection refused", "connection reset by peer", "connection error occurred"):
            assert gateway_module._is_retriable_provider_error(msg) is True

    def test_not_retriable_auth_401(self, gateway_module):
        assert gateway_module._is_retriable_provider_error("401 Unauthorized") is False

    def test_not_retriable_forbidden_403(self, gateway_module):
        assert gateway_module._is_retriable_provider_error("403 Forbidden") is False

    def test_not_retriable_bad_request_400(self, gateway_module):
        assert gateway_module._is_retriable_provider_error("400 bad request") is False

    def test_not_retriable_empty_string(self, gateway_module):
        assert gateway_module._is_retriable_provider_error("") is False

    def test_not_retriable_none(self, gateway_module):
        assert gateway_module._is_retriable_provider_error(None) is False

    def test_not_retriable_broken_pipe(self, gateway_module):
        # broken pipe is handled by _is_fatal_provider_error, not retriable
        assert gateway_module._is_retriable_provider_error("broken pipe") is False


class TestFallbackValidation:
    """Test validate_setup_config handles fallback_providers correctly."""

    def test_valid_fallback_providers(self, gateway_module):
        config = {
            "provider_type": "openai",
            "api_key": "sk-test",
            "fallback_providers": [
                {"provider": "anthropic", "model": "claude-opus-4-6"},
                {"provider": "groq", "model": "llama-3.3-70b-versatile"},
            ],
        }
        valid, errors = gateway_module.validate_setup_config(config)
        fallback_errors = [e for e in errors if "fallback" in e.lower()]
        assert len(fallback_errors) == 0

    def test_fallback_providers_not_array(self, gateway_module):
        config = {
            "provider_type": "openai",
            "api_key": "sk-test",
            "fallback_providers": "not an array",
        }
        valid, errors = gateway_module.validate_setup_config(config)
        assert any("fallback_providers must be an array" in e for e in errors)

    def test_fallback_provider_missing_provider(self, gateway_module):
        config = {
            "provider_type": "openai",
            "api_key": "sk-test",
            "fallback_providers": [{"model": "gpt-4o"}],
        }
        valid, errors = gateway_module.validate_setup_config(config)
        assert any("missing" in e.lower() and "provider" in e.lower() for e in errors)

    def test_fallback_provider_unknown_provider(self, gateway_module):
        config = {
            "provider_type": "openai",
            "api_key": "sk-test",
            "fallback_providers": [{"provider": "nonexistent_provider", "model": "some-model"}],
        }
        valid, errors = gateway_module.validate_setup_config(config)
        assert any("unknown" in e.lower() and "fallback" in e.lower() for e in errors)

    def test_fallback_provider_missing_model(self, gateway_module):
        config = {
            "provider_type": "openai",
            "api_key": "sk-test",
            "fallback_providers": [{"provider": "anthropic"}],
        }
        valid, errors = gateway_module.validate_setup_config(config)
        assert any("missing" in e.lower() and "model" in e.lower() for e in errors)

    def test_mem0_fallback_providers_validates_same(self, gateway_module):
        config = {
            "provider_type": "openai",
            "api_key": "sk-test",
            "mem0_fallback_providers": [
                {"provider": "nonexistent_provider", "model": "some-model"},
            ],
        }
        valid, errors = gateway_module.validate_setup_config(config)
        assert any("unknown" in e.lower() and "fallback" in e.lower() for e in errors)

    def test_empty_fallback_array_valid(self, gateway_module):
        config = {
            "provider_type": "openai",
            "api_key": "sk-test",
            "fallback_providers": [],
        }
        valid, errors = gateway_module.validate_setup_config(config)
        fallback_errors = [e for e in errors if "fallback" in e.lower()]
        assert len(fallback_errors) == 0


class TestFallbackPersistence:
    """Test fallback config survives save/load cycle."""

    def test_fallback_config_roundtrip(self, gateway_module, tmp_path):
        orig_setup_file = gateway_module.SETUP_FILE
        orig_config_dir = gateway_module.CONFIG_DIR
        try:
            config_dir = tmp_path / "config"
            config_dir.mkdir()
            gateway_module.CONFIG_DIR = str(config_dir)
            gateway_module.SETUP_FILE = str(config_dir / "setup.json")

            config = {
                "provider_type": "openai",
                "api_key": "sk-test",
                "fallback_providers": [
                    {"provider": "anthropic", "model": "claude-opus-4-6"},
                    {"provider": "groq", "model": "llama-3.3-70b-versatile"},
                ],
            }
            gateway_module.save_setup(config)
            loaded = gateway_module.load_setup()
            assert loaded["fallback_providers"] == config["fallback_providers"]
        finally:
            gateway_module.SETUP_FILE = orig_setup_file
            gateway_module.CONFIG_DIR = orig_config_dir

    def test_mem0_fallback_config_roundtrip(self, gateway_module, tmp_path):
        orig_setup_file = gateway_module.SETUP_FILE
        orig_config_dir = gateway_module.CONFIG_DIR
        try:
            config_dir = tmp_path / "config"
            config_dir.mkdir()
            gateway_module.CONFIG_DIR = str(config_dir)
            gateway_module.SETUP_FILE = str(config_dir / "setup.json")

            config = {
                "provider_type": "openai",
                "api_key": "sk-test",
                "mem0_fallback_providers": [
                    {"provider": "openai", "model": "gpt-4o"},
                ],
            }
            gateway_module.save_setup(config)
            loaded = gateway_module.load_setup()
            assert loaded["mem0_fallback_providers"] == config["mem0_fallback_providers"]
        finally:
            gateway_module.SETUP_FILE = orig_setup_file
            gateway_module.CONFIG_DIR = orig_config_dir


class TestPrimaryRestore:
    """Test that fallback chain always starts with primary provider."""

    def test_primary_always_first(self, gateway_module):
        """Verify _try_fallback_providers only runs AFTER primary fails.

        The function signature takes an error_string from the primary attempt,
        meaning primary was already tried. It returns None for non-retriable
        errors, confirming the chain is only entered on retriable failures.
        """
        # Non-retriable error => should not trigger fallback
        result = gateway_module._try_fallback_providers(
            relay_fn=lambda t, s: ("", "401 Unauthorized", []),
            user_text="test",
            session_id="test-session",
            error_string="401 Unauthorized",
        )
        assert result is None, "Fallback should not trigger for non-retriable errors"
