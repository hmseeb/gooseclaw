# GooseClaw

## What This Is

A self-hosted personal AI agent platform built on Block's Goose. Users deploy on Railway, configure via setup wizard, and interact through Telegram or any channel plugin. Gateway manages goose web lifecycle, job scheduling, notification bus, and channel plugin system. Supports 23+ LLM providers.

## Core Value

A user with zero DevOps knowledge can deploy GooseClaw and configure it correctly on the first try, every time. If they can paste an API key, they can run their own AI agent.

## Current Milestone: v2.0 Multi-Channel & Multi-Bot

**Goal:** Make channel plugins first-class citizens with full parity to telegram, and support multiple bots with independent provider/model configs on a single gateway.

**Target features:**
- Channel parity: shared command routing, per-user session locks, cancellation support for all plugins
- Multi-bot: multiple telegram bots on one gateway, each with own provider/model
- Per-channel provider routing: different channels can use different LLMs
- API parity: /api/notify and cron scheduler support channel targeting

## Requirements

### Validated

- ✓ Web-based setup wizard with 23+ providers — v1.0 phases 1-2
- ✓ API key validation, credential mapping, env var rehydration — v1.0 phase 2
- ✓ Gateway resilience: auto-restart, health monitor, stderr capture — v1.0 phase 3
- ✓ Advanced lead/worker multi-model config — v1.0 phase 4
- ✓ Production hardening: security headers, rate limiting, auth recovery — v1.0 phase 5
- ✓ Telegram gateway with session management, commands, streaming — v1.0
- ✓ Channel plugin system with hot-reload — v1.0
- ✓ Job engine with cron, timers, provider override, auto-expiry — v1.0
- ✓ Notification bus with per-job channel targeting — v1.0
- ✓ Per-channel verbosity settings — v1.0

### Active

(Defined in REQUIREMENTS.md for v2.0)

### Out of Scope

- Mobile-responsive wizard — desktop-first, Railway dashboard is desktop anyway
- Custom extension management in wizard — separate concern, goose web handles this
- OAuth flows (OpenRouter OAuth, GitHub Copilot device flow) — too complex for single HTML file
- Multiple goose web processes — single process, sessions provide isolation

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
