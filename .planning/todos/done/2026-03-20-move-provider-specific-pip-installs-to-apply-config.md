---
created: 2026-03-20T01:27:37.464Z
title: "Move provider-specific pip installs to apply_config"
area: gateway
files:
  - docker/gateway.py:3261-3270
  - docker/requirements.txt
  - docker/mem0_config.py
---

## Problem

anthropic SDK is bundled in requirements.txt for ALL users, adding ~10s to every Docker build even for users on openai/groq/ollama. Only anthropic/claude-code users need it. mem0's anthropic LLM client crashes with ImportError if the package is missing, so it must be installed before mem0 initializes.

Same issue will recur for other provider-specific packages (google-generativeai, groq SDK, etc.) as mem0 adds providers.

## Solution

Move anthropic (and future provider-specific packages) out of requirements.txt. Install on demand in gateway.py's `apply_config()` when the user saves their provider choice in the setup wizard. Same pattern as the claude CLI install (`_setup_claude_cli()`).

Flow: setup wizard → save → `apply_config()` → detect provider → `pip install anthropic` if needed → restart goosed → mem0 works immediately.

No gap between setup and first use. No bloat for other providers.
