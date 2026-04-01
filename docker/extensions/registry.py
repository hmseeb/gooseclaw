"""Persistent registry for auto-generated MCP server extensions.

Tracks all generated extensions in a JSON manifest at /data/extensions/registry.json.
This is the single source of truth for what generated extensions exist.
Config.yaml is a derived artifact written from this registry.

Uses fcntl file locking and atomic os.replace() for crash-safe writes.
"""

import fcntl
import json
import logging
import os
import sys
from datetime import datetime, timezone

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("extension-registry")

# Configurable path (monkeypatch-friendly for tests)
REGISTRY_PATH = "/data/extensions/registry.json"


def _load_registry():
    """Load registry from disk.

    Returns {"version": 1, "extensions": {}} if file missing or corrupt.
    """
    if not os.path.isfile(REGISTRY_PATH):
        return {"version": 1, "extensions": {}}
    try:
        with open(REGISTRY_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        logger.warning("Registry file corrupt or unreadable, returning empty registry")
        return {"version": 1, "extensions": {}}


def _save_registry(data):
    """Atomic write registry to disk with file locking.

    Uses tmp file + fcntl.flock + os.fsync + os.replace for
    cross-process safety and crash durability.
    """
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    tmp_path = REGISTRY_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, REGISTRY_PATH)


def register(name, template, vault_prefix, vault_keys, server_path, description="", extra_subs=None):
    """Add or update an extension in the registry.

    Args:
        name: Extension name (unique key).
        template: Template name used to generate the extension.
        vault_prefix: Prefix for vault key lookups.
        vault_keys: List of vault key paths.
        server_path: Absolute path to the generated server.py.
        description: Human-readable description.
        extra_subs: Optional dict of template substitution overrides (auth_type, base_url, etc.)

    Returns:
        The registered entry dict.
    """
    reg = _load_registry()
    reg["extensions"][name] = {
        "template": template,
        "extension_name": name,
        "vault_prefix": vault_prefix,
        "vault_keys": vault_keys,
        "server_path": server_path,
        "description": description,
        "extra_subs": extra_subs or {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": True,
    }
    _save_registry(reg)
    logger.info("Registered extension: %s (template=%s)", name, template)
    return reg["extensions"][name]


def unregister(name, delete_files=False):
    """Remove an extension from the registry.

    Args:
        name: Extension name to remove.
        delete_files: If True, also delete the server.py file and
            its parent directory if empty.

    Returns:
        The removed entry dict, or None if not found.
    """
    reg = _load_registry()
    entry = reg["extensions"].pop(name, None)
    if entry and delete_files:
        server_path = entry.get("server_path", "")
        if os.path.isfile(server_path):
            os.remove(server_path)
            parent = os.path.dirname(server_path)
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
    _save_registry(reg)
    if entry:
        logger.info("Unregistered extension: %s", name)
    return entry


def list_extensions():
    """Return dict of all registered extensions."""
    return _load_registry().get("extensions", {})


def get_config_entries():
    """Return goosed config.yaml extension dicts for all enabled extensions.

    Produces the exact dict format goosed expects for stdio extensions.
    Skips disabled entries.

    Returns:
        Dict keyed by extension name, values are goosed config dicts.
    """
    entries = {}
    for name, meta in list_extensions().items():
        if not meta.get("enabled", True):
            continue
        entries[name] = {
            "enabled": True,
            "type": "stdio",
            "name": name,
            "description": meta.get("description", f"Auto-generated {name} extension"),
            "cmd": "python3",
            "args": [meta["server_path"]],
            "envs": {},
            "env_keys": [],
            "timeout": 300,
            "bundled": None,
            "available_tools": [],
        }
    return entries
