"""Tests for infrastructure hardening (Phase 20).

HARD-01: Hash-pinned dependency lock file
HARD-02: Dependabot CVE scanning configuration
HARD-03: Graceful shutdown watchdog
HARD-05: JSON log formatter
HARD-06: Security-sensitive structured logging
"""

import json
import os
import re

import pytest
import yaml


# ── helpers ──────────────────────────────────────────────────────────────────

def _project_root():
    return os.path.join(os.path.dirname(__file__), "..", "..")


def _read_file(relpath):
    with open(os.path.join(_project_root(), relpath)) as f:
        return f.read()


# ── HARD-01: requirements.lock ───────────────────────────────────────────────


def test_requirements_txt_pins_exact_versions():
    """requirements.txt uses == for all direct dependencies (no ranges)."""
    src = _read_file("docker/requirements.txt")
    pkg_lines = [
        l.strip()
        for l in src.splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    for line in pkg_lines:
        assert ">=" not in line and "<" not in line and "~=" not in line, (
            f"requirements.txt should pin exact versions: {line}"
        )
        assert "==" in line, f"requirements.txt should use ==: {line}"


def test_dockerfile_supports_require_hashes():
    """Dockerfile uses --require-hashes when requirements.lock is present."""
    src = _read_file("Dockerfile")
    assert "require-hashes" in src, "Dockerfile should use --require-hashes"
    assert "requirements.lock" in src, "Dockerfile should reference requirements.lock"


def test_lockfile_generation_script_exists():
    """generate-lockfile.sh exists and is executable."""
    script_path = os.path.join(_project_root(), "docker", "generate-lockfile.sh")
    assert os.path.isfile(script_path), "docker/generate-lockfile.sh should exist"
    assert os.access(script_path, os.X_OK), "generate-lockfile.sh should be executable"


# ── HARD-02: Dependabot ──────────────────────────────────────────────────────


def test_dependabot_config_exists():
    """Dependabot configuration file exists."""
    path = os.path.join(_project_root(), ".github", "dependabot.yml")
    assert os.path.isfile(path), ".github/dependabot.yml should exist"


def test_dependabot_config_targets_docker_dir():
    """Dependabot is configured for pip ecosystem targeting /docker directory."""
    path = os.path.join(_project_root(), ".github", "dependabot.yml")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    assert "updates" in cfg, "dependabot.yml should have 'updates' key"
    pip_entries = [u for u in cfg["updates"] if u.get("package-ecosystem") == "pip"]
    assert len(pip_entries) > 0, "dependabot.yml should have pip ecosystem entry"
    assert pip_entries[0]["directory"] == "/docker", (
        "pip ecosystem should target /docker directory"
    )


# ── HARD-03: Shutdown watchdog ───────────────────────────────────────────────


def _extract_shutdown_function(gateway_source):
    """Extract the shutdown() function body from gateway.py source."""
    # Find the shutdown function definition and extract its body
    match = re.search(
        r"(    def shutdown\(_sig, _frame\):.*?)(?=\n    signal\.signal|\n    [a-zA-Z]|\nif __name__)",
        gateway_source,
        re.DOTALL,
    )
    assert match, "Could not find shutdown() function in gateway.py"
    return match.group(1)


def test_shutdown_handler_has_watchdog(gateway_source):
    """Shutdown function contains a threading.Timer watchdog and os._exit."""
    body = _extract_shutdown_function(gateway_source)
    assert "threading.Timer" in body, "shutdown() should use threading.Timer for watchdog"
    assert "os._exit" in body, "shutdown() should use os._exit for force-exit"


def test_shutdown_watchdog_timeout_is_5_seconds(gateway_source):
    """Shutdown watchdog uses a 5-second timeout."""
    body = _extract_shutdown_function(gateway_source)
    assert re.search(r"Timer\(5\.0", body) or re.search(r"Timer\(5,", body), (
        "shutdown watchdog should have 5-second timeout"
    )


def test_shutdown_watchdog_is_daemon(gateway_source):
    """Shutdown watchdog thread is a daemon (won't prevent exit)."""
    body = _extract_shutdown_function(gateway_source)
    assert "watchdog.daemon = True" in body, "watchdog should be a daemon thread"


def test_shutdown_watchdog_cancelled_on_success(gateway_source):
    """Shutdown watchdog is cancelled after successful cleanup."""
    body = _extract_shutdown_function(gateway_source)
    assert "watchdog.cancel()" in body, "watchdog should be cancelled on successful cleanup"
