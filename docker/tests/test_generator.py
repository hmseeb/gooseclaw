"""Unit tests for the extension generator module."""

import ast
import os
import sys

import pytest

# Ensure docker/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from extensions.generator import generate_extension, list_templates
import extensions.generator as generator_module


@pytest.fixture
def template_env(tmp_path, monkeypatch):
    """Set up isolated template and output directories."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    # Copy base_helpers.py.tmpl from the real templates dir
    real_base = os.path.join(
        os.path.dirname(__file__), "..", "extensions", "templates", "base_helpers.py.tmpl"
    )
    base_content = open(real_base).read()
    (templates_dir / "base_helpers.py.tmpl").write_text(base_content)

    # Monkeypatch generator paths
    monkeypatch.setattr(generator_module, "TEMPLATES_DIR", str(templates_dir))
    monkeypatch.setattr(generator_module, "OUTPUT_BASE_DIR", str(output_dir))

    return templates_dir, output_dir


def _create_test_template(templates_dir, name="test_service", content=None):
    """Create a minimal test template."""
    if content is None:
        content = '# test tool for $${extension_name}\n\n@mcp.tool()\ndef test_tool(query: str) -> str:\n    """A test tool."""\n    key = _vault_get("$${vault_prefix}.api_key")\n    return f"result for {query}"\n\nif __name__ == "__main__":\n    mcp.run()\n'
    (templates_dir / f"{name}.py.tmpl").write_text(content)


class TestListTemplates:
    def test_list_templates_empty(self, template_env):
        """list_templates returns empty when only base_helpers exists."""
        templates_dir, _ = template_env
        result = list_templates()
        assert result == []

    def test_list_templates_finds_templates(self, template_env):
        """list_templates finds service templates, excludes base_helpers."""
        templates_dir, _ = template_env
        _create_test_template(templates_dir, "email_imap")
        _create_test_template(templates_dir, "rest_api")
        result = list_templates()
        assert "email_imap" in result
        assert "rest_api" in result
        assert "base_helpers" not in result


class TestGenerateExtension:
    def test_generate_creates_file(self, template_env):
        """generate_extension creates server.py at the expected path."""
        templates_dir, output_dir = template_env
        _create_test_template(templates_dir)

        result_path = generate_extension(
            template_name="test_service",
            extension_name="test_svc",
            vault_prefix="test",
            vault_keys=["test.key1"],
        )

        assert os.path.exists(result_path)
        assert result_path.endswith("server.py")
        assert "test_svc" in result_path

        content = open(result_path).read()

        # Verify base_helpers content is present
        assert "_vault_get" in content
        assert "sys.stdout = sys.stderr" in content
        assert 'FastMCP("test_svc")' in content
        assert "subprocess.run" in content
        assert '"secret", "get"' in content

    def test_generate_produces_valid_python(self, template_env):
        """Generated output passes ast.parse() (valid Python syntax)."""
        templates_dir, output_dir = template_env
        _create_test_template(templates_dir)

        result_path = generate_extension(
            template_name="test_service",
            extension_name="test_svc",
            vault_prefix="test",
            vault_keys=["test.key1"],
        )

        content = open(result_path).read()
        # Should not raise SyntaxError
        ast.parse(content)

    def test_generate_no_hardcoded_creds(self, template_env):
        """Generated output contains vault key paths, not raw credential values."""
        templates_dir, output_dir = template_env
        _create_test_template(templates_dir)

        result_path = generate_extension(
            template_name="test_service",
            extension_name="test_svc",
            vault_prefix="test",
            vault_keys=["test.key1"],
        )

        content = open(result_path).read()

        # Vault key path should appear (inside _vault_get calls)
        assert "test.key1" in content or "test.api_key" in content

        # The word "secret" should appear in subprocess context, not as raw value
        assert "subprocess.run" in content
        assert '"secret", "get"' in content

    def test_generate_idempotent(self, template_env):
        """Calling generate_extension twice with same params succeeds (overwrites)."""
        templates_dir, output_dir = template_env
        _create_test_template(templates_dir)

        path1 = generate_extension(
            template_name="test_service",
            extension_name="test_svc",
            vault_prefix="test",
            vault_keys=["test.key1"],
        )

        path2 = generate_extension(
            template_name="test_service",
            extension_name="test_svc",
            vault_prefix="test",
            vault_keys=["test.key1"],
        )

        assert path1 == path2
        assert os.path.exists(path2)

    def test_generate_file_executable(self, template_env):
        """Generated server.py has executable permissions."""
        templates_dir, output_dir = template_env
        _create_test_template(templates_dir)

        result_path = generate_extension(
            template_name="test_service",
            extension_name="test_svc",
            vault_prefix="test",
            vault_keys=["test.key1"],
        )

        import stat
        mode = os.stat(result_path).st_mode
        assert mode & stat.S_IEXEC  # Owner execute bit set

    def test_generate_missing_template_raises(self, template_env):
        """generate_extension raises FileNotFoundError for missing template."""
        with pytest.raises(FileNotFoundError):
            generate_extension(
                template_name="nonexistent",
                extension_name="test_svc",
                vault_prefix="test",
                vault_keys=["test.key1"],
            )

    def test_generate_vault_keys_numbered(self, template_env):
        """Extra vault keys are available as numbered substitution variables."""
        templates_dir, output_dir = template_env
        # Template that uses numbered vault keys
        content = '# uses vault_key_0=${vault_key_0} and vault_key_1=${vault_key_1}\n\n@mcp.tool()\ndef multi_key_tool() -> str:\n    """Tool with multiple keys."""\n    k0 = _vault_get("${vault_key_0}")\n    k1 = _vault_get("${vault_key_1}")\n    return "ok"\n\nif __name__ == "__main__":\n    mcp.run()\n'
        _create_test_template(templates_dir, content=content)

        result_path = generate_extension(
            template_name="test_service",
            extension_name="multi_svc",
            vault_prefix="svc",
            vault_keys=["svc.host", "svc.token"],
        )

        rendered = open(result_path).read()
        assert "svc.host" in rendered
        assert "svc.token" in rendered
