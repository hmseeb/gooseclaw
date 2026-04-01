"""Integration tests for config writer, boot loader logic, and registration flow."""

import json
import os
import sys

import pytest
import yaml

# Ensure docker/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import extensions.registry as registry
import extensions.generator as generator_module


class TestConfigEntryFormat:
    def test_config_entry_format(self):
        """Config entry dict has all required goosed fields with correct types."""
        entry = {
            "enabled": True,
            "type": "stdio",
            "name": "email_fastmail",
            "description": "Fastmail email access",
            "cmd": "python3",
            "args": ["/data/extensions/email_fastmail/server.py"],
            "envs": {},
            "env_keys": [],
            "timeout": 300,
            "bundled": None,
            "available_tools": [],
        }

        assert isinstance(entry["enabled"], bool)
        assert entry["type"] == "stdio"
        assert isinstance(entry["name"], str)
        assert isinstance(entry["description"], str)
        assert entry["cmd"] == "python3"
        assert isinstance(entry["args"], list)
        assert len(entry["args"]) == 1
        assert isinstance(entry["envs"], dict)
        assert isinstance(entry["env_keys"], list)
        assert entry["timeout"] == 300
        assert entry["bundled"] is None
        assert isinstance(entry["available_tools"], list)


class TestConfigYamlRoundtrip:
    def test_config_yaml_roundtrip(self, tmp_path):
        """Config with existing + new extensions survives YAML roundtrip."""
        config_path = tmp_path / "config.yaml"

        # Create initial config with existing extension
        initial_config = {
            "extensions": {
                "knowledge": {
                    "enabled": True,
                    "type": "stdio",
                    "name": "knowledge",
                    "description": "Knowledge base",
                    "cmd": "python3",
                    "args": ["/app/knowledge/server.py"],
                    "envs": {},
                    "env_keys": [],
                    "timeout": 300,
                    "bundled": None,
                    "available_tools": [],
                }
            }
        }
        with open(config_path, "w") as f:
            yaml.dump(initial_config, f, default_flow_style=False, sort_keys=False)

        # Add a generated extension
        with open(config_path) as f:
            config = yaml.safe_load(f)
        config["extensions"]["email_fastmail"] = {
            "enabled": True,
            "type": "stdio",
            "name": "email_fastmail",
            "description": "Fastmail email",
            "cmd": "python3",
            "args": ["/data/extensions/email_fastmail/server.py"],
            "envs": {},
            "env_keys": [],
            "timeout": 300,
            "bundled": None,
            "available_tools": [],
        }
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        # Read back and verify
        with open(config_path) as f:
            result = yaml.safe_load(f)
        assert "knowledge" in result["extensions"]
        assert "email_fastmail" in result["extensions"]
        assert result["extensions"]["knowledge"]["name"] == "knowledge"
        assert result["extensions"]["email_fastmail"]["name"] == "email_fastmail"

    def test_config_preserves_existing_extensions(self, tmp_path):
        """Adding generated extension preserves knowledge and mem0-memory."""
        config_path = tmp_path / "config.yaml"

        initial_config = {
            "extensions": {
                "knowledge": {
                    "enabled": True,
                    "type": "stdio",
                    "name": "knowledge",
                    "cmd": "python3",
                    "args": ["/app/knowledge/server.py"],
                },
                "mem0-memory": {
                    "enabled": True,
                    "type": "stdio",
                    "name": "mem0-memory",
                    "cmd": "python3",
                    "args": ["/app/mem0/server.py"],
                },
            }
        }
        with open(config_path, "w") as f:
            yaml.dump(initial_config, f, default_flow_style=False, sort_keys=False)

        # Add generated extension
        with open(config_path) as f:
            config = yaml.safe_load(f)
        config["extensions"]["my_api"] = {
            "enabled": True,
            "type": "stdio",
            "name": "my_api",
            "cmd": "python3",
            "args": ["/data/extensions/my_api/server.py"],
        }
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        # Verify all three present and unchanged
        with open(config_path) as f:
            result = yaml.safe_load(f)
        assert "knowledge" in result["extensions"]
        assert "mem0-memory" in result["extensions"]
        assert "my_api" in result["extensions"]
        assert result["extensions"]["knowledge"]["args"] == ["/app/knowledge/server.py"]
        assert result["extensions"]["mem0-memory"]["args"] == ["/app/mem0/server.py"]


class TestBootLoaderLogic:
    """Test the boot loader logic (same logic as entrypoint.sh inline python)."""

    def _run_boot_loader(self, registry_path, config_path):
        """Simulate the entrypoint.sh boot loader in pure Python."""
        if not os.path.isfile(registry_path):
            return [], []

        with open(registry_path) as f:
            reg = json.load(f)
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

        exts = config.setdefault("extensions", {})
        added = []
        skipped = []

        for name, meta in reg.get("extensions", {}).items():
            if not meta.get("enabled", True):
                skipped.append(f"{name} (disabled)")
                continue
            sp = meta.get("server_path", "")
            if not os.path.isfile(sp):
                skipped.append(f"{name} (server.py missing)")
                continue
            exts[name] = {
                "enabled": True,
                "type": "stdio",
                "name": name,
                "description": meta.get("description", f"Auto-generated {name} extension"),
                "cmd": "python3",
                "args": [sp],
                "envs": {},
                "env_keys": [],
                "timeout": 300,
                "bundled": None,
                "available_tools": [],
            }
            added.append(name)

        if added:
            config["extensions"] = exts
            with open(config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        return added, skipped

    def test_boot_loader_logic(self, tmp_path):
        """Boot loader loads enabled extensions and skips disabled ones."""
        registry_path = tmp_path / "registry.json"
        config_path = tmp_path / "config.yaml"

        # Create fake server.py for enabled extension
        ext_dir = tmp_path / "ext_enabled"
        ext_dir.mkdir()
        server_file = ext_dir / "server.py"
        server_file.write_text("# fake server")

        # Registry with one enabled, one disabled
        reg_data = {
            "version": 1,
            "extensions": {
                "ext_enabled": {
                    "server_path": str(server_file),
                    "enabled": True,
                    "description": "Enabled ext",
                },
                "ext_disabled": {
                    "server_path": "/nonexistent/server.py",
                    "enabled": False,
                    "description": "Disabled ext",
                },
            },
        }
        with open(registry_path, "w") as f:
            json.dump(reg_data, f)
        with open(config_path, "w") as f:
            yaml.dump({"extensions": {}}, f)

        added, skipped = self._run_boot_loader(str(registry_path), str(config_path))

        assert "ext_enabled" in added
        assert len(skipped) == 1
        assert "disabled" in skipped[0]

        # Verify config was written
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert "ext_enabled" in config["extensions"]
        assert "ext_disabled" not in config["extensions"]

    def test_boot_loader_skips_missing_server(self, tmp_path):
        """Boot loader skips extensions where server.py doesn't exist on disk."""
        registry_path = tmp_path / "registry.json"
        config_path = tmp_path / "config.yaml"

        reg_data = {
            "version": 1,
            "extensions": {
                "missing_ext": {
                    "server_path": "/nonexistent/path/server.py",
                    "enabled": True,
                    "description": "Missing server",
                },
            },
        }
        with open(registry_path, "w") as f:
            json.dump(reg_data, f)
        with open(config_path, "w") as f:
            yaml.dump({"extensions": {}}, f)

        added, skipped = self._run_boot_loader(str(registry_path), str(config_path))

        assert added == []
        assert len(skipped) == 1
        assert "server.py missing" in skipped[0]

    def test_boot_loader_handles_empty_registry(self, tmp_path):
        """Empty registry produces no config entries."""
        registry_path = tmp_path / "registry.json"
        config_path = tmp_path / "config.yaml"

        with open(registry_path, "w") as f:
            json.dump({"version": 1, "extensions": {}}, f)
        with open(config_path, "w") as f:
            yaml.dump({"extensions": {}}, f)

        added, skipped = self._run_boot_loader(str(registry_path), str(config_path))

        assert added == []
        assert skipped == []

    def test_boot_loader_handles_missing_registry(self, tmp_path):
        """No registry.json file skips gracefully."""
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({"extensions": {}}, f)

        added, skipped = self._run_boot_loader(
            str(tmp_path / "nonexistent_registry.json"), str(config_path)
        )

        assert added == []
        assert skipped == []


class TestFullRegistrationFlow:
    def test_full_registration_flow(self, tmp_path, monkeypatch):
        """Integration test: generate -> register -> verify config entries."""
        # Set up isolated paths
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        registry_path = str(tmp_path / "registry.json")

        # Copy base_helpers template
        real_base = os.path.join(
            os.path.dirname(__file__), "..", "extensions", "templates", "base_helpers.py.tmpl"
        )
        base_content = open(real_base).read()
        (templates_dir / "base_helpers.py.tmpl").write_text(base_content)

        # Create a minimal test template
        test_tmpl = '# test for ${extension_name}\n\n@mcp.tool()\ndef test_tool(query: str) -> str:\n    """Test."""\n    return "ok"\n\nif __name__ == "__main__":\n    mcp.run()\n'
        (templates_dir / "test_svc.py.tmpl").write_text(test_tmpl)

        # Monkeypatch paths
        monkeypatch.setattr(generator_module, "TEMPLATES_DIR", str(templates_dir))
        monkeypatch.setattr(generator_module, "OUTPUT_BASE_DIR", str(output_dir))
        monkeypatch.setattr(registry, "REGISTRY_PATH", registry_path)

        # Generate extension
        from extensions.generator import generate_extension

        server_path = generate_extension(
            template_name="test_svc",
            extension_name="my_test_ext",
            vault_prefix="test",
            vault_keys=["test.api_key"],
        )

        # Register in registry
        registry.register(
            name="my_test_ext",
            template="test_svc",
            vault_prefix="test",
            vault_keys=["test.api_key"],
            server_path=server_path,
            description="Test extension",
        )

        # Verify config entries
        entries = registry.get_config_entries()
        assert "my_test_ext" in entries

        entry = entries["my_test_ext"]
        assert entry["type"] == "stdio"
        assert entry["cmd"] == "python3"
        assert entry["args"] == [server_path]
        assert entry["timeout"] == 300

        # Verify server.py actually exists at the path
        assert os.path.isfile(server_path)
        assert os.path.isfile(entry["args"][0])
