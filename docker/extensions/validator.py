"""Validation functions for auto-generated MCP server extensions.

Provides syntax checking, health checking, and failure tracking
with automatic disable after repeated failures.

Uses stdlib only, matching project patterns (generator.py, registry.py).
"""

import ast
import json
import logging
import os
import subprocess
import sys

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("extension-validator")


def validate_syntax(server_path):
    """Check that a generated .py file has valid Python syntax.

    Args:
        server_path: Path to the .py file to check.

    Returns:
        (True, "") on success, (False, error_message) on failure.
    """
    try:
        with open(server_path) as f:
            source = f.read()
        ast.parse(source)
        return (True, "")
    except SyntaxError as e:
        msg = f"SyntaxError at line {e.lineno}: {e.msg}"
        logger.error("Syntax validation failed for %s: %s", server_path, msg)
        return (False, msg)
    except FileNotFoundError:
        msg = f"File not found: {server_path}"
        logger.error("Syntax validation failed: %s", msg)
        return (False, msg)


def health_check(server_path, timeout=10):
    """Spawn the extension and verify it responds to MCP initialize.

    Sends a JSON-RPC initialize request on stdin, expects a valid
    JSON-RPC response on stdout within the timeout.

    Args:
        server_path: Path to the extension server.py.
        timeout: Seconds to wait for response (default 10).

    Returns:
        (True, "") on success, (False, error_message) on failure.
    """
    initialize_request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "health-check", "version": "1.0"},
        },
    })

    process = None
    try:
        process = subprocess.Popen(
            ["python3", server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Send initialize request
        process.stdin.write((initialize_request + "\n").encode())
        process.stdin.flush()

        # Wait for response
        import select

        ready, _, _ = select.select([process.stdout], [], [], timeout)
        if not ready:
            return (False, f"Timeout after {timeout}s waiting for MCP response")

        line = process.stdout.readline().decode().strip()
        if not line:
            return (False, "Empty response from extension")

        response = json.loads(line)
        if "result" in response:
            return (True, "")
        elif "error" in response:
            return (False, f"MCP error: {response['error']}")
        else:
            return (False, f"Invalid MCP response: {line}")

    except subprocess.SubprocessError as e:
        return (False, f"Process error: {e}")
    except json.JSONDecodeError as e:
        return (False, f"Invalid JSON response: {e}")
    except Exception as e:
        return (False, f"Health check error: {e}")
    finally:
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass


def record_failure(extension_name):
    """Increment the consecutive failure counter for an extension.

    Args:
        extension_name: Name of the extension in the registry.

    Returns:
        The new failure count.
    """
    import extensions.registry as registry

    reg = registry._load_registry()
    ext = reg.get("extensions", {}).get(extension_name)
    if ext is None:
        logger.warning("Extension %s not found in registry for failure tracking", extension_name)
        return 0

    count = ext.get("consecutive_failures", 0) + 1
    ext["consecutive_failures"] = count
    registry._save_registry(reg)

    logger.info("Extension %s failure count: %d", extension_name, count)
    return count


def clear_failures(extension_name):
    """Reset the consecutive failure counter for an extension.

    Args:
        extension_name: Name of the extension in the registry.
    """
    import extensions.registry as registry

    reg = registry._load_registry()
    ext = reg.get("extensions", {}).get(extension_name)
    if ext is None:
        return

    ext["consecutive_failures"] = 0
    registry._save_registry(reg)

    logger.info("Cleared failure count for extension %s", extension_name)


def check_and_disable(extension_name, max_failures=3):
    """Record a failure and disable the extension if threshold reached.

    Args:
        extension_name: Name of the extension in the registry.
        max_failures: Number of consecutive failures before auto-disable (default 3).

    Returns:
        True if the extension was disabled, False otherwise.
    """
    import extensions.registry as registry

    count = record_failure(extension_name)
    if count <= 0:
        # Extension not found
        return False

    if count >= max_failures:
        reg = registry._load_registry()
        ext = reg.get("extensions", {}).get(extension_name)
        if ext is not None:
            ext["enabled"] = False
            registry._save_registry(reg)
            logger.warning(
                "Extension %s disabled after %d consecutive failures",
                extension_name,
                count,
            )
            return True

    return False
