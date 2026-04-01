"""Template rendering engine for auto-generated MCP server extensions.

Uses Python's string.Template for safe variable substitution.
No Jinja2 dependency — stdlib only.
"""

import os
import sys
import stat
import logging
from datetime import datetime, timezone
from pathlib import Path
from string import Template

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("extension-generator")

# Configurable paths (monkeypatch-friendly for tests)
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
OUTPUT_BASE_DIR = "/data/extensions"


def list_templates():
    """List available template names by scanning the templates directory.

    Returns template names (without .py.tmpl suffix), excluding base_helpers
    which is a shared snippet, not a standalone template.
    """
    templates_path = Path(TEMPLATES_DIR)
    if not templates_path.exists():
        return []
    return sorted(
        p.stem.replace(".py", "")
        for p in templates_path.glob("*.py.tmpl")
        if p.stem.replace(".py", "") != "base_helpers"
    )


def generate_extension(
    template_name,
    extension_name,
    vault_prefix,
    vault_keys,
    service_description="",
    extra_subs=None,
):
    """Render a template into a standalone MCP server .py file.

    Args:
        template_name: Name of the template (without .py.tmpl suffix).
        extension_name: Display name for the generated extension.
        vault_prefix: Prefix for vault key lookups (e.g., "fastmail").
        vault_keys: List of vault key paths (e.g., ["fastmail.imap_host"]).
        service_description: Human-readable service description for tool docstrings.
        extra_subs: Optional dict of additional template substitution variables.

    Returns:
        Full path to the generated server.py file.
    """
    templates_path = Path(TEMPLATES_DIR)

    # Read base helpers (shared boilerplate)
    base_helpers_path = templates_path / "base_helpers.py.tmpl"
    if not base_helpers_path.exists():
        raise FileNotFoundError(f"Base helpers template not found: {base_helpers_path}")
    base_content = base_helpers_path.read_text()

    # Read the service-specific template
    template_path = templates_path / f"{template_name}.py.tmpl"
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    template_content = template_path.read_text()

    # Build substitution dict
    subs = {
        "extension_name": extension_name,
        "template_name": template_name,
        "vault_prefix": vault_prefix,
        "vault_keys_display": ", ".join(vault_keys),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service_description": service_description,
        # Defaults for optional template variables
        "auth_type": "bearer",
        "auth_header": "X-API-Key",
    }

    # Add numbered vault key variables
    for i, key in enumerate(vault_keys):
        subs[f"vault_key_{i}"] = key

    # Merge extra substitutions (overrides defaults)
    if extra_subs:
        subs.update(extra_subs)

    # Render: base helpers + template content
    combined = base_content + "\n" + template_content
    rendered = Template(combined).safe_substitute(subs)

    # Create output directory
    output_dir = Path(OUTPUT_BASE_DIR) / extension_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write server.py
    server_path = output_dir / "server.py"
    server_path.write_text(rendered)

    # Make executable
    server_path.chmod(server_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    logger.info("Generated extension: %s -> %s", extension_name, server_path)
    return str(server_path)
