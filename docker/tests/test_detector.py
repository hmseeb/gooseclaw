"""Unit tests for the extension detector module."""

import os
import subprocess
import sys

import pytest

# Ensure docker/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import extensions.detector as detector


class TestDetectCredentials:
    def test_detects_api_key(self):
        """Detects API key with sk- prefix."""
        text = "my api key is sk-abc123def456ghi789jkl012"
        results = detector.detect_credentials(text)
        assert len(results) >= 1
        match = results[0]
        assert "sk-abc123def456ghi789jkl012" in match["value"]
        assert match["confidence"] > 0.5

    def test_detects_github_token(self):
        """Detects GitHub personal access token with ghp_ prefix."""
        text = "here's my token: ghp_ABCdefGHIjklMNOpqrSTUvwxYZ1234567890"
        results = detector.detect_credentials(text)
        assert len(results) >= 1
        found = [r for r in results if "github" in r["type"]]
        assert len(found) >= 1
        assert "ghp_" in found[0]["value"]

    def test_detects_app_password(self):
        """Detects 16-character lowercase app password."""
        text = "my fastmail app password is abcdefghijklmnop"
        results = detector.detect_credentials(text)
        assert len(results) >= 1
        found = [r for r in results if r["type"] == "app_password"]
        assert len(found) >= 1
        assert found[0]["value"] == "abcdefghijklmnop"

    def test_detects_app_password_with_spaces(self):
        """Detects app password formatted with spaces every 4 chars."""
        text = "password: abcd efgh ijkl mnop"
        results = detector.detect_credentials(text)
        assert len(results) >= 1
        found = [r for r in results if r["type"] == "app_password"]
        assert len(found) >= 1
        # Value should be normalized (spaces removed)
        assert found[0]["value"] == "abcdefghijklmnop"

    def test_no_false_positives_on_normal_text(self):
        """Normal text without credentials returns empty list."""
        text = "Hello, how are you today? I need help with my project."
        results = detector.detect_credentials(text)
        assert results == []

    def test_detects_aws_key(self):
        """Detects AWS access key with AKIA prefix."""
        text = "AKIAIOSFODNN7EXAMPLE"
        results = detector.detect_credentials(text)
        assert len(results) >= 1
        found = [r for r in results if r["type"] == "aws_key"]
        assert len(found) >= 1

    def test_strips_quotes(self):
        """Detected values have surrounding quotes stripped."""
        text = 'key: "sk-test1234567890abcdefgh"'
        results = detector.detect_credentials(text)
        assert len(results) >= 1
        # Value should not have surrounding quotes
        assert not results[0]["value"].startswith('"')
        assert not results[0]["value"].endswith('"')


class TestClassifyCredential:
    def test_classifies_email_credential(self):
        """App password with email hint maps to email_imap template."""
        cred = {"value": "abcdefghijklmnop", "type": "app_password", "confidence": 0.8}
        result = detector.classify_credential(cred, "fastmail email")
        assert result["template"] == "email_imap"
        assert "imap" in result["vault_keys"][0] or "app_password" in str(result["vault_keys"])

    def test_classifies_github(self):
        """GitHub token maps to rest_api template with github prefix."""
        cred = {"value": "ghp_xxx", "type": "github_token", "confidence": 0.95}
        result = detector.classify_credential(cred, "")
        assert result["template"] == "rest_api"
        assert result["vault_prefix"] == "github"

    def test_classifies_generic_api_key(self):
        """Generic API key with hint derives prefix from hint."""
        cred = {"value": "sk-xxx", "type": "api_key", "confidence": 0.7}
        result = detector.classify_credential(cred, "openai")
        assert result["template"] == "rest_api"
        assert "openai" in result["vault_prefix"]

    def test_default_classification(self):
        """Unknown credential with no hint gets valid default classification."""
        cred = {"value": "sometoken123", "type": "api_key", "confidence": 0.5}
        result = detector.classify_credential(cred, "")
        assert "template" in result
        assert "vault_prefix" in result
        assert "vault_keys" in result
        assert "extension_name" in result
        assert len(result["vault_keys"]) > 0


class TestCredentialPipeline:
    def test_credential_to_extension_e2e(self, monkeypatch):
        """Full pipeline succeeds with monkeypatched dependencies."""
        # Monkeypatch subprocess.run for vault
        monkeypatch.setattr(
            subprocess, "run",
            lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )

        # Monkeypatch gateway.register_generated_extension
        import types
        fake_gateway = types.ModuleType("gateway")
        fake_gateway.register_generated_extension = lambda **kwargs: "/data/extensions/test/server.py"
        monkeypatch.setitem(sys.modules, "gateway", fake_gateway)

        # Monkeypatch validator functions
        import extensions.validator as validator
        monkeypatch.setattr(validator, "validate_syntax", lambda path: (True, ""))
        monkeypatch.setattr(validator, "health_check", lambda path, timeout=10: (True, ""))
        monkeypatch.setattr(validator, "clear_failures", lambda name: None)

        classification = {
            "template": "rest_api",
            "vault_prefix": "test_svc",
            "vault_keys": ["test_svc.api_key"],
            "extension_name": "test_svc_api",
            "description": "Test API",
            "extra_subs": {},
        }

        # Need to reimport to pick up monkeypatched modules
        import importlib
        importlib.reload(detector)

        result = detector.credential_to_extension("fake-api-key-12345", classification)
        assert result["success"] is True

    def test_pipeline_fails_on_syntax_error(self, monkeypatch):
        """Pipeline returns failure when syntax validation fails."""
        # Monkeypatch subprocess.run for vault
        monkeypatch.setattr(
            subprocess, "run",
            lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )

        # Monkeypatch gateway.register_generated_extension
        import types
        fake_gateway = types.ModuleType("gateway")
        fake_gateway.register_generated_extension = lambda **kwargs: "/data/extensions/test/server.py"
        monkeypatch.setitem(sys.modules, "gateway", fake_gateway)

        # Monkeypatch validator - syntax check fails
        import extensions.validator as validator
        monkeypatch.setattr(validator, "validate_syntax", lambda path: (False, "SyntaxError at line 1: invalid syntax"))
        monkeypatch.setattr(validator, "health_check", lambda path, timeout=10: (True, ""))

        classification = {
            "template": "rest_api",
            "vault_prefix": "test_svc",
            "vault_keys": ["test_svc.api_key"],
            "extension_name": "test_svc_api",
            "description": "Test API",
            "extra_subs": {},
        }

        import importlib
        importlib.reload(detector)

        result = detector.credential_to_extension("fake-api-key-12345", classification)
        assert result["success"] is False
        assert result["stage"] == "validation"
