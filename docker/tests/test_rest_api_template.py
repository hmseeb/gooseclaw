"""Tests for the REST API MCP server template generation."""

import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from extensions.generator import generate_extension
import extensions.generator as generator_module


@pytest.fixture
def template_env(tmp_path, monkeypatch):
    """Set up isolated template and output directories with real templates."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    # Copy real templates
    real_templates = os.path.join(os.path.dirname(__file__), "..", "extensions", "templates")
    for name in ["base_helpers.py.tmpl", "rest_api.py.tmpl"]:
        src = os.path.join(real_templates, name)
        (templates_dir / name).write_text(open(src).read())

    monkeypatch.setattr(generator_module, "TEMPLATES_DIR", str(templates_dir))
    monkeypatch.setattr(generator_module, "OUTPUT_BASE_DIR", str(output_dir))

    return templates_dir, output_dir


def _generate_rest_api(template_env, auth_type="bearer", auth_header="X-API-Key"):
    """Helper to generate a REST API extension."""
    return generate_extension(
        template_name="rest_api",
        extension_name="test_api",
        vault_prefix="github",
        vault_keys=["github.api_key", "github.base_url"],
        service_description="GitHub API",
        extra_subs={"auth_type": auth_type, "auth_header": auth_header},
    )


class TestRestApiTemplate:
    def test_rest_api_template_generates_valid_python(self, template_env):
        """Generated REST API extension passes ast.parse() (valid Python)."""
        path = _generate_rest_api(template_env)
        content = open(path).read()
        ast.parse(content)

    def test_rest_api_template_has_required_tools(self, template_env):
        """Generated REST API extension has api_get, api_post, api_put, api_delete tools."""
        path = _generate_rest_api(template_env)
        content = open(path).read()
        tree = ast.parse(content)

        tool_functions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.decorator_list:
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                        if dec.func.attr == "tool":
                            tool_functions.append(node.name)

        assert "api_get" in tool_functions, f"Missing api_get, found: {tool_functions}"
        assert "api_post" in tool_functions, f"Missing api_post, found: {tool_functions}"
        assert "api_put" in tool_functions, f"Missing api_put, found: {tool_functions}"
        assert "api_delete" in tool_functions, f"Missing api_delete, found: {tool_functions}"

    def test_rest_api_template_vault_reads(self, template_env):
        """Generated REST API extension reads API credentials from vault."""
        path = _generate_rest_api(template_env)
        content = open(path).read()

        assert "github.api_key" in content
        assert "github.base_url" in content
        assert "_vault_get" in content

    def test_rest_api_template_no_requests_library(self, template_env):
        """Generated REST API extension uses urllib (stdlib), not requests."""
        path = _generate_rest_api(template_env)
        content = open(path).read()
        tree = ast.parse(content)

        # Check no 'requests' import
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "requests", "Found import of 'requests' library"
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    assert not node.module.startswith("requests"), (
                        f"Found import from 'requests': {node.module}"
                    )

        # Check urllib.request IS imported
        assert "urllib.request" in content

    def test_rest_api_template_stdout_redirected(self, template_env):
        """Generated REST API extension redirects stdout to stderr (TMPL-05)."""
        path = _generate_rest_api(template_env)
        content = open(path).read()
        assert "sys.stdout = sys.stderr" in content

    def test_rest_api_template_auth_types(self, template_env):
        """Both bearer and api_key_header auth types produce valid Python."""
        # Bearer auth
        bearer_path = _generate_rest_api(template_env, auth_type="bearer")
        bearer_content = open(bearer_path).read()
        ast.parse(bearer_content)
        assert "Bearer" in bearer_content

        # API key header auth
        api_key_path = _generate_rest_api(
            template_env, auth_type="api_key_header", auth_header="X-Custom-Key"
        )
        api_key_content = open(api_key_path).read()
        ast.parse(api_key_content)
        assert "X-Custom-Key" in api_key_content
