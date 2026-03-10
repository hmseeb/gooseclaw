# GooseClaw Setup Wizard v2

## What This Is

A bulletproof onboarding experience for GooseClaw, the open-source personal AI agent deployment template. Users deploy GooseClaw on Railway, hit a setup wizard, pick their LLM provider, paste an API key, and have a working AI agent in under 2 minutes. The wizard handles all config.yaml generation, env var management, and goose web lifecycle so users never touch a terminal.

## Core Value

A user with zero DevOps knowledge can deploy GooseClaw and configure it correctly on the first try, every time. If they can paste an API key, they can run their own AI agent.

## Requirements

### Validated

- ✓ Web-based setup wizard served by gateway.py — existing
- ✓ Provider selection for 7 providers (anthropic, openai, google, groq, openrouter, claude-code, custom) — existing
- ✓ API key validation via test endpoint — existing
- ✓ Config persistence to setup.json on Railway volume — existing
- ✓ Telegram gateway integration — existing
- ✓ Basic auth for setup page after first config — existing
- ✓ Reverse proxy from gateway to goose web — existing

### Active

- [ ] Expand provider support from 7 to 15+ (add mistral, xai, deepseek, ollama, azure-openai, together, cerebras, perplexity)
- [ ] Categorized provider selection (Cloud API / Subscription / Local / Custom)
- [ ] Per-provider "how to get API key" links and descriptions
- [ ] Smart model selection with defaults and suggestions per provider
- [ ] Mandatory credential validation before save (non-empty + format check)
- [ ] Pre-fill form with existing values when reconfiguring
- [ ] Real-time startup status after save (not "refresh in a few seconds" forever)
- [ ] Auto-restart goose web on crash with exponential backoff
- [ ] Health check thread monitoring goose web process
- [ ] Proper error reporting to web UI (show actual goose web errors)
- [ ] Fix env var rehydration for ALL providers on container restart
- [ ] Telegram setup improvements (BotFather instructions, token format validation, pairing code in UI)
- [ ] Advanced settings toggle for lead/worker multi-model config
- [ ] Auth token recovery path (reset mechanism)
- [ ] Confirmation summary step showing what was configured
- [ ] Proper goose web stderr capture for debugging

### Out of Scope

- Multiple saved provider profiles with switching — adds complexity, goose only uses one at a time anyway
- Full multi-model UI (planner + subagent separate providers) — too complex for v2, advanced toggle covers lead/worker
- Mobile-responsive wizard — desktop-first, Railway dashboard is desktop anyway
- Custom extension management in wizard — separate concern, goose web handles this
- OAuth flows (OpenRouter OAuth, GitHub Copilot device flow) — too complex for single HTML file

## Context

GooseClaw is a Docker-based deployment template for Block's Goose AI agent. It runs on Railway with a persistent volume at /data. The architecture is:

- **entrypoint.sh**: Container startup, env var setup, starts gateway.py and telegram gateway
- **gateway.py**: Python HTTP server (stdlib only, no pip). Serves setup wizard, reverse proxies to goose web, manages goose web subprocess lifecycle
- **setup.html**: Single-file HTML/CSS/JS wizard. No build step, no npm
- **goose web**: Experimental goose CLI command that serves a chat UI on an internal port

Current bugs we've already fixed (committed but deployment was broken):
1. Env vars from setup.json not rehydrated on container restart
2. PATH missing ~/.local/bin for claude CLI
3. GOOSE_MODEL: default missing for claude-code provider

Key technical constraints:
- Python stdlib only (no pip install in gateway.py)
- Single HTML file (no build tooling)
- ubuntu:22.04 Docker base
- Railway volumes for persistence
- goose config.yaml uses GOOSE_ prefix keys
- Env vars override config.yaml

## Constraints

- **No build tooling**: setup.html must be self-contained HTML/CSS/JS
- **Python stdlib only**: gateway.py cannot use pip packages
- **Railway compatible**: Must work with Railway volumes, PORT env var, health checks
- **Goose binary**: Pre-installed in Dockerfile, version may vary
- **Single provider**: Goose uses one GOOSE_PROVIDER + GOOSE_MODEL at a time
- **goose web experimental**: May crash, needs resilience layer

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Option A: one provider + easy reconfigure | Goose only uses one provider at a time, profiles add UX complexity | -- Pending |
| Advanced toggle for lead/worker | Power users get multi-model without cluttering main flow | -- Pending |
| Skip OAuth flows (OpenRouter, Copilot) | Too complex for single HTML file, can add later | -- Pending |
| Keep Python stdlib only | No pip in container, keeps gateway.py simple and portable | -- Pending |
| Provider categories in UI | Reduces decision paralysis for new users | -- Pending |

---
*Last updated: 2026-03-10 after initialization*
