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
