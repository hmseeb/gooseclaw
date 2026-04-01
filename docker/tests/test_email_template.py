"""Tests for the email IMAP/SMTP MCP server template generation."""

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
    for name in ["base_helpers.py.tmpl", "email_imap.py.tmpl"]:
        src = os.path.join(real_templates, name)
        (templates_dir / name).write_text(open(src).read())

    monkeypatch.setattr(generator_module, "TEMPLATES_DIR", str(templates_dir))
    monkeypatch.setattr(generator_module, "OUTPUT_BASE_DIR", str(output_dir))

    return templates_dir, output_dir


def _generate_email(template_env):
    """Helper to generate an email extension."""
    return generate_extension(
        template_name="email_imap",
        extension_name="test_email",
        vault_prefix="fastmail",
        vault_keys=[
            "fastmail.imap_host",
            "fastmail.username",
            "fastmail.app_password",
            "fastmail.smtp_host",
        ],
        service_description="Fastmail email",
    )


class TestEmailTemplate:
    def test_email_template_generates_valid_python(self, template_env):
        """Generated email extension passes ast.parse() (valid Python)."""
        path = _generate_email(template_env)
        content = open(path).read()
        ast.parse(content)

    def test_email_template_has_required_tools(self, template_env):
        """Generated email extension has email_search, email_read, email_send tools."""
        path = _generate_email(template_env)
        content = open(path).read()
        tree = ast.parse(content)

        # Find all function definitions decorated with @mcp.tool()
        tool_functions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.decorator_list:
                for dec in node.decorator_list:
                    # Match @mcp.tool() decorator
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                        if dec.func.attr == "tool":
                            tool_functions.append(node.name)

        assert "email_search" in tool_functions, f"Missing email_search, found: {tool_functions}"
        assert "email_read" in tool_functions, f"Missing email_read, found: {tool_functions}"
        assert "email_send" in tool_functions, f"Missing email_send, found: {tool_functions}"

    def test_email_template_vault_reads(self, template_env):
        """Generated email extension reads IMAP credentials from vault."""
        path = _generate_email(template_env)
        content = open(path).read()

        # Should contain vault reads for all credential fields
        assert "fastmail.imap_host" in content
        assert "fastmail.username" in content
        assert "fastmail.app_password" in content
        assert "fastmail.smtp_host" in content

        # Should use _vault_get for reading
        assert "_vault_get" in content

    def test_email_template_no_external_deps(self, template_env):
        """Generated email extension uses only stdlib + mcp SDK."""
        path = _generate_email(template_env)
        content = open(path).read()
        tree = ast.parse(content)

        allowed_modules = {
            "sys", "os", "subprocess", "logging",
            "imaplib", "smtplib", "email", "json", "re",
            "email.mime.text", "email.mime.multipart", "email.header",
            "mcp.server.fastmcp", "mcp",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    assert mod in allowed_modules or alias.name in allowed_modules, (
                        f"External dependency found: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mod = node.module.split(".")[0]
                    assert mod in allowed_modules or node.module in allowed_modules, (
                        f"External dependency found: {node.module}"
                    )

    def test_email_template_stdout_redirected(self, template_env):
        """Generated email extension redirects stdout to stderr (TMPL-05)."""
        path = _generate_email(template_env)
        content = open(path).read()
        assert "sys.stdout = sys.stderr" in content
