"""Tests for infrastructure hardening (Phase 20).

HARD-01: Hash-pinned dependency lock file
HARD-02: Dependabot CVE scanning configuration
HARD-03: Graceful shutdown watchdog
HARD-05: JSON log formatter
HARD-06: Security-sensitive structured logging
"""

import json
import logging
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


# ── HARD-05: JSON log formatter ──────────────────────────────────────────────


def test_json_formatter_produces_valid_json(gateway_module):
    """JSONFormatter.format() produces valid JSON with required fields."""
    fmt = gateway_module.JSONFormatter()
    record = logging.LogRecord("test", logging.INFO, "", 0, "hello world", (), None)
    output = fmt.format(record)
    parsed = json.loads(output)
    assert "ts" in parsed, "JSON log should have 'ts' field"
    assert parsed["level"] == "info", "JSON log level should be 'info'"
    assert parsed["component"] == "test", "JSON log component should match logger name"
    assert parsed["msg"] == "hello world", "JSON log msg should match record message"


def test_json_formatter_includes_extra_fields(gateway_module):
    """JSONFormatter includes extra fields like event and ip."""
    fmt = gateway_module.JSONFormatter()
    record = logging.LogRecord("auth", logging.WARNING, "", 0, "login failed", (), None)
    record.event = "auth_failure"
    record.ip = "192.168.1.1"
    output = fmt.format(record)
    parsed = json.loads(output)
    assert parsed.get("event") == "auth_failure", "JSON log should include event extra"
    assert parsed.get("ip") == "192.168.1.1", "JSON log should include ip extra"


def test_json_formatter_includes_exception(gateway_module):
    """JSONFormatter includes error and traceback for exceptions."""
    fmt = gateway_module.JSONFormatter()
    try:
        raise ValueError("test error")
    except ValueError:
        import sys
        exc_info = sys.exc_info()
    record = logging.LogRecord("test", logging.ERROR, "", 0, "something broke", (), exc_info)
    output = fmt.format(record)
    parsed = json.loads(output)
    assert "error" in parsed, "JSON log should have 'error' key for exceptions"
    assert "traceback" in parsed, "JSON log should have 'traceback' key for exceptions"
    assert "ValueError" in parsed["error"], "Error should contain exception type"


# ── HARD-06: Print migration and structured logging ──────────────────────────


def test_no_print_calls_in_gateway(gateway_source):
    """No print() calls remain in gateway.py (all migrated to logging)."""
    # Find print( calls that are not in comments
    matches = re.findall(r"^[^#]*\bprint\(", gateway_source, re.MULTILINE)
    assert len(matches) == 0, (
        f"Found {len(matches)} print() calls in gateway.py, expected 0"
    )


def test_security_logging_uses_structured_format(gateway_source):
    """Security-sensitive loggers (auth, session, config) are used in gateway.py."""
    assert "_auth_log." in gateway_source, "gateway.py should use _auth_log for auth events"
    assert "_session_log." in gateway_source, "gateway.py should use _session_log for session events"
    assert "_config_log." in gateway_source, "gateway.py should use _config_log for config events"


def test_logging_outputs_to_stdout(gateway_source):
    """Logging StreamHandler writes to sys.stdout (not stderr)."""
    assert "StreamHandler(sys.stdout)" in gateway_source, (
        "Log handler should write to sys.stdout"
    )
