---
created: 2026-03-20T01:30:00.000Z
title: "Verify cheap model IDs for all providers"
area: gateway
files:
  - docker/mem0_config.py:30-42
---

## Problem

CHEAP_MODELS dict in mem0_config.py has model IDs that were guessed during phase 22 planning. The claude-haiku-4 ID was already wrong (had to fix to claude-haiku-4-5-20251001). Other providers likely have stale or incorrect model IDs too:

- groq: "llama-3.3-70b-versatile" — might be deprecated
- together: "meta-llama/Llama-3-8b-chat-hf" — old naming
- openrouter: "anthropic/claude-3-haiku-20240307" — old haiku
- deepseek: "deepseek-chat" — might have a cheaper option
- google: "gemini-2.0-flash" — verify still cheapest
- openai: "gpt-4.1-nano" — verify exists

Each wrong model ID causes mem0.add() to fail silently for that provider's users.

## Solution

Research current cheapest model for each provider via their API docs. Verify each model ID is valid by checking the provider's model list endpoint. Update CHEAP_MODELS dict and add a test that validates model ID format per provider.
