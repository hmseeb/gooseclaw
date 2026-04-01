"""Unit tests for the extension registry module."""

import json
import os
import sys

import pytest

# Ensure docker/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import extensions.registry as registry


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    """Set up isolated registry path for tests."""
    registry_path = str(tmp_path / "registry.json")
    monkeypatch.setattr(registry, "REGISTRY_PATH", registry_path)
    return registry_path


class TestRegister:
    def test_register_creates_registry_file(self, registry_env):
        """Register an extension, verify registry.json is created on disk."""
        registry.register(
            name="email_fastmail",
            template="email_imap",
            vault_prefix="fastmail",
            vault_keys=["fastmail.imap_host", "fastmail.password"],
            server_path="/data/extensions/email_fastmail/server.py",
        )

        assert os.path.isfile(registry_env)
        with open(registry_env) as f:
            data = json.load(f)
        assert data["version"] == 1
        assert "email_fastmail" in data["extensions"]

    def test_register_entry_fields(self, registry_env):
        """Register an extension, verify all fields are present and correct."""
        entry = registry.register(
            name="email_fastmail",
            template="email_imap",
            vault_prefix="fastmail",
            vault_keys=["fastmail.imap_host", "fastmail.password"],
            server_path="/data/extensions/email_fastmail/server.py",
            description="Fastmail email access",
        )

        assert entry["template"] == "email_imap"
        assert entry["extension_name"] == "email_fastmail"
        assert entry["vault_prefix"] == "fastmail"
        assert entry["vault_keys"] == ["fastmail.imap_host", "fastmail.password"]
        assert entry["server_path"] == "/data/extensions/email_fastmail/server.py"
        assert entry["description"] == "Fastmail email access"
        assert entry["enabled"] is True
        # generated_at should be ISO format
        assert "T" in entry["generated_at"]

    def test_register_updates_existing(self, registry_env):
        """Register same name twice with different template, second overwrites."""
        registry.register(
            name="my_ext",
            template="rest_api",
            vault_prefix="svc",
            vault_keys=["svc.key"],
            server_path="/data/extensions/my_ext/server.py",
        )
        registry.register(
            name="my_ext",
            template="email_imap",
            vault_prefix="svc",
            vault_keys=["svc.key", "svc.host"],
            server_path="/data/extensions/my_ext/server.py",
        )

        exts = registry.list_extensions()
        assert len(exts) == 1
        assert exts["my_ext"]["template"] == "email_imap"
        assert exts["my_ext"]["vault_keys"] == ["svc.key", "svc.host"]


class TestUnregister:
    def test_unregister_removes_entry(self, registry_env):
        """Register then unregister. Extensions dict should be empty."""
        registry.register(
            name="my_ext",
            template="rest_api",
            vault_prefix="svc",
            vault_keys=["svc.key"],
            server_path="/data/extensions/my_ext/server.py",
        )
        removed = registry.unregister("my_ext")

        assert removed is not None
        assert removed["extension_name"] == "my_ext"
        assert registry.list_extensions() == {}

    def test_unregister_nonexistent_returns_none(self, registry_env):
        """Unregister a name that doesn't exist returns None without crash."""
        result = registry.unregister("nonexistent")
        assert result is None

    def test_unregister_with_delete_files(self, registry_env, tmp_path):
        """Unregister with delete_files=True removes server.py and empty parent."""
        # Create a fake server.py
        ext_dir = tmp_path / "ext_output" / "my_ext"
        ext_dir.mkdir(parents=True)
        server_file = ext_dir / "server.py"
        server_file.write_text("# fake server")

        registry.register(
            name="my_ext",
            template="rest_api",
            vault_prefix="svc",
            vault_keys=["svc.key"],
            server_path=str(server_file),
        )

        removed = registry.unregister("my_ext", delete_files=True)

        assert removed is not None
        assert not os.path.exists(str(server_file))
        assert not os.path.exists(str(ext_dir))  # empty parent dir removed


class TestListExtensions:
    def test_list_extensions(self, registry_env):
        """Register two extensions, list_extensions returns both."""
        registry.register(
            name="ext_a",
            template="rest_api",
            vault_prefix="a",
            vault_keys=["a.key"],
            server_path="/data/extensions/ext_a/server.py",
        )
        registry.register(
            name="ext_b",
            template="email_imap",
            vault_prefix="b",
            vault_keys=["b.host"],
            server_path="/data/extensions/ext_b/server.py",
        )

        exts = registry.list_extensions()
        assert len(exts) == 2
        assert "ext_a" in exts
        assert "ext_b" in exts


class TestGetConfigEntries:
    def test_get_config_entries_format(self, registry_env):
        """get_config_entries returns exact goosed extension format."""
        registry.register(
            name="email_fastmail",
            template="email_imap",
            vault_prefix="fastmail",
            vault_keys=["fastmail.imap_host"],
            server_path="/data/extensions/email_fastmail/server.py",
            description="Fastmail email",
        )

        entries = registry.get_config_entries()
        assert "email_fastmail" in entries

        entry = entries["email_fastmail"]
        assert entry["enabled"] is True
        assert entry["type"] == "stdio"
        assert entry["name"] == "email_fastmail"
        assert entry["description"] == "Fastmail email"
        assert entry["cmd"] == "python3"
        assert entry["args"] == ["/data/extensions/email_fastmail/server.py"]
        assert entry["envs"] == {}
        assert entry["env_keys"] == []
        assert entry["timeout"] == 300
        assert entry["bundled"] is None
        assert entry["available_tools"] == []

    def test_get_config_entries_skips_disabled(self, registry_env):
        """get_config_entries skips extensions with enabled=False."""
        registry.register(
            name="disabled_ext",
            template="rest_api",
            vault_prefix="svc",
            vault_keys=["svc.key"],
            server_path="/data/extensions/disabled_ext/server.py",
        )

        # Manually set enabled=False in registry file
        with open(registry_env) as f:
            data = json.load(f)
        data["extensions"]["disabled_ext"]["enabled"] = False
        with open(registry_env, "w") as f:
            json.dump(data, f)

        entries = registry.get_config_entries()
        assert entries == {}


class TestEmptyRegistry:
    def test_empty_registry(self, registry_env):
        """list_extensions and get_config_entries return empty dicts when no file exists."""
        assert registry.list_extensions() == {}
        assert registry.get_config_entries() == {}
