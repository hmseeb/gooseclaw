"""Credential detection, classification, and orchestration for auto-generated MCP extensions.

Scans text for credential-like patterns (API keys, app passwords, tokens),
classifies them to the correct template, and orchestrates the full
vault -> generate -> validate -> register pipeline.

Uses stdlib only in module scope. Lazy imports for internal modules.
"""

import logging
import os
import re
import subprocess
import sys

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("extension-detector")

# Known credential prefixes and their types
_PREFIX_PATTERNS = [
    (r"\bsk-[A-Za-z0-9_\-]{20,}", "api_key"),
    (r"\bpk-[A-Za-z0-9_\-]{20,}", "api_key"),
    (r"\bghp_[A-Za-z0-9]{36,}", "github_token"),
    (r"\bgho_[A-Za-z0-9]{36,}", "github_token"),
    (r"\bxoxb-[A-Za-z0-9\-]+", "slack_token"),
    (r"\bxoxp-[A-Za-z0-9\-]+", "slack_token"),
    (r"\bAKIA[A-Z0-9]{16}", "aws_key"),
    (r"\bglpat-[A-Za-z0-9_\-]{20,}", "gitlab_token"),
]

# App password pattern: 16 lowercase letters (with optional spaces every 4 chars)
_APP_PASSWORD_PATTERN = re.compile(r"\b([a-z]{4}\s+[a-z]{4}\s+[a-z]{4}\s+[a-z]{4})\b|(?<!\w)([a-z]{16})(?!\w)")

# Generic API key pattern: long alphanumeric string after credential keywords
_GENERIC_KEY_PATTERN = re.compile(
    r"(?:key|token|api|secret|password|credential)\s*[:=]\s*[\"']?([A-Za-z0-9_\-]{20,})[\"']?",
    re.IGNORECASE,
)

# Bearer token pattern
_BEARER_PATTERN = re.compile(r"\bBearer\s+([A-Za-z0-9_\-\.]{20,})")


def detect_credentials(text):
    """Scan text for credential-like patterns.

    Args:
        text: Input text to scan for credentials.

    Returns:
        List of dicts with keys: value, type, confidence.
        Empty list if no credentials detected.
    """
    results = []
    seen_values = set()

    # Check known prefixes first (highest confidence)
    for pattern, cred_type in _PREFIX_PATTERNS:
        for match in re.finditer(pattern, text):
            value = match.group(0).strip().strip("\"'")
            if value not in seen_values:
                seen_values.add(value)
                results.append({
                    "value": value,
                    "type": cred_type,
                    "confidence": 0.95,
                })

    # Check bearer tokens
    for match in _BEARER_PATTERN.finditer(text):
        value = match.group(1).strip().strip("\"'")
        if value not in seen_values:
            seen_values.add(value)
            results.append({
                "value": value,
                "type": "bearer_token",
                "confidence": 0.9,
            })

    # Check app passwords (16 lowercase letters, with or without spaces)
    for match in _APP_PASSWORD_PATTERN.finditer(text):
        value = (match.group(1) or match.group(2)).strip()
        # Normalize: remove spaces for the value
        normalized = value.replace(" ", "")
        if normalized not in seen_values and len(normalized) == 16:
            seen_values.add(normalized)
            results.append({
                "value": normalized,
                "type": "app_password",
                "confidence": 0.8,
            })

    # Check generic API key patterns (lower confidence)
    for match in _GENERIC_KEY_PATTERN.finditer(text):
        value = match.group(1).strip().strip("\"'")
        if value not in seen_values:
            seen_values.add(value)
            results.append({
                "value": value,
                "type": "api_key",
                "confidence": 0.7,
            })

    return results


def classify_credential(credential, user_hint=""):
    """Classify a detected credential to a template and vault configuration.

    Args:
        credential: Dict with keys: value, type, confidence.
        user_hint: Optional hint from user (e.g., "fastmail email", "github token").

    Returns:
        Dict with keys: template, vault_prefix, vault_keys, extension_name,
        description, extra_subs.
    """
    cred_type = credential.get("type", "api_key")
    hint_lower = user_hint.lower() if user_hint else ""

    # Email/IMAP classification
    email_keywords = ("mail", "fastmail", "email", "imap", "smtp", "gmail", "outlook")
    if cred_type == "app_password" and any(kw in hint_lower for kw in email_keywords):
        # Extract service name from hint
        service = "email"
        for kw in ("fastmail", "gmail", "outlook", "protonmail"):
            if kw in hint_lower:
                service = kw
                break

        prefix = service
        return {
            "template": "email_imap",
            "vault_prefix": prefix,
            "vault_keys": [
                f"{prefix}.imap_host",
                f"{prefix}.imap_user",
                f"{prefix}.app_password",
                f"{prefix}.smtp_host",
                f"{prefix}.smtp_user",
                f"{prefix}.smtp_password",
            ],
            "extension_name": f"email_{service}",
            "description": f"{service.title()} email access via IMAP/SMTP",
            "extra_subs": {},
        }

    # GitHub classification
    if cred_type == "github_token" or "github" in hint_lower:
        return {
            "template": "rest_api",
            "vault_prefix": "github",
            "vault_keys": ["github.api_key"],
            "extension_name": "github_api",
            "description": "GitHub API access",
            "extra_subs": {
                "auth_type": "bearer",
                "service_description": "GitHub API",
                "base_url": "https://api.github.com",
            },
        }

    # Slack classification
    if cred_type == "slack_token" or "slack" in hint_lower:
        return {
            "template": "rest_api",
            "vault_prefix": "slack",
            "vault_keys": ["slack.api_key"],
            "extension_name": "slack_api",
            "description": "Slack API access",
            "extra_subs": {
                "auth_type": "bearer",
                "service_description": "Slack API",
                "base_url": "https://slack.com/api",
            },
        }

    # AWS classification
    if cred_type == "aws_key" or "aws" in hint_lower:
        return {
            "template": "rest_api",
            "vault_prefix": "aws",
            "vault_keys": ["aws.api_key"],
            "extension_name": "aws_api",
            "description": "AWS API access",
            "extra_subs": {
                "auth_type": "header",
                "service_description": "AWS API",
                "base_url": "https://amazonaws.com",
            },
        }

    # GitLab classification
    if cred_type == "gitlab_token" or "gitlab" in hint_lower:
        return {
            "template": "rest_api",
            "vault_prefix": "gitlab",
            "vault_keys": ["gitlab.api_key"],
            "extension_name": "gitlab_api",
            "description": "GitLab API access",
            "extra_subs": {
                "auth_type": "bearer",
                "service_description": "GitLab API",
                "base_url": "https://gitlab.com/api/v4",
            },
        }

    # Default: generic API key
    # Derive prefix from hint or use "custom_api"
    if hint_lower:
        # Extract first word as service name
        prefix = re.sub(r"[^a-z0-9_]", "_", hint_lower.split()[0])
    else:
        prefix = "custom_api"

    return {
        "template": "rest_api",
        "vault_prefix": prefix,
        "vault_keys": [f"{prefix}.api_key"],
        "extension_name": f"{prefix}_api",
        "description": f"{prefix.replace('_', ' ').title()} API access",
        "extra_subs": {
            "auth_type": "bearer",
            "service_description": f"{prefix.replace('_', ' ').title()} API",
        },
    }


def credential_to_extension(credential_value, classification):
    """End-to-end pipeline: vault -> generate -> validate -> register.

    Args:
        credential_value: The raw credential string to vault.
        classification: Dict from classify_credential() with template, vault_prefix, etc.

    Returns:
        Dict with success status and details.
    """
    template = classification["template"]
    vault_prefix = classification["vault_prefix"]
    vault_keys = classification["vault_keys"]
    extension_name = classification["extension_name"]
    description = classification.get("description", "")
    extra_subs = classification.get("extra_subs", {})

    try:
        # Step A: Vault the credential
        # For email templates with multiple keys, only vault the app_password key
        if template == "email_imap":
            primary_key = f"{vault_prefix}.app_password"
            subprocess.run(
                ["secret", "set", primary_key, credential_value],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("Vaulted credential for %s at %s", extension_name, primary_key)
            # Return prompt for remaining fields
            return {
                "success": True,
                "partial": True,
                "extension_name": extension_name,
                "needs_input": [k for k in vault_keys if k != primary_key],
                "message": f"App password vaulted. Please provide: {', '.join(k.split('.')[-1] for k in vault_keys if k != primary_key)}",
            }
        else:
            primary_key = vault_keys[0]
            subprocess.run(
                ["secret", "set", primary_key, credential_value],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("Vaulted credential for %s at %s", extension_name, primary_key)

        # Step B: Generate and register extension
        # Lazy import to avoid circular imports
        from gateway import register_generated_extension
        server_path = register_generated_extension(
            template_name=template,
            extension_name=extension_name,
            vault_prefix=vault_prefix,
            vault_keys=vault_keys,
            description=description,
            extra_subs=extra_subs,
        )

        # Step C: Validate syntax
        from extensions.validator import validate_syntax
        valid, err_msg = validate_syntax(server_path)
        if not valid:
            logger.error("Extension %s failed syntax validation: %s", extension_name, err_msg)
            return {
                "success": False,
                "error": f"Syntax validation failed: {err_msg}",
                "stage": "validation",
            }

        # Step D: Health check
        from extensions.validator import health_check, check_and_disable, clear_failures
        ok, hc_err = health_check(server_path, timeout=15)
        if ok:
            clear_failures(extension_name)
            logger.info("Extension %s passed health check", extension_name)
        else:
            # Log warning but don't auto-disable on first health check during setup
            # Vault may not be fully populated yet in some environments
            logger.warning(
                "Extension %s failed health check during setup: %s (not disabling on first setup)",
                extension_name,
                hc_err,
            )

        # Step E: Success
        return {
            "success": True,
            "extension_name": extension_name,
            "server_path": server_path,
        }

    except subprocess.CalledProcessError as e:
        logger.error("Vault operation failed: %s", e)
        return {
            "success": False,
            "error": f"Vault operation failed: {e}",
            "stage": "vault",
        }
    except Exception as e:
        logger.error("Pipeline error for %s: %s", extension_name, e)
        return {
            "success": False,
            "error": str(e),
            "stage": "unknown",
        }
